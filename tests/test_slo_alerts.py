"""SLO alert delivery — tier 44: the tier-42/43 verdicts get teeth.

The contract pinned here, in one paragraph: measurement pages no one,
delivery does. A background evaluator (the canary-loop shape —
always started, re-reading ``slos:`` each pass; tests drive
``evaluate_once()`` directly because the lifespan loop never runs
under pytest) turns each pass — the same ``status()`` the admin
endpoint serves, so an alert is decided by exactly the numbers the
dashboard shows — into the two transitions on-call consumes:
``slo.alert`` when a target starts firing (bar breached, a burn rule
burning, or the budget exhausted — in that precedence) and
``slo.recovered`` when it is provably healthy again. Fire-once /
re-arm per target: state persists on the settings KV table (a
restart never re-pages), a target that stays firing stays silent,
and recovery re-arms it. The honesty rule: a quiet window
(insufficient_evidence) is *unknown* — it holds the previous state,
never firing and never un-firing, because an SLO that stops
receiving traffic must neither page nor quietly clear a page.
Delivery is best-effort (bounded retries, ``gateway_alerts_total
{kind="slo",outcome=sent|failed|skipped}``); without a webhook URL
the POST is a skipped no-op while the events still fire, and
``slos.alerts.enabled: false`` turns the evaluator off entirely —
no state, no events — while measurement keeps serving.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from prometheus_client import REGISTRY
from sqlalchemy import delete

from gateway.config import (
    EventsConfig,
    EventHandlerConfig,
    SLOAlertsConfig,
    SLOTargetConfig,
    SLOsConfig,
)
from gateway.core.slo_alerts import (
    SLOAlertEvaluator,
    alert_condition,
    alert_payload,
    recovered_payload,
)
from gateway.db import get_sessionmaker
from gateway.models import RequestLog

from .conftest import ADMIN_KEY, _make_env, auth, make_config


# -- helpers ---------------------------------------------------------------


def _target(**kw) -> SLOTargetConfig:
    kw.setdefault("name", "svc")
    kw.setdefault("model", "test-model")
    kw.setdefault("metric", "error_rate")
    kw.setdefault("target", 0.05)
    kw.setdefault("window_minutes", 1440)
    kw.setdefault("min_requests", 20)
    return SLOTargetConfig(**kw)


class WebhookSink:
    """Captures webhook deliveries; configurable status for failure tests."""

    def __init__(self, status: int = 200) -> None:
        self.payloads: list[dict] = []
        self.attempts = 0
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        self.payloads.append(json.loads(request.content))
        return httpx.Response(self.status, json={"ok": True})


def _slo_count(outcome: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "gateway_alerts_total", {"kind": "slo", "outcome": outcome}
        )
        or 0
    )


def _slo_config(
    alerts: SLOAlertsConfig | None = None, targets: list[dict] | None = None
):
    """The app config every test here uses: SLO targets + the
    Recorder event handler (records everything it sees)."""
    return make_config(
        events=EventsConfig(
            handlers=[EventHandlerConfig(class_path="tests.test_events:Recorder")]
        ),
        slos=SLOsConfig(
            targets=[_target(**t) for t in (targets or [{}])],
            **({"alerts": alerts} if alerts is not None else {}),
        ),
    )


async def _drain(app) -> list:
    await app.state.events.drain()
    recorder = app.state.events.handlers[0]
    events, recorder.events = recorder.events, []
    return events


async def _seed(session, rows: list[tuple[str, int]]) -> None:
    """Seed (status, minutes_ago) rows for test-model at controlled
    ages — the windows the evaluator measures."""
    now = datetime.now(timezone.utc)
    for i, (status, minutes_ago) in enumerate(rows):
        session.add(
            RequestLog(
                request_id=f"t44-{i}",
                model="test-model",
                deployment="openai/primary",
                status=status,
                latency_ms=100,
                created_at=now - timedelta(minutes=minutes_ago),
            )
        )
    await session.commit()


async def _clear_rows() -> None:
    async with get_sessionmaker()() as session:
        await session.execute(delete(RequestLog))
        await session.commit()


async def _evaluate(env) -> list[dict]:
    """One evaluator pass through the app's own session factory."""
    async with get_sessionmaker()() as session:
        return await env.app.state.slo_alerts.evaluate_once(
            session, env.app.state.config
        )


