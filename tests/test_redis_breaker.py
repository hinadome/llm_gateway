"""Tests for the Redis-backed circuit-breaker registry (shared state).

``RedisCircuitBreakerRegistry`` (``routing.breaker.backend: redis``) keeps
the same three-state machine as the in-memory registry, but the state lives
in Redis — so one replica tripping a deployment's circuit opens it for the
whole fleet, and an admin reset anywhere closes it everywhere. Units run
against fakeredis with an injectable wall clock; the e2e runs two full app
"replicas" (separate processes' worth of app + DB) sharing one Redis.
"""

from __future__ import annotations

from fakeredis import FakeAsyncRedis
from redis.exceptions import ConnectionError as RedisConnectionError

from gateway.config import BreakerConfig, RoutingConfig
from gateway.core.redis_backends import RedisCircuitBreakerRegistry
from gateway.observability import metrics

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config

THRESHOLD = 3
COOLDOWN = 30.0


class Clock:
    """Injectable wall clock (Redis state must compare across processes)."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _registry(redis, clock, **breaker_kwargs) -> RedisCircuitBreakerRegistry:
    reg = RedisCircuitBreakerRegistry(
        redis,
        RoutingConfig(
            breaker=BreakerConfig(
                failure_threshold=THRESHOLD, cooldown_seconds=COOLDOWN, **breaker_kwargs
            )
        ),
        prefix="t",
        clock=clock,
    )
    reg.register("d", "openai")
    return reg


class _DeadRedis:
    """A redis client that is down: every command raises."""

    def pipeline(self, *args, **kwargs):
        raise RedisConnectionError("redis unavailable")


# ----------------------------------------------------------------------
# Units: state-machine parity with the in-memory breaker
# ----------------------------------------------------------------------


async def test_trip_cooldown_probe_and_recovery():
    redis = FakeAsyncRedis()
    clock = Clock()
    reg = _registry(redis, clock)

    assert await reg.allow("d") is True  # no state yet: reads as closed
    for _ in range(THRESHOLD - 1):
        await reg.record_failure("d")
    assert await reg.allow("d") is True  # below threshold: still closed

    await reg.record_failure("d")  # threshold reached -> trip
    assert await reg.allow("d") is False
    snap = (await reg.snapshots())["d"]
    assert snap["state"] == "open"
    assert snap["consecutive_failures"] == 0
    assert snap["opened_count"] == 1
    assert snap["cooldown_remaining_seconds"] == COOLDOWN

    clock.advance(COOLDOWN + 1)
    # snapshot derives half-open purely from the clock (no request needed)
    snap = (await reg.snapshots())["d"]
    assert snap["state"] == "half_open"
    assert snap["cooldown_remaining_seconds"] == 0.0
    # the next request is the probe
    assert await reg.allow("d") is True

    await reg.record_success("d")  # probe succeeded -> closed
    snap = (await reg.snapshots())["d"]
    assert snap["state"] == "closed"
    assert snap["consecutive_failures"] == 0
    assert snap["opened_count"] == 1  # lifetime count survives recovery
    assert await reg.allow("d") is True


async def test_consecutive_failures_reset_on_success():
    redis = FakeAsyncRedis()
    reg = _registry(redis, Clock())

    for _ in range(2):
        await reg.record_failure("d")
    assert (await reg.snapshots())["d"]["consecutive_failures"] == 2
    await reg.record_success("d")
    assert (await reg.snapshots())["d"]["consecutive_failures"] == 0

    # the count restarted: two more failures do not trip (2 < threshold 3)
    for _ in range(2):
        await reg.record_failure("d")
    assert (await reg.snapshots())["d"]["state"] == "closed"


async def test_failed_probe_retrips_with_fresh_cooldown():
    redis = FakeAsyncRedis()
    clock = Clock()
    reg = _registry(redis, clock)

    for _ in range(THRESHOLD):
        await reg.record_failure("d")
    clock.advance(COOLDOWN + 1)
    assert await reg.allow("d") is True  # probe

    await reg.record_failure("d")  # probe failed -> re-open
    snap = (await reg.snapshots())["d"]
    assert snap["state"] == "open"
    assert snap["opened_count"] == 2
    assert snap["cooldown_remaining_seconds"] == COOLDOWN  # fresh cooldown


async def test_all_open_bypass_failure_does_not_extend_cooldown():
    """A failure recorded while already open (the router's all-open bypass)
    changes nothing: no re-trip, no cooldown extension."""
    redis = FakeAsyncRedis()
    clock = Clock()
    reg = _registry(redis, clock)

    for _ in range(THRESHOLD):
        await reg.record_failure("d")
    before = (await reg.snapshots())["d"]
    clock.advance(5.0)
    await reg.record_failure("d")  # bypass accounting
    after = (await reg.snapshots())["d"]
    assert after["opened_count"] == before["opened_count"] == 1
    assert after["cooldown_remaining_seconds"] == COOLDOWN - 5.0


async def test_reset_force_closes_and_keeps_lifetime_count():
    redis = FakeAsyncRedis()
    reg = _registry(redis, Clock())

    for _ in range(THRESHOLD):
        await reg.record_failure("d")
    await reg.reset("d")
    snap = (await reg.snapshots())["d"]
    assert snap["state"] == "closed"
    assert snap["opened_count"] == 1  # ops signal: kept across resets

    for _ in range(THRESHOLD):  # trips again -> 2 lifetime trips
        await reg.record_failure("d")
    assert (await reg.snapshots())["d"]["opened_count"] == 2

    try:
        await reg.reset("unknown")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for unregistered deployment")


async def test_state_carries_a_ttl():
    """Idle breaker state cleans itself up, but never before a cooldown
    could have elapsed."""
    redis = FakeAsyncRedis()
    reg = _registry(redis, Clock())
    await reg.record_failure("d")
    ttl = await redis.ttl("t:cb:d")
    assert 0 < ttl <= 3600


# ----------------------------------------------------------------------
# Units: shared state + failure modes
# ----------------------------------------------------------------------


async def test_state_shared_across_registries():
    """Two registries on one Redis = two replicas: one trip opens the
    circuit for both; one reset closes it for both."""
    redis = FakeAsyncRedis()
    replica_a = _registry(redis, Clock())
    replica_b = _registry(redis, Clock())

    for _ in range(THRESHOLD):
        await replica_a.record_failure("d")
    assert await replica_b.allow("d") is False  # sees A's trip
    assert (await replica_b.snapshots())["d"]["state"] == "open"

    await replica_b.reset("d")
    assert await replica_a.allow("d") is True  # sees B's reset
    assert (await replica_a.snapshots())["d"]["state"] == "closed"


async def test_disabled_registry_is_a_no_op():
    redis = FakeAsyncRedis()
    reg = _registry(redis, Clock(), enabled=False)
    for _ in range(10):
        await reg.record_failure("d")
    assert await reg.allow("d") is True
    assert (await reg.snapshots())["d"]["state"] == "closed"


async def test_redis_outage_fails_open_for_routing():
    """The breaker is protection, not correctness: with Redis down, allow()
    routes anyway and record calls are dropped (with a warning); admin
    operations surface the outage instead of lying."""
    reg = _registry(_DeadRedis(), Clock())
    assert await reg.allow("d") is True
    await reg.record_failure("d")  # dropped, must not raise
    await reg.record_success("d")
    await reg.refresh_gauges()  # gauges stay stale, must not raise
    for op in (reg.snapshots(), reg.reset("d")):
        try:
            await op
        except RedisConnectionError:
            pass
        else:
            raise AssertionError("admin ops should surface the outage")


# ----------------------------------------------------------------------
# e2e: two app replicas sharing one Redis
# ----------------------------------------------------------------------


def _shared_config() -> "make_config":
    return make_config(
        routing=RoutingConfig(
            retry_backoff_base=0.0,
            breaker=BreakerConfig(
                backend="redis", failure_threshold=THRESHOLD, cooldown_seconds=COOLDOWN
            ),
        )
    )


async def _chat(env, key: str, content: str):
    return await env.client.post(
        "/v1/chat/completions", json=chat_body(content=content), headers=auth(key)
    )


async def test_replica_trips_replica_skips_and_reset_is_shared(tmp_path):
    redis = FakeAsyncRedis()
    config = _shared_config()
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    # Replica A: three request-level failures trip the *shared* circuit
    async for env_a in _make_env(dir_a, redis_client=redis, config=config):
        key = (await create_key(env_a.client, "replica-user"))["key"]
        env_a.mock.fail_counts["model-a"] = 100
        for i in range(THRESHOLD):
            resp = await _chat(env_a, key, f"trip probe {i}")
            assert resp.status_code == 200
            assert resp.headers["X-Gateway-Deployment"] == "openai/fallback"

        # Replica B (its own app, DB and mock upstream — only Redis is
        # shared) skips the primary without ever talking to it
        async for env_b in _make_env(dir_b, redis_client=redis, config=config):
            key_b = (await create_key(env_b.client, "replica-user"))["key"]
            before = len(env_b.mock.requests)
            resp = await _chat(env_b, key_b, "replica b request")
            assert resp.status_code == 200
            assert resp.headers["X-Gateway-Deployment"] == "openai/fallback"
            # no retries burned on the primary either: circuit was open
            assert [b["model"] for b in env_b.mock.bodies()[before:]] == ["model-b"]

            # fleet-accurate admin view + gauge (B never observed the trip
            # itself — the scrape re-syncs from shared state)
            routing = (
                await env_b.client.get("/admin/routing", headers=auth(ADMIN_KEY))
            ).json()
            assert (
                routing["circuit_breaker"]["deployments"]["openai/primary"]["state"]
                == "open"
            )
            await env_b.client.get("/metrics")
            assert (
                metrics.CIRCUIT_STATE.labels(
                    deployment="openai/primary", provider="openai"
                )._value.get()
                == 2  # open
            )

            # an admin reset on B reopens traffic (for every replica)
            reset = await env_b.client.post(
                "/admin/routing/breakers/openai/primary/reset",
                headers=auth(ADMIN_KEY),
            )
            assert reset.status_code == 200
            assert reset.json()["breaker"]["state"] == "closed"

            resp = await _chat(env_b, key_b, "after shared reset")
            assert resp.status_code == 200
            assert resp.headers["X-Gateway-Deployment"] == "openai/primary"
            break
        break
