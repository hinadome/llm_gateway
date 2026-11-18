"""Tests for the MCP proxy surface — tier 33.

``POST /v1/mcp/{server}`` is the sixth inbound transport and the first
that is not for the gateway's models at all: a Streamable-HTTP proxy
(MCP 2025-06-18) between MCP clients and operator-configured servers.
The suite pins, against a scripted JSON-RPC upstream:

* the session lifecycle — the gateway issues its OWN ``Mcp-Session-Id``
  (never the upstream's, so a client id is useless upstream), maps it
  to the upstream session + negotiated protocol version, requires it
  (400) on every post-initialize message, 404s unknown/expired ids,
  terminates on DELETE (relayed upstream with the UPSTREAM id), drops
  lazily on an upstream 404, and sweeps TTL-expired sessions;
* the wire contract — one JSON-RPC message per POST (arrays 400:
  batching was removed from the transport), notifications answered
  202, upstream auth is the OPERATOR'S configured headers while the
  caller's gateway credential never travels upstream, GET is 405, and
  SSE answers relay live (progress frames verbatim, THE response
  frame intercepted);
* the policy spine — server allowlists (key + tenant, fnmatch; admins
  bypass), the ``mcp`` endpoint scope, RPM (no TPM/budget: no tokens
  exist on this surface), the tier-30 tool policy applied per name
  (tools/list filtered, tools/call admitted one name at a time,
  nameless calls failing closed under a policy), and the request/
  response guardrail phases over every content-bearing method
  (tier 49): the tool arguments and the result's text items, embedded-
  resource texts and structuredContent on tools/call; the whole params
  and every server text shape on resources/read, prompts/get and
  completion/complete (modify only when the rewrite stays a JSON
  object; redirect and broken rewrites fail closed; base64 payloads —
  blobs, image/audio items — are binary, skipped by design; denials
  are in-band JSON-RPC errors, HTTP 200, so the caller's JSON-RPC
  layer sees them) — with the guardrail config scopes pinned as they
  bind on this route (routes: "mcp", models: the SERVER name, keys);
* observability — the log row (route ``mcp``, provider ``mcp``,
  model = server name), the mcp.session.opened / mcp.tool.blocked /
  mcp.session.closed events, /admin/mcp introspection (header NAMES
  only), and hot-reload (a removed server 404s the next message).
"""

from __future__ import annotations

import asyncio
import json

import httpx

from gateway.config import (
    EventsConfig,
    EventHandlerConfig,
    GuardrailConfig,
    GuardrailScope,
    MCPConfig,
    MCPServerConfig,
)
from gateway.core.events import EventHandler, GatewayEvent
from gateway.core.mcp import (
    GUARDRAIL_BLOCKED,
    INVALID_REQUEST,
    PARSE_ERROR,
    SESSION_REQUIRED,
    SESSION_TERMINATED,
    TOOL_NOT_ALLOWED,
)
from gateway.guardrails.base import Guardrail, GuardrailResult

from .conftest import ADMIN_KEY, _make_env, auth, create_key, make_config

# The mock upstream lives at mcp.test so _make_env's composite
# transport routes it to the MockMCP, everything else to MockUpstream.
SERVER = MCPServerConfig(
    name="test-server",
    url="http://mcp.test/mcp",
    headers={"Authorization": "Bearer upstream-secret"},
)


def _mcp_config(
    *, enabled: bool = True, servers: list[MCPServerConfig] | None = None
) -> MCPConfig:
    return MCPConfig(
        enabled=enabled, servers=servers if servers is not None else [SERVER]
    )


# ----------------------------------------------------------------------
# The scripted upstream
# ----------------------------------------------------------------------


def _chunked(payload: bytes, size: int = 16):
    """A payload as an async byte stream in small chunks — the SSE
    answers arrive split across reads, so the relay must actually
    parse frames incrementally instead of assuming one frame per
    chunk."""

    async def gen():
        for i in range(0, len(payload), size):
            yield payload[i : i + size]

    return gen()


def _sse_response(
    message: dict, *, headers: dict | None = None, progress: bool = False
) -> httpx.Response:
    """One JSON-RPC message as an SSE answer. ``progress`` prefixes a
    notification frame (relayed verbatim) before the response frame
    (intercepted) — the streaming tools/* shape."""
    frames = b""
    if progress:
        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progress": 50},
        }
        frames += f"data: {json.dumps(notification)}\n\n".encode()
    frames += f"data: {json.dumps(message)}\n\n".encode()
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream", **(headers or {})},
        content=_chunked(frames),
    )


