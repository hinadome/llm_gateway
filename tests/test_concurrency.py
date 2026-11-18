"""Concurrency admission control: per-key/tenant in-flight caps with
priority queueing (QoS).

RPM/TPM bound the *rate* of requests; these caps bound how many may be
in-flight against the upstream at once. The slot wraps the upstream
call: acquired after the cache miss (hits and idempotent replays
never hold one), released where the in-flight gauge decrements — the
non-stream tail, the stream wrapper's finally and every error path, so
a slow upstream call holds the slot for exactly as long as it runs and
an error never leaks one. At the cap, requests QUEUE for up to
concurrency.wait_seconds, admitted highest-priority-first (FIFO
within a priority); an expired wait answers 429 + Retry-After. State
is in-process by default (the idempotency in-flight map's stance):
per-replica shares.

The Redis backend (tier 62, ``concurrency.backend: redis`` →
``RedisConcurrencyLimiter``) is tested here too. The unit tests drive
two limiter instances over one fakeredis — the multi-replica scenario
the backend exists for: fleet-shared caps, ghost-lease self-healing,
lease renewal, fail-open outages, the fleet snapshot. The e2e tests
build real apps on it, one of them TWO apps over one shared database
+ one Redis, so a single capped key row binds both "replicas" (the
trick test_two_apps_share_backends uses for the other backends).

Holding a slot from the outside: ``env.mock.delay_seconds`` makes the
upstream call itself slow (the slot is held for exactly the duration
of the call it wraps — the honest way to simulate a busy upstream).
"""

from __future__ import annotations

import asyncio
import time

import httpx
from fakeredis import FakeAsyncRedis
from prometheus_client.registry import REGISTRY
from redis.exceptions import RedisError

from gateway.config import ConcurrencyConfig, Settings
from gateway.core.redis_backends import RedisConcurrencyLimiter
from gateway.db import dispose_engine, init_engine, reset_engine_for_tests
from gateway.main import create_app

from .conftest import ADMIN_KEY, MockUpstream, _make_env, auth, chat_body, create_key, make_config


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


async def _create_tenant(client, name: str, **fields) -> dict:
    resp = await client.post(
        "/admin/tenants", json={"name": name, **fields}, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _post_chat(env, key: str, content: str = "hello", **extra):
    return await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content=content, **extra),
        headers=auth(key),
    )


# ----------------------------------------------------------------------
# The cap: held slots, queueing, timeout
# ----------------------------------------------------------------------


async def test_second_request_queues_until_slot_frees(tmp_path):
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=5.0))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "holder", max_concurrency=1))["key"]

        env.mock.delay_seconds = 1.0  # the upstream call holds the slot ~1s
        holder = asyncio.create_task(_post_chat(env, key, "slow upstream"))
        await asyncio.sleep(0.15)
        second = asyncio.create_task(_post_chat(env, key, "waiting for a slot"))
        await asyncio.sleep(0.15)

        # Mid-hold: one slot in flight, one waiter queued.
        assert _sample("gateway_concurrency_inflight", {"key": "holder"}) == 1.0
        assert _sample("gateway_concurrency_queued", {"key": "holder"}) == 1.0

        resp = await holder
        assert resp.status_code == 200
        resp = await second  # admitted the moment the holder's slot freed
        assert resp.status_code == 200

        env.mock.delay_seconds = 0.0
        assert _sample("gateway_concurrency_inflight", {"key": "holder"}) == 0.0
        assert _sample("gateway_concurrency_queued", {"key": "holder"}) == 0.0


async def test_queue_wait_timeout_429(tmp_path):
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.25))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "tiny", max_concurrency=1))["key"]

        env.mock.delay_seconds = 1.0
        holder = asyncio.create_task(_post_chat(env, key, "slow upstream"))
        await asyncio.sleep(0.15)
        rejected_before = _sample(
            "gateway_concurrency_rejected_total", {"key": "tiny"}
        )

        second = await _post_chat(env, key, "no patience")
        assert second.status_code == 429
        err = second.json()["error"]
        assert err["code"] == "rate_limit_exceeded"
        assert "max 1 in-flight request" in err["message"]  # singular, cap=1
        assert second.headers["retry-after"] == "2"  # int(1.0) + 1
        assert (
            _sample("gateway_concurrency_rejected_total", {"key": "tiny"})
            - rejected_before
        ) == 1.0

        resp = await holder
        assert resp.status_code == 200
        env.mock.delay_seconds = 0.0

        # The waiters drained: the next request serves normally.
        ok = await _post_chat(env, key, "after the dust settles")
        assert ok.status_code == 200


