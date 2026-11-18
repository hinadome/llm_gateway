"""Realtime (WebSocket) surface: ``WS /v1/realtime?model=...``.

The fifth inbound transport, and the first that is not one-request/
one-response: a session. The route is deliberately thin — authenticate
(same chain, same credential namespace, including ``?key=`` because
browsers cannot set headers), then hand the socket to the pipeline
(``GatewayPipeline.handle_realtime``), which runs the open gates and
the event relay (``core/realtime.py``).

Rejection semantics: every failure to OPEN a session (auth, scope,
model permission, rate limit, budget, no realtime-capable deployment,
handshake refused) is accepted-first, then delivered as an OpenAI-
shaped realtime ``error`` event followed by close 1008 with the short
reason — the session-shaped equivalent of the HTTP error envelopes,
so a client with no access to the HTTP surface still gets a
debuggable message instead of a bare handshake refusal.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress

from fastapi import APIRouter, WebSocket

from ..core.auth import AuthChain
from ..core.context import AuthContext
from ..errors import AuthError, GatewayError
from .deps import _credential_hint, _extract_raw_key, _ip_allowed

router = APIRouter()
logger = logging.getLogger("gateway.realtime")

_PATH = "/v1/realtime"


async def authenticate_websocket(ws: WebSocket) -> AuthContext:
    """The WebSocket twin of ``require_api_key``: the same chain, the
    same credential extraction (``Authorization: Bearer``, ``x-api-key``,
    ``x-goog-api-key``, ``api-key`` headers — and ``?key=`` for clients
    that cannot set headers, browsers first among them), the same IP
    and endpoint-scope checks. Raises AuthError/GatewayError; the
    route translates those into error events + close."""
    headers = ws.headers
    raw = _extract_raw_key(
        headers.get("authorization"),
        headers.get("x-api-key"),
        headers.get("x-goog-api-key"),
        headers.get("api-key"),
    )
    if not raw:
        raw = (ws.query_params.get("key") or "").strip() or None
    host = ws.client.host if ws.client else None
    if not raw:
        logger.warning(
            "auth rejected: WS %s from %s — no credential presented",
            _PATH,
            host or "unknown",
        )
        events = getattr(ws.app.state, "events", None)
        if events is not None:
            await events.emit(
                "auth.rejected", None, path=_PATH, reason="no credential presented"
            )
        raise AuthError(
            "Missing API key. Connect with 'Authorization: Bearer <key>' "
            "(or 'x-api-key' / 'x-goog-api-key' / 'api-key'), or pass "
            "'?key=<key>' in the query string — browsers cannot set "
            "WebSocket headers."
        )
    chain: AuthChain | None = getattr(ws.app.state, "auth_chain", None)
    if chain is None:
        raise GatewayError(500, "Auth chain is not configured on this app.", "api_error")
    try:
        auth = await chain.authenticate(raw)
    except AuthError as exc:
        logger.warning(
            "auth rejected: WS %s from %s — %s (credential %s)",
            _PATH,
            host or "unknown",
            exc.message,
            _credential_hint(raw),
        )
        events = getattr(ws.app.state, "events", None)
        if events is not None:
            await events.emit("auth.rejected", None, path=_PATH, reason=exc.message)
        raise

    # Scoping, exactly as require_api_key applies it (never to admin
    # contexts): where the call may come from, and which parts of the
    # data plane the key may touch. Realtime is its own scope.
    if not auth.is_admin:
        if auth.allowed_ips is not None and not _ip_allowed(host, auth.allowed_ips):
            logger.warning(
                "authz rejected: WS %s from %s — key '%s' is not allowed "
                "to call from this IP address",
                _PATH,
                host or "unknown",
                auth.key_name,
            )
            raise GatewayError(
                403,
                "This API key is not allowed to call from this IP address.",
                "invalid_request_error",
                code="ip_not_allowed",
            )
        if auth.allowed_endpoints is not None and "realtime" not in auth.allowed_endpoints:
            logger.warning(
                "authz rejected: WS %s from %s — key '%s' lacks the "
                "'realtime' endpoint scope",
                _PATH,
                host or "unknown",
                auth.key_name,
            )
            raise GatewayError(
                403,
                "This API key does not have access to the 'realtime' endpoint "
                f"scope (allowed: {', '.join(auth.allowed_endpoints) or 'none'}).",
                "invalid_request_error",
                code="endpoint_not_allowed",
            )
    return auth


async def _reject(ws: WebSocket, exc: GatewayError) -> None:
    """Session-fatal failure: an OpenAI-shaped realtime error event,
    then close 1008 (policy) with the short reason. Accept-first means
    the client actually receives both instead of a bare handshake
    refusal (reasons are capped ~120 bytes by the protocol)."""
    with suppress(Exception):
        await ws.send_text(
            json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": exc.error_type,
                        "code": exc.code,
                        "message": exc.message[:500],
                    },
                }
            )
        )
    with suppress(Exception):
        await ws.close(code=1008, reason=(exc.message or "rejected")[:120])


@router.websocket("/v1/realtime")
async def realtime_session(websocket: WebSocket) -> None:
    """One realtime session. Accept, authenticate, gate, relay; every
    open-failure is an error event + close(1008), every normal
    disconnect returns quietly (the pipeline's teardown wrote the row)."""
    # Accept first: rejections carry their reason in-band.
    await websocket.accept()
    try:
        auth = await authenticate_websocket(websocket)
    except (AuthError, GatewayError) as exc:
        await _reject(websocket, exc)
        return
    model = (websocket.query_params.get("model") or "").strip()
    if not model:
        await _reject(
            websocket,
            GatewayError(
                400,
                "Missing 'model' query parameter — connect to "
                "/v1/realtime?model=<public-model>.",
                "invalid_request_error",
            ),
        )
        return
    try:
        await websocket.app.state.pipeline.handle_realtime(websocket, model, auth)
    except GatewayError as exc:
        # Open-gate failures (permission, rate limit, budget, routing)
        # surface here — the session-shaped error envelope.
        await _reject(websocket, exc)
    except Exception:
        logger.exception("realtime session crashed")
        with suppress(Exception):
            await websocket.close(code=1011, reason="internal error")
