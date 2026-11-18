"""Cost anomaly flags (tier 46): unusual money and traffic patterns,
surfaced as flags beside the invoice — never pager alerts.

The invoice (tier 45) says what each key and tenant consumed this
month; this module answers the follow-up a finops person asks while
reading it: *is any of this unusual?* The scope is deliberately the
money lens — the error-rate story is the SLO epic's job (tiers
42–44), not this one. Four shapes, each a billing surprise in its
own way:

* **cost_spike** — a key spending several times its trailing daily
  rate (the runaway-agent shape, the mispriced-model shape, the
  "someone pointed a loop at us" shape).
* **traffic_spike** — the request-rate version (volume before it
  becomes money).
* **traffic_stop** — a key with real baseline traffic that went
  silent: the integration-broke / churned-overnight signal, and the
  one a spend dashboard structurally cannot show (nothing is a
  great price).
* **first_seen_model** — a model with zero baseline traffic that
  appeared in the window: something new is on the bill, and new
  line items are how invoices surprise people.

The honesty rule is the SLO tiers' rule: **a thin baseline is not a
verdict.** A spike needs ``min_baseline_requests`` baseline rows
before any ratio counts — 5× on four requests is a anecdote, not an
anomaly. A key with no baseline history (brand-new) cannot spike
(honestly: nothing to compare); it will simply appear on the invoice
with its spend. And a baseline that cost nothing ($0.00 — free
models, all-cached replays) with window spend is a spike the ratio
cannot express: the flag fires with ``ratio: null`` and the message
says the baseline spend was zero — never a fabricated infinity.

The same measure/pure split as billing.py: ``measure_windows`` is
the query leg (two grouped aggregations over the client-consumed
lens — shadow mirrors and trial replays excluded, cache hits
included), ``detect_flags`` is pure (rows in, flags out — every
number in the payload, so a flag is auditable without calling back),
and ``anomalies`` composes them. Flags are informational, read at
request time, no loop, no state, no delivery — the operator reads
them beside the invoice and decides.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import FinopsConfig
from ..models import RequestLog


async def _measure_between(
    session: AsyncSession,
    since: datetime,
    until: datetime,
    tenant_id: str | None,
) -> list[dict[str, Any]]:
    """One grouped aggregation over a period's client-consumed rows —
    the billing lens: (tenant, key, model) -> requests and cost.
    Shadow mirrors and trial replays are excluded; cache hits ride
    along as ordinary rows (the tier-45 rules, one window wide)."""
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
            "cost_usd": round(cost, 6),
        }
        for (tenant, key_id, key_name, model, requests, cost) in rows
    ]


async def measure_windows(
    session: AsyncSession,
    config: FinopsConfig,
    tenant_id: str | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], datetime, datetime, datetime]:
    """The two windows detection compares: the recent window (the last
    ``window_hours``) and the baseline (the ``baseline_days`` ending
    where the window starts). Returns (window_rows, baseline_rows,
    window_since, baseline_since, until) — the bounds ship with the
    flags so every number is auditable against its period."""
    now = now or datetime.now(timezone.utc)
    window_days = config.window_hours / 24.0
    window_since = now - timedelta(days=window_days)
    baseline_since = window_since - timedelta(days=config.baseline_days)
    window_rows = await _measure_between(
        session, window_since, now, tenant_id
    )
    baseline_rows = await _measure_between(
        session, baseline_since, window_since, tenant_id
    )
    return window_rows, baseline_rows, window_since, baseline_since, now


def _by_key(rows: list[dict[str, Any]]) -> dict[tuple[Any, Any], dict[str, Any]]:
    """Rows (per key × model) collapsed to per-key: (tenant, key) ->
    summed requests and cost, plus the display name."""
    keys: dict[tuple[Any, Any], dict[str, Any]] = {}
    for row in rows:
        entry = keys.setdefault(
            (row["tenant_id"], row["key_id"]),
            {
                "tenant_id": row["tenant_id"],
                "key_id": row["key_id"],
                "key_name": row["key_name"],
                "requests": 0,
                "cost_usd": 0.0,
            },
        )
        entry["requests"] += row["requests"]
        entry["cost_usd"] += row["cost_usd"]
    return keys


def _by_model(rows: list[dict[str, Any]]) -> dict[str | None, dict[str, Any]]:
    """Rows collapsed to per-model: model -> summed requests and cost."""
    models: dict[str | None, dict[str, Any]] = {}
    for row in rows:
        entry = models.setdefault(
            row["model"], {"requests": 0, "cost_usd": 0.0}
        )
        entry["requests"] += row["requests"]
        entry["cost_usd"] += row["cost_usd"]
    return models


def _usd(value: float) -> str:
    return f"${value:.2f}"


def detect_flags(
    window_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    config: FinopsConfig,
) -> list[dict[str, Any]]:
    """Pure: the flags the two windows justify, every one carrying its
    own numbers — window vs baseline requests and cost, the ratio
    where one exists, and a human message. Sorted deterministically:
    cost spikes by ratio (biggest first), then traffic spikes, then
    silences by lost volume, then new models by name. A thin
    baseline (< ``min_baseline_requests``) is not a verdict: no spike,
    no silence, and a brand-new key cannot spike at all."""
    window_days = config.window_hours / 24.0
    spikes: list[dict[str, Any]] = []
    traffic: list[dict[str, Any]] = []
    stops: list[dict[str, Any]] = []

    window_keys = _by_key(window_rows)
    baseline_keys = _by_key(baseline_rows)

    for (tenant, key_id), win in window_keys.items():
        base = baseline_keys.get((tenant, key_id))
        if base is None or base["requests"] < config.min_baseline_requests:
            # No history (or not enough) to compare against: an
            # honest absence, never a scary ratio on thin evidence.
            continue
        # Cost rate: the window extrapolated to a day over the
        # baseline's daily average. A zero-cost baseline with window
        # spend is a spike the ratio cannot express — fired with
        # ratio None, the message saying so (never an invented ∞).
        baseline_daily_cost = base["cost_usd"] / config.baseline_days
        window_daily_cost = win["cost_usd"] / window_days
        cost_ratio = (
            window_daily_cost / baseline_daily_cost
            if baseline_daily_cost > 0
            else None
        )
        if win["cost_usd"] > 0 and (
            cost_ratio is None or cost_ratio >= config.spike_ratio
        ):
            spikes.append(
                {
                    "kind": "cost_spike",
                    "tenant_id": tenant,
                    "key_id": key_id,
                    "key_name": win["key_name"],
                    "window": {"requests": win["requests"], "cost_usd": round(win["cost_usd"], 6)},
                    "baseline": {"requests": base["requests"], "cost_usd": round(base["cost_usd"], 6)},
                    "ratio": (
                        round(cost_ratio, 2) if cost_ratio is not None else None
                    ),
                    "message": (
                        f"key '{win['key_name']}' is spending "
                        + (
                            f"{cost_ratio:.1f}x its baseline rate "
                            f"({_usd(window_daily_cost)}/day vs {_usd(baseline_daily_cost)}/day)"
                            if cost_ratio is not None
                            else "against a baseline that cost nothing"
                        )
                    ),
                }
            )
        traffic_ratio = (
            win["requests"] / window_days
        ) / (base["requests"] / config.baseline_days)
        if traffic_ratio >= config.spike_ratio:
            traffic.append(
                {
                    "kind": "traffic_spike",
                    "tenant_id": tenant,
                    "key_id": key_id,
                    "key_name": win["key_name"],
                    "window": {"requests": win["requests"], "cost_usd": round(win["cost_usd"], 6)},
                    "baseline": {"requests": base["requests"], "cost_usd": round(base["cost_usd"], 6)},
                    "ratio": round(traffic_ratio, 2),
                    "message": (
                        f"key '{win['key_name']}' is sending {traffic_ratio:.1f}x "
                        f"its baseline request rate "
                        f"({win['requests'] / window_days:.0f}/day vs "
                        f"{base['requests'] / config.baseline_days:.0f}/day)"
                    ),
                }
            )

    # Silences: keys with real baseline traffic and none in the
    # window — the integration-broke / churned-overnight signal.
    for (tenant, key_id), base in baseline_keys.items():
        if base["requests"] < config.min_baseline_requests:
            continue
        if (tenant, key_id) in window_keys:
            continue
        stops.append(
            {
                "kind": "traffic_stop",
                "tenant_id": tenant,
                "key_id": key_id,
                "key_name": base["key_name"],
                "window": {"requests": 0, "cost_usd": 0.0},
                "baseline": {"requests": base["requests"], "cost_usd": round(base["cost_usd"], 6)},
                "ratio": None,
                "message": (
                    f"key '{base['key_name']}' went silent: "
                    f"{base['requests'] / config.baseline_days:.0f} requests/day in "
                    f"the baseline, none in the last {config.window_hours:.0f}h"
                ),
            }
        )

    # New models: zero baseline traffic, appeared in the window —
    # something new is on the bill. Informational: no volume gate,
    # the numbers ride along so the reader can judge.
    baseline_models = _by_model(baseline_rows)
    new_models = []
    for model, win in _by_model(window_rows).items():
        if model in baseline_models:
            continue
        new_models.append(
            {
                "kind": "first_seen_model",
                "model": model,
                "window": {"requests": win["requests"], "cost_usd": round(win["cost_usd"], 6)},
                "baseline": {"requests": 0, "cost_usd": 0.0},
                "ratio": None,
                "message": (
                    f"model '{model}' appears for the first time "
                    f"(no baseline traffic): {win['requests']} requests, "
                    f"{_usd(win['cost_usd'])} in the last {config.window_hours:.0f}h"
                ),
            }
        )

    spikes.sort(key=lambda f: (-(f["ratio"] if f["ratio"] is not None else float("inf")), f["key_name"] or ""))
    traffic.sort(key=lambda f: (-f["ratio"], f["key_name"] or ""))
    stops.sort(key=lambda f: (-f["baseline"]["requests"], f["key_name"] or ""))
    new_models.sort(key=lambda f: f["model"] or "")
    return [*spikes, *traffic, *stops, *new_models]


async def anomalies(
    session: AsyncSession,
    config: FinopsConfig,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """The composed surface: the two windows' bounds beside the flags
    they justify. ``enabled: false`` skips detection entirely — an
    honest empty list, while the invoice keeps serving."""
    window_rows, baseline_rows, window_since, baseline_since, now = (
        await measure_windows(session, config, tenant_id)
    )
    flags = detect_flags(window_rows, baseline_rows, config) if config.enabled else []
    return {
        "window": {
            "since": window_since.isoformat(),
            "hours": config.window_hours,
        },
        "baseline": {
            "since": baseline_since.isoformat(),
            "days": config.baseline_days,
        },
        "generated_at": now.isoformat(),
        "flags": flags,
    }