async def test_fail_fast_when_wait_is_zero(tmp_path):
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.0))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "fast-fail", max_concurrency=2))["key"]

        env.mock.delay_seconds = 1.0
        holders = [
            asyncio.create_task(_post_chat(env, key, f"held {i}")) for i in range(2)
        ]
        await asyncio.sleep(0.15)
        third = await _post_chat(env, key, "immediately refused")
        assert third.status_code == 429
        assert "max 2 in-flight requests" in third.json()["error"]["message"]
        for task in holders:
            assert (await task).status_code == 200
        env.mock.delay_seconds = 0.0


async def test_capless_keys_skip_admission_entirely(tmp_path):
    # No cap on the key (and no tenant) -> no admission control, no
    # queueing, no gauges: parallel requests all proceed.
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.1))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "free"))["key"]
        env.mock.delay_seconds = 0.5
        both = await asyncio.gather(
            _post_chat(env, key, "one"),
            _post_chat(env, key, "two"),
        )
        assert [r.status_code for r in both] == [200, 200]
        env.mock.delay_seconds = 0.0
        # No gauge series ever appeared for this key.
        assert _sample("gateway_concurrency_inflight", {"key": "free"}) == 0.0


async def test_disabled_via_config(tmp_path):
    config = make_config(concurrency=ConcurrencyConfig(enabled=False, wait_seconds=0.0))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "capped-but-off", max_concurrency=1))["key"]
        env.mock.delay_seconds = 0.5
        both = await asyncio.gather(
            _post_chat(env, key, "one"),
            _post_chat(env, key, "two"),
        )
        assert [r.status_code for r in both] == [200, 200]
        env.mock.delay_seconds = 0.0


# ----------------------------------------------------------------------
# Tenant pooled caps + QoS priority
# ----------------------------------------------------------------------


async def test_tenant_pooled_cap_blocks_member_keys(tmp_path):
    # The cap is the TENANT's: two different member keys contend for
    # one pooled slot, exactly like the pooled RPM window.
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.25))
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "acme", max_concurrency=1)
        k1 = (await create_key(env.client, "member-1", tenant_id=acme["id"]))["key"]
        k2 = (await create_key(env.client, "member-2", tenant_id=acme["id"]))["key"]

        env.mock.delay_seconds = 1.0
        holder = asyncio.create_task(_post_chat(env, k1, "member 1 is busy"))
        await asyncio.sleep(0.15)
        second = await _post_chat(env, k2, "other key, same pool")
        assert second.status_code == 429
        assert "max 1 in-flight request" in second.json()["error"]["message"]
        assert (await holder).status_code == 200
        env.mock.delay_seconds = 0.0


async def test_priority_admits_highest_first(tmp_path):
    # QoS: when waiters contend for the pooled tenant cap, the higher
    # priority key is admitted first — priority is a key attribute, so
    # it matters exactly where contention crosses keys.
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=5.0))
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "acme", max_concurrency=1)
        holder_k = (
            await create_key(env.client, "holder", tenant_id=acme["id"])
        )["key"]
        low_k = (
            await create_key(env.client, "low", tenant_id=acme["id"], priority=-5)
        )["key"]
        high_k = (
            await create_key(env.client, "high", tenant_id=acme["id"], priority=10)
        )["key"]

        done: list[str] = []

        async def _track(key: str, tag: str) -> None:
            resp = await _post_chat(env, key, f"request from {tag}")
            assert resp.status_code == 200, resp.text
            done.append(tag)

        env.mock.delay_seconds = 0.6
        holder = asyncio.create_task(_track(holder_k, "holder"))
        await asyncio.sleep(0.1)  # the low-priority waiter queues first
        low = asyncio.create_task(_track(low_k, "low"))
        await asyncio.sleep(0.1)  # then the high-priority one
        high = asyncio.create_task(_track(high_k, "high"))

        await asyncio.gather(holder, low, high)
        env.mock.delay_seconds = 0.0
        # The holder finished first (it started first, uncontended);
        # when its slot freed, `high` jumped the queue ahead of `low`.
        assert done[0] == "holder"
        assert done[1:] == ["high", "low"]


# ----------------------------------------------------------------------
# Slot lifetime: what holds it, what bypasses it, error paths
# ----------------------------------------------------------------------


