"""Tests for the semantic (similarity) response cache."""

from __future__ import annotations

import httpx
import math

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client.registry import REGISTRY

from gateway.config import SemanticCacheConfig, Settings
from gateway.core.cache import cache_key_for
from gateway.core.redis_backends import RedisSemanticCache
from gateway.core.semantic_cache import SemanticCache, cosine_similarity
from gateway.db import dispose_engine, init_engine, reset_engine_for_tests
from gateway.main import create_app
from gateway.schemas import ChatCompletionRequest

from .conftest import (
    ADMIN_KEY,
    MockUpstream,
    _make_env,
    auth,
    chat_body,
    create_key,
    make_config,
)


def cache_count(result: str) -> float:
    return REGISTRY.get_sample_value("gateway_cache_hits_total", {"result": result}) or 0


def _request(content: str, model: str = "test-model") -> ChatCompletionRequest:
    return ChatCompletionRequest(**chat_body(content=content, model=model))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _fake_embed(text: str) -> list[float]:
    return [1.0, 0.0]


# ----------------------------------------------------------------------
# cosine_similarity (pure)
# ----------------------------------------------------------------------


def test_cosine_similarity_basics():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert cosine_similarity([3.0, 4.0], [6.0, 8.0]) == pytest.approx(1.0)  # scale-free
    assert cosine_similarity([1.0, 0.0], [1.0, 1.0]) == pytest.approx(1 / math.sqrt(2))


def test_cosine_similarity_degenerate():
    assert cosine_similarity([], []) == 0.0
    assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0  # length mismatch
    assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0  # zero vector


# ----------------------------------------------------------------------
# SemanticCache unit behaviour (explicit vectors, injectable clock)
# ----------------------------------------------------------------------


async def test_put_get_roundtrip_and_threshold():
    cache = SemanticCache(SemanticCacheConfig(threshold=0.95), _fake_embed)
    await cache.put("m", _request("hello"), [1.0, 0.0], {"id": "r1"})
    # identical direction -> hit
    assert await cache.get("m", [2.0, 0.0]) == {"id": "r1"}
    # cosine([1,0],[1,1]) ~ 0.707 -> below threshold
    assert await cache.get("m", [1.0, 1.0]) is None
    # relaxed threshold accepts it
    relaxed = SemanticCache(SemanticCacheConfig(threshold=0.7), _fake_embed)
    await relaxed.put("m", _request("hello"), [1.0, 0.0], {"id": "r1"})
    assert await relaxed.get("m", [1.0, 1.0]) == {"id": "r1"}


async def test_get_picks_best_match():
    cache = SemanticCache(SemanticCacheConfig(threshold=0.9), _fake_embed)
    await cache.put("m", _request("a"), [1.0, 0.0], {"id": "weak"})
    await cache.put("m", _request("b"), [0.99, 0.141], {"id": "strong"})
    # closer to the second entry
    assert await cache.get("m", [0.98, 0.2]) == {"id": "strong"}


async def test_model_isolation():
    cache = SemanticCache(SemanticCacheConfig(), _fake_embed)
    await cache.put("model-a", _request("hello"), [1.0, 0.0], {"id": "r1"})
    assert await cache.get("model-b", [1.0, 0.0]) is None
    assert await cache.get("model-a", [1.0, 0.0]) == {"id": "r1"}


async def test_ttl_expiry():
    clock = FakeClock()
    cache = SemanticCache(SemanticCacheConfig(ttl_seconds=10), _fake_embed, clock=clock)
    await cache.put("m", _request("hello"), [1.0, 0.0], {"id": "r1"})
    assert await cache.get("m", [1.0, 0.0]) == {"id": "r1"}
    clock.now += 10.1
    assert await cache.get("m", [1.0, 0.0]) is None


