"""Budget forecast alert delivery — tier 48: the tier-47 projections
get teeth.

The contract pinned here, in one paragraph: a forecast nobody reads is
a spreadsheet, so a background evaluator (the canary-loop shape —
always started, re-reading ``forecast_alerts:`` each pass; tests drive
``evaluate_once()`` directly, pinning the pass's clock, because the
lifespan loop never runs under pytest) turns each pass — the same
``forecast()`` the admin endpoint serves, the whole platform — into
the two transitions on-call consumes: ``forecast.alert`` when a
budgeted entity starts trending badly (the month-end projection over
its budget, or at risk ≥80% — ``trending_over`` / ``trending_at_risk``)
and ``forecast.recovered`` when the projection is provably back within
plan (often because the operator raised the budget — an honest fix
recovers honestly). Fire-once / re-arm per (month, entity): state
persists on the settings KV table keyed ``{month}|{kind}:{id}`` (a
restart never re-pages; a NEW MONTH is a new plan and re-arms
everything), and recovery is what re-arms within a month. The honesty
rules: before ``min_elapsed_days`` of month the pass holds everything
— a day-one straight-line extrapolation is hours of evidence dressed
as a trend and never reaches a webhook — and a section with no budget
has no bar: it never fires, and a budget removed while firing drops
the state silently (nothing to recover to; a re-added budget starts
cold). Both levels alert — a key against its own budget, a tenant
against its pooled one. Delivery is best-effort (bounded retries,
``gateway_alerts_total{kind="forecast",outcome=sent|failed|skipped}``);
without a webhook URL the POST is a skipped no-op while the events
still fire, and ``forecast_alerts.enabled: false`` turns the evaluator
off entirely — no state, no events — while the forecast keeps serving
at ``GET /admin/forecasts`` with each section's persisted alert state
(``null`` when never fired or recovered) and the delivery config
echoed beside it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from prometheus_client import REGISTRY

from gateway.config import (
    EventsConfig,
    EventHandlerConfig,
    ForecastAlertsConfig,
)
from gateway.core.forecast_alerts import (
    ForecastAlertEvaluator,
    alert_condition,
    alert_payload,
    recovered_payload,
)
from gateway.db import get_sessionmaker
from gateway.models import RequestLog, Tenant, VirtualKey

from .conftest import ADMIN_KEY, _make_env, auth, create_key, make_config
from .test_tenants import _create_tenant

# The tests pin the evaluator's clock: a mid-month `now` makes every
# pass deterministic on any real date (the month is whatever the
# pinned clock says it is — the same `now` forecast() takes), and a
# day-2 12:00 `now` sits under the default min_elapsed_days gate.
_REAL = datetime.now(timezone.utc)
_MID = datetime(_REAL.year, _REAL.month, 15, 12, 0, tzinfo=timezone.utc)
if _REAL.month == 12:
    _NEXT = datetime(_REAL.year + 1, 1, 5, 12, 0, tzinfo=timezone.utc)
else:
    _NEXT = datetime(_REAL.year, _REAL.month + 1, 5, 12, 0, tzinfo=timezone.utc)
_EARLY = datetime(_REAL.year, _REAL.month, 2, 12, 0, tzinfo=timezone.utc)


class WebhookSink:
    """Captures webhook deliveries; configurable status for failure tests."""

    def __init__(self, status: int = 200) -> None:
        self.payloads: list[dict] = []
        self.attempts = 0
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        self.payloads.append(__import__("json").loads(request.content))
        return httpx.Response(self.status, json={"ok": True})


def _alert_count(outcome: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "gateway_alerts_total", {"kind": "forecast", "outcome": outcome}
        )
        or 0
    )


def _config(alerts: ForecastAlertsConfig | None = None):
    """The app config every test here uses: the Recorder event handler
    (records everything it sees) + the forecast alert settings."""
    return make_config(
        events=EventsConfig(
            handlers=[EventHandlerConfig(class_path="tests.test_events:Recorder")]
        ),
        **({"forecast_alerts": alerts} if alerts is not None else {}),
    )


async def _drain(app) -> list:
    await app.state.events.drain()
    recorder = app.state.events.handlers[0]
    events, recorder.events = recorder.events, []
    return events


async def _seed_trend(env, key, tenant_id=None, *, rows: int = 25, cost: float = 1.0,
                      at: datetime | None = None) -> None:
    """Seed `rows` requests of `cost` one day before the pinned
    clock — a flat recent window the projections straight-line."""
    base = (at or _MID) - timedelta(days=1)
    async with get_sessionmaker()() as session:
        for i in range(rows):
            session.add(
                RequestLog(
                    request_id=f"t48-{key['id']}-{at or _MID}-{i}",
                    key_id=key["id"],
                    key_name=key["name"],
                    tenant_id=tenant_id,
                    model="test-model",
                    status="success",
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                    cost_usd=cost,
                    created_at=base + timedelta(minutes=i),
                )
            )
        await session.commit()


async def _set_budget(kind: str, entity_id: str, budget: float | None) -> None:
    """The operator's lever, applied the way the admin API would."""
    table = VirtualKey if kind == "key" else Tenant
    async with get_sessionmaker()() as session:
        row = await session.get(table, entity_id)
        row.monthly_budget_usd = budget
        await session.commit()