class MockMCP:
    """A scripted MCP server speaking Streamable HTTP at mcp.test.

    Records every request it sees (the httpx.Request — headers and
    body both — so tests can pin the upstream wire contract), answers
    by JSON-RPC method, and can be scripted to answer SSE
    (``sse_initialize`` / ``sse_calls``) or to fail the next request
    with a chosen status (``fail_next``).
    """

    def __init__(
        self,
        *,
        sse_initialize: bool = False,
        sse_calls: bool = False,
        call_text: str = "weather is sunny",
        call_content: list | None = None,
        structured_content: dict | None = None,
        read_text: str = "the docs say hello",
        read_blob: str | None = None,
        prompt_text: str = "the password is swordfish",
        prompt_shape: str = "list",
        completion_values: list[str] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.deleted: list[str | None] = []
        self.sse_initialize = sse_initialize
        self.sse_calls = sse_calls
        self.call_text = call_text
        # Tier 49: scripted result shapes — call_content replaces the
        # default tools/call content list (embedded resources, image
        # items), structured_content rides beside it.
        self.call_content = call_content
        self.structured_content = structured_content
        self.read_text = read_text
        self.read_blob = read_blob
        self.prompt_text = prompt_text
        self.prompt_shape = prompt_shape
        self.completion_values = completion_values
        self.fail_next: int | None = None
        self._next_sid = 0

    def bodies(self, method: str) -> list[dict]:
        """Parsed bodies of the upstream requests for one method."""
        seen = []
        for request in self.requests:
            try:
                body = json.loads(request.content)
            except ValueError:
                continue
            if isinstance(body, dict) and body.get("method") == method:
                seen.append(body)
        return seen

    def _json(self, message: dict, headers: dict | None = None) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", **(headers or {})},
            content=json.dumps(message),
        )

    def _answer(self, message: dict, headers: dict | None = None) -> httpx.Response:
        if self.sse_calls:
            return _sse_response(message, headers=headers, progress=True)
        return self._json(message, headers=headers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "DELETE":
            self.deleted.append(request.headers.get("mcp-session-id"))
            return httpx.Response(202)
        if self.fail_next is not None:
            status, self.fail_next = self.fail_next, None
            return httpx.Response(status, json={"error": "scripted"})
        try:
            body = json.loads(request.content)
        except ValueError:
            return httpx.Response(400)
        method = body.get("method") if isinstance(body, dict) else None
        message_id = body.get("id") if isinstance(body, dict) else None

        if method == "initialize":
            self._next_sid += 1
            sid = f"upstream-sid-{self._next_sid}"
            message = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock-mcp", "version": "1.0"},
                },
            }
            if self.sse_initialize:
                return _sse_response(message, headers={"Mcp-Session-Id": sid})
            return self._json(message, headers={"Mcp-Session-Id": sid})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            message = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "tools": [
                        {"name": "search_web", "description": "Search the web"},
                        {"name": "get_weather", "description": "Get weather"},
                    ]
                },
            }
            return self._answer(message)
        if method == "tools/call":
            result: dict = {}
            if self.call_content is not None:
                result["content"] = self.call_content
            else:
                result["content"] = [{"type": "text", "text": self.call_text}]
            if self.structured_content is not None:
                result["structuredContent"] = self.structured_content
            message = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": result,
            }
            return self._answer(message)
        if method == "resources/read":
            contents = [
                {
                    "uri": "file:///docs/readme.md",
                    "mimeType": "text/plain",
                    "text": self.read_text,
                }
            ]
            if self.read_blob is not None:
                contents.append(
                    {
                        "uri": "file:///docs/blob.bin",
                        "mimeType": "application/octet-stream",
                        "blob": self.read_blob,
                    }
                )
            message = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"contents": contents},
            }
            return self._answer(message)
        if method == "prompts/get":
            block = {"type": "text", "text": self.prompt_text}
            content = [block] if self.prompt_shape == "list" else block
            message = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"messages": [{"role": "user", "content": content}]},
            }
            return self._answer(message)
        if method == "completion/complete":
            values = (
                list(self.completion_values)
                if self.completion_values is not None
                else ["hello there", "help"]
            )
            message = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"completion": {"values": values}},
            }
            return self._answer(message)
        # ping and any other request: an empty result.
        return self._json({"jsonrpc": "2.0", "id": message_id, "result": {}})


# ----------------------------------------------------------------------
# Client-side helpers
# ----------------------------------------------------------------------


def _init_body(message_id: int | str = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.0"},
        },
    }


def _notification(method: str, params: dict | None = None) -> dict:
    message: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def _request(method: str, params: dict | None = None, message_id: int | str = 2) -> dict:
    message: dict = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _tools_call(name: str, arguments: dict, message_id: int | str = 3) -> dict:
    return _request(
        "tools/call", {"name": name, "arguments": arguments}, message_id=message_id
    )


async def _post(
    client: httpx.AsyncClient,
    body: dict | list,
    key: str,
    *,
    server: str = "test-server",
    session: str | None = None,
) -> httpx.Response:
    headers = auth(key)
    if session is not None:
        headers["Mcp-Session-Id"] = session
    return await client.post(f"/v1/mcp/{server}", json=body, headers=headers)


async def _open_session(
    client: httpx.AsyncClient,
    key: str,
    *,
    server: str = "test-server",
) -> str:
    """The client handshake: initialize (gets OUR session id) + the
    initialized notification (relayed 202)."""
    resp = await _post(client, _init_body(), key, server=server)
    assert resp.status_code == 200, resp.text
    sid = resp.headers["mcp-session-id"]
    resp = await _post(
        client, _notification("notifications/initialized"), key, server=server, session=sid
    )
    assert resp.status_code == 202, resp.text
    return sid


# ----------------------------------------------------------------------
# Custom guardrails (module level — configs load them by class path)
# ----------------------------------------------------------------------


class _ArgsRewriter(Guardrail):
    """Rewrites the (single) user message — on the MCP surface that is
    the serialized tool arguments, so this scripts request-phase
    MODIFY verdicts (a clean rewrite, or a broken one)."""

    name = "test_args_rewriter"

    def __init__(self, params=None):
        super().__init__(params)
        self.text = (params or {}).get("text", "{}")

    async def check_request(self, request, ctx):
        return GuardrailResult.modified(
            messages=[request.messages[-1].model_copy(update={"content": self.text})],
            reason="rewritten args",
        )


class _Redirector(Guardrail):
    """Scripts a model-redirect verdict — meaningless on the MCP
    surface, which must fail closed."""

    name = "test_redirector"

    async def check_request(self, request, ctx):
        return GuardrailResult.redirect("other-model", "policy: redirect")


class _ResponseRewriter(Guardrail):
    """Rewrites the (single) assistant message — on the MCP surface
    that is one piece of server-returned text, so this scripts
    response-phase MODIFY verdicts: a clean rewrite, or a broken one
    (text that is no longer the JSON object structuredContent must
    stay — the fail-closed rule)."""

    name = "test_response_rewriter"

    def __init__(self, params=None):
        super().__init__(params)
        self.text = (params or {}).get("text", "rewritten")

    async def check_response(self, request, response, ctx):
        return GuardrailResult.modified(
            response={
                **response,
                "choices": [
                    {
                        **response["choices"][0],
                        "message": {"role": "assistant", "content": self.text},
                    }
                ],
            },
            reason="rewritten response",
        )


class _Recorder(EventHandler):
    """Records every event it sees, in delivery order."""

    name = "mcp_recorder"

    def __init__(self, params=None):
        super().__init__(params)
        self.events: list[GatewayEvent] = []

    async def on_event(self, event: GatewayEvent) -> None:
        self.events.append(event)


def _recorded_config(**config_overrides):
    """make_config with the _Recorder handler wired (MCP overrides
    pass straight through)."""
    return make_config(
        events=EventsConfig(
            enabled=True,
            handlers=[EventHandlerConfig(class_path="tests.test_mcp:_Recorder")],
        ),
        **config_overrides,
    )


