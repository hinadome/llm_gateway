"""Tests for daily/monthly budget windows (per-key spend caps).

Windows are UTC calendar periods (day rolls at 00:00 UTC, month on the
1st). The current window's spend and its bucket id live on the key row:
a stored bucket that differs from the current one means the window has
rolled, so that spend counts as 0 (lazily reset by the next add_spend —
one atomic UPDATE with a CASE, so concurrent requests can't straddle a
boundary). Caps compose with, and are independent of, the total
``max_budget_usd``.

Tier 50 adds the projected stance on top of the windows: with
``budget_enforcement.enabled``, a monthly window whose month-end
RUN-RATE projection (snapshot spend over elapsed days, scaled to the
month — the same counter lens the hard cap reads) exceeds its budget
denies with the same 429 ``insufficient_quota``, the message saying it
is the projection, with the numbers. Off by default (the hard caps are
the floor, not a stance); the honesty gate (``min_elapsed_days``)
mirrors the forecast family — hours of evidence never deny; the
denial self-heals (a projection is a ratio, so a stopped key's
projection decays under its budget as the month elapses and traffic
resumes); and the hard cap always fires first when actual spend has
already crossed, so the two messages can never lie about each other.
Key and tenant legs share one rulebook.
"""

from __future__ import annotations

import asyncio
import calendar
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from gateway.config import BudgetEnforcementConfig
from gateway.core.context import AuthContext, RequestContext
from gateway.core.forecasts import run_rate_month_end
from gateway.db import get_sessionmaker
from gateway.errors import BudgetExceededError, BudgetProjectionError
from gateway.models import VirtualKey
from gateway.utils import daily_bucket, monthly_bucket

from .conftest import (
    ADMIN_KEY,
    _make_env,
    auth,
    chat_body,
    create_key,
    make_config,
)
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


# ----------------------------------------------------------------------
# The projected stance (tier 50): pure math, the gate, the traffic
# ----------------------------------------------------------------------

# A pinned month clock for the stance tests: _MID is day 18 at noon
# UTC (elapsed 17.5 of 30, comfortably past the honesty gate), _EARLY
# day 2 at noon (elapsed 1.5, inside the default gate), _LATE day 25
# at noon (elapsed 24.5 — the same spend decays under its budget).
_MID = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
_EARLY = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
_LATE = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def test_run_rate_month_end_math():
    # Mid-month, exact scaling: Nov 11 12:00 UTC = elapsed 10.5 of 30.
    proj = run_rate_month_end(
        datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc), 3.0
    )
    assert proj is not None
    assert proj["elapsed_days"] == 10.5
    assert proj["days_in_month"] == 30.0
    assert proj["projected_usd"] == pytest.approx(3.0 * 30.0 / 10.5)
    # February 2026: 28 days; noon on the 14th = elapsed 13.5.
    feb = run_rate_month_end(
        datetime(2026, 2, 14, 12, 0, tzinfo=timezone.utc), 2.8
    )
    assert feb["days_in_month"] == 28.0
    assert feb["elapsed_days"] == 13.5
    assert feb["projected_usd"] == pytest.approx(2.8 * 28.0 / 13.5)
    # A 31-day month: Jan 31 00:00 UTC = elapsed 30 of 31.
    jan = run_rate_month_end(
        datetime(2026, 1, 31, 0, 0, tzinfo=timezone.utc), 6.2
    )
    assert jan["days_in_month"] == 31.0
    assert jan["elapsed_days"] == 30.0
    assert jan["projected_usd"] == pytest.approx(6.2 * 31.0 / 30.0)
    # The month's exact first instant: nothing to divide by.
    assert (
        run_rate_month_end(
            datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc), 5.0
        )
        is None
    )
    # Zero spend projects zero, honestly.
    zero = run_rate_month_end(
        datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc), 0.0
    )
    assert zero is not None
    assert zero["projected_usd"] == 0.0


def test_budget_projection_error_wording():
    key = BudgetProjectionError(
        "trending",
        budget=10.0,
        spent=6.0,
        projected_usd=10.29,
        elapsed_days=17.5,
        days_in_month=30,
    )
    tenant = BudgetProjectionError(
        "acme",
        budget=10.0,
        spent=6.0,
        projected_usd=10.29,
        elapsed_days=17.5,
        days_in_month=30,
        kind="tenant",
    )
    assert key.message.startswith("Virtual key 'trending'")
    assert tenant.message.startswith("Tenant 'acme'")
    for err in (key, tenant):
        assert "is projected to spend $10.29 by month end" in err.message
        assert "day 17.5 of 30" in err.message
        assert "$10.00 monthly budget" in err.message
        assert "$6.0000 spent so far" in err.message
        assert err.status_code == 429
        assert err.code == "insufficient_quota"


def _gate_ctx(**fields) -> RequestContext:
    """A minimal RequestContext around a synthetic AuthContext —
    _check_budget reads only ctx.auth and the pipeline's config, so
    the gate's rules are pinnable without a request."""
    return RequestContext(
        request_id="gate-test", auth=AuthContext(key_id="k1", **fields)
    )


