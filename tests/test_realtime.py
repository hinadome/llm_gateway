"""Tests for the realtime (WebSocket) surface — tier 32.

``WS /v1/realtime?model=<public-model>`` is the fifth inbound transport
and the first stateful one: one request-per-response HTTP becomes one
long-lived session holding an upstream WebSocket for its life. The
suite drives the FastAPI app's websocket scope directly (the httpx
ASGITransport the rest of the suite uses speaks HTTP only) against a
scripted OpenAI-shaped realtime upstream, and pins:

* the open gates — auth (headers and ``?key=``), the ``realtime``
  endpoint scope, model permission, model requirement, budget (at
  open AND re-checked per response.create), a concurrency slot held
  for the session's life, and realtime capability
  (``realtime_base``) — each rejected accept-first with an
  OpenAI-shaped realtime error event + close 1008;
* routing — the residency- and breaker-filtered ladder walked at
  CONNECT time only, failing over between handshakes (dead openai leg
  → azure leg, authenticating with ``api-key``), and the BYOK vault
  riding the tenant's stored key on the upstream handshake;
* the per-event policy — the request phase over ``response.create``
  (blocked creates never reach upstream, the session survives), the
  stream phase + held-back rewriting over ``output_text.delta``
  (nothing the rewriter masks leaks, no matter how the provider
  chunked it), and mid-response stream blocks (error event,
  ``response.cancel`` upstream, the rest of that response suppressed
  while its ``response.done`` still bills);
* accounting — ONE request-log row per session, tokens/cost/TTFT
  summed across responses, usage mapped input→prompt, and
  ``gateway_realtime_sessions`` semantics.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets.asyncio.server  # noqa: F401 — binds websockets.asyncio
from cryptography.fernet import Fernet

from gateway.config import (
    ConcurrencyConfig,
    DeploymentConfig,
    ModelConfig,
    ProviderConfig,
)
from gateway.guardrails.base import Guardrail, GuardrailResult

from .conftest import ADMIN_KEY, _make_env, auth, create_key, make_config
from .test_alerts import _wait_for


# ----------------------------------------------------------------------
# A WebSocket client driving the app's ASGI interface directly
# ----------------------------------------------------------------------


class WSClosed(Exception):
    """The server closed the session; ``code``/``reason`` mirror the
    close frame (1008 = policy, per the route's _reject)."""

    def __init__(self, message: dict) -> None:
        self.code = message.get("code")
        self.reason = message.get("reason", "")
        super().__init__(f"websocket closed: {self.code} {self.reason}")


class WSClient:
    """Hand-rolled ASGI driver for one WebSocket session.

    Queues carry messages both directions; the app runs as a task, so
    ``close()`` can wait for the route to finish — the session teardown
    (log row, metrics, slot release) runs as the endpoint returns.
    """

    def __init__(
        self,
        app,
        path: str,
        key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.app = app
        self.path = path
        self.to_app: asyncio.Queue = asyncio.Queue()
        self.from_app: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.closed: dict | None = None
        hdrs = dict(headers or {})
        if key is not None and "authorization" not in {k.lower() for k in hdrs}:
            hdrs["Authorization"] = f"Bearer {key}"
        self.headers = hdrs

    async def connect(self) -> None:
        path, _, query = self.path.partition("?")
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": [
                (k.lower().encode(), v.encode()) for k, v in self.headers.items()
            ],
            "client": ("127.0.0.1", 54321),
            "server": ("gateway.test", 80),
            "subprotocols": [],
        }

        async def receive():
            return await self.to_app.get()

        async def send(message):
            await self.from_app.put(message)

        # The ASGI handshake: the connect message must be queued BEFORE
        # the app task starts — Starlette's WebSocket.accept() drains
        # it first, so nothing arrives until the client has spoken.
        await self.to_app.put({"type": "websocket.connect"})
        self.task = asyncio.create_task(self.app(scope, receive, send))
        first = await self.from_app.get()
        assert first["type"] == "websocket.accept", first

    async def recv(self) -> dict:
        """Next ASGI message from the app; raises WSClosed on close."""
        message = await self.from_app.get()
        if message["type"] == "websocket.close":
            self.closed = message
            raise WSClosed(message)
        return message

    async def recv_json(self) -> dict:
        return json.loads((await self.recv())["text"])

    async def send_json(self, event: dict) -> None:
        await self.to_app.put(
            {"type": "websocket.receive", "text": json.dumps(event)}
        )

    async def close(self, code: int = 1000) -> None:
        """Client-side disconnect, then WAIT for the app: the log row
        and the slot release land before the caller asserts on them."""
        await self.to_app.put({"type": "websocket.disconnect", "code": code})
        await self.wait()

    async def wait(self) -> None:
        if self.task is not None:
            await asyncio.wait_for(self.task, timeout=10)


