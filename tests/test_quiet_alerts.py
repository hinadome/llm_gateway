"""Quiet-key alerts — tier 56: the liveness edition of the alert sweeps.

The contract pinned here, in one paragraph: a key that WAS alive (it
has a ``last_used_at``, tier 55) and has been idle for
``alerts.quiet_days`` days (default **7/30** — 7 the early page, a
cron stopped or an integration is stuck; 30 the churn page, rotate or
revoke) fires each day-level once, lowest first (a quiet period
unfolds in order — the mirror of expiry's highest-first, which counts
down to a deadline). State persists on the key row
(``quiet_alerts_fired``) before delivery — duplicate silence over
duplicate noise. Resumed use re-arms silently ON THE SWEEP (idle below
every level clears the state), so one row pages once per quiet period,
forever — and the decision stays inside the alert layer:
``record_key_use`` never touches alert state. There is NO arming hook
at create/PATCH — the mirror-image difference from expiry: a deadline
is the operator's choice (arms silently), quiet is earned by traffic
(the first use starts the clock). Never-used keys never alert (they
were never alive — that finding is the ``idle_days`` listing filter's);
revoked keys never warn; rotation never touches the state. A key the
sweep DISCOVERS already 40 days quiet fires every crossed level
honestly. Delivery rides the budget/expiry webhook family
(``gateway_alerts_total{kind="quiet",outcome=...}``); without a URL
the POST is a skipped no-op while the ``quiet.threshold`` observer
events still fire; ``quiet_days: []`` stops this sweep alone.

Time-travel is direct row UPDATE (the tier-55 pattern) — the sweep's
clock is pinned via ``evaluate_once(now=...)`` (the loop never starts
under pytest), so no test sleeps.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from prometheus_client import REGISTRY
from sqlalchemy import update

from gateway.config import AlertsConfig, EventsConfig, EventHandlerConfig
from gateway.core.quiet_alerts import crossed_levels, idle_days_count, should_rearm
from gateway.db import get_sessionmaker
from gateway.models import VirtualKey

from .conftest import ADMIN_KEY, _make_env, auth, create_key, make_config
from .test_forecast_alerts import WebhookSink

_LEVELS = [7, 30]


def _config(alerts: AlertsConfig | None = None):
    """The app config every test here uses: the Recorder event handler
    (records everything it sees) + the alert settings."""
    return make_config(
        events=EventsConfig(
            handlers=[EventHandlerConfig(class_path="tests.test_events:Recorder")]
        ),
        **({"alerts": alerts} if alerts is not None else {}),
    )


async def _drain(app) -> list:
    await app.state.events.drain()
    recorder = app.state.events.handlers[0]
    events, recorder.events = recorder.events, []
    return events


async def _sweep(env, now: datetime) -> int:
    """One sweep pass at the pinned clock — production leaves it at
    the real time (the lifespan loop never runs under pytest)."""
    return await env.app.state.quiet_alerts.evaluate_once(now=now)


async def _row(client, name: str) -> dict:
    data = (await client.get("/admin/keys", headers=auth(ADMIN_KEY))).json()["data"]
    return next(k for k in data if k["name"] == name)


async def _set_last_used(key_id: str, **values) -> None:
    """Direct row UPDATE — the tier-55 pattern for time-traveling
    last_used_at (and is_active) without waiting out a real window."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(VirtualKey).where(VirtualKey.id == key_id).values(**values)
        )
        await session.commit()


def _alert_count(outcome: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "gateway_alerts_total", {"kind": "quiet", "outcome": outcome}
        )
        or 0
    )


# ----------------------------------------------------------------------
# The pure math
# ----------------------------------------------------------------------


