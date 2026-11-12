"""Tests for background queued logging (``logging.background_write``).

The default logging service awaits the row INSERT in the response path —
a returned response implies its log row is queryable. The opt-in queued
service moves that write behind a bounded queue: the response path stops
paying the last DB round trip, a full queue drops rows (counted on
``gateway_logs_dropped_total``) instead of blocking, and ``stop()``
(tests + lifespan teardown) drains everything pending before the engine
goes away.
"""

from __future__ import annotations

import asyncio

from prometheus_client.registry import REGISTRY

from gateway.config import LoggingConfig
from gateway.core.logging_svc import LoggingService, QueuedLoggingService

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config
from .test_alerts import _wait_for


def _dropped() -> float:
    return REGISTRY.get_sample_value("gateway_logs_dropped_total") or 0


class _Row:
    """Minimal log-row payload (only request_id is touched on drop)."""

    def __init__(self, i: int) -> None:
        self.request_id = f"req-{i}"


# ----------------------------------------------------------------------
# End to end through the app
# ----------------------------------------------------------------------


async def test_background_write_rows_arrive(tmp_path):
    """With background_write on, a chat response's log row shows up via
    the queue consumer (not the response path)."""
    config = make_config(logging=LoggingConfig(background_write=True))
    async for env in _make_env(tmp_path, config=config):
        svc = env.app.state.pipeline.logging_service
        assert isinstance(svc, QueuedLoggingService)

        key = (await create_key(env.client, "bg-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello bg"),
            headers=auth(key),
        )
        assert resp.status_code == 200

        async def _row_logged() -> bool:
            listing = await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
            return any(
                e["key_name"] == "bg-user" and e["status"] == "success"
                for e in listing.json()["data"]
            )

        await _wait_for(_row_logged, what="background log row")
        # the queued service is a drop-in: same pipeline call sites
        assert svc.pending <= 1  # at most the row still in flight
        break


async def test_default_write_is_synchronous(tmp_path):
    """The default (background_write off) keeps the old invariant: the
    row is queryable the moment the response returns — what the rest
    of the suite (and admin UI reads right after a request) relies on."""
    async for env in _make_env(tmp_path):
        assert not isinstance(
            env.app.state.pipeline.logging_service, QueuedLoggingService
        )
        key = (await create_key(env.client, "sync-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello sync"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        listing = await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        assert any(
            e["key_name"] == "sync-user" for e in listing.json()["data"]
        ), "sync logging must make the row visible immediately"
        break


# ----------------------------------------------------------------------
# Queue mechanics (unit level — DB write stubbed out)
# ----------------------------------------------------------------------


async def test_full_queue_drops_and_counts(monkeypatch):
    """A full queue drops rows and counts them; every write either lands
    or is counted (conservation), and stop() drains what survived."""
    written: list[_Row] = []
    released = asyncio.Event()

    async def _held_write(self, ctx: _Row) -> None:
        # Block the consumer so the queue demonstrably fills.
        if ctx.request_id == "req-1":
            await released.wait()
        written.append(ctx)

    monkeypatch.setattr(LoggingService, "write_request_log", _held_write)
    svc = QueuedLoggingService(queue_size=2)

    before = _dropped()
    rows = [_Row(i) for i in range(6)]
    for row in rows:
        await svc.write_request_log(row)
    # No awaits inside the enqueue path: the consumer task exists but
    # has not run yet, so the queue holds 2 of the 6 rows.
    assert svc.pending == 2

    released.set()
    await svc.stop()

    # Conservation: 6 writes = written + dropped (no third outcome).
    dropped_delta = _dropped() - before
    assert len(written) + int(dropped_delta) == 6
    assert dropped_delta >= 3  # 6 writes vs capacity 2 + 1 in flight
    # First-come-first-served: the survivors are the earliest rows.
    assert written == rows[: len(written)]
    assert svc.pending == 0


async def test_stop_drains_pending(monkeypatch):
    """stop() writes out everything still queued, then the consumer is
    gone (a later write lazily starts a fresh one)."""
    written: list[_Row] = []

    async def _write(self, ctx: _Row) -> None:
        written.append(ctx)

    monkeypatch.setattr(LoggingService, "write_request_log", _write)
    svc = QueuedLoggingService(queue_size=10)

    rows = [_Row(i) for i in range(5)]
    for row in rows:
        await svc.write_request_log(row)
    assert svc.pending == 5  # nothing has yielded to the consumer yet

    await svc.stop()
    assert written == rows  # drained in order, nothing dropped
    assert svc.pending == 0
    assert svc._consumer is None
    assert _dropped() == _dropped()  # no drops involved

    # After stop, a new write lazily restarts the consumer — the
    # service stays usable rather than silently swallowing rows.
    extra = _Row(99)
    await svc.write_request_log(extra)
    await svc.stop()
    assert written == rows + [extra]


async def test_stop_without_any_writes_is_a_no_op(monkeypatch):
    """stop() before anything was queued (fresh service, no consumer)
    must not hang or raise."""
    svc = QueuedLoggingService(queue_size=4)
    await svc.stop()
    assert svc.pending == 0
