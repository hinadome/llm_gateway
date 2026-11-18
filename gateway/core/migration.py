"""Migration verdicts (tier 37): the go/no-go recommendation engine
over the migration evidence.

Tiers 13/35/36 built the evidence: shadow mirroring scores live traffic
raw-vs-raw, shadow diffing aggregates it per (model, candidate), and
candidate trials generate it on demand for any deployment. What was
still missing is the *call*: an operator staring at "match rate 96%,
mean similarity 0.91, 2.4% shadow errors, +18% latency" had to judge
by feel whether the candidate is promotable. This module turns the
evidence into a recommendation — auditable, never authoritative:

* ``evaluate`` is a PURE function over one evidence group (the
  ``shadow_diff.aggregate`` group shape — and the same shape
  ``trials.run_summaries`` produces, so mirror evidence and trial
  evidence verdict identically through one code path). No DB, no
  clock: every response can echo exactly what it measured and the
  thresholds it applied (``core/migration`` owns no state to drift).
* The verdict is a RECOMMENDATION, not a gate. ``go`` — the evidence
  clears every bar. ``hold`` — answers acceptable, soft concern
  (marginal similarity, a latency regression) needs a business call.
  ``no_go`` — the evidence says not ready: answers diverge, or the
  candidate *fails the traffic it is given* (the loudest finding —
  shadow errors are measurable on every pair, comparability is not).
  ``insufficient_evidence`` — too thin to judge; collect more traffic
  or run a candidate trial.
* Honesty mirrors the evidence layer: a missing measurement is never
  faked. Unmeasurable checks report null values and are skipped, not
  passed; a verdict on thin evidence is pinned to
  ``insufficient_evidence`` even when the observable numbers look
  fine — with one deliberate exception: a shadow-error rate measured
  on a full volume of pairs and failing its bar is a ``no_go`` even
  when nothing was comparable, because a candidate that fails the
  traffic it is given has already answered the question; and the raw
  numbers always ship beside the verdict (``checks``), so the
  recommendation can be audited, re-bucketed or second-guessed —
  never taken on faith.
* Tier 40 adds the POST-promotion leg: once a candidate is promoted
  the mirrors stop, so the pair-shaped evidence above ends exactly
  when watching matters. ``evaluate_serving`` verdicts what the logs
  still say — the attributed serving rows the deployment carried
  (mirrors, trial replays and cache replays excluded; a row lands on
  a deployment only when a dispatch completed on it, so a candidate
  failing at dispatch shows up as traffic that ran away, not as
  attributed errors) — against its priority-tier peers over the same
  window: its own error rate (against ``max_shadow_error_rate`` — the
  same SLO, measured on serving traffic) and its mean latency as a
  percent of the peers' mean (``max_latency_delta_pct``). The same
  vocabulary, the same thresholds, the same honesty — a
  recommendation to keep serving or roll back, never an automatic
  one.

Thresholds live in the ``migration:`` config section, read live
(hot-reloadable — tighten a bar and the next read re-verdicts).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import MigrationConfig
from ..models import RequestLog

# The recommendation vocabulary (this module's only exported state).
RECOMMENDATION_GO = "go"
RECOMMENDATION_HOLD = "hold"
RECOMMENDATION_NO_GO = "no_go"
RECOMMENDATION_INSUFFICIENT = "insufficient_evidence"
RECOMMENDATIONS = (
    RECOMMENDATION_GO,
    RECOMMENDATION_HOLD,
    RECOMMENDATION_NO_GO,
    RECOMMENDATION_INSUFFICIENT,
)

# Hold loses to no_go when both apply — the worst honest call wins.
_SEVERITY = {
    RECOMMENDATION_GO: 0,
    RECOMMENDATION_HOLD: 1,
    RECOMMENDATION_NO_GO: 2,
}


def evaluate(evidence: dict[str, Any], thresholds: MigrationConfig) -> dict[str, Any]:
    """Verdict one evidence group against the thresholds.

    ``evidence`` is the group shape both surfaces produce — the
    ``shadow_diff.aggregate`` row for a (model, deployment) mirror
    group, and the ``trials.run_summaries`` entry for a trial run:
    pairs / exact / near / divergent / shadow_error / not_comparable /
    comparable / match_rate / similarity_mean / shadow_latency_ms /
    primary_latency_ms (plus whatever else the surface carries; only
    these are read).

    Returns ``{"verdict", "checks", "reasons"}``: ``checks`` is the
    per-threshold measurement (value + threshold + pass, null when
    unmeasurable) so the call is auditable; ``reasons`` carries one
    human string per FAILED check (empty for a clean go — the checks
    speak), plus the thin-evidence note on an insufficient verdict.

    The verdict flow, in order: ``pairs`` below ``min_pairs`` —
    insufficient (nothing was measured on enough volume, errors
    included); a failed shadow-error rate on a full volume of pairs —
    no_go (the loudest finding is never downgraded by a thin
    comparable leg); ``comparable`` below ``min_pairs`` — insufficient
    (answer equivalence can't be judged on a handful); then the worst
    honest call over the measured checks (any no_go outranks any
    hold).
    """
    pairs = int(evidence.get("pairs") or 0)
    shadow_errors = int(evidence.get("shadow_error") or 0)
    comparable = int(evidence.get("comparable") or 0)
    match_rate = evidence.get("match_rate")
    similarity_mean = evidence.get("similarity_mean")
    shadow_latency = evidence.get("shadow_latency_ms")
    primary_latency = evidence.get("primary_latency_ms")

    checks: list[dict[str, Any]] = []
    reasons: list[str] = []

    def check(
        name: str, value: Any, threshold: str, passed: bool | None
    ) -> bool | None:
        checks.append(
            {"name": name, "value": value, "threshold": threshold, "pass": passed}
        )
        return passed

    # -- no_go tier: correctness of the candidate's answers/uptime ----
    error_rate = (shadow_errors / pairs) if pairs else None
    check(
        "shadow_error_rate",
        round(error_rate, 4) if error_rate is not None else None,
        f"<= {thresholds.max_shadow_error_rate}",
        (
            error_rate <= thresholds.max_shadow_error_rate
            if error_rate is not None
            else None
        ),
    )
    if error_rate is not None and error_rate > thresholds.max_shadow_error_rate:
        reasons.append(
            f"shadow_error rate {error_rate:.1%} exceeds "
            f"max_shadow_error_rate {thresholds.max_shadow_error_rate:.1%} — "
            f"the candidate failed {shadow_errors} of {pairs} mirrored calls"
        )

    check(
        "match_rate",
        match_rate,
        f">= {thresholds.min_match_rate}",
        (
            match_rate >= thresholds.min_match_rate
            if match_rate is not None
            else None
        ),
    )
    if match_rate is not None and match_rate < thresholds.min_match_rate:
        divergent = int(evidence.get("divergent") or 0)
        reasons.append(
            f"match rate {match_rate:.1%} below min_match_rate "
            f"{thresholds.min_match_rate:.1%} — {divergent} of {comparable} "
            f"comparable answers diverged"
        )

    # -- hold tier: acceptable answers, business-trade concerns ------
    check(
        "similarity_mean",
        similarity_mean,
        f">= {thresholds.min_similarity_mean}",
        (
            similarity_mean >= thresholds.min_similarity_mean
            if similarity_mean is not None
            else None
        ),
    )
    if (
        similarity_mean is not None
        and similarity_mean < thresholds.min_similarity_mean
    ):
        reasons.append(
            f"mean similarity {similarity_mean:.3f} below "
            f"min_similarity_mean {thresholds.min_similarity_mean:.3f} — "
            f"answers match but are cosmetically drifted overall"
        )

    # Latency compares means only when both sides have one (a trial
    # replaying pruned primaries, or bodies-off, is honestly skipped —
    # never a fake 0%).
    latency_pct = (
        (shadow_latency - primary_latency) / primary_latency * 100.0
        if shadow_latency is not None
        and primary_latency is not None
        and primary_latency > 0
        else None
    )
    check(
        "latency_delta_pct",
        round(latency_pct, 2) if latency_pct is not None else None,
        f"<= {thresholds.max_latency_delta_pct}",
        (
            latency_pct <= thresholds.max_latency_delta_pct
            if latency_pct is not None
            else None
        ),
    )
    if latency_pct is not None and latency_pct > thresholds.max_latency_delta_pct:
        reasons.append(
            f"candidate mean latency {latency_pct:+.1f}% vs the serving "
            f"primary (max_latency_delta_pct {thresholds.max_latency_delta_pct:.1f}% "
            f"— slower answers are a business call, not a correctness bar)"
        )

    # -- evidence sufficiency gates the verdict --------------------------
    # Two volumes, one bar: ``pairs`` is what a shadow-error rate can
    # be measured on (every mirror call is evidence), ``comparable`` is
    # what answer equivalence can be judged on. Thin on either — an
    # open question, whatever the observable numbers say. ONE
    # exception: a shadow-error rate measured on a full volume of
    # pairs and failing its bar is a no_go even when nothing was
    # comparable — a candidate that fails the traffic it is given has
    # already answered the question; a thin comparable leg cannot
    # downgrade that.
    check(
        "comparable_pairs",
        comparable,
        f">= {thresholds.min_pairs}",
        comparable >= thresholds.min_pairs,
    )
    if pairs < thresholds.min_pairs:
        reasons.insert(
            0,
            f"only {pairs} pair(s) — min_pairs {thresholds.min_pairs} — "
            f"collect more mirrored traffic or run a candidate trial "
            f"before judging",
        )
        return {
            "verdict": RECOMMENDATION_INSUFFICIENT,
            "checks": checks,
            "reasons": reasons,
        }
    if error_rate is not None and error_rate > thresholds.max_shadow_error_rate:
        # Full-volume failure — the escape above. reasons already
        # carries the error failure (and any other measurable ones).
        return {"verdict": RECOMMENDATION_NO_GO, "checks": checks, "reasons": reasons}
    if comparable < thresholds.min_pairs:
        reasons.insert(
            0,
            f"only {comparable} of {pairs} pair(s) were comparable — "
            f"min_pairs {thresholds.min_pairs} — the rest were errors or "
            f"not_comparable and prove nothing about answer equivalence",
        )
        return {
            "verdict": RECOMMENDATION_INSUFFICIENT,
            "checks": checks,
            "reasons": reasons,
        }

    # Worst honest call: any no_go outranks any hold.
    verdict = RECOMMENDATION_GO
    for measured in checks:
        if measured["pass"] is False:
            verdict = (
                RECOMMENDATION_NO_GO
                if measured["name"] in ("shadow_error_rate", "match_rate")
                else _worse(verdict, RECOMMENDATION_HOLD)
            )
    return {"verdict": verdict, "checks": checks, "reasons": reasons}


def _worse(current: str, candidate: str) -> str:
    return candidate if _SEVERITY[candidate] > _SEVERITY[current] else current


# ---------------------------------------------------------------------------
# tier 40: the post-promotion watch — verdicts over serving rows
# ---------------------------------------------------------------------------

# What counts as a deployment SERVING a request: the attributed,
# client-facing row the gateway logs once a dispatch COMPLETED on the
# deployment (the request's final outcome — success, or an error after
# the dispatch: a stream that dies mid-flight; a request that fails
# before ANY dispatch completes — the router exhausted its ladder —
# logs with no deployment and therefore never lands on a watch row:
# a failing candidate shows up as traffic that ran away, which the
# share split tells, never as fabricated attributed errors). Mirrors
# (shadow twins AND prompt A/B variants) are evidence, not traffic;
# trial replays are experiments, not traffic; a cache replay measures
# the cache, not the deployment. None of them reach the watch.
_SERVING_ROW = (
    RequestLog.shadow.isnot(True),
    RequestLog.trial_id.is_(None),
    RequestLog.cache_hit.is_(False),
)


def _serving_stats(row: Any) -> dict[str, Any]:
    """One aggregate row → the watch's stats shape (nulls when the
    leg was never measured — the honesty stance, never faked 0s)."""
    requests = int(row.requests or 0)
    errors = int(row.errors or 0)
    return {
        "requests": requests,
        "errors": errors,
        "error_rate": round(errors / requests, 4) if requests else None,
        "latency_mean_ms": round(float(row.latency_mean), 1)
        if row.latency_mean is not None
        else None,
        "cost_mean_usd": round(float(row.cost_mean), 6)
        if row.cost_mean is not None
        else None,
        "first_served_at": row.first_served_at,
    }


async def serving_overview(
    session: AsyncSession, model: str, since: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Per-deployment serving aggregates for one public model — the
    watch's evidence base, in ONE grouped query: the requests each
    deployment actually carried (serving rows only), its error count,
    its latency/cost means, and ``first_served_at`` — when it started
    serving the model, the anchor the watch window opens at (the
    promotion moment, measured on the traffic itself; no side state).
    ``since`` (the ``?since=`` override) bounds every aggregate
    instead — the anchor is then the override, not the first row."""
    conditions = [RequestLog.model == model, *_SERVING_ROW]
    if since is not None:
        conditions.append(RequestLog.created_at >= since)
    result = await session.execute(
        select(
            RequestLog.deployment,
            func.count(RequestLog.id).label("requests"),
            func.sum(case((RequestLog.status == "error", 1), else_=0)).label(
                "errors"
            ),
            func.avg(RequestLog.latency_ms).label("latency_mean"),
            func.avg(RequestLog.cost_usd).label("cost_mean"),
            func.min(RequestLog.created_at).label("first_served_at"),
        ).where(*conditions).group_by(RequestLog.deployment)
    )
    return {
        row.deployment: _serving_stats(row)
        for row in result
        if row.deployment is not None
    }


