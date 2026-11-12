"""Redis-backed rate limiter + response cache.

Unit tests run against ``fakeredis`` (no server needed); the two e2e tests
build real apps on the Redis backends — including two apps sharing one
client, which is the multi-replica scenario the backends exist for.
"""

from __future__ import annotations

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from gateway.core.redis_backends import RedisRateLimiter, RedisResponseCache
from gateway.db import dispose_engine, init_engine, reset_engine_for_tests
from gateway.main import create_app
from gateway.config import Settings

from .conftest import ADMIN_KEY, MockUpstream, auth, chat_body, create_key, make_config


class Clock:
    """Controllable clock: sliding windows and LRU order become deterministic."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def redis() -> FakeAsyncRedis:
    return FakeAsyncRedis()


@pytest.fixture
def clock() -> Clock:
    return Clock()


# ----------------------------------------------------------------------
# Rate limiter semantics (parity with the in-memory implementation)
# ----------------------------------------------------------------------


async def test_rpm_sliding_window(redis, clock):
    limiter = RedisRateLimiter(redis, prefix="t", clock=clock)
    assert (await limiter.check("k", rpm_limit=2, tpm_limit=None))[0] is True
    await limiter.record_request("k")
    await limiter.record_request("k")
    allowed, retry_after = await limiter.check("k", rpm_limit=2, tpm_limit=None)
    assert allowed is False
    assert 0 < retry_after <= 60

    clock.advance(61)  # window slides, old requests fall out
    assert (await limiter.check("k", rpm_limit=2, tpm_limit=None))[0] is True


async def test_tpm_sliding_window(redis, clock):
    limiter = RedisRateLimiter(redis, prefix="t", clock=clock)
    await limiter.record_usage("k", 500)
    allowed, _ = await limiter.check("k", rpm_limit=None, tpm_limit=1000, estimated_tokens=600)
    assert allowed is False
    allowed, _ = await limiter.check("k", rpm_limit=None, tpm_limit=1000, estimated_tokens=400)
    assert allowed is True

    clock.advance(61)
    allowed, _ = await limiter.check("k", rpm_limit=None, tpm_limit=1000, estimated_tokens=600)
    assert allowed is True


async def test_rpm_and_tpm_checked_together(redis, clock):
    limiter = RedisRateLimiter(redis, prefix="t", clock=clock)
    await limiter.record_usage("k", 900)
    allowed, retry_after = await limiter.check(
        "k", rpm_limit=5, tpm_limit=1000, estimated_tokens=200
    )
    assert allowed is False
    assert 0 < retry_after <= 60


async def test_no_limits_touches_nothing(redis, clock):
    limiter = RedisRateLimiter(redis, prefix="t", clock=clock)
    assert (await limiter.check("k", rpm_limit=None, tpm_limit=None)) == (True, 0.0)
    assert await redis.dbsize() == 0


async def test_windows_are_per_key(redis, clock):
    limiter = RedisRateLimiter(redis, prefix="t", clock=clock)
    await limiter.record_request("k1")
    await limiter.record_request("k1")
    assert (await limiter.check("k2", rpm_limit=1, tpm_limit=None))[0] is True


async def test_record_usage_ignores_non_positive(redis, clock):
    limiter = RedisRateLimiter(redis, prefix="t", clock=clock)
    await limiter.record_usage("k", 0)
    allowed, _ = await limiter.check("k", rpm_limit=None, tpm_limit=10, estimated_tokens=5)
    assert allowed is True


async def test_state_is_shared_across_instances(redis, clock):
    """Two limiter instances (think: two replicas) see one window."""
    replica_a = RedisRateLimiter(redis, prefix="t", clock=clock)
    replica_b = RedisRateLimiter(redis, prefix="t", clock=clock)

    await replica_a.record_request("k")
    # RPM=1: exhausted by replica A's request...
    allowed, _ = await replica_b.check("k", rpm_limit=1, tpm_limit=None)
    assert allowed is False

    await replica_a.record_usage("k2", 900)
    allowed, _ = await replica_b.check("k2", rpm_limit=None, tpm_limit=1000, estimated_tokens=200)
    assert allowed is False


# ----------------------------------------------------------------------
# Cache semantics
# ----------------------------------------------------------------------


async def test_cache_roundtrip_and_ttl(redis, clock):
    cache = RedisResponseCache(redis, ttl_seconds=60, max_entries=10, prefix="t", clock=clock)
    response = {"id": "chatcmpl-1", "choices": [{"message": {"content": "hi"}}]}
    await cache.put("k", response)
    assert await cache.get("k") == response
    # the stored entry carries a server-side TTL
    key = next(iter(await redis.keys("t:cache:*")))
    assert key != b"t:cache:index"
    assert await redis.ttl(key) > 0
    # misses return None
    assert await cache.get("nope") is None


async def test_disabled_cache_is_noop(redis):
    cache = RedisResponseCache(redis, enabled=False, ttl_seconds=60, max_entries=10, prefix="t")
    await cache.put("k", {"a": 1})
    assert await cache.get("k") is None
    assert await redis.dbsize() == 0


async def test_cache_lru_eviction(redis, clock):
    cache = RedisResponseCache(redis, ttl_seconds=60, max_entries=2, prefix="t", clock=clock)
    clock.advance(1)
    await cache.put("a", {"v": 1})
    clock.advance(1)
    await cache.put("b", {"v": 2})
    clock.advance(1)
    await cache.get("a")  # touch a -> most recently used
    clock.advance(1)
    await cache.put("c", {"v": 3})  # evicts b, not a
    assert await cache.get("a") == {"v": 1}
    assert await cache.get("b") is None
    assert await cache.get("c") == {"v": 3}


async def test_cache_clear(redis, clock):
    cache = RedisResponseCache(redis, ttl_seconds=60, max_entries=10, prefix="t", clock=clock)
    await cache.put("a", {"v": 1})
    await cache.put("b", {"v": 2})
    await cache.clear()
    assert await cache.get("a") is None
    assert await redis.dbsize() == 0


# ----------------------------------------------------------------------
# End to end through the app (fakeredis-injected backends)
# ----------------------------------------------------------------------


async def test_app_with_redis_backends(tmp_path):
    redis = FakeAsyncRedis()
    async for ns in _make_env_with_redis(tmp_path, redis):
        key = (await create_key(ns.client, "redis-e2e", rpm_limit=2))["key"]

        first = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello redis"),
            headers=auth(key),
        )
        assert first.status_code == 200
        assert first.headers["X-Gateway-Cache"] == "MISS"

        # exact repeat: served from the shared Redis cache
        second = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello redis"),
            headers=auth(key),
        )
        assert second.status_code == 200
        assert second.headers["X-Gateway-Cache"] == "HIT"
        assert len(ns.mock.requests) == 1  # cache hit never reached upstream

        # RPM=2 (miss + hit both count): a third request is rejected
        third = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="a different prompt"),
            headers=auth(key),
        )
        assert third.status_code == 429
        assert "retry-after" in {k.lower() for k in third.headers}

        # and the rate-limit window really lives in Redis
        assert await redis.exists("llm-gateway:rl:") or await redis.keys("llm-gateway:rl:*")


async def test_two_apps_share_backends(tmp_path):
    """Two app instances over one Redis = the multi-replica guarantee.

    Cache entries written by replica A are replayed by replica B; the RPM
    budget is consumed jointly.
    """
    redis = FakeAsyncRedis()
    reset_engine_for_tests()
    db_url = f"sqlite+aiosqlite:///{tmp_path}/replicas.db"
    await init_engine(db_url)

    def build() -> tuple:
        mock = MockUpstream()
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock.handler))
        app = create_app(
            settings=Settings(admin_key=ADMIN_KEY, database_url=db_url),
            config=make_config(redis_backends=True),
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

        key = (await create_key(client_a, "replica", rpm_limit=10))["key"]

        # replica A serves the request (cache MISS, upstream called on A only)
        first = await client_a.post(
            "/v1/chat/completions",
            json=chat_body(content="shared cache entry"),
            headers=auth(key),
        )
        assert first.status_code == 200
        assert first.headers["X-Gateway-Cache"] == "MISS"

        # replica B replays it from the shared Redis cache
        second = await client_b.post(
            "/v1/chat/completions",
            json=chat_body(content="shared cache entry"),
            headers=auth(key),
        )
        assert second.status_code == 200
        assert second.headers["X-Gateway-Cache"] == "HIT"
        assert mock_b.requests == []  # B never called upstream

        # replica B sees the RPM requests recorded by A (10 - 2 = 8 left,
        # but cache replays count toward RPM too — 8 more, then blocked)
        for _ in range(8):
            resp = await client_b.post(
                "/v1/chat/completions",
                json=chat_body(content="shared cache entry"),
                headers=auth(key),
            )
            assert resp.status_code == 200
        blocked = await client_b.post(
            "/v1/chat/completions",
            json=chat_body(content="shared cache entry"),
            headers=auth(key),
        )
        assert blocked.status_code == 429
        await client_a.aclose()
        await client_b.aclose()
    finally:
        await http_a.aclose()
        await http_b.aclose()
        await dispose_engine()


async def _make_env_with_redis(tmp_path, redis):
    """The standard test env, but on the Redis backends with one shared client."""
    from .conftest import _make_env

    async for ns in _make_env(tmp_path, redis_client=redis, config=make_config(redis_backends=True)):
        yield ns