def test_crossed_and_rearm_units():
    now = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)

    # 8 days idle: the 7 crossed, lowest first — never 30 without 7
    assert crossed_levels(now, now - timedelta(days=8), _LEVELS, None) == [7]
    # 40 days idle (the discovery shape): every level, IN ORDER
    assert crossed_levels(now, now - timedelta(days=40), _LEVELS, None) == [7, 30]
    # fire-once: an already-fired level never re-fires within the period
    assert crossed_levels(now, now - timedelta(days=40), _LEVELS, [7]) == [30]
    assert crossed_levels(now, now - timedelta(days=40), _LEVELS, [7, 30]) == []
    # freshly used: nothing crossed
    assert crossed_levels(now, now - timedelta(days=1), _LEVELS, None) == []
    # naive last_used_at (SQLite hands stored datetimes back naive)
    # normalizes to UTC — the _is_expired rule
    naive = (now - timedelta(days=8)).replace(tzinfo=None)
    assert crossed_levels(now, naive, _LEVELS, None) == [7]
    # fractional: 6.9 days idle is below the 7-day level — the
    # comparison is >=, so a whole day boundary must actually pass
    assert abs(idle_days_count(now, now - timedelta(days=6.9)) - 6.9) < 1e-6
    assert crossed_levels(now, now - timedelta(days=6.9), _LEVELS, None) == []

    # re-arm: resumed use (idle below every level) clears lingering
    # state — the sweep's decision, never record_key_use's
    assert should_rearm(1.0, _LEVELS, [7, 30]) is True
    assert should_rearm(6.9, _LEVELS, [7, 30]) is True
    # still quiet (between levels): the period holds
    assert should_rearm(10.0, _LEVELS, [7]) is False
    assert should_rearm(40.0, _LEVELS, [7, 30]) is False
    # nothing to clear / nothing configured
    assert should_rearm(1.0, _LEVELS, None) is False
    assert should_rearm(1.0, [], [7]) is False


# ----------------------------------------------------------------------
# The sweep: levels, order, payloads, events
# ----------------------------------------------------------------------