async def _evaluate(env, now: datetime | None = _MID) -> list[dict]:
    """One evaluator pass through the app's own session factory, at
    the pinned clock (tests) — production leaves it at the real time."""
    async with get_sessionmaker()() as session:
        return await env.app.state.forecast_alerts.evaluate_once(
            session, env.app.state.config, now=now
        )


# -- units: the pure condition and payload machine --------------------------


def test_alert_condition_and_no_budget():
    """The pure (firing, condition) machine: the forecast's own
    verdicts, verbatim — over_budget fires ``trending_over``,
    at_risk fires ``trending_at_risk``, within_budget is provably
    healthy — and a section with NO verdict (no budget) is not
    alertable at all: it never fires and never recovers, and a firing
    entry for it drops from the state (the bar was removed; there is
    nothing to recover to)."""
    over = {"verdict": "over_budget"}
    assert alert_condition(over) == (True, "trending_over")
    at_risk = {"verdict": "at_risk"}
    assert alert_condition(at_risk) == (True, "trending_at_risk")
    within = {"verdict": "within_budget"}
    assert alert_condition(within) == (False, None)
    # No budget → no verdict: not alertable, in either direction.
    assert alert_condition({"verdict": None}) == (None, None)


def test_payloads_carry_the_numbers():
    """The webhook payloads are self-sufficient: the entity (kind +
    ids), the month, the condition, the projections that produced it
    and the human message — the same numbers the dashboard shows,
    never a second story."""
    section = {
        "key_id": "k1",
        "key_name": "alpha",
        "tenant_id": "t1",
        "monthly_budget_usd": 10.0,
        "spend_so_far_usd": 25.0,
        "run_rate": {"daily_usd": 1.7, "projected_month_usd": 51.0},
        "recent_pace": {"daily_usd": 3.571429, "projected_month_usd": 80.36},
        "trend_ratio": 2.1,
        "projected_pct_of_budget": 803.57,
        "verdict": "over_budget",
        "verdict_based_on": "recent_pace",
    }
    payload = alert_payload(
        section, "key", "trending_over", "2026-11-15T12:00:00+00:00", "2026-11"
    )
    assert payload["type"] == "budget_forecast_alert"
    assert payload["kind"] == "key"
    assert payload["key_id"] == "k1"
    assert payload["tenant_id"] is None  # a key payload carries the key's ids
    assert payload["month"] == "2026-11"
    assert payload["condition"] == "trending_over"
    assert payload["verdict_based_on"] == "recent_pace"
    assert payload["projected_month_usd"] == 80.36  # the effective projection
    assert payload["recent_pace"]["daily_usd"] == 3.571429
    assert payload["trend_ratio"] == 2.1
    assert "projected $80.36 by month end (803.57% of budget" in payload["message"]
    assert payload["fired_at"] == "2026-11-15T12:00:00+00:00"

    tenant_section = {
        "tenant_id": "t1",
        "tenant_name": "Acme",
        "monthly_budget_usd": 20.0,
        "spend_so_far_usd": 10.0,
        "run_rate": {"daily_usd": 0.7, "projected_month_usd": 21.0},
        "recent_pace": {"daily_usd": None, "projected_month_usd": 15.0},
        "trend_ratio": None,
        "projected_pct_of_budget": 75.0,
        "verdict": "within_budget",
        "verdict_based_on": "run_rate",
    }
    recovered = recovered_payload(
        tenant_section, "tenant", "2026-11-16T00:00:00+00:00", "2026-11"
    )
    assert recovered["type"] == "budget_forecast_recovered"
    assert recovered["kind"] == "tenant"
    assert recovered["tenant_id"] == "t1"
    assert recovered["projected_month_usd"] == 21.0  # recent daily None → the run-rate's
    assert "has recovered: projected $21.00 by month end" in recovered["message"]


