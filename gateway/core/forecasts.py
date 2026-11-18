"""Spend forecasts (tier 47): the invoice's forward-looking sibling —
what will the month's final bill be, per tenant and per key?

The invoice (tier 45) states what each key and tenant consumed this
month; the anomalies (tier 46) flag what about it is unusual. The
forecast answers the question the finance side asks on the way to
either: *where will this month land?* Two projections, both pure
math over the same client-consumed rows (shadow mirrors and trial
replays excluded, cache hits included — the billing lens, the
tier-45 rules):

* **Run-rate** — straight-line extrapolation: spend so far over
  the elapsed fraction of the month, scaled to the whole month.
  Honest about being an average: a key that stopped spending three
  days ago still projects its old pace.
* **Recent pace** — the correction: the last ``RECENT_WINDOW_DAYS``
  days' daily spend (bounded to the month) carried over the
  remaining days, on top of the spend already booked. A key that
  stopped projects flat; a key that accelerated projects the
  acceleration.

The two lines meet at the **trend ratio** — recent daily pace over
the month-to-date daily average — and at month end they meet each
other: the verdicts (projected vs the monthly budget: over budget /
at risk ≥80% / within) are decided by the **recent-pace projection
when one exists** (the better estimate), the run-rate otherwise,
with ``verdict_based_on`` saying which. Both projections always
ship side by side — a verdict is never a mystery about its inputs.

The honesty rules are the epic's: **a complete month is not
forecast** — a past month serves its actual total as the
projection (``month_complete: true``, remaining days 0, trend
``null``): the invoice IS the final bill, and a projection of a
finished month would be a lie told with extra steps. A future
month is the invoice's 400. **A trend needs at least two days of
month** — less than that (and month one at large) means
``trend_ratio: null``, never a ratio built on hours of evidence;
the run-rate still computes (noisy but labeled: ``elapsed_days``
ships in the envelope so the reader judges). Zero spend projects
zero, honestly. No budget set → projected pct and verdict ``null``,
never a fake zero — and every existing key appears, zero-spend
included, with its budget beside its projection (an unused key
projects $0 of its budget — that is information, not an absence).

The measure/pure split, the epic's shape: ``measure_between`` is
the query leg (one grouped aggregation per window, plain range
predicates — no SQL date functions, so the UTC day arithmetic is
the same on SQLite and PostgreSQL), ``project`` is pure (one
spender's two projections + trend from the two windows' numbers),
``rollup`` is pure (spenders into keys into tenants, the invoice's
zero-usage/deleted-key/unassigned rules), ``forecast`` composes
them. Read at request time — no loop, no state, no config: the
window (``RECENT_WINDOW_DAYS``) is a module constant, the math is
deterministic, and ``parse_month`` is the invoice's own month
contract (same calendar months in UTC, same 400s).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import RequestLog, Tenant, VirtualKey
from .billing import parse_month

# The recent-pace window, in days. A module constant, not config: the
# forecast is deterministic math with no operator judgment to tune —
# a later tier can lift it into config if a real need appears.
RECENT_WINDOW_DAYS = 7.0

# A trend needs at least this much month behind it: recent-pace vs
# average over less than two days is hours of evidence dressed as a
# line. Below it the trend is null — the projections still ship.
_MIN_TREND_DAYS = 2.0


async def measure_between(
    session: AsyncSession,
    since: datetime,
    until: datetime,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """One grouped aggregation over a window's client-consumed rows:
    (tenant, key) -> requests and cost. The billing lens (the
    ``_measure_between`` twins' rule: shadow mirrors and trial
    replays excluded, cache hits included), plain range predicates —
    the day arithmetic lives in Python, so UTC days mean the same
    thing on every backend."""
    conditions = [
        RequestLog.created_at >= since,
        RequestLog.created_at < until,
        RequestLog.shadow.isnot(True),
        RequestLog.trial_id.is_(None),
    ]
    if tenant_id is not None:
        conditions.append(RequestLog.tenant_id == tenant_id)
    rows = (
        await session.execute(
            select(
                RequestLog.tenant_id,
                RequestLog.key_id,
                RequestLog.key_name,
                func.count(),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
            )
            .where(*conditions)
            .group_by(RequestLog.tenant_id, RequestLog.key_id, RequestLog.key_name)
        )
    ).all()
    return [
        {
            "tenant_id": tenant,
            "key_id": key_id,
            "key_name": key_name,
            "requests": requests,
            "cost_usd": round(cost, 6),
        }
        for (tenant, key_id, key_name, requests, cost) in rows
    ]


def project(
    spend_so_far: float,
    requests_so_far: int,
    *,
    elapsed_days: float,
    remaining_days: float,
    days_in_month: float,
    recent_spend: float,
    recent_window_days: float,
    month_complete: bool,
) -> dict[str, Any]:
    """Pure: one spender's forecast block — the two projections, the
    trend where the evidence justifies one, and nothing else. Every
    input ships in the envelope's bounds so the block is auditable
    without calling back.

    The rules: a complete month's projections are its actuals (the
    month is over; the invoice is the final bill); the recent-pace
    window is bounded to the month (a day-2 "last 7 days" is really
    2 days, and divides by 2); the trend needs ``_MIN_TREND_DAYS`` of
    month and a nonzero month-to-date daily average — otherwise
    ``null``, never a ratio on hours of evidence or a fabricated
    infinity against a zero average."""
    if month_complete:
        return {
            "requests": requests_so_far,
            "spend_so_far_usd": round(spend_so_far, 6),
            "run_rate": {
                "daily_usd": round(spend_so_far / days_in_month, 6)
                if days_in_month
                else None,
                "projected_month_usd": round(spend_so_far, 6),
            },
            "recent_pace": {
                "daily_usd": None,
                "projected_month_usd": round(spend_so_far, 6),
            },
            "trend_ratio": None,
        }
    month_daily = (spend_so_far / elapsed_days) if elapsed_days > 0 else 0.0
    # The recent window is bounded by the month start: early in the
    # month "the last 7 days" is however much month has elapsed.
    window_days = min(recent_window_days, elapsed_days)
    recent_daily = (recent_spend / window_days) if window_days > 0 else 0.0
    trend_ratio = None
    if elapsed_days >= _MIN_TREND_DAYS and month_daily > 0:
        trend_ratio = round(recent_daily / month_daily, 2)
    return {
        "requests": requests_so_far,
        "spend_so_far_usd": round(spend_so_far, 6),
        "run_rate": {
            "daily_usd": round(month_daily, 6),
            "projected_month_usd": round(month_daily * days_in_month, 6),
        },
        "recent_pace": {
            "daily_usd": round(recent_daily, 6),
            "projected_month_usd": round(spend_so_far + recent_daily * remaining_days, 6),
        },
        "trend_ratio": trend_ratio,
    }


def _verdict(projection: float, based_on: str, budget: float | None) -> dict[str, Any]:
    """The projected-vs-budget fields: the pct and the over / at_risk
    (≥80%) / within verdict, decided by ``projection``. No budget →
    both ``null`` — an honest absence, never a fake zero."""
    if budget is None:
        return {"projected_pct_of_budget": None, "verdict": None, "verdict_based_on": None}
    pct = round(projection / budget * 100.0, 2)
    verdict = "over_budget" if pct >= 100 else "at_risk" if pct >= 80 else "within_budget"
    return {"projected_pct_of_budget": pct, "verdict": verdict, "verdict_based_on": based_on}


def _effective_projection(block: dict[str, Any]) -> float:
    """The projection the verdicts and the sorting trust: the recent
    pace's when one exists (it corrects for stopped and accelerated
    spenders alike — and on day one it is arithmetically identical
    to the run-rate, since the recent window IS the month so far),
    the run-rate's otherwise. On a complete month both are the
    actual, so either would do."""
    if block["recent_pace"]["daily_usd"] is not None:
        return block["recent_pace"]["projected_month_usd"]
    return block["run_rate"]["projected_month_usd"]


def _key_view(
    key_id: str | None,
    key_name: str | None,
    month_spend: float,
    month_requests: int,
    recent_spend: float,
    *,
    elapsed_days: float,
    remaining_days: float,
    days_in_month: float,
    budget: float | None,
    month_complete: bool,
) -> dict[str, Any]:
    block = project(
        month_spend,
        month_requests,
        elapsed_days=elapsed_days,
        remaining_days=remaining_days,
        days_in_month=days_in_month,
        recent_spend=recent_spend,
        recent_window_days=RECENT_WINDOW_DAYS,
        month_complete=month_complete,
    )
    based_on = "recent_pace" if not month_complete else "run_rate"
    return {
        "key_id": key_id,
        "key_name": key_name,
        "monthly_budget_usd": budget,
        **block,
        **_verdict(_effective_projection(block), based_on, budget),
    }


def rollup(
    month_rows: list[dict[str, Any]],
    recent_rows: list[dict[str, Any]],
    keys_by_id: dict[str, dict[str, Any]],
    tenants_by_id: dict[str, dict[str, Any]],
    *,
    elapsed_days: float,
    remaining_days: float,
    days_in_month: float,
    month_complete: bool,
) -> dict[str, Any]:
    """Pure: the forecast statement. Keys group into tenants by the
    ROW's tenant (a deleted key still projects where it lived), every
    existing key appears (zero-spend included — an unused key
    projects $0 of its budget, which is information), tenants carry
    their pooled budget, platform keys land in ``unassigned``. Every
    level recomputes the math at its own scope (a tenant's projection
    is the tenant's numbers, not the sum of key projections — the
    projections are nonlinear in nothing, but recomputing keeps each
    level honest about its own spend_so_far). Sorting: by the
    effective projection, biggest first — the forecast's big numbers
    are the month-end ones, so they lead."""
    month: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for row in month_rows:
        month[(row["tenant_id"], row["key_id"])] = row
    recent: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for row in recent_rows:
        recent[(row["tenant_id"], row["key_id"])] = row

    def merged(key: tuple[str | None, str | None]) -> tuple[float, int, float, str | None]:
        m = month.get(key)
        r = recent.get(key)
        return (
            m["cost_usd"] if m else 0.0,
            m["requests"] if m else 0,
            r["cost_usd"] if r else 0.0,
            (m or r or {}).get("key_name"),
        )

    # (tenant, key) -> section, every existing key first (the invoice's
    # rule), then keys with traffic but no row (deleted) with no budget.
    sections: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for key_id, meta in keys_by_id.items():
        spend, requests, rec, _ = merged((meta["tenant_id"], key_id))
        sections[(meta["tenant_id"], key_id)] = _key_view(
            key_id,
            meta["name"],
            spend,
            requests,
            rec,
            elapsed_days=elapsed_days,
            remaining_days=remaining_days,
            days_in_month=days_in_month,
            budget=meta["monthly_budget_usd"],
            month_complete=month_complete,
        )
    for key in month:
        if key not in sections:
            spend, requests, rec, name = merged(key)
            sections[key] = _key_view(
                key[1],
                name,
                spend,
                requests,
                rec,
                elapsed_days=elapsed_days,
                remaining_days=remaining_days,
                days_in_month=days_in_month,
                budget=None,
                month_complete=month_complete,
            )

    by_tenant: dict[str | None, list[dict[str, Any]]] = {}
    for (tenant, _), section in sections.items():
        by_tenant.setdefault(tenant, []).append(section)

    def sort_key(section: dict[str, Any]) -> tuple[float, str]:
        # The effective projection leads; names break ties
        # deterministically.
        return (-_effective_projection(section), section.get("key_name") or "")

    # Per-tenant and platform totals come from the raw rows, exact —
    # not re-derived from the key sections' rounded daily rates (the
    # projections are nonlinear nowhere, but rounding drift is still
    # drift, and the totals should reconcile with the invoice).
    month_by_tenant: dict[str | None, dict[str, Any]] = {}
    for row in month_rows:
        entry = month_by_tenant.setdefault(row["tenant_id"], {"requests": 0, "cost_usd": 0.0})
        entry["requests"] += row["requests"]
        entry["cost_usd"] += row["cost_usd"]
    recent_by_tenant: dict[str | None, dict[str, Any]] = {}
    for row in recent_rows:
        entry = recent_by_tenant.setdefault(row["tenant_id"], {"cost_usd": 0.0})
        entry["cost_usd"] += row["cost_usd"]

    tenants: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []
    for tenant, key_sections in by_tenant.items():
        key_sections.sort(key=sort_key)
        spend = month_by_tenant.get(tenant, {}).get("cost_usd", 0.0)
        requests = month_by_tenant.get(tenant, {}).get("requests", 0)
        rec = recent_by_tenant.get(tenant, {}).get("cost_usd", 0.0)
        budget = tenants_by_id.get(tenant, {}).get("monthly_budget_usd")
        block = project(
            spend,
            requests,
            elapsed_days=elapsed_days,
            remaining_days=remaining_days,
            days_in_month=days_in_month,
            recent_spend=rec,
            recent_window_days=RECENT_WINDOW_DAYS,
            month_complete=month_complete,
        )
        based_on = "recent_pace" if not month_complete else "run_rate"
        section = {
            "tenant_id": tenant,
            "tenant_name": tenants_by_id.get(tenant, {}).get("name"),
            "monthly_budget_usd": budget,
            **block,
            **_verdict(_effective_projection(block), based_on, budget),
            "keys": key_sections,
        }
        if tenant is None:
            unassigned.append(section)
        else:
            tenants.append(section)
    tenants.sort(
        key=lambda t: (
            -(_effective_projection(t)),
            t["tenant_id"],
        )
    )

    total_spend = sum(row["cost_usd"] for row in month_rows)
    total_recent = sum(row["cost_usd"] for row in recent_rows)
    total_requests = sum(row["requests"] for row in month_rows)
    totals = project(
        total_spend,
        total_requests,
        elapsed_days=elapsed_days,
        remaining_days=remaining_days,
        days_in_month=days_in_month,
        recent_spend=total_recent,
        recent_window_days=RECENT_WINDOW_DAYS,
        month_complete=month_complete,
    )
    return {"tenants": tenants, "unassigned": unassigned, "totals": totals}


async def forecast(
    session: AsyncSession,
    month: str | None,
    tenant_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The composed surface: the month's bounds and clock context
    beside the forecast statement. ``month`` is the invoice's own
    parameter (``None`` = the current UTC month — of ``now``; malformed
    or future months raise the invoice's 400s). A complete month skips
    the recent-pace query entirely — the pace of a finished month
    projects nothing, and asking for it would imply otherwise."""
    now = now or datetime.now(timezone.utc)
    month, since, until = parse_month(month, now=now)
    month_complete = now >= until
    days_in_month = float((until - since).days)
    # The clock context: how much month has elapsed (a fraction — the
    # run-rate divides by it, so day one at 00:30 is 0.02 days, noisy
    # but honest and labeled), how much remains.
    elapsed_days = min(max((now - since).total_seconds() / 86400.0, 0.0), days_in_month)
    remaining_days = days_in_month - elapsed_days

    month_rows = await measure_between(session, since, until, tenant_id=tenant_id)
    if month_complete:
        recent_rows: list[dict[str, Any]] = []
    else:
        window_since = max(since, now - timedelta(days=RECENT_WINDOW_DAYS))
        recent_rows = await measure_between(
            session, window_since, until, tenant_id=tenant_id
        )

    keys_by_id = {
        row.id: {
            "name": row.name,
            "tenant_id": row.tenant_id,
            "monthly_budget_usd": row.monthly_budget_usd,
        }
        for row in (
            await session.execute(
                select(VirtualKey).where(
                    *([VirtualKey.tenant_id == tenant_id] if tenant_id is not None else [])
                )
            )
        ).scalars()
    }
    tenants_by_id = {
        row.id: {"name": row.name, "monthly_budget_usd": row.monthly_budget_usd}
        for row in (await session.execute(select(Tenant))).scalars()
    }
    return {
        "month": month,
        "period": {"since": since.isoformat(), "until": until.isoformat()},
        "generated_at": now.isoformat(),
        "month_complete": month_complete,
        "days_in_month": days_in_month,
        "elapsed_days": round(elapsed_days, 4),
        "remaining_days": round(remaining_days, 4),
        "recent_window_days": RECENT_WINDOW_DAYS,
        **rollup(
            month_rows,
            recent_rows,
            keys_by_id,
            tenants_by_id,
            elapsed_days=elapsed_days,
            remaining_days=remaining_days,
            days_in_month=days_in_month,
            month_complete=month_complete,
        ),
    }