# ----------------------------------------------------------------------
# A scripted OpenAI-shaped realtime upstream
# ----------------------------------------------------------------------


class MockRealtime:
    """``websockets`` server standing in for the provider: records the
    handshake (path + headers) and every client event, then scripts one
    response per ``response.create`` — ``response.created``, text
    deltas, ``output_text.done`` and a usage-carrying ``response.done``.
    ``response.cancel`` is recorded (the relay suppresses the rest of
    the response on its side; the terminal done still bills).
    """

    def __init__(
        self,
        responses: list[tuple[str, ...]] | None = None,
        usage: dict | None = None,
    ) -> None:
        self.responses = list(responses or [("Hel", "lo!")])
        self.usage = usage or {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}
        self.handshakes: list[dict] = []
        self.events: list[dict] = []
        self.cancels: list[str] = []
        self.server = None
        self.port = None

    async def start(self) -> "MockRealtime":
        self.server = await websockets.asyncio.server.serve(
            self._handler, "127.0.0.1", 0
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    @property
    def base_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _handler(self, connection) -> None:
        self.handshakes.append(
            {
                "path": connection.request.path,
                "headers": dict(connection.request.headers),
            }
        )
        await connection.send(
            json.dumps({"type": "session.created", "session": {"id": "sess_test"}})
        )
        async for raw in connection:
            event = json.loads(raw)
            self.events.append(event)
            if event.get("type") == "response.create":
                await self._script_response(connection)
            elif event.get("type") == "response.cancel":
                self.cancels.append(event.get("response_id"))

    async def _script_response(self, connection) -> None:
        rid = f"resp_{len(self.events)}"
        deltas = self.responses[min(len(self.responses) - 1, len(self.events) - 1)]
        await connection.send(
            json.dumps({"type": "response.created", "response": {"id": rid}})
        )
        for delta in deltas:
            await connection.send(
                json.dumps(
                    {
                        "type": "response.output_text.delta",
                        "response_id": rid,
                        "delta": delta,
                    }
                )
            )
            # Yield between deltas: a mid-response stream block on the
            # relay side gets its response.cancel upstream while the
            # response is still streaming.
            await asyncio.sleep(0.02)
        await connection.send(
            json.dumps(
                {
                    "type": "response.output_text.done",
                    "response_id": rid,
                    "text": "".join(deltas),
                }
            )
        )
        await connection.send(
            json.dumps(
                {
                    "type": "response.done",
                    "response": {
                        "id": rid,
                        "status": "completed",
                        "usage": self.usage,
                    },
                }
            )
        )


# ----------------------------------------------------------------------
# Test guardrails (registered on the live manager, like the tier-8 tests)
# ----------------------------------------------------------------------


class _RequestBlocker(Guardrail):
    """Request phase: blocks any input containing 'forbidden'."""

    name = "request_blocker"

    async def check_request(self, request, ctx):
        text = "\n".join(
            m.content if isinstance(m.content, str) else ""
            for m in request.messages
        )
        if "forbidden" in text:
            return GuardrailResult.block("forbidden word in the input")
        return None


class _StreamBlocker(Guardrail):
    """Stream phase: blocks the moment 'forbidden' reaches the SOURCE."""

    name = "stream_blocker"

    async def check_stream_delta(self, request, accumulated_text, delta_text, ctx):
        if "forbidden" in accumulated_text:
            return GuardrailResult.block("forbidden word in model output")
        return None


class _Masker(Guardrail):
    """Stream rewriter: masks 'SECRET' (split across chunks too) with
    '******', holding back any suffix that could still grow into one."""

    name = "secret_masker"
    supports_stream_rewrite = True

    async def rewrite_stream_text(self, text, *, final=False):
        masked = text.replace("SECRET", "******")
        if final:
            return masked, ""
        for size in range(min(len(masked), 5), 0, -1):
            tail = masked[-size:]
            if "SECRET".startswith(tail):
                return masked[:-size], tail
        return masked, ""


# ----------------------------------------------------------------------
# Config + assertion helpers
# ----------------------------------------------------------------------

DEAD_PORT = "ws://127.0.0.1:1"  # nothing listens: immediate refusal


def _rt_config(mock: MockRealtime | None = None, **overrides):
    """make_config with an openai provider whose realtime_base points at
    the mock (or a dead port, for tests that never reach the handshake)."""
    providers = overrides.pop("providers", None) or {
        "openai": ProviderConfig(
            api_base="http://mockupstream.test/v1",
            api_key="provider-key",
            realtime_base=(mock.base_url if mock is not None else DEAD_PORT),
        )
    }
    return make_config(providers=providers, **overrides)


def _create(text: str) -> dict:
    """A response.create carrying its own input (fork semantics)."""
    return {
        "type": "response.create",
        "response": {
            "instructions": "Answer briefly.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                }
            ],
        },
    }