def _recorder(env) -> _Recorder:
    return next(h for h in env.app.state.events.handlers if isinstance(h, _Recorder))


# ----------------------------------------------------------------------
# initialize + the wire contract
# ----------------------------------------------------------------------


async def test_initialize_issues_gateway_session(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        resp = await _post(env.client, _init_body(), key)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The upstream's answer passes through (id echoed, version
        # negotiated) — but the session id is OURS, not upstream's.
        assert body["id"] == 1
        assert body["result"]["protocolVersion"] == "2025-06-18"
        sid = resp.headers["mcp-session-id"]
        assert sid.startswith("gw_") and "upstream-sid" not in sid

        # The upstream leg: the configured server auth and the JSON-RPC
        # content types — and NOT the caller's gateway credential.
        upstream = env.mcp.requests[-1]
        assert upstream.headers.get("authorization") == "Bearer upstream-secret"
        assert key not in json.dumps(dict(upstream.headers))
        # Session/protocol stamps are absent on initialize (nothing
        # negotiated yet) — they start with the second message.
        assert "mcp-session-id" not in upstream.headers


async def test_initialize_sse_answered_as_json(tmp_path):
    """An SSE-answering initialize still lands on the client as plain
    JSON (the gateway consumes the stream itself)."""
    async for env in _make_env(
        tmp_path,
        config=make_config(mcp=_mcp_config()),
        mcp_mock=MockMCP(sse_initialize=True),
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        resp = await _post(env.client, _init_body(), key)
        assert resp.status_code == 200
        assert "text/event-stream" not in resp.headers.get("content-type", "")
        assert resp.json()["result"]["protocolVersion"] == "2025-06-18"
        assert resp.headers["mcp-session-id"].startswith("gw_")


async def test_query_param_auth(tmp_path):
    """``?key=`` works — browser-based MCP clients cannot set headers."""
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        resp = await env.client.post(
            "/v1/mcp/test-server", json=_init_body(), params={"key": key}
        )
        assert resp.status_code == 200
        assert resp.headers["mcp-session-id"].startswith("gw_")


async def test_post_initialize_stamps_upstream_session(tmp_path):
    """From the second message on, the gateway quotes the UPSTREAM
    session id and the negotiated protocol version upstream."""
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        await _post(env.client, _request("ping"), key, session=sid)
        last = env.mcp.requests[-1]
        assert last.headers.get("mcp-session-id") == "upstream-sid-1"
        assert last.headers.get("mcp-protocol-version") == "2025-06-18"


async def test_initialized_notification_relays_202(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        resp = await _post(env.client, _init_body(), key)
        sid = resp.headers["mcp-session-id"]
        resp = await _post(
            env.client, _notification("notifications/initialized"), key, session=sid
        )
        assert resp.status_code == 202
        # Relay verbatim: the upstream saw exactly the client's method.
        assert env.mcp.bodies("notifications/initialized")


async def test_ping_roundtrip(tmp_path):
    """A generic request (no special policy) round-trips unchanged."""
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(env.client, _request("ping", message_id="p1"), key, session=sid)
        assert resp.status_code == 200
        assert resp.json() == {"jsonrpc": "2.0", "id": "p1", "result": {}}


async def test_log_row_labels_the_server(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        await _open_session(env.client, key)
        logs = await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        row = logs.json()["data"][0]
        assert row["route"] == "mcp"
        assert row["provider"] == "mcp"
        assert row["model"] == "test-server"
        assert row["status"] == "success"


# ----------------------------------------------------------------------
# Envelope + session-header failures (transport-level 4xx)
# ----------------------------------------------------------------------


async def test_malformed_body_parse_error(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        resp = await env.client.post(
            "/v1/mcp/test-server", content=b"{nope", headers=auth(key)
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == PARSE_ERROR
        assert resp.json()["id"] is None


async def test_batch_array_rejected(tmp_path):
    """Batching was REMOVED from the 2025-06-18 transport — one JSON-RPC
    message per POST, arrays answered -32600."""
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        resp = await _post(env.client, [_init_body(), _notification("x")], key)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == INVALID_REQUEST


async def test_not_jsonrpc_rejected(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        resp = await _post(env.client, {"hello": "world"}, key)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == INVALID_REQUEST


async def test_missing_session_header_400(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        resp = await _post(env.client, _request("tools/list"), key)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == SESSION_REQUIRED


async def test_unknown_session_404(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        resp = await _post(env.client, _request("tools/list"), key, session="gw_bogus")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == SESSION_TERMINATED


async def test_get_method_405(tmp_path):
    """The standalone server-to-client stream is out of scope; the
    transport spec allows a 405."""
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        resp = await env.client.get("/v1/mcp/test-server", headers=auth(key))
        assert resp.status_code == 405
        assert "POST" in resp.headers.get("allow", "")


# ----------------------------------------------------------------------
# Gates (gateway-envelope failures)
# ----------------------------------------------------------------------


async def test_disabled_404s(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config(enabled=False)), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        resp = await _post(env.client, _init_body(), key)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "mcp_disabled"
        # The route does not exist as a side channel either way.
        assert (
            await env.client.get("/v1/mcp/test-server", headers=auth(key))
        ).status_code == 404


async def test_unknown_server_404(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        resp = await _post(env.client, _init_body(), key, server="nope")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "mcp_server_not_found"


async def test_key_server_allowlist(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        denied = (
            await create_key(env.client, "denied", allowed_mcp_servers=["other-*"])
        )["key"]
        resp = await _post(env.client, _init_body(), denied)
        assert resp.status_code == 404  # same shape as unknown — no leak
        assert resp.json()["error"]["code"] == "mcp_server_not_found"

        allowed = (
            await create_key(env.client, "allowed", allowed_mcp_servers=["test-*"])
        )["key"]
        assert (await _post(env.client, _init_body(), allowed)).status_code == 200


async def test_tenant_server_allowlist_binds_members(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        tenant = (
            await env.client.post(
                "/admin/tenants",
                json={"name": "acme", "allowed_mcp_servers": ["other-*"]},
                headers=auth(ADMIN_KEY),
            )
        ).json()
        member = (await create_key(env.client, "member", tenant_id=tenant["id"]))["key"]
        assert (await _post(env.client, _init_body(), member)).status_code == 404
        # A member key WIDER than the tenant narrows to the tenant's.
        wide = (
            await create_key(
                env.client, "wide", tenant_id=tenant["id"], allowed_mcp_servers=["*"]
            )
        )["key"]
        assert (await _post(env.client, _init_body(), wide)).status_code == 404
        # Admin keys bypass the key layer (they are the operators).
        assert (await _post(env.client, _init_body(), ADMIN_KEY)).status_code == 200


async def test_endpoint_scope_403(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        chat_only = (
            await create_key(env.client, "chat-only", allowed_endpoints=["chat"])
        )["key"]
        resp = await _post(env.client, _init_body(), chat_only)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "endpoint_not_allowed"

        scoped = (
            await create_key(env.client, "scoped", allowed_endpoints=["mcp"])
        )["key"]
        assert (await _post(env.client, _init_body(), scoped)).status_code == 200


async def test_rpm_limit_429(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "limited", rpm_limit=1))["key"]
        assert (await _post(env.client, _init_body(), key)).status_code == 200
        resp = await _post(env.client, _request("ping"), key, session="gw_whatever")
        assert resp.status_code == 429


# ----------------------------------------------------------------------
# Tool policy: tools/list filtered, tools/call admitted per name
# ----------------------------------------------------------------------


async def test_tools_list_filtered_by_key_policy(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        restricted = (
            await create_key(env.client, "restricted", allowed_tools=["get_*"])
        )["key"]
        sid = await _open_session(env.client, restricted)
        resp = await _post(env.client, _request("tools/list"), restricted, session=sid)
        names = [t["name"] for t in resp.json()["result"]["tools"]]
        assert names == ["get_weather"]  # the restricted catalog

        free = (await create_key(env.client, "free"))["key"]
        sid = await _open_session(env.client, free)
        resp = await _post(env.client, _request("tools/list"), free, session=sid)
        names = [t["name"] for t in resp.json()["result"]["tools"]]
        assert names == ["search_web", "get_weather"]  # unchanged, order kept


async def test_tools_list_tenant_narrows(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        tenant = (
            await env.client.post(
                "/admin/tenants",
                json={"name": "acme", "allowed_tools": ["get_*"]},
                headers=auth(ADMIN_KEY),
            )
        ).json()
        member = (await create_key(env.client, "member", tenant_id=tenant["id"]))["key"]
        sid = await _open_session(env.client, member)
        resp = await _post(env.client, _request("tools/list"), member, session=sid)
        names = [t["name"] for t in resp.json()["result"]["tools"]]
        assert names == ["get_weather"]


async def test_tools_call_allowed_verbatim(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "caller"))["key"]
        sid = await _open_session(env.client, key)
        args = {"city": "Tokyo", "unit": "celsius"}
        resp = await _post(env.client, _tools_call("get_weather", args), key, session=sid)
        assert resp.status_code == 200
        assert resp.json()["result"]["content"][0]["text"] == "weather is sunny"
        # The upstream saw the arguments verbatim — a transparent proxy.
        assert env.mcp.bodies("tools/call")[0]["params"]["arguments"] == args


async def test_tools_call_denied_by_policy_in_band(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        denied_key = (
            await create_key(env.client, "denied", allowed_tools=["get_*"])
        )["key"]
        sid = await _open_session(env.client, denied_key)
        resp = await _post(
            env.client, _tools_call("search_web", {"q": "x"}), denied_key, session=sid
        )
        # In-band: HTTP 200 + the JSON-RPC error the caller's MCP
        # client layer parses — never a transport 4xx, never upstream.
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == TOOL_NOT_ALLOWED
        assert resp.json()["id"] == 3
        assert not env.mcp.bodies("tools/call")

        # The denial is observable: a blocked log row.
        logs = await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        assert logs.json()["data"][0]["status"] == "blocked"


async def test_tools_call_nameless_fails_closed(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        policy_key = (
            await create_key(env.client, "with-policy", allowed_tools=["get_*"])
        )["key"]
        sid = await _open_session(env.client, policy_key)
        resp = await _post(
            env.client,
            _request("tools/call", {"arguments": {"a": 1}}),
            policy_key,
            session=sid,
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == TOOL_NOT_ALLOWED
        assert not env.mcp.bodies("tools/call")

        # Unrestricted callers fall through to the server's own error.
        free = (await create_key(env.client, "free"))["key"]
        sid = await _open_session(env.client, free)
        resp = await _post(
            env.client,
            _request("tools/call", {"arguments": {"a": 1}}),
            free,
            session=sid,
        )
        assert resp.status_code == 200
        assert env.mcp.bodies("tools/call")


# ----------------------------------------------------------------------
# Guardrails on tools/call (request phase: the arguments)
# ----------------------------------------------------------------------


async def test_tools_call_request_phase_blocked(tmp_path):
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="keyword_blocklist",
                params={"blocked_terms": ["forbidden"], "check_completion": False},
            )
        ],
    )
    async for env in _make_env(tmp_path, config=config, mcp_mock=MockMCP()):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client,
            _tools_call("get_weather", {"note": "forbidden word"}),
            key,
            session=sid,
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED
        assert "keyword_blocklist" in resp.json()["error"]["message"]
        assert not env.mcp.bodies("tools/call")


async def test_tools_call_request_phase_modified(tmp_path):
    """A clean rewrite (still a JSON object) is applied and travels."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[GuardrailConfig(name="pii_redaction", params={"types": ["email"]})],
    )
    async for env in _make_env(tmp_path, config=config, mcp_mock=MockMCP()):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client,
            _tools_call("get_weather", {"email": "alice@example.com"}),
            key,
            session=sid,
        )
        assert resp.status_code == 200
        sent = env.mcp.bodies("tools/call")[0]["params"]["arguments"]
        assert sent["email"] == "[REDACTED:EMAIL]"


async def test_tools_call_broken_rewrite_fails_closed(tmp_path):
    """A guardrail that corrupts the arguments (no longer a JSON
    object) — the call fails closed rather than corrupting the tool."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="test_args_rewriter",
                class_path="tests.test_mcp:_ArgsRewriter",
                params={"text": "definitely not json"},
            )
        ],
    )
    async for env in _make_env(tmp_path, config=config, mcp_mock=MockMCP()):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client, _tools_call("get_weather", {"city": "Tokyo"}), key, session=sid
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED
        assert not env.mcp.bodies("tools/call")


async def test_tools_call_model_redirect_fails_closed(tmp_path):
    """A model redirect cannot apply to a tool call — fail closed."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(name="test_redirector", class_path="tests.test_mcp:_Redirector")
        ],
    )
    async for env in _make_env(tmp_path, config=config, mcp_mock=MockMCP()):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client, _tools_call("get_weather", {"city": "Tokyo"}), key, session=sid
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED
        assert not env.mcp.bodies("tools/call")


# ----------------------------------------------------------------------
# Guardrails on tools/call (response phase: the result texts)
# ----------------------------------------------------------------------


async def test_tools_call_response_phase_blocked(tmp_path):
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="keyword_blocklist",
                params={"blocked_terms": ["sunny"], "check_prompt": False},
            )
        ],
    )
    async for env in _make_env(
        tmp_path, config=config, mcp_mock=MockMCP(call_text="weather is sunny")
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client, _tools_call("get_weather", {"city": "x"}), key, session=sid
        )
        # The tool ran, the result came back, and the guardrail
        # blocked the TEXT — an in-band error replaces the answer.
        assert env.mcp.bodies("tools/call")
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED


async def test_tools_call_response_phase_redacted(tmp_path):
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="pii_redaction",
                params={"types": ["email"], "mask_in_response": True},
            )
        ],
    )
    async for env in _make_env(
        tmp_path,
        config=config,
        mcp_mock=MockMCP(call_text="contact alice@example.com now"),
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client, _tools_call("get_weather", {"city": "x"}), key, session=sid
        )
        assert resp.status_code == 200
        text = resp.json()["result"]["content"][0]["text"]
        assert "[REDACTED:EMAIL]" in text
        assert "alice@example.com" not in text


# ----------------------------------------------------------------------
# Guardrails on tools/call results: structuredContent + embedded
# resources (tier 49 — the non-text shapes the old pass skipped)
# ----------------------------------------------------------------------


async def test_tools_call_structured_content_blocked(tmp_path):
    """structuredContent is server-generated text (serialized JSON):
    a blocked term inside it denies the whole answer, in-band —
    the tool ran, the answer never reaches the caller."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="keyword_blocklist",
                params={"blocked_terms": ["sunny"], "check_prompt": False},
            )
        ],
    )
    async for env in _make_env(
        tmp_path,
        config=config,
        mcp_mock=MockMCP(
            call_content=[],
            structured_content={"forecast": "weather is sunny"},
        ),
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client, _tools_call("get_weather", {"city": "x"}), key, session=sid
        )
        assert env.mcp.bodies("tools/call")
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED


async def test_tools_call_structured_content_redacted(tmp_path):
    """A clean rewrite of structuredContent (still a JSON object) is
    applied — the arguments rule, on the result side."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="pii_redaction",
                params={"types": ["email"], "mask_in_response": True},
            )
        ],
    )
    async for env in _make_env(
        tmp_path,
        config=config,
        mcp_mock=MockMCP(
            call_content=[],
            structured_content={"contact": "alice@example.com"},
        ),
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client, _tools_call("get_weather", {"city": "x"}), key, session=sid
        )
        assert resp.status_code == 200
        structured = resp.json()["result"]["structuredContent"]
        assert structured["contact"] == "[REDACTED:EMAIL]"


async def test_tools_call_structured_content_broken_rewrite_fails_closed(tmp_path):
    """A rewrite that corrupts structuredContent (no longer a JSON
    object) — fail closed, the structuredContent twin of the
    arguments rule: a corrupted result is worse than a blocked one."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="test_response_rewriter",
                class_path="tests.test_mcp:_ResponseRewriter",
                params={"text": "definitely not json"},
            )
        ],
    )
    async for env in _make_env(
        tmp_path,
        config=config,
        mcp_mock=MockMCP(call_content=[], structured_content={"a": 1}),
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client, _tools_call("get_weather", {"city": "x"}), key, session=sid
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED
        assert "structuredContent" in resp.json()["error"]["message"]


async def test_tools_call_resource_item_blocked(tmp_path):
    """An embedded resource's TEXT is guarded like any other
    server-returned text — the exfiltration shape (a tool result
    wrapping a file the server read) cannot launder past the
    response phase by riding a resource item."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="keyword_blocklist",
                params={"blocked_terms": ["swordfish"], "check_prompt": False},
            )
        ],
    )
    async for env in _make_env(
        tmp_path,
        config=config,
        mcp_mock=MockMCP(
            call_content=[
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///secrets.txt",
                        "mimeType": "text/plain",
                        "text": "the password is swordfish",
                    },
                }
            ]
        ),
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client, _tools_call("get_weather", {"city": "x"}), key, session=sid
        )
        assert env.mcp.bodies("tools/call")
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED


async def test_tools_call_resource_item_redacted_image_skipped(tmp_path):
    """Redaction reaches the embedded resource's text — and the image
    item beside it is UNTOUCHED: base64 is binary, no text to scan,
    skipped by design (a text guardrail run over it would only mangle
    it)."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="pii_redaction",
                params={"types": ["email"], "mask_in_response": True},
            )
        ],
    )
    async for env in _make_env(
        tmp_path,
        config=config,
        mcp_mock=MockMCP(
            call_content=[
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///contact.txt",
                        "mimeType": "text/plain",
                        "text": "reach alice@example.com",
                    },
                },
                {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
            ]
        ),
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client, _tools_call("get_weather", {"city": "x"}), key, session=sid
        )
        assert resp.status_code == 200
        items = resp.json()["result"]["content"]
        assert "[REDACTED:EMAIL]" in items[0]["resource"]["text"]
        assert items[1] == {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}


# ----------------------------------------------------------------------
# Guardrails on the content methods (tier 49): resources/read,
# prompts/get, completion/complete — both directions
# ----------------------------------------------------------------------


async def test_resources_read_request_phase_blocked(tmp_path):
    """The URI is caller-controlled content: the request-phase
    guardrails run over the whole params object (uri included —
    nothing the caller wrote slips past unscanned), and a denial
    means NOTHING upstream, answered in-band."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="keyword_blocklist",
                params={"blocked_terms": ["forbidden"], "check_completion": False},
            )
        ],
    )
    async for env in _make_env(tmp_path, config=config, mcp_mock=MockMCP()):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client,
            _request("resources/read", {"uri": "file:///docs/forbidden.md"}),
            key,
            session=sid,
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED
        assert "keyword_blocklist" in resp.json()["error"]["message"]
        assert not env.mcp.bodies("resources/read")


async def test_prompts_get_request_phase_modified(tmp_path):
    """The prompt arguments are caller-controlled content: a clean
    rewrite (still a JSON object) is applied to the whole params and
    travels — the arguments rule, moved to the content methods, with
    everything the caller did NOT write (here: nothing, the whole
    params is theirs) carried through the rewrite."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[GuardrailConfig(name="pii_redaction", params={"types": ["email"]})],
    )
    async for env in _make_env(tmp_path, config=config, mcp_mock=MockMCP()):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client,
            _request(
                "prompts/get",
                {"name": "greet", "arguments": {"user": "alice@example.com"}},
            ),
            key,
            session=sid,
        )
        assert resp.status_code == 200
        sent = env.mcp.bodies("prompts/get")[0]["params"]
        assert sent["name"] == "greet"  # untouched fields survive the rewrite
        assert sent["arguments"]["user"] == "[REDACTED:EMAIL]"


