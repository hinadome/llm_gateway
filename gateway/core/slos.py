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

Tier 43 adds the ERROR-BUDGET lens, same query+pure split: every
metric restated as a bad-event fraction against its allowance
(``measure_bad`` — failed+blocked rows for ``error_rate``, where the
bar IS the allowance; rows over the bar for the latency/TTFT
percentiles, where a pXX bar implies ``1 - p`` of requests may
exceed it), burn rate = observed / allowed (``budget_state`` —
on_track / at_risk / exhausted / insufficient_evidence), and the
multiwindow pace verdicts (``burn_rule_verdict`` — the SRE
recipe: a long window establishes the pace, a short one confirms
it is still burning, so a burst that already ended never fires).
A zero-tolerance bar (allowance 0) has no rate to compute: any
bad event exhausts the budget, and burn stays None rather than
inventing an infinity. Windows carry their own sufficiency gates,
scaled from the target's ``min_requests`` by the window's share of
the target's own window, floored at 1.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import SLOBurnRule, SLOTargetConfig
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


# -- error budgets & burn rate (tier 43) --------------------------------------
# The same promise, restated as a budget: how many bad events does the
# bar allow, how many happened, and how FAST is the allowance being
# spent. A percentile bar and a bad-event allowance are two lenses on
# one promise — "p95 under 800ms" means at most ~5% of requests may
# exceed 800ms — so the budget ships BESIDE the tier-42 verdict, never
# replacing it.

# The allowance implied by each percentile metric: the fraction of
# requests the bar permits to be "bad" (over the bar). error_rate's
# allowance is the bar itself (handled separately — not in this table).
_METRIC_BAD_ALLOWED = {
    "latency_p50_ms": 0.50,
    "latency_p95_ms": 0.05,
    "latency_p99_ms": 0.01,
    "ttft_p95_ms": 0.05,
}


def allowed_bad_fraction(slo: SLOTargetConfig) -> float:
    """The bad-event allowance the bar implies: ``error_rate`` 0.01
    allows 1% failed+blocked rows; ``latency_p95_ms`` 800 allows 5% of
    requests over 800ms (the nearest-rank p95 and the 5% tail are the
    same promise seen from two sides)."""
    if slo.metric == "error_rate":
        return slo.target
    return _METRIC_BAD_ALLOWED[slo.metric]


