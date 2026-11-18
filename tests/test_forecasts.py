"""Spend forecasts — tier 47: the invoice's forward-looking sibling.

The contract pinned here, in one paragraph: the invoice states what
each key and tenant consumed this month; the forecast answers where
the month will land. Two projections over the same client-consumed
rows (shadow mirrors and trial replays excluded, cache hits included
— the billing lens): the **run-rate** (spend so far over the elapsed
fraction, scaled to the month) and the **recent pace** (the last 7
days' daily spend carried over the remaining days — the correction
for spenders that stopped or accelerated), with the trend ratio
(recent daily over month-to-date daily) beside them and the
projected-vs-budget verdicts (over / at_risk ≥80% / within) decided
by the recent pace when one exists — ``verdict_based_on`` says
which, and both projections always ship side by side. The honesty
rules are the epic's: a COMPLETE month serves its actual total as
the projection (``month_complete: true``, trend null — the invoice
IS the final bill); a future or malformed month is the invoice's
400; a trend needs two days of month (less is hours of evidence
dressed as a line — null, never a guess), and a zero month-to-date
average with recent spend is a null trend too, never a fabricated
infinity. Every existing key appears — an unused key projects $0 of
its budget, information, not an absence — and keys whose row is
gone project where they lived with no budget to compare against.
Tenant admins see their own tenant only (forced); plain keys get
the admin 403; the clock context (elapsed / remaining days) ships in
the envelope so every number is auditable against it.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from gateway.core.forecasts import project, rollup
from gateway.db import get_sessionmaker
from gateway.models import RequestLog, VirtualKey

from .conftest import ADMIN_KEY, _make_env, auth, create_key
from .test_tenants import _create_tenant

_NOW = datetime.now(timezone.utc)
_THIS_MONTH = f"{_NOW.year:04d}-{_NOW.month:02d}"
_DAYS_IN_MONTH = calendar.monthrange(_NOW.year, _NOW.month)[1]
if _NOW.month == 12:
    _NEXT_MONTH = f"{_NOW.year + 1:04d}-01"
    _LAST_MONTH = f"{_NOW.year:04d}-12"
    _LAST_MONTH_DATE = datetime(_NOW.year, 12, 15, tzinfo=timezone.utc)
else:
    _NEXT_MONTH = f"{_NOW.year:04d}-{_NOW.month + 1:02d}"
    _LAST_MONTH = f"{_NOW.year:04d}-{_NOW.month - 1:02d}"
    _LAST_MONTH_DATE = datetime(_NOW.year, _NOW.month - 1, 15, tzinfo=timezone.utc)


def _log(
    request_id: str,
    *,
    key_id: str | None = None,
    key_name: str | None = None,
    tenant_id: str | None = None,
    cost: float = 0.0,
    created_at: datetime,
    shadow: bool = False,
    trial_id: str | None = None,
) -> RequestLog:
    """One request-log row for the forecast lens — money and time are
    all this surface reads (requests counted, tokens ignored)."""
    return RequestLog(
        request_id=request_id,
        key_id=key_id,
        key_name=key_name,
        tenant_id=tenant_id,
        model="test-model",
        status="success",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cost_usd=cost,
        cache_hit=None,
        shadow=shadow or None,
        trial_id=trial_id,
        created_at=created_at,
    )


async def _seed(*rows: RequestLog) -> None:
    async with get_sessionmaker()() as session:
        for row in rows:
            session.add(row)
        await session.commit()


def _approx(value: float) -> float:
    """The e2e recomputes from the body's rounded clock context
    (elapsed/remaining ship at 4 decimals), so tight-but-not-exact is
    the honest comparison."""
    return pytest.approx(value, rel=1e-3)


# -- pure units: the projection math ----------------------------------------


def test_project_run_rate_and_recent_pace():
    """Mid-month: the run-rate straight-lines the elapsed average
    ($100 over 12 of 30 days → $250 projected), the recent pace
    carries the last 7 days over the remaining 18 ($30/7/day on top
    of the booked $100), and the trend ratio says which is bigger —
    0.51, the recent pace is half the month's average."""
    block = project(
        100.0, 50,
        elapsed_days=12.0, remaining_days=18.0, days_in_month=30.0,
        recent_spend=30.0, recent_window_days=7.0, month_complete=False,
    )
    assert block["spend_so_far_usd"] == 100.0
    assert block["requests"] == 50
    assert block["run_rate"] == {"daily_usd": 8.333333, "projected_month_usd": 250.0}
    # 100 + (30/7)*18 = 100 + 77.142857
    assert block["recent_pace"] == {
        "daily_usd": 4.285714, "projected_month_usd": 177.142857,
    }
    # (30/7) / (100/12) = 0.514285… → 0.51
    assert block["trend_ratio"] == 0.51


