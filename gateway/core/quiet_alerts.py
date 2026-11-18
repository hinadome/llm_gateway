"""Quiet-key alerts (tier 56): the liveness edition of the alert sweeps.

Tiers 52 and 55 gave the key lifecycle its halves: the deadline's pages
(``expiry.threshold`` as ``expires_at`` approaches) and the liveness
signal itself (``last_used_at``, stamped at auth). What was still
missing is the page that SPEND alerts structurally cannot give — the
churn/broken-integration signal. A key that was alive and stopped
authenticating costs nothing, alerts nothing, and pages nothing: the
integration died on the caller's side and the gateway is the only
party that can see it.

This module is the expiry sweep's sibling, re-reading ``alerts:``
each pass (the canary-loop shape — always started; tests drive
``evaluate_once()`` directly with a pinned clock because the lifespan
loop never runs under pytest). A key fires a day-level when it WAS
alive (it has a ``last_used_at`` — never-used keys were never alive;
that finding belongs to the ``idle_days`` listing filter) and has now
been idle for ``alerts.quiet_days`` days (default **7/30**: 7 is the
early page — a cron stopped, an integration is stuck; 30 the churn
page — rotate or revoke it).

The rules, and where they deliberately differ from expiry's:

* **Fire-once per level per quiet period, persisted on the key row**
  (``quiet_alerts_fired``, beside the budget and expiry levels):
  a restart never re-pages, and state persists *before* delivery —
  duplicate silence over duplicate noise (the budget rule).
* **Lowest level first**, the mirror of expiry's highest-first: a
  deadline counts DOWN to one moment, so its most distant warning
  fires first; a quiet period counts UP from an event, so its story
  must arrive in order — a key the sweep discovers already 40 days
  quiet pages 7-then-30, never 30 alone, never merged. Persisted
  state sorts ascending either way (the budget convention).
* **Resumed use re-arms silently ON THE SWEEP.** A key that
  authenticates again is alive; its quiet period ended, and the next
  pass clears the fired state (idle < every level). So one row pages
  once per quiet period, forever — with the whole decision inside
  the alert layer: ``record_key_use`` (tier 55) stays pure telemetry
  and never touches alert state, the same separation expiry keeps
  from rotation.
* **No arming hook at create/PATCH** — the mirror-image difference.
  Expiry arms silently on set/move because a deadline is the
  operator's own choice; quiet-key state is earned by traffic, not
  chosen, so the first use simply starts the clock and the sweep
  decides everything from there.
* **Revoked keys never warn** (``is_active`` gates the sweep) — a
  dead key being dead is not news. Rotation (tier 51) never touches
  the state: ``last_used_at`` belongs to the row, whichever
  credential presents it.
* **Discovery is honest**: a key quiet 40 days at the feature's first
  sweep (or after the clock time-traveled) fires every crossed level
  — "you have a key nobody has used in over a month" is exactly the
  page this module exists for.

Delivery is best-effort and blocks nothing: the same webhook and
``gateway_alerts_total`` metric family as the budget and expiry
alerts (``kind="quiet"``, ``outcome=sent|failed|skipped``), one
retry, and without a webhook URL the POST is a no-op counted
``skipped`` while the ``quiet.threshold`` observer events still fire
(key, key_name, level_days, idle_days, last_used_at). An empty
``quiet_days`` list stops this sweep alone; ``alerts.enabled: false``
stops every alert family.
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

logger = logging.getLogger("gateway.quiet_alerts")

_SECS_PER_DAY = 86400.0


def idle_days_count(now: datetime, last_used_at: datetime) -> float:
    """Fractional days since the key's last use. A naive
    ``last_used_at`` (SQLite hands stored datetimes back naive)
    normalizes to UTC — the ``_is_expired`` rule."""
    if last_used_at.tzinfo is None:
        last_used_at = last_used_at.replace(tzinfo=timezone.utc)
    return (now - last_used_at).total_seconds() / _SECS_PER_DAY


def crossed_levels(
    now: datetime,
    last_used_at: datetime,
    thresholds: list[int],
    already_fired: list[int] | None,
) -> list[int]:
    """Day-levels newly crossed — idle for at least the level —
    **lowest first**, so the 7-day page never arrives after the
    30-day one: a quiet period unfolds in order, unlike a deadline
    (which counts down, expiry's highest-first). Pure function
    (unit-tested directly)."""
    fired = set(already_fired or [])
    idle = idle_days_count(now, last_used_at)
    return sorted(t for t in thresholds if idle >= t and t not in fired)


def should_rearm(idle: float, thresholds: list[int], already_fired: list[int] | None) -> bool:
    """The quiet period ended: the key was used recently enough that it
    is below every level, yet fired state lingers from a previous
    period. The sweep clears it (the re-arm) — silently, inside the
    alert layer; ``record_key_use`` never touches alert state."""
    if not already_fired or not thresholds:
        return False
    return idle < min(thresholds)


class QuietKeyAlertManager:
    """Sweeps keys that were alive and went quiet, and delivers the
    pages their silence never sends."""

    def __init__(
        self,
        config: AlertsConfig,
        client: httpx.AsyncClient | None = None,
        events: EventBus | None = None,
    ) -> None:
        self.config = config
        # Lifecycle-event bus (optional — tests may construct without
        # one): level crossings surface as `quiet.threshold` events in
        # addition to the webhook delivery.
        self.events = events
        # Dedicated client (not the instrumented app one): alert POSTs
        # should not appear as spans in request traces. Shared with the
        # budget/expiry alert managers when main.py injects one (this
        # manager then owns nothing).
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- the sweep ---------------------------------------------------------

    async def evaluate_once(self, now: datetime | None = None) -> int:
        """One pass over every active key with a ``last_used_at``: fire
        the day-levels newly crossed (persisting each key's fired
        state before its deliveries), and clear the fired state of keys
        whose quiet period ended (idle below every level — resumed
        use). Returns the number of levels fired (not keys). The caller
        is the single writer (the lifespan loop; tests drive this
        directly with a pinned clock), so the state update needs no
        compare-and-swap."""
        if now is None:
            now = datetime.now(timezone.utc)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(VirtualKey).where(
                    VirtualKey.is_active,
                    VirtualKey.last_used_at.isnot(None),
                )
            )
            keys = result.scalars().all()
        fired_count = 0
        for vkey in keys:
            idle = idle_days_count(now, vkey.last_used_at)
            if should_rearm(idle, self.config.quiet_days, vkey.quiet_alerts_fired):
                # Resumed use: the quiet period ended, the row may page
                # again in its next one. No webhook for a recovery —
                # resumed use is not news, it is the happy path.
                async with sessionmaker() as session:
                    await session.execute(
                        update(VirtualKey)
                        .where(VirtualKey.id == vkey.id)
                        .values(quiet_alerts_fired=None)
                    )
                    await session.commit()
                continue
            levels = crossed_levels(
                now, vkey.last_used_at, self.config.quiet_days,
                vkey.quiet_alerts_fired,
            )
            if not levels:
                continue
            # Persist first (duplicate silence over duplicate noise),
            # then deliver — the budget rule.
            new_fired = sorted(set(vkey.quiet_alerts_fired or []) | set(levels))
            async with sessionmaker() as session:
                await session.execute(
                    update(VirtualKey)
                    .where(VirtualKey.id == vkey.id)
                    .values(quiet_alerts_fired=new_fired)
                )
                await session.commit()
            for level in levels:
                await self._deliver_quiet_alert(
                    vkey.id, vkey.name, level, vkey.last_used_at, idle, now
                )
                if self.events is not None:
                    await self.events.emit(
                        "quiet.threshold",
                        None,
                        key=vkey.id,
                        key_name=vkey.name,
                        level_days=level,
                        idle_days=round(idle, 3),
                        last_used_at=vkey.last_used_at.isoformat(),
                    )
                fired_count += 1
        return fired_count

    # -- delivery -----------------------------------------------------------

    def _quiet_payload(
        self,
        key_id: str,
        key_name: str | None,
        level: int,
        last_used_at: datetime,
        idle: float,
        now: datetime,
    ) -> dict:
        message = (
            f"Virtual key '{key_name or key_id}' has not been used in "
            f"{idle:.1f} days (crossed the {level}-day quiet threshold; "
            f"last use {last_used_at.isoformat()})."
        )
        return {
            "type": "quiet_key_alert",
            "level_days": level,
            "key_id": key_id,
            "key_name": key_name,
            "last_used_at": last_used_at.isoformat(),
            "idle_days": round(idle, 3),
            "message": message,
            "timestamp": now.isoformat(),
        }

    async def _deliver_quiet_alert(
        self,
        key_id: str,
        key_name: str | None,
        level: int,
        last_used_at: datetime,
        idle: float,
        now: datetime | None = None,
    ) -> None:
        if now is None:
            now = datetime.now(timezone.utc)
        payload = self._quiet_payload(
            key_id, key_name, level, last_used_at, idle, now
        )
        await self._deliver(payload)

    async def _deliver(self, payload: dict) -> bool:
        """POST the payload; retry once; metric + log on failure —
        the budget alerts' delivery leg, kind swapped."""
        url = self.config.resolve_url()
        if not self.config.enabled or not url:
            # No webhook configured: still count the alert that *would*
            # have fired, so ops can spot silent keys in /metrics.
            metrics.ALERTS.labels(kind="quiet", outcome="skipped").inc()
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
                    metrics.ALERTS.labels(kind="quiet", outcome="sent").inc()
                    return True
                logger.warning(
                    "quiet-key alert webhook returned %s for key %s (attempt %s)",
                    response.status_code,
                    payload.get("key_id"),
                    attempt,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "quiet-key alert webhook failed for key %s (attempt %s): %s",
                    payload.get("key_id"),
                    attempt,
                    exc,
                )
            if attempt >= max(1, self.config.max_attempts):
                metrics.ALERTS.labels(kind="quiet", outcome="failed").inc()
                return False
            await asyncio.sleep(0.5)
