"""MCP proxy machinery (tier 33) — the protocol half of ``/v1/mcp``.

The pipeline (``handle_mcp``) owns the policy spine — gates, tool
policy, guardrail phases, accounting. This module owns everything that
is *protocol mechanics* instead, so the MCP wire format lives in one
place just as the realtime event protocol lives in ``core/realtime.py``:

* JSON-RPC message classification and the error envelope the client
  SDKs parse (``classify`` / ``jsonrpc_error`` / ``sse_frame``);
* the session registry — the gateway issues its OWN ``Mcp-Session-Id``
  per ``initialize`` and maps it to the upstream's session plus the
  negotiated protocol version (the client's id never travels upstream,
  so a gateway restart or id rotation breaks nothing upstream);
* the upstream header contract (configured server auth, the session
  and protocol-version stamps — never the caller's gateway credential);
* the incremental SSE frame parser used to relay upstream streams
  in-band: notifications and server-initiated requests pass through
  VERBATIM (raw frame bytes preserved), THE response frame is handed to
  the pipeline's interceptor, and keepalive comments survive.

Transport behavior follows MCP 2025-06-18 Streamable HTTP: one
JSON-RPC message per POST (batching was removed), notifications and
responses answered 202, ``Mcp-Session-Id`` required after initialize
(400 missing / 404 unknown), ``MCP-Protocol-Version`` stamped on every
post-initialize upstream call, DELETE terminates a session, and the
server may answer either ``application/json`` or ``text/event-stream``.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..config import MCPServerConfig
from ..observability import metrics


# JSON-RPC / MCP application error codes (the client-SDK-visible
# vocabulary; documented in the README's MCP section). The -32xxx range
# is JSON-RPC's "server error" reservation.
PARSE_ERROR = -32700  # body was not valid JSON (transport-level 400)
INVALID_REQUEST = -32600  # not a JSON-RPC 2.0 message, or a batch array
SESSION_REQUIRED = -32000  # post-initialize message without a session header
TOOL_NOT_ALLOWED = -32001  # tools/call outside the key's tool policy
GUARDRAIL_BLOCKED = -32002  # a guardrail blocked the call's args or result
SESSION_TERMINATED = -32003  # unknown/expired Mcp-Session-Id (404)

# The JSON-RPC methods that move content worth guarding (tier 49):
# caller-controlled text in (a URI, prompt arguments, a completion
# argument), server-generated text out (resource bodies, prompt
# messages, completion values). tools/call guards its own path. Every
# other method — lifecycle (initialize), notifications, catalog reads
# (tools/list, resources/list, prompts/list, resources/templates/list),
# ping — passes through untouched by design: nothing the caller wrote
# beyond a cursor, nothing the server generated beyond static catalog
# descriptions.
MCP_CONTENT_METHODS: frozenset[str] = frozenset(
    {"resources/read", "prompts/get", "completion/complete"}
)


def jsonrpc_error(
    message_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    """The JSON-RPC error envelope — the body MCP clients parse. Used
    in-band (HTTP 200 answering a request whose tool call was denied —
    the client's JSON-RPC layer must see the reason, a transport 4xx
    would hide it) and for transport-level failures (missing/unknown
    session, parse errors), where ``message_id`` is None/invalid."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


def classify(message: Any) -> str | None:
    """Classify one parsed JSON-RPC message: ``'request'`` (method + id),
    ``'notification'`` (method, no id), ``'response'`` (id, no method —
    a client answering a server-initiated request) or None (not a
    JSON-RPC 2.0 object)."""
    if not isinstance(message, dict):
        return None
    if message.get("jsonrpc") != "2.0":
        return None
    has_method = isinstance(message.get("method"), str)
    has_id = "id" in message
    if has_method and has_id:
        return "request"
    if has_method:
        return "notification"
    if has_id:
        return "response"
    return None


def sse_frame(message: dict[str, Any], event: str | None = None) -> bytes:
    """Serialize one JSON-RPC message as an SSE frame (``event: message``
    is MCP's default event type)."""
    return f"event: {event or 'message'}\ndata: {json.dumps(message)}\n\n".encode()


def parse_jsonrpc_message(raw: bytes, content_type: str) -> dict[str, Any] | None:
    """The one JSON-RPC message from an upstream answer, whether the
    server chose ``application/json`` (the body IS the message) or SSE
    (frames until the response frame; anything after it belongs to the
    out-of-scope standalone stream). None = no message found. Used by
    ``initialize``, which consumes the upstream answer itself and
    re-emits JSON to the client."""
    if "text/event-stream" in content_type:
        reader = SSEReader()
        for frame in reader.feed(raw):
            if frame.data is None:
                continue
            try:
                message = json.loads(frame.data)
            except ValueError:
                continue
            if classify(message) == "response":
                return message
        return None
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def upstream_headers(server: MCPServerConfig, session: "MCPSession | None") -> dict[str, str]:
    """Headers for one upstream call: the configured server auth (the
    operator's concern — the caller's gateway credential NEVER travels
    upstream), the JSON-RPC content types the transport requires, and
    the session/protocol-version stamps for everything post-initialize
    (per the spec the gateway, as the server's client, quotes the
    upstream session and the version negotiated at initialize)."""
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **server.resolve_headers(),
    }
    if session is not None:
        if session.upstream_session_id:
            headers["Mcp-Session-Id"] = session.upstream_session_id
        if session.protocol_version:
            headers["MCP-Protocol-Version"] = session.protocol_version
    return headers


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