async def _drain_until_done(ws: WSClient) -> list[dict]:
    """Collect client events through a response.done."""
    events = []
    while True:
        event = await ws.recv_json()
        events.append(event)
        if event["type"] == "response.done":
            return events
        assert event["type"] != "error", event


async def _session_row(client) -> dict | None:
    """The session's request-log row, once the background write lands."""
    logs = await client.get("/admin/logs", headers=auth(ADMIN_KEY))
    for row in reversed(logs.json()["data"]):
        if row["route"] == "realtime":
            return row
    return None


async def _rejected(ws: WSClient, *, code: str | None = None, message: str | None = None):
    """Drain the session-shaped rejection: an OpenAI-shaped realtime
    error event, then close 1008. Returns the error payload."""
    event = await ws.recv_json()
    assert event["type"] == "error", event
    error = event["error"]
    if code is not None:
        assert error.get("code") == code, error
    if message is not None:
        assert message in error["message"], error
    with pytest.raises(WSClosed) as closed_info:
        await ws.recv()
    assert closed_info.value.code == 1008
    await ws.wait()
    return error


# ----------------------------------------------------------------------
# The happy path + per-session accounting
# ----------------------------------------------------------------------


async def test_realtime_happy_path_relays_events_and_bills_the_session(tmp_path):
    mock = await MockRealtime().start()
    try:
        async for env in _make_env(tmp_path, config=_rt_config(mock)):
            key = (await create_key(env.client, "rt-user"))["key"]
            ws = WSClient(env.app, "/v1/realtime?model=test-model", key=key)
            await ws.connect()
            await ws.send_json(_create("hello"))

            assert (await ws.recv_json())["type"] == "session.created"
            types = [e["type"] for e in await _drain_until_done(ws)]
            assert types == [
                "response.created",
                "response.output_text.delta",
                "response.output_text.delta",
                "response.output_text.done",
                "response.done",
            ]
            await ws.close()

            # The upstream handshake authenticated the OpenAI way and
            # addressed the DEPLOYMENT's native model.
            assert len(mock.handshakes) == 1
            assert mock.handshakes[0]["path"] == "/realtime?model=model-a"
            assert (
                mock.handshakes[0]["headers"]["authorization"]
                == "Bearer provider-key"
            )
            # The create reached the upstream verbatim (nothing blocked).
            assert [e["type"] for e in mock.events] == ["response.create"]

            await _wait_for(
                lambda: _session_row(env.client), what="session log row"
            )
            row = await _session_row(env.client)
            assert row is not None
            assert row["route"] == "realtime"
            assert row["status"] == "success"
            assert row["stream"] is True
            assert row["provider"] == "openai"
            assert row["prompt_tokens"] == 5
            assert row["completion_tokens"] == 3
            assert row["ttft_ms"] is not None
            assert row["cost_usd"] == pytest.approx(2.2e-5)  # 5*2 + 3*4 per 1M
            break
    finally:
        await mock.stop()


