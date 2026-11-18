"""Key last-used tracking (tier 55) — "which keys are actually alive?"

Rotation and revocation were blind: a key row said when it was created
and what it may do, never when it was last *used*. The answer to "can I
revoke this?" was a guess (or a logs query per key). The row now
carries ``last_used_at``:

* **"Use" = a valid credential named the key and traffic is about to
  ride its policy.** Stamped at the four sites where that happens —
  both chain steps (raw credential and any JWT naming the key; the
  grace-window *previous* credential included, it is the same
  identity), playground impersonation, the batch worker's per-wave
  auth. Auth runs first in the pipeline, so cache hits and post-auth
  refusals still count: possession was proven either way.
* **The replay harness deliberately never stamps** — a replayed
  request must not mark a key alive. This is the one deliberate gap,
  and it is what makes the stamp trustworthy as an *alive* signal.
* **Throttled to one write per ~60s per key** (checked in-memory
  against the already-loaded row; a fixed guard, not a config knob —
  it is a write-amplification bound, not policy) and **best-effort**:
  a stamping failure never fails the request it observes.
* **NULL = never used** — itself a rotation candidate (or a key that
  predates tracking). ``GET /admin/keys?idle_days=N`` narrows to keys
  idle ≥ N days plus the never-used ones.

Time-travel throughout is direct row UPDATE (the house pattern from
test_key_rotation) — no sleeps, no clock injection, no config knob
to pin.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from sqlalchemy import update

from gateway.config import AuthConfig, JWTAuthConfig
from gateway.db import get_sessionmaker
from gateway.models import VirtualKey

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config
from .test_batch import _create_batch, _jsonl, _line, _upload
from .test_replay import _capture, _replay
from .test_key_rotation import _rotate

SECRET = "test-shared-secret"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


async def _set_last_used(key_id: str, **values) -> None:
    """Direct row UPDATE — the house pattern for time-traveling a
    column without waiting out a real window."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            update(VirtualKey).where(VirtualKey.id == key_id).values(**values)
        )
        await session.commit()


def _parse(ts: str | None) -> datetime | None:
    """Parse an echoed ISO timestamp, normalized to UTC the way the
    codebase does (_is_expired) — SQLite hands back naive datetimes."""
    if ts is None:
        return None
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _last_used(client, key_id: str) -> datetime | None:
    resp = await client.get(f"/admin/keys/{key_id}", headers=auth(ADMIN_KEY))
    assert resp.status_code == 200, resp.text
    return _parse(resp.json()["last_used_at"])


async def _chat(env, key: str, content: str = "Hello") -> None:
    resp = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content=content),
        headers=auth(key),
    )
    assert resp.status_code == 200, resp.text


# ----------------------------------------------------------------------
# First use, echo, and the throttle
# ----------------------------------------------------------------------


async def test_first_use_stamps_and_echoes(tmp_path):
    async for env in _make_env(tmp_path):
        # Never used: the create echo and the detail row both say so.
        row = await create_key(env.client, "fresh")
        assert row["last_used_at"] is None
        assert await _last_used(env.client, row["id"]) is None

        await _chat(env, row["key"])
        stamped = await _last_used(env.client, row["id"])
        assert stamped is not None
        now = datetime.now(timezone.utc)
        assert abs((now - stamped).total_seconds()) < 10  # ~now, UTC


async def test_throttle_skips_fresh_stamp(tmp_path):
    # 10 seconds ago is inside the ~60s interval: the request is served
    # (auth passed, traffic rode the policy) but no write lands — the
    # stamp stays where the row said it was. Pinned by time-travel, not
    # by sleeping out a window.
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "recent")
        written = datetime.now(timezone.utc) - timedelta(seconds=10)
        await _set_last_used(row["id"], last_used_at=written)
        await _chat(env, row["key"])
        after = await _last_used(env.client, row["id"])
        assert after is not None
        assert abs((after - written).total_seconds()) < 5  # unchanged
        assert (datetime.now(timezone.utc) - after).total_seconds() > 5  # not ~now


async def test_throttle_writes_past_interval(tmp_path):
    # 100 seconds ago is past the interval: the next request re-stamps.
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "stale-ish")
        await _set_last_used(
            row["id"], last_used_at=datetime.now(timezone.utc) - timedelta(seconds=100)
        )
        await _chat(env, row["key"])
        after = await _last_used(env.client, row["id"])
        assert after is not None
        assert abs((datetime.now(timezone.utc) - after).total_seconds()) < 10


