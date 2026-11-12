"""Tests for log retention and PII-safe logging.

* ``logging.store_bodies: false`` — request/response bodies are not
  persisted to request logs (compliance mode); metadata still is.
* ``logging.retention_days`` — old request log rows are deleted by
  ``LoggingService.prune`` (run at startup and then periodically by the
  lifespan task); the prune unit takes an injectable clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gateway.config import LoggingConfig
from gateway.db import get_sessionmaker
from gateway.models import RequestLog
from gateway.observability.metrics import LOGS_PRUNED

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config


# ----------------------------------------------------------------------
# PII-safe mode: store_bodies: false
# ----------------------------------------------------------------------


async def test_store_bodies_false_omits_bodies(tmp_path):
    config = make_config(logging=LoggingConfig(store_bodies=False))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "compliance"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="my email is jane@example.com"),
            headers=auth(key),
        )
        assert resp.status_code == 200

        logs = await env.client.get("/admin/logs?limit=1", headers=auth(ADMIN_KEY))
        entry = logs.json()["data"][0]
        detail = await env.client.get(
            f"/admin/logs/{entry['id']}", headers=auth(ADMIN_KEY)
        )
        row = detail.json()
        # bodies are gone...
        assert row["request_body"] is None
        assert row["response_body"] is None
        # ...but the accounting metadata survives
        assert row["status"] == "success"
        assert row["model"] == "test-model"
        assert row["prompt_tokens"] > 0
        assert row["cost_usd"] > 0
        assert row["latency_ms"] is not None
        break


async def test_store_bodies_default_stores_bodies(env):
    key = (await create_key(env.client, "default-logging"))["key"]
    await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content="regular logging"),
        headers=auth(key),
    )
    logs = await env.client.get("/admin/logs?limit=1", headers=auth(ADMIN_KEY))
    entry = logs.json()["data"][0]
    detail = await env.client.get(
        f"/admin/logs/{entry['id']}", headers=auth(ADMIN_KEY)
    )
    assert detail.json()["request_body"]["messages"][0]["content"] == "regular logging"


# ----------------------------------------------------------------------
# Retention: prune()
# ----------------------------------------------------------------------


async def _insert_log(request_id: str, created_at: datetime) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            RequestLog(
                request_id=request_id,
                key_name="pruner",
                model="test-model",
                status="success",
                created_at=created_at,
            )
        )
        await session.commit()


async def test_prune_deletes_only_old_rows(tmp_path):
    config = make_config()
    async for env in _make_env(tmp_path, config=config):
        now = datetime.now(timezone.utc)
        await _insert_log("old-1", now - timedelta(days=9))
        await _insert_log("old-2", now - timedelta(days=11))
        await _insert_log("new-1", now - timedelta(days=1))

        service = env.app.state.pipeline.logging_service
        service.retention_days = 7  # simulate configured retention
        before = LOGS_PRUNED._value.get()
        assert await service.prune(now=now) == 2

        logs = await env.client.get("/admin/logs?limit=50", headers=auth(ADMIN_KEY))
        ids = [e["request_id"] for e in logs.json()["data"]]
        assert "new-1" in ids
        assert "old-1" not in ids and "old-2" not in ids
        assert LOGS_PRUNED._value.get() >= before + 2  # metric moved
        break


async def test_prune_noop_without_retention(tmp_path):
    config = make_config()
    async for env in _make_env(tmp_path, config=config):
        now = datetime.now(timezone.utc)
        await _insert_log("ancient", now - timedelta(days=3650))
        removed = await env.app.state.pipeline.logging_service.prune(now=now)
        assert removed == 0
        logs = await env.client.get("/admin/logs?limit=10", headers=auth(ADMIN_KEY))
        assert any(e["request_id"] == "ancient" for e in logs.json()["data"])
        break


async def test_prune_with_configured_retention(tmp_path):
    config = make_config(logging=LoggingConfig(retention_days=30))
    async for env in _make_env(tmp_path, config=config):
        now = datetime.now(timezone.utc)
        await _insert_log("elderly", now - timedelta(days=45))
        await _insert_log("fresh", now - timedelta(days=3))
        service = env.app.state.pipeline.logging_service
        assert service.retention_days == 30
        assert await service.prune(now=now) == 1
        break


async def test_store_bodies_false_and_retention_compose(tmp_path):
    """Both knobs are independent LoggingConfig settings."""
    cfg = LoggingConfig(store_bodies=False, retention_days=7)
    assert cfg.store_bodies is False
    assert cfg.retention_days == 7
    assert cfg.retention_interval_seconds == 3600.0
