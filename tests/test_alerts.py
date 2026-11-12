"""Tests for budget alerting (spend thresholds -> webhook delivery)."""

from __future__ import annotations

import asyncio
import inspect
import json
import time

import httpx
import pytest
from prometheus_client.registry import REGISTRY

from gateway.config import AlertsConfig
from gateway.core.alerts import BudgetAlertManager, crossed_levels

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config

# Pricing that makes one non-stream mock request (10 prompt tokens) cost
# $20: 10 tokens x $2/token ($2M per million).
BIG_PRICING = {"model-a": {"input": 2_000_000, "output": 0}}


class WebhookSink:
    """Captures webhook deliveries; configurable status for failure tests."""

    def __init__(self, status: int = 200) -> None:
        self.payloads: list[dict] = []
        self.attempts = 0
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        self.payloads.append(json.loads(request.content))
        return httpx.Response(self.status, json={"ok": True})


def alert_count(outcome: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "gateway_alerts_total", {"kind": "budget", "outcome": outcome}
        )
        or 0
    )


async def _make_alert_env(tmp_path, alerts: AlertsConfig, sink: WebhookSink):
    config = make_config(pricing=BIG_PRICING, alerts=alerts)
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for ns in _make_env(tmp_path, config=config, alert_client=ac):
            ns.webhooks = sink
            yield ns


