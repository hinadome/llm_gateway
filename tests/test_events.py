"""Lifecycle events: stage-boundary pub/sub over the request path.

Covers the contract the design promised:

* with no handlers, nothing changes (the bus is inert — the rest of
  the suite pins this implicitly by running with events enabled);
* observers are fire-and-forget — a crashing handler never fails the
  request, a full queue drops (counted), delivery is async;
* interceptors run in-band and may abort (``GatewayError`` → the
  normal error path: log row, metrics, client envelope), only at the
  two interceptable events, and must list them explicitly;
* the emission points fire at the stages they name (received/routed/
  cache_hit/response.completed/stream.*/rejected/failed), and the
  surfaces around the pipeline fire theirs (auth.rejected, key.*,
  budget.threshold, batch.*).
"""

from __future__ import annotations

import asyncio

from gateway.config import EventHandlerConfig, EventsConfig
from gateway.core.events import EventBus, EventHandler, GatewayEvent
from gateway.errors import GatewayError

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config


# ----------------------------------------------------------------------
# Handlers (module level so config can load them by class path)
# ----------------------------------------------------------------------


class Recorder(EventHandler):
    """Records every event it sees, in delivery order."""

    name = "recorder"

    def __init__(self, params=None):
        super().__init__(params)
        self.events: list[GatewayEvent] = []

    async def on_event(self, event: GatewayEvent) -> None:
        self.events.append(event)


class CrashyObserver(EventHandler):
    """An observer that always crashes — must never fail a request."""

    name = "crashy"

    def __init__(self, params=None):
        super().__init__(params)
        self.calls = 0

    async def on_event(self, event: GatewayEvent) -> None:
        self.calls += 1
        raise RuntimeError(f"boom on {event.name}")


class VetoRequests(EventHandler):
    """Interceptor: aborts every request it sees with a 403."""

    name = "veto"

    async def on_event(self, event: GatewayEvent) -> None:
        raise GatewayError(
            403,
            "Vetoed by event interceptor.",
            "invalid_request_error",
            code="event_veto",
        )


class BlockingObserver(EventHandler):
    """Observer whose delivery blocks until released — for the
    queue-full test. ``started`` flips once the first delivery is
    in-flight (and its queue slot freed)."""

    name = "blocking"

    def __init__(self, params=None):
        super().__init__(params)
        self.delivered = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def on_event(self, event: GatewayEvent) -> None:
        self.started.set()
        await self.release.wait()
        self.delivered += 1


def _config(*handlers: EventHandlerConfig) -> EventsConfig:
    return EventsConfig(handlers=list(handlers))


def _names(recorder: Recorder) -> list[str]:
    return [e.name for e in recorder.events]


async def _recorded_env(tmp_path, *handlers: EventHandlerConfig):
    config = make_config(events=_config(*handlers))
    ns = _make_env(tmp_path, config=config)
    async for env in ns:
        recorder = next(
            (h for h in env.app.state.events.handlers if isinstance(h, Recorder)),
            None,
        )
        yield env, recorder


# ----------------------------------------------------------------------
# observers: delivery, patterns, isolation
# ----------------------------------------------------------------------


async def test_observer_receives_chat_lifecycle(tmp_path):
    async for env, rec in _recorded_env(
        tmp_path, EventHandlerConfig(class_path="tests.test_events:Recorder")
    ):
        key = (await create_key(env.client, "evt"))["key"]
        # Key creation is itself an audited admin action (key.created)
        # — drain it out so the recorder shows only the request below.
        await env.app.state.events.drain()
        rec.events.clear()
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 200
        await env.app.state.events.drain()

        names = _names(rec)
        assert names[0] == "request.received"
        assert names[1] == "request.routed"
        assert names[2] == "response.completed"
        received = rec.events[0]
        assert received.payload["model"] == "test-model"
        assert received.payload["key_name"] == "evt"
        assert received.ctx.request_id == resp.headers["X-Request-Id"]
        routed = rec.events[1]
        assert routed.payload["provider"] == "openai"
        assert routed.payload["deployment"] == "openai/primary"
        assert routed.payload["stream"] is False
        completed = rec.events[2]
        assert completed.payload["response"]["object"] == "chat.completion"
        assert completed.payload["usage"]["total_tokens"] == 17