def test_project_recent_window_bounded_to_month():
    """Early in the month the recent window is however much month has
    elapsed, never a divide-by-seven that pretends week one lasted a
    week. Day 1.5: the window is 1.5 days — and under two days of
    month there is no trend at all (null, not a ratio on hours of
    evidence); the two projections are arithmetically identical
    because the recent window IS the month so far. Day 5: the window
    is 5 days and the trend compares honestly (recent/month, not
    recent/7)."""
    day_one = project(
        3.0, 3,
        elapsed_days=1.5, remaining_days=28.5, days_in_month=30.0,
        recent_spend=3.0, recent_window_days=7.0, month_complete=False,
    )
    assert day_one["trend_ratio"] is None
    assert day_one["recent_pace"]["daily_usd"] == 2.0  # 3 / 1.5, not 3 / 7
    assert day_one["recent_pace"]["projected_month_usd"] == 60.0
    assert day_one["run_rate"]["projected_month_usd"] == 60.0  # the identity

    day_five = project(
        100.0, 10,
        elapsed_days=5.0, remaining_days=25.0, days_in_month=30.0,
        recent_spend=50.0, recent_window_days=7.0, month_complete=False,
    )
    assert day_five["recent_pace"]["daily_usd"] == 10.0  # 50 / 5, not 50 / 7
    assert day_five["trend_ratio"] == 0.5  # (50/5) / (100/5)


def test_project_complete_month_is_actuals():
    """A finished month is not forecast: both projections are the
    actual total, the recent daily pace is null (the pace of a
    finished month projects nothing), and the trend is null — the
    invoice IS the final bill, and a projection of the past would be
    a lie told with extra steps."""
    block = project(
        310.0, 900,
        elapsed_days=31.0, remaining_days=0.0, days_in_month=31.0,
        recent_spend=0.0, recent_window_days=7.0, month_complete=True,
    )
    assert block["run_rate"] == {
        "daily_usd": 10.0, "projected_month_usd": 310.0,
    }
    assert block["recent_pace"] == {
        "daily_usd": None, "projected_month_usd": 310.0,
    }
    assert block["trend_ratio"] is None


def test_project_stopped_accelerated_and_started_from_zero():
    """The recent pace earns its keep: a spender that stopped projects
    flat (trend 0.0 — the run-rate would keep pretending), a spender
    that accelerated projects the acceleration (trend > 1, above the
    run-rate), and a key that spent nothing until today trends
    against a zero average — which is null, never a fabricated
    infinity (the recent-pace projection still ships: $0 booked plus
    the new pace over the rest)."""
    stopped = project(
        100.0, 10,
        elapsed_days=20.0, remaining_days=10.0, days_in_month=30.0,
        recent_spend=0.0, recent_window_days=7.0, month_complete=False,
    )
    assert stopped["trend_ratio"] == 0.0
    assert stopped["recent_pace"]["projected_month_usd"] == 100.0  # flat
    assert stopped["run_rate"]["projected_month_usd"] == 150.0  # the lie it corrects

    accelerated = project(
        100.0, 10,
        elapsed_days=20.0, remaining_days=10.0, days_in_month=30.0,
        recent_spend=50.0, recent_window_days=7.0, month_complete=False,
    )
    assert accelerated["trend_ratio"] == 1.43  # (50/7) / 5
    assert accelerated["recent_pace"]["projected_month_usd"] == 171.428571

    from_zero = project(
        0.0, 0,
        elapsed_days=5.0, remaining_days=25.0, days_in_month=30.0,
        recent_spend=10.0, recent_window_days=7.0, month_complete=False,
    )
    assert from_zero["trend_ratio"] is None  # zero average: null, never ∞
    assert from_zero["recent_pace"]["projected_month_usd"] == 50.0  # 0 + 2/day × 25