async def test_projected_leg_denies_trending_key(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(
            budget_enforcement=BudgetEnforcementConfig(enabled=True)
        ),
    ):
        pipeline = env.app.state.pipeline
        ctx = _gate_ctx(
            key_name="trending",
            monthly_budget_usd=10.0,
            monthly_spend_usd=6.0,
            monthly_bucket="2026-11",
        )
        # $6 of a $10 budget at day 17.5 of 30 projects to $10.29 —
        # denied BEFORE the budget is spent, with the numbers.
        with pytest.raises(BudgetProjectionError) as raised:
            pipeline._check_budget(ctx, now=_MID)
        assert "is projected to spend $10.29 by month end" in raised.value.message
        assert raised.value.status_code == 429
        assert raised.value.code == "insufficient_quota"


async def test_projected_leg_denies_trending_tenant(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(
            budget_enforcement=BudgetEnforcementConfig(enabled=True)
        ),
    ):
        pipeline = env.app.state.pipeline
        # key_id=None: the key leg is skipped, so the denial can only
        # come from the tenant block — placement pinned.
        ctx = RequestContext(
            request_id="gate-test",
            auth=AuthContext(
                tenant_id="t1",
                tenant_name="acme",
                tenant_monthly_budget_usd=10.0,
                tenant_monthly_spend_usd=6.0,
                tenant_monthly_bucket="2026-11",
            ),
        )
        with pytest.raises(BudgetProjectionError) as raised:
            pipeline._check_budget(ctx, now=_MID)
        assert raised.value.message.startswith("Tenant 'acme'")
        assert "is projected to spend" in raised.value.message


async def test_projected_leg_within_budget_passes(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(
            budget_enforcement=BudgetEnforcementConfig(enabled=True)
        ),
    ):
        pipeline = env.app.state.pipeline
        # $5 of $10 at day 17.5 projects to $8.57 — within plan,
        # traffic flows.
        ctx = _gate_ctx(
            key_name="healthy",
            monthly_budget_usd=10.0,
            monthly_spend_usd=5.0,
            monthly_bucket="2026-11",
        )
        pipeline._check_budget(ctx, now=_MID)


async def test_projected_leg_honesty_gate_holds(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(
            budget_enforcement=BudgetEnforcementConfig(enabled=True)
        ),
    ):
        pipeline = env.app.state.pipeline
        ctx = _gate_ctx(
            key_name="day-one",
            monthly_budget_usd=10.0,
            monthly_spend_usd=6.0,
            monthly_bucket="2026-11",
        )
        # At day 1.5 the raw run-rate projects to $120 — but the
        # default honesty gate (min_elapsed_days 2.0, the forecast
        # family's threshold) holds: hours of evidence never deny.
        pipeline._check_budget(ctx, now=_EARLY)


async def test_projected_leg_stale_bucket_counts_zero(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(
            budget_enforcement=BudgetEnforcementConfig(enabled=True)
        ),
    ):
        pipeline = env.app.state.pipeline
        # Last month's spend, this month's clock: the window rolled,
        # the stored spend counts as 0 — nothing to project. The
        # stance composes with the rollover, not against it.
        ctx = _gate_ctx(
            key_name="rolled",
            monthly_budget_usd=10.0,
            monthly_spend_usd=6.0,
            monthly_bucket="2026-10",
        )
        pipeline._check_budget(ctx, now=_MID)


async def test_projected_leg_hard_cap_fires_first(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(
            budget_enforcement=BudgetEnforcementConfig(enabled=True)
        ),
    ):
        pipeline = env.app.state.pipeline
        ctx = _gate_ctx(
            key_name="overspent",
            monthly_budget_usd=10.0,
            monthly_spend_usd=10.5,
            monthly_bucket="2026-11",
        )
        # Actual spend over budget is the floor's call, not the
        # stance's: the hard cap raises, with the original wording —
        # never a trend's clothes on an over-budget spend.
        with pytest.raises(BudgetExceededError) as raised:
            pipeline._check_budget(ctx, now=_MID)
        assert "has exceeded its monthly budget" in raised.value.message
        assert "projected" not in raised.value.message


async def test_projected_leg_self_heals(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(
            budget_enforcement=BudgetEnforcementConfig(enabled=True)
        ),
    ):
        pipeline = env.app.state.pipeline
        ctx = _gate_ctx(
            key_name="stopped",
            monthly_budget_usd=10.0,
            monthly_spend_usd=6.0,
            monthly_bucket="2026-11",
        )
        # Day 17.5: $6 projects to $10.29 — denied.
        with pytest.raises(BudgetProjectionError):
            pipeline._check_budget(ctx, now=_MID)
        # Same spend, day 24.5: the projection decayed to $7.35 —
        # the key stopped, the month moved, traffic resumed without
        # any operator action. The hard cap stays the wall; the
        # stance is a trend, and trends end.
        pipeline._check_budget(ctx, now=_LATE)


