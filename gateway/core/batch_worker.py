"""Background executor for the Batch API (see api/batches.py).

One ``tick()`` = expire stale batches, then run one *wave* — up to
``wave_size`` input lines of the oldest runnable batch, sequentially
through ``pipeline.handle_chat``. Each line is a full interactive
request: guardrails, cache, routing, retries, spend accounting and a
request-log row (request_id ``<batch id>-L<line index>``, auth_method
``batch``).

Failure model (the part worth reading twice):

* A line only ever *fails* (a result row with status ``failed``) for
  per-request errors — unknown model, guardrail block, invalid body,
  upstream failure after retries. Exactly what the same request sent
  interactively would produce, recorded in the batch's error file.
* Rate limits and budget ceilings never fail a line: the wave stops,
  the batch defers (``retry_not_before``) and the line re-runs on a
  later tick. The 24h completion window bounds how long that can go
  on — then the batch expires instead.
* Crash safety: result rows append in the same transaction that
  advances the cursor, one wave ahead at most. A crash mid-wave
  re-runs that wave's lines on restart (duplicate request-log rows
  are possible; results are never lost). ``finalizing`` is the
  checkpoint from which a restart resumes output building without
  re-running any line.
* A wave's cursor advance is guarded on ``status='in_progress'`` — a
  cancel that lands mid-wave wins, and its partial results are simply
  never assembled into output files.

The worker never runs by itself under pytest (ASGITransport skips the
lifespan); tests drive ``tick()`` directly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import or_, select, update

from ..config import BatchConfig
from ..db import get_sessionmaker
from ..errors import BudgetExceededError, GatewayError, RateLimitError
from ..models import Batch, BatchResult, StoredFile, Tenant, VirtualKey
from ..observability import metrics
from ..schemas import ChatCompletionRequest
from .auth import auth_context_from_key, record_key_use
from .context import AuthContext

if TYPE_CHECKING:  # pragma: no cover — import for typing only
    from .pipeline import GatewayPipeline

logger = logging.getLogger("gateway.batch")

# Budget ceilings carry no Retry-After; re-check after a minute rather
# than spin (window expiry still bounds the total).
_DEFER_BUDGET_SECONDS = 60.0
# Rate limits without a parsed Retry-After header.
_DEFER_FALLBACK_SECONDS = 30.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validation_summary(exc: ValidationError) -> str:
    """One line out of pydantic's error list (the same shape the
    gateway's global validation handler reports)."""
    try:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first.get("loc", ()))
        return f"{loc}: {first.get('msg')}" if loc else str(first.get("msg"))
    except (IndexError, KeyError):  # pragma: no cover — defensive
        return str(exc)


