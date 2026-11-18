"""Key-expiry warnings — tier 52: the deadline's own pages.

The contract pinned here, in one paragraph: ``expires_at`` was always
enforced (the 401 tells callers the moment a key dies) but never
announced — on-call's first signal was the user complaint. A background
sweep (the canary-loop shape — always started, re-reading ``alerts:``
each pass; tests drive ``evaluate_once()`` directly, pinning the sweep's
clock, because the lifespan loop never runs under pytest) walks active
keys with an ``expires_at`` and fires once per day-level as the
deadline approaches: ``alerts.expiry_days`` (14/7/1/0; 0 is the page
that the key just died). Fire-once per level, state persisted on the
key row before delivery (a restart never re-pages; duplicate silence
beats duplicate noise). Setting or moving a deadline ARMS silently —
every level already crossed is marked fired, the budget re-arm rule
for deadlines, so a key born 6 days from its deadline never pages
"expires in 14 days" for a choice the operator just made; moving a
deadline OUT honestly re-arms the levels it now sits above, moving it
IN never pages. A key the sweep DISCOVERS with a near deadline (fired
state NULL — it predates the feature) fires its already-crossed levels
honestly. Revoked keys and deadline-less keys never warn; rotation
(tier 51) never touches the state — a deadline is a deadline whichever
credential presents it. Delivery is best-effort on the budget alerts'
webhook (``gateway_alerts_total{kind="expiry",outcome=...}``); without
a URL the POST is a skipped no-op while the ``expiry.threshold``
observer events still fire.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import httpx
from prometheus_client import REGISTRY

from gateway.config import AlertsConfig, EventsConfig, EventHandlerConfig
from gateway.core.auth import hash_key
from gateway.core.expiry_alerts import crossed_levels, rearm_levels
from gateway.db import get_sessionmaker
from gateway.models import VirtualKey

from .conftest import ADMIN_KEY, _make_env, auth, create_key, make_config
from .test_forecast_alerts import WebhookSink

_LEVELS = [14, 7, 1, 0]


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
    return await env.app.state.expiry_alerts.evaluate_once(now=now)


async def _row(client, name: str) -> dict:
    data = (await client.get("/admin/keys", headers=auth(ADMIN_KEY))).json()["data"]
    return next(k for k in data if k["name"] == name)


async def _insert_key(name: str, *, expires_at=None, is_active=True) -> None:
    """Direct row insert — the discovery tests bypass the admin API's
    arming on purpose (these keys predate the feature)."""
    raw = "sk-gw-exp-" + secrets.token_urlsafe(12)
    async with get_sessionmaker()() as session:
        session.add(
            VirtualKey(
                name=name,
                key_hash=hash_key(raw),
                key_prefix=raw[:12],
                expires_at=expires_at,
                is_active=is_active,
            )
        )
        await session.commit()


# ----------------------------------------------------------------------
# The pure math
# ----------------------------------------------------------------------


def test_crossed_levels_units():
    now = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)

    # inside two of the levels, highest crossed first
    deadline = now + timedelta(days=6.5)
    assert crossed_levels(now, deadline, _LEVELS, None) == [14, 7]
    assert crossed_levels(now, deadline, _LEVELS, [14]) == [7]
    assert crossed_levels(now, deadline, _LEVELS, [14, 7]) == []

    # a deadline safely past: every level crossed, 0 included
    assert crossed_levels(now, now - timedelta(days=1), _LEVELS, None) == [14, 7, 1, 0]

    # far from the deadline: nothing crossed (0 only fires once the
    # deadline has actually passed)
    assert crossed_levels(now, now + timedelta(days=30), _LEVELS, None) == []
    assert crossed_levels(now, now + timedelta(days=0.5), _LEVELS, None) == [14, 7, 1]

    # naive deadlines (SQLite hands stored datetimes back naive)
    # normalize to UTC — the _is_expired rule (3 days out: the 1-day
    # level is NOT crossed)
    naive = (now + timedelta(days=3)).replace(tzinfo=None)
    assert crossed_levels(now, naive, _LEVELS, None) == [14, 7]

    # rearm: everything already crossed at (re)arm time is marked fired
    # (persisted state sorts ascending, the budget convention — firing
    # order is what runs highest-first)
    assert rearm_levels(now, deadline, _LEVELS) == [7, 14]
    assert rearm_levels(now, None, _LEVELS) == []  # no deadline, nothing to track
    assert rearm_levels(now, now + timedelta(days=30), _LEVELS) == []
    assert rearm_levels(now, now - timedelta(days=1), _LEVELS) == [0, 1, 7, 14]


# ----------------------------------------------------------------------
# The sweep: levels, order, payloads, events
# ----------------------------------------------------------------------


async def test_sweep_walks_the_ladder_once_per_level(tmp_path):
    sink = WebhookSink()
    t0 = datetime.now(timezone.utc)
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_env(
            tmp_path,
            config=_config(AlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            # 60 days out: nothing crossed at arm time — the ladder
            # below is entirely future crossings
            await create_key(
                env.client, "ladder", expires_at=(t0 + timedelta(days=60)).isoformat()
            )
            assert (await _row(env.client, "ladder"))["expiry_alerts_fired"] == []

            assert await _sweep(env, t0 + timedelta(days=47)) == 1   # 14 fires
            assert await _sweep(env, t0 + timedelta(days=48)) == 0   # once, ever
            assert await _sweep(env, t0 + timedelta(days=54)) == 1   # 7
            assert await _sweep(env, t0 + timedelta(days=59, hours=12)) == 1  # 1
            assert await _sweep(env, t0 + timedelta(days=60, hours=6)) == 1   # 0 — the page that it died

            assert [p["level_days"] for p in sink.payloads] == [14, 7, 1, 0]

            # payload shape: what on-call reads
            p14 = sink.payloads[0]
            assert p14["type"] == "expiry_alert"
            assert p14["key_name"] == "ladder"
            assert p14["key_id"] == (await _row(env.client, "ladder"))["id"]
            assert 12.9 < p14["days_remaining"] <= 13.0
            assert "crossed the 14-day warning" in p14["message"]
            p0 = sink.payloads[-1]
            assert p0["days_remaining"] < 0
            assert "has expired" in p0["message"]

            # every level landed beside the webhooks as observer events
            events = await _drain(env.app)
            threshold_events = [e for e in events if e.name == "expiry.threshold"]
            assert [e.payload["level_days"] for e in threshold_events] == [14, 7, 1, 0]
            assert threshold_events[0].payload["key_name"] == "ladder"

            # and the fired state persisted on the row, restart-safe
            assert (await _row(env.client, "ladder"))["expiry_alerts_fired"] == [
                0, 1, 7, 14,
            ]


async def test_sweep_fires_multiple_levels_in_one_pass(env):
    t0 = datetime.now(timezone.utc)
    # a key the sweep first sees already past its deadline (the
    # discovery case, deadline edition): everything crossed fires,
    # highest first — one page per level, never one merged page
    await _insert_key("overdue", expires_at=t0 - timedelta(days=2))
    assert await _sweep(env, t0) == 4
    assert await _sweep(env, t0) == 0  # once


async def test_create_arms_silently(tmp_path):
    sink = WebhookSink()
    t0 = datetime.now(timezone.utc)
    async with httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)) as ac:
        async for env in _make_env(
            tmp_path,
            config=_config(AlertsConfig(webhook_url="http://sink/hook")),
            alert_client=ac,
        ):
            # born 6 days from its deadline: the crossed levels (14, 7)
            # are armed as fired — the operator just chose the deadline
            await create_key(
                env.client, "short-fuse", expires_at=(t0 + timedelta(days=6)).isoformat()
            )
            assert (await _row(env.client, "short-fuse"))["expiry_alerts_fired"] == [7, 14]
            assert sink.payloads == []  # no page for a choice just made

            # 5 days out: nothing new crossed — the 1-day page waits
            assert await _sweep(env, t0 + timedelta(days=1)) == 0
            # past the deadline: 1 and 0 cross together, one pass
            assert await _sweep(env, t0 + timedelta(days=12)) == 2
            assert [p["level_days"] for p in sink.payloads] == [1, 0]


async def test_patch_moves_deadline_out_rearms(env):
    t0 = datetime.now(timezone.utc)
    created = await create_key(
        env.client, "extend", expires_at=(t0 + timedelta(days=6)).isoformat()
    )
    assert (await _row(env.client, "extend"))["expiry_alerts_fired"] == [7, 14]

    resp = await env.client.patch(
        f"/admin/keys/{created['id']}",
        json={"expires_at": (t0 + timedelta(days=60)).isoformat()},
        headers=auth(ADMIN_KEY),
    )
    assert resp.status_code == 200
    # the re-arm is the honest one: the deadline now sits above every
    # level, so they are all live again
    assert resp.json()["expiry_alerts_fired"] == []

    # and a re-armed level genuinely re-fires on its next crossing
    assert await _sweep(env, t0 + timedelta(days=47)) == 1
    assert (await _row(env.client, "extend"))["expiry_alerts_fired"] == [14]


async def test_patch_moves_deadline_in_never_pages(env):
    t0 = datetime.now(timezone.utc)
    created = await create_key(
        env.client, "sooner", expires_at=(t0 + timedelta(days=60)).isoformat()
    )
    resp = await env.client.patch(
        f"/admin/keys/{created['id']}",
        json={"expires_at": (t0 + timedelta(days=2)).isoformat()},
        headers=auth(ADMIN_KEY),
    )
    # everything the moved-in deadline crosses at arm time (14, 7 — two
    # days do not cross the 1-day level) is armed silently; the 1 page
    # still fires when genuinely reached, the 0 page at the deadline
    assert resp.json()["expiry_alerts_fired"] == [7, 14]
    assert await _sweep(env, t0 + timedelta(days=1)) == 1  # 1 day left, genuinely crossed now
    assert await _sweep(env, t0 + timedelta(days=3)) == 1  # 0 — and only 0


async def test_sweep_discovers_preexisting_keys(env):
    t0 = datetime.now(timezone.utc)
    # fired state NULL: a key that predates the feature, 3 days out.
    # Discovery is honest — "you have a key expiring in 3 days" is the
    # exact page this tier exists for.
    await _insert_key("legacy", expires_at=t0 + timedelta(days=3))
    assert await _sweep(env, t0) == 2  # 14 and 7 fire
    assert (await _row(env.client, "legacy"))["expiry_alerts_fired"] == [7, 14]
    assert await _sweep(env, t0) == 0


async def test_revoked_and_deadlineless_never_warn(env):
    t0 = datetime.now(timezone.utc)
    await _insert_key("dead-key", expires_at=t0 + timedelta(days=1), is_active=False)
    await _insert_key("undated", expires_at=None)
    assert await _sweep(env, t0) == 0


async def test_no_webhook_counts_skipped_events_still_fire(tmp_path):
    before = (
        REGISTRY.get_sample_value(
            "gateway_alerts_total", {"kind": "expiry", "outcome": "skipped"}
        )
        or 0
    )
    t0 = datetime.now(timezone.utc)
    # no webhook URL: the POST is a no-op counted as skipped —
    # silent deadlines are visible in /metrics, and the observer
    # events still fire
    async for env in _make_env(tmp_path, config=_config()):
        await create_key(
            env.client, "unwired", expires_at=(t0 + timedelta(days=60)).isoformat()
        )
        assert await _sweep(env, t0 + timedelta(days=47)) == 1
        events = await _drain(env.app)
        fired = [e for e in events if e.name == "expiry.threshold"]
        assert len(fired) == 1
        assert fired[0].payload["level_days"] == 14
        assert fired[0].payload["days_remaining"] == 13.0
        after = (
            REGISTRY.get_sample_value(
                "gateway_alerts_total", {"kind": "expiry", "outcome": "skipped"}
            )
            or 0
        )
        assert after >= before + 1


async def test_rotation_leaves_expiry_state_alone(env):
    t0 = datetime.now(timezone.utc)
    # tier-51 interaction: rotation swaps the credential and never the
    # deadline — the expiry state is untouched, and the sweep pages on
    # schedule whichever credential the key now uses
    created = await create_key(
        env.client, "rotating-expiry", expires_at=(t0 + timedelta(days=60)).isoformat()
    )
    expires_before = (await _row(env.client, "rotating-expiry"))["expires_at"]

    resp = await env.client.post(
        f"/admin/keys/{created['id']}/rotate",
        json={"grace_seconds": 3600},
        headers=auth(ADMIN_KEY),
    )
    assert resp.status_code == 200

    row = await _row(env.client, "rotating-expiry")
    assert row["expires_at"] == expires_before
    assert row["expiry_alerts_fired"] == []
    assert await _sweep(env, t0 + timedelta(days=47)) == 1