async def test_cache_hits_bypass_admission(tmp_path):
    # Cache hits never touch the upstream, so they never need a slot:
    # a repeat request serves from cache while the key's only slot is
    # held by a slow upstream call.
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.1))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "cached", max_concurrency=1))["key"]

        first = await _post_chat(env, key, "cached content")
        assert first.status_code == 200

        env.mock.delay_seconds = 1.0
        holder = asyncio.create_task(_post_chat(env, key, "different content"))
        await asyncio.sleep(0.15)
        repeat = await _post_chat(env, key, "cached content")
        assert repeat.status_code == 200  # would 429 if it needed a slot
        assert repeat.headers["x-gateway-cache"] == "HIT"
        assert (await holder).status_code == 200
        env.mock.delay_seconds = 0.0


async def test_idempotent_replays_bypass_admission(tmp_path):
    # Same story for idempotency replays: the stored response serves
    # without a slot (phase 2b runs before 5.5).
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.1))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "careful", max_concurrency=1))["key"]

        original = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="executed once"),
            headers={**auth(key), "Idempotency-Key": "retry-1"},
        )
        assert original.status_code == 200

        env.mock.delay_seconds = 1.0
        holder = asyncio.create_task(_post_chat(env, key, "holds the only slot"))
        await asyncio.sleep(0.15)
        replay = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="executed once"),
            headers={**auth(key), "Idempotency-Key": "retry-1"},
        )
        assert replay.status_code == 200
        assert replay.headers["x-gateway-cache"] == "IDEMPOTENT"
        assert (await holder).status_code == 200
        env.mock.delay_seconds = 0.0


async def test_slot_released_when_upstream_fails(tmp_path):
    # Every attempt fails -> retries exhausted -> the request errors
    # AND releases its slot. Proof in two parts: the per-key gauge is
    # back to 0 the moment the 5xx lands, and once the upstream
    # recovers (the all-open breaker bypass still routes) the next
    # request is ADMITTED immediately — a leaked slot would have
    # queued it into a 429 instead.
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.2))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "unlucky", max_concurrency=1))["key"]
        env.mock.fail_counts["model-a"] = 10
        env.mock.fail_counts["model-b"] = 10

        boom = await _post_chat(env, key, "everything fails")
        assert boom.status_code >= 500

        # The slot came back with the error — the limiter's own gauge
        # says so (the response is only in hand after the error path's
        # _finish_span released it).
        assert _sample("gateway_concurrency_inflight", {"key": "unlucky"}) == 0.0

        # Upstream recovers; the breaker-open bypass routes there.
        env.mock.fail_counts["model-a"] = 0
        env.mock.fail_counts["model-b"] = 0
        ok = await _post_chat(env, key, "clean again")
        assert ok.status_code == 200  # admitted, not queue-blocked


async def test_streamed_response_releases_slot_on_completion(tmp_path):
    # Streams release in the stream wrapper's finally (the non-stream
    # path would never run there): a cap-1 key can stream, then
    # immediately serve the next request — the slot is not leaked.
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.2))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "streamer", max_concurrency=1))["key"]

        streamed = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="streamed", stream=True),
            headers=auth(key),
        )
        assert streamed.status_code == 200
        await streamed.aread()

        after = await _post_chat(env, key, "next request needs the freed slot")
        assert after.status_code == 200


async def test_patch_applies_to_next_request(tmp_path):
    # The cap snapshots at auth time like every other ceiling: setting
    # it via PATCH applies from the NEXT request, never mid-flight.
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=0.25))
    async for env in _make_env(tmp_path, config=config):
        key_info = await create_key(env.client, "later")
        key = key_info["key"]
        assert key_info["max_concurrency"] is None
        assert key_info["priority"] == 0

        first = await _post_chat(env, key, "before the cap exists")
        assert first.status_code == 200

        patched = await env.client.patch(
            f"/admin/keys/{key_info['id']}",
            json={"max_concurrency": 1},
            headers=auth(ADMIN_KEY),
        )
        assert patched.status_code == 200
        assert patched.json()["max_concurrency"] == 1

        env.mock.delay_seconds = 1.0
        holder = asyncio.create_task(_post_chat(env, key, "capped now"))
        await asyncio.sleep(0.15)
        second = await _post_chat(env, key, "blocked by the new cap")
        assert second.status_code == 429
        assert (await holder).status_code == 200
        env.mock.delay_seconds = 0.0


# ----------------------------------------------------------------------
# Admin surface + events
# ----------------------------------------------------------------------