class BatchWorker:
    """Drives batches forward, one wave per ``tick()``."""

    def __init__(self, pipeline: GatewayPipeline, config: BatchConfig) -> None:
        self.pipeline = pipeline
        # Public on purpose: the lifespan loop re-points this at the
        # (possibly reloaded) config before every tick, so wave_size
        # changes apply live.
        self.config = config

    # -- one step -------------------------------------------------------

    async def tick(self) -> None:
        """Expire stale batches, then run one wave of the oldest
        runnable batch (a finalizing batch finalizes instead)."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            expired = await self._expire_stale(session)
            if expired:
                logger.info(
                    "batch: expired %d batch(es) past their completion window",
                    len(expired),
                )
            batch = (
                (
                    await session.execute(
                        select(Batch.id)
                        .where(
                            or_(
                                Batch.status == "finalizing",
                                (Batch.status == "in_progress")
                                & or_(
                                    Batch.retry_not_before.is_(None),
                                    Batch.retry_not_before <= _now(),
                                ),
                            )
                        )
                        .order_by(Batch.created_at, Batch.id)
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        for batch_id in expired:
            await self._emit("batch.expired", batch_id=batch_id)
        if batch is not None:
            await self._run_wave(batch)

    async def _expire_stale(self, session) -> list[str]:
        """in_progress batches past their completion window → expired.
        (finalizing ones are excluded: their results are complete —
        the next tick finalizes them regardless of the window.)"""
        result = await session.execute(
            update(Batch)
            .where(
                Batch.status == "in_progress",
                Batch.expires_at.isnot(None),
                Batch.expires_at <= _now(),
            )
            .values(status="expired", expired_at=_now())
            .returning(Batch.id)
        )
        await session.commit()
        return list(result.scalars())

    # -- wave execution -------------------------------------------------

    async def _run_wave(self, batch_id: str) -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            batch = await session.get(Batch, batch_id)
            if batch is None:  # pragma: no cover — deleted mid-tick
                return
            if batch.status == "finalizing":
                # Crash-recovery checkpoint: results are complete, only
                # the output files still need building.
                await self._finalize(session, batch)
                return
            if batch.status != "in_progress":
                return  # cancelled/expired between selection and now
            if batch.next_line >= batch.total_requests:
                await self._mark_finalizing(session, batch)
                await self._finalize(session, batch)
                return
            await self._execute_wave(session, batch)

    async def _emit(self, name: str, **payload) -> None:
        """Observer-only lifecycle events (batches are background work —
        nothing to veto; subscribers get notified)."""
        if self.pipeline.events is not None:
            await self.pipeline.events.emit(name, None, **payload)

    async def _auth_for(self, session, batch: Batch) -> AuthContext | None:
        """Snapshot the owning key (and its tenant) as of NOW — key
        policy, budgets and tenant ceilings apply at execution time,
        not at batch-create time. None if the key is gone/inactive."""
        vkey = await session.get(VirtualKey, batch.key_id)
        if vkey is None or not vkey.is_active:
            return None
        tenant = None
        if vkey.tenant_id:
            tenant = await session.get(Tenant, vkey.tenant_id)
        # Last-used telemetry (tier 55): the batch's lines are about to
        # ride this key's policy, so the wave counts as use — same as a
        # live request would. Per-wave, not per-line: the ~60s throttle
        # makes the distinction moot for any real batch.
        await record_key_use(vkey)
        return auth_context_from_key(vkey, tenant, auth_method="batch")

    async def _execute_wave(self, session, batch: Batch) -> None:
        # Fresh auth per wave (see _auth_for); a missing key fails the
        # batch — there is no one left to bill.
        auth = await self._auth_for(session, batch)
        if auth is None:
            await session.execute(
                update(Batch)
                .where(Batch.id == batch.id, Batch.status == "in_progress")
                .values(
                    status="failed",
                    failed_at=_now(),
                    error_message=(
                        "The virtual key that owns this batch no longer "
                        "exists or is inactive."
                    ),
                )
            )
            await session.commit()
            await self._emit(
                "batch.failed",
                batch_id=batch.id,
                reason="owning key missing/inactive",
            )
            logger.warning("batch %s failed: owning key missing/inactive", batch.id)
            return

        file = await session.get(StoredFile, batch.input_file_id)
        if file is None:  # pragma: no cover — no file deletion exists
            await session.execute(
                update(Batch)
                .where(Batch.id == batch.id, Batch.status == "in_progress")
                .values(
                    status="failed",
                    failed_at=_now(),
                    error_message="The batch input file no longer exists.",
                )
            )
            await session.commit()
            return
        lines = file.content.splitlines()

        start = batch.next_line
        end = min(start + self.config.wave_size, batch.total_requests)
        results: list[BatchResult] = []
        completed = failed = 0
        defer_seconds: float | None = None
        stop_at = end

        for index in range(start, end):
            request_id = f"{batch.id}-L{index}"
            # -- parse + validate the line (validated at create; this
            #    only fires on DB corruption or a config change since).
            try:
                line = json.loads(lines[index])
                if not isinstance(line, dict):
                    raise ValueError("not a JSON object")
                custom_id = str(line.get("custom_id") or "")
                body = dict(line.get("body") or {})
                body.pop("stream", None)  # batches never stream
                request = ChatCompletionRequest.model_validate(body)
            except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
                message = (
                    _validation_summary(exc)
                    if isinstance(exc, ValidationError)
                    else str(exc)
                )
                err = GatewayError(
                    400,
                    f"Input line {index + 1} is invalid: {message}",
                    "invalid_request_error",
                    code="invalid_batch_line",
                )
                failed += 1
                results.append(
                    BatchResult(
                        batch_id=batch.id,
                        line_index=index,
                        custom_id=str(line.get("custom_id") or "")
                        if isinstance(line, dict)
                        else "",
                        status="failed",
                        response={
                            "status_code": 400,
                            "request_id": request_id,
                            "body": err.payload(),
                        },
                    )
                )
                continue

            # -- execute exactly like an interactive request
            try:
                response = await self.pipeline.handle_chat(
                    request, auth, request_id=request_id
                )
            except RateLimitError as exc:
                defer_seconds = exc.retry_after or _DEFER_FALLBACK_SECONDS
                stop_at = index
                break
            except BudgetExceededError:
                defer_seconds = _DEFER_BUDGET_SECONDS
                stop_at = index
                break
            except GatewayError as exc:
                # Everything the client would have received as an error
                # response: guardrail block, model not found, upstream
                # failure after retries, ...
                failed += 1
                results.append(
                    BatchResult(
                        batch_id=batch.id,
                        line_index=index,
                        custom_id=custom_id,
                        status="failed",
                        response={
                            "status_code": exc.status_code,
                            "request_id": request_id,
                            "body": exc.payload(),
                        },
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 — never kill the batch
                logger.exception(
                    "batch %s line %d: unexpected error", batch.id, index + 1
                )
                failed += 1
                err = GatewayError(
                    500,
                    f"The gateway encountered an unexpected error: {exc}",
                    "api_error",
                )
                results.append(
                    BatchResult(
                        batch_id=batch.id,
                        line_index=index,
                        custom_id=custom_id,
                        status="failed",
                        response={
                            "status_code": 500,
                            "request_id": request_id,
                            "body": err.payload(),
                        },
                    )
                )
                continue

            # -- a real response (2xx/4xx body, never a stream)
            try:
                body_out = json.loads(response.body)
            except (ValueError, TypeError):
                body_out = {
                    "raw": response.body.decode(errors="replace")
                    if isinstance(response.body, bytes)
                    else str(response.body)
                }
            ok = 200 <= response.status_code < 400
            if ok:
                completed += 1
            else:
                failed += 1
            results.append(
                BatchResult(
                    batch_id=batch.id,
                    line_index=index,
                    custom_id=custom_id,
                    status="succeeded" if ok else "failed",
                    response={
                        "status_code": response.status_code,
                        "request_id": request_id,
                        "body": body_out,
                    },
                )
            )

        # -- commit the wave: results + cursor in one transaction,
        #    guarded so a mid-wave cancel wins (its results are then
        #    simply never assembled into output files).
        advanced = await session.execute(
            update(Batch)
            .where(Batch.id == batch.id, Batch.status == "in_progress")
            .values(
                next_line=stop_at,
                completed_lines=Batch.completed_lines + completed,
                failed_lines=Batch.failed_lines + failed,
                retry_not_before=(
                    _now() + timedelta(seconds=defer_seconds)
                    if defer_seconds is not None
                    else None
                ),
            )
        )
        if results:
            session.add_all(results)
        await session.commit()
        if completed:
            metrics.BATCH_LINES.labels(status="completed").inc(completed)
        if failed:
            metrics.BATCH_LINES.labels(status="failed").inc(failed)

        if defer_seconds is not None:
            metrics.BATCH_LINES.labels(status="deferred").inc()
            await self._emit(
                "batch.deferred",
                batch_id=batch.id,
                retry_seconds=defer_seconds,
                lines_done=stop_at,
                lines_total=batch.total_requests,
            )
            logger.info(
                "batch %s deferred %.0fs (rate limit / budget); %d/%d lines done",
                batch.id,
                defer_seconds,
                stop_at,
                batch.total_requests,
            )
            return
        if (advanced.rowcount or 0) == 0:
            return  # cancelled mid-wave
        if stop_at >= batch.total_requests:
            await self._mark_finalizing(session, batch)
            await self._finalize(session, batch)

    # -- finalization ----------------------------------------------------

    async def _mark_finalizing(self, session, batch: Batch) -> None:
        await session.execute(
            update(Batch)
            .where(Batch.id == batch.id, Batch.status == "in_progress")
            .values(status="finalizing", finalizing_at=_now())
        )
        await session.commit()

    async def _finalize(self, session, batch: Batch) -> None:
        """Assemble the stored per-line results into the OpenAI-shaped
        output JSONL (succeeded lines) and error JSONL (failed lines),
        then mark the batch completed — all in one transaction, guarded
        so a cancel that raced us is never overwritten."""
        rows = (
            (
                await session.execute(
                    select(BatchResult)
                    .where(BatchResult.batch_id == batch.id)
                    .order_by(BatchResult.line_index)
                )
            )
            .scalars()
            .all()
        )
        output_lines: list[str] = []
        error_lines: list[str] = []
        for r in rows:
            response = r.response or {}
            entry = json.dumps(
                {
                    "id": response.get("request_id") or f"{batch.id}-L{r.line_index}",
                    "custom_id": r.custom_id,
                    "response": response,
                    "error": None,
                }
            )
            (output_lines if r.status == "succeeded" else error_lines).append(entry)

        output_text = "\n".join(output_lines) + ("\n" if output_lines else "")
        error_text = "\n".join(error_lines) + ("\n" if error_lines else "")
        output_file = StoredFile(
            key_id=batch.key_id,
            tenant_id=batch.tenant_id,
            filename=f"batch_{batch.id}_output.jsonl",
            purpose="batch_output",
            bytes=len(output_text.encode()),
            content=output_text,
        )
        error_file = (
            StoredFile(
                key_id=batch.key_id,
                tenant_id=batch.tenant_id,
                filename=f"batch_{batch.id}_errors.jsonl",
                purpose="batch_errors",
                bytes=len(error_text.encode()),
                content=error_text,
            )
            if error_lines
            else None
        )
        session.add(output_file)
        if error_file is not None:
            session.add(error_file)
        await session.flush()  # ids before the guarded update

        finished = await session.execute(
            update(Batch)
            .where(Batch.id == batch.id, Batch.status == "finalizing")
            .values(
                status="completed",
                completed_at=_now(),
                output_file_id=output_file.id,
                error_file_id=error_file.id if error_file is not None else None,
            )
        )
        if (finished.rowcount or 0) == 0:
            # Cancelled while we were building — discard the files.
            await session.rollback()
            return
        await session.commit()
        logger.info(
            "batch %s completed: %d succeeded, %d failed",
            batch.id,
            len(output_lines),
            len(error_lines),
        )
        await self._emit(
            "batch.completed",
            batch_id=batch.id,
            key_id=batch.key_id,
            succeeded=len(output_lines),
            failed=len(error_lines),
            total=batch.total_requests,
            output_file_id=output_file.id,
            error_file_id=error_file.id if error_file is not None else None,
        )
