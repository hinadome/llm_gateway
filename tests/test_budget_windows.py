"""Tests for daily/monthly budget windows (per-key spend caps).

Windows are UTC calendar periods (day rolls at 00:00 UTC, month on the
1st). The current window's spend and its bucket id live on the key row:
a stored bucket that differs from the current one means the window has
rolled, so that spend counts as 0 (lazily reset by the next add_spend —
one atomic UPDATE with a CASE, so concurrent requests can't straddle a
boundary). Caps compose with, and are independent of, the total
``max_budget_usd``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from gateway.db import get_sessionmaker
from gateway.errors import BudgetExceededError
from gateway.models import VirtualKey
from gateway.utils import daily_bucket, monthly_bucket

from .conftest import ADMIN_KEY, auth, chat_body, create_key
from .test_alerts import _key_row, _wait_for


# ----------------------------------------------------------------------
# Pure units: bucket ids + error message
# ----------------------------------------------------------------------


def test_bucket_helpers():
    now = datetime(2026, 11, 11, 9, 30, tzinfo=timezone.utc)
    assert daily_bucket(now) == "2026-11-11"
    assert monthly_bucket(now) == "2026-11"
    # non-UTC input is normalized: 01:00 on the 12th at +14:00 is still
    # the 11th in UTC
    shifted = datetime(2026, 11, 12, 1, 0, tzinfo=timezone(timedelta(hours=14)))
    assert daily_bucket(shifted) == "2026-11-11"
    assert monthly_bucket(shifted) == "2026-11"
    # month boundary
    edge = datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc)
    assert daily_bucket(edge) == "2026-12-01"
    assert monthly_bucket(edge) == "2026-12"


def test_budget_exceeded_error_period_wording():
    total = BudgetExceededError("k", 1.0, 2.0)
    daily = BudgetExceededError("k", 1.0, 2.0, period="daily")
    monthly = BudgetExceededError("k", 1.0, 2.0, period="monthly")
    assert "exceeded its budget" in total.message
    assert "daily" not in total.message
    assert "exceeded its daily budget" in daily.message
    assert "exceeded its monthly budget" in monthly.message
    assert all(e.status_code == 429 for e in (total, daily, monthly))
    assert all(e.code == "insufficient_quota" for e in (total, daily, monthly))


# ----------------------------------------------------------------------
# e2e: enforcement
# ----------------------------------------------------------------------


async def _set_window(name: str, **values) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            update(VirtualKey).where(VirtualKey.name == name).values(**values)
        )
        await session.commit()


async def _fresh(env, key_id: str) -> dict:
    return await _key_row(env.client, key_id)


async def _key_id(client, name: str) -> str:
    listing = await client.get("/admin/keys", headers=auth(ADMIN_KEY))
    return next(k["id"] for k in listing.json()["data"] if k["name"] == name)


def _field_check(env, key_id: str, field: str, expected, mode: str = "eq"):
    """Async predicate comparing a key-row field (eq / ge / gt)."""

    async def check() -> bool:
        row = await _key_row(env.client, key_id)
        value = row[field]
        if mode == "ge":
            return value >= expected
        if mode == "gt":
            return value > expected
        return value == expected

    return check


async def test_daily_window_enforced(env):
    # Cap so small that a single (nonzero-cost) request exhausts it: the
    # first request is allowed (spent 0 < cap), the second is not.
    key = (await create_key(env.client, "daily", daily_budget_usd=1e-9))["key"]
    first = await env.client.post(
        "/v1/chat/completions", json=chat_body(content="one"), headers=auth(key)
    )
    assert first.status_code == 200
    cost1 = float(first.headers["x-gateway-cost-usd"])
    assert cost1 > 0

    key_id = await _key_id(env.client, "daily")
    await _wait_for(
        _field_check(env, key_id, "daily_spend_usd", cost1, mode="ge"),
        what="daily spend visible",
    )
    fresh = await _fresh(env, key_id)
    assert fresh["daily_bucket"] == daily_bucket(datetime.now(timezone.utc))
    assert fresh["daily_spend_usd"] == cost1

    second = await env.client.post(
        "/v1/chat/completions", json=chat_body(content="two"), headers=auth(key)
    )
    assert second.status_code == 429
    error = second.json()["error"]
    assert error["code"] == "insufficient_quota"
    assert "daily budget" in error["message"]


async def test_monthly_window_enforced(env):
    key = (await create_key(env.client, "monthly", monthly_budget_usd=1e-9))["key"]
    first = await env.client.post(
        "/v1/chat/completions", json=chat_body(content="hello"), headers=auth(key)
    )
    assert first.status_code == 200

    key_id = await _key_id(env.client, "monthly")
    await _wait_for(
        _field_check(env, key_id, "monthly_spend_usd", 0, mode="gt"),
        what="monthly spend visible",
    )
    fresh = await _fresh(env, key_id)
    assert fresh["monthly_bucket"] == monthly_bucket(datetime.now(timezone.utc))

    second = await env.client.post(
        "/v1/chat/completions", json=chat_body(content="again"), headers=auth(key)
    )
    assert second.status_code == 429
    error = second.json()["error"]
    assert error["code"] == "insufficient_quota"
    assert "monthly budget" in error["message"]


async def test_stale_bucket_counts_as_zero(env):
    """Yesterday's spend does not count; the write lazily resets it."""
    key = (await create_key(env.client, "rolled", daily_budget_usd=1.0))["key"]
    await _set_window("rolled", daily_spend_usd=5.0, daily_bucket="2020-01-01")
    resp = await env.client.post(
        "/v1/chat/completions", json=chat_body(content="fresh day"), headers=auth(key)
    )
    assert resp.status_code == 200  # stale window spend ignored
    cost = float(resp.headers["x-gateway-cost-usd"])

    key_id = await _key_id(env.client, "rolled")
    await _wait_for(
        _field_check(
            env, key_id, "daily_bucket", daily_bucket(datetime.now(timezone.utc))
        ),
        what="bucket rolled to today",
    )
    fresh = await _fresh(env, key_id)
    assert fresh["daily_spend_usd"] == cost  # reset to just this request
    assert fresh["daily_spend_usd"] < 5.0