async def test_delivery_outcomes_no_url_retry_then_fail():
    """Delivery semantics: without a webhook URL the POST is a skipped
    no-op (the threshold was real even if nobody listens); with a
    failing endpoint the bounded retries run out and the outcome is
    honestly failed; with a working one, sent."""
    ev = ForecastAlertEvaluator()
    try:
        skipped_before = _alert_count("skipped")
        assert await ev._deliver(ForecastAlertsConfig(), {"message": "x"}) is False
        assert _alert_count("skipped") == skipped_before + 1

        sink = WebhookSink(status=500)
        async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
            ev_retry = ForecastAlertEvaluator(client=ac)
            failed_before = _alert_count("failed")
            assert (
                await ev_retry._deliver(
                    ForecastAlertsConfig(
                        webhook_url="http://sink/hook", max_attempts=2
                    ),
                    {"message": "x"},
                )
                is False
            )
            assert sink.attempts == 2  # 1 try + 1 retry, then honest failure
            assert _alert_count("failed") == failed_before + 1

            ok_sink = WebhookSink()
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(ok_sink.handler)
            ) as ac2:
                ev_ok = ForecastAlertEvaluator(client=ac2)
                sent_before = _alert_count("sent")
                assert (
                    await ev_ok._deliver(
                        ForecastAlertsConfig(webhook_url="http://sink/hook"),
                        {"message": "x"},
                    )
                    is True
                )
                assert len(ok_sink.payloads) == 1
                assert _alert_count("sent") == sent_before + 1
            await ev_retry.aclose()
            await ev_ok.aclose()
    finally:
        await ev.aclose()


# -- e2e: transitions over seeded trends ------------------------------------
#
# The sink tests inject the webhook client at app level (the
# alert_http_client param — the same wiring the SLO alert tests use)
# and wrap _make_env in an explicit async-with so the client closes on
# the test's own loop; the generator stays single-level (a nested
# async generator finalizes late, off-loop, and leaves aiosqlite
# worker threads with pending futures on a dead loop).