async def test_realtime_one_row_per_session_sums_responses(tmp_path):
    """A session is ONE request-log row: two responses, tokens/cost
    summed, TTFT from the first delta only."""
    mock = await MockRealtime().start()
    try:
        async for env in _make_env(tmp_path, config=_rt_config(mock)):
            key = (await create_key(env.client, "rt-user"))["key"]
            ws = WSClient(env.app, "/v1/realtime?model=test-model", key=key)
            await ws.connect()
            await ws.recv_json()  # session.created
            await ws.send_json(_create("one"))
            await _drain_until_done(ws)
            await ws.send_json(_create("two"))
            await _drain_until_done(ws)
            await ws.close()

            await _wait_for(
                lambda: _session_row(env.client), what="session log row"
            )
            rows = await _session_row(env.client)
            assert rows is not None
            assert rows["prompt_tokens"] == 10  # 5 + 5
            assert rows["completion_tokens"] == 6  # 3 + 3
            assert rows["cost_usd"] == pytest.approx(4.4e-5)
            assert rows["status"] == "success"

            detail = (
                await env.client.get(
                    f"/admin/logs/{rows['id']}", headers=auth(ADMIN_KEY)
                )
            ).json()
            assert detail["response_body"]["responses"] == 2
            assert detail["response_body"]["text"] == "Hello!"
            break
    finally:
        await mock.stop()


# ----------------------------------------------------------------------
# The open gates — accept first, error event + close 1008
# ----------------------------------------------------------------------


async def test_realtime_requires_a_credential(tmp_path):
    async for env in _make_env(tmp_path):
        ws = WSClient(env.app, "/v1/realtime?model=test-model")
        await ws.connect()
        await _rejected(
            ws, message="Missing API key"
        )  # same wording the HTTP surface uses


async def test_realtime_rejects_a_bad_key(tmp_path):
    async for env in _make_env(tmp_path):
        ws = WSClient(env.app, "/v1/realtime?model=test-model", key="sk-invalid")
        await ws.connect()
        await _rejected(ws, code="invalid_api_key")


async def test_realtime_accepts_query_param_key(tmp_path):
    """Browsers cannot set WebSocket headers — ?key= works, like the
    Gemini HTTP surface."""
    mock = await MockRealtime().start()
    try:
        async for env in _make_env(tmp_path, config=_rt_config(mock)):
            key = (await create_key(env.client, "browser"))["key"]
            ws = WSClient(env.app, f"/v1/realtime?model=test-model&key={key}")
            await ws.connect()
            assert (await ws.recv_json())["type"] == "session.created"
            await ws.close()
            assert len(mock.handshakes) == 1
            break
    finally:
        await mock.stop()


async def test_realtime_requires_a_model(tmp_path):
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "modelless"))["key"]
        ws = WSClient(env.app, "/v1/realtime", key=key)
        await ws.connect()
        await _rejected(ws, message="Missing 'model'")


async def test_realtime_model_permission(tmp_path):
    """The key's model allowlist binds sessions like requests."""
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "restricted", allowed_models=["other-model"]))[
            "key"
        ]
        ws = WSClient(env.app, "/v1/realtime?model=test-model", key=key)
        await ws.connect()
        await _rejected(ws, code="model_not_found", message="not available for this API key")


async def test_realtime_endpoint_scope(tmp_path):
    """"realtime" is its own endpoint scope: a chat-only key is
    rejected; a realtime-scoped key holds sessions."""
    mock = await MockRealtime().start()
    try:
        async for env in _make_env(tmp_path, config=_rt_config(mock)):
            chat_only = (await create_key(env.client, "chatty", allowed_endpoints=["chat"]))[
                "key"
            ]
            ws = WSClient(env.app, "/v1/realtime?model=test-model", key=chat_only)
            await ws.connect()
            await _rejected(ws, code="endpoint_not_allowed", message="'realtime' endpoint")

            scoped = (
                await create_key(env.client, "scoped", allowed_endpoints=["realtime"])
            )["key"]
            ws = WSClient(env.app, "/v1/realtime?model=test-model", key=scoped)
            await ws.connect()
            assert (await ws.recv_json())["type"] == "session.created"
            await ws.close()
            break
    finally:
        await mock.stop()


