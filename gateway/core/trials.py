"""Candidate trials (tier 36): the deliberate-replay leg of the
provider-migration epic.

Shadow mirroring (tier 13) + shadow diffing (tier 35) answer "how does
a candidate compare" for *live* traffic — but only for traffic the
model actually gets, only for candidates wired as shadow deployments,
and only at the rate real requests arrive. Candidate trials close the
gap: ``POST /admin/trials`` deliberately replays a captured traffic
window (request-log rows of one public model, with stored bodies)
against a named candidate deployment — **real upstream calls**, unlike
the dry-run replay harness — and scores every replayed entry with the
tier-35 verdict vocabulary, so the same evidence surface (aggregate +
drilldown + the admin panel) serves both.

Three deliberate boundaries shape the runner:

* **Mirror-like execution.** Trial calls skip guardrails, counters,
  budgets, caches, lifecycle events and metrics — and, unlike mirrors,
  even *breaker bookkeeping*: a candidate that is currently serving
  traffic must not have its breaker punished by an experiment against
  it. The run row plus the scored log rows ARE the evidence surface
  (no Prometheus counters — an admin-scoped experiment is not fleet
  telemetry).
* **Throttled and spend-capped.** Entries replay in waves of at most
  ``concurrency`` parallel upstream calls (the batch worker's wave
  precedent); between waves the runner re-reads the trial row and
  stops before the next wave when cumulative ``cost_usd`` has reached
  ``max_cost_usd`` or the status changed. The cap is a *between-wave*
  cap by design: it can overshoot by at most one wave's cost, and it
  can never abort calls already in flight.
* **DB status is the only truth.** Claiming, progress, cost and
  finalization are guarded ``UPDATE``s keyed on the row's current
  status, and the runner re-reads status between waves — so a cancel
  from the admin API, another replica, or the startup sweep always
  wins within one wave, on any number of replicas.

Scoring honesty, pinned by tests: the primary's RAW provider text is
not persisted (only the client-seen body is), so trials score against
the *stored* body — and when the primary's ``guardrail_actions`` show
a response/stream ``modify`` (the stored body is the rewritten,
client-seen text), the pair is ``not_comparable`` instead of fake
divergence. Raw-vs-raw stays impossible-to-fake the same way it is
for mirrors; it just costs one bucket here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import DeploymentConfig, GatewayConfig, TrialsConfig
from ..db import get_sessionmaker
from ..errors import GatewayError
from ..models import RequestLog, Trial
from ..observability import wire
from ..schemas import ChatCompletionRequest
from .context import AuthContext, RequestContext
from .shadow_diff import (
    VERDICT_EXACT,
    VERDICT_NEAR,
    VERDICT_DIVERGENT,
    VERDICT_NOT_COMPARABLE,
    VERDICT_SHADOW_ERROR,
    extract_content_text,
    score_pair,
    trial_baselines,
)

logger = logging.getLogger(__name__)

# Why a run stopped (Trial.stop_reason). "done"/"no_entries"/"cost_cap"
# are normal completions; "cancelled" belongs to the cancel path;
# "orphaned_by_restart" to the startup sweep; "error" to a failed run
# (details in error_message).
STOP_DONE = "done"
STOP_NO_ENTRIES = "no_entries"
STOP_COST_CAP = "cost_cap"
STOP_CANCELLED = "cancelled"
STOP_ORPHANED = "orphaned_by_restart"
STOP_ERROR = "error"


# ----------------------------------------------------------------------
# Validation helpers (the admin endpoint's data layer)
# ----------------------------------------------------------------------


def resolve_deployment(config: GatewayConfig, name: str) -> DeploymentConfig | None:
    """Find a configured deployment by name, across every model.

    A trial's candidate may be ANY configured deployment — it does not
    need to belong to the replayed model: the adapter maps the request
    to ``deployment.model``, which is exactly how migrations work (a
    candidate usually runs a *different* upstream model).
    """
    for model in config.models:
        for deployment in model.deployments:
            if deployment.name == name:
                return deployment
    return None


def clamp_params(
    config: TrialsConfig,
    limit: int | None = None,
    concurrency: int | None = None,
    max_cost_usd: float | None = None,
) -> dict[str, Any]:
    """Fold the request's run parameters into what will actually run.

    Missing values fall back to the ceiling (the safe end of the
    range); explicit values are clamped into ``[1, ceiling]`` (cost
    into ``(0, ceiling]``) — a caller can never ask for more replay
    volume, parallelism or spend than ``trials:`` allows, and the
    stored params are always the clamped truth the runner used.
    """
    return {
        "since": None,  # set by the endpoint after ISO-8601 parsing
        "until": None,
        "limit": max(1, min(int(limit or config.max_entries), config.max_entries)),
        "concurrency": max(
            1, min(int(concurrency or config.max_concurrency), config.max_concurrency)
        ),
        "max_cost_usd": min(
            float(max_cost_usd or config.max_cost_usd), config.max_cost_usd
        ),
    }


def validate_run_request(
    config: GatewayConfig, model: str, deployment: str
) -> DeploymentConfig:
    """400-class validation shared by the endpoint: the public model
    must exist and the candidate deployment must be configured (any
    model's — see resolve_deployment)."""
    if not any(m.name == model for m in config.models):
        raise GatewayError(
            400,
            f"Unknown model '{model}': no such public model is configured.",
            "invalid_request_error",
            param="model",
        )
    target = resolve_deployment(config, deployment)
    if target is None:
        raise GatewayError(
            400,
            f"Unknown deployment '{deployment}': no configured deployment "
            "has that name.",
            "invalid_request_error",
            param="deployment",
        )
    return target


# ----------------------------------------------------------------------
# Entry selection + the scoring honesty check
# ----------------------------------------------------------------------


async def select_entries(
    session: AsyncSession,
    model: str,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    """The captured window to replay: client-facing chat rows of one
    public model, successful (a failed primary has no answer to
    compare — mirrors only fire on success too), with stored bodies
    (nothing to replay without one), newest first.

    Rows are SNAPSHOT into plain dicts here — the waves run on their
    own sessions, and a retention prune must never race a replay.
    """
    terms = [
        RequestLog.model == model,
        RequestLog.route == "chat",
        RequestLog.status == "success",
        RequestLog.shadow.is_(False),
        RequestLog.request_body.is_not(None),
    ]
    if since is not None:
        terms.append(RequestLog.created_at >= since)
    if until is not None:
        terms.append(RequestLog.created_at < until)
    rows = (
        (
            await session.execute(
                select(RequestLog)
                .where(*terms)
                .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "request_id": row.request_id,
            "key_name": row.key_name,
            "request_body": row.request_body,
            "response_body": row.response_body,
            "rewritten": _primary_rewritten(row.guardrail_actions),
        }
        for row in rows
    ]


def _primary_rewritten(actions: Any) -> bool:
    """Did a response/stream guardrail REWRITE the primary?

    The stored primary body is what the CLIENT saw; a response/stream
    ``modify`` action means it was rewritten in flight, so the raw
    provider text is gone. Scoring a raw candidate answer against it
    would read a guardrail rewrite as provider drift — the one lie
    shadow diffing exists to prevent. Callers force ``not_comparable``
    for such primaries instead.
    """
    if not isinstance(actions, list):
        return False
    for action in actions:
        if (
            isinstance(action, dict)
            and action.get("phase") in ("response", "stream")
            and action.get("action") == "modify"
        ):
            return True
    return False


# ----------------------------------------------------------------------
# The runner
# ----------------------------------------------------------------------


async def run_trial(pipeline: Any, trial_id: str) -> None:
    """Execute one trial: claim it, replay the selected window in
    cost-capped waves, finalize. Spawned via ``pipeline._spawn`` (so
    shutdown drains it); safe to spawn twice — the claim is guarded on
    ``status='pending'``.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        claim = await session.execute(
            update(Trial)
            .where(Trial.id == trial_id, Trial.status == "pending")
            .values(status="running", started_at=datetime.now(timezone.utc))
        )
        await session.commit()
        if (claim.rowcount or 0) == 0:
            return  # cancelled while pending, already claimed, unknown
        trial = await session.get(Trial, trial_id)
        if trial is None:
            return
        params = dict(trial.params or {})
        model = trial.model

    deployment = resolve_deployment(pipeline.router.config, trial.deployment)
    if deployment is None:
        # The candidate vanished in a config reload between create and
        # run — nothing to replay against.
        await _finalize(
            sessionmaker,
            trial_id,
            "failed",
            STOP_ERROR,
            error="candidate deployment is no longer configured (config reloaded?)",
        )
        return

    try:
        async with sessionmaker() as session:
            entries = await select_entries(
                session,
                model,
                _parse_dt(params.get("since")),
                _parse_dt(params.get("until")),
                int(params.get("limit") or 1),
            )
            if entries:
                await session.execute(
                    update(Trial)
                    .where(Trial.id == trial_id)
                    .values(selected=len(entries))
                )
                await session.commit()

        if not entries:
            await _finalize(sessionmaker, trial_id, "completed", STOP_NO_ENTRIES)
            return

        concurrency = max(1, int(params.get("concurrency") or 1))
        max_cost = float(params.get("max_cost_usd") or 0.0)
        stop_reason = STOP_DONE
        index = 0
        while index < len(entries):
            # Between waves: DB status is the only truth — a cancel from
            # the API, another replica or the startup sweep wins here
            # and owns the row's final state; the cost cap refuses the
            # NEXT wave (never one already in flight).
            async with sessionmaker() as session:
                current = await session.get(Trial, trial_id)
                if current is None or current.status != "running":
                    return
                if current.cost_usd >= max_cost:
                    stop_reason = STOP_COST_CAP
                    break
            wave = entries[index : index + concurrency]
            index += len(wave)
            results = await asyncio.gather(
                *(_run_entry(pipeline, trial_id, model, deployment, e) for e in wave)
            )
            done = sum(1 for wrote, _ in results if wrote)
            skipped = sum(1 for wrote, _ in results if not wrote)
            wave_cost = sum(cost for _, cost in results)
            # Column-arithmetic update: concurrent writers (another
            # replica's progress, a cancel landing mid-run) can never
            # lose a wave's counters.
            async with sessionmaker() as session:
                await session.execute(
                    update(Trial)
                    .where(Trial.id == trial_id)
                    .values(
                        completed=Trial.completed + done,
                        skipped=Trial.skipped + skipped,
                        cost_usd=Trial.cost_usd + wave_cost,
                    )
                )
                await session.commit()
        await _finalize(sessionmaker, trial_id, "completed", stop_reason)
    except Exception as exc:  # noqa: BLE001 — a trial must never crash the app
        logger.exception("trial %s failed", trial_id)
        await _finalize(sessionmaker, trial_id, "failed", STOP_ERROR, error=str(exc)[:500])


async def _finalize(
    sessionmaker, trial_id: str, status: str, stop_reason: str, error: str | None = None
) -> bool:
    """Guarded finalization: only a still-``running`` row transitions —
    a cancel that landed between the last checks owns the row."""
    async with sessionmaker() as session:
        result = await session.execute(
            update(Trial)
            .where(Trial.id == trial_id, Trial.status == "running")
            .values(
                status=status,
                stop_reason=stop_reason,
                error_message=error,
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        return (result.rowcount or 0) > 0


def _parse_dt(value: Any) -> datetime | None:
    """Stored params carry ISO strings (JSON columns); the runner
    tolerates either shape."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


async def _run_entry(
    pipeline: Any,
    trial_id: str,
    model: str,
    deployment: DeploymentConfig,
    entry: dict[str, Any],
) -> tuple[bool, float]:
    """Replay one captured entry against the candidate and write its
    scored shadow row. Returns ``(row_written, cost)`` — no row means
    the stored body could not be parsed (counted as skipped).

    Mirror-like by construction: direct ``adapter.complete`` (no
    guardrails, counters, budgets, cache, events or metrics), no
    breaker bookkeeping either — trials must not punish a serving
    candidate. The row shares the ORIGINAL primary's request_id and
    carries ``trial_id``; cost never touches key/tenant spend (no
    key_id on the auth context, shadow row).
    """
    try:
        request = ChatCompletionRequest.model_validate(entry["request_body"])
    except Exception:  # noqa: BLE001 — a malformed capture is a skip, not a crash
        return False, 0.0
    replay = request.model_copy(update={"stream": False, "stream_options": None})

    # The row records THIS call's wire capture, never the original's.
    wire.reset_upstream()
    started = time.monotonic()
    outcome, error_message, response = "success", None, None
    try:
        adapter = pipeline.router.providers[deployment.provider]
        response = await adapter.complete(
            deployment,
            replay,
            pipeline.router._provider_cfg(deployment),
            pipeline.router._timeout(deployment),
        )
    except GatewayError as exc:
        outcome, error_message = "error", exc.message
    except Exception as exc:  # noqa: BLE001 — one bad entry never sinks the wave
        outcome, error_message = "error", f"internal error: {exc}"

    usage = (response or {}).get("usage") or {}
    cost = (
        pipeline.cost_calculator.calculate(usage, deployment.model)
        or pipeline.cost_calculator.calculate(usage, model)
        if response is not None
        else None
    )
    # Honesty: the stored primary body is the CLIENT-SEEN text; a
    # response/stream rewrite on the primary makes raw-vs-raw
    # impossible, so the pair is not_comparable — never fake divergence.
    primary_text = (
        None if entry["rewritten"] else extract_content_text(entry["response_body"])
    )
    verdict, similarity = score_pair(
        primary_text, extract_content_text(response), outcome
    )
    ctx = RequestContext(
        request_id=entry["request_id"],  # correlation: the original primary's id
        auth=AuthContext(key_name=entry["key_name"], is_admin=False),
        # no key_id — trial rows never touch spend, like every shadow row
        model=model,
        provider=deployment.provider,
        deployment=deployment.name,
        native_model=deployment.model,
        stream=False,
        request_body=replay.model_dump(exclude_none=True),
        response_body=response,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        cost_usd=cost,
        status=outcome,
        error_message=error_message,
        shadow=True,
        shadow_verdict=verdict,
        shadow_similarity=similarity,
        trial_id=trial_id,
    )
    ctx.started_monotonic = started  # honest latency: call start, not ctx birth
    ctx.upstream_request = wire.upstream_request()
    ctx.upstream_headers = wire.upstream_headers()
    try:
        await pipeline.logging_service.write_request_log(ctx)
    except Exception:  # noqa: BLE001
        logger.exception("trial %s: failed to write log row", trial_id)
    return True, cost or 0.0


async def sweep_stale(sessionmaker) -> int:
    """Startup sweep: trials left ``pending``/``running`` by a process
    that died (or a spawn that never ran) are marked cancelled.

    Cross-replica honest: a run LIVE on another replica sees the
    cancel at its next between-wave check and stops within one wave —
    the sweep cannot corrupt a live run's rows, it can only stop it.
    Returns the number of rows swept."""
    async with sessionmaker() as session:
        result = await session.execute(
            update(Trial)
            .where(Trial.status.in_(["pending", "running"]))
            .values(
                status="cancelled",
                stop_reason=STOP_ORPHANED,
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        return result.rowcount or 0


# ----------------------------------------------------------------------
# Reporting (the admin API's data layer)
# ----------------------------------------------------------------------


async def run_summaries(session: AsyncSession, trial_ids: list[str]) -> dict[str, dict]:
    """Verdict counts + mean similarity for several trials' rows in one
    grouped query — the list endpoint merges these into its rows so the
    runs table can show the verdict badges without N+1 detail fetches.
    Computed from the log rows: the Trial row deliberately
    denormalizes nothing the pairs already say.

    Tier 37: the summary IS a migration evidence group — the same
    shape a ``shadow_diff.aggregate`` mirror group carries (comparable,
    match_rate, and the latency means against the original primaries,
    via shadow_diff.trial_baselines) — so ``core/migration.evaluate``
    verdicts a trial and a mirrored deployment identically through one
    code path.
    """
    if not trial_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(
                    RequestLog.trial_id,
                    RequestLog.shadow_verdict,
                    func.count(),
                    func.sum(RequestLog.shadow_similarity),
                )
                .where(RequestLog.trial_id.in_(trial_ids))
                .group_by(RequestLog.trial_id, RequestLog.shadow_verdict)
            )
        )
        .all()
    )
    per: dict[str, dict[str, Any]] = {
        trial_id: {
            "pairs": 0,
            VERDICT_EXACT: 0,
            VERDICT_NEAR: 0,
            VERDICT_DIVERGENT: 0,
            VERDICT_SHADOW_ERROR: 0,
            VERDICT_NOT_COMPARABLE: 0,
            "_similarity_sum": 0.0,
            "_comparable": 0,
        }
        for trial_id in trial_ids
    }
    for trial_id, verdict, count, similarity_total in rows:
        summary = per.get(trial_id)
        if summary is None:
            continue
        if verdict is not None and verdict in summary:
            summary[verdict] = count
        summary["pairs"] += count
        if similarity_total is not None:
            summary["_similarity_sum"] += float(similarity_total)
            summary["_comparable"] += count
    baselines = await trial_baselines(session, list(trial_ids))
    out: dict[str, dict[str, Any]] = {}
    for trial_id, summary in per.items():
        comparable = summary.pop("_comparable")
        similarity_sum = summary.pop("_similarity_sum")
        matched = summary[VERDICT_EXACT] + summary[VERDICT_NEAR]
        latency = baselines.get(trial_id) or {
            "shadow_latency_ms": None,
            "primary_latency_ms": None,
        }
        shadow_latency = latency.get("shadow_latency_ms")
        primary_latency = latency.get("primary_latency_ms")
        out[trial_id] = {
            **summary,
            "comparable": comparable,
            "match_rate": round(matched / comparable, 4) if comparable else None,
            "similarity_mean": (
                round(similarity_sum / comparable, 4) if comparable else None
            ),
            "shadow_latency_ms": shadow_latency,
            "primary_latency_ms": primary_latency,
            "latency_delta_ms": (
                shadow_latency - primary_latency
                if shadow_latency is not None and primary_latency is not None
                else None
            ),
        }
    return out


async def run_summary(session: AsyncSession, trial_id: str) -> dict[str, Any]:
    """The single-run form of run_summaries — the trial detail
    endpoint's go/no-go numbers."""
    summaries = await run_summaries(session, [trial_id])
    return summaries.get(trial_id) or _empty_summary()


def _empty_summary() -> dict[str, Any]:
    """The zero-evidence evidence group (an empty window, a pending
    run): every count zero, every mean null — evaluates to
    insufficient_evidence, never a fabricated 0.0."""
    return {
        "pairs": 0,
        VERDICT_EXACT: 0,
        VERDICT_NEAR: 0,
        VERDICT_DIVERGENT: 0,
        VERDICT_SHADOW_ERROR: 0,
        VERDICT_NOT_COMPARABLE: 0,
        "comparable": 0,
        "match_rate": None,
        "similarity_mean": None,
        "shadow_latency_ms": None,
        "primary_latency_ms": None,
        "latency_delta_ms": None,
    }
