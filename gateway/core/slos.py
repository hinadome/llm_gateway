"""Service level objectives (tier 42): named, deliberate promises
about the client experience, measured over the request logs and
verdicted honestly.

The shape mirrors the migration verdicts (``core/migration``): a
QUERY leg (``measure`` — one windowed aggregation per target, the
numbers that answer "what did clients experience?") and a PURE
verdict leg (``evaluate`` — no DB, no clock, no state to drift: the
measured value against the configured bar, with the same honesty
the watch has — thin volume is ``insufficient_evidence`` regardless
of the numbers, and an unmeasurable metric (``ttft_p95_ms`` when
nothing streamed) is a null value with a caveat, never a fabricated
one).

What counts is CLIENT EXPERIENCE, not deployment health: every
non-shadow, non-trial row for the scope — cache replays included (a
cached answer is a fast answer the client really received) and, for
a model-scoped target, unattributed error rows too (the client
experienced the failure even when no dispatch completed to blame a
deployment — the tier-40 attribution story). A deployment-scoped
target measures only the rows attributed to that deployment, the
same filter the post-promotion watch uses.

Evaluation is on demand, at read time (``status`` per target): no
loop, no persistence, no notifications — a tightened bar re-verdicts
on the next read because there is no cached verdict to go stale.
Alert delivery on breach is the next tier; this module is the
measurement and the honest answer, never a pager.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import SLOTargetConfig
from ..models import RequestLog

# Client experience, defined once: the rows a client could feel.
# Mirrors (shadow twins and prompt A/B) are evidence, not traffic;
# trial replays are experiments, not traffic. Cache replays COUNT —
# a cached answer is a real answer the client received (and its row
# is flagged ``cache_hit`` for anyone who wants to split them out).
_CLIENT_ROW = (
    RequestLog.shadow.isnot(True),
    RequestLog.trial_id.is_(None),
)


def _percentile(sorted_values: list[int], pct: float) -> int | None:
    """The same nearest-rank percentile /admin/stats uses, so an SLO
    bar and the stats panel can never disagree about the method."""
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, max(0, round(pct * (len(sorted_values) - 1))))
    return sorted_values[idx]


async def measure(
    session: AsyncSession,
    slo: SLOTargetConfig,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One windowed aggregation for one SLO target: everything the
    verdict (and the operator) needs about what clients experienced.

    Returns ``requests``, ``errors``, ``error_rate`` and the latency
    percentiles (``latency_p50_ms`` / ``latency_p95_ms`` /
    ``latency_p99_ms``) plus ``ttft_p95_ms`` over the rows that
    streamed — all measured in one pass so every metric of a target
    comes from the same window, the same rows, the same truth.
    Unmeasurable legs are ``None`` (nothing streamed, no latencies),
    never fabricated. ``now`` is injectable so tests can pin window
    boundaries without sleeping.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=slo.window_minutes)
    conds = [
        RequestLog.model == slo.model,
        RequestLog.created_at >= since,
        *_CLIENT_ROW,
    ]
    if slo.deployment is not None:
        # Deployment scope: attributed rows only — a request that
        # failed before any dispatch completed has no deployment to
        # match (the tier-40 story), so it cannot inflate or flatter
        # a deployment's SLO either way.
        conds.append(RequestLog.deployment == slo.deployment)
    base = select(RequestLog).where(*conds)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    total = total or 0
    err_count = await session.scalar(
        select(func.count()).where(
            *conds,
            case((RequestLog.status.in_(("error", "blocked")), 1), else_=0) == 1,
        )
    )
    err_count = err_count or 0
    # Latencies (success-relevant on every row; errors carry their
    # own failed-fast ones — an SLO measures the whole experience)
    # and TTFT over the streamed rows, sorted for percentiles.
    latencies = [
        row[0]
        for row in (
            await session.execute(
                select(RequestLog.latency_ms)
                .where(*conds, RequestLog.latency_ms.isnot(None))
                .order_by(RequestLog.latency_ms)
            )
        ).all()
        if row[0] is not None
    ]
    ttfts = [
        row[0]
        for row in (
            await session.execute(
                select(RequestLog.ttft_ms)
                .where(*conds, RequestLog.ttft_ms.isnot(None))
                .order_by(RequestLog.ttft_ms)
            )
        ).all()
        if row[0] is not None
    ]
    return {
        "requests": total,
        "errors": err_count,
        "error_rate": (err_count / total) if total else None,
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "latency_p99_ms": _percentile(latencies, 0.99),
        "ttft_p95_ms": _percentile(ttfts, 0.95),
    }


def evaluate(
    measured: dict[str, Any], slo: SLOTargetConfig
) -> dict[str, Any]:
    """The pure verdict: the measured metric against the configured
    bar, honestly gated. ``met`` (the ceiling holds),
    ``breached`` (it does not), ``insufficient_evidence`` (the window
    carried fewer than ``min_requests`` requests — or nothing
    streamed, for a TTFT target — and a quiet window is not a
    verdict). No DB, no clock: every input is in the arguments, so
    the verdict is re-computable and never drifts from its numbers."""
    requests = measured.get("requests") or 0
    if requests < slo.min_requests:
        return {
            "verdict": "insufficient_evidence",
            "check": {"value": None, "pass": None},
            "reason": (
                f"only {requests} request(s) in the last "
                f"{slo.window_minutes}m — the minimum is "
                f"{slo.min_requests}; a quiet window is not a verdict"
            ),
        }
    value = measured.get(slo.metric)
    if value is None:
        # Unmeasurable, not unmet: a TTFT target on a window where
        # nothing streamed has no number to compare — the honest
        # answer is no answer.
        return {
            "verdict": "insufficient_evidence",
            "check": {"value": None, "pass": None},
            "reason": (
                f"no measurement for {slo.metric} in the window "
                "(nothing streamed?)"
            ),
        }
    passed = value <= slo.target
    return {
        "verdict": "met" if passed else "breached",
        "check": {"value": value, "pass": passed},
        "reason": (
            None
            if passed
            else (
                f"{slo.metric} is {value} against the "
                f"{slo.target} bar"
            )
        ),
    }


async def status(
    session: AsyncSession,
    config: Any,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Every configured target, measured and verdicted: one row per
    SLO with the definition echoed, the window's ``since`` bound, the
    full measurement (so the volume and the other metrics are
    inspectable beside the verdict), the target metric's ``value``,
    and the verdict with its check and human reason. Ordered by name
    — the list is the operator's own config, and a stable order keeps
    dashboards calm across reloads."""
    now = now or datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for slo in sorted(config.slos.targets, key=lambda s: s.name):
        measured = await measure(session, slo, now=now)
        verdict = evaluate(measured, slo)
        rows.append(
            {
                "name": slo.name,
                "model": slo.model,
                "deployment": slo.deployment,
                "metric": slo.metric,
                "target": slo.target,
                "window_minutes": slo.window_minutes,
                "min_requests": slo.min_requests,
                "since": (now - timedelta(minutes=slo.window_minutes)).isoformat(),
                "measured": measured,
                "value": measured.get(slo.metric),
                **verdict,
            }
        )
    return rows
