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
from sqlalchemy import update

from ..config import AlertsConfig
from ..db import get_sessionmaker
from ..models import VirtualKey
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
    ) -> list[int]:
        """Fire webhooks for thresholds newly crossed by this spend.

        Persists the updated fired-levels on the key row first (so a crash
        during delivery prefers duplicate silence over duplicate noise),
        then delivers each alert. Returns the levels fired.
        """
        levels = crossed_levels(
            spend_usd, budget_usd, self.config.budget_thresholds, fired
        )
        if not levels:
            return []
        new_fired = sorted(set(fired or []) | set(levels))
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            await session.execute(
                update(VirtualKey)
                .where(VirtualKey.id == key_id)
                .values(budget_alerts_fired=new_fired)
            )
            await session.commit()
        for level in levels:
            await self._deliver_budget_alert(
                key_id, key_name, spend_usd, budget_usd, level
            )
        return levels

    # -- delivery -----------------------------------------------------------

    def _budget_payload(
        self,
        key_id: str,
        key_name: str | None,
        spend_usd: float,
        budget_usd: float,
        level: int,
    ) -> dict:
        percent = spend_usd / budget_usd * 100.0 if budget_usd else 0.0
        if level >= 100:
            message = (
                f"Virtual key '{key_name or key_id}' has exhausted its "
                f"${budget_usd:.2f} budget (${spend_usd:.2f} spent)."
            )
        else:
            message = (
                f"Virtual key '{key_name or key_id}' has spent "
                f"${spend_usd:.2f} of its ${budget_usd:.2f} budget "
                f"({percent:.1f}%)."
            )
        return {
            "type": "budget_alert",
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
    ) -> None:
        payload = self._budget_payload(key_id, key_name, spend_usd, budget_usd, level)
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