# -- units: the pure condition machine ---------------------------------------


def test_alert_condition_precedence():
    """The pure (firing, condition) machine: the bar verdict first
    (the promise itself), then the burn rules (the pace — what the
    average cannot see), then the budget (the overspent window);
    provably healthy only when every leg says so; anything
    insufficient is *unknown*, never healthy."""
    # Breached outranks everything — the promise itself is broken.
    assert alert_condition(
        {"verdict": "breached", "budget": {"state": "exhausted"}, "burn": []}
    ) == (True, "breached")
    # A burn rule fires even when the bar holds (the at_risk story:
    # fine on average, burning fast NOW).
    assert alert_condition(
        {
            "verdict": "met",
            "budget": {"state": "at_risk"},
            "burn": [{"name": "fast-burn", "verdict": "burning"}],
        }
    ) == (True, "burning")
    # An exhausted budget fires even when the bar somehow holds.
    assert alert_condition(
        {
            "verdict": "met",
            "budget": {"state": "exhausted"},
            "burn": [{"verdict": "steady"}],
        }
    ) == (True, "exhausted")
    # Provably healthy: every leg good, nothing firing.
    assert alert_condition(
        {
            "verdict": "met",
            "budget": {"state": "on_track"},
            "burn": [{"verdict": "steady"}],
        }
    ) == (False, None)
    # Unknown: a quiet window is not a verdict — in either direction.
    assert alert_condition(
        {
            "verdict": "insufficient_evidence",
            "budget": {"state": "insufficient_evidence"},
            "burn": [{"verdict": "insufficient_evidence"}],
        }
    ) == (None, None)
    # A firing burn rule outranks an insufficient bar verdict: the
    # rule's evidence is independent of the bar's volume gate.
    assert alert_condition(
        {
            "verdict": "insufficient_evidence",
            "budget": {"state": "insufficient_evidence"},
            "burn": [{"verdict": "burning"}],
        }
    ) == (True, "burning")


def test_alert_payload_carries_the_numbers():
    """The webhook payload is self-sufficient: the definition, the
    condition, the measurement that produced it and the human
    message — the same reasons the dashboard tooltips show, never a
    second story."""
    row = {
        "name": "svc",
        "model": "gpt-4o",
        "deployment": None,
        "metric": "error_rate",
        "target": 0.05,
        "value": 0.2,
        "verdict": "breached",
        "reason": "error_rate is 0.2 against the 0.05 bar",
        "budget": {
            "state": "exhausted",
            "burn_rate": 4.0,
            "remaining_pct": -300.0,
            "bad": 80,
            "measured": 400,
        },
        "burn": [
            {
                "name": "fast-burn",
                "verdict": "burning",
                "rate": 14.4,
                "long": {"burn_rate": 15.2},
                "short": {"burn_rate": 16.0},
                "reason": "burning 15.2x over 60m",
            },
            {"name": "slow-burn", "verdict": "steady", "rate": 3.0},
        ],
    }
    payload = alert_payload(row, "breached", "2026-11-18T00:00:00+00:00")
    assert payload["type"] == "slo_alert"
    assert payload["target"] == "svc"
    assert payload["condition"] == "breached"
    assert payload["value"] == 0.2
    assert payload["budget"]["state"] == "exhausted"
    assert payload["burn_rules"][0]["long_burn_rate"] == 15.2
    assert "against the 0.05 bar" in payload["message"]
    assert payload["fired_at"] == "2026-11-18T00:00:00+00:00"

    recovered = recovered_payload(
        {
            "name": "svc",
            "model": "gpt-4o",
            "deployment": None,
            "metric": "error_rate",
            "target": 0.05,
            "verdict": "met",
            "budget": {"state": "on_track", "remaining_pct": 80.0},
        },
        "2026-11-18T00:01:00+00:00",
    )
    assert recovered["type"] == "slo_recovered"
    assert "has recovered" in recovered["message"]
    assert recovered["budget_state"] == "on_track"