async def test_per_entry_ttl_and_freshness_window():
    # Per-key cache policy (tier 54), both sides: a writer's own window
    # stamps the entry (a LONGER window than the configured global
    # outlives it), and a reader's max_age bounds what it accepts
    # without deleting the entry.
    clock = FakeClock()
    cache = SemanticCache(SemanticCacheConfig(ttl_seconds=10), _fake_embed, clock=clock)
    await cache.put("m", _request("hello"), [1.0, 0.0], {"id": "long"}, ttl_seconds=30)
    clock.now += 15  # past the global 10, inside the writer's 30
    assert await cache.get("m", [1.0, 0.0]) == {"id": "long"}
    # ...but 15s old is too stale for a 5-second caller — a freshness
    # miss that leaves the entry in place for wider-window readers.
    assert await cache.get("m", [1.0, 0.0], max_age=5) is None
    assert await cache.get("m", [1.0, 0.0], max_age=15) == {"id": "long"}
    clock.now += 16  # 31s total: past the writer's window — expired
    assert await cache.get("m", [1.0, 0.0]) is None


async def test_lru_eviction_bound():
    cache = SemanticCache(SemanticCacheConfig(max_entries=2), _fake_embed)
    await cache.put("m", _request("one"), [1.0, 0.0], {"id": "r1"})
    await cache.put("m", _request("two"), [0.0, 1.0], {"id": "r2"})
    await cache.put("m", _request("three"), [1.0, 1.0], {"id": "r3"})
    # oldest entry ("one") was evicted; the others are not similar enough
    # to a [1,0] query (orthogonal and 0.707) to mask the eviction.
    assert await cache.get("m", [1.0, 0.0]) is None
    assert await cache.get("m", [0.0, 1.0]) == {"id": "r2"}


async def test_reput_same_request_replaces():
    cache = SemanticCache(SemanticCacheConfig(), _fake_embed)
    await cache.put("m", _request("hello"), [1.0, 0.0], {"id": "old"})
    await cache.put("m", _request("hello"), [1.0, 0.1], {"id": "new"})
    assert await cache.get("m", [1.0, 0.1]) == {"id": "new"}
    assert len(cache._entries) == 1


async def test_embed_failure_returns_none():
    async def broken(text: str) -> list[float]:
        raise RuntimeError("embedder down")

    cache = SemanticCache(SemanticCacheConfig(), broken)
    assert await cache.embed(_request("hello")) is None


# ----------------------------------------------------------------------
# End-to-end through the app (deterministic fake embedder)
# ----------------------------------------------------------------------


async def _topic_embedder(text: str) -> list[float]:
    """[1,0] for topicA payloads, [0,1] for everything else."""
    return [1.0, 0.0] if "topicA" in text else [0.0, 1.0]


async def _make_semantic_env(tmp_path, **cfg):
    config = make_config(
        semantic_cache=SemanticCacheConfig(enabled=True, threshold=0.9, **cfg)
    )
    async for ns in _make_env(tmp_path, config=config, semantic_embedder=_topic_embedder):
        yield ns


async def test_semantic_hit_e2e(tmp_path):
    async for ns in _make_semantic_env(tmp_path):
        key = await create_key(ns.client, "sem")
        before = cache_count("semantic")

        r1 = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="topicA first phrasing"),
            headers=auth(key["key"]),
        )
        assert r1.status_code == 200
        assert r1.headers["X-Gateway-Cache"] == "MISS"

        # Different exact request (payload differs -> exact miss), same
        # topic -> same embedding -> served from the semantic cache.
        r2 = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="topicA second phrasing"),
            headers=auth(key["key"]),
        )
        assert r2.status_code == 200
        assert r2.headers["X-Gateway-Cache"] == "SEMANTIC"
        assert r2.json()["choices"][0]["message"]["content"] == (
            r1.json()["choices"][0]["message"]["content"]
        )
        assert cache_count("semantic") == before + 1

        # Only the first request reached upstream.
        completions = [b for b in ns.mock.bodies() if "messages" in b]
        assert len(completions) == 1

        # The hit is recorded as a cache hit in the log rows.
        logs = await ns.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        rows = logs.json()["data"]
        hits = [r for r in rows if r.get("cache_hit")]
        assert len(hits) == 1


async def test_exact_hit_takes_precedence(tmp_path):
    async for ns in _make_semantic_env(tmp_path):
        key = await create_key(ns.client, "sem")
        body = chat_body(content="topicA repeated")
        r1 = await ns.client.post("/v1/chat/completions", json=body, headers=auth(key["key"]))
        r2 = await ns.client.post("/v1/chat/completions", json=body, headers=auth(key["key"]))
        assert r1.headers["X-Gateway-Cache"] == "MISS"
        assert r2.headers["X-Gateway-Cache"] == "HIT"  # exact, not semantic
        assert len([b for b in ns.mock.bodies() if "messages" in b]) == 1