async def test_realtime_requires_a_realtime_capable_deployment(tmp_path):
    """A provider without realtime_base is not realtime-capable: the
    session is rejected with a clear code, never a silent HTTP
    fallback."""
    async for env in _make_env(tmp_path):  # default provider has no realtime_base
        key = (await create_key(env.client, "caller"))["key"]
        ws = WSClient(env.app, "/v1/realtime?model=test-model", key=key)
        await ws.connect()
        error = await _rejected(ws, code="realtime_unavailable")
        assert "realtime_base" in error["message"]
        break


# ----------------------------------------------------------------------
# Per-event policy: guardrails on the session
# ----------------------------------------------------------------------


async def test_realtime_request_phase_blocks_the_create_not_the_session(tmp_path):
    """A BLOCKed response.create never reaches the upstream; the session
    lives on and the next, clean create serves normally."""
    mock = await MockRealtime().start()
    try:
        async for env in _make_env(tmp_path, config=_rt_config(mock)):
            env.app.state.pipeline.guardrails.register(_RequestBlocker())
            key = (await create_key(env.client, "rt-user"))["key"]
            ws = WSClient(env.app, "/v1/realtime?model=test-model", key=key)
            await ws.connect()
            await ws.recv_json()  # session.created

            await ws.send_json(_create("you are forbidden to answer"))
            event = await ws.recv_json()
            assert event["type"] == "error"
            assert event["error"]["code"] == "guardrail_blocked"
            assert "request_blocker" in event["error"]["message"]

            # The session survived: a clean create goes through and is
            # answered in full.
            await ws.send_json(_create("nice question"))
            types = [e["type"] for e in await _drain_until_done(ws)]
            assert types[0] == "response.created"
            assert "response.output_text.delta" in types
            await ws.close()

            # The forbidden create never left the gateway.
            creates = [e for e in mock.events if e["type"] == "response.create"]
            assert len(creates) == 1
            assert "you are forbidden" not in json.dumps(mock.events)
            break
    finally:
        await mock.stop()


async def test_realtime_stream_rewriter_masks_across_chunks(tmp_path):
    """The held-back rewrite protocol, realtime-shaped: 'SECRET' split
    across deltas never leaks, no matter how the provider chunked it."""
    mock = await MockRealtime(responses=[("SE", "CRET!", " done")]).start()
    try:
        async for env in _make_env(tmp_path, config=_rt_config(mock)):
            env.app.state.pipeline.guardrails.register(_Masker())
            key = (await create_key(env.client, "rt-user"))["key"]
            ws = WSClient(env.app, "/v1/realtime?model=test-model", key=key)
            await ws.connect()
            await ws.recv_json()  # session.created

            await ws.send_json(_create("tell me the code"))
            deltas = []
            for event in await _drain_until_done(ws):
                if event["type"] == "response.output_text.delta":
                    deltas.append(event["delta"])
                if event["type"] == "response.output_text.done":
                    assert event["text"] == "******! done"
            assert "".join(deltas) == "******! done"
            await ws.close()
            break
    finally:
        await mock.stop()


async def test_realtime_stream_block_cancels_and_suppresses_but_bills(tmp_path):
    """A mid-response stream BLOCK: error event to the client,
    response.cancel upstream, the rest of that response suppressed —
    while its response.done still bills (the provider ran)."""
    mock = await MockRealtime(
        responses=[("Hel", "lo forbidden!", " done"), ("clean ", "text")]
    ).start()
    try:
        async for env in _make_env(tmp_path, config=_rt_config(mock)):
            env.app.state.pipeline.guardrails.register(_StreamBlocker())
            key = (await create_key(env.client, "rt-user"))["key"]
            ws = WSClient(env.app, "/v1/realtime?model=test-model", key=key)
            await ws.connect()
            await ws.recv_json()  # session.created

            await ws.send_json(_create("anything"))
            seen = []
            while True:
                event = await ws.recv_json()
                seen.append(event)
                if event["type"] == "error":
                    assert event["error"]["code"] == "guardrail_blocked"
                    break
            # The blocked delta never reached the client, and neither did
            # the rest of the response (its done carries the source text).
            assert not any(
                "forbidden" in json.dumps(e)
                for e in seen
                if e["type"] != "error"
            )

            # The session survives: the next response streams clean.
            await ws.send_json(_create("try again"))
            await _drain_until_done(ws)
            await ws.close()

            # The provider was told to stop the blocked response.
            assert mock.cancels == ["resp_1"]
            # BOTH responses billed (the cancelled one's done counted).
            await _wait_for(
                lambda: _session_row(env.client), what="session log row"
            )
            row = await _session_row(env.client)
            assert row is not None
            assert row["status"] == "success"
            assert row["prompt_tokens"] == 10
            assert row["completion_tokens"] == 6
            break
    finally:
        await mock.stop()


