"""Budget alerting: webhooks when a key's spend crosses budget thresholds.

Thresholds are percentages of the key's ``max_budget_usd`` (default
50/80/90/100). Each level fires once per key: the fired levels persist on
the key row (``budget_alerts_fired``), so alerts survive restarts and apply
across workers. Raising a key's budget re-arms thresholds — levels already
crossed are marked fired and only future crossings alert.

Delivery is best-effort and never blocks a request: evaluation happens in
the pipeline's background spend task. Failures are logged and counted on
``gateway_alerts_total{kind,outcome}``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update

from ..config import AlertsConfig
from ..db import get_sessionmaker
from ..models import Tenant, VirtualKey
from ..observability import metrics

logger = logging.getLogger("gateway.alerts")


def crossed_levels(
    spend_usd: float,
    budget_usd: float | None,
    thresholds: list[int],
    already_fired: list[int] | None,
) -> list[int]:
    """Thresholds newly crossed by ``spend_usd``, highest-first.

    Pure function (unit-tested directly). A threshold counts as crossed at
    >= its percentage of the budget; the 100% level means "exhausted".
    """
    if not budget_usd or budget_usd <= 0:
        return []
    fired = set(already_fired or [])
    percent = spend_usd / budget_usd * 100.0
    return sorted(
        (t for t in thresholds if percent >= t and t not in fired),
        reverse=True,
    )


class BudgetAlertManager:
    """Evaluates budget crossings and delivers webhook alerts."""

    # Bounded retries for the fired-levels compare-and-swap: more than
    # any realistic number of concurrent evaluations of one entity.
    # Alerting is best-effort, so exhaustion only logs.
    _CAS_ATTEMPTS = 5

    def __init__(
        self,
        config: AlertsConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._owns_client = client is None
        # Dedicated client (not the instrumented app one): alert POSTs
        # should not appear as spans in request traces.
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- evaluation --------------------------------------------------------

    async def evaluate(
        self,
        key_id: str,
        key_name: str | None,
        spend_usd: float,
        budget_usd: float,
        fired: list[int] | None,
        kind: str = "key",
    ) -> list[int]:
        """Fire webhooks for thresholds newly crossed by this spend.

        ``kind`` selects what is being budgeted — "key" (a virtual key)
        or "tenant" (the pooled spend of every member key); the fired
        levels persist on the matching table and the webhook payload
        says which. ``key_id``/``key_name`` are the budgeted entity's
        (a tenant passes its id, prefixed "tenant:" for uniqueness in
        payloads that flatten both kinds).

        ``fired`` is the caller's snapshot and may already be stale:
        two requests crossing thresholds concurrently each see the
        other's spend but not its alert write (both snapshots predate
        the other's commit). The persisted update is therefore a
        compare-and-swap on ``budget_alerts_fired`` — a writer whose
        snapshot lost re-reads the winner's state and recomputes, so
        every level delivers exactly once and the persisted set never
        loses the winner's levels.

        Levels persist first (so a crash during delivery prefers
        duplicate silence over duplicate noise), then deliver. Returns
        the levels fired.
        """
        table = VirtualKey if kind == "key" else Tenant
        sessionmaker = get_sessionmaker()
        # The payload names the budgeted entity; tenants get a
        # "tenant:" prefix so flattened consumers can tell kinds apart
        # (the row update below uses the raw id).
        payload_id = f"tenant:{key_id}" if kind == "tenant" else key_id
        for _ in range(self._CAS_ATTEMPTS):
            levels = crossed_levels(
                spend_usd, budget_usd, self.config.budget_thresholds, fired
            )
            if not levels:
                return []
            new_fired = sorted(set(fired or []) | set(levels))
            async with sessionmaker() as session:
                result = await session.execute(
                    update(table)
                    .where(table.id == key_id)
                    .where(table.budget_alerts_fired == fired)
                    .values(budget_alerts_fired=new_fired)
                )
                await session.commit()
            if (result.rowcount or 0) == 1:
                for level in levels:
                    await self._deliver_budget_alert(
                        payload_id, key_name, spend_usd, budget_usd, level, kind=kind
                    )
                return levels
            # Lost the race: another evaluation already advanced the
            # fired set. Retry against its state — only what it missed
            # fires now.
            async with sessionmaker() as session:
                fired = (
                    await session.execute(
                        select(table.budget_alerts_fired).where(table.id == key_id)
                    )
                ).scalar_one_or_none()
        logger.warning(
            "budget alert state kept moving for %s %s after %d attempts",
            kind,
            key_id,
            self._CAS_ATTEMPTS,
        )
        return []

    # -- delivery -----------------------------------------------------------

    def _budget_payload(
        self,
        key_id: str,
        key_name: str | None,
        spend_usd: float,
        budget_usd: float,
        level: int,
        kind: str = "key",
    ) -> dict:
        percent = spend_usd / budget_usd * 100.0 if budget_usd else 0.0
        what = "tenant" if kind == "tenant" else "virtual key"
        if level >= 100:
            message = (
                f"{what.capitalize()} '{key_name or key_id}' has exhausted its "
                f"${budget_usd:.2f} budget (${spend_usd:.2f} spent)."
            )
        else:
            message = (
                f"{what.capitalize()} '{key_name or key_id}' has spent "
                f"${spend_usd:.2f} of its ${budget_usd:.2f} budget "
                f"({percent:.1f}%)."
            )
        return {
            "type": "budget_alert",
            "kind": kind,
            "level_percent": level,
            "key_id": key_id,
            "key_name": key_name,
            "spend_usd": round(spend_usd, 8),
            "budget_usd": budget_usd,
            "percent": round(percent, 2),
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def _deliver_budget_alert(
        self,
        key_id: str,
        key_name: str | None,
        spend_usd: float,
        budget_usd: float,
        level: int,
        kind: str = "key",
    ) -> None:
        payload = self._budget_payload(
            key_id, key_name, spend_usd, budget_usd, level, kind=kind
        )
        await self._deliver(payload)

    async def _deliver(self, payload: dict) -> bool:
        """POST the payload; retry once; metric + log on failure."""
        url = self.config.resolve_url()
        if not self.config.enabled or not url:
            # No webhook configured: still count the alert that *would*
            # have fired, so ops can spot silent thresholds in /metrics.
            metrics.ALERTS.labels(kind="budget", outcome="skipped").inc()
            return False
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.post(
                    url,
                    json=payload,
                    timeout=httpx.Timeout(self.config.timeout_seconds),
                )
                if response.status_code < 400:
                    metrics.ALERTS.labels(kind="budget", outcome="sent").inc()
                    return True
                logger.warning(
                    "budget alert webhook returned %s for key %s (attempt %s)",
                    response.status_code,
                    payload.get("key_id"),
                    attempt,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "budget alert webhook failed for key %s (attempt %s): %s",
                    payload.get("key_id"),
                    attempt,
                    exc,
                )
            if attempt >= max(1, self.config.max_attempts):
                metrics.ALERTS.labels(kind="budget", outcome="failed").inc()
                return False
            await asyncio.sleep(0.5)

    # -- admin helpers --------------------------------------------------------

    def rearm_levels(self, spend_usd: float, budget_usd: float | None) -> list[int]:
        """Fired-levels state after a budget change: everything already
        crossed is marked fired, so only future crossings alert."""
        if budget_usd is None or budget_usd <= 0:
            return []
        percent = spend_usd / budget_usd * 100.0
        return sorted(
            t for t in self.config.budget_thresholds if percent >= t
        )