async def test_semantic_miss_on_different_topic(tmp_path):
    async for ns in _make_semantic_env(tmp_path):
        key = await create_key(ns.client, "sem")
        r1 = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="topicA question"),
            headers=auth(key["key"]),
        )
        r2 = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="something entirely different"),
            headers=auth(key["key"]),
        )
        assert r1.headers["X-Gateway-Cache"] == "MISS"
        assert r2.headers["X-Gateway-Cache"] == "MISS"
        assert len([b for b in ns.mock.bodies() if "messages" in b]) == 2


async def test_semantic_model_isolation_e2e(tmp_path):
    async for ns in _make_semantic_env(tmp_path):
        key = await create_key(ns.client, "sem")
        r1 = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="topicA on model a"),
            headers=auth(key["key"]),
        )
        # Same content, other routing group: no cross-model matching.
        r2 = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="topicA on model b", model="other-model"),
            headers=auth(key["key"]),
        )
        assert r1.headers["X-Gateway-Cache"] == "MISS"
        assert r2.headers["X-Gateway-Cache"] == "MISS"
        assert len([b for b in ns.mock.bodies() if "messages" in b]) == 2


async def test_semantic_stream_replay(tmp_path):
    async for ns in _make_semantic_env(tmp_path):
        key = await create_key(ns.client, "sem")

        r1 = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="topicA streamed once", stream=True),
            headers=auth(key["key"]),
        )
        assert r1.status_code == 200
        assert r1.headers["X-Gateway-Cache"] == "MISS"
        first_body = r1.text

        r2 = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="topicA streamed differently", stream=True),
            headers=auth(key["key"]),
        )
        assert r2.status_code == 200
        assert r2.headers["X-Gateway-Cache"] == "SEMANTIC"
        # Same assembled content, replayed as a synthetic single chunk.
        assert "Hello!" in r2.text
        assert r2.text.endswith("data: [DONE]\n\n")
        assert len([b for b in ns.mock.bodies() if "messages" in b]) == 1
        # The first stream arrived chunked ("Hel" + "lo!"); the replay is a
        # synthetic single chunk carrying the whole content.
        assert '"Hel"' in first_body and '"lo!"' in first_body
        assert '"Hello!"' not in first_body
        assert '"Hello!"' in r2.text


async def test_semantic_threshold_configurable(tmp_path):
    # [1,0] vs [1,1] cosine ~ 0.707: below the default, above a relaxed one.
    async def embedder(text: str) -> list[float]:
        return [1.0, 1.0] if "diag" in text else [1.0, 0.0]

    async for ns in _make_env(
        tmp_path,
        config=make_config(
            semantic_cache=SemanticCacheConfig(enabled=True, threshold=0.7)
        ),
        semantic_embedder=embedder,
    ):
        key = await create_key(ns.client, "sem")
        r1 = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="axis request"),
            headers=auth(key["key"]),
        )
        r2 = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="diag request"),
            headers=auth(key["key"]),
        )
        assert r1.headers["X-Gateway-Cache"] == "MISS"
        assert r2.headers["X-Gateway-Cache"] == "SEMANTIC"


async def test_semantic_embedder_failure_degrades_to_miss(tmp_path):
    async def broken(text: str) -> list[float]:
        raise RuntimeError("embedder down")

    async for ns in _make_env(
        tmp_path,
        config=make_config(semantic_cache=SemanticCacheConfig(enabled=True)),
        semantic_embedder=broken,
    ):
        key = await create_key(ns.client, "sem")
        for content in ("first try", "second try"):
            r = await ns.client.post(
                "/v1/chat/completions",
                json=chat_body(content=content),
                headers=auth(key["key"]),
            )
            assert r.status_code == 200
            assert r.headers["X-Gateway-Cache"] == "MISS"
        # Nothing was stored, so both requests hit upstream.
        assert len([b for b in ns.mock.bodies() if "messages" in b]) == 2