async def test_realtime_conversation_mirror_blocks_inputless_create(tmp_path):
    """A create WITHOUT input runs on the conversation state, which the
    relay shadows: a forbidden item earlier in the session blocks it —
    while a create carrying its own input (fork semantics) is judged on
    that input alone."""
    mock = await MockRealtime().start()
    try:
        async for env in _make_env(tmp_path, config=_rt_config(mock)):
            env.app.state.pipeline.guardrails.register(_RequestBlocker())
            key = (await create_key(env.client, "rt-user"))["key"]
            ws = WSClient(env.app, "/v1/realtime?model=test-model", key=key)
            await ws.connect()
            await ws.recv_json()  # session.created

            # A forbidden item joins the conversation...
            await ws.send_json(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "forbidden things"}
                        ],
                    },
                }
            )
            # ...so an inputless create implies it and is blocked.
            await ws.send_json({"type": "response.create"})
            event = await ws.recv_json()
            assert event["type"] == "error"
            assert event["error"]["code"] == "guardrail_blocked"

            # Fork semantics: a create with its own clean input is judged
            # on that input and serves in full.
            await ws.send_json(_create("clean question"))
            await _drain_until_done(ws)
            await ws.close()

            # Only the clean create reached the upstream.
            creates = [e for e in mock.events if e["type"] == "response.create"]
            assert len(creates) == 1
            assert "input" in creates[0]["response"]
            break
    finally:
        await mock.stop()


# ----------------------------------------------------------------------
# Budget + concurrency: the gates a long-lived session re-checks
# ----------------------------------------------------------------------


async def test_realtime_budget_exhaustion_mid_session(tmp_path):
    """Budgets bind at open AND per response: one affordable response,
    then the second create is refused — error event, close 1008, and
    the row says why."""
    mock = await MockRealtime().start()
    try:
        async for env in _make_env(tmp_path, config=_rt_config(mock)):
            # 5+3 tokens at 2.0/4.0 per 1M = 2.2e-5 per response: a
            # 2.0e-5 budget lets exactly one response through.
            key = (await create_key(env.client, "budgeted", max_budget_usd=2.0e-5))["key"]
            ws = WSClient(env.app, "/v1/realtime?model=test-model", key=key)
            await ws.connect()
            await ws.recv_json()  # session.created

            await ws.send_json(_create("first"))
            await _drain_until_done(ws)

            await ws.send_json(_create("second"))
            await _rejected(ws, code="insufficient_quota", message="exceeded its budget")

            await _wait_for(
                lambda: _session_row(env.client), what="session log row"
            )
            row = await _session_row(env.client)
            assert row is not None
            assert row["status"] == "error"
            assert "exceeded its budget" in row["error_message"]
            # Only the first response ran.
            assert row["prompt_tokens"] == 5
            assert row["cost_usd"] == pytest.approx(2.2e-5)
            break
    finally:
        await mock.stop()


async def test_realtime_concurrency_slot_held_for_session_life(tmp_path):
    """max_concurrency counts SESSIONS: the second session queues,
    times out (wait_seconds 0) and is refused — until the first
    session's close frees the slot."""
    mock = await MockRealtime().start()
    try:
        config = _rt_config(
            mock, concurrency=ConcurrencyConfig(enabled=True, wait_seconds=0)
        )
        async for env in _make_env(tmp_path, config=config):
            key = (await create_key(env.client, "single", max_concurrency=1))["key"]
            first = WSClient(env.app, "/v1/realtime?model=test-model", key=key)
            await first.connect()
            await first.recv_json()  # session.created — the slot is held

            second = WSClient(env.app, "/v1/realtime?model=test-model", key=key)
            await second.connect()
            await _rejected(second, message="Concurrency limit reached")

            # Closing the first frees the slot for the next session.
            await first.close()
            third = WSClient(env.app, "/v1/realtime?model=test-model", key=key)
            await third.connect()
            assert (await third.recv_json())["type"] == "session.created"
            await third.close()
            break
    finally:
        await mock.stop()