async def serving_baseline(
    session: AsyncSession,
    model: str,
    deployments: list[str],
    since: datetime,
) -> dict[str, Any]:
    """The tier peers' aggregate over the candidate's window — the
    honest comparison leg: the same traffic regime, the same clock.
    One light query; the endpoint calls it once per watched
    deployment that has traffic."""
    result = await session.execute(
        select(
            func.count(RequestLog.id).label("requests"),
            func.sum(case((RequestLog.status == "error", 1), else_=0)).label(
                "errors"
            ),
            func.avg(RequestLog.latency_ms).label("latency_mean"),
        ).where(
            RequestLog.model == model,
            *_SERVING_ROW,
            RequestLog.deployment.in_(deployments),
            RequestLog.created_at >= since,
        )
    )
    row = result.one()
    requests = int(row.requests or 0)
    errors = int(row.errors or 0)
    return {
        "requests": requests,
        "errors": errors,
        "error_rate": round(errors / requests, 4) if requests else None,
        "latency_mean_ms": round(float(row.latency_mean), 1)
        if row.latency_mean is not None
        else None,
    }


def evaluate_serving(stats: dict[str, Any], thresholds: MigrationConfig) -> dict[str, Any]:
    """Verdict one deployment's serving stats against the same
    ``migration:`` thresholds — the post-promotion watch, tier 40.

    ``stats`` is the shape the watch endpoint assembles: the
    deployment's own ``requests`` / ``errors`` / ``latency_mean_ms``
    and its tier peers' aggregate (``baseline_requests`` /
    ``baseline_errors`` / ``baseline_latency_mean_ms``; zero requests
    means no peers carried traffic in the window — nothing to
    compare against).

    The checks, mirroring ``evaluate``'s contract (value + threshold
    + pass, null when unmeasurable, one human reason per FAILED
    check):

    * ``requests`` — the sufficiency gate (``min_pairs``): a serving
      verdict needs volume; a bad five-minute window is not a verdict.
    * ``error_rate`` — the deployment's own error rate over its
      attributed rows (``max_shadow_error_rate`` — the same SLO, judged
      on serving traffic). Attribution is the honest part: a row lands
      on a deployment only when a dispatch COMPLETED on it, so the
      error rows here are failures after that point — a stream that
      died mid-flight — while a candidate failing at dispatch (its
      every attempt rescued by a tier peer, or failing with no peer
      to rescue it) leaves no attributed row at all: it shows up as
      traffic that ran away, which the watch row's expected→actual
      share split says louder than any attributed error rate could.
    * ``latency_delta_pct`` — the deployment's mean latency as a
      percent of the peers' mean (``max_latency_delta_pct``) —
      unmeasurable when no peer carried traffic in the window or the
      peer mean is zero, honestly skipped then.

    The verdict flow: thin volume → ``insufficient_evidence``; a
    failed error rate on a full volume → ``no_go`` (the candidate
    fails the traffic it is given); a failed latency delta →
    ``hold`` (slower answers are a business call — but on serving
    traffic, an operator reading ``hold`` on a freshly promoted
    candidate knows where the rollback button is); else ``go``. A
    no_go always outranks a hold — the worst honest call wins."""
    requests = int(stats.get("requests") or 0)
    errors = int(stats.get("errors") or 0)
    error_rate = round(errors / requests, 4) if requests else None
    latency_mean = stats.get("latency_mean_ms")
    baseline_requests = int(stats.get("baseline_requests") or 0)
    baseline_errors = int(stats.get("baseline_errors") or 0)
    baseline_rate = (
        round(baseline_errors / baseline_requests, 4) if baseline_requests else None
    )
    baseline_latency = stats.get("baseline_latency_mean_ms")

    checks: list[dict[str, Any]] = []
    reasons: list[str] = []

    def check(name: str, value: Any, threshold: str, passed: bool | None) -> None:
        checks.append(
            {"name": name, "value": value, "threshold": threshold, "pass": passed}
        )

    # -- sufficiency gates the verdict first --------------------------
    check("requests", requests, f">= {thresholds.min_pairs}", requests >= thresholds.min_pairs)
    if requests < thresholds.min_pairs:
        reasons.append(
            f"only {requests} serving request(s) since it started serving "
            f"— min_pairs {thresholds.min_pairs} — watch longer before "
            f"judging"
        )
        return {
            "verdict": RECOMMENDATION_INSUFFICIENT,
            "checks": checks,
            "reasons": reasons,
        }

    # -- the deployment's own error rate ------------------------------
    check(
        "error_rate",
        error_rate,
        f"<= {thresholds.max_shadow_error_rate}",
        error_rate <= thresholds.max_shadow_error_rate if error_rate is not None else None,
    )
    if error_rate is not None and error_rate > thresholds.max_shadow_error_rate:
        reasons.append(
            f"error rate {error_rate:.1%} over max_shadow_error_rate "
            f"{thresholds.max_shadow_error_rate:.1%} — the deployment "
            f"failed {errors} of the {requests} requests that ended on it"
        )

    # -- latency vs the tier peers, as a percent of their mean --------
    latency_pct = (
        (latency_mean - baseline_latency) / baseline_latency * 100.0
        if latency_mean is not None
        and baseline_latency is not None
        and baseline_latency > 0
        else None
    )
    check(
        "latency_delta_pct",
        round(latency_pct, 2) if latency_pct is not None else None,
        f"<= {thresholds.max_latency_delta_pct}",
        latency_pct <= thresholds.max_latency_delta_pct if latency_pct is not None else None,
    )
    if latency_pct is not None and latency_pct > thresholds.max_latency_delta_pct:
        reasons.append(
            f"mean latency {latency_pct:+.1f}% vs its priority tier "
            f"(max_latency_delta_pct {thresholds.max_latency_delta_pct:.1f}%) "
            f"— slower answers on serving traffic are a business call, "
            f"and the rollback button is one click away"
        )
    if latency_pct is None:
        note = (
            "no other serving deployment in its priority tier to compare "
            "latency against"
            if baseline_requests == 0
            else "tier peers carried no measurable latency in the window"
        )
        reasons.append(f"latency unmeasured — {note}")

    # -- the verdict: errors are the loud finding, latency the soft one
    if error_rate is not None and error_rate > thresholds.max_shadow_error_rate:
        return {"verdict": RECOMMENDATION_NO_GO, "checks": checks, "reasons": reasons}
    verdict = RECOMMENDATION_HOLD if (
        latency_pct is not None and latency_pct > thresholds.max_latency_delta_pct
    ) else RECOMMENDATION_GO
    return {"verdict": verdict, "checks": checks, "reasons": reasons}
