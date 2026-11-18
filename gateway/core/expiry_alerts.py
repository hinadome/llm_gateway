"""Key-expiry warnings (tier 52): the deadline's own pages.

``expires_at`` is enforced at both auth steps — a key past its deadline
answers 401 "This API key has expired." — but nothing warned *before*
the moment. The operator's first signal was the user complaint. This
module closes that gap with the budget-alert machinery, deadline
edition: a background sweep (the canary-loop shape — always started,
re-reading ``alerts:`` each pass; tests drive ``evaluate_once()``
directly, pinning the sweep's clock, because the lifespan loop never
runs under pytest) walks every active key with an ``expires_at`` and
fires once per day-level as the deadline approaches — ``alerts.
expiry_days`` (default 14/7/1/0; 0 is the page that the key just
died, the one the 401 already gives callers but the operator never
saw coming).

The rules borrow deliberately, and are the budget thresholds' mirror:

* **Fire-once per level, persisted on the key row**
  (``expiry_alerts_fired``, beside ``budget_alerts_fired``): a restart
  never re-pages, and state persists *before* delivery — a crash
  mid-sweep prefers duplicate silence over duplicate noise (the
  budget rule).
* **Setting or moving a deadline arms silently.** A key created with
  6 days to live does not page "expires in 14 days" seconds after the
  operator chose that deadline: at create and at PATCH, every level
  already crossed is marked fired, exactly the way raising a budget
  re-arms below-current thresholds. Only *future* crossings alert —
  which is also why moving a deadline IN never pages, and moving it
  OUT honestly re-arms the levels it now sits above.
* **Discovery is honest.** A key that predates the feature (fired
  state NULL) is discovered by the first sweep with its already-
  crossed levels firing — "you have a key expiring in 3 days" is
  exactly the page this module exists for. Rotation (tier 51) never
  touches the state: a deadline is a deadline whichever credential
  presents it.
* **Revoked keys never warn** (``is_active`` gates the sweep) — a
  dead key being dead is not news; keys with no deadline never warn
  (there is nothing to warn about). And the sweep skips nothing
  else: an expiring key with zero traffic still pages, which is the
  whole point — spend-triggered alerts cannot see a quiet key.

Delivery is best-effort and blocks nothing: the same webhook and
``gateway_alerts_total`` metric family as the budget alerts
(``kind="expiry"``, ``outcome=sent|failed|skipped``), one retry, and
without a webhook URL the POST is a no-op counted ``skipped`` while
the ``expiry.threshold`` observer events still fire (key, key_name,
level_days, days_remaining, expires_at). ``alerts.enabled: false``
stops the sweep entirely.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select, update

from ..config import AlertsConfig
from ..db import get_sessionmaker
from ..models import VirtualKey
from ..observability import metrics

if TYPE_CHECKING:  # pragma: no cover — import for typing only
    from .events import EventBus

logger = logging.getLogger("gateway.expiry_alerts")

_SECS_PER_DAY = 86400.0


def days_remaining(now: datetime, expires_at: datetime) -> float:
    """Fractional days between ``now`` and the deadline (negative past
    it). A naive ``expires_at`` (SQLite hands stored datetimes back
    naive) normalizes to UTC — the ``_is_expired`` rule."""
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return (expires_at - now).total_seconds() / _SECS_PER_DAY


def crossed_levels(
    now: datetime,
    expires_at: datetime,
    thresholds: list[int],
    already_fired: list[int] | None,
) -> list[int]:
    """Day-levels newly crossed — remaining days <= the level —
    highest first, so the 14-day warning never arrives after the
    7-day one. Pure function (unit-tested directly)."""
    fired = set(already_fired or [])
    remaining = days_remaining(now, expires_at)
    return sorted(
        (t for t in thresholds if remaining <= t and t not in fired),
        reverse=True,
    )


def rearm_levels(
    now: datetime,
    expires_at: datetime | None,
    thresholds: list[int],
) -> list[int]:
    """Fired state after the deadline was set or moved (create/PATCH):
    every level already crossed is marked fired, so only future
    crossings alert — the budget rule for deadlines. ``None`` (no
    deadline) has nothing to track. A deadline moved OUT re-arms the
    levels it now sits above; one moved IN never pages."""
    if expires_at is None:
        return []
    remaining = days_remaining(now, expires_at)
    return sorted(t for t in thresholds if remaining <= t)


class ExpiryAlertManager:
    """Sweeps active keys with a deadline and delivers the warnings
    the deadline itself never sends."""

    def __init__(
        self,
        config: AlertsConfig,
        client: httpx.AsyncClient | None = None,
        events: EventBus | None = None,
    ) -> None:
        self.config = config
        # Lifecycle-event bus (optional — tests may construct without
        # one): level crossings surface as `expiry.threshold` events
        # in addition to the webhook delivery.
        self.events = events
        # Dedicated client (not the instrumented app one): alert POSTs
        # should not appear as spans in request traces. Shared with the
        # budget alert manager when main.py injects one (this manager
        # then owns nothing).
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- the sweep ---------------------------------------------------------

    async def evaluate_once(self, now: datetime | None = None) -> int:
        """One pass over every active key with an ``expires_at``:
        fire the day-levels newly crossed, persisting each key's fired
        state before its deliveries. Returns the number of levels
        fired (not keys). The caller is the single writer (the
        lifespan loop; tests drive this directly with a pinned
        clock), so the state update needs no compare-and-swap — unlike
        the spend-threshold alerts, which fire from request paths."""
        if now is None:
            now = datetime.now(timezone.utc)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(VirtualKey).where(
                    VirtualKey.is_active,
                    VirtualKey.expires_at.isnot(None),
                )
            )
            keys = result.scalars().all()
        fired_count = 0
        for vkey in keys:
            levels = crossed_levels(
                now, vkey.expires_at, self.config.expiry_days,
                vkey.expiry_alerts_fired,
            )
            if not levels:
                continue
            # Persist first (duplicate silence over duplicate noise),
            # then deliver — the budget rule.
            new_fired = sorted(set(vkey.expiry_alerts_fired or []) | set(levels))
            async with sessionmaker() as session:
                await session.execute(
                    update(VirtualKey)
                    .where(VirtualKey.id == vkey.id)
                    .values(expiry_alerts_fired=new_fired)
                )
                await session.commit()
            for level in levels:
                remaining = days_remaining(now, vkey.expires_at)
                await self._deliver_expiry_alert(
                    vkey.id, vkey.name, level, vkey.expires_at, remaining
                )
                if self.events is not None:
                    await self.events.emit(
                        "expiry.threshold",
                        None,
                        key=vkey.id,
                        key_name=vkey.name,
                        level_days=level,
                        days_remaining=round(remaining, 3),
                        expires_at=vkey.expires_at.isoformat(),
                    )
                fired_count += 1
        return fired_count

    # -- arming (admin API hooks) -------------------------------------------

    def rearm(self, now: datetime | None, expires_at: datetime | None) -> list[int]:
        """The fired state for a key whose deadline was just set or
        moved — the create/PATCH hook (see module docstring for the
        arming rules)."""
        if now is None:
            now = datetime.now(timezone.utc)
        return rearm_levels(now, expires_at, self.config.expiry_days)

    # -- delivery -----------------------------------------------------------

    def _expiry_payload(
        self,
        key_id: str,
        key_name: str | None,
        level: int,
        expires_at: datetime,
        remaining: float,
        now: datetime,
    ) -> dict:
        if level <= 0:
            message = (
                f"Virtual key '{key_name or key_id}' has expired — its "
                f"credential stopped authenticating at "
                f"{expires_at.isoformat()}."
            )
        else:
            message = (
                f"Virtual key '{key_name or key_id}' has {remaining:.1f} "
                f"days left before it expires (crossed the {level}-day "
                f"warning)."
            )
        return {
            "type": "expiry_alert",
            "level_days": level,
            "key_id": key_id,
            "key_name": key_name,
            "expires_at": expires_at.isoformat(),
            "days_remaining": round(remaining, 3),
            "message": message,
            "timestamp": now.isoformat(),
        }

    async def _deliver_expiry_alert(
        self,
        key_id: str,
        key_name: str | None,
        level: int,
        expires_at: datetime,
        remaining: float,
        now: datetime | None = None,
    ) -> None:
        if now is None:
            now = datetime.now(timezone.utc)
        payload = self._expiry_payload(
            key_id, key_name, level, expires_at, remaining, now
        )
        await self._deliver(payload)

    async def _deliver(self, payload: dict) -> bool:
        """POST the payload; retry once; metric + log on failure —
        the budget alerts' delivery leg, kind swapped."""
        url = self.config.resolve_url()
        if not self.config.enabled or not url:
            # No webhook configured: still count the alert that *would*
            # have fired, so ops can spot silent deadlines in /metrics.
            metrics.ALERTS.labels(kind="expiry", outcome="skipped").inc()
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
                    metrics.ALERTS.labels(kind="expiry", outcome="sent").inc()
                    return True
                logger.warning(
                    "expiry alert webhook returned %s for key %s (attempt %s)",
                    response.status_code,
                    payload.get("key_id"),
                    attempt,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "expiry alert webhook failed for key %s (attempt %s): %s",
                    payload.get("key_id"),
                    attempt,
                    exc,
                )
            if attempt >= max(1, self.config.max_attempts):
                metrics.ALERTS.labels(kind="expiry", outcome="failed").inc()
                return False
            await asyncio.sleep(0.5)