async def test_alert_fires_once_and_state_survives_restart(tmp_path):
    """The fire-once contract, both levels: a key trending over its
    own budget AND its tenant trending over the pooled one deliver
    exactly one webhook and one event each; a second pass over the
    same trend is silent (still firing, already paged); and the state
    lives on the settings KV table, not in memory — a brand-new
    evaluator (the restart story) reads it and stays silent too."""
    sink = WebhookSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_env(
            tmp_path,
            config=_config(ForecastAlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            acme = await _create_tenant(env.client, "Acme", monthly_budget_usd=20.0)
            alpha = await create_key(
                env.client, "alpha", tenant_id=acme["id"], monthly_budget_usd=10.0
            )
            await _seed_trend(env, alpha, tenant_id=acme["id"])
            # $25 booked, all of it recent: projected ~$80 by month end
            # — 803% of the key's $10, 401% of the tenant's $20.

            delivered = await _evaluate(env)
            assert len(delivered) == 2
            by_kind = {d["kind"]: d for d in delivered}
            assert by_kind["key"]["type"] == "budget_forecast_alert"
            assert by_kind["key"]["condition"] == "trending_over"
            assert "trending over its $10.00 monthly budget" in by_kind["key"]["message"]
            assert by_kind["tenant"]["type"] == "budget_forecast_alert"
            assert by_kind["tenant"]["budget_usd"] == 20.0
            assert sink.attempts == 2

            events = await _drain(env.app)
            alerts = [e for e in events if e.name == "forecast.alert"]
            assert len(alerts) == 2
            assert {a.payload["condition"] for a in alerts} == {"trending_over"}

            # Second pass over the same trend: silence (fire-once).
            assert await _evaluate(env) == []
            assert sink.attempts == 2
            assert not [
                e for e in await _drain(env.app) if e.name.startswith("forecast.")
            ]

            # The restart story: a brand-new evaluator, fresh client —
            # the persisted state still says firing, so no re-page.
            restart_sink = WebhookSink()
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(restart_sink.handler)
            ) as ac2:
                restarted = ForecastAlertEvaluator(
                    client=ac2, events=env.app.state.events
                )
                async with get_sessionmaker()() as session:
                    assert await restarted.evaluate_once(
                        session, env.app.state.config, now=_MID
                    ) == []
                assert restart_sink.attempts == 0
            break


async def test_recovery_rearms_and_refires(tmp_path):
    """The re-arm contract: the operator's honest fix — raising the
    budget — heals the projection and delivers ``budget_forecast_recovered``
    with the healed numbers; the same entity trending over AGAIN fires
    a second alert, because recovery is what re-arms, and only
    recovery."""
    sink = WebhookSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_env(
            tmp_path,
            config=_config(ForecastAlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            acme = await _create_tenant(env.client, "Acme")
            alpha = await create_key(
                env.client, "alpha", tenant_id=acme["id"], monthly_budget_usd=10.0
            )
            await _seed_trend(env, alpha, tenant_id=acme["id"])
            delivered = await _evaluate(env)
            assert len(delivered) == 1 and delivered[0]["kind"] == "key"
            assert sink.attempts == 1

            # The fix: budget $10 → $500. The projection heals to
            # within plan and the recovery says so, with the numbers.
            await _set_budget("key", alpha["id"], 500.0)
            delivered = await _evaluate(env)
            assert len(delivered) == 1
            recovered = delivered[0]
            assert recovered["type"] == "budget_forecast_recovered"
            assert "has recovered: projected $80.36 by month end (16.07%" in (
                recovered["message"]
            )
            events = await _drain(env.app)
            assert [e.name for e in events if e.name.startswith("forecast.")] == [
                "forecast.alert",
                "forecast.recovered",
            ]

            # Trend over the NEW budget: the recovery re-armed it.
            await _seed_trend(env, alpha, tenant_id=acme["id"], rows=60, cost=5.0)
            delivered = await _evaluate(env)
            assert len(delivered) == 1 and delivered[0]["type"] == "budget_forecast_alert"
            assert "over its $500.00 monthly budget" in delivered[0]["message"]
            assert sink.attempts == 3  # fire, recover, re-fire
            break


async def test_at_risk_fires_trending_at_risk(tmp_path):
    """The softer condition: a projection landing between 80% and
    100% of budget pages ``trending_at_risk`` — the month is not over
    budget yet, but it is where the money is going. The event carries
    the condition; the payload the verdict it came from."""
    sink = WebhookSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_env(
            tmp_path,
            config=_config(ForecastAlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            acme = await _create_tenant(env.client, "Acme")
            # Budget $100, projected ~$80 by month end: $25 recent
            # carried over the ~15.5 remaining days — at_risk (≥80%,
            # under 100%).
            alpha = await create_key(
                env.client, "alpha", tenant_id=acme["id"], monthly_budget_usd=100.0
            )
            await _seed_trend(env, alpha, tenant_id=acme["id"], rows=25, cost=1.0)

            delivered = await _evaluate(env)
            assert len(delivered) == 1
            assert delivered[0]["condition"] == "trending_at_risk"
            assert delivered[0]["verdict"] == "at_risk"
            assert "trending toward its $100.00 monthly budget" in (
                delivered[0]["message"]
            )
            assert sink.attempts == 1
            break


async def test_early_month_holds_then_fires(tmp_path):
    """The honesty gate: before ``min_elapsed_days`` of month (a
    day-one straight-line extrapolation is hours of evidence dressed
    as a trend) the pass delivers nothing and writes no state — and
    the same trend, once the month has real days behind it, fires."""
    sink = WebhookSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_env(
            tmp_path,
            config=_config(
                ForecastAlertsConfig(
                    webhook_url="http://sink/hook", min_elapsed_days=2.0
                )
            ),
            alert_client=ac,
        ):
            acme = await _create_tenant(env.client, "Acme")
            alpha = await create_key(
                env.client, "alpha", tenant_id=acme["id"], monthly_budget_usd=10.0
            )
            # Seed relative to the EARLY clock so the rows sit inside
            # the month the early pass measures.
            await _seed_trend(env, alpha, tenant_id=acme["id"], at=_EARLY)

            assert await _evaluate(env, now=_EARLY) == []
            assert sink.attempts == 0
            assert not [
                e for e in await _drain(env.app) if e.name.startswith("forecast.")
            ]
            from gateway.core.forecast_alerts import alert_state

            async with get_sessionmaker()() as session:
                assert await alert_state(session) == {}  # the gate wrote nothing

            # The same trend at mid-month: the gate is open, it fires.
            await _seed_trend(env, alpha, tenant_id=acme["id"])
            delivered = await _evaluate(env)
            assert len(delivered) == 1 and delivered[0]["condition"] == "trending_over"
            break


async def test_no_budget_never_alerts_and_removal_drops_state(tmp_path):
    """No bar, no page: a key without a budget never fires however
    much it spends (the unassigned platform bucket included), and a
    budget REMOVED while firing drops the state silently — no
    recovery event, nothing to recover to — with a re-added budget
    starting honestly cold."""
    sink = WebhookSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_env(
            tmp_path,
            config=_config(ForecastAlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            acme = await _create_tenant(env.client, "Acme")
            alpha = await create_key(
                env.client, "alpha", tenant_id=acme["id"], monthly_budget_usd=10.0
            )
            bare = await create_key(env.client, "bare", tenant_id=acme["id"])
            platform = await create_key(env.client, "platform")
            await _seed_trend(env, alpha, tenant_id=acme["id"])
            await _seed_trend(env, bare, tenant_id=acme["id"], rows=30, cost=2.0)
            await _seed_trend(env, platform, rows=10, cost=5.0)

            delivered = await _evaluate(env)
            # Only alpha — bare has no budget, the platform key
            # neither, and Acme (created without a pooled one) has no
            # bar to trend over.
            assert [d["kind"] for d in delivered] == ["key"]
            assert delivered[0]["key_id"] == alpha["id"]

            # The budget is removed while firing: the state drops,
            # silently — no recovery, because there is nothing left
            # to recover to.
            await _set_budget("key", alpha["id"], None)
            events = await _drain(env.app)  # clear the fire events
            assert await _evaluate(env) == []
            assert not [e for e in await _drain(env.app) if e.name.startswith("forecast.")]
            from gateway.core.forecast_alerts import alert_state

            async with get_sessionmaker()() as session:
                state = await alert_state(session)
            assert state == {}  # nothing left with a bar to fire on

            # A re-added budget starts honestly cold: still trending
            # over, it fires again — the page belongs to the new bar.
            await _set_budget("key", alpha["id"], 10.0)
            delivered = await _evaluate(env)
            assert [d["type"] for d in delivered] == ["budget_forecast_alert"]
            assert delivered[0]["kind"] == "key"
            break


async def test_month_roll_rearms_everything(tmp_path):
    """The month is part of the state key: a new month is a new plan.
    The entity that trended over (and was paged) in month M is paged
    again in M+1 if M+1 trends the same way — the state is keyed
    ``{month}|{kind}:{id}``, and the M entries drop from the state on
    the first pass that evaluates M+1 (the removed-target rule)."""
    sink = WebhookSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_env(
            tmp_path,
            config=_config(ForecastAlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            acme = await _create_tenant(env.client, "Acme")
            alpha = await create_key(
                env.client, "alpha", tenant_id=acme["id"], monthly_budget_usd=10.0
            )
            await _seed_trend(env, alpha, tenant_id=acme["id"])
            assert len(await _evaluate(env)) == 1  # paged in month M
            assert sink.attempts == 1

            # Month M+1, day 5: the same trend (seeded in M+1's
            # window) pages again — a new month, a new plan.
            await _seed_trend(env, alpha, tenant_id=acme["id"], at=_NEXT, rows=10)
            delivered = await _evaluate(env, now=_NEXT)
            assert len(delivered) == 1
            assert delivered[0]["type"] == "budget_forecast_alert"
            assert sink.attempts == 2

            # And the state holds only M+1 entries now.
            from gateway.core.forecast_alerts import alert_state

            async with get_sessionmaker()() as session:
                state = await alert_state(session)
            month = f"{_REAL.year:04d}-{_REAL.month:02d}"
            assert not [k for k in state if k.startswith(f"{month}|")]
            assert [k for k in state if k.startswith(f"{_NEXT.year:04d}-{_NEXT.month:02d}|")]
            break


async def test_disabled_alerts_touch_nothing(tmp_path):
    """``forecast_alerts.enabled: false`` is the explicit kill switch:
    a trending-over entity evaluates to nothing — no deliveries, no
    events, no state written — while the forecast keeps serving at
    ``GET /admin/forecasts`` with ``alert: null`` and the config echo
    saying the delivery is off."""
    async for env in _make_env(
        tmp_path, config=_config(ForecastAlertsConfig(enabled=False))
    ):
        acme = await _create_tenant(env.client, "Acme")
        alpha = await create_key(
            env.client, "alpha", tenant_id=acme["id"], monthly_budget_usd=10.0
        )
        await _seed_trend(env, alpha, tenant_id=acme["id"])

        assert await _evaluate(env) == []
        events = await _drain(env.app)
        assert not [e for e in events if e.name.startswith("forecast.")]

        resp = await env.client.get("/admin/forecasts", headers=auth(ADMIN_KEY))
        assert resp.status_code == 200
        body = resp.json()
        (acme_section,) = body["tenants"]
        (alpha_key,) = acme_section["keys"]
        assert alpha_key["verdict"] == "over_budget"  # measurement untouched
        assert alpha_key["alert"] is None
        assert body["alerts"]["enabled"] is False
        assert body["alerts"]["min_elapsed_days"] == 2.0
        break


async def test_admin_forecasts_carries_alert_state(tmp_path):
    """The endpoint shows what on-call was paged for: after a fire,
    the key and tenant sections carry the firing condition (from the
    same settings row the evaluator wrote); a section that never
    fired carries null; recovery clears it to null again; the config
    echo carries the delivery settings."""
    sink = WebhookSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_env(
            tmp_path,
            config=_config(
                ForecastAlertsConfig(webhook_url="http://sink/hook"),
            ),
            alert_client=ac,
        ):
            acme = await _create_tenant(env.client, "Acme", monthly_budget_usd=20.0)
            alpha = await create_key(
                env.client, "alpha", tenant_id=acme["id"], monthly_budget_usd=10.0
            )
            beta = await create_key(
                env.client, "beta", tenant_id=acme["id"], monthly_budget_usd=50.0
            )
            await _seed_trend(env, alpha, tenant_id=acme["id"])
            await _evaluate(env)

            resp = await env.client.get("/admin/forecasts", headers=auth(ADMIN_KEY))
            body = resp.json()
            (acme_section,) = body["tenants"]
            assert acme_section["alert"]["firing"] is True
            assert acme_section["alert"]["condition"] == "trending_over"
            assert acme_section["alert"]["since"]
            by_name = {k["key_name"]: k for k in acme_section["keys"]}
            assert by_name["alpha"]["alert"]["firing"] is True
            assert by_name["beta"]["alert"] is None  # never fired
            assert body["alerts"]["webhook_url"] == "http://sink/hook"
            assert body["alerts"]["evaluation_interval_seconds"] == 300.0

            # The tenant's fix (pooled budget raised): the recovery
            # clears BOTH the tenant section and — via alpha's own
            # raise — the key's too; null again.
            await _set_budget("tenant", acme["id"], 5000.0)
            await _set_budget("key", alpha["id"], 5000.0)
            await _evaluate(env)
            resp = await env.client.get("/admin/forecasts", headers=auth(ADMIN_KEY))
            (acme_section,) = resp.json()["tenants"]
            assert acme_section["alert"] is None
            break