@dataclass
class MCPSession:
    """One proxy session: the client's gateway-issued ``Mcp-Session-Id``
    mapped to the upstream's session id and the protocol version the
    upstream negotiated. ``upstream_session_id`` may be None (an
    upstream that does not use sessions — the gateway still issues its
    own id so the client's lifecycle is uniform)."""

    gateway_session_id: str
    server_name: str
    upstream_session_id: str | None
    protocol_version: str | None
    created_at: float
    last_used_at: float


class MCPSessionRegistry:
    """Gateway session ids → upstream sessions. Lives on ``app.state``,
    created once per app and surviving hot-reloads: it is the gateway's
    OWN state, not config-derived, so a reload never strands a client
    mid-session. A server removed from the config evicts lazily — the
    next request 404s at the config gate and the client re-initializes.

    Single event loop, no awaits between check and mutate: a plain dict
    with no locks (the same reasoning as the rate limiter's windows).
    """

    def __init__(self) -> None:
        self._sessions: dict[str, MCPSession] = {}

    def create(
        self,
        server_name: str,
        upstream_session_id: str | None,
        protocol_version: str | None,
    ) -> MCPSession:
        sid = f"gw_{uuid.uuid4().hex}"
        now = time.time()
        session = MCPSession(
            gateway_session_id=sid,
            server_name=server_name,
            upstream_session_id=upstream_session_id,
            protocol_version=protocol_version,
            created_at=now,
            last_used_at=now,
        )
        self._sessions[sid] = session
        metrics.MCP_SESSIONS.labels(server=server_name).inc()
        return session

    def get(self, gateway_session_id: str) -> MCPSession | None:
        """Pure lookup — no implicit touch, so the TTL sweep can inspect
        without extending (the pipeline touches explicitly, once it has
        decided to serve the request)."""
        return self._sessions.get(gateway_session_id)

    @staticmethod
    def touch(session: MCPSession) -> None:
        session.last_used_at = time.time()

    def drop(self, gateway_session_id: str) -> MCPSession | None:
        session = self._sessions.pop(gateway_session_id, None)
        if session is not None:
            metrics.MCP_SESSIONS.labels(server=session.server_name).dec()
        return session

    def sweep(self, ttl_seconds: float) -> list[MCPSession]:
        """Evict sessions idle past the TTL (the lifespan loop's pass).
        Returns them so the caller can emit the closed event and try a
        best-effort upstream DELETE — per the transport spec, a client
        that comes back after its session expired simply re-initializes."""
        now = time.time()
        expired = [
            session
            for session in self._sessions.values()
            if now - session.last_used_at > ttl_seconds
        ]
        for session in expired:
            self.drop(session.gateway_session_id)
        return expired

    def live_counts(self) -> dict[str, int]:
        """Open sessions per server name (/admin/mcp introspection)."""
        counts: dict[str, int] = {}
        for session in self._sessions.values():
            counts[session.server_name] = counts.get(session.server_name, 0) + 1
        return counts


class MCPToolBlocked(Exception):
    """A guarded MCP message denied by gateway policy — carried as a
    plain exception (NOT a GatewayError) so the exchange turns it into
    an IN-BAND JSON-RPC error — HTTP 200 with the error body, or an SSE
    error frame — instead of a transport-level failure. A transport 4xx
    would hide the reason from the client's JSON-RPC layer; the agent
    at the other end needs to see *which* message (``method``, and the
    tool name for tools/call) and *why*.
    """

    def __init__(
        self,
        code: int,
        reason: str,
        *,
        tool: str | None = None,
        guardrail: str | None = None,
        method: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.tool = tool
        self.guardrail = guardrail
        # The JSON-RPC method the denial belongs to (tools/call,
        # resources/read, ...) — None when the blocker did not say.
        self.method = method


# --------------------------------------------------------------------------
# SSE
# --------------------------------------------------------------------------


@dataclass
class SSEFrame:
    event: str | None
    data: str | None
    # The frame's raw text INCLUDING its boundary — non-intercepted
    # frames relay byte-identical, preserving upstream formatting.
    raw: str


class SSEReader:
    """Incremental SSE parser: ``feed()`` bytes in, complete frames out.

    Handles ``\\n\\n`` and ``\\r\\n\\r\\n`` frame boundaries, multi-line
    ``data:`` (joined with newlines per the SSE spec), ``event:`` names
    and ``:``-prefixed comment/keepalive frames (``data is None`` —
    relayed verbatim so client read timeouts never fire)."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, chunk: bytes) -> list[SSEFrame]:
        self._pending += chunk.decode("utf-8", errors="replace")
        frames: list[SSEFrame] = []
        while True:
            boundary = self._find_boundary(self._pending)
            if boundary is None:
                break
            end, length = boundary
            raw = self._pending[: end + length]
            self._pending = self._pending[end + length :]
            frames.append(self._parse(raw))
        return frames

    @staticmethod
    def _find_boundary(pending: str) -> tuple[int, int] | None:
        lf = pending.find("\n\n")
        crlf = pending.find("\r\n\r\n")
        if crlf != -1 and (lf == -1 or crlf < lf):
            return crlf, 4
        if lf != -1:
            return lf, 2
        return None

    @staticmethod
    def _parse(raw: str) -> SSEFrame:
        event: str | None = None
        data_lines: list[str] = []
        for line in raw.split("\n"):
            line = line.rstrip("\r")  # tolerate \r\n inside frames
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                value = line[5:]
                data_lines.append(value[1:] if value.startswith(" ") else value)
            elif line.startswith("event:"):
                value = line[6:]
                event = value[1:] if value.startswith(" ") else value
        return SSEFrame(
            event=event,
            data="\n".join(data_lines) if data_lines else None,
            raw=raw,
        )