async def test_semantic_disabled_by_default(tmp_path):
    async for ns in _make_env(tmp_path):  # no semantic config -> disabled
        key = await create_key(ns.client, "sem")
        for content in ("plain one", "plain two"):
            r = await ns.client.post(
                "/v1/chat/completions",
                json=chat_body(content=content),
                headers=auth(key["key"]),
            )
            assert r.headers["X-Gateway-Cache"] == "MISS"
        assert len([b for b in ns.mock.bodies() if "messages" in b]) == 2


async def test_semantic_real_router_embedder_wiring(tmp_path):
    """No injected embedder: the router-based one must work end to end.

    The mock upstream's embeddings are pseudo-random per payload, so this
    test uses a degenerate threshold (-1: any stored entry matches) — it
    proves the wiring (embed via router.embed -> store -> similarity
    lookup), not the math (covered by the unit tests above).
    """
    async for ns in _make_env(
        tmp_path,
        config=make_config(
            semantic_cache=SemanticCacheConfig(
                enabled=True, model="other-model", threshold=-1.0
            )
        ),
    ):
        key = await create_key(ns.client, "sem")
        r1 = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="wiring check one"),
            headers=auth(key["key"]),
        )
        assert r1.status_code == 200
        assert r1.headers["X-Gateway-Cache"] == "MISS"

        # The embedder really routed an embeddings call through the router
        # (public name "other-model" -> deployment openai/other -> model-c).
        embedding_calls = [b for b in ns.mock.bodies() if "input" in b]
        assert len(embedding_calls) == 1
        assert embedding_calls[0]["model"] == "model-c"

        r2 = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="wiring check two"),
            headers=auth(key["key"]),
        )
        assert r2.headers["X-Gateway-Cache"] == "SEMANTIC"
        # 2 embedder calls (one per request) + exactly 1 chat completion.
        assert len([b for b in ns.mock.bodies() if "input" in b]) == 2
        assert len([b for b in ns.mock.bodies() if "messages" in b]) == 1


async def test_semantic_model_without_deployment_disables(tmp_path, caplog):
    config = make_config(
        semantic_cache=SemanticCacheConfig(enabled=True, model="no-such-model")
    )
    async for ns in _make_env(tmp_path, config=config):
        key = await create_key(ns.client, "sem")
        for content in ("a", "b"):
            r = await ns.client.post(
                "/v1/chat/completions",
                json=chat_body(content=content),
                headers=auth(key["key"]),
            )
            assert r.status_code == 200
            assert r.headers["X-Gateway-Cache"] == "MISS"
        # No embeddings call was attempted (layer disabled at startup).
        assert len([b for b in ns.mock.bodies() if "input" in b]) == 0


# ----------------------------------------------------------------------
# Redis-backed store (tier 59): parity with the in-memory implementation,
# plus the sharing the backend exists for. Unit tests run against
# fakeredis; the e2e tests inject it into real apps.
# ----------------------------------------------------------------------


async def test_redis_put_get_roundtrip_and_threshold():
    redis = FakeAsyncRedis()
    cache = RedisSemanticCache(
        redis, SemanticCacheConfig(threshold=0.95), _fake_embed, prefix="t", clock=FakeClock()
    )
    await cache.put("m", _request("hello"), [1.0, 0.0], {"id": "r1"})
    # identical direction -> hit
    assert await cache.get("m", [2.0, 0.0]) == {"id": "r1"}
    # cosine([1,0],[1,1]) ~ 0.707 -> below threshold
    assert await cache.get("m", [1.0, 1.0]) is None


async def test_redis_get_picks_best_match():
    redis = FakeAsyncRedis()
    clock = FakeClock()
    cache = RedisSemanticCache(
        redis, SemanticCacheConfig(threshold=0.9), _fake_embed, prefix="t", clock=clock
    )
    await cache.put("m", _request("a"), [1.0, 0.0], {"id": "weak"})
    await cache.put("m", _request("b"), [0.99, 0.141], {"id": "strong"})
    # closer to the second entry
    assert await cache.get("m", [0.98, 0.2]) == {"id": "strong"}


async def test_redis_model_isolation():
    redis = FakeAsyncRedis()
    cache = RedisSemanticCache(redis, SemanticCacheConfig(), _fake_embed, prefix="t")
    await cache.put("model-a", _request("hello"), [1.0, 0.0], {"id": "r1"})
    assert await cache.get("model-b", [1.0, 0.0]) is None
    assert await cache.get("model-a", [1.0, 0.0]) == {"id": "r1"}