async def test_projected_stance_off_by_default_unit(env):
    # The default config: the stance is off, the identical context
    # that the enabled test denies sails through — today's behavior
    # (hard caps only) is exactly preserved.
    pipeline = env.app.state.pipeline
    ctx = _gate_ctx(
        key_name="trending",
        monthly_budget_usd=10.0,
        monthly_spend_usd=6.0,
        monthly_bucket="2026-11",
    )
    pipeline._check_budget(ctx, now=_MID)


async def test_projected_stance_off_by_default_traffic(env):
    # The same through HTTP: $9 of a $10 budget with the stance off —
    # passes on any date, whatever the run-rate would say.
    key = (await create_key(env.client, "trending", monthly_budget_usd=10.0))[
        "key"
    ]
    await _set_window(
        "trending",
        monthly_spend_usd=9.0,
        monthly_bucket=monthly_bucket(datetime.now(timezone.utc)),
    )
    resp = await env.client.post(
        "/v1/chat/completions", json=chat_body(content="hello"), headers=auth(key)
    )
    assert resp.status_code == 200


async def test_projected_stance_denies_trending_traffic(tmp_path):
    """The full path: HTTP request → _check_budget → the projected
    leg → 429. The seed is computed from the test's own clock so the
    denial holds on any day of any month: a spend strictly inside the
    denial band — above the budget's elapsed fraction (projects
    over), below the budget itself (the hard cap's territory) —
    wherever the month stands."""
    async for env in _make_env(
        tmp_path,
        config=make_config(
            budget_enforcement=BudgetEnforcementConfig(
                enabled=True, min_elapsed_days=0.0
            )
        ),
    ):
        key = (await create_key(env.client, "trending", monthly_budget_usd=10.0))[
            "key"
        ]
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elapsed_days = (now - month_start).total_seconds() / 86400.0
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        budget = 10.0
        spend = budget * (elapsed_days / days_in_month + 1.0) / 2.0
        await _set_window(
            "trending",
            monthly_spend_usd=spend,
            monthly_bucket=monthly_bucket(now),
        )
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello"),
            headers=auth(key),
        )
        if elapsed_days > 0:
            assert resp.status_code == 429
            body = resp.json()["error"]
            assert body["code"] == "insufficient_quota"
            assert "is projected to spend" in body["message"]
        else:
            # The month's exact first instant: elapsed 0, nothing to
            # divide by — the gate honestly cannot project, and the
            # request passes under the floor alone.
            assert resp.status_code == 200


async def test_projected_stance_honesty_gate_holds_traffic(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(
            budget_enforcement=BudgetEnforcementConfig(
                enabled=True, min_elapsed_days=40.0
            )
        ),
    ):
        key = (await create_key(env.client, "honest", monthly_budget_usd=10.0))[
            "key"
        ]
        # $9 of a $10 budget, whenever this runs: the raw projection
        # is over budget for most of any month, but no calendar month
        # ever reaches 40 elapsed days — the honesty gate holds on
        # every date, which is the point.
        await _set_window(
            "honest",
            monthly_spend_usd=9.0,
            monthly_bucket=monthly_bucket(datetime.now(timezone.utc)),
        )
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hello"), headers=auth(key)
        )
        assert resp.status_code == 200


async def test_projected_stance_no_budget_no_bar(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(
            budget_enforcement=BudgetEnforcementConfig(enabled=True)
        ),
    ):
        # No monthly budget → no bar, never a fake verdict: the
        # spend sits on the row, nothing to weigh it against.
        key = (await create_key(env.client, "unbudgeted"))["key"]
        await _set_window(
            "unbudgeted",
            monthly_spend_usd=100.0,
            monthly_bucket=monthly_bucket(datetime.now(timezone.utc)),
        )
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hello"), headers=auth(key)
        )
        assert resp.status_code == 200


async def test_projected_stance_stale_bucket_passes(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(
            budget_enforcement=BudgetEnforcementConfig(
                enabled=True, min_elapsed_days=0.0
            )
        ),
    ):
        # Last month's $50 under a $10 budget: the window rolled, the
        # spend counts as 0 — the stance composes with the rollover
        # through the full HTTP path too.
        key = (await create_key(env.client, "rolled", monthly_budget_usd=10.0))[
            "key"
        ]
        await _set_window("rolled", monthly_spend_usd=50.0, monthly_bucket="2000-01")
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hello"), headers=auth(key)
        )
        assert resp.status_code == 200


async def test_projected_stance_hard_cap_wins_traffic(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(
            budget_enforcement=BudgetEnforcementConfig(
                enabled=True, min_elapsed_days=0.0
            )
        ),
    ):
        # Spend AT the budget: the floor's call on any date — the
        # original wording, never the projection's.
        key = (await create_key(env.client, "overspent", monthly_budget_usd=10.0))[
            "key"
        ]
        await _set_window(
            "overspent",
            monthly_spend_usd=10.0,
            monthly_bucket=monthly_bucket(datetime.now(timezone.utc)),
        )
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hello"), headers=auth(key)
        )
        assert resp.status_code == 429
        body = resp.json()["error"]
        assert body["code"] == "insufficient_quota"
        assert "has exceeded its monthly budget" in body["message"]
        assert "projected" not in body["message"]
