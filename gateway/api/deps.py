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
# all exercise the same underlying completions machinery. "batch" covers
# the files/batches surfaces (their lines execute as chat anyway, but
# the scope is granted separately so a key may chat without batching).
# "realtime" is the WebSocket session surface (/v1/realtime): granted
# separately like batch — a key may chat without holding live sessions,
# and sessions consume concurrency slots for their whole life.
ENDPOINT_SCOPES = frozenset(
    {"chat", "embeddings", "images", "audio", "models", "batch", "realtime", "mcp"}
)


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
    if path == "/v1/files" or path.startswith("/v1/files/"):
        return "batch"
    if path == "/v1/batches" or path.startswith("/v1/batches/"):
        return "batch"
    # The WebSocket session surface (its own scope: sessions hold
    # concurrency slots for their whole life, so a key may chat
    # without being allowed to hold live sessions).
    if path == "/v1/realtime":
        return "realtime"
    # The MCP proxy surface: its own scope for the same reason — a key
    # that may chat or declare tools is not automatically allowed to
    # drive MCP servers (which hold upstream sessions of their own).
    if path == "/v1/mcp" or path.startswith("/v1/mcp/"):
        return "mcp"
    # Feedback is commentary on chat traffic — the chat scope covers it
    # (a key that may rate is a key that may chat).
    if path == "/v1/feedback":
        return "chat"
    last = path.rsplit("/", 1)[-1]
    if last.endswith(":embedContent") or last.endswith(":batchEmbedContents"):
        return "embeddings"  # Gemini-native embeddings (not chat, despite :method path)
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


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _credential_hint(credential: str) -> str:
    """A short, non-sensitive credential hint for rejection logs.

    The same shape as the ``key_prefix`` column (never the full
    secret): enough to tell *which* credential was tried — an old
    revoked key vs. a typo vs. a different client — without exposing
    it. 12 chars cover the whole ``sk-gw-`` namespace plus a few.
    """
    return credential[:12] + ("…" if len(credential) > 12 else "")


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

    Every rejection is logged (WARNING, one line) with the method, path,
    client address, the client-facing reason and a redacted credential
    hint: the response body tells the caller why, this tells whoever is
    watching the service — uvicorn's access log shows only the bare
    status code, and failed auth never reaches the request-log pipeline.
    """
    raw = _extract_raw_key(authorization, x_api_key, x_goog_api_key, api_key)
    if not raw:
        # Gemini SDKs may pass the key as a query parameter instead
        raw = (request.query_params.get("key") or "").strip() or None
    if not raw:
        logger.warning(
            "auth rejected: %s %s from %s — no credential presented",
            request.method,
            request.url.path,
            _client_host(request),
        )
        events = getattr(request.app.state, "events", None)
        if events is not None:
            await events.emit(
                "auth.rejected",
                None,
                path=request.url.path,
                reason="no credential presented",
            )
        raise AuthError(
            "Missing API key. Send 'Authorization: Bearer <key>', 'x-api-key: <key>', "
            "'x-goog-api-key: <key>' or 'api-key: <key>'."
        )

    chain: AuthChain | None = getattr(request.app.state, "auth_chain", None)
    if chain is None:
        raise GatewayError(
            500, "Auth chain is not configured on this app.", "api_error"
        )
    try:
        auth = await chain.authenticate(raw)
    except AuthError as exc:
        logger.warning(
            "auth rejected: %s %s from %s — %s (credential %s)",
            request.method,
            request.url.path,
            _client_host(request),
            exc.message,
            _credential_hint(raw),
        )
        events = getattr(request.app.state, "events", None)
        if events is not None:
            await events.emit(
                "auth.rejected",
                None,
                path=request.url.path,
                reason=exc.message,
            )
        raise

    # Token scoping (never applied to admin contexts): where the call may
    # come from, and which parts of the data plane the key may touch.
    if not auth.is_admin:
        if auth.allowed_ips is not None and not _ip_allowed(
            (request.client.host if request.client else None), auth.allowed_ips
        ):
            logger.warning(
                "authz rejected: %s %s from %s — key '%s' is not allowed "
                "to call from this IP address (allowed_ips: %s)",
                request.method,
                request.url.path,
                _client_host(request),
                auth.key_name,
                ", ".join(auth.allowed_ips),
            )
            raise GatewayError(
                403,
                "This API key is not allowed to call from this IP address.",
                "invalid_request_error",
                code="ip_not_allowed",
            )
        if auth.allowed_endpoints is not None:
            scope = required_scope(request.url.path)
            if scope is not None and scope not in auth.allowed_endpoints:
                logger.warning(
                    "authz rejected: %s %s from %s — key '%s' lacks the "
                    "'%s' endpoint scope (allowed: %s)",
                    request.method,
                    request.url.path,
                    _client_host(request),
                    auth.key_name,
                    scope,
                    ", ".join(auth.allowed_endpoints) or "none",
                )
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


async def require_platform_admin(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> AuthContext:
    """Admin *and* outside every tenant — the operator.

    Endpoints that configure the platform itself (tenants, prompt
    library, routing, guardrail inspection, hot reload, backups) take
    this dependency: a tenant admin may manage their own tenant's
    keys and read its logs/stats, but the platform's policy is not
    theirs to change. 403, not 404 — tenant admins know these
    endpoints exist (they ship in the same API surface).
    """
    if auth.tenant_id is not None:
        raise GatewayError(
            status_code=403,
            message="Platform administrator access required — tenant "
            "administrators cannot manage platform settings.",
            error_type="invalid_request_error",
            code="forbidden",
        )
    return auth