async def test_redis_policy_token_isolation():
    # A hit skips response guardrails, so entries never cross policy
    # buckets (the same contract the in-memory store keeps).
    redis = FakeAsyncRedis()
    cache = RedisSemanticCache(redis, SemanticCacheConfig(), _fake_embed, prefix="t")
    await cache.put("m", _request("hello"), [1.0, 0.0], {"id": "r1"}, "policy-a")
    assert await cache.get("m", [1.0, 0.0], "policy-b") is None
    assert await cache.get("m", [1.0, 0.0], "policy-a") == {"id": "r1"}


async def test_redis_per_entry_ttl_and_freshness_window():
    # Per-key cache policy (tier 54), both sides: the writer's own
    # window rides the entry's Redis EX (pinned here — server-side real
    # time, not clock-testable), and a reader's max_age bounds what it
    # accepts from the payload's stored write time — a freshness miss
    # leaves the entry for wider-window readers.
    redis = FakeAsyncRedis()
    clock = FakeClock()
    cache = RedisSemanticCache(
        redis, SemanticCacheConfig(ttl_seconds=10), _fake_embed, prefix="t", clock=clock
    )
    req = _request("hello")
    await cache.put("m", req, [1.0, 0.0], {"id": "long"}, ttl_seconds=30)
    entry_key = f"t:sem:m||{cache_key_for(req)}".encode()
    assert await redis.ttl(entry_key) == 30
    clock.now += 5
    assert await cache.get("m", [1.0, 0.0], max_age=4) is None  # too stale for a 4s caller
    assert await cache.get("m", [1.0, 0.0], max_age=5) == {"id": "long"}


async def test_redis_lru_eviction_bound():
    redis = FakeAsyncRedis()
    clock = FakeClock()
    cache = RedisSemanticCache(
        redis, SemanticCacheConfig(max_entries=2), _fake_embed, prefix="t", clock=clock
    )
    await cache.put("m", _request("one"), [1.0, 0.0], {"id": "r1"})
    clock.now += 1
    await cache.put("m", _request("two"), [0.0, 1.0], {"id": "r2"})
    clock.now += 1
    await cache.put("m", _request("three"), [1.0, 1.0], {"id": "r3"})
    # oldest entry ("one") was evicted; the others are not similar enough
    # to a [1,0] query (orthogonal and 0.707) to mask the eviction.
    assert await cache.get("m", [1.0, 0.0]) is None
    assert await cache.get("m", [0.0, 1.0]) == {"id": "r2"}
    assert await redis.zcard("t:sem:index") == 2


async def test_redis_reput_same_request_replaces():
    redis = FakeAsyncRedis()
    cache = RedisSemanticCache(redis, SemanticCacheConfig(), _fake_embed, prefix="t")
    await cache.put("m", _request("hello"), [1.0, 0.0], {"id": "old"})
    await cache.put("m", _request("hello"), [1.0, 0.1], {"id": "new"})
    assert await cache.get("m", [1.0, 0.1]) == {"id": "new"}
    # one entry + the index — a reput replaces, never duplicates
    assert await redis.zcard("t:sem:index") == 1
    assert await redis.dbsize() == 2


async def test_redis_tombstone_pruning():
    # An entry whose EX passed (or that was evicted mid-index) leaves a
    # tombstone in the index; the next lookup that walks it prunes it.
    redis = FakeAsyncRedis()
    cache = RedisSemanticCache(redis, SemanticCacheConfig(), _fake_embed, prefix="t")
    req = _request("hello")
    await cache.put("m", req, [1.0, 0.0], {"id": "r1"})
    assert await cache.get("m", [1.0, 0.0]) == {"id": "r1"}
    await redis.delete(f"t:sem:m||{cache_key_for(req)}".encode())  # simulate EX passing
    assert await redis.zcard("t:sem:index") == 1  # tombstone lingers...
    assert await cache.get("m", [1.0, 0.0]) is None
    assert await redis.zcard("t:sem:index") == 0  # ...pruned by the lookup


async def test_redis_clear():
    redis = FakeAsyncRedis()
    cache = RedisSemanticCache(redis, SemanticCacheConfig(), _fake_embed, prefix="t")
    await cache.put("m", _request("a"), [1.0, 0.0], {"id": "r1"})
    await cache.put("m", _request("b"), [0.0, 1.0], {"id": "r2"})
    await cache.clear()
    assert await cache.get("m", [1.0, 0.0]) is None
    assert await redis.dbsize() == 0