async def _wait_for(check, timeout: float = 2.0, what: str = "condition"):
    """Poll a sync or async predicate until truthy or the deadline hits.

    Spend accounting and webhook delivery run in the pipeline's background
    task, so e2e tests must poll for their side effects.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def _key_row(client, key_id: str) -> dict:
    resp = await client.get(f"/admin/keys/{key_id}", headers=auth(ADMIN_KEY))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _spend_visible(client, key_id: str, expected: float) -> bool:
    return (await _key_row(client, key_id))["spend_usd"] == expected


# ----------------------------------------------------------------------
# crossed_levels / rearm_levels (pure)
# ----------------------------------------------------------------------


def test_crossed_levels_no_budget():
    assert crossed_levels(100.0, None, [50, 80], None) == []
    assert crossed_levels(100.0, 0.0, [50, 80], None) == []


@pytest.mark.parametrize(
    "spend,budget,expected",
    [
        (0.0, 100.0, []),                    # nothing spent
        (49.9, 100.0, []),                   # just below 50
        (50.0, 100.0, [50]),                 # boundary counts as crossed
        (79.9, 100.0, [50]),
        (100.0, 100.0, [100, 90, 80, 50]),   # exhausted: all, highest first
        (140.0, 100.0, [100, 90, 80, 50]),   # over budget
    ],
)
def test_crossed_levels_thresholds(spend, budget, expected):
    assert crossed_levels(spend, budget, [50, 80, 90, 100], None) == expected


def test_crossed_levels_dedupes_fired():
    assert crossed_levels(75.0, 100.0, [50, 80, 90, 100], [50]) == []
    assert crossed_levels(95.0, 100.0, [50, 80, 90, 100], [50, 80]) == [90]


def test_crossed_levels_custom_thresholds():
    assert crossed_levels(25.0, 100.0, [10, 25], None) == [25, 10]


async def test_rearm_levels_after_budget_change():
    manager = BudgetAlertManager(AlertsConfig(), client=_null_client())
    try:
        # spend 15 of 20 = 75%: only 50 is already crossed under the new budget
        assert manager.rearm_levels(15.0, 20.0) == [50]
        # budget raised to 40: 37.5% -> nothing crossed, alerts fully re-armed
        assert manager.rearm_levels(15.0, 40.0) == []
        # budget removed -> no alert state
        assert manager.rearm_levels(15.0, None) == []
    finally:
        await manager.aclose()


def _null_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )


# ----------------------------------------------------------------------
# End-to-end: spend crossing thresholds through the app
# ----------------------------------------------------------------------


@pytest.fixture
async def alert_env(tmp_path):
    sink = WebhookSink()
    alerts = AlertsConfig(webhook_url="http://alerts.test/hook", max_attempts=1)
    async for ns in _make_alert_env(tmp_path, alerts, sink):
        yield ns


async def test_budget_alerts_fire_once_per_level(alert_env):
    before_sent = alert_count("sent")
    key = await create_key(alert_env.client, "alerts", max_budget_usd=30.0)

    # Request 1 costs $20 of a $30 budget (66.7%): crosses 50 only.
    resp = await alert_env.client.post(
        "/v1/chat/completions", json=chat_body(content="one"), headers=auth(key["key"])
    )
    assert resp.status_code == 200
    await _wait_for(
        lambda: len(alert_env.webhooks.payloads) >= 1, what="first webhook"
    )

    # Request 2 costs another $20 (133% total): crosses 80, 90 and 100.
    resp = await alert_env.client.post(
        "/v1/chat/completions", json=chat_body(content="two"), headers=auth(key["key"])
    )
    assert resp.status_code == 200
    await _wait_for(
        lambda: len(alert_env.webhooks.payloads) >= 4, what="remaining webhooks"
    )

    levels = [p["level_percent"] for p in alert_env.webhooks.payloads]
    assert sorted(levels) == [50, 80, 90, 100]  # each exactly once — 50 did not re-fire

    first = alert_env.webhooks.payloads[0]
    assert first["type"] == "budget_alert"
    assert first["key_id"] == key["id"]
    assert first["key_name"] == "alerts"
    assert first["budget_usd"] == 30.0
    assert first["spend_usd"] == 20.0
    assert first["percent"] == 66.67
    assert "has spent" in first["message"]

    exhausted = next(p for p in alert_env.webhooks.payloads if p["level_percent"] == 100)
    assert "exhausted" in exhausted["message"]

    assert alert_count("sent") >= before_sent + 4

    # The fired state persists on the key row.
    row = await _key_row(alert_env.client, key["id"])
    assert row["budget_alerts_fired"] == [50, 80, 90, 100]
    assert row["spend_usd"] == 40.0


async def test_no_webhook_url_skips_delivery(tmp_path):
    # Default alerts config (no webhook_url): crossings must be counted
    # as "skipped", never delivered, and never break the request.
    sink = WebhookSink()
    before = alert_count("skipped")
    async for ns in _make_alert_env(tmp_path, AlertsConfig(), sink):
        key = await create_key(ns.client, "silent", max_budget_usd=10.0)
        resp = await ns.client.post(
            "/v1/chat/completions", json=chat_body(content="hi"), headers=auth(key["key"])
        )
        assert resp.status_code == 200
        await _wait_for(lambda: alert_count("skipped") > before, what="skipped metric")
        assert sink.attempts == 0
        row = await _key_row(ns.client, key["id"])
        assert row["budget_alerts_fired"] == [50, 80, 90, 100]


async def test_webhook_failure_retries_and_counts(tmp_path):
    sink = WebhookSink(status=500)
    alerts = AlertsConfig(webhook_url="http://alerts.test/hook", max_attempts=2)
    before_failed = alert_count("failed")
    async for ns in _make_alert_env(tmp_path, alerts, sink):
        key = await create_key(ns.client, "failing", max_budget_usd=10.0)
        resp = await ns.client.post(
            "/v1/chat/completions", json=chat_body(content="hi"), headers=auth(key["key"])
        )
        assert resp.status_code == 200  # delivery failure never breaks requests
        # 100% crossed in one request -> 4 alerts x 2 attempts each. The
        # 0.5s retry backoff makes this the slowest wait in the file.
        await _wait_for(
            lambda: sink.attempts >= 8, timeout=8.0, what="all delivery attempts"
        )
        assert alert_count("failed") >= before_failed + 4
        row = await _key_row(ns.client, key["id"])
        assert row["budget_alerts_fired"] == [50, 80, 90, 100]  # state marked anyway


async def test_budget_raise_rearms_alerts(alert_env):
    key = await create_key(alert_env.client, "rearm", max_budget_usd=30.0)

    resp = await alert_env.client.post(
        "/v1/chat/completions", json=chat_body(content="one"), headers=auth(key["key"])
    )
    assert resp.status_code == 200
    await _wait_for(
        lambda: len(alert_env.webhooks.payloads) >= 1, what="first webhook"
    )

    # Raise the budget: spend $20 of $60 = 33% -> nothing crossed under
    # the new budget, so all levels re-arm.
    patch = await alert_env.client.patch(
        f"/admin/keys/{key['id']}",
        json={"max_budget_usd": 60.0},
        headers=auth(ADMIN_KEY),
    )
    assert patch.status_code == 200
    assert patch.json()["budget_alerts_fired"] == []

    # The next request re-crosses 50% (40/60 = 66.7%) and fires again.
    resp = await alert_env.client.post(
        "/v1/chat/completions", json=chat_body(content="two"), headers=auth(key["key"])
    )
    assert resp.status_code == 200
    await _wait_for(
        lambda: len(alert_env.webhooks.payloads) >= 2, what="re-armed webhook"
    )
    assert [p["level_percent"] for p in alert_env.webhooks.payloads] == [50, 50]
    row = await _key_row(alert_env.client, key["id"])
    assert row["budget_alerts_fired"] == [50]


async def test_no_budget_no_alerts(alert_env):
    key = await create_key(alert_env.client, "no-budget")
    resp = await alert_env.client.post(
        "/v1/chat/completions", json=chat_body(content="hi"), headers=auth(key["key"])
    )
    assert resp.status_code == 200
    # Wait until the background spend task has committed ($20 at BIG_PRICING),
    # then confirm nothing was delivered and no levels were recorded.
    await _wait_for(
        lambda: _spend_visible(alert_env.client, key["id"], 20.0),
        what="spend task",
    )
    assert alert_env.webhooks.attempts == 0
    row = await _key_row(alert_env.client, key["id"])
    assert row["budget_alerts_fired"] is None  # no levels crossed -> never written


# ----------------------------------------------------------------------
# Concurrency: evaluations racing on the same entity
# ----------------------------------------------------------------------


async def test_concurrent_evaluations_fire_each_level_once(tmp_path):
    """Regression: two spend tasks can evaluate the same budget while
    both hold a stale fired snapshot (each request's spend UPDATE ...
    RETURNING predates the other's alert write — e.g. two keys pooling
    onto one tenant, or two requests on one key). A lost writer must
    re-read the winner's state, so every level delivers exactly once
    and the persisted set never loses levels.
    """
    sink = WebhookSink()
    alerts = AlertsConfig(webhook_url="http://alerts.test/hook", max_attempts=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_alert_env(tmp_path, alerts, sink):
            key = await create_key(env.client, "race", max_budget_usd=30.0)
            manager = BudgetAlertManager(alerts, client=ac)
            try:
                # Both hold fired=None; the spend snapshots are $20
                # (66.7%) and $40 (133%) — every level crosses in both
                # views, and both would fire all of them.
                await asyncio.gather(
                    manager.evaluate(key["id"], "race", 20.0, 30.0, None),
                    manager.evaluate(key["id"], "race", 40.0, 30.0, None),
                )
            finally:
                await manager.aclose()

            levels = sorted(p["level_percent"] for p in sink.payloads)
            assert levels == [50, 80, 90, 100]  # each exactly once
            row = await _key_row(env.client, key["id"])
            assert row["budget_alerts_fired"] == [50, 80, 90, 100]


async def test_concurrent_tenant_evaluations_fire_each_level_once(tmp_path):
    """The same race on the tenant side (two member keys' spend tasks
    pooling onto one tenant row) — kind="tenant" shares the guard."""
    sink = WebhookSink()
    alerts = AlertsConfig(webhook_url="http://alerts.test/hook", max_attempts=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_alert_env(tmp_path, alerts, sink):
            resp = await env.client.post(
                "/admin/tenants",
                json={"name": "race-co", "max_budget_usd": 30.0},
                headers=auth(ADMIN_KEY),
            )
            assert resp.status_code == 201, resp.text
            tenant = resp.json()

            manager = BudgetAlertManager(alerts, client=ac)
            try:
                await asyncio.gather(
                    manager.evaluate(
                        tenant["id"], "race-co", 20.0, 30.0, None, kind="tenant"
                    ),
                    manager.evaluate(
                        tenant["id"], "race-co", 40.0, 30.0, None, kind="tenant"
                    ),
                )
            finally:
                await manager.aclose()

            levels = sorted(p["level_percent"] for p in sink.payloads)
            assert levels == [50, 80, 90, 100]
            assert all(p["kind"] == "tenant" for p in sink.payloads)
            row = await env.client.get(
                f"/admin/tenants/{tenant['id']}", headers=auth(ADMIN_KEY)
            )
            assert row.status_code == 200
            assert row.json()["budget_alerts_fired"] == [50, 80, 90, 100]
