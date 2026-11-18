"""Usage invoices (tier 45): the billing epic's first surface — a
period statement of what each key and tenant consumed.

The usage export (``GET /admin/usage/export``) answers "which
requests did this key make?" one row at a time — a chargeback *raw
material*. An invoice answers the question the finance side actually
asks: "what did each tenant consume in October, per key, per
model?" — the same rows rolled up into line items, with the
monthly budget beside the spend so a statement is also a
spend-vs-plan check.

The billing lens is CLIENT-CONSUMED usage: shadow mirrors are
excluded (they never cost the client anything and never reach a
tenant), and so are candidate-trial replays (deliberately re-run
traffic, not the tenant's doing). Cache hits are INCLUDED — a
cached answer is a real served request, billed at the cached rate
the calculator already recorded on the row.

The shape mirrors the other measure/pure splits: ``measure_lines``
is the query leg (one grouped aggregation over the request log for
the period — tenant, key, model, requests, errors, the token
splits, cost), ``rollup`` is the pure leg (line items into keys,
keys into tenants, keys with no traffic included as zero-spend —
an unused key is a finops finding, not an absence — beside the
key's and tenant's ``monthly_budget_usd``). ``parse_month`` pins
the period: calendar months in UTC, an explicit ``YYYY-MM`` or the
current month by default; a future month is a 400, not an empty
statement pretending to exist.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import GatewayError
from ..models import RequestLog, Tenant, VirtualKey

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")

# The CSV column order — a stable contract for downstream consumers.
INVOICE_CSV_COLUMNS = (
    "month",
    "tenant_id",
    "tenant_name",
    "key_id",
    "key_name",
    "model",
    "requests",
    "errors",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "total_tokens",
    "cost_usd",
)


def parse_month(value: str | None) -> tuple[str, datetime, datetime]:
    """Pure: an invoice month to its (month, since, until) bounds.

    ``None`` means the current UTC month. Calendar months in UTC —
    the naive default every billing system starts with; anything
    fancier (day-of-month anchors, timezones) is a later tier, not a
    hidden surprise. A malformed month or one in the future is a 400:
    an invoice for a period that has not happened must not pretend to
    be empty.
    """
    if value is None:
        now = datetime.now(timezone.utc)
        month = f"{now.year:04d}-{now.month:02d}"
    else:
        month = value
        match = _MONTH_RE.match(month)
        if not match:
            raise GatewayError(
                400,
                "Invalid month: expected YYYY-MM.",
                "invalid_request_error",
                param="month",
            )
        year, mon = int(match.group(1)), int(match.group(2))
        if not 1 <= mon <= 12:
            raise GatewayError(
                400,
                "Invalid month: expected YYYY-MM.",
                "invalid_request_error",
                param="month",
            )
        now = datetime.now(timezone.utc)
        if (year, mon) > (now.year, now.month):
            raise GatewayError(
                400,
                "Invalid month: must not be in the future.",
                "invalid_request_error",
                param="month",
            )
    year, mon = int(month[:4]), int(month[5:7])
    since = datetime(year, mon, 1, tzinfo=timezone.utc)
    until = datetime(year + (mon == 12), (mon % 12) + 1, 1, tzinfo=timezone.utc)
    return month, since, until


async def measure_lines(
    session: AsyncSession,
    since: datetime,
    until: datetime,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """One grouped aggregation over the period's client-consumed
    rows: (tenant, key, model) -> requests, errors, the token splits
    and cost. Shadow mirrors and trial replays are excluded — the
    billing lens — while cache hits ride along as ordinary rows (a
    cached answer is a real served request, already billed at the
    cached rate). A tenant scope filters to one tenant's rows."""
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
                RequestLog.model,
                func.count(),
                func.coalesce(func.sum(case((RequestLog.status == "error", 1), else_=0)), 0),
                func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
                func.coalesce(func.sum(RequestLog.completion_tokens), 0),
                func.coalesce(func.sum(RequestLog.cached_tokens), 0),
                func.coalesce(func.sum(RequestLog.total_tokens), 0),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
            )
            .where(*conditions)
            .group_by(
                RequestLog.tenant_id,
                RequestLog.key_id,
                RequestLog.key_name,
                RequestLog.model,
            )
        )
    ).all()
    return [
        {
            "tenant_id": tenant,
            "key_id": key_id,
            "key_name": key_name,
            "model": model,
            "requests": requests,
            "errors": errors,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cached_tokens": cached,
            "total_tokens": total,
            "cost_usd": round(cost, 6),
        }
        for (
            tenant,
            key_id,
            key_name,
            model,
            requests,
            errors,
            prompt,
            completion,
            cached,
            total,
            cost,
        ) in rows
    ]