async def test_redis_embed_failure_returns_none():
    async def broken(text: str) -> list[float]:
        raise RuntimeError("embedder down")

    redis = FakeAsyncRedis()
    cache = RedisSemanticCache(redis, SemanticCacheConfig(), broken, prefix="t")
    assert await cache.embed(_request("hello")) is None


async def test_redis_two_stores_share_entries():
    """Two stores over one client = two replicas' stores: what A
    stores, B serves — the whole point of the backend."""
    redis = FakeAsyncRedis()
    cfg = SemanticCacheConfig()
    replica_a = RedisSemanticCache(redis, cfg, _fake_embed, prefix="t")
    replica_b = RedisSemanticCache(redis, cfg, _fake_embed, prefix="t")
    await replica_a.put("m", _request("hello"), [1.0, 0.0], {"id": "shared"})
    assert await replica_b.get("m", [2.0, 0.0]) == {"id": "shared"}


# -- e2e through the app (fakeredis-injected backend) ---------------------


async def test_redis_semantic_hit_e2e(tmp_path):
    redis = FakeAsyncRedis()
    config = make_config(
        redis_backends=True,
        semantic_cache=SemanticCacheConfig(enabled=True, threshold=0.9),
    )
    async for ns in _make_env(
        tmp_path, config=config, semantic_embedder=_topic_embedder, redis_client=redis
    ):
        key = await create_key(ns.client, "sem")

        r1 = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="topicA first phrasing"),
            headers=auth(key["key"]),
        )
        assert r1.status_code == 200
        assert r1.headers["X-Gateway-Cache"] == "MISS"

        r2 = await ns.client.post(
            "/v1/chat/completions",
            json=chat_body(content="topicA second phrasing"),
            headers=auth(key["key"]),
        )
        assert r2.status_code == 200
        assert r2.headers["X-Gateway-Cache"] == "SEMANTIC"
        # Only the first request reached upstream.
        assert len([b for b in ns.mock.bodies() if "messages" in b]) == 1

        # The entry really lives in Redis — the app did not fall back
        # to the per-process store.
        assert await redis.exists("llm-gateway:sem:index")
        keys = await redis.keys("llm-gateway:sem:*")
        assert any(not k.endswith(b"index") for k in keys)  # the entry itself


async def test_two_apps_share_semantic_entries(tmp_path):
    """The tier-59 guarantee end to end: replica A stores a semantic
    entry; replica B serves a near-duplicate from it. B's process never
    saw the request — the entry lives in Redis."""
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
                redis_backends=True,
                semantic_cache=SemanticCacheConfig(enabled=True, threshold=0.9),
            ),
            http_client=http_client,
            redis_client=redis,
            semantic_embedder=_topic_embedder,
        )
        app.state.admin_key = ADMIN_KEY  # ASGITransport does not run the lifespan
        return app, mock, http_client

    app_a, mock_a, http_a = build()
    app_b, mock_b, http_b = build()
    try:
        client_a = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="http://a.test"
        )
        client_b = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="http://b.test"
        )
        key = (await create_key(client_a, "replica-sem", rpm_limit=10))["key"]

        # replica A serves the request (semantic MISS, upstream on A only)
        first = await client_a.post(
            "/v1/chat/completions",
            json=chat_body(content="topicA first phrasing"),
            headers=auth(key),
        )
        assert first.status_code == 200
        assert first.headers["X-Gateway-Cache"] == "MISS"

        # replica B serves the near-duplicate from A's shared entry
        second = await client_b.post(
            "/v1/chat/completions",
            json=chat_body(content="topicA second phrasing"),
            headers=auth(key),
        )
        assert second.status_code == 200
        assert second.headers["X-Gateway-Cache"] == "SEMANTIC"
        assert [b for b in mock_b.bodies() if "messages" in b] == []  # B never called upstream

        # and the shared store really holds the entry
        assert await redis.exists("llm-gateway:sem:index")
        await client_a.aclose()
        await client_b.aclose()
    finally:
        await http_a.aclose()
        await http_b.aclose()
        await dispose_engine()