async def test_delivery_outcomes_no_url_retry_then_fail():
    """Delivery semantics: without a webhook URL the POST is a
    skipped no-op (the threshold was real even if nobody listens);
    with a failing endpoint the bounded retries run out and the
    outcome is honestly failed; with a working one, sent."""
    ev = SLOAlertEvaluator()
    try:
        skipped_before = _slo_count("skipped")
        assert await ev._deliver(SLOAlertsConfig(), {"target": "x"}) is False
        assert _slo_count("skipped") == skipped_before + 1

        sink = WebhookSink(status=500)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(sink.handler)
        ) as ac:
            ev_retry = SLOAlertEvaluator(client=ac)
            failed_before = _slo_count("failed")
            assert (
                await ev_retry._deliver(
                    SLOAlertsConfig(webhook_url="http://sink/hook", max_attempts=2),
                    {"target": "x"},
                )
                is False
            )
            assert sink.attempts == 2  # 1 try + 1 retry, then honest failure
            assert _slo_count("failed") == failed_before + 1

            ok_sink = WebhookSink()
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(ok_sink.handler)
            ) as ac2:
                ev_ok = SLOAlertEvaluator(client=ac2)
                sent_before = _slo_count("sent")
                assert (
                    await ev_ok._deliver(
                        SLOAlertsConfig(webhook_url="http://sink/hook"),
                        {"target": "x"},
                    )
                    is True
                )
                assert len(ok_sink.payloads) == 1
                assert _slo_count("sent") == sent_before + 1
            await ev_retry.aclose()
            await ev_ok.aclose()
    finally:
        await ev.aclose()


# -- e2e: transitions over seeded windows -----------------------------------
#
# The sink tests inject the webhook client at app level (the
# alert_http_client param — the same wiring the budget-alert tests
# use) and wrap _make_env in an explicit async-with so the client
# closes on the test's own loop; the generator stays single-level
# (a nested async generator finalizes late, off-loop, and leaves
# aiosqlite worker threads with pending futures on a dead loop).