async def test_sweep_fires_levels_once_with_payloads_and_events(tmp_path):
    sink = WebhookSink()
    t0 = datetime.now(timezone.utc)
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_env(
            tmp_path,
            config=_config(AlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            row = await create_key(env.client, "gone-quiet")
            await _set_last_used(row["id"], last_used_at=t0 - timedelta(days=8))

            assert await _sweep(env, t0) == 1   # 7 fires
            assert await _sweep(env, t0) == 0   # once, ever (this period)
            assert [p["level_days"] for p in sink.payloads] == [7]

            # payload shape: what on-call reads
            p7 = sink.payloads[0]
            assert p7["type"] == "quiet_key_alert"
            assert p7["key_name"] == "gone-quiet"
            assert p7["key_id"] == row["id"]
            assert 7.9 < p7["idle_days"] <= 8.0
            assert p7["last_used_at"].startswith((t0 - timedelta(days=8)).isoformat()[:10])
            assert "crossed the 7-day quiet threshold" in p7["message"]

            # the crossing landed beside the webhook as an observer event
            events = await _drain(env.app)
            threshold_events = [e for e in events if e.name == "quiet.threshold"]
            assert [e.payload["level_days"] for e in threshold_events] == [7]
            assert threshold_events[0].payload["key_name"] == "gone-quiet"

            # and the fired state persisted on the row, restart-safe
            assert (await _row(env.client, "gone-quiet"))["quiet_alerts_fired"] == [7]


async def test_discovery_fires_every_crossed_level_in_order(tmp_path):
    # A key the sweep first sees already 40 days quiet (the discovery
    # case, liveness edition): every crossed level fires, LOWEST FIRST
    # — the story arrives in order, never 30 alone, never merged.
    sink = WebhookSink()
    t0 = datetime.now(timezone.utc)
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_env(
            tmp_path,
            config=_config(AlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            row = await create_key(env.client, "ancient")
            await _set_last_used(row["id"], last_used_at=t0 - timedelta(days=40))

            assert await _sweep(env, t0) == 2
            assert [p["level_days"] for p in sink.payloads] == [7, 30]
            assert (await _row(env.client, "ancient"))["quiet_alerts_fired"] == [7, 30]


async def test_resumed_use_rearms_and_the_next_period_pages_again(tmp_path):
    # The quiet-specific rule: a key that authenticates again is alive;
    # the next pass clears the fired state silently (no recovery page —
    # resumed use is the happy path), so the row pages once per quiet
    # period, forever. The decision lives entirely in the sweep —
    # record_key_use never touches alert state.
    sink = WebhookSink()
    t0 = datetime.now(timezone.utc)
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_env(
            tmp_path,
            config=_config(AlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            row = await create_key(env.client, "on-off")
            await _set_last_used(row["id"], last_used_at=t0 - timedelta(days=8))
            assert await _sweep(env, t0) == 1  # period 1: 7 fires
            assert (await _row(env.client, "on-off"))["quiet_alerts_fired"] == [7]

            # Resumed use: one request, the stamp lands ~now, the next
            # pass re-arms silently.
            used_at = t0 - timedelta(days=1)
            await _set_last_used(row["id"], last_used_at=used_at)
            assert await _sweep(env, t0) == 0  # no page, no recovery page
            assert (await _row(env.client, "on-off"))["quiet_alerts_fired"] is None

            # And the next quiet period pages honestly again.
            await _set_last_used(row["id"], last_used_at=t0 - timedelta(days=9))
            assert await _sweep(env, t0) == 1
            assert [p["level_days"] for p in sink.payloads] == [7, 7]


async def test_never_used_and_revoked_never_alert(env):
    # Never-used keys were never alive — that finding is the idle_days
    # listing filter's, not this alert's; revoked keys being dead is
    # not news. Neither pages.
    t0 = datetime.now(timezone.utc)
    await create_key(env.client, "never-used")
    gone = await create_key(env.client, "revoked-quiet")
    await _set_last_used(gone["id"], last_used_at=t0 - timedelta(days=40))
    await _set_last_used(gone["id"], is_active=False)

    assert await _sweep(env, t0) == 0
    assert (await _row(env.client, "never-used"))["quiet_alerts_fired"] is None
    assert (await _row(env.client, "revoked-quiet"))["quiet_alerts_fired"] is None


async def test_empty_levels_stop_this_sweep_alone(env):
    # quiet_days: [] disables the quiet sweep without touching the
    # budget or expiry families — the operator's per-family opt-out.
    t0 = datetime.now(timezone.utc)
    env.app.state.quiet_alerts.config = AlertsConfig(quiet_days=[])
    row = await create_key(env.client, "quiet-anyway")
    await _set_last_used(row["id"], last_used_at=t0 - timedelta(days=40))
    assert await _sweep(env, t0) == 0
    assert (await _row(env.client, "quiet-anyway"))["quiet_alerts_fired"] is None


async def test_no_webhook_skips_delivery_but_events_fire(tmp_path):
    # Without a URL the POST is a no-op counted "skipped" — ops can
    # spot silent keys in /metrics — while the quiet.threshold
    # observer events still fire.
    t0 = datetime.now(timezone.utc)
    skipped_before = _alert_count("skipped")
    async for env in _make_env(tmp_path, config=_config(AlertsConfig())):
        row = await create_key(env.client, "silent-key")
        await _set_last_used(row["id"], last_used_at=t0 - timedelta(days=8))

        assert await _sweep(env, t0) == 1
        assert _alert_count("skipped") == skipped_before + 1
        events = await _drain(env.app)
        assert any(e.name == "quiet.threshold" for e in events)


async def test_delivery_failure_retries_then_counts_failed(tmp_path):
    # The budget/expiry delivery leg, kind swapped: a 500-ing webhook
    # gets exactly max_attempts tries, then counts "failed" — the
    # crossing itself still persisted (duplicate silence over noise).
    sink = WebhookSink(status=500)
    t0 = datetime.now(timezone.utc)
    failed_before = _alert_count("failed")
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_env(
            tmp_path,
            config=_config(AlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            row = await create_key(env.client, "flaky-hook")
            await _set_last_used(row["id"], last_used_at=t0 - timedelta(days=8))

            assert await _sweep(env, t0) == 1
            assert sink.attempts == 2  # 1 try + 1 retry
            assert _alert_count("failed") == failed_before + 1
            # state persisted before delivery — the failure re-pages nothing
            assert (await _row(env.client, "flaky-hook"))["quiet_alerts_fired"] == [7]