async def test_content_method_broken_rewrite_fails_closed(tmp_path):
    """A rewrite that corrupts the params (no longer a JSON object)
    fails closed — the content methods share the arguments rule, and
    never corrupt the call."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="test_args_rewriter",
                class_path="tests.test_mcp:_ArgsRewriter",
                params={"text": "definitely not json"},
            )
        ],
    )
    async for env in _make_env(tmp_path, config=config, mcp_mock=MockMCP()):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client,
            _request("resources/read", {"uri": "file:///docs/readme.md"}),
            key,
            session=sid,
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED
        assert "valid JSON object" in resp.json()["error"]["message"]
        assert not env.mcp.bodies("resources/read")


async def test_resources_read_response_phase_blocked(tmp_path):
    """The resource body is server-generated text: a denial replaces
    the whole answer in-band (the read ran, the body never reaches the
    caller) — and the block event carries the METHOD now, so an
    operator reading the stream knows what was denied."""
    config = _recorded_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="keyword_blocklist",
                params={"blocked_terms": ["swordfish"], "check_prompt": False},
            )
        ],
    )
    async for env in _make_env(
        tmp_path, config=config, mcp_mock=MockMCP(read_text="the password is swordfish")
    ):
        recorder = _recorder(env)
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client,
            _request("resources/read", {"uri": "file:///docs/readme.md"}),
            key,
            session=sid,
        )
        assert env.mcp.bodies("resources/read")
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED
        await env.app.state.events.drain()
        blocked = [e for e in recorder.events if e.name == "mcp.tool.blocked"]
        assert blocked
        assert blocked[0].payload["method"] == "resources/read"
        assert blocked[0].payload["tool"] is None  # no tool on this method


async def test_resources_read_response_phase_redacted_blob_skipped(tmp_path):
    """Redaction rewrites the resource TEXT; a blob twin (base64) is
    binary — no text to scan, skipped by design and passed through
    byte-identical."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="pii_redaction",
                params={"types": ["email"], "mask_in_response": True},
            )
        ],
    )
    async for env in _make_env(
        tmp_path,
        config=config,
        mcp_mock=MockMCP(
            read_text="contact alice@example.com",
            read_blob="aGVsbG8gd29ybGQ=",
        ),
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client,
            _request("resources/read", {"uri": "file:///docs/readme.md"}),
            key,
            session=sid,
        )
        assert resp.status_code == 200
        contents = resp.json()["result"]["contents"]
        assert "[REDACTED:EMAIL]" in contents[0]["text"]
        assert "alice@example.com" not in contents[0]["text"]
        assert contents[1]["blob"] == "aGVsbG8gd29ybGQ="  # untouched


