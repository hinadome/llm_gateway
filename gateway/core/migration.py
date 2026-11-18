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

Thresholds live in the ``migration:`` config section, read live
(hot-reloadable — tighten a bar and the next read re-verdicts).
"""

from __future__ import annotations

from typing import Any

from ..config import MigrationConfig

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
