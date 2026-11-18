"""Shadow diffing: the evidence layer over mirrored traffic (tier 35).

Shadow mirroring (tier 13) already sends a copy of every successful
chat request to the model's shadow deployments and writes a paired
log row — same ``request_id``, its own body, latency, cost and tokens.
What was missing is the *comparison*: an operator weighing a provider
migration had hundreds of pairs and no way to answer "how divergent is
the candidate? at what latency/cost delta? show me the worst pairs."

This module is that evidence layer, in two halves:

* **Scoring** (pure functions, wired into the mirror tasks in
  ``pipeline.py``): each mirror's answer is scored against the
  primary's **raw provider text** — the pre-guardrail response on the
  buffered path, the accumulated source text on the streaming path —
  against the mirror's equally raw answer (mirrors run no guardrails),
  so a primary-leg rewrite never reads as provider drift. The verdict
  buckets are deliberately coarse and fixed (no config surface):

  - ``exact``        — similarity 1.0, byte-identical text
  - ``near``         — similarity ≥ NEAR_THRESHOLD (0.80): same answer,
                       cosmetic drift (whitespace, punctuation, phrasing)
  - ``divergent``    — below the threshold: different answers
  - ``shadow_error`` — the mirror call itself failed
  - ``not_comparable`` — no text on either side (tool-call replies,
                       empty content, unextractable body shapes)

  The similarity is ``difflib.SequenceMatcher``'s ratio — deterministic,
  dependency-free and good enough for "did the provider drift"; the raw
  score is stored on the row so an operator can re-bucket however they
  like. Both verdict and similarity are computed in the mirror task
  and written onto the shadow row, so aggregation stays a SQL
  ``GROUP BY`` over stored columns — never a re-diff of every pair.

* **Reporting** (session-based queries, served by the admin API):
  aggregate per (model, shadow deployment) — pair counts, verdict
  breakdown, match rate, mean similarity, latency and cost deltas
  against the paired primaries — and a per-pair drilldown for reading
  the divergent ones side by side. Deployment mirrors only: prompt
  A/B rows (also shadow rows, grouped by ``prompt_variant``) are
  excluded — their divergence is by design (a different prompt was
  tested), which would poison a migration signal. Those rows still
  carry verdicts on the log surface for the prompt workflow.

Comparability is honest about the persistence boundary: verdict and
similarity are computed in memory at mirror time, so they exist even
when ``logging.store_bodies`` is off — but the drilldown can only show
excerpts for rows whose bodies were actually stored.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import aliased

from ..models import RequestLog

# Verdict vocabulary (stored on RequestLog.shadow_verdict).
VERDICT_EXACT = "exact"
VERDICT_NEAR = "near"
VERDICT_DIVERGENT = "divergent"
VERDICT_SHADOW_ERROR = "shadow_error"
VERDICT_NOT_COMPARABLE = "not_comparable"
VERDICTS = (
    VERDICT_EXACT,
    VERDICT_NEAR,
    VERDICT_DIVERGENT,
    VERDICT_SHADOW_ERROR,
    VERDICT_NOT_COMPARABLE,
)

# exact = ratio 1.0; >= this is "near"; below is "divergent". A fixed,
# documented constant (not config): the raw similarity ships on every
# row, so re-bucketing is always possible after the fact.
NEAR_THRESHOLD = 0.80


# ----------------------------------------------------------------------
# Scoring (pure functions — unit-tested without a database)
# ----------------------------------------------------------------------


def extract_content_text(body: Any) -> str | None:
    """Best-effort text of a logged response body.

    Handles the two shapes a primary's body takes — the full
    OpenAI-shaped response (``choices[].message.content``, a string or
    a content-parts array) and the synthesized streaming body the
    pipeline logs for streamed requests (``{"content": <text>, ...}``)
    — plus a bare ``{"content": ...}`` dict. Returns None when nothing
    textual can be extracted (tool-call replies, multimodal parts
    without text, foreign shapes): honest "not comparable", never a
    wrong diff.
    """
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content")
        return _text_of_content(content)
    if "content" in body:
        return _text_of_content(body.get("content"))
    return None


def _text_of_content(content: Any) -> str | None:
    """One content field to text: a string, or an array of parts whose
    ``text`` members are joined. None for anything untextual (None,
    empty, tool-call structures)."""
    if isinstance(content, str):
        return content if content else None
    if isinstance(content, list):
        parts = [
            p.get("text")
            for p in content
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        ]
        joined = "".join(parts)
        return joined if joined else None
    return None


def similarity(primary_text: str, shadow_text: str) -> float:
    """difflib ratio of the two texts — deterministic, in [0.0, 1.0]."""
    return SequenceMatcher(None, primary_text, shadow_text).ratio()


def score_pair(
    primary_text: str | None,
    shadow_text: str | None,
    shadow_status: str,
) -> tuple[str, float | None]:
    """Verdict + similarity for one mirrored pair.

    The mirror's own outcome wins first: a failed mirror call is
    ``shadow_error`` (there is nothing to compare, and a candidate that
    fails traffic is the loudest migration signal of all). A missing
    text on either side is ``not_comparable`` — never a fake 0.0 that
    would read as maximal divergence. Otherwise the ratio buckets:
    1.0 exact, >= NEAR_THRESHOLD near, below divergent. The similarity
    is None exactly when no comparison happened (error /
    not-comparable), so SQL aggregates skip it.
    """
    if shadow_status != "success":
        return VERDICT_SHADOW_ERROR, None
    if primary_text is None or shadow_text is None:
        return VERDICT_NOT_COMPARABLE, None
    ratio = similarity(primary_text, shadow_text)
    if ratio == 1.0:
        return VERDICT_EXACT, ratio
    if ratio >= NEAR_THRESHOLD:
        return VERDICT_NEAR, ratio
    return VERDICT_DIVERGENT, ratio


# ----------------------------------------------------------------------
# Reporting (the admin API's data layer)
# ----------------------------------------------------------------------


def _pair_conditions(
    shadow,
    primary,
    model: str | None = None,
    deployment: str | None = None,
    verdict: str | None = None,
    since=None,
    trial_id: str | list[str] | None = None,
) -> list:
    """The WHERE terms shared by the aggregate and the drilldown.

    Deployment mirrors only — ``prompt_variant IS NULL`` keeps prompt
    A/B rows out (their divergence is by design). Only scored rows:
    mirrors written before tier 35 carry no verdict and are not
    re-guessed. The primary join condition (not a WHERE term) is LEFT:
    a primary can be missing (retention pruning, legacy rows) — the
    pair still scored, only its baseline deltas are absent.

    Tier 36: ``trial_id`` scopes the pair set. None (the standing
    scope) excludes candidate-trial rows — a deliberate replay is an
    experiment, not live mirroring, and would double-count a candidate
    already shadow-mirrored; a trial id selects exactly that trial's
    pairs (the /admin/trials/{id}/pairs drilldown). Tier 37 accepts a
    LIST of ids too — one grouped query over many trials
    (trial_baselines), same terms.
    """
    terms = [
        shadow.shadow.is_(True),
        shadow.prompt_variant.is_(None),
        shadow.shadow_verdict.is_not(None),
        (
            shadow.trial_id.is_(None)
            if trial_id is None
            else (
                shadow.trial_id.in_(trial_id)
                if isinstance(trial_id, list)
                else shadow.trial_id == trial_id
            )
        ),
    ]
    if model is not None:
        terms.append(shadow.model == model)
    if deployment is not None:
        terms.append(shadow.deployment == deployment)
    if verdict is not None:
        terms.append(shadow.shadow_verdict == verdict)
    if since is not None:
        terms.append(shadow.created_at >= since)
    return terms


def _primary_join(primary, shadow):
    return and_(
        primary.request_id == shadow.request_id,
        primary.shadow.is_(False),
    )


async def aggregate(
    session,
    model: str | None = None,
    since=None,
    trial_id: str | None = None,
) -> dict:
    """Migration evidence per (model, shadow deployment).

    One group per scored shadow deployment with the pair counts, the
    verdict breakdown, the match rate ((exact + near) / comparable),
    the mean similarity over comparable pairs, and the latency/cost/
    token deltas against the paired primaries. A group with zero
    comparable pairs reports null similarity/match-rate rather than a
    misleading number.

    ``trial_id`` (tier 36): None keeps the standing scope — live
    mirroring only, candidate-trial rows excluded; a trial id scopes
    the aggregate to that run (its rows share the trial's model and
    candidate deployment, so they collapse into one group).
    """
    shadow = aliased(RequestLog)
    primary = aliased(RequestLog)

    def counted(name: str):
        return func.sum(case((shadow.shadow_verdict == name, 1), else_=0))

    stmt = (
        select(
            shadow.model,
            shadow.deployment,
            shadow.provider,
            func.count().label("pairs"),
            counted(VERDICT_EXACT).label("exact"),
            counted(VERDICT_NEAR).label("near"),
            counted(VERDICT_DIVERGENT).label("divergent"),
            counted(VERDICT_SHADOW_ERROR).label("shadow_error"),
            counted(VERDICT_NOT_COMPARABLE).label("not_comparable"),
            func.avg(shadow.shadow_similarity).label("similarity_mean"),
            func.avg(shadow.latency_ms).label("shadow_latency_mean"),
            func.avg(primary.latency_ms).label("primary_latency_mean"),
            func.avg(shadow.completion_tokens).label("shadow_tokens_mean"),
            func.avg(primary.completion_tokens).label("primary_tokens_mean"),
            func.sum(shadow.cost_usd).label("shadow_cost"),
            func.sum(primary.cost_usd).label("primary_cost"),
            func.max(shadow.created_at).label("last_pair_at"),
        )
        .select_from(shadow)
        .outerjoin(primary, _primary_join(primary, shadow))
        .where(
            *_pair_conditions(
                shadow, primary, model=model, since=since, trial_id=trial_id
            )
        )
        .group_by(shadow.model, shadow.deployment, shadow.provider)
        .order_by(shadow.model, shadow.deployment)
    )
    rows = (await session.execute(stmt)).all()

    groups = []
    for row in rows:
        comparable = (row.exact or 0) + (row.near or 0) + (row.divergent or 0)
        groups.append(
            {
                "model": row.model,
                "deployment": row.deployment,
                "provider": row.provider,
                "pairs": row.pairs,
                "exact": row.exact or 0,
                "near": row.near or 0,
                "divergent": row.divergent or 0,
                "shadow_error": row.shadow_error or 0,
                "not_comparable": row.not_comparable or 0,
                "comparable": comparable,
                "match_rate": (
                    round(((row.exact or 0) + (row.near or 0)) / comparable, 4)
                    if comparable
                    else None
                ),
                "similarity_mean": (
                    round(row.similarity_mean, 4)
                    if row.similarity_mean is not None
                    else None
                ),
                "latency_delta_ms": (
                    int(row.shadow_latency_mean - row.primary_latency_mean)
                    if row.shadow_latency_mean is not None
                    and row.primary_latency_mean is not None
                    else None
                ),
                # The means themselves (tier 37): the migration verdict
                # expresses the latency concern as a PERCENT of the
                # primary's mean — an absolute delta means different
                # things on a 200ms and a 20s model.
                "shadow_latency_ms": (
                    int(row.shadow_latency_mean)
                    if row.shadow_latency_mean is not None
                    else None
                ),
                "primary_latency_ms": (
                    int(row.primary_latency_mean)
                    if row.primary_latency_mean is not None
                    else None
                ),
                "tokens_delta": (
                    int(row.shadow_tokens_mean - row.primary_tokens_mean)
                    if row.shadow_tokens_mean is not None
                    and row.primary_tokens_mean is not None
                    else None
                ),
                "cost_delta_usd": (
                    round(row.shadow_cost - row.primary_cost, 6)
                    if row.shadow_cost is not None and row.primary_cost is not None
                    else None
                ),
                "last_pair_at": (
                    row.last_pair_at.isoformat() if row.last_pair_at else None
                ),
            }
        )
    return {"groups": groups}


def _excerpt(body: Any, limit: int = 500) -> str | None:
    """The stored body's text, truncated for a drilldown payload. None
    when the body was not stored (``store.store_bodies`` off) or
    nothing textual is in it — the payload never fabricates text."""
    text = extract_content_text(body)
    if text is None:
        return None
    if len(text) > limit:
        return text[:limit] + " …"
    return text


async def pairs(
    session,
    model: str,
    deployment: str,
    verdict: str | None = None,
    limit: int = 25,
    offset: int = 0,
    trial_id: str | None = None,
) -> dict:
    """The drilldown: individual scored pairs for one shadow deployment,
    newest first — the divergent ones are the migration review queue.

    Each pair carries both sides' status/latency/tokens/cost and the
    stored bodies' text excerpts; ``primary.text``/``shadow.text`` are
    null when the bodies were not persisted (the verdict still exists
    — it was computed in memory at mirror time).

    ``trial_id`` (tier 36): None keeps the standing scope (live
    mirrors); a trial id returns that run's pairs instead — same
    shape, so the admin panel's drilldown dialog serves both.
    """
    limit = max(1, min(limit, 100))
    shadow = aliased(RequestLog)
    primary = aliased(RequestLog)
    conditions = _pair_conditions(
        shadow,
        primary,
        model=model,
        deployment=deployment,
        verdict=verdict,
        trial_id=trial_id,
    )
    rows_stmt = (
        select(shadow, primary)
        .outerjoin(primary, _primary_join(primary, shadow))
        .where(*conditions)
        .order_by(shadow.created_at.desc(), shadow.id.desc())
        .limit(limit)
        .offset(offset)
    )
    total_stmt = (
        select(func.count())
        .select_from(shadow)
        .outerjoin(primary, _primary_join(primary, shadow))
        .where(*conditions)
    )
    total = (await session.execute(total_stmt)).scalar_one()
    rows = (await session.execute(rows_stmt)).all()

    out = []
    for shadow_row, primary_row in rows:
        out.append(
            {
                "request_id": shadow_row.request_id,
                "created_at": (
                    shadow_row.created_at.isoformat()
                    if shadow_row.created_at
                    else None
                ),
                "verdict": shadow_row.shadow_verdict,
                "similarity": shadow_row.shadow_similarity,
                "primary": (
                    None
                    if primary_row is None
                    else {
                        "status": primary_row.status,
                        "latency_ms": primary_row.latency_ms,
                        "completion_tokens": primary_row.completion_tokens,
                        "cost_usd": primary_row.cost_usd,
                        "text": _excerpt(primary_row.response_body),
                    }
                ),
                "shadow": {
                    "status": shadow_row.status,
                    "latency_ms": shadow_row.latency_ms,
                    "completion_tokens": shadow_row.completion_tokens,
                    "cost_usd": shadow_row.cost_usd,
                    "text": _excerpt(shadow_row.response_body),
                },
            }
        )
    return {"data": out, "total": total, "limit": limit, "offset": offset}


async def trial_baselines(session, trial_ids: list[str]) -> dict[str, dict]:
    """Mean latency of each trial's candidate calls against their
    original primaries' recorded latencies — one grouped query over
    the same pairing join the drilldown uses (``_primary_join``),
    scoped per trial id. Tier 37: this is the latency leg of a trial
    run's evidence (``trials.run_summaries`` folds it in) so
    ``core/migration.evaluate`` sees the same shape a mirror group
    carries. A trial row whose primary was retention-pruned joins no
    baseline — the mean over the rows that did, honestly.

    Returns ``{trial_id: {"shadow_latency_ms", "primary_latency_ms"}}``
    for exactly the ids that have trial rows; missing ids mean no
    rows (the summary reports nulls, the verdict skips the check).
    """
    if not trial_ids:
        return {}
    shadow = aliased(RequestLog)
    primary = aliased(RequestLog)
    rows = (
        (
            await session.execute(
                select(
                    shadow.trial_id,
                    func.avg(shadow.latency_ms).label("shadow_latency"),
                    func.avg(primary.latency_ms).label("primary_latency"),
                )
                .select_from(shadow)
                .outerjoin(primary, _primary_join(primary, shadow))
                .where(*_pair_conditions(shadow, primary, trial_id=list(trial_ids)))
                .group_by(shadow.trial_id)
            )
        )
        .all()
    )
    return {
        row.trial_id: {
            "shadow_latency_ms": (
                int(row.shadow_latency) if row.shadow_latency is not None else None
            ),
            "primary_latency_ms": (
                int(row.primary_latency) if row.primary_latency is not None else None
            ),
        }
        for row in rows
    }