async def test_prompts_get_response_phase_blocked(tmp_path):
    """Prompt message text is server-generated: blocked in-band, the
    list-shaped content blocks walked (the spec's 2025-06-18 shape)."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="keyword_blocklist",
                params={"blocked_terms": ["swordfish"], "check_prompt": False},
            )
        ],
    )
    async for env in _make_env(
        tmp_path,
        config=config,
        mcp_mock=MockMCP(prompt_text="the password is swordfish"),
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client, _request("prompts/get", {"name": "greet"}), key, session=sid
        )
        assert env.mcp.bodies("prompts/get")
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED


async def test_prompts_get_response_phase_redacted_dict_shape(tmp_path):
    """The single-block content shape (the pre-widening revision
    servers still ship) walks the same — the guard must not care
    which shape the server chose."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="pii_redaction",
                params={"types": ["email"], "mask_in_response": True},
            )
        ],
    )
    async for env in _make_env(
        tmp_path,
        config=config,
        mcp_mock=MockMCP(
            prompt_text="email alice@example.com", prompt_shape="dict"
        ),
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client, _request("prompts/get", {"name": "greet"}), key, session=sid
        )
        assert resp.status_code == 200
        block = resp.json()["result"]["messages"][0]["content"]
        assert "[REDACTED:EMAIL]" in block["text"]
        assert "alice@example.com" not in block["text"]