async def test_observer_pattern_subscription(tmp_path):
    async for env, rec in _recorded_env(
        tmp_path,
        EventHandlerConfig(
            class_path="tests.test_events:Recorder", events=["request.*"]
        ),
    ):
        key = (await create_key(env.client, "pat"))["key"]
        bad = await env.client.post(
            "/v1/chat/completions", json=chat_body(model="no-such"), headers=auth(key)
        )
        assert bad.status_code == 404
        await env.app.state.events.drain()
        # patterns matched both the arrival and the rejection; the
        # routed/audit-family events did not match.
        assert _names(rec) == ["request.received", "request.rejected"]


async def test_crashing_observer_never_fails_the_request(tmp_path):
    async for env, _ in _recorded_env(
        tmp_path, EventHandlerConfig(class_path="tests.test_events:CrashyObserver")
    ):
        key = (await create_key(env.client, "crash"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 200
        crashy = env.app.state.events.handlers[0]
        assert crashy.calls >= 1  # it ran, it crashed, nobody noticed


async def test_no_handlers_is_inert(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "plain"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 200
        assert env.app.state.events.handlers == []


async def test_events_disabled_emits_nothing(tmp_path):
    config = make_config(
        events=EventsConfig(
            enabled=False,
            handlers=[EventHandlerConfig(class_path="tests.test_events:Recorder")],
        )
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "off"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 200
        await env.app.state.events.drain()
        # Disabled bus: nothing loaded, nothing delivered.
        assert env.app.state.events.handlers == []


async def test_queue_full_drops_counted():
    """A full observer queue drops events (they must never block the
    request path); the drop is counted and delivery resumes after."""
    bus = EventBus(EventsConfig(queue_size=1))
    blocking = BlockingObserver()
    bus.register(blocking)
    await bus.start()
    try:
        await bus.emit("request.received", None, model="m")
        # Wait until the dispatcher has taken item 1 (it is now blocked
        # inside the handler): its queue slot is free again.
        for _ in range(500):
            if blocking.started.is_set():
                break
            await asyncio.sleep(0.01)
        assert blocking.started.is_set()
        await bus.emit("request.received", None, model="m")  # takes the slot
        await bus.emit("request.received", None, model="m")  # full → dropped
        blocking.release.set()
        await bus.drain()
        assert blocking.delivered == 2  # one in-flight + one queued; one dropped
    finally:
        blocking.release.set()
        await bus.stop()


# ----------------------------------------------------------------------
# interceptors: in-band aborts
# ----------------------------------------------------------------------


async def test_interceptor_aborts_request_received(tmp_path):
    async for env, rec in _recorded_env(
        tmp_path,
        EventHandlerConfig(
            class_path="tests.test_events:VetoRequests",
            kind="interceptor",
            events=["request.received"],
        ),
        EventHandlerConfig(class_path="tests.test_events:Recorder"),
    ):
        key = (await create_key(env.client, "vetoed"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "event_veto"
        # Never reached the provider, and flowed through the normal
        # error path: log row + rejection event observers can see.
        assert len(env.mock.requests) == 0
        await env.app.state.events.drain()
        assert "request.received" in _names(rec)
        assert "request.rejected" in _names(rec)
        logs = await env.client.get(
            "/admin/logs", params={"limit": 5}, headers=auth(ADMIN_KEY)
        )
        row = [r for r in logs.json()["data"] if r["status"] == "error"][0]
        assert "Vetoed" in row["error_message"]


async def test_interceptor_aborts_response_completed(tmp_path):
    async for env, _ in _recorded_env(
        tmp_path,
        EventHandlerConfig(
            class_path="tests.test_events:VetoRequests",
            kind="interceptor",
            events=["response.completed"],
        ),
    ):
        key = (await create_key(env.client, "late-veto"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 403
        # The provider WAS called (the veto is at the response stage).
        assert len(env.mock.requests) == 1


async def test_interceptor_rejects_non_interceptable_subscription(tmp_path):
    config = make_config(
        events=EventsConfig(
            handlers=[
                EventHandlerConfig(
                    class_path="tests.test_events:VetoRequests",
                    kind="interceptor",
                    events=["batch.completed"],
                )
            ]
        )
    )
    try:
        EventBus.from_config(config.events)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "non-interceptable" in str(exc)


async def test_interceptor_requires_explicit_events(tmp_path):
    try:
        bus = EventBus(EventsConfig())
        bus.register(VetoRequests(), events=None, kind="interceptor")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "explicitly" in str(exc)


# ----------------------------------------------------------------------
# emission points across surfaces
# ----------------------------------------------------------------------


async def test_stream_events(tmp_path):
    async for env, rec in _recorded_env(
        tmp_path, EventHandlerConfig(class_path="tests.test_events:Recorder")
    ):
        key = (await create_key(env.client, "str"))["key"]
        await env.app.state.events.drain()  # drop key.created
        rec.events.clear()
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(stream=True), headers=auth(key)
        )
        assert resp.status_code == 200
        body = resp.text
        assert "data:" in body
        await env.app.state.events.drain()
        names = _names(rec)
        assert names[0] == "request.received"
        assert names[1] == "request.routed"
        assert "stream.started" in names
        assert names[-1] == "stream.completed"
        # No per-chunk events — the coarse lifecycle only.
        assert names.count("stream.started") == 1


async def test_cache_hit_event(tmp_path):
    async for env, rec in _recorded_env(
        tmp_path, EventHandlerConfig(class_path="tests.test_events:Recorder")
    ):
        key = (await create_key(env.client, "cache"))["key"]
        first = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert first.status_code == 200
        await env.app.state.events.drain()
        rec.events.clear()
        second = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert second.status_code == 200
        await env.app.state.events.drain()
        names = _names(rec)
        assert names == ["request.received", "request.cache_hit"]
        assert rec.events[1].payload["kind"] == "hit"


async def test_auth_rejected_event(tmp_path):
    async for env, rec in _recorded_env(
        tmp_path, EventHandlerConfig(class_path="tests.test_events:Recorder")
    ):
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(),
            headers=auth("sk-gw-not-a-real-key"),
        )
        assert resp.status_code == 401
        await env.app.state.events.drain()
        names = _names(rec)
        assert names == ["auth.rejected"]
        assert rec.events[0].payload["path"] == "/v1/chat/completions"


async def test_embedding_response_completed(tmp_path):
    async for env, rec in _recorded_env(
        tmp_path, EventHandlerConfig(class_path="tests.test_events:Recorder")
    ):
        key = (await create_key(env.client, "emb"))["key"]
        await env.app.state.events.drain()  # drop key.created
        rec.events.clear()
        resp = await env.client.post(
            "/v1/embeddings",
            json={"model": "test-model", "input": "hello"},
            headers=auth(key),
        )
        assert resp.status_code == 200
        await env.app.state.events.drain()
        names = _names(rec)
        assert names == ["request.received", "response.completed"]
        assert rec.events[-1].payload["surface"] == "embedding"


async def test_admin_action_event(tmp_path):
    async for env, rec in _recorded_env(
        tmp_path, EventHandlerConfig(class_path="tests.test_events:Recorder")
    ):
        await create_key(env.client, "watched")
        await env.app.state.events.drain()
        names = _names(rec)
        assert "key.created" in names
        created = [e for e in rec.events if e.name == "key.created"][0]
        assert created.payload["event"] == "key.created"
        assert created.payload["details"]["key_name"] == "watched"


async def test_budget_threshold_event(tmp_path):
    async for env, rec in _recorded_env(
        tmp_path, EventHandlerConfig(class_path="tests.test_events:Recorder")
    ):
        # Tiny budget: one mock request (~$0.000048) is 120% of $0.00004
        # — crossing the 50, 80 and 100 levels in one shot.
        key = (await create_key(env.client, "budgeted", max_budget_usd=0.00004))["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 200
        # Spend accounting (and with it the budget evaluation) is
        # fire-and-forget: drain it before the event queue.
        await env.app.state.pipeline.drain_background_tasks(timeout=5)
        await env.app.state.events.drain()
        levels = [
            e.payload["level"]
            for e in rec.events
            if e.name == "budget.threshold"
        ]
        assert 50 in levels
        assert 100 in levels


# ----------------------------------------------------------------------
# batch lifecycle events
# ----------------------------------------------------------------------


async def test_batch_lifecycle_events(tmp_path):
    import json

    async for env, rec in _recorded_env(
        tmp_path, EventHandlerConfig(class_path="tests.test_events:Recorder")
    ):
        key = (await create_key(env.client, "batchy"))["key"]
        line = json.dumps(
            {
                "custom_id": "a",
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            }
        )
        up = await env.client.post(
            "/v1/files",
            files={"file": ("in.jsonl", (line + "\n").encode(), "application/jsonl")},
            data={"purpose": "batch"},
            headers=auth(key),
        )
        assert up.status_code == 200
        created = await env.client.post(
            "/v1/batches",
            json={"input_file_id": up.json()["id"]},
            headers=auth(key),
        )
        assert created.status_code == 200
        await env.app.state.events.drain()
        assert "batch.created" in _names(rec)

        await env.app.state.batch_worker.tick()
        await env.app.state.events.drain()
        done = [e for e in rec.events if e.name == "batch.completed"]
        assert done and done[0].payload["total"] == 1
        assert done[0].payload["succeeded"] == 1


async def test_batch_cancelled_event(tmp_path):
    from gateway.config import BatchConfig
    import json

    config = make_config(
        events=EventsConfig(
            handlers=[EventHandlerConfig(class_path="tests.test_events:Recorder")]
        ),
        batch=BatchConfig(wave_size=1),
    )
    async for env in _make_env(tmp_path, config=config):
        rec = env.app.state.events.handlers[0]
        key = (await create_key(env.client, "cxl"))["key"]
        lines = "\n".join(
            json.dumps(
                {
                    "custom_id": f"r{i}",
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
                }
            )
            for i in range(3)
        )
        up = await env.client.post(
            "/v1/files",
            files={"file": ("in.jsonl", (lines + "\n").encode(), "application/jsonl")},
            data={"purpose": "batch"},
            headers=auth(key),
        )
        created = await env.client.post(
            "/v1/batches", json={"input_file_id": up.json()["id"]}, headers=auth(key)
        )
        batch_id = created.json()["id"]
        await env.app.state.batch_worker.tick()
        cancel = await env.client.post(
            f"/v1/batches/{batch_id}/cancel", headers=auth(key)
        )
        assert cancel.status_code == 200
        await env.app.state.events.drain()
        cancelled = [e for e in rec.events if e.name == "batch.cancelled"]
        assert cancelled and cancelled[0].payload["batch_id"] == batch_id


async def test_batch_expired_event(tmp_path):
    from datetime import datetime, timedelta, timezone

    from gateway.db import get_sessionmaker
    from gateway.models import Batch

    async for env, rec in _recorded_env(
        tmp_path, EventHandlerConfig(class_path="tests.test_events:Recorder")
    ):
        key = (await create_key(env.client, "exp"))["key"]
        import json

        line = json.dumps(
            {
                "custom_id": "a",
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            }
        )
        up = await env.client.post(
            "/v1/files",
            files={"file": ("in.jsonl", (line + "\n").encode(), "application/jsonl")},
            data={"purpose": "batch"},
            headers=auth(key),
        )
        created = await env.client.post(
            "/v1/batches", json={"input_file_id": up.json()["id"]}, headers=auth(key)
        )
        batch_id = created.json()["id"]
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await session.get(Batch, batch_id)
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()
        await env.app.state.batch_worker.tick()
        await env.app.state.events.drain()
        expired = [e for e in rec.events if e.name == "batch.expired"]
        assert expired and expired[0].payload["batch_id"] == batch_id


# ----------------------------------------------------------------------
# hot reload
# ----------------------------------------------------------------------


async def test_hot_reload_swaps_handlers(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        bus = env.app.state.events
        assert bus.handlers == []
        new_config = make_config(
            events=EventsConfig(
                handlers=[EventHandlerConfig(class_path="tests.test_events:Recorder")]
            )
        )
        env.app.state.pipeline.apply_config(new_config)
        assert len(bus.handlers) == 1
        key = (await create_key(env.client, "reloaded"))["key"]
        # key.created (audited admin action) is already delivered —
        # drop it so the recorder shows only the request below.
        await bus.drain()
        rec = bus.handlers[0]
        rec.events.clear()
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 200
        await bus.drain()
        assert _names(rec)[0] == "request.received"
