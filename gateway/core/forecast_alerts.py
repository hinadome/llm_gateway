"""Budget forecast alert delivery (tier 48): the tier-47 projections
get teeth.

A forecast nobody reads is a spreadsheet. This module turns the
month-end projections into the two transitions an on-call actually
consumes: a budgeted entity that starts trending badly (the recent
projection puts it over its monthly budget, or at risk of it) and one
that is provably back within plan. The design borrows its shapes
deliberately:

* **Decide from the served numbers.** One pass is exactly the
  ``forecasts_core.forecast()`` the admin endpoint serves — an alert
  is decided by the numbers the dashboard shows, so delivery can
  never drift from measurement. Both levels alert: a key against its
  own ``monthly_budget_usd``, a tenant against its pooled one — the
  same two budgets the invoice compares.
* **Fire-once / re-arm per (month, entity)**, persisted on the
  settings KV table (``forecast_alert_state``) so a restart never
  re-pages for a condition it already announced. State persists
  before delivery: a crash mid-pass prefers duplicate silence over
  duplicate noise. The month is part of the state key — a new month
  is a new plan, and a tenant that trended over in November is paged
  again in December if December trends the same way.
* **Transitions, not levels** — the canary discipline:
  ``forecast.alert`` on the bad transition, ``forecast.recovered``
  when the projection genuinely heals; an entity that stays trending
  stays silent. Recovery is what re-arms, and recovery includes the
  operator's honest fix: raising the budget heals the projection.

The honesty rules are the forecast's own, paging edition. **Hours of
evidence never page**: before ``min_elapsed_days`` of month (default
2, the forecast's trend threshold) the pass holds everything — a
day-one straight-line extrapolation is exactly the noise the trend
rules refuse, and it must not reach a webhook. **No budget means no
bar**: sections without ``monthly_budget_usd`` never fire, and a
budget removed while firing drops the state silently — there is
nothing left to recover to, and a re-added budget starts honestly
cold (the removed-target rule). And the alerts ride the FORECAST's
verdicts only: actual spend crossings are the tier-2 budget alerts'
job (``core/alerts.py``, thresholds on spend so far); this surface
pages on where the month is GOING.

The evaluator is a single-writer surface (the lifespan loop; tests
drive ``evaluate_once()`` directly, optionally pinning the pass's
clock), so the KV read-modify-write needs no compare-and-swap —
unlike the spend-threshold alerts, which fire from concurrent
request paths.

Delivery is best-effort and never blocks anything: bounded retries,
``gateway_alerts_total{kind="forecast",outcome=sent|failed|skipped}``,
and without a webhook URL the POST is a no-op that still counts as
``skipped`` while the observer events still fire.
``forecast_alerts.enabled: false`` turns the whole evaluator off —
no state writes, no events — while the forecast keeps serving at
``GET /admin/forecasts`` (which also carries each section's
persisted alert state, so the dashboard shows exactly what on-call
was paged for, from the same settings row the evaluator wrote).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from ..config import ForecastAlertsConfig
from ..models import SettingKV
from ..observability import metrics
from . import forecasts as forecasts_core

if TYPE_CHECKING:  # pragma: no cover — import for typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..config import GatewayConfig
    from .events import EventBus

logger = logging.getLogger("gateway.forecast_alerts")

# The settings KV row carrying per-entity alert state: a JSON map
# "{month}|{kind}:{id}" -> {"firing": true, "condition": ..., "since":
# ...}. Entries exist only while firing (recovery clears them by
# omission); the month rides in the key so a new month starts cold,
# and last month's entries drop on the first pass that evaluates the
# new one (the removed-target rule).
_STATE_KEY = "forecast_alert_state"


def entity_name(month: str, kind: str, entity_id: str) -> str:
    """The state key for one budgeted entity in one month."""
    return f"{month}|{kind}:{entity_id}"


def alert_condition(section: dict[str, Any]) -> tuple[bool | None, str | None]:
    """Pure: the alertable condition of one forecast section (a key's
    or a tenant's). Returns ``(firing, condition)``:

    * ``(True, "trending_over" | "trending_at_risk")`` — the month-end
      projection puts the entity over its budget, or at risk of it
      (≥80%, the forecast's own thresholds).
    * ``(False, None)`` — provably within plan: verdict
      ``within_budget``.
    * ``(None, None)`` — not alertable: no budget, hence no verdict.
      The caller skips the section entirely — it never fires, and a
      firing entry for it drops from the state (the bar was removed;
      there is nothing to recover to).
    """
    verdict = section.get("verdict")
    if verdict == "over_budget":
        return True, "trending_over"
    if verdict == "at_risk":
        return True, "trending_at_risk"
    if verdict == "within_budget":
        return False, None
    return None, None


def _effective_projection(section: dict[str, Any]) -> float:
    """The projection the forecast's own verdicts trust (the module's
    rule, applied consistently here): the recent pace's when one
    exists, the run-rate's otherwise."""
    if (section.get("recent_pace") or {}).get("daily_usd") is not None:
        return section["recent_pace"]["projected_month_usd"]
    return section["run_rate"]["projected_month_usd"]


def _entity_display(kind: str, section: dict[str, Any]) -> tuple[str, str]:
    """(what, name) for the human message — the same vocabulary the
    spend-threshold alerts use."""
    if kind == "tenant":
        return "tenant", str(section.get("tenant_name") or section.get("tenant_id"))
    return "virtual key", str(section.get("key_name") or section.get("key_id"))


def alert_payload(
    section: dict[str, Any],
    kind: str,
    condition: str,
    fired_at: str,
    month: str,
) -> dict[str, Any]:
    """The webhook payload for a fire transition (pure — unit-tested
    directly). Carries the entity, the month, the condition, and the
    projections that produced it, so the receiving end can route on
    any of them without calling back."""
    what, name = _entity_display(kind, section)
    budget = section.get("monthly_budget_usd") or 0.0
    projected = _effective_projection(section)
    pct = section.get("projected_pct_of_budget")
    based_on = section.get("verdict_based_on")
    trending = "trending over" if condition == "trending_over" else "trending toward"
    message = (
        f"{what.capitalize()} '{name}' is {trending} its ${budget:.2f} "
        f"monthly budget: projected ${projected:.2f} by month end "
        f"({pct}% of budget, the {based_on} projection)."
    )
    return {
        "type": "budget_forecast_alert",
        "kind": kind,
        "key_id": section.get("key_id") if kind == "key" else None,
        "key_name": section.get("key_name") if kind == "key" else None,
        "tenant_id": section.get("tenant_id") if kind == "tenant" else None,
        "tenant_name": section.get("tenant_name") if kind == "tenant" else None,
        "month": month,
        "condition": condition,
        "verdict": section.get("verdict"),
        "verdict_based_on": based_on,
        "budget_usd": budget,
        "spend_so_far_usd": section.get("spend_so_far_usd"),
        "projected_month_usd": projected,
        "run_rate": section.get("run_rate"),
        "recent_pace": section.get("recent_pace"),
        "trend_ratio": section.get("trend_ratio"),
        "projected_pct_of_budget": pct,
        "message": message,
        "fired_at": fired_at,
    }


def recovered_payload(
    section: dict[str, Any],
    kind: str,
    recovered_at: str,
    month: str,
) -> dict[str, Any]:
    """The webhook payload for a recovery transition: what healed, and
    the within-plan projection it healed to (often because the
    operator raised the budget — an honest fix recovers honestly)."""
    what, name = _entity_display(kind, section)
    budget = section.get("monthly_budget_usd") or 0.0
    projected = _effective_projection(section)
    pct = section.get("projected_pct_of_budget")
    return {
        "type": "budget_forecast_recovered",
        "kind": kind,
        "key_id": section.get("key_id") if kind == "key" else None,
        "key_name": section.get("key_name") if kind == "key" else None,
        "tenant_id": section.get("tenant_id") if kind == "tenant" else None,
        "tenant_name": section.get("tenant_name") if kind == "tenant" else None,
        "month": month,
        "verdict": section.get("verdict"),
        "verdict_based_on": section.get("verdict_based_on"),
        "budget_usd": budget,
        "spend_so_far_usd": section.get("spend_so_far_usd"),
        "projected_month_usd": projected,
        "projected_pct_of_budget": pct,
        "message": (
            f"{what.capitalize()} '{name}' has recovered: projected "
            f"${projected:.2f} by month end ({pct}% of its "
            f"${budget:.2f} budget)."
        ),
        "recovered_at": recovered_at,
    }


async def alert_state(session: AsyncSession) -> dict[str, Any]:
    """The persisted per-entity alert state (the settings KV row) —
    read by ``GET /admin/forecasts`` so the panel shows exactly what
    on-call was paged for, from the same row the evaluator wrote."""
    row = await session.get(SettingKV, _STATE_KEY)
    if row is None:
        return {}
    try:
        parsed = json.loads(row.value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sections(data: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(kind, id, section) for every entity the forecast carries: each
    tenant (assigned only — the unassigned bucket has no budget by
    construction) and every key section, assigned or not (unassigned
    keys carry their own budgets). Admin-key rows (no key id) cannot
    have budgets and never appear here as alertable — they simply
    never fire."""
    out: list[tuple[str, str, dict[str, Any]]] = []
    for section in [*data["tenants"], *data["unassigned"]]:
        if section.get("tenant_id") is not None:
            out.append(("tenant", section["tenant_id"], section))
        for key in section.get("keys") or []:
            if key.get("key_id") is not None:
                out.append(("key", key["key_id"], key))
    return out


class ForecastAlertEvaluator:
    """Evaluates the month-end projections on a schedule and delivers
    transition alerts. Constructed once in the app lifespan; the loop
    calls ``evaluate_once`` per interval and tests call it directly
    (optionally pinning the pass's clock, so early-month gates and
    month rolls are testable on any real date)."""

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
        config: "GatewayConfig",
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """One evaluation pass: forecast the current month (the same
        ``forecast()`` the admin endpoint serves, the whole platform),
        compute the alert transitions against the persisted state, and
        deliver. ``now`` pins the pass's clock (tests); production
        leaves it at the real time.

        Returns the payloads delivered this pass (fire + recovery).
        With ``forecast_alerts.enabled: false`` the pass is a no-op
        that touches nothing — measurement without delivery is the
        operator's explicit choice, and it leaves no trace."""
        alerts_cfg: ForecastAlertsConfig = config.forecast_alerts
        if not alerts_cfg.enabled:
            return []
        data = await forecasts_core.forecast(session, None, now=now)
        # The early-month honesty gate, before anything else: a
        # day-one straight-line extrapolation is hours of evidence
        # dressed as a trend, and it never reaches a webhook. The
        # hold writes nothing — there cannot be current-month state
        # before the gate opens (nothing ever fired under it).
        if data["elapsed_days"] < alerts_cfg.min_elapsed_days:
            return []
        month = data["month"]
        state = await alert_state(session)
        fired_at = data["generated_at"]

        # Pass 1 — decide every transition before touching delivery:
        # the state write happens once, before any POST, so a crash
        # mid-pass prefers duplicate silence over duplicate noise.
        new_state: dict[str, Any] = {}
        deliveries: list[tuple[str, dict[str, Any]]] = []  # (kind, payload)
        for kind, entity_id, section in _sections(data):
            firing, condition = alert_condition(section)
            if firing is None:
                # No budget: not alertable. A firing entry for it
                # (the budget was removed) drops from the state by
                # omission — no recovery event, because there is
                # nothing left to recover to.
                continue
            name = entity_name(month, kind, entity_id)
            prev = state.get(name) or {}
            if firing:
                if prev.get("firing"):
                    # Fire-once: still trending, silence. Re-arms only
                    # after a genuine recovery (or a new month).
                    new_state[name] = prev
                    continue
                entry = {
                    "firing": True,
                    "condition": condition,
                    "since": fired_at,
                }
                new_state[name] = entry
                deliveries.append(
                    ("alert", alert_payload(section, kind, condition, fired_at, month))
                )
            elif prev.get("firing"):
                # Was trending, now provably within plan: clear
                # (re-arm) and announce the recovery.
                deliveries.append(
                    ("recovered", recovered_payload(section, kind, fired_at, month))
                )
            # Within plan and never fired: nothing to say.

        # Names absent from this pass drop out of the state — last
        # month's entries when the month rolled, a removed budget, a
        # deleted key with its budget: nothing left to recover from,
        # and everything re-added starts honestly cold.
        await self._save_state(session, new_state)
        self.last = new_state

        for kind, payload in deliveries:
            if kind == "alert":
                await self._deliver(alerts_cfg, payload)
                if self.events is not None:
                    self.events.emit_background(
                        "forecast.alert",
                        None,
                        entity=f"{payload['kind']}:{payload['key_id'] or payload['tenant_id']}",
                        condition=payload["condition"],
                        month=payload["month"],
                        budget_usd=payload["budget_usd"],
                        projected_month_usd=payload["projected_month_usd"],
                        message=payload["message"],
                    )
            else:
                await self._deliver(alerts_cfg, payload)
                if self.events is not None:
                    self.events.emit_background(
                        "forecast.recovered",
                        None,
                        entity=f"{payload['kind']}:{payload['key_id'] or payload['tenant_id']}",
                        month=payload["month"],
                        message=payload["message"],
                    )
        return [payload for _, payload in deliveries]

    async def _deliver(self, alerts_cfg: ForecastAlertsConfig, payload: dict) -> bool:
        """POST the payload; bounded retries; metric + log on
        failure. Without a URL the delivery counts as ``skipped`` —
        the threshold was real even if nobody is listening (the
        budget-alert rule)."""
        url = alerts_cfg.resolve_url()
        if not url:
            metrics.ALERTS.labels(kind="forecast", outcome="skipped").inc()
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
                    metrics.ALERTS.labels(kind="forecast", outcome="sent").inc()
                    return True
                logger.warning(
                    "forecast alert webhook returned %s for %s (attempt %s)",
                    response.status_code,
                    payload.get("message"),
                    attempt,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "forecast alert webhook failed (attempt %s): %s",
                    attempt,
                    exc,
                )
            if attempt >= max(1, alerts_cfg.max_attempts):
                metrics.ALERTS.labels(kind="forecast", outcome="failed").inc()
                return False
            await asyncio.sleep(0.5)
