"""Request logging + spend accounting (SQLite)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, delete, func, select, update

from ..db import get_sessionmaker
from ..models import RequestLog, Tenant, VirtualKey
from ..observability.metrics import LOGS_DROPPED, LOGS_PRUNED
from ..utils import daily_bucket, monthly_bucket, truncate
from .context import RequestContext

if TYPE_CHECKING:  # pragma: no cover
    from .alerts import BudgetAlertManager

logger = logging.getLogger("gateway.logging")


class LoggingService:
    def __init__(
        self,
        max_body_chars: int = 20000,
        alerts: "BudgetAlertManager | None" = None,
        store_bodies: bool = True,
        retention_days: float | None = None,
    ) -> None:
        self.max_body_chars = max_body_chars
        self.alerts = alerts
        # PII-safe mode: bodies are not persisted (see LoggingConfig).
        self.store_bodies = store_bodies
        self.retention_days = retention_days

    def _truncate_payload(self, payload: dict | None) -> dict | None:
        if payload is None:
            return None
        text = json.dumps(payload, default=str)
        if len(text) <= self.max_body_chars:
            return payload
        return {"_truncated": True, "preview": truncate(text, self.max_body_chars)}

    async def write_request_log(self, ctx: RequestContext) -> None:
        """Persist one request log row from the pipeline context.

        Awaited in the response path by default (a returned response
        implies its row is queryable); ``QueuedLoggingService`` moves the
        write behind a bounded background queue when
        ``logging.background_write`` is set.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            log = RequestLog(
                request_id=ctx.request_id,
                key_name=ctx.auth.key_name,
                tenant_id=ctx.auth.tenant_id,
                model=ctx.model,
                provider=ctx.provider,
                deployment=ctx.deployment,
                status=ctx.status,
                error_message=truncate(ctx.error_message or "", 2000) or None,
                guardrail_actions=ctx.guardrail_actions or None,
                request_body=(
                    self._truncate_payload(ctx.request_body) if self.store_bodies else None
                ),
                response_body=(
                    self._truncate_payload(ctx.response_body) if self.store_bodies else None
                ),
                stream=ctx.stream,
                prompt_tokens=ctx.prompt_tokens,
                completion_tokens=ctx.completion_tokens,
                total_tokens=(
                    (ctx.prompt_tokens or 0) + (ctx.completion_tokens or 0)
                    if ctx.prompt_tokens is not None or ctx.completion_tokens is not None
                    else None
                ),
                cost_usd=ctx.cost_usd,
                latency_ms=ctx.latency_ms(),
                cache_hit=ctx.cache_hit,
                shadow=ctx.shadow,
                prompt_variant=ctx.prompt_variant,
                trace_id=ctx.trace_id,
                span_id=ctx.span_id,
                auth_method=ctx.auth.auth_method,
            )
            session.add(log)
            await session.commit()

    async def prune(self, now: datetime | None = None) -> int:
        """Delete request log rows older than ``retention_days``.

        Returns the number of rows removed (0 when retention is unset).
        ``now`` is injectable so tests can decide what "old" means.
        """
        if self.retention_days is None:
            return 0
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.retention_days)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                delete(RequestLog).where(RequestLog.created_at < cutoff)
            )
            await session.commit()
        removed = result.rowcount or 0
        if removed:
            LOGS_PRUNED.inc(removed)
        return removed

    async def add_spend(
        self, key_id: str | None, cost_usd: float, tenant_id: str | None = None
    ) -> None:
        """Accumulate spend on a virtual key and its tenant (best-effort).

        The UPDATE ... RETURNING gives the post-update totals atomically,
        which is what budget alerts evaluate against — concurrent requests
        each see their own consistent snapshot. Alert delivery (if a
        threshold is newly crossed) happens here too: this method runs in
        the pipeline's background task, so webhooks never block requests.

        The same statement also rolls the daily/monthly budget windows: a
        CASE on the stored bucket id either accumulates (same window) or
        replaces (the window has rolled since the last write — the old
        spend becomes this request's cost). One statement, so concurrent
        requests can never straddle a window boundary incorrectly.

        When the key belongs to a tenant, the identical update runs on
        the tenant row (tenant budgets pool every member key's spend) and
        tenant thresholds alert the same way. The two updates are
        separate transactions: a tenant write failing must not lose the
        key's accounting.
        """
        if not key_id or cost_usd <= 0:
            return
        now = datetime.now(timezone.utc)
        day, month = daily_bucket(now), monthly_bucket(now)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                update(VirtualKey)
                .where(VirtualKey.id == key_id)
                .values(
                    spend_usd=VirtualKey.spend_usd + cost_usd,
                    daily_spend_usd=case(
                        (
                            VirtualKey.daily_bucket == day,
                            func.coalesce(VirtualKey.daily_spend_usd, 0.0) + cost_usd,
                        ),
                        else_=cost_usd,
                    ),
                    daily_bucket=day,
                    monthly_spend_usd=case(
                        (
                            VirtualKey.monthly_bucket == month,
                            func.coalesce(VirtualKey.monthly_spend_usd, 0.0) + cost_usd,
                        ),
                        else_=cost_usd,
                    ),
                    monthly_bucket=month,
                )
                .returning(
                    VirtualKey.name,
                    VirtualKey.spend_usd,
                    VirtualKey.max_budget_usd,
                    VirtualKey.budget_alerts_fired,
                )
            )
            row = result.first()
            await session.commit()
        if row is None:
            return
        if self.alerts is not None:
            try:
                await self.alerts.evaluate(
                    key_id,
                    row.name,
                    row.spend_usd,
                    row.max_budget_usd,
                    row.budget_alerts_fired,
                )
            except Exception:  # noqa: BLE001
                logger.exception("budget alert evaluation failed for key %s", key_id)
        if tenant_id:
            await self._add_tenant_spend(tenant_id, cost_usd, now, day, month)

    async def _add_tenant_spend(
        self,
        tenant_id: str,
        cost_usd: float,
        now: datetime,
        day: str,
        month: str,
    ) -> None:
        """The tenant half of add_spend: pooled spend + tenant alerts.

        Same window-rolling UPDATE ... RETURNING, on the tenant row; a
        failed tenant write never loses the key's accounting (already
        committed above).
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                update(Tenant)
                .where(Tenant.id == tenant_id)
                .values(
                    spend_usd=Tenant.spend_usd + cost_usd,
                    daily_spend_usd=case(
                        (
                            Tenant.daily_bucket == day,
                            func.coalesce(Tenant.daily_spend_usd, 0.0) + cost_usd,
                        ),
                        else_=cost_usd,
                    ),
                    daily_bucket=day,
                    monthly_spend_usd=case(
                        (
                            Tenant.monthly_bucket == month,
                            func.coalesce(Tenant.monthly_spend_usd, 0.0) + cost_usd,
                        ),
                        else_=cost_usd,
                    ),
                    monthly_bucket=month,
                )
                .returning(
                    Tenant.name,
                    Tenant.spend_usd,
                    Tenant.max_budget_usd,
                    Tenant.budget_alerts_fired,
                )
            )
            trow = result.first()
            await session.commit()
        if trow is None or self.alerts is None:
            return
        try:
            await self.alerts.evaluate(
                tenant_id,
                trow.name,
                trow.spend_usd,
                trow.max_budget_usd,
                trow.budget_alerts_fired,
                kind="tenant",
            )
        except Exception:  # noqa: BLE001
            logger.exception("budget alert evaluation failed for tenant %s", tenant_id)


async def get_key_by_hash(key_hash: str) -> VirtualKey | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(VirtualKey).where(VirtualKey.key_hash == key_hash)
        )
        return result.scalar_one_or_none()


async def get_tenant_by_id(tenant_id: str) -> Tenant | None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return await session.get(Tenant, tenant_id)


class _Stop:
    """Sentinel telling the queue consumer to drain and exit."""


class QueuedLoggingService(LoggingService):
    """``LoggingService`` with request-log writes behind a bounded queue.

    ``logging.background_write`` moves the row INSERT — the last DB round
    trip in the response path — off the hot path: ``write_request_log``
    only enqueues (never blocks), and a lazily started consumer task
    performs the writes. A full queue drops the row (counted on
    ``gateway_logs_dropped_total``) rather than blocking or failing the
    request; the trade is that a returned response no longer implies its
    log row is queryable (typically a milliseconds-long gap).

    The consumer starts on the first write, so no lifespan wiring is
    needed to begin; ``stop()`` (called from the lifespan teardown)
    drains everything pending before the engine is disposed.
    """

    def __init__(self, *args, queue_size: int = 1000, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, queue_size))
        self._consumer: asyncio.Task | None = None

    async def write_request_log(self, ctx: RequestContext) -> None:
        if self._consumer is None:
            # Lazy start: no lifespan required (tests run without one).
            self._consumer = asyncio.create_task(self._consume())
        try:
            self._queue.put_nowait(ctx)
        except asyncio.QueueFull:
            LOGS_DROPPED.inc()
            logger.warning(
                "background log queue full (size %s) — dropping log row for %s",
                self._queue.maxsize,
                ctx.request_id,
            )

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            if isinstance(item, _Stop):
                return
            try:
                await super().write_request_log(item)
            except Exception:  # noqa: BLE001 — one bad row must not kill the loop
                logger.exception("background request log write failed")

    async def stop(self) -> None:
        """Drain pending writes and stop the consumer (shutdown path)."""
        if self._consumer is None:
            return
        await self._queue.put(_Stop())  # waits for space; consumer drains ahead
        try:
            await self._consumer
        finally:
            self._consumer = None

    @property
    def pending(self) -> int:
        """Rows queued but not yet written (observable in tests/metrics)."""
        return self._queue.qsize()