async def test_completion_complete_response_phase_blocked(tmp_path):
    """completion/complete's values are server-generated text
    suggestions: one poisoned value denies the whole answer, in-band."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="keyword_blocklist",
                params={"blocked_terms": ["swordfish"], "check_prompt": False},
            )
        ],
    )
    async for env in _make_env(
        tmp_path,
        config=config,
        mcp_mock=MockMCP(completion_values=["hello", "the swordfish option"]),
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client,
            _request(
                "completion/complete",
                {"ref": {"type": "ref/prompt", "name": "greet"}, "argument": {"name": "g", "value": "s"}},
            ),
            key,
            session=sid,
        )
        assert env.mcp.bodies("completion/complete")
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED


async def test_completion_complete_response_phase_redacted(tmp_path):
    """Each value is its own text piece: a redaction rewrites the
    pieces it matches and leaves the rest alone — never one mangled
    blob."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="pii_redaction",
                params={"types": ["email"], "mask_in_response": True},
            )
        ],
    )
    async for env in _make_env(
        tmp_path,
        config=config,
        mcp_mock=MockMCP(
            completion_values=["ping alice@example.com", "hello"]
        ),
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client,
            _request(
                "completion/complete",
                {"ref": {"type": "ref/prompt", "name": "greet"}, "argument": {"name": "g", "value": "s"}},
            ),
            key,
            session=sid,
        )
        assert resp.status_code == 200
        values = resp.json()["result"]["completion"]["values"]
        assert values[0] == "ping [REDACTED:EMAIL]"
        assert values[1] == "hello"


