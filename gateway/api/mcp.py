"""MCP proxy surface: ``POST/GET/DELETE /v1/mcp/{server}``.

The sixth inbound surface, and the first whose payload is not for the
gateway's models at all: a transparent Streamable-HTTP proxy between
MCP clients and operator-configured MCP servers (Model Context
Protocol, 2025-06-18 transport). The route is deliberately thin —
authenticate (the same chain, the same credential namespace, `?key=`
included, so browser-based MCP clients work), validate the JSON-RPC
envelope, then hand the message to the pipeline
(``GatewayPipeline.handle_mcp``), which runs the gates and the
protocol mechanics (``core/mcp.py``).

Envelope validation happens here, before any policy: a body that is
not valid JSON (-32700), an array (batching was REMOVED from the
transport spec — one message per POST), or not a JSON-RPC 2.0 object
(-32600) answers 400 in the protocol envelope (id: null) so MCP client
SDKs can parse the rejection.

Rejection split (documented in the README's MCP section): GATEWAY
failures keep the gateway's OpenAI-shaped error envelope (auth, the
``mcp`` endpoint scope, disabled/unknown/not-allowed server, rate
limit, concurrency — the operator's concerns, identical to every other
surface), while PROTOCOL-level failures use the JSON-RPC envelope (the
codes above, missing/unknown ``Mcp-Session-Id``, and in-band tool
denials — the caller's concerns). DELETE terminates a session (local
mapping first, then the upstream); GET is 405 — the standalone
server-to-client stream (sampling/elicitation outside an open POST
stream) is out of scope, which the transport spec explicitly permits.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from ..core.auth import AuthContext
from ..core.mcp import (
    INVALID_REQUEST,
    PARSE_ERROR,
    classify,
    jsonrpc_error,
)
from .deps import require_api_key

router = APIRouter()


def _request_error(code: int, message: str) -> JSONResponse:
    """A transport-level 400 in the protocol envelope (id: null — the
    request could not be parsed into anything that has one)."""
    return JSONResponse(jsonrpc_error(None, code, message), status_code=400)


@router.post("/v1/mcp/{server}")
async def mcp_post(
    server: str,
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
) -> Response:
    body = await raw_request.body()
    try:
        message = json.loads(body)
    except ValueError:
        return _request_error(PARSE_ERROR, "Parse error: the request body is not valid JSON.")
    if isinstance(message, list):
        return _request_error(
            INVALID_REQUEST,
            "Invalid Request: JSON-RPC batching is not supported "
            "(removed in MCP 2025-06-18) — send one message per POST.",
        )
    if classify(message) is None:
        return _request_error(
            INVALID_REQUEST,
            "Invalid Request: not a JSON-RPC 2.0 message "
            "(expected an object with jsonrpc/method/id).",
        )
    pipeline = raw_request.app.state.pipeline
    return await pipeline.handle_mcp(
        message,
        auth,
        server,
        raw_request.app.state.mcp_registry,
        session_header=raw_request.headers.get("mcp-session-id"),
        request_id=getattr(raw_request.state, "request_id", None),
    )


@router.delete("/v1/mcp/{server}")
async def mcp_delete(
    server: str,
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
) -> Response:
    pipeline = raw_request.app.state.pipeline
    return await pipeline.mcp_terminate(
        server,
        raw_request.headers.get("mcp-session-id"),
        raw_request.app.state.mcp_registry,
        auth,
    )


@router.get("/v1/mcp/{server}")
async def mcp_get(
    server: str,
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
) -> Response:
    # The standalone server->client stream (server-initiated requests
    # outside an open POST stream) is out of scope; the transport spec
    # allows a 405 here. Mirrors the disabled surface's 404 so the
    # route does not exist as a side channel when the proxy is off.
    if not raw_request.app.state.config.mcp.enabled:
        from ..errors import MCPDisabledError

        raise MCPDisabledError()
    return Response(status_code=405, headers={"Allow": "POST, DELETE"})