# ----------------------------------------------------------------------
# The "use" definition: what counts, what deliberately doesn't
# ----------------------------------------------------------------------


async def test_cache_hit_and_post_auth_refusal_still_stamp(tmp_path):
    # Auth runs first, so both edge shapes still count as use:
    # a cache HIT served no upstream traffic, and a refused request
    # rode none of the key's policy — but possession was proven.
    async for env in _make_env(tmp_path):
        # (a) Cache hit: first request stamps None -> now...
        hitter = await create_key(env.client, "hitter")
        await _chat(env, hitter["key"], "same question")
        assert await _last_used(env.client, hitter["id"]) is not None
        # ...time-travel past the throttle, then the identical request
        # serves from the cache — and re-stamps anyway.
        await _set_last_used(
            hitter["id"],
            last_used_at=datetime.now(timezone.utc) - timedelta(seconds=100),
        )
        await _chat(env, hitter["key"], "same question")
        after = await _last_used(env.client, hitter["id"])
        assert abs((datetime.now(timezone.utc) - after).total_seconds()) < 10

        # (b) Refused after auth: the model allowlist says no (404 at
        # model resolution, well after auth) — the stamp still lands.
        denied = await create_key(env.client, "denied", allowed_models=["nomatch-*"])
        refused = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="let me in"),
            headers=auth(denied["key"]),
        )
        assert refused.status_code == 404
        assert await _last_used(env.client, denied["id"]) is not None


async def test_jwt_caller_stamps_the_named_key(tmp_path):
    # The token proves identity; the named key row supplies policy —
    # and the stamp: a JWT caller keeps its key just as alive as a raw
    # credential would.
    config = make_config(
        auth=AuthConfig(
            chain=["admin_key", "jwt", "virtual_key"],
            jwt=JWTAuthConfig(secret=SECRET),
        )
    )
    async for env in _make_env(tmp_path, config=config):
        row = await create_key(env.client, "mobile-app")
        token = pyjwt.encode(
            {"sub": "mobile-app", "exp": int(time.time()) + 600},
            SECRET,
            algorithm="HS256",
        )
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello from the token"),
            headers=auth(token),
        )
        assert resp.status_code == 200, resp.text
        stamped = await _last_used(env.client, row["id"])
        assert stamped is not None
        assert abs((datetime.now(timezone.utc) - stamped).total_seconds()) < 10


async def test_playground_impersonation_stamps(tmp_path):
    # The playground runs as the impersonated key's policy exactly as
    # a live request would — so it counts as use. No key named (the
    # admin's own context) means no row to stamp, and must not crash.
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "impersonated")
        resp = await env.client.post(
            "/admin/playground/chat",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "from the playground"}],
                "key": "impersonated",
            },
            headers=auth(ADMIN_KEY),
        )
        assert resp.status_code == 200, resp.text
        stamped = await _last_used(env.client, row["id"])
        assert stamped is not None

        # The admin's own context has no key row — nothing to stamp.
        anon = await env.client.post(
            "/admin/playground/chat",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "as myself"}],
            },
            headers=auth(ADMIN_KEY),
        )
        assert anon.status_code == 200, anon.text


async def test_batch_wave_stamps(tmp_path):
    # Background execution is still the key's traffic: the wave's auth
    # snapshot is a use, per-wave (the ~60s throttle makes per-line
    # indistinguishable for any real batch).
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "batcher")
        up = await _upload(env.client, row["key"], _jsonl(_line("req-0", "hello")))
        assert up.status_code == 200
        created = await _create_batch(env.client, row["key"], up.json()["id"])
        assert created.status_code == 200, created.text
        await env.app.state.batch_worker.tick()

        done = await env.client.get(
            f"/v1/batches/{created.json()['id']}", headers=auth(row["key"])
        )
        assert done.json()["status"] == "completed"
        stamped = await _last_used(env.client, row["id"])
        assert stamped is not None
        assert abs((datetime.now(timezone.utc) - stamped).total_seconds()) < 10