async def test_admin_concurrency_view(tmp_path):
    config = make_config(concurrency=ConcurrencyConfig(wait_seconds=5.0))
    async for env in _make_env(tmp_path, config=config):
        acme = await _create_tenant(env.client, "acme", max_concurrency=1)
        key_info = await create_key(
            env.client, "watched", max_concurrency=1, tenant_id=acme["id"]
        )
        key = key_info["key"]

        env.mock.delay_seconds = 1.5
        holder = asyncio.create_task(_post_chat(env, key, "in flight"))
        await asyncio.sleep(0.1)
        queued = asyncio.create_task(_post_chat(env, key, "in queue"))
        await asyncio.sleep(0.1)

        view = (
            await env.client.get("/admin/concurrency", headers=auth(ADMIN_KEY))
        ).json()
        assert view["enabled"] is True
        assert view["wait_seconds"] == 5.0
        assert view["queued_total"] == 1
        entry = next(e for e in view["keys"] if e["key_id"] == key_info["id"])
        assert entry["key_name"] == "watched"  # id -> name join
        assert entry["inflight"] == 1
        assert entry["queued"] == 1
        assert view["tenants"] == [
            {"tenant_id": acme["id"], "inflight": 1, "tenant_name": "acme"}
        ]

        assert (await holder).status_code == 200
        assert (await queued).status_code == 200
        env.mock.delay_seconds = 0.0

        # Tenant admins never see the platform view.
        tadmin = await create_key(
            env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
        )
        denied = await env.client.get(
            "/admin/concurrency", headers=auth(tadmin["key"])
        )
        assert denied.status_code == 403


async def test_concurrency_timeout_event(tmp_path):
    from .test_events import Recorder  # local import: no circular module dep

    from gateway.config import EventHandlerConfig, EventsConfig

    config = make_config(
        concurrency=ConcurrencyConfig(wait_seconds=0.25),
        events=EventsConfig(
            handlers=[
                EventHandlerConfig(
                    class_path="tests.test_events:Recorder",
                    events=["concurrency.timeout"],
                )
            ]
        ),
    )
    async for env in _make_env(tmp_path, config=config):
        recorder = next(
            h for h in env.app.state.events.handlers if isinstance(h, Recorder)
        )
        key = (await create_key(env.client, "alarmed", max_concurrency=1))["key"]

        env.mock.delay_seconds = 1.0
        holder = asyncio.create_task(_post_chat(env, key, "slow upstream"))
        await asyncio.sleep(0.15)
        second = await _post_chat(env, key, "this wait will expire")
        assert second.status_code == 429
        await env.app.state.events.drain()

        assert [e.name for e in recorder.events] == ["concurrency.timeout"]
        payload = recorder.events[0].payload
        assert payload["key_name"] == "alarmed"
        assert payload["model"] == "test-model"
        assert payload["max_concurrency"] == 1
        assert payload["wait_seconds"] == 0.25
        assert payload["waited_seconds"] >= 0.25

        assert (await holder).status_code == 200
        env.mock.delay_seconds = 0.0


async def test_api_surface_and_validation(tmp_path):
    async for env in _make_env(tmp_path):
        # Key surface: cap + priority on create, echo on the row,
        # PATCH moves both, explicit null resets the cap (and priority
        # null means the default class 0).
        created = await create_key(
            env.client, "gold", max_concurrency=5, priority=7
        )
        assert created["max_concurrency"] == 5
        assert created["priority"] == 7

        patched = await env.client.patch(
            f"/admin/keys/{created['id']}",
            json={"max_concurrency": None, "priority": None},
            headers=auth(ADMIN_KEY),
        )
        assert patched.status_code == 200
        assert patched.json()["max_concurrency"] is None
        assert patched.json()["priority"] == 0

        # Validation: caps are positive ints, priority bounded. Body
        # validation surfaces as the OpenAI-style 400.
        for bad in ({"max_concurrency": 0}, {"priority": 1001}):
            resp = await env.client.post(
                "/admin/keys",
                json={"name": "bad", **bad},
                headers=auth(ADMIN_KEY),
            )
            assert resp.status_code == 400, bad

        # Tenant surface: the pooled cap, same shape.
        tenant = await _create_tenant(env.client, "acme", max_concurrency=8)
        assert tenant["max_concurrency"] == 8
        patched_tenant = await env.client.patch(
            f"/admin/tenants/{tenant['id']}",
            json={"max_concurrency": None},
            headers=auth(ADMIN_KEY),
        )
        assert patched_tenant.status_code == 200
        assert patched_tenant.json()["max_concurrency"] is None


# ----------------------------------------------------------------------
# The Redis backend: fleet-shared slots (tier 62)
# ----------------------------------------------------------------------
#
# Unit tests drive TWO limiter instances over one fakeredis — the
# multi-replica scenario the backend exists for. A controllable clock
# makes lease expiries deterministic; tiny poll/renew intervals keep
# the tests fast; aclose() at the end stops the sweeper/poller tasks.