def test_rollup_verdicts_sorting_and_inclusions():
    """The statement rules at every level: verdicts against budgets
    (over / at_risk at exactly 80% / within; no budget → honest
    nulls), the effective projection (recent pace when one exists)
    deciding both the verdict and the ordering — the month-end
    numbers lead — and the invoice's inclusions: zero-spend keys
    with budgets (projecting $0 of them), deleted keys projecting
    where they lived with no budget, platform keys in the
    unassigned bucket, tenant totals recomputed from the raw rows."""
    month_rows = [
        {"tenant_id": "t1", "key_id": "k1", "key_name": "overshoot", "requests": 1, "cost_usd": 8.0},
        {"tenant_id": "t1", "key_id": "k2", "key_name": "risk", "requests": 1, "cost_usd": 4.0},
        {"tenant_id": "t1", "key_id": "k3", "key_name": "fine", "requests": 1, "cost_usd": 1.0},
        {"tenant_id": "t1", "key_id": "k5", "key_name": "gone", "requests": 1, "cost_usd": 12.0},
        {"tenant_id": None, "key_id": "k6", "key_name": "platform", "requests": 1, "cost_usd": 0.5},
    ]
    recent_rows = [
        {"tenant_id": "t1", "key_id": "k1", "key_name": "overshoot", "requests": 1, "cost_usd": 8.0},
        {"tenant_id": "t1", "key_id": "k3", "key_name": "fine", "requests": 1, "cost_usd": 1.0},
    ]
    keys_by_id = {
        "k1": {"name": "overshoot", "tenant_id": "t1", "monthly_budget_usd": 10.0},
        "k2": {"name": "risk", "tenant_id": "t1", "monthly_budget_usd": 5.0},
        "k3": {"name": "fine", "tenant_id": "t1", "monthly_budget_usd": 10.0},
        "k4": {"name": "unused", "tenant_id": "t1", "monthly_budget_usd": 2.0},
    }
    tenants_by_id = {"t1": {"name": "Acme", "monthly_budget_usd": 20.0}}

    out = rollup(
        month_rows, recent_rows, keys_by_id, tenants_by_id,
        elapsed_days=20.0, remaining_days=10.0, days_in_month=30.0,
        month_complete=False,
    )
    # Tenants: just Acme; the platform row is unassigned.
    assert [t["tenant_name"] for t in out["tenants"]] == ["Acme"]
    assert [u["tenant_id"] for u in out["unassigned"]] == [None]
    acme = out["tenants"][0]

    # Keys by effective (recent-pace) projection: 19.43 > 12 > 4 > 2.43 > 0.
    assert [k["key_name"] for k in acme["keys"]] == [
        "overshoot", "gone", "risk", "fine", "unused",
    ]
    by_name = {k["key_name"]: k for k in acme["keys"]}

    assert by_name["overshoot"]["projected_pct_of_budget"] == 194.29
    assert by_name["overshoot"]["verdict"] == "over_budget"
    assert by_name["overshoot"]["verdict_based_on"] == "recent_pace"

    # risk: budget 5, spend 4, stopped — flat 4.0 projected = exactly 80%.
    assert by_name["risk"]["projected_pct_of_budget"] == 80.0
    assert by_name["risk"]["verdict"] == "at_risk"

    assert by_name["fine"]["projected_pct_of_budget"] == 24.29
    assert by_name["fine"]["verdict"] == "within_budget"

    # unused: zero spend, included, honestly projecting $0 of $2.
    assert by_name["unused"]["spend_so_far_usd"] == 0.0
    assert by_name["unused"]["recent_pace"]["projected_month_usd"] == 0.0
    assert by_name["unused"]["projected_pct_of_budget"] == 0.0
    assert by_name["unused"]["verdict"] == "within_budget"
    assert by_name["unused"]["trend_ratio"] is None  # zero average: no trend

    # gone: rows without a key row — projects where it lived, no budget.
    assert by_name["gone"]["spend_so_far_usd"] == 12.0
    assert by_name["gone"]["monthly_budget_usd"] is None
    assert by_name["gone"]["verdict"] is None
    assert by_name["gone"]["verdict_based_on"] is None

    # The tenant: raw-row totals (25 booked, 9 recent), pooled budget.
    assert acme["spend_so_far_usd"] == 25.0
    assert acme["requests"] == 4
    assert acme["recent_pace"]["projected_month_usd"] == 37.857143  # 25 + (9/7)*10
    assert acme["run_rate"]["projected_month_usd"] == 37.5
    assert acme["trend_ratio"] == 1.03
    assert acme["projected_pct_of_budget"] == 189.29
    assert acme["verdict"] == "over_budget"

    # Totals over everything (25.5 booked, 9 recent — unassigned had none).
    assert out["totals"]["spend_so_far_usd"] == 25.5
    assert out["totals"]["requests"] == 5
    assert out["totals"]["recent_pace"]["projected_month_usd"] == 38.357143
    assert out["totals"]["trend_ratio"] == 1.01