async def test_alert_fires_once_and_state_survives_restart(tmp_path):
    """The fire-once contract: a breached window delivers exactly one
    webhook and one event; a second pass over the same breach is
    silent (still firing, already paged); and the state lives on the
    settings KV table, not in memory — a brand-new evaluator (the
    restart story) reads it and stays silent too."""
    sink = WebhookSink()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(sink.handler)
    ) as ac:
        async for env in _make_env(
            tmp_path,
            config=_slo_config(alerts=SLOAlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            async with get_sessionmaker()() as session:
                # 25 rows, 5 over the 5% bar and the min gate: breached.
                await _seed(
                    session,
                    [("error", 5)] * 5 + [("success", 10)] * 20,
                )

            delivered = await _evaluate(env)
            assert len(delivered) == 1
            assert sink.attempts == 1
            payload = sink.payloads[0]
            assert payload["type"] == "slo_alert"
            assert payload["target"] == "svc"
            assert payload["condition"] == "breached"
            assert "against the 0.05 bar" in payload["message"]

            events = await _drain(env.app)
            alerts = [e for e in events if e.name == "slo.alert"]
            assert len(alerts) == 1
            assert alerts[0].payload["target"] == "svc"
            assert alerts[0].payload["condition"] == "breached"

            # Second pass over the same breach: silence (fire-once).
            assert await _evaluate(env) == []
            assert sink.attempts == 1
            assert not [
                e for e in await _drain(env.app) if e.name.startswith("slo.")
            ]

            # The restart story: a brand-new evaluator, fresh client —
            # the persisted state still says firing, so no re-page.
            restart_sink = WebhookSink()
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(restart_sink.handler)
            ) as ac2:
                restarted = SLOAlertEvaluator(
                    client=ac2, events=env.app.state.events
                )
                async with get_sessionmaker()() as session:
                    assert await restarted.evaluate_once(
                        session, env.app.state.config
                    ) == []
                assert restart_sink.attempts == 0
            break


async def test_recovery_rearms_and_refires(tmp_path):
    """The re-arm contract: a fired target that heals delivers
    ``slo_recovered`` and clears the state; the same target breached
    AGAIN fires a second alert — recovery is what re-arms, and only
    recovery."""
    sink = WebhookSink()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(sink.handler)
    ) as ac:
        async for env in _make_env(
            tmp_path,
            config=_slo_config(alerts=SLOAlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            async with get_sessionmaker()() as session:
                await _seed(session, [("error", 5)] * 5 + [("success", 10)] * 20)
            await _evaluate(env)
            assert sink.attempts == 1

            # Heal: a clean window, same volume.
            await _clear_rows()
            async with get_sessionmaker()() as session:
                await _seed(session, [("success", 5)] * 25)
            delivered = await _evaluate(env)
            assert len(delivered) == 1
            assert delivered[0]["type"] == "slo_recovered"
            assert "has recovered" in delivered[0]["message"]
            events = await _drain(env.app)
            assert [e.name for e in events if e.name.startswith("slo.")] == [
                "slo.alert",
                "slo.recovered",
            ]

            # Breach again: the recovery re-armed it — a second alert.
            await _clear_rows()
            async with get_sessionmaker()() as session:
                await _seed(session, [("error", 5)] * 5 + [("success", 10)] * 20)
            delivered = await _evaluate(env)
            assert len(delivered) == 1 and delivered[0]["type"] == "slo_alert"
            assert sink.attempts == 3  # fire, recover, re-fire
            break


async def test_insufficient_holds_state_never_recovers(tmp_path):
    """The honesty rule: after firing, a window gone quiet
    (insufficient evidence) holds the alert state — no recovery
    event, no cleared state — because an SLO that stopped receiving
    traffic must never quietly un-page. Only provable health
    recovers."""
    sink = WebhookSink()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(sink.handler)
    ) as ac:
        async for env in _make_env(
            tmp_path,
            config=_slo_config(alerts=SLOAlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            async with get_sessionmaker()() as session:
                await _seed(session, [("error", 5)] * 5 + [("success", 10)] * 20)
            await _evaluate(env)
            assert sink.attempts == 1

            # The traffic stops: 3 rows in the window — under the
            # min_requests gate, the verdict is insufficient.
            await _clear_rows()
            async with get_sessionmaker()() as session:
                await _seed(session, [("success", 5)] * 3)
            assert await _evaluate(env) == []
            assert sink.attempts == 1  # neither recovered nor re-fired
            events = await _drain(env.app)
            assert [e.name for e in events if e.name.startswith("slo.")] == [
                "slo.alert"
            ]  # only the original fire — the quiet window said nothing

            # Evidence returns and it is healthy: NOW the recovery fires.
            await _clear_rows()
            async with get_sessionmaker()() as session:
                await _seed(session, [("success", 5)] * 25)
            delivered = await _evaluate(env)
            assert len(delivered) == 1 and delivered[0]["type"] == "slo_recovered"
            break


async def test_burn_alerts_on_pace_while_bar_met(tmp_path):
    """The tier's thesis delivered: the at_risk row — bar MET on the
    day, fast-burn BURNING now — pages with condition ``burning``
    while the payload honestly carries verdict ``met``: the pace
    alerted on what the average could not see."""
    sink = WebhookSink()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(sink.handler)
    ) as ac:
        async for env in _make_env(
            tmp_path,
            config=_slo_config(alerts=SLOAlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            rows: list[tuple[str, int]] = []
            # A quiet day carrying the average: 1000 good rows, 3-23h old.
            rows += [("success", m) for m in range(200, 1200)][:1000]
            # A bad hour, 10-33 minutes ago: 24 bad + 8 good.
            rows += [("error", m) for m in range(10, 34)][:24]
            rows += [("success", m) for m in range(10, 30)][:8]
            # And it is still burning now: 8 bad + 2 good in the last 2m.
            rows += [("error", 2)] * 8 + [("success", 2)] * 2
            async with get_sessionmaker()() as session:
                await _seed(session, rows)

            delivered = await _evaluate(env)
            assert len(delivered) == 1
            payload = delivered[0]
            assert payload["condition"] == "burning"
            assert payload["verdict"] == "met"  # the day is within the bar
            assert "fast-burn" in payload["message"]
            assert payload["budget"]["state"] == "at_risk"
            assert sink.attempts == 1

            events = await _drain(env.app)
            alert = next(e for e in events if e.name == "slo.alert")
            assert alert.payload["condition"] == "burning"
            break


async def test_admin_slos_rows_carry_alert_state(tmp_path):
    """The endpoint shows what on-call was paged for: after a fire,
    the row carries the firing condition (from the same settings row
    the evaluator wrote); a target that never fired carries null; a
    recovered target carries null again. The config echo includes
    the delivery settings."""
    sink = WebhookSink()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(sink.handler)
    ) as ac:
        async for env in _make_env(
            tmp_path,
            config=_slo_config(
                alerts=SLOAlertsConfig(webhook_url="http://sink/hook"),
                targets=[{"name": "svc"}, {"name": "quiet", "model": "other-model"}],
            ),
            alert_client=ac,
        ):
            async with get_sessionmaker()() as session:
                await _seed(session, [("error", 5)] * 5 + [("success", 10)] * 20)
            await _evaluate(env)

            resp = await env.client.get("/admin/slos", headers=auth(ADMIN_KEY))
            assert resp.status_code == 200, resp.text
            body = resp.json()
            svc = next(r for r in body["data"] if r["name"] == "svc")
            assert svc["alert"]["firing"] is True
            assert svc["alert"]["condition"] == "breached"
            assert svc["alert"]["since"]
            quiet = next(r for r in body["data"] if r["name"] == "quiet")
            assert quiet["alert"] is None  # never fired
            # The delivery settings echo with the rest of the config.
            assert body["config"]["alerts"]["webhook_url"] == "http://sink/hook"
            assert body["config"]["evaluation_interval_seconds"] == 60.0

            # Recovery clears it: the row reads null again.
            await _clear_rows()
            async with get_sessionmaker()() as session:
                await _seed(session, [("success", 5)] * 25)
            await _evaluate(env)
            resp = await env.client.get("/admin/slos", headers=auth(ADMIN_KEY))
            svc = next(r for r in resp.json()["data"] if r["name"] == "svc")
            assert svc["alert"] is None
            break


async def test_disabled_alerts_touch_nothing(tmp_path):
    """``slos.alerts.enabled: false`` is the explicit kill switch: a
    breached window evaluates to nothing — no deliveries, no events,
    no state written — while measurement keeps serving at
    ``GET /admin/slos``."""
    async for env in _make_env(
        tmp_path, config=_slo_config(alerts=SLOAlertsConfig(enabled=False))
    ):
        async with get_sessionmaker()() as session:
            await _seed(session, [("error", 5)] * 5 + [("success", 10)] * 20)

        assert await _evaluate(env) == []
        events = await _drain(env.app)
        assert not [e for e in events if e.name.startswith("slo.")]

        # Measurement is untouched: the endpoint still verdicts.
        resp = await env.client.get("/admin/slos", headers=auth(ADMIN_KEY))
        svc = next(r for r in resp.json()["data"] if r["name"] == "svc")
        assert svc["verdict"] == "breached"
        assert svc["alert"] is None
        break


async def test_no_targets_is_a_noop_pass(tmp_path):
    """No targets means nothing to evaluate: the pass touches no
    state and delivers nothing — the loop idles at its short re-check
    (the canary discipline for a feature with nothing to do)."""
    sink = WebhookSink()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(sink.handler)
    ) as ac:
        config = _slo_config(alerts=SLOAlertsConfig(webhook_url="http://sink/hook"))
        async for env in _make_env(tmp_path, config=config, alert_client=ac):
            async with get_sessionmaker()() as session:
                assert await env.app.state.slo_alerts.evaluate_once(
                    session, env.app.state.config
                ) == []
            assert sink.attempts == 0
            break