# ----------------------------------------------------------------------
# SSE answers
# ----------------------------------------------------------------------


async def test_sse_answer_relayed_live(tmp_path):
    """An SSE-answering tools/list: the progress frame relays
    VERBATIM, the response frame is intercepted (filtered) and
    re-serialized — one pass over a chunked stream."""
    async for env in _make_env(
        tmp_path,
        config=make_config(mcp=_mcp_config()),
        mcp_mock=MockMCP(sse_calls=True),
    ):
        restricted = (
            await create_key(env.client, "restricted", allowed_tools=["get_*"])
        )["key"]
        sid = await _open_session(env.client, restricted)
        resp = await env.client.post(
            "/v1/mcp/test-server",
            json=_request("tools/list"),
            headers={**auth(restricted), "Mcp-Session-Id": sid},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        text = resp.text
        # The progress notification, byte-for-byte.
        assert "notifications/progress" in text
        # The filtered response frame: only the allowed tool remains.
        assert "get_weather" in text
        assert "search_web" not in text


async def test_sse_answer_block_becomes_error_frame(tmp_path):
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="keyword_blocklist",
                params={"blocked_terms": ["sunny"], "check_prompt": False},
            )
        ],
    )
    async for env in _make_env(
        tmp_path,
        config=config,
        mcp_mock=MockMCP(sse_calls=True, call_text="weather is sunny"),
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await env.client.post(
            "/v1/mcp/test-server",
            json=_tools_call("get_weather", {"city": "x"}),
            headers={**auth(key), "Mcp-Session-Id": sid},
        )
        assert resp.status_code == 200  # the stream itself is fine
        # The response frame was REPLACED by the in-band error frame —
        # the client's JSON-RPC layer sees the denial, not the answer.
        assert str(GUARDRAIL_BLOCKED) in resp.text
        assert '"result"' not in resp.text


# ----------------------------------------------------------------------
# Session termination: DELETE, lazy drop, TTL sweep
# ----------------------------------------------------------------------


async def test_delete_lifecycle(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await env.client.delete(
            "/v1/mcp/test-server", headers={**auth(key), "Mcp-Session-Id": sid}
        )
        assert resp.status_code == 200
        # The upstream saw the termination with ITS session id.
        assert env.mcp.deleted == ["upstream-sid-1"]
        # Idempotent locally: a second DELETE 404s.
        resp = await env.client.delete(
            "/v1/mcp/test-server", headers={**auth(key), "Mcp-Session-Id": sid}
        )
        assert resp.status_code == 404
        # And the session is gone for POSTs too.
        resp = await _post(env.client, _request("ping"), key, session=sid)
        assert resp.status_code == 404


async def test_delete_without_header_400(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        resp = await env.client.delete("/v1/mcp/test-server", headers=auth(ADMIN_KEY))
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == SESSION_REQUIRED


async def test_upstream_404_drops_session_lazily(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        env.mcp.fail_next = 404
        resp = await _post(env.client, _request("ping"), key, session=sid)
        assert resp.status_code == 404  # relayed verbatim
        # The mapping is gone: the next request fails at the gateway.
        resp = await _post(env.client, _request("ping"), key, session=sid)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == SESSION_TERMINATED


async def test_ttl_sweep_evicts_and_notifies_upstream(tmp_path):
    config = _recorded_config(mcp=_mcp_config())
    async for env in _make_env(tmp_path, config=config, mcp_mock=MockMCP()):
        recorder = _recorder(env)
        await env.app.state.events.drain()
        recorder.events.clear()

        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        await env.app.state.events.drain()
        assert "mcp.session.opened" in [e.name for e in recorder.events]

        # TTL sweep (the lifespan loop's pass, run by hand: the
        # ASGITransport never starts the loop).
        await asyncio.sleep(0.01)
        registry = env.app.state.mcp_registry
        expired = registry.sweep(0.001)
        assert [s.gateway_session_id for s in expired] == [sid]
        for session in expired:
            await env.app.state.pipeline._mcp_sweep_one(
                session, env.app.state.config.mcp
            )
        await env.app.state.events.drain()
        closed = [e for e in recorder.events if e.name == "mcp.session.closed"]
        assert closed and closed[-1].payload["reason"] == "ttl"
        # Best-effort upstream termination with the upstream id.
        assert env.mcp.deleted == ["upstream-sid-1"]
        # And the client's next message 404s at the gateway.
        resp = await _post(env.client, _request("ping"), key, session=sid)
        assert resp.status_code == 404


# ----------------------------------------------------------------------
# Observability: /admin/mcp, key fields, hot-reload, events
# ----------------------------------------------------------------------


async def test_admin_mcp_introspection(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        # Non-admins cannot see the proxy's shape.
        assert (await env.client.get("/admin/mcp", headers=auth(key))).status_code == 403
        sid = await _open_session(env.client, key)

        resp = await env.client.get("/admin/mcp", headers=auth(ADMIN_KEY))
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["session_ttl_seconds"] == 3600.0
        server = data["servers"][0]
        assert server["name"] == "test-server"
        assert server["url"] == "http://mcp.test/mcp"
        # Header NAMES only — values are secrets.
        assert server["header_names"] == ["Authorization"]
        assert "upstream-secret" not in resp.text
        assert server["open_sessions"] == 1


async def test_key_allowed_mcp_servers_roundtrip(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = await create_key(env.client, "mcp", allowed_mcp_servers=["test-*"])
        listed = await env.client.get("/admin/keys", headers=auth(ADMIN_KEY))
        row = next(k for k in listed.json()["data"] if k["name"] == "mcp")
        assert row["allowed_mcp_servers"] == ["test-*"]
        # Patching to None clears it (a null is an explicit clear).
        patched = await env.client.patch(
            f"/admin/keys/{key['id']}",
            json={"allowed_mcp_servers": None},
            headers=auth(ADMIN_KEY),
        )
        assert patched.json()["allowed_mcp_servers"] is None


async def test_hot_reload_removing_server_404s(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(mcp=_mcp_config()), mcp_mock=MockMCP()
    ):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        # The admin reload path: swap the live config in place.
        new_config = make_config(mcp=_mcp_config(servers=[]))
        env.app.state.pipeline.apply_config(new_config)
        env.app.state.config = new_config
        resp = await _post(env.client, _request("ping"), key, session=sid)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "mcp_server_not_found"


async def test_events_session_and_tool_lifecycle(tmp_path):
    config = _recorded_config(mcp=_mcp_config())
    async for env in _make_env(tmp_path, config=config, mcp_mock=MockMCP()):
        recorder = _recorder(env)
        await env.app.state.events.drain()
        recorder.events.clear()

        denied = (
            await create_key(env.client, "denied", allowed_tools=["get_*"])
        )["key"]
        sid = await _open_session(env.client, denied)
        await _post(env.client, _tools_call("search_web", {"q": "x"}), denied, session=sid)
        await env.app.state.events.drain()

        names = [e.name for e in recorder.events]
        assert "mcp.session.opened" in names
        blocked = [e for e in recorder.events if e.name == "mcp.tool.blocked"]
        assert blocked
        assert blocked[0].payload["tool"] == "search_web"
        assert blocked[0].payload["code"] == TOOL_NOT_ALLOWED
        # The opened event carries the gateway-issued id + version.
        opened = [e for e in recorder.events if e.name == "mcp.session.opened"][-1]
        assert opened.payload["gateway_session_id"] == sid
        assert opened.payload["protocol_version"] == "2025-06-18"
        assert opened.payload["server"] == "test-server"


# ----------------------------------------------------------------------
# Guardrail scope on the MCP surface (tier 49): how config scopes bind
# on this route — routes: "mcp", models: the SERVER name, keys: names
# ----------------------------------------------------------------------


async def test_guardrail_scope_routes_mcp(tmp_path):
    """``scope.routes: ["mcp"]`` runs the guardrail HERE and nowhere
    else: the same content, denied on the MCP surface, passes clean
    on /v1/chat/completions — the operator's lever for MCP-only
    policies (an exfiltration-worried proxy, say) works exactly as
    written."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="keyword_blocklist",
                params={"blocked_terms": ["forbidden"]},
                scope=GuardrailScope(routes=["mcp"]),
            )
        ],
    )
    async for env in _make_env(tmp_path, config=config, mcp_mock=MockMCP()):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        # In scope here: denied before upstream.
        resp = await _post(
            env.client, _tools_call("get_weather", {"note": "forbidden"}), key, session=sid
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED
        assert not env.mcp.bodies("tools/call")
        # Out of scope on chat: the same term passes untouched.
        chat = await env.client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "forbidden word"}],
            },
            headers=auth(key),
        )
        assert chat.status_code == 200
        assert not chat.json().get("error")


async def test_guardrail_scope_models_server_name(tmp_path):
    """``scope.models`` binds to the SERVER name on this route (the
    log row's model column) — a guardrail scoped to the server denies
    MCP traffic while the SAME guardrail is out of scope for a chat
    request on a real model name: the two namespaces never collide."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="keyword_blocklist",
                params={"blocked_terms": ["forbidden"], "check_completion": False},
                scope=GuardrailScope(models=["test-server"]),
            )
        ],
    )
    async for env in _make_env(tmp_path, config=config, mcp_mock=MockMCP()):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        # ctx.model == "test-server" on this route: in scope, denied.
        resp = await _post(
            env.client, _tools_call("get_weather", {"note": "forbidden"}), key, session=sid
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED
        # ctx.model == "test-model" on chat: out of scope, passes.
        chat = await env.client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "forbidden word"}],
            },
            headers=auth(key),
        )
        assert chat.status_code == 200
        assert not chat.json().get("error")


async def test_guardrail_scope_models_other_server_passes(tmp_path):
    """The negative leg: a guardrail scoped to some OTHER server never
    runs here — MCP traffic to test-server is untouched, the call
    reaches upstream and the answer comes back whole."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="keyword_blocklist",
                params={"blocked_terms": ["forbidden"], "check_completion": False},
                scope=GuardrailScope(models=["some-other-server"]),
            )
        ],
    )
    async for env in _make_env(tmp_path, config=config, mcp_mock=MockMCP()):
        key = (await create_key(env.client, "mcp-key"))["key"]
        sid = await _open_session(env.client, key)
        resp = await _post(
            env.client, _tools_call("get_weather", {"note": "forbidden"}), key, session=sid
        )
        assert resp.status_code == 200
        assert "error" not in resp.json()
        assert resp.json()["result"]["content"][0]["text"] == "weather is sunny"
        assert env.mcp.bodies("tools/call")


async def test_guardrail_scope_keys_on_mcp(tmp_path):
    """``scope.keys`` / ``exclude_keys`` match the calling key's NAME
    on this surface like every other one — one guardrail, two keys:
    the named key is denied, the other passes clean."""
    config = make_config(
        mcp=_mcp_config(),
        guardrails=[
            GuardrailConfig(
                name="keyword_blocklist",
                params={"blocked_terms": ["forbidden"], "check_completion": False},
                scope=GuardrailScope(keys=["mcp-key"]),
            )
        ],
    )
    async for env in _make_env(tmp_path, config=config, mcp_mock=MockMCP()):
        named = (await create_key(env.client, "mcp-key"))["key"]
        other = (await create_key(env.client, "other-key"))["key"]
        sid_named = await _open_session(env.client, named)
        resp = await _post(
            env.client, _tools_call("get_weather", {"note": "forbidden"}), named, session=sid_named
        )
        assert resp.json()["error"]["code"] == GUARDRAIL_BLOCKED
        # The other key: out of scope, the call travels.
        sid_other = await _open_session(env.client, other)
        resp = await _post(
            env.client, _tools_call("get_weather", {"note": "forbidden"}), other, session=sid_other
        )
        assert resp.status_code == 200
        assert "error" not in resp.json()
        assert env.mcp.bodies("tools/call")