# -- e2e: the endpoint -------------------------------------------------------


async def test_forecasts_endpoint_seeded(tmp_path):
    """The seeded story end to end: a two-phase spender (old pace,
    recent pace), an unused key with a budget, a key deleted after
    its traffic, and a platform key — every number recomputed from
    the envelope's own clock context (elapsed / remaining days ship
    in the body, so the response is auditable against itself), the
    verdicts against real budgets, and the month-end ordering."""
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(
            env.client, "Acme", monthly_budget_usd=20.0
        )
        alpha = await create_key(
            env.client, "alpha", tenant_id=acme["id"], monthly_budget_usd=10.0
        )
        beta = await create_key(
            env.client, "beta", tenant_id=acme["id"], monthly_budget_usd=2.0
        )
        gamma = await create_key(env.client, "gamma", tenant_id=acme["id"])
        epsilon = await create_key(env.client, "epsilon")
        now = datetime.now(timezone.utc)

        # alpha: $20 ten days ago (outside the 7-day window) + $5
        # yesterday (inside it) — a key whose pace has slowed.
        await _seed(*[
            _log(f"a-old-{i}", key_id=alpha["id"], key_name="alpha",
                 tenant_id=acme["id"], cost=1.0,
                 created_at=now - timedelta(days=10, minutes=i))
            for i in range(20)
        ])
        await _seed(*[
            _log(f"a-new-{i}", key_id=alpha["id"], key_name="alpha",
                 tenant_id=acme["id"], cost=1.0,
                 created_at=now - timedelta(days=1, minutes=i))
            for i in range(5)
        ])
        # gamma: one row, then the key row is deleted outright.
        await _seed(
            _log("g1", key_id=gamma["id"], key_name="gamma",
                 tenant_id=acme["id"], cost=0.889,
                 created_at=now - timedelta(minutes=2))
        )
        async with get_sessionmaker()() as session:
            await session.execute(
                delete(VirtualKey).where(VirtualKey.id == gamma["id"])
            )
            await session.commit()
        # epsilon: a platform key (no tenant).
        await _seed(
            _log("e1", key_id=epsilon["id"], key_name="epsilon",
                 cost=0.05, created_at=now - timedelta(minutes=2))
        )

        resp = await env.client.get("/admin/forecasts", headers=auth(ADMIN_KEY))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["object"] == "forecast"
        assert body["month"] == _THIS_MONTH
        assert body["period"]["since"] == f"{_THIS_MONTH}-01T00:00:00+00:00"
        assert "generated_at" in body
        assert body["month_complete"] is False
        assert body["days_in_month"] == _DAYS_IN_MONTH
        assert body["recent_window_days"] == 7.0
        # The clock context the numbers hang on.
        elapsed = body["elapsed_days"]
        remaining = body["remaining_days"]
        assert elapsed > 0 and remaining > 0
        assert round(elapsed + remaining, 4) == _DAYS_IN_MONTH

        # The projections are self-consistent against the body's own
        # bounds — the auditability the envelope owes.
        assert body["totals"]["spend_so_far_usd"] == 25.939
        assert body["totals"]["requests"] == 27
        expected_run = 25.939 / elapsed * _DAYS_IN_MONTH
        # Totals include the unassigned bucket: recent spend is
        # alpha's $5 + gamma's $0.889 + epsilon's $0.05.
        expected_recent = 25.939 + (5.939 / 7.0) * remaining
        assert body["totals"]["run_rate"]["projected_month_usd"] == _approx(expected_run)
        assert body["totals"]["recent_pace"]["projected_month_usd"] == _approx(expected_recent)

        # Tenants: Acme only (epsilon is unassigned).
        assert [t["tenant_name"] for t in body["tenants"]] == ["Acme"]
        acme_section = body["tenants"][0]
        assert acme_section["spend_so_far_usd"] == 25.889
        assert acme_section["monthly_budget_usd"] == 20.0
        # 25.889 + (5.889/7)*remaining against $20 — comfortably over.
        assert acme_section["verdict"] == "over_budget"
        assert acme_section["verdict_based_on"] == "recent_pace"

        # Keys by month-end projection: alpha (~33.7) > gamma (~2.4) > beta (0).
        assert [k["key_name"] for k in acme_section["keys"]] == [
            "alpha", "gamma", "beta",
        ]
        alpha_key, gamma_key, beta_key = acme_section["keys"]

        assert alpha_key["spend_so_far_usd"] == 25.0
        assert alpha_key["run_rate"]["daily_usd"] == _approx(25.0 / elapsed)
        assert alpha_key["recent_pace"]["daily_usd"] == _approx(5.0 / 7.0)
        # (5/7) / (25/elapsed) — the slowdown, as a ratio under 1.
        assert alpha_key["trend_ratio"] == round((5.0 / 7.0) / (25.0 / elapsed), 2)
        assert alpha_key["verdict"] == "over_budget"  # ~33.7 against $10
        assert alpha_key["verdict_based_on"] == "recent_pace"

        # gamma: projected where it lived, no budget to compare against.
        assert gamma_key["spend_so_far_usd"] == 0.889
        assert gamma_key["monthly_budget_usd"] is None
        assert gamma_key["verdict"] is None
        assert gamma_key["verdict_based_on"] is None

        # beta: zero spend, honestly projecting $0 of its $2.
        assert beta_key["spend_so_far_usd"] == 0.0
        assert beta_key["projected_pct_of_budget"] == 0.0
        assert beta_key["verdict"] == "within_budget"

        # The platform key rides the unassigned bucket.
        (unassigned,) = body["unassigned"]
        assert unassigned["tenant_id"] is None
        assert unassigned["spend_so_far_usd"] == 0.05
        assert unassigned["verdict"] is None
        break


