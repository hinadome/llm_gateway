"""API dependencies: credential extraction + the authentication chain."""

from __future__ import annotations

import ipaddress
import logging
from typing import Annotated

from fastapi import Depends, Header, Request

from ..core.auth import AuthChain, hash_key  # noqa: F401 — hash_key re-exported
from ..core.context import AuthContext
from ..errors import AuthError, GatewayError

logger = logging.getLogger("gateway.auth")


# Data-plane endpoint scopes (a virtual key's allowed_endpoints entries).
# "chat" covers every chat-shaped surface — OpenAI chat, the Responses
# bridge, and the native Anthropic/Gemini inbound formats — because they
# all exercise the same underlying completions machinery.
ENDPOINT_SCOPES = frozenset({"chat", "embeddings", "images", "audio", "models"})


def required_scope(path: str) -> str | None:
    """The data-plane scope a request path belongs to (None = unscoped).

    Pure function so tests and admins can reason about the mapping.
    """
    if path == "/v1/embeddings":
        return "embeddings"
    if path.startswith("/v1/images/"):
        return "images"
    if path.startswith("/v1/audio/"):
        return "audio"
    last = path.rsplit("/", 1)[-1]
    if path == "/v1/models" or (path.startswith("/v1/models/") and ":" not in last):
        return "models"  # OpenAI model objects (not Gemini's /v1/models/x:generate)
    if path.startswith("/v1/") or path.startswith("/v1beta/"):
        return "chat"
    return None  # admin plane / metrics / docs — guarded elsewhere


def _ip_allowed(host: str | None, allowed_ips: list[str]) -> bool:
    """Whether the caller's address matches any plain IP / CIDR entry.

    Malformed entries never match (they are admin input; a typo restricts
    rather than opens). No client address + a restriction set = denied.
    """
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    for entry in allowed_ips:
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            logger.warning("allowed_ips entry '%s' is not a valid IP/CIDR", entry)
    return False


def _extract_raw_key(
    authorization: str | None,
    x_api_key: str | None,
    x_goog_api_key: str | None = None,
    api_key: str | None = None,
) -> str | None:
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    # Gemini SDKs send the key as x-goog-api-key (or ?key= — read in require_api_key)
    if x_goog_api_key and x_goog_api_key.strip():
        return x_goog_api_key.strip()
    # Azure OpenAI SDKs send the key as `api-key: <key>`
    if api_key and api_key.strip():
        return api_key.strip()
    return None


async def require_api_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
    x_goog_api_key: Annotated[str | None, Header()] = None,
    api_key: Annotated[str | None, Header(alias="api-key")] = None,
) -> AuthContext:
    """Authenticate the caller via the configured chain (``auth.chain``).

    Credential extraction is transport-level and shared by every strategy:
    ``Authorization: Bearer`` (OpenAI SDKs), ``x-api-key`` (Anthropic SDKs),
    ``x-goog-api-key`` / ``?key=`` (Gemini SDKs) and ``api-key`` (Azure
    OpenAI SDKs) — one credential namespace for every inbound wire format.
    The chain (``app.state.auth_chain``, built by ``create_app``) decides
    what that credential is: admin key, virtual key, JWT/OAuth token, or a
    custom authenticator.
    """
    raw = _extract_raw_key(authorization, x_api_key, x_goog_api_key, api_key)
    if not raw:
        # Gemini SDKs may pass the key as a query parameter instead
        raw = (request.query_params.get("key") or "").strip() or None
    if not raw:
        raise AuthError(
            "Missing API key. Send 'Authorization: Bearer <key>', 'x-api-key: <key>', "
            "'x-goog-api-key: <key>' or 'api-key: <key>'."
        )

    chain: AuthChain | None = getattr(request.app.state, "auth_chain", None)
    if chain is None:
        raise GatewayError(
            500, "Auth chain is not configured on this app.", "api_error"
        )
    auth = await chain.authenticate(raw)

    # Token scoping (never applied to admin contexts): where the call may
    # come from, and which parts of the data plane the key may touch.
    if not auth.is_admin:
        if auth.allowed_ips is not None and not _ip_allowed(
            (request.client.host if request.client else None), auth.allowed_ips
        ):
            raise GatewayError(
                403,
                "This API key is not allowed to call from this IP address.",
                "invalid_request_error",
                code="ip_not_allowed",
            )
        if auth.allowed_endpoints is not None:
            scope = required_scope(request.url.path)
            if scope is not None and scope not in auth.allowed_endpoints:
                raise GatewayError(
                    403,
                    f"This API key does not have access to the '{scope}' endpoint "
                    f"scope (allowed: {', '.join(auth.allowed_endpoints) or 'none'}).",
                    "invalid_request_error",
                    code="endpoint_not_allowed",
                )
    return auth


async def require_admin(auth: Annotated[AuthContext, Depends(require_api_key)]) -> AuthContext:
    if not auth.is_admin:
        raise GatewayError(
            status_code=403,
            message="Admin access required for this endpoint.",
            error_type="invalid_request_error",
            code="forbidden",
        )
    return auth