# ----------------------------------------------------------------------
# Routing: connect-time failover + BYOK on the handshake
# ----------------------------------------------------------------------


async def test_realtime_connect_time_failover_to_azure(tmp_path):
    """The ladder is walked at CONNECT time only: a dead openai leg
    moves the session to azure, which authenticates with the api-key
    header (not Bearer) and serves the session for its life."""
    azure_mock = await MockRealtime().start()
    try:
        config = make_config(
            providers={
                "openai": ProviderConfig(
                    api_base="http://mockupstream.test/v1",
                    api_key="platform-openai",
                    realtime_base=DEAD_PORT,
                ),
                "azure": ProviderConfig(
                    api_base="http://mockupstream.test",
                    api_key="platform-azure",
                    api_version="2024-02-01",
                    realtime_base=azure_mock.base_url,
                ),
            },
            models=[
                ModelConfig(
                    name="test-model",
                    deployments=[
                        DeploymentConfig(
                            name="openai/primary", provider="openai", model="model-a"
                        ),
                        DeploymentConfig(
                            name="azure/fallback",
                            provider="azure",
                            model="gpt-4o-deploy",
                            priority=200,
                        ),
                    ],
                )
            ],
            pricing={
                "model-a": {"input": 2.0, "output": 4.0},
                "gpt-4o-deploy": {"input": 2.0, "output": 4.0},
            },
        )
        async for env in _make_env(tmp_path, config=config):
            key = (await create_key(env.client, "rt-user"))["key"]
            ws = WSClient(env.app, "/v1/realtime?model=test-model", key=key)
            await ws.connect()
            await ws.recv_json()  # session.created — served by azure
            await ws.send_json(_create("hello"))
            await _drain_until_done(ws)
            await ws.close()

            # The azure handshake: deployment-addressed native model,
            # authenticated the Azure way.
            assert len(azure_mock.handshakes) == 1
            assert azure_mock.handshakes[0]["path"] == "/realtime?model=gpt-4o-deploy"
            assert azure_mock.handshakes[0]["headers"]["api-key"] == "platform-azure"

            await _wait_for(
                lambda: _session_row(env.client), what="session log row"
            )
            row = await _session_row(env.client)
            assert row is not None
            assert row["provider"] == "azure"
            assert row["deployment"] == "azure/fallback"
            break
    finally:
        await azure_mock.stop()


async def test_realtime_byok_rides_the_tenants_key(tmp_path):
    """The vault applies to the realtime handshake like every serving
    path: a tenant that keyed openai connects with THEIR key, and the
    row records the BYOK leg."""
    mock = await MockRealtime().start()
    master = Fernet.generate_key().decode()
    try:
        async for env in _make_env(
            tmp_path, config=_rt_config(mock), vault_master_key=master
        ):
            tenant = (
                await env.client.post(
                    "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
                )
            ).json()
            member = (
                await create_key(env.client, "member", tenant_id=tenant["id"])
            )["key"]
            put = await env.client.put(
                f"/admin/tenants/{tenant['id']}/provider-keys/openai",
                json={"key": "sk-acme-rt-1234", "label": "acme-prod"},
                headers=auth(ADMIN_KEY),
            )
            assert put.status_code == 200, put.text

            ws = WSClient(env.app, "/v1/realtime?model=test-model", key=member)
            await ws.connect()
            await ws.recv_json()  # session.created
            await ws.send_json(_create("hello"))
            await _drain_until_done(ws)
            await ws.close()

            assert len(mock.handshakes) == 1
            assert (
                mock.handshakes[0]["headers"]["authorization"]
                == "Bearer sk-acme-rt-1234"
            )
            await _wait_for(
                lambda: _session_row(env.client), what="session log row"
            )
            row = await _session_row(env.client)
            assert row is not None
            assert row["byok_provider"] == "openai"
            break
    finally:
        await mock.stop()