async def measure_bad(
    session: AsyncSession,
    slo: SLOTargetConfig,
    window_minutes: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One window's bad-event accounting for one target: the
    denominator (the rows the metric can measure — every row for
    ``error_rate``, latency-bearing rows for the latency percentiles,
    streamed rows for TTFT) and the bad count (failed+blocked for
    ``error_rate``; rows OVER the bar for the percentile metrics —
    the same rows their percentile was computed over, so the budget
    and the verdict can never disagree about what counts).``now``
    is injectable so tests can pin window boundaries."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=window_minutes)
    conds = [
        RequestLog.model == slo.model,
        RequestLog.created_at >= since,
        *_CLIENT_ROW,
    ]
    if slo.deployment is not None:
        conds.append(RequestLog.deployment == slo.deployment)
    if slo.metric == "error_rate":
        denominator: tuple[Any, ...] = ()
        bad_extra: tuple[Any, ...] = (RequestLog.status.in_(("error", "blocked")),)
    elif slo.metric == "ttft_p95_ms":
        denominator = (RequestLog.ttft_ms.isnot(None),)
        # NULL > bar is NULL in SQL, so the bad count needs no extra
        # non-null guard of its own.
        bad_extra = (RequestLog.ttft_ms > slo.target,)
    else:  # the latency percentiles
        denominator = (RequestLog.latency_ms.isnot(None),)
        bad_extra = (RequestLog.latency_ms > slo.target,)
    measured = (
        await session.scalar(
            select(func.count()).where(*conds, *denominator)
        )
        or 0
    )
    bad = (
        await session.scalar(
            select(func.count()).where(*conds, *bad_extra)
        )
        or 0
    )
    return {
        "minutes": window_minutes,
        "measured": measured,
        "bad": bad,
        "bad_fraction": (bad / measured) if measured else None,
    }


def _window_gate(slo: SLOTargetConfig, window_minutes: int) -> int:
    """The sufficiency floor for one measurement window: the target's
    ``min_requests`` scaled by the window's share of the target's own
    window (20-of-an-hour is 20 in the hour, 2 in five minutes, 480
    in a day), floored at 1 — a window that carried nothing measures
    nothing, regardless of the pace arithmetic."""
    return max(1, round(slo.min_requests * window_minutes / max(slo.window_minutes, 1)))


def _window_view(window: dict[str, Any], slo: SLOTargetConfig, gate: int) -> dict[str, Any]:
    """One measured window, verdict-ready: the burn rate (observed
    fraction over the allowance) and its sufficiency against the
    scaled gate. Zero-tolerance bars have no rate to compute."""
    allowed = allowed_bad_fraction(slo)
    fraction = window.get("bad_fraction")
    burn = (fraction / allowed) if (fraction is not None and allowed > 0) else None
    measured = window.get("measured") or 0
    return {
        "minutes": window["minutes"],
        "measured": measured,
        "bad": window.get("bad") or 0,
        "bad_fraction": fraction,
        "burn_rate": burn,
        "gate": gate,
        "sufficient": measured >= gate,
    }


def budget_state(
    window: dict[str, Any], slo: SLOTargetConfig, any_burning: bool
) -> dict[str, Any]:
    """The pure budget verdict over the target's OWN window: burn rate
    (observed bad fraction over the allowance), remaining fraction
    (1 - burn, negative when the window overspent), and the state —
    ``on_track`` (burn <= 1), ``at_risk`` (within allowance overall
    but a burn rule is firing — fine on average, burning fast NOW),
    ``exhausted`` (the window overspent its allowance), or
    ``insufficient_evidence`` (fewer measurable rows than
    ``min_requests`` — the same honesty the bar verdict has). A
    zero-tolerance bar (allowance 0) has no rate: any bad event
    exhausts it, and burn/remaining stay None rather than inventing
    an infinity."""
    allowed = allowed_bad_fraction(slo)
    measured = window.get("measured") or 0
    bad = window.get("bad") or 0
    fraction = window.get("bad_fraction")
    burn = (fraction / allowed) if (fraction is not None and allowed > 0) else None
    remaining = (1 - burn) if burn is not None else None
    out: dict[str, Any] = {
        "allowed_fraction": allowed,
        "window_minutes": slo.window_minutes,
        "measured": measured,
        "bad": bad,
        "bad_fraction": fraction,
        "burn_rate": burn,
        "remaining_fraction": remaining,
        "remaining_pct": (remaining * 100) if remaining is not None else None,
    }
    if measured < slo.min_requests:
        out.update(
            state="insufficient_evidence",
            reason=(
                f"only {measured} measurable request(s) in the last "
                f"{slo.window_minutes}m — the minimum is {slo.min_requests}; "
                "a quiet window has no budget to read"
            ),
        )
    elif allowed <= 0:
        if bad > 0:
            out.update(
                state="exhausted",
                reason=(
                    f"zero-tolerance budget: {bad} bad event(s) against a "
                    "0 allowance"
                ),
            )
        else:
            out.update(
                state="on_track",
                reason="zero-tolerance budget intact: no bad events",
            )
    elif burn is None:
        out.update(
            state="insufficient_evidence",
            reason="no measurable requests in the window",
        )
    elif burn > 1:
        out.update(
            state="exhausted",
            reason=f"burn rate {burn:.1f}x — the window overspent its allowance",
        )
    elif any_burning:
        out.update(
            state="at_risk",
            reason=(
                f"burn rate {burn:.1f}x is within allowance, but a burn "
                "rule is firing — spending fast right now"
            ),
        )
    else:
        out.update(state="on_track", reason=None)
    return out


def burn_rule_verdict(
    long_window: dict[str, Any],
    short_window: dict[str, Any],
    rule: SLOBurnRule,
    slo: SLOTargetConfig,
) -> dict[str, Any]:
    """The pure multiwindow pace verdict for one burn rule: the SRE
    anti-flap AND — the long window establishes the pace is real
    (burn over the rule's rate), the short window confirms it is
    STILL burning now. Both windows must clear their own scaled
    sufficiency gates, or the verdict is honestly insufficient. A
    zero-tolerance bar has no rate to compare (allowance 0), so its
    rules are insufficient with the reason saying so — never a
    fabricated infinity."""
    long_view = _window_view(long_window, slo, _window_gate(slo, rule.long_minutes))
    short_view = _window_view(short_window, slo, _window_gate(slo, rule.short_minutes))
    out: dict[str, Any] = {
        "name": rule.name,
        "rate": rule.rate,
        "long": long_view,
        "short": short_view,
    }
    if allowed_bad_fraction(slo) <= 0:
        out.update(
            verdict="insufficient_evidence",
            reason="zero-tolerance SLO — burn rate is undefined (allowance 0)",
        )
        return out
    if not long_view["sufficient"]:
        out.update(
            verdict="insufficient_evidence",
            reason=(
                f"only {long_view['measured']} request(s) in the "
                f"{rule.long_minutes}m pace window — the scaled gate is "
                f"{long_view['gate']}"
            ),
        )
    elif not short_view["sufficient"]:
        out.update(
            verdict="insufficient_evidence",
            reason=(
                f"only {short_view['measured']} request(s) in the "
                f"{rule.short_minutes}m confirmation window — the scaled "
                f"gate is {short_view['gate']}"
            ),
        )
    elif (
        long_view["burn_rate"] is not None
        and short_view["burn_rate"] is not None
        and long_view["burn_rate"] >= rule.rate
        and short_view["burn_rate"] >= rule.rate
    ):
        out.update(
            verdict="burning",
            reason=(
                f"burning {long_view['burn_rate']:.1f}x over "
                f"{rule.long_minutes}m, confirmed {short_view['burn_rate']:.1f}x "
                f"over the last {rule.short_minutes}m (bar {rule.rate}x)"
            ),
        )
    else:
        # Steady includes "the pace window is over the bar but the
        # confirmation window is not" — the burst already ended, and
        # the two-window AND refuses to page on it.
        out.update(verdict="steady", reason=None)
    return out


async def status(
    session: AsyncSession,
    config: Any,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Every configured target, measured and verdicted: one row per
    SLO with the definition echoed, the window's ``since`` bound, the
    full measurement (so the volume and the other metrics are
    inspectable beside the verdict), the target metric's ``value``,
    and the verdict with its check and human reason — plus, tier 43,
    the ``budget`` (bad-event allowance, bad count, burn rate,
    remaining, state) and the ``burn`` rules (each rule's long/short
    window numbers and burning/steady/insufficient verdict). Ordered
    by name — the list is the operator's own config, and a stable
    order keeps dashboards calm across reloads."""
    now = now or datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for slo in sorted(config.slos.targets, key=lambda s: s.name):
        measured = await measure(session, slo, now=now)
        verdict = evaluate(measured, slo)
        # Tier 43: the error-budget lens over the same rows — the
        # target's own window for the budget, each burn rule's
        # long/short windows for the pace verdicts. The rules verdict
        # first so at_risk can see them (fine on average, burning
        # fast now).
        burn_rules: list[dict[str, Any]] = []
        for rule in config.slos.burn:
            long_window = await measure_bad(session, slo, rule.long_minutes, now=now)
            short_window = await measure_bad(session, slo, rule.short_minutes, now=now)
            burn_rules.append(burn_rule_verdict(long_window, short_window, rule, slo))
        any_burning = any(r["verdict"] == "burning" for r in burn_rules)
        budget = budget_state(
            await measure_bad(session, slo, slo.window_minutes, now=now),
            slo,
            any_burning,
        )
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
                "budget": budget,
                "burn": burn_rules,
            }
        )
    return rows