class _FakeClock:
    """Controllable epoch clock: lease expiries become deterministic."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _BrokenRedis:
    """A client that is down: every pipeline() raises (Redis outage)."""

    def pipeline(self, transaction: bool = True):
        raise RedisError("redis is down")


class _FlakyRedis:
    """A working client that can go down mid-test."""

    def __init__(self, inner: FakeAsyncRedis) -> None:
        self.inner = inner
        self.broken = False

    def pipeline(self, transaction: bool = True):
        if self.broken:
            raise RedisError("redis went down")
        return self.inner.pipeline(transaction=transaction)


def _key_zset(key_id: str) -> str:
    return f"llm-gateway:conc:key:{key_id}"


async def _try_acquire(
    limiter,
    key_id: str,
    key_label: str,
    *,
    tenant_id: str | None = None,
    key_cap: int | None = 1,
    tenant_cap: int | None = None,
    priority: int = 0,
    wait_seconds: float = 0.0,
):
    return await limiter.acquire(
        key_id=key_id,
        tenant_id=tenant_id,
        key_label=key_label,
        key_cap=key_cap,
        tenant_cap=tenant_cap,
        priority=priority,
        wait_seconds=wait_seconds,
    )


async def test_redis_fleet_cap_shares_slots_across_limiters():
    """Two limiters over one Redis = one cap: A's held slot is B's
    refusal, exactly what a multi-replica fleet needs."""
    redis = FakeAsyncRedis()
    clock = _FakeClock()
    limiter_a = RedisConcurrencyLimiter(
        redis, ConcurrencyConfig(), clock=clock, poll_interval=0.02, renew_interval=0.05
    )
    limiter_b = RedisConcurrencyLimiter(
        redis, ConcurrencyConfig(), clock=clock, poll_interval=0.02, renew_interval=0.05
    )
    try:
        slot_a = await _try_acquire(limiter_a, "k1", "fa")
        assert slot_a is not None
        assert await redis.zcard(_key_zset("k1")) == 1  # the lease is fleet-visible

        # Replica B is refused: the cap binds the fleet, not the process.
        assert await _try_acquire(limiter_b, "k1", "fb") is None
        assert _sample("gateway_concurrency_rejected_total", {"key": "fb"}) == 1.0

        limiter_a.release(slot_a)
        await asyncio.sleep(0.05)  # the fire-and-forget lease removal lands
        assert await redis.zcard(_key_zset("k1")) == 0  # lease gone, zset gone

        slot_b = await _try_acquire(limiter_b, "k1", "fb2")
        assert slot_b is not None  # the freed slot is B's now
        limiter_b.release(slot_b)
    finally:
        await limiter_a.aclose()
        await limiter_b.aclose()


async def test_redis_queue_admits_on_other_replicas_release():
    """A waiter on replica B is admitted when replica A releases — the
    poller noticing a cross-replica free, the whole point of keeping
    the queue per-replica with a fleet re-check."""
    redis = FakeAsyncRedis()
    clock = _FakeClock()
    limiter_a = RedisConcurrencyLimiter(
        redis, ConcurrencyConfig(), clock=clock, poll_interval=0.02, renew_interval=0.05
    )
    limiter_b = RedisConcurrencyLimiter(
        redis, ConcurrencyConfig(), clock=clock, poll_interval=0.02, renew_interval=0.05
    )
    try:
        slot_a = await _try_acquire(limiter_a, "k1", "qa")
        assert slot_a is not None

        started = time.monotonic()
        waiter = asyncio.create_task(_try_acquire(limiter_b, "k1", "qb", wait_seconds=2.0))
        await asyncio.sleep(0.08)  # refused, now queued on B
        assert _sample("gateway_concurrency_queued", {"key": "qb"}) == 1.0

        limiter_a.release(slot_a)  # A's ZREM lands; B's poller retries
        slot_b = await waiter
        elapsed = time.monotonic() - started
        assert slot_b is not None  # admitted by the poller, not the timeout
        assert elapsed < 1.5
        limiter_b.release(slot_b)
    finally:
        await limiter_a.aclose()
        await limiter_b.aclose()


async def test_redis_ghost_leases_self_heal():
    """A crashed replica's leases (expiry passed, never released) are
    pruned by the next admission — a full-looking store must never be
    hiding ghosts. LIVE leases are never pruned."""
    redis = FakeAsyncRedis()
    clock = _FakeClock()
    limiter = RedisConcurrencyLimiter(
        redis, ConcurrencyConfig(), clock=clock, poll_interval=0.02, renew_interval=0.05
    )
    try:
        # A ghost on the key zset: expiry already passed.
        await redis.zadd(_key_zset("k1"), {"ghost": clock.now - 1.0})
        slot = await _try_acquire(limiter, "k1", "ga")  # cap 1, store looks full
        assert slot is not None  # the ghost was pruned, not counted
        members = await redis.zrange(_key_zset("k1"), 0, -1)
        decoded = {m.decode() if isinstance(m, bytes) else m for m in members}
        assert decoded == {slot.token}  # the ghost is gone

        # A LIVE lease is never pruned — it keeps refusing.
        await redis.zadd(_key_zset("k2"), {"live": clock.now + 60.0})
        assert await _try_acquire(limiter, "k2", "ga2") is None

        # Tenant zsets heal the same way.
        await redis.zadd("llm-gateway:conc:tnt:t1", {"stale-tenant": clock.now - 1.0})
        tslot = await _try_acquire(
            limiter, "k3", "ga3", tenant_id="t1", key_cap=None, tenant_cap=1
        )
        assert tslot is not None
        limiter.release(slot)
        limiter.release(tslot)
    finally:
        await limiter.aclose()


async def test_redis_renewal_extends_lease_expiry():
    """The renewal sweeper refreshes held leases, so a live call never
    ages out of its own slot: after the original expiry passes, the
    lease still holds the cap."""
    redis = FakeAsyncRedis()
    clock = _FakeClock()
    limiter = RedisConcurrencyLimiter(
        redis,
        ConcurrencyConfig(lease_ttl_seconds=2.0),
        clock=clock,
        poll_interval=0.02,
        renew_interval=0.02,
    )
    try:
        slot = await _try_acquire(limiter, "k1", "ra")
        assert slot is not None
        zset = _key_zset("k1")
        assert await redis.zscore(zset, slot.token) == 1002.0  # now + ttl

        clock.advance(1.0)  # halfway: renewal fires while the lease is live
        await asyncio.sleep(0.06)  # ≥ 2 sweeps, all with the clock at 1001.0
        assert await redis.zscore(zset, slot.token) == 1003.0  # 1001.0 + ttl

        clock.advance(1.5)  # 1002.5: past the ORIGINAL expiry
        # The renewed lease still refuses — it did not age out.
        assert await _try_acquire(limiter, "k1", "ra2") is None
        limiter.release(slot)
    finally:
        await limiter.aclose()


async def test_redis_tenant_cap_is_fleet_wide():
    """The pooled tenant cap binds across replicas: a member key on
    replica B is refused while replica A's key holds the tenant's
    only slot."""
    redis = FakeAsyncRedis()
    clock = _FakeClock()
    limiter_a = RedisConcurrencyLimiter(
        redis, ConcurrencyConfig(), clock=clock, poll_interval=0.02, renew_interval=0.05
    )
    limiter_b = RedisConcurrencyLimiter(
        redis, ConcurrencyConfig(), clock=clock, poll_interval=0.02, renew_interval=0.05
    )
    try:
        slot_a = await _try_acquire(
            limiter_a, "k1", "ta", tenant_id="t1", key_cap=5, tenant_cap=1
        )
        assert slot_a is not None
        # A different key, same tenant, other replica: the tenant pool is full.
        assert (
            await _try_acquire(limiter_b, "k2", "tb", tenant_id="t1", key_cap=5, tenant_cap=1)
            is None
        )
        # A different tenant is unaffected.
        other = await _try_acquire(
            limiter_b, "k2", "tb2", tenant_id="t2", key_cap=5, tenant_cap=1
        )
        assert other is not None

        limiter_a.release(slot_a)
        await asyncio.sleep(0.05)  # A's lease removal lands
        after = await _try_acquire(
            limiter_b, "k2", "tb3", tenant_id="t1", key_cap=5, tenant_cap=1
        )
        assert after is not None  # the tenant pool freed, fleet-wide
        limiter_b.release(other)
        limiter_b.release(after)
    finally:
        await limiter_a.aclose()
        await limiter_b.aclose()


async def test_redis_outage_fails_open_with_symmetric_release():
    """A Redis outage admits anyway (fail open — the breaker's stance),
    counted in-process so release stays symmetric: the fleet is never
    asked about a slot it never saw."""
    limiter = RedisConcurrencyLimiter(
        _BrokenRedis(), ConcurrencyConfig(), poll_interval=0.02, renew_interval=0.05
    )
    try:
        slot = await _try_acquire(
            limiter, "k1", "oa", tenant_id="t1", key_cap=1, tenant_cap=1, wait_seconds=5.0
        )
        assert slot is not None  # admitted despite the outage
        assert slot.local is True  # counted in-process, not in Redis
        assert _sample("gateway_concurrency_inflight", {"key": "oa"}) == 1.0
        assert _sample("gateway_concurrency_rejected_total", {"key": "oa"}) == 0.0

        limiter.release(slot)
        assert _sample("gateway_concurrency_inflight", {"key": "oa"}) == 0.0
        assert limiter._outage == {} and limiter._outage_tenants == {}
    finally:
        await limiter.aclose()


async def test_redis_waiter_outage_bounded_by_wait_seconds():
    """A waiter queued BEFORE an outage stays queued — the poller keeps
    retrying and admission does not fail open from the queue — until
    its own wait_seconds expires, exactly the documented stance."""
    inner = FakeAsyncRedis()
    client = _FlakyRedis(inner)
    clock = _FakeClock()
    limiter = RedisConcurrencyLimiter(
        client, ConcurrencyConfig(), clock=clock, poll_interval=0.02, renew_interval=0.05
    )
    try:
        # A live lease (held directly — another replica's call) makes
        # the first admission queue.
        await inner.zadd(_key_zset("k1"), {"held": clock.now + 60.0})
        waiter = asyncio.create_task(_try_acquire(limiter, "k1", "wa", wait_seconds=0.4))
        await asyncio.sleep(0.08)  # refused, queued
        assert _sample("gateway_concurrency_queued", {"key": "wa"}) == 1.0

        client.broken = True  # the outage hits while the waiter waits
        slot = await waiter
        assert slot is None  # the wait expired: bounded, no fail-open from the queue
        assert _sample("gateway_concurrency_rejected_total", {"key": "wa"}) == 1.0
        assert _sample("gateway_concurrency_queued", {"key": "wa"}) == 0.0
        assert limiter._waiters == []  # left the queue cleanly
    finally:
        client.broken = False
        await limiter.aclose()


async def test_redis_snapshot_is_fleet_view():
    """snapshot() reads the fleet (SCAN + ZCARD over the lease zsets),
    with this replica's queued waiters and outage admissions riding
    along — the queue and the outage counts are per-replica by
    design."""
    redis = FakeAsyncRedis()
    clock = _FakeClock()
    common = {"clock": clock, "poll_interval": 0.02, "renew_interval": 0.05}
    limiter_a = RedisConcurrencyLimiter(redis, ConcurrencyConfig(), **common)
    limiter_b = RedisConcurrencyLimiter(redis, ConcurrencyConfig(), **common)
    try:
        slot_a = await _try_acquire(
            limiter_a, "k1", "sa", tenant_id="t1", key_cap=2, tenant_cap=5
        )
        slot_b = await _try_acquire(
            limiter_b, "k2", "sb", tenant_id="t1", key_cap=2, tenant_cap=5
        )
        assert slot_a is not None and slot_b is not None

        # A live lease on k3 (held directly — another replica's call)
        # makes B's next request queue behind it.
        await redis.zadd(_key_zset("k3"), {"held": clock.now + 60.0})
        queued = asyncio.create_task(_try_acquire(limiter_b, "k3", "sq", wait_seconds=10.0))
        await asyncio.sleep(0.08)  # refused, queued on B only

        # An outage admission on A: fail open, counted locally — so it
        # rides along in A's snapshot only.
        limiter_a._client = _BrokenRedis()
        outage_slot = await _try_acquire(limiter_a, "k9", "so", key_cap=1, wait_seconds=5.0)
        limiter_a._client = redis
        assert outage_slot is not None and outage_slot.local is True

        assert await limiter_a.snapshot() == {
            "keys": [
                {"key_id": "k1", "inflight": 1, "queued": 0},
                {"key_id": "k2", "inflight": 1, "queued": 0},
                {"key_id": "k3", "inflight": 1, "queued": 0},
                {"key_id": "k9", "inflight": 1, "queued": 0},  # outage, A's only
            ],
            "tenants": [{"tenant_id": "t1", "inflight": 2}],
            "queued_total": 0,
        }
        assert await limiter_b.snapshot() == {
            "keys": [
                {"key_id": "k1", "inflight": 1, "queued": 0},
                {"key_id": "k2", "inflight": 1, "queued": 0},
                {"key_id": "k3", "inflight": 1, "queued": 1},  # B's waiter
            ],
            "tenants": [{"tenant_id": "t1", "inflight": 2}],
            "queued_total": 1,  # per-replica, like the queued column
        }

        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        limiter_a.release(slot_a)
        limiter_b.release(slot_b)
        limiter_a.release(outage_slot)
    finally:
        await limiter_a.aclose()
        await limiter_b.aclose()


async def test_redis_backend_e2e_app_leases_and_admin_view(tmp_path):
    """Through a real app: the slot is a live lease in the shared
    store, the admin view reads the fleet, and completion hands the
    lease back."""
    redis = FakeAsyncRedis()
    config = make_config(
        redis_backends=True, concurrency=ConcurrencyConfig(wait_seconds=5.0)
    )
    async for env in _make_env(tmp_path, redis_client=redis, config=config):
        info = await create_key(env.client, "held", max_concurrency=1)
        key = info["key"]

        env.mock.delay_seconds = 0.6
        holder = asyncio.create_task(_post_chat(env, key, "slow upstream"))
        await asyncio.sleep(0.15)

        # Mid-hold: the lease is fleet-visible (raw store) and the
        # admin view reads it with the id -> name join.
        assert await redis.zcard(_key_zset(info["id"])) == 1
        view = (
            await env.client.get("/admin/concurrency", headers=auth(ADMIN_KEY))
        ).json()
        assert view["enabled"] is True
        assert view["wait_seconds"] == 5.0
        entry = next(e for e in view["keys"] if e["key_id"] == info["id"])
        assert entry["key_name"] == "held"
        assert entry["inflight"] == 1
        assert entry["queued"] == 0

        assert (await holder).status_code == 200
        env.mock.delay_seconds = 0.0
        await asyncio.sleep(0.05)  # the fire-and-forget lease removal lands
        assert await redis.zcard(_key_zset(info["id"])) == 0
        view = (
            await env.client.get("/admin/concurrency", headers=auth(ADMIN_KEY))
        ).json()
        assert view["keys"] == []
        assert view["queued_total"] == 0


async def test_two_apps_share_one_concurrency_cap(tmp_path):
    """The multi-replica guarantee end to end: replica A's held slot is
    replica B's 429 — one capped key row over one shared database, one
    shared lease store (the trick test_two_apps_share_backends uses
    for the other backends)."""
    redis = FakeAsyncRedis()
    reset_engine_for_tests()
    db_url = f"sqlite+aiosqlite:///{tmp_path}/replicas.db"
    await init_engine(db_url)

    def build() -> tuple:
        mock = MockUpstream()
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock.handler))
        app = create_app(
            settings=Settings(admin_key=ADMIN_KEY, database_url=db_url),
            config=make_config(
                redis_backends=True, concurrency=ConcurrencyConfig(wait_seconds=0.0)
            ),
            http_client=http_client,
            redis_client=redis,
        )
        app.state.admin_key = ADMIN_KEY  # ASGITransport does not run the lifespan
        return app, mock, http_client

    app_a, mock_a, http_a = build()
    app_b, mock_b, http_b = build()
    try:
        client_a = httpx.AsyncClient(transport=httpx.ASGITransport(app=app_a), base_url="http://a.test")
        client_b = httpx.AsyncClient(transport=httpx.ASGITransport(app=app_b), base_url="http://b.test")

        key = (await create_key(client_a, "replica", max_concurrency=1))["key"]

        mock_a.delay_seconds = 0.6  # replica A holds the only slot
        holder = asyncio.create_task(
            client_a.post(
                "/v1/chat/completions",
                json=chat_body(content="held by replica a"),
                headers=auth(key),
            )
        )
        await asyncio.sleep(0.15)

        # Replica B sees the fleet slot count and fails fast.
        refused = await client_b.post(
            "/v1/chat/completions",
            json=chat_body(content="replica b wants in"),
            headers=auth(key),
        )
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "rate_limit_exceeded"
        assert "max 1 in-flight request" in refused.json()["error"]["message"]

        # Once A completes, the lease is gone and B is admitted.
        assert (await holder).status_code == 200
        await asyncio.sleep(0.05)  # the fire-and-forget ZREM lands
        allowed = await client_b.post(
            "/v1/chat/completions",
            json=chat_body(content="replica b wants in"),
            headers=auth(key),
        )
        assert allowed.status_code == 200
        assert len(mock_b.requests) == 1  # B reached upstream exactly once
        await client_a.aclose()
        await client_b.aclose()
    finally:
        await http_a.aclose()
        await http_b.aclose()
        await dispose_engine()