def _key_view(
    key_id: str | None,
    key_name: str | None,
    lines: list[dict[str, Any]],
    budget: float | None,
) -> dict[str, Any]:
    """One key's section of the statement: its line items sorted by
    cost (biggest spenders first — the export's grouping order), the
    key's totals, and the monthly budget beside the spend."""
    line_items = sorted(lines, key=lambda l: (-l["cost_usd"], l["model"]))
    totals = {
        "requests": sum(l["requests"] for l in lines),
        "errors": sum(l["errors"] for l in lines),
        "prompt_tokens": sum(l["prompt_tokens"] for l in lines),
        "completion_tokens": sum(l["completion_tokens"] for l in lines),
        "cached_tokens": sum(l["cached_tokens"] for l in lines),
        "total_tokens": sum(l["total_tokens"] for l in lines),
        "cost_usd": round(sum(l["cost_usd"] for l in lines), 6),
    }
    return {
        "key_id": key_id,
        "key_name": key_name,
        "monthly_budget_usd": budget,
        "budget_pct": (
            round(totals["cost_usd"] / budget * 100.0, 2)
            if budget
            else None
        ),
        "line_items": line_items,
        **totals,
    }


def rollup(
    lines: list[dict[str, Any]],
    keys_by_id: dict[str, dict[str, Any]],
    tenants_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Pure: line items into a statement. Keys group into tenants by
    the ROW's tenant (a deleted key still bills where it lived), each
    key section carries its ``monthly_budget_usd`` and spend-vs-budget
    pct, tenants carry their pooled monthly budget the same way. Keys
    with no traffic in the period still appear — zero-spend sections
    with no line items, because an unused key is a finops finding,
    not an absence. Tenants sort by spend (desc), keys within a
    tenant and models within a key the same way: the big numbers are
    the top of the statement."""
    # (tenant_id, key_id) -> that key's line items, from the log rows.
    grouped: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
    for line in lines:
        grouped.setdefault((line["tenant_id"], line["key_id"]), []).append(line)

    key_sections: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    # Every existing key appears, traffic or not.
    for key_id, meta in keys_by_id.items():
        key_sections[(meta["tenant_id"], key_id)] = _key_view(
            key_id, meta["name"], grouped.get((meta["tenant_id"], key_id), []),
            meta["monthly_budget_usd"],
        )
    # Keys with traffic but no key row (deleted): the log's names, no budget.
    for (tenant, key_id), group in grouped.items():
        if (tenant, key_id) not in key_sections:
            key_sections[(tenant, key_id)] = _key_view(
                key_id, group[0]["key_name"], group, None
            )

    tenants: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []
    by_tenant: dict[str | None, list[dict[str, Any]]] = {}
    for (tenant, _), section in key_sections.items():
        by_tenant.setdefault(tenant, []).append(section)

    for tenant, sections in by_tenant.items():
        sections.sort(key=lambda s: (-s["cost_usd"], s["key_name"] or ""))
        tenant_cost = round(sum(s["cost_usd"] for s in sections), 6)
        if tenant is None:
            unassigned = sections
            continue
        meta = tenants_by_id.get(tenant, {})
        budget = meta.get("monthly_budget_usd")
        tenants.append(
            {
                "tenant_id": tenant,
                "tenant_name": meta.get("name"),
                "monthly_budget_usd": budget,
                "budget_pct": (
                    round(tenant_cost / budget * 100.0, 2) if budget else None
                ),
                "requests": sum(s["requests"] for s in sections),
                "total_tokens": sum(s["total_tokens"] for s in sections),
                "cost_usd": tenant_cost,
                "keys": sections,
            }
        )
    tenants.sort(key=lambda t: (-t["cost_usd"], t["tenant_id"]))

    all_sections = [s for sections in by_tenant.values() for s in sections]
    return {
        "tenants": tenants,
        "unassigned": unassigned,
        "totals": {
            "requests": sum(s["requests"] for s in all_sections),
            "errors": sum(s["errors"] for s in all_sections),
            "prompt_tokens": sum(s["prompt_tokens"] for s in all_sections),
            "completion_tokens": sum(
                s["completion_tokens"] for s in all_sections
            ),
            "cached_tokens": sum(s["cached_tokens"] for s in all_sections),
            "total_tokens": sum(s["total_tokens"] for s in all_sections),
            "cost_usd": round(sum(s["cost_usd"] for s in all_sections), 6),
        },
    }


async def statement(
    session: AsyncSession,
    month: str | None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """One invoice: the month's bounds, the rolled-up statement and
    the period metadata. ``tenant_id`` scopes both the measurement
    and the key/tenant metadata (a tenant admin's statement shows
    only their keys)."""
    month, since, until = parse_month(month)
    lines = await measure_lines(session, since, until, tenant_id=tenant_id)
    keys_by_id = {
        row.id: {
            "name": row.name,
            "tenant_id": row.tenant_id,
            "monthly_budget_usd": row.monthly_budget_usd,
        }
        for row in (
            await session.execute(
                select(VirtualKey).where(
                    *(
                        [VirtualKey.tenant_id == tenant_id]
                        if tenant_id is not None
                        else []
                    )
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
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **rollup(lines, keys_by_id, tenants_by_id),
    }