async def test_forecasts_scoping(tmp_path):
    """The invoice's scoping rule: tenant admins see their own
    tenant's forecast only (forced, even when they ask for another
    tenant's), plain keys get the admin 403, and platform admins may
    filter ``?tenant_id=`` or read the whole platform."""
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "Acme")
        globex = await _create_tenant(env.client, "Globex")
        acme_admin = await create_key(
            env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
        )
        member = await create_key(env.client, "member", tenant_id=acme["id"])
        spiker = await create_key(env.client, "spiker", tenant_id=acme["id"])
        quiet = await create_key(env.client, "quiet", tenant_id=globex["id"])
        now = datetime.now(timezone.utc)
        await _seed(*[
            _log(f"s-{i}", key_id=spiker["id"], key_name="spiker",
                 tenant_id=acme["id"], cost=2.0,
                 created_at=now - timedelta(days=1, minutes=i))
            for i in range(10)
        ])
        await _seed(
            _log("q1", key_id=quiet["id"], key_name="quiet",
                 tenant_id=globex["id"], cost=0.5,
                 created_at=now - timedelta(minutes=10))
        )

        # A plain key: not an admin at all.
        resp = await env.client.get(
            "/admin/forecasts", headers=auth(member["key"])
        )
        assert resp.status_code == 403

        # The tenant admin: their tenant's forecast only — even when
        # asking for globex's.
        resp = await env.client.get(
            "/admin/forecasts",
            params={"tenant_id": globex["id"]},
            headers=auth(acme_admin["key"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert [t["tenant_id"] for t in body["tenants"]] == [acme["id"]]
        assert not [k for k in body["tenants"][0]["keys"] if k["key_name"] == "quiet"]

        # The platform admin: one tenant on request — globex's forecast,
        # and nothing of acme's.
        body = (
            await env.client.get(
                "/admin/forecasts",
                params={"tenant_id": globex["id"]},
                headers=auth(ADMIN_KEY),
            )
        ).json()
        assert [t["tenant_id"] for t in body["tenants"]] == [globex["id"]]
        assert body["totals"]["spend_so_far_usd"] == 0.5

        # ...and both tenants without a filter.
        body = (
            await env.client.get("/admin/forecasts", headers=auth(ADMIN_KEY))
        ).json()
        assert {t["tenant_id"] for t in body["tenants"]} == {acme["id"], globex["id"]}
        break


async def test_forecasts_month_rules(tmp_path):
    """The month parameter is the invoice's own contract: a past
    month is COMPLETE — its actual total served as the projection,
    remaining days 0, trend null — while a future or malformed month
    is the app's 400 envelope naming the param, never a fake
    forecast for a period that has not happened."""
    async for env in _make_env(tmp_path):
        epsilon = await create_key(env.client, "epsilon")
        await _seed(
            _log("past-1", key_id=epsilon["id"], key_name="epsilon",
                 cost=3.0, created_at=_LAST_MONTH_DATE)
        )
        await _seed(
            _log("now-1", key_id=epsilon["id"], key_name="epsilon",
                 cost=1.0, created_at=datetime.now(timezone.utc) - timedelta(minutes=2))
        )

        # A complete month: the projection IS the actual.
        resp = await env.client.get(
            "/admin/forecasts",
            params={"month": _LAST_MONTH},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 200
        past = resp.json()
        assert past["month"] == _LAST_MONTH
        assert past["month_complete"] is True
        assert past["remaining_days"] == 0.0
        assert past["totals"]["spend_so_far_usd"] == 3.0
        assert past["totals"]["run_rate"]["projected_month_usd"] == 3.0
        assert past["totals"]["recent_pace"]["daily_usd"] is None
        assert past["totals"]["trend_ratio"] is None

        # The future: the invoice's 400, naming the param.
        resp = await env.client.get(
            "/admin/forecasts",
            params={"month": _NEXT_MONTH},
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 400
        error = resp.json()["error"]
        assert "future" in error["message"]
        assert error["type"] == "invalid_request_error"

        # Malformed: the same envelope.
        for bad in ("2026-13", "not-a-month"):
            resp = await env.client.get(
                "/admin/forecasts",
                params={"month": bad},
                headers=auth(ADMIN_KEY),
            )
            assert resp.status_code == 400
            assert "month" in resp.json()["error"]["message"]
        break


async def test_forecasts_lens_excludes_shadow_and_trial(tmp_path):
    """The billing lens on both windows: shadow mirrors and trial
    replays project toward nothing — an expensive mirror storm in
    the recent window is comparison traffic, not a spending pace,
    and a replayed yesterday is not yesterday's bill."""
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "Acme")
        alpha = await create_key(env.client, "alpha", tenant_id=acme["id"])
        now = datetime.now(timezone.utc)
        rows = [
            # A modest early-month history + one ordinary recent row.
            *(
                _log(f"b-{i}", key_id=alpha["id"], key_name="alpha",
                     tenant_id=acme["id"], cost=0.01,
                     created_at=now - timedelta(days=10, minutes=i))
                for i in range(14)
            ),
            _log("w-0", key_id=alpha["id"], key_name="alpha",
                 tenant_id=acme["id"], cost=0.01,
                 created_at=now - timedelta(minutes=30)),
        ]
        # An expensive shadow storm + a trial replay in the recent
        # window: ~$599 that must not project anywhere.
        rows += [
            _log(f"sh-{i}", key_id=alpha["id"], key_name="alpha",
                 tenant_id=acme["id"], cost=10.0, shadow=True,
                 created_at=now - timedelta(days=1, minutes=i % 60))
            for i in range(50)
        ]
        rows.append(
            _log("tr-0", key_id=alpha["id"], key_name="alpha",
                 tenant_id=acme["id"], cost=99.0, trial_id="trial-1",
                 created_at=now - timedelta(minutes=5))
        )
        await _seed(*rows)

        body = (
            await env.client.get("/admin/forecasts", headers=auth(ADMIN_KEY))
        ).json()
        (acme_section,) = body["tenants"]
        (alpha_key,) = acme_section["keys"]
        # 14 cents booked, one cent recent — the storm never landed.
        assert alpha_key["spend_so_far_usd"] == 0.15
        assert alpha_key["recent_pace"]["daily_usd"] == _approx(0.01 / 7.0)
        assert alpha_key["run_rate"]["projected_month_usd"] == _approx(
            0.15 / body["elapsed_days"] * _DAYS_IN_MONTH
        )
        assert alpha_key["recent_pace"]["projected_month_usd"] == _approx(
            0.15 + (0.01 / 7.0) * body["remaining_days"]
        )
        break
