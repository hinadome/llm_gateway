"""SLO alert delivery (tier 44): the tier-42/43 verdicts get teeth.

Measurement pages no one. This module turns the SLO numbers into the
two transitions an on-call actually consumes: a target that starts
firing (the bar is breached, a burn rule is burning, or the window
overspent its error budget) and a target that is provably healthy
again. The design borrows its shapes deliberately:

* **Decide from the served numbers.** One pass is exactly the
  ``slos.status()`` the admin endpoint serves — an alert is decided
  by the numbers the dashboard shows, so delivery can never drift
  from measurement.
* **Fire-once / re-arm** — the budget-alert discipline, persisted on
  the settings KV table (``slo_alert_state``) so a restart never
  re-pages for a condition it already announced. State persists
  before delivery: a crash mid-pass prefers duplicate silence over
  duplicate noise.
* **Transitions, not levels** — the canary discipline: ``slo.alert``
  on the bad transition, ``slo.recovered`` when it genuinely heals;
  a target that stays firing stays silent.

The honesty rule that makes this safe to leave on: a quiet window is
not a verdict in EITHER direction. A target whose evidence went
insufficient (fewer than ``min_requests`` rows, nothing streamed)
holds its previous alert state — no fire, no recovery — until real
evidence returns. An SLO that stops receiving traffic never pages
and never un-pages.

The evaluator is a single-writer surface (the lifespan loop; tests
drive ``evaluate_once()`` directly), so the KV read-modify-write
needs no compare-and-swap — unlike budget alerts, which fire from
concurrent request paths.

Delivery is best-effort and never blocks anything: bounded retries,
``gateway_alerts_total{kind="slo",outcome=sent|failed|skipped}``
(the budget-alert metric), and without a webhook URL the POST is a
no-op that still counts as ``skipped`` while the observer events
still fire. ``slos.alerts.enabled: false`` turns the whole evaluator
off — no state writes, no events — while measurement keeps serving
at ``GET /admin/slos``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select

from ..config import SLOAlertsConfig
from ..models import SettingKV
from ..observability import metrics
from . import slos as slos_core

if TYPE_CHECKING:  # pragma: no cover — import for typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..config import GatewayConfig
    from .events import EventBus

logger = logging.getLogger("gateway.slo_alerts")

# The settings KV row carrying per-target alert state: a JSON map
# target name -> {"firing": true, "condition": ..., "since": ...}.
# Entries exist only while firing (recovery clears them), and names
# vanish when the target leaves the config — nothing to recover from.
_STATE_KEY = "slo_alert_state"


def alert_condition(row: dict[str, Any]) -> tuple[bool | None, str | None]:
    """Pure: the alertable condition of one ``slos.status()`` row.

    Returns ``(firing, condition)``:

    * ``(True, "breached" | "burning" | "exhausted")`` — firing. The
      bar verdict is checked first (the promise itself), then the
      burn rules (the pace — which catches what the average cannot
      see), then the budget (the window overspent).
    * ``(False, None)`` — provably healthy: verdict ``met``, budget
      ``on_track``, no rule burning.
    * ``(None, None)`` — unknown: some leg is insufficient_evidence.
      The caller holds the previous state; a quiet window neither
      fires nor recovers.
    """
    budget = row.get("budget") or {}
    rules = row.get("burn") or []
    if row.get("verdict") == "breached":
        return True, "breached"
    if any(r.get("verdict") == "burning" for r in rules):
        return True, "burning"
    if budget.get("state") == "exhausted":
        return True, "exhausted"
    if (
        row.get("verdict") == "met"
        and budget.get("state") == "on_track"
        and not any(r.get("verdict") == "burning" for r in rules)
    ):
        return False, None
    return None, None


def _condition_reason(row: dict[str, Any], condition: str) -> str:
    """The human line for a firing condition — the same reasons the
    dashboard tooltips show, never a second story."""
    if condition == "breached":
        return row.get("reason") or "the bar is breached"
    if condition == "burning":
        burning = [r for r in row.get("burn") or [] if r.get("verdict") == "burning"]
        return "; ".join(
            f"{r.get('name')}: {r.get('reason') or 'burn rule firing'}" for r in burning
        )
    budget = row.get("budget") or {}
    return budget.get("reason") or "the error budget is exhausted"


def alert_payload(row: dict[str, Any], condition: str, fired_at: str) -> dict:
    """The webhook payload for a fire transition (pure — unit-tested
    directly). Carries the definition, the condition, the numbers
    that produced it and the human message, so the receiving end can
    route on any of them without calling back."""
    message = f"SLO '{row['name']}' is {condition}: {_condition_reason(row, condition)}"
    return {
        "type": "slo_alert",
        "target": row["name"],
        "model": row.get("model"),
        "deployment": row.get("deployment"),
        "metric": row.get("metric"),
        "bar": row.get("target"),
        "condition": condition,
        "value": row.get("value"),
        "verdict": row.get("verdict"),
        "budget": {
            k: (row.get("budget") or {}).get(k)
            for k in ("state", "burn_rate", "remaining_pct", "bad", "measured")
        },
        "burn_rules": [
            {
                "name": r.get("name"),
                "verdict": r.get("verdict"),
                "rate": r.get("rate"),
                "long_burn_rate": (r.get("long") or {}).get("burn_rate"),
                "short_burn_rate": (r.get("short") or {}).get("burn_rate"),
            }
            for r in row.get("burn") or []
        ],
        "reason": _condition_reason(row, condition),
        "message": message,
        "fired_at": fired_at,
    }


def recovered_payload(row: dict[str, Any], recovered_at: str) -> dict:
    """The webhook payload for a recovery transition: what healed, and
    the healthy numbers it healed to."""
    budget = row.get("budget") or {}
    return {
        "type": "slo_recovered",
        "target": row["name"],
        "model": row.get("model"),
        "deployment": row.get("deployment"),
        "metric": row.get("metric"),
        "bar": row.get("target"),
        "verdict": row.get("verdict"),
        "budget_state": budget.get("state"),
        "remaining_pct": budget.get("remaining_pct"),
        "message": (
            f"SLO '{row['name']}' has recovered: verdict "
            f"{row.get('verdict')}, budget {budget.get('state')}"
        ),
        "recovered_at": recovered_at,
    }


async def alert_state(session: AsyncSession) -> dict[str, Any]:
    """The persisted per-target alert state (the settings KV row) —
    read by ``GET /admin/slos`` so the panel shows exactly what
    on-call was paged for, from the same row the evaluator wrote."""
    row = await session.get(SettingKV, _STATE_KEY)
    if row is None:
        return {}
    try:
        parsed = json.loads(row.value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class SLOAlertEvaluator:
    """Evaluates the configured SLO targets on a schedule and delivers
    transition alerts. Constructed once in the app lifespan; the loop
    calls ``evaluate_once`` per interval and tests call it directly."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        events: EventBus | None = None,
    ) -> None:
        # Dedicated client (not the instrumented app one): alert POSTs
        # should not appear as spans in request traces — the
        # budget-alert rule. main.py injects the shared alert client;
        # standalone use owns one.
        self.events = events
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        # In-memory mirror of the persisted state — the last pass's
        # view, for surfaces that want it without a DB read.
        self.last: dict[str, Any] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _save_state(self, session: AsyncSession, state: dict[str, Any]) -> None:
        row = await session.get(SettingKV, _STATE_KEY)
        encoded = json.dumps(state)
        if row is None:
            session.add(SettingKV(key=_STATE_KEY, value=encoded))
        else:
            row.value = encoded
        await session.commit()

    async def evaluate_once(
        self,
        session: AsyncSession,
        config: GatewayConfig,
    ) -> list[dict[str, Any]]:
        """One evaluation pass: measure every configured target (the
        same ``status()`` the admin endpoint serves), compute the
        alert transitions against the persisted state, and deliver.

        Returns the payloads delivered this pass (fire + recovery).
        With no targets, or ``alerts.enabled: false``, the pass is a
        no-op that touches nothing — measurement without delivery is
        the operator's explicit choice, and it leaves no trace."""
        alerts_cfg: SLOAlertsConfig = config.slos.alerts
        if not config.slos.targets or not alerts_cfg.enabled:
            return []
        rows = await slos_core.status(session, config)
        state = await alert_state(session)
        now = datetime.now(timezone.utc)

        # Pass 1 — decide every transition before touching delivery:
        # the state write happens once, before any POST, so a crash
        # mid-pass prefers duplicate silence over duplicate noise.
        new_state: dict[str, Any] = {}
        deliveries: list[tuple[str, dict[str, Any]]] = []  # (kind, payload)
        for row in rows:
            name = row["name"]
            prev = state.get(name) or {}
            firing, condition = alert_condition(row)
            if firing is None:
                # Unknown (insufficient evidence): hold the previous
                # state verbatim — never fire, never recover on a
                # quiet window.
                if prev:
                    new_state[name] = prev
                continue
            if firing:
                if prev.get("firing"):
                    # Fire-once: still firing, silence. Re-arms only
                    # after a genuine recovery.
                    new_state[name] = prev
                    continue
                entry = {
                    "firing": True,
                    "condition": condition,
                    "since": now.isoformat(),
                }
                new_state[name] = entry
                deliveries.append(("alert", alert_payload(row, condition, entry["since"])))
            elif prev.get("firing"):
                # Was firing, now provably healthy: clear (re-arm) and
                # announce the recovery.
                deliveries.append(("recovered", recovered_payload(row, now.isoformat())))
            # Healthy and never fired: nothing to say.

        # Names absent from this pass (target removed from the
        # config) drop out of the state — there is nothing left to
        # recover from, and a re-added target starts honestly cold.
        await self._save_state(session, new_state)
        self.last = new_state

        for kind, payload in deliveries:
            if kind == "alert":
                await self._deliver(alerts_cfg, payload)
                if self.events is not None:
                    self.events.emit_background(
                        "slo.alert",
                        None,
                        target=payload["target"],
                        condition=payload["condition"],
                        metric=payload["metric"],
                        bar=payload["bar"],
                        value=payload["value"],
                        message=payload["message"],
                    )
            else:
                await self._deliver(alerts_cfg, payload)
                if self.events is not None:
                    self.events.emit_background(
                        "slo.recovered",
                        None,
                        target=payload["target"],
                        metric=payload["metric"],
                        message=payload["message"],
                    )
        return [payload for _, payload in deliveries]

    async def _deliver(self, alerts_cfg: SLOAlertsConfig, payload: dict) -> bool:
        """POST the payload; bounded retries; metric + log on
        failure. Without a URL the delivery counts as ``skipped`` —
        the threshold was real even if nobody is listening (the
        budget-alert rule)."""
        url = alerts_cfg.resolve_url()
        if not url:
            metrics.ALERTS.labels(kind="slo", outcome="skipped").inc()
            return False
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.post(
                    url,
                    json=payload,
                    timeout=httpx.Timeout(alerts_cfg.timeout_seconds),
                )
                if response.status_code < 400:
                    metrics.ALERTS.labels(kind="slo", outcome="sent").inc()
                    return True
                logger.warning(
                    "slo alert webhook returned %s for target %s (attempt %s)",
                    response.status_code,
                    payload.get("target"),
                    attempt,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "slo alert webhook failed for target %s (attempt %s): %s",
                    payload.get("target"),
                    attempt,
                    exc,
                )
            if attempt >= max(1, alerts_cfg.max_attempts):
                metrics.ALERTS.labels(kind="slo", outcome="failed").inc()
                return False
            await asyncio.sleep(0.5)