async def test_current_bucket_blocks(env):
    key = (await create_key(env.client, "spent-today", daily_budget_usd=1.0))["key"]
    today = daily_bucket(datetime.now(timezone.utc))
    await _set_window("spent-today", daily_spend_usd=5.0, daily_bucket=today)
    resp = await env.client.post(
        "/v1/chat/completions", json=chat_body(content="blocked"), headers=auth(key)
    )
    assert resp.status_code == 429
    assert "daily budget" in resp.json()["error"]["message"]


async def test_total_budget_message_has_no_period(env):
    """Total cap unchanged: exhausted total reports without window wording."""
    key = (await create_key(env.client, "total-only", max_budget_usd=1.0))["key"]
    await _set_window("total-only", spend_usd=2.0)
    resp = await env.client.post(
        "/v1/chat/completions", json=chat_body(content="hi"), headers=auth(key)
    )
    assert resp.status_code == 429
    message = resp.json()["error"]["message"]
    assert "budget" in message
    assert "daily budget" not in message and "monthly budget" not in message


# ----------------------------------------------------------------------
# e2e: accumulation + caches
# ----------------------------------------------------------------------


async def test_window_accumulates_across_requests(env):
    key = (await create_key(env.client, "accumulator", daily_budget_usd=10.0))["key"]
    first = await env.client.post(
        "/v1/chat/completions", json=chat_body(content="req one"), headers=auth(key)
    )
    second = await env.client.post(
        "/v1/chat/completions", json=chat_body(content="req two"), headers=auth(key)
    )
    assert first.status_code == 200 and second.status_code == 200
    expected = float(first.headers["x-gateway-cost-usd"]) + float(
        second.headers["x-gateway-cost-usd"]
    )

    key_id = await _key_id(env.client, "accumulator")
    await _wait_for(
        _field_check(env, key_id, "daily_spend_usd", expected),
        what="window spend accumulated",
    )
    fresh = await _fresh(env, key_id)
    assert fresh["monthly_spend_usd"] == expected  # same requests, same month
    # and the total budget tracks too
    assert fresh["spend_usd"] == expected


async def test_cache_hit_adds_no_window_spend(env):
    key = (await create_key(env.client, "cacheful", daily_budget_usd=10.0))["key"]
    first = await env.client.post(
        "/v1/chat/completions", json=chat_body(content="identical"), headers=auth(key)
    )
    assert first.headers["x-gateway-cache"] == "MISS"
    cost1 = float(first.headers["x-gateway-cost-usd"])

    key_id = await _key_id(env.client, "cacheful")
    await _wait_for(
        _field_check(env, key_id, "daily_spend_usd", cost1),
        what="first spend visible",
    )

    replay = await env.client.post(
        "/v1/chat/completions", json=chat_body(content="identical"), headers=auth(key)
    )
    assert replay.headers["x-gateway-cache"] == "HIT"
    await asyncio.sleep(0.25)  # any (wrong) background spend would land by now
    fresh = await _fresh(env, key_id)
    assert fresh["daily_spend_usd"] == cost1  # cache hits cost nothing


# ----------------------------------------------------------------------
# Admin API surface
# ----------------------------------------------------------------------


async def test_admin_api_window_fields(env):
    created = await create_key(
        env.client, "windowed", daily_budget_usd=5.0, monthly_budget_usd=50.0
    )
    assert created["daily_budget_usd"] == 5.0
    assert created["monthly_budget_usd"] == 50.0
    # state fields exposed even before any spend
    assert created["daily_spend_usd"] == 0.0
    assert created["daily_bucket"] is None

    patched = await env.client.patch(
        f"/admin/keys/{created['id']}",
        json={"daily_budget_usd": 2.5, "monthly_budget_usd": None},
        headers=auth(ADMIN_KEY),
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["daily_budget_usd"] == 2.5
    assert body["monthly_budget_usd"] is None