async def test_replay_does_not_stamp(tmp_path):
    # The deliberate gap: a replay re-runs recorded traffic as the
    # key, but must never mark it alive — the stamp is only written by
    # callers proving possession NOW. Time-travel past the throttle,
    # replay, and the stamp stays stale: the replay left it untouched.
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "replayed")
        await _chat(env, row["key"], "record me")
        entries = await _capture(env.client)
        assert len(entries) == 1

        stale = datetime.now(timezone.utc) - timedelta(seconds=100)
        await _set_last_used(row["id"], last_used_at=stale)

        report = await _replay(env.client, entries)
        assert report["replayed"] == 1
        assert report["unchanged"] == 1

        after = await _last_used(env.client, row["id"])
        assert abs((after - stale).total_seconds()) < 5  # untouched


async def test_grace_old_credential_stamps_same_row(tmp_path):
    # Inside the grace window the previous credential IS this key
    # (same policy, same budgets, same logs) — so its traffic stamps
    # the same row. One request, made with the OLD credential: the
    # stamp on the shared row proves the old credential counts.
    async for env in _make_env(tmp_path):
        created = await create_key(env.client, "rotating")
        old_raw = created["key"]
        rotated = await _rotate(env.client, created["id"], grace_seconds=3600)
        assert rotated["key"] != old_raw

        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="via the old credential"),
            headers=auth(old_raw),
        )
        assert resp.status_code == 200
        stamped = await _last_used(env.client, created["id"])
        assert stamped is not None
        assert abs((datetime.now(timezone.utc) - stamped).total_seconds()) < 10


# ----------------------------------------------------------------------
# The stale-key surface: idle_days
# ----------------------------------------------------------------------


async def test_idle_days_filter(tmp_path):
    async for env in _make_env(tmp_path):
        fresh = await create_key(env.client, "fresh")  # used now
        await _chat(env, fresh["key"])

        stale = await create_key(env.client, "ten-days")  # used 10d ago
        await _set_last_used(
            stale["id"],
            last_used_at=datetime.now(timezone.utc) - timedelta(days=10),
        )

        never = await create_key(env.client, "never-used")  # never

        async def _names(idle_days) -> set[str]:
            resp = await env.client.get(
                "/admin/keys", params={"idle_days": idle_days}, headers=auth(ADMIN_KEY)
            )
            assert resp.status_code == 200, resp.text
            return {k["name"] for k in resp.json()["data"]}

        # A week's idleness keeps the 10-day-old key and the never-used
        # one (a rotation candidate, not proof of death) — not the
        # fresh key.
        assert await _names(7) == {"ten-days", "never-used"}
        # Loosened to a month, only never-used survives.
        assert await _names(30) == {"never-used"}
        # 0 = everything (every key was last used >= 0 days ago).
        assert await _names(0) == {"fresh", "ten-days", "never-used"}
        # Negative is a client error — the app's 400 envelope.
        bad = await env.client.get(
            "/admin/keys", params={"idle_days": -1}, headers=auth(ADMIN_KEY)
        )
        assert bad.status_code == 400


async def test_idle_days_tenant_scoping(tmp_path):
    # The filter rides the listing's existing tenant boundary: a
    # tenant admin sweeping for stale keys sees only their own.
    async for env in _make_env(tmp_path):
        tenant = (
            await env.client.post(
                "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
            )
        ).json()
        t_key = (await create_key(env.client, "acme-key", tenant_id=tenant["id"]))["key"]
        await _chat(env, t_key)
        await create_key(env.client, "acme-never", tenant_id=tenant["id"])
        await create_key(env.client, "platform-never")  # not acme's

        acme_admin = (
            await create_key(env.client, "acme-admin", is_admin=True, tenant_id=tenant["id"])
        )["key"]

        # Tenant admin: the sweep names only this tenant's keys.
        scoped = await env.client.get(
            "/admin/keys", params={"idle_days": 0}, headers=auth(acme_admin)
        )
        assert scoped.status_code == 200
        assert {k["name"] for k in scoped.json()["data"]} == {"acme-admin", "acme-key", "acme-never"}

        # Platform admin: everyone.
        wide = await env.client.get(
            "/admin/keys", params={"idle_days": 0}, headers=auth(ADMIN_KEY)
        )
        assert "platform-never" in {k["name"] for k in wide.json()["data"]}
