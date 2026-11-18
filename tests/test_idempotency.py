"""Idempotency-Key replay: caller-keyed, retry-safe chat completions.

The contract: a client supplies an ``Idempotency-Key`` header on any
chat-family surface (OpenAI chat, /v1/responses, native Anthropic,
native Gemini); the *successful* response is stored under (caller,
client key) and replays on retry — no double generation, no double
billing (replays log as cache hits with zero cost and answer
``X-Gateway-Cache: IDEMPOTENT``). Only successes are stored — errors
and guardrail blocks re-execute, which is what a transient failure
needs. Reusing a key with different request parameters is a 400.
Duplicates racing while the original is in flight dedup in-process
(bounded wait, then execute). No header = zero behavior change;
disabled config ignores the header entirely. Entries are private per
caller. The check runs before budget so a retry serves even when the
ceiling was crossed since the original was authorized.

Every test disables the content cache (``cache.enabled=False``) so a
replayed response proves the *idempotency* store served it, not the
shared content cache.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from fakeredis import FakeAsyncRedis

from gateway.config import CacheConfig, IdempotencyConfig, Settings
from gateway.db import dispose_engine, init_engine, reset_engine_for_tests
from gateway.main import create_app
from gateway.observability.metrics import IDEMPOTENCY

from .conftest import ADMIN_KEY, MockUpstream, _make_env, auth, chat_body, create_key, make_config
from .test_native_anthropic import anthropic_body


def _config(**overrides):
    """Content cache off — isolate idempotency from cache-hit behavior."""
    overrides.setdefault("cache", CacheConfig(enabled=False))
    return make_config(**overrides)


def _idem(key: str, idempotency_key: str | None) -> dict[str, str]:
    headers = auth(key)
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _stream_content(text: str) -> str:
    """Concatenated delta content from an SSE body."""
    out = []
    for line in text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            chunk = json.loads(line[len("data: ") :])
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    out.append(delta["content"])
    return "".join(out)


async def _key_row(env, name: str) -> dict:
    listing = await env.client.get("/admin/keys", headers=auth(ADMIN_KEY))
    row = next(k for k in listing.json()["data"] if k["name"] == name)
    return (await env.client.get(f"/admin/keys/{row['id']}", headers=auth(ADMIN_KEY))).json()


async def test_replay_returns_stored_response(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        stored_before = IDEMPOTENCY.labels(outcome="stored")._value.get()
        replayed_before = IDEMPOTENCY.labels(outcome="replayed")._value.get()
        key = (await create_key(env.client, "replayer"))["key"]

        first = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello"),
            headers=_idem(key, "order-123"),
        )
        assert first.status_code == 200
        assert first.headers["X-Gateway-Cache"] == "MISS"

        second = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello"),
            headers=_idem(key, "order-123"),
        )
        assert second.status_code == 200
        assert second.headers["X-Gateway-Cache"] == "IDEMPOTENT"
        assert second.headers["X-Gateway-Provider"] == "idempotency"
        # Same completion, different request (a fresh id + log row).
        assert second.json()["choices"][0]["message"]["content"] == (
            first.json()["choices"][0]["message"]["content"]
        )
        assert second.headers["X-Request-Id"] != first.headers["X-Request-Id"]

        # Executed exactly once: one store (the original) + one replay.
        chat_calls = [r for r in env.mock.requests if "messages" in r["body"]]
        assert len(chat_calls) == 1
        assert IDEMPOTENCY.labels(outcome="stored")._value.get() == stored_before + 1
        assert IDEMPOTENCY.labels(outcome="replayed")._value.get() == replayed_before + 1

        # Two log rows, both status "success" (the cache-hit family keeps
        # its boolean column — same contract as content-cache hits): the
        # replay is a zero-cost cache_hit row, the original was billed.
        logs = (
            await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        ).json()["data"]
        assert [row["status"] for row in logs] == ["success", "success"]
        assert logs[0]["cache_hit"] is True
        assert logs[0]["cost_usd"] == 0
        assert logs[1]["cache_hit"] is False
        assert logs[1]["cost_usd"] > 0


async def test_no_header_means_normal_execution(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "plain"))["key"]
        for _ in range(2):
            resp = await env.client.post(
                "/v1/chat/completions", json=chat_body(content="Hello"), headers=auth(key)
            )
            assert resp.status_code == 200
            assert resp.headers["X-Gateway-Cache"] == "MISS"
        assert len([r for r in env.mock.requests if "messages" in r["body"]]) == 2


async def test_key_reuse_with_different_params_is_rejected(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "careless"))["key"]
        first = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello"),
            headers=_idem(key, "order-1"),
        )
        assert first.status_code == 200

        # Same key, different request parameters: refuse rather than serve
        # the wrong answer.
        rejected = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Different"),
            headers=_idem(key, "order-1"),
        )
        assert rejected.status_code == 400
        assert rejected.json()["error"]["code"] == "idempotency_key_reuse"

        # The original parameters still replay afterwards.
        again = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello"),
            headers=_idem(key, "order-1"),
        )
        assert again.status_code == 200
        assert again.headers["X-Gateway-Cache"] == "IDEMPOTENT"


async def test_stream_replay(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "streamer"))["key"]
        body = chat_body(content="Hello", stream=True)

        first = await env.client.post("/v1/chat/completions", json=body, headers=_idem(key, "s-1"))
        assert first.status_code == 200
        assert first.headers["X-Gateway-Cache"] == "MISS"
        assert "text/event-stream" in first.headers["content-type"]

        second = await env.client.post("/v1/chat/completions", json=body, headers=_idem(key, "s-1"))
        assert second.status_code == 200
        assert second.headers["X-Gateway-Cache"] == "IDEMPOTENT"
        # Replays synthesize the stream from the stored completion: the
        # client sees the same content without a second generation.
        assert _stream_content(second.text) == _stream_content(first.text)
        assert len([r for r in env.mock.requests if "messages" in r["body"]]) == 1


async def test_inflight_duplicate_waits_for_original(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "racer"))["key"]
        adapter = env.app.state.pipeline.router.providers["openai"]
        original = adapter.complete
        calls = {"n": 0}

        async def slow_complete(deployment, request, provider_cfg, timeout):
            calls["n"] += 1
            await asyncio.sleep(0.25)
            return await original(deployment, request, provider_cfg, timeout)

        adapter.complete = slow_complete
        try:
            first, second = await asyncio.gather(
                env.client.post(
                    "/v1/chat/completions",
                    json=chat_body(content="slow"),
                    headers=_idem(key, "race-1"),
                ),
                env.client.post(
                    "/v1/chat/completions",
                    json=chat_body(content="slow"),
                    headers=_idem(key, "race-1"),
                ),
            )
        finally:
            adapter.complete = original

        assert first.status_code == 200 and second.status_code == 200
        # The duplicate deduplicated against the in-flight original —
        # one upstream execution — and both callers got the same answer.
        assert calls["n"] == 1
        states = sorted([first.headers["X-Gateway-Cache"], second.headers["X-Gateway-Cache"]])
        assert states == ["IDEMPOTENT", "MISS"]
        assert (
            first.json()["choices"][0]["message"]["content"]
            == second.json()["choices"][0]["message"]["content"]
        )


async def test_failed_original_reexecutes(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "retrying"))["key"]
        # Single-deployment model ("other-model" -> openai/other ->
        # model-c): all 3 attempts (max_retries=2) fail, the request 500s.
        env.mock.fail_counts["model-c"] = 3

        first = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="other-model", content="Hello"),
            headers=_idem(key, "flaky-1"),
        )
        assert first.status_code == 500
        # Errors are never stored — and the retry re-executes rather
        # than replaying a failure.
        second = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="other-model", content="Hello"),
            headers=_idem(key, "flaky-1"),
        )
        assert second.status_code == 200
        assert second.headers["X-Gateway-Cache"] == "MISS"

        # ...and once a success exists, it replays.
        third = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="other-model", content="Hello"),
            headers=_idem(key, "flaky-1"),
        )
        assert third.headers["X-Gateway-Cache"] == "IDEMPOTENT"


async def test_replay_is_not_billed_twice(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        created = await create_key(env.client, "payer")
        key = created["key"]

        first = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello"),
            headers=_idem(key, "bill-1"),
        )
        assert first.status_code == 200
        assert "X-Gateway-Cost-Usd" in first.headers
        await env.app.state.pipeline.drain_background_tasks()
        spend_after_first = (await _key_row(env, "payer"))["spend_usd"]
        assert spend_after_first > 0

        second = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello"),
            headers=_idem(key, "bill-1"),
        )
        assert second.status_code == 200
        # Zero-cost replay: no cost header, spend unchanged.
        assert "X-Gateway-Cost-Usd" not in second.headers
        await env.app.state.pipeline.drain_background_tasks()
        assert (await _key_row(env, "payer"))["spend_usd"] == spend_after_first


async def test_disabled_config_ignores_header(tmp_path):
    async for env in _make_env(
        tmp_path, config=_config(idempotency=IdempotencyConfig(enabled=False))
    ):
        key = (await create_key(env.client, "off"))["key"]
        for _ in range(2):
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="Hello"),
                headers=_idem(key, "ignored-1"),
            )
            assert resp.status_code == 200
            assert resp.headers["X-Gateway-Cache"] == "MISS"
        assert len([r for r in env.mock.requests if "messages" in r["body"]]) == 2


async def test_invalid_key_rejected(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        rejected_before = IDEMPOTENCY.labels(outcome="rejected")._value.get()
        key = (await create_key(env.client, "picky"))["key"]
        for bad in ("", "   ", "x" * 256):
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content="Hello"),
                headers=_idem(key, bad),
            )
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == "invalid_idempotency_key"
        # 255 chars is fine (the boundary is inclusive).
        ok = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello"),
            headers=_idem(key, "x" * 255),
        )
        assert ok.status_code == 200
        assert IDEMPOTENCY.labels(outcome="rejected")._value.get() == rejected_before + 3


async def test_embeddings_ignore_the_header(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        replayed_before = IDEMPOTENCY.labels(outcome="replayed")._value.get()
        key = (await create_key(env.client, "embedder"))["key"]
        for _ in range(2):
            resp = await env.client.post(
                "/v1/embeddings",
                json={"model": "test-model", "input": "hello"},
                headers=_idem(key, "emb-1"),
            )
            assert resp.status_code == 200
        # Both executed: embeddings have no replay semantics, the header
        # simply does not apply to the simple surfaces.
        assert len([r for r in env.mock.requests if "/embeddings" in r["url"]]) == 2
        assert IDEMPOTENCY.labels(outcome="replayed")._value.get() == replayed_before


async def test_native_messages_surface_replays(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "anthropic-user"))["key"]
        headers = {"x-api-key": key, "Idempotency-Key": "msg-1"}

        first = await env.client.post("/v1/messages", json=anthropic_body("hello"), headers=headers)
        assert first.status_code == 200
        second = await env.client.post("/v1/messages", json=anthropic_body("hello"), headers=headers)
        assert second.status_code == 200
        # Same translated body, replayed without a second execution.
        assert second.json() == first.json()
        assert second.headers["X-Gateway-Cache"] == "IDEMPOTENT"
        assert len([r for r in env.mock.requests if "messages" in r["body"]]) == 1


async def test_entries_are_private_per_caller(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        key_a = (await create_key(env.client, "caller-a"))["key"]
        key_b = (await create_key(env.client, "caller-b"))["key"]
        # The same client-chosen key from two callers: each executes —
        # entries are keyed by (caller, client key), never global.
        resp_a = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="Hello"), headers=_idem(key_a, "shared")
        )
        resp_b = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="Hello"), headers=_idem(key_b, "shared")
        )
        assert resp_a.status_code == 200 and resp_b.status_code == 200
        assert len([r for r in env.mock.requests if "messages" in r["body"]]) == 2

        # ...and each caller replays their own entry afterwards.
        again_b = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="Hello"), headers=_idem(key_b, "shared")
        )
        assert again_b.headers["X-Gateway-Cache"] == "IDEMPOTENT"
        assert len([r for r in env.mock.requests if "messages" in r["body"]]) == 2


async def test_replay_serves_even_when_budget_exceeded_since(tmp_path):
    async for env in _make_env(tmp_path, config=_config()):
        # One mock completion costs ~$0.000048 — a $0.00002 ceiling
        # authorizes exactly one request.
        created = await create_key(env.client, "budgeted", max_budget_usd=0.00002)
        key = created["key"]

        first = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello"),
            headers=_idem(key, "bud-1"),
        )
        assert first.status_code == 200
        await env.app.state.pipeline.drain_background_tasks()

        # The ceiling was crossed after the original was authorized — a
        # retry must still serve: idempotency runs BEFORE budget.
        replayed = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello"),
            headers=_idem(key, "bud-1"),
        )
        assert replayed.status_code == 200
        assert replayed.headers["X-Gateway-Cache"] == "IDEMPOTENT"

        # A *new* request under the same key still hits the ceiling.
        fresh = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="New work"),
            headers=auth(key),
        )
        assert fresh.status_code == 429


async def test_redis_backend_replays_cross_replica(tmp_path):
    """The Redis backend shares stored entries across replicas (the
    documented limit: only *completed* entries dedup cross-replica —
    the in-flight map stays per-process)."""
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
                redis_backends=True, cache=CacheConfig(enabled=False)
            ),
            http_client=http_client,
            redis_client=redis,
        )
        app.state.admin_key = ADMIN_KEY  # ASGITransport does not run the lifespan
        return app, mock, http_client

    app_a, mock_a, http_a = build()
    app_b, mock_b, http_b = build()
    client_a = httpx.AsyncClient(transport=httpx.ASGITransport(app=app_a), base_url="http://a.test")
    client_b = httpx.AsyncClient(transport=httpx.ASGITransport(app=app_b), base_url="http://b.test")
    try:
        key = (await create_key(client_a, "replica-user"))["key"]
        first = await client_a.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello"),
            headers=_idem(key, "repl-1"),
        )
        assert first.status_code == 200
        assert first.headers["X-Gateway-Cache"] == "MISS"

        # Replica B has its own process state — but the completed entry
        # lives in Redis, so the retry replays.
        second = await client_b.post(
            "/v1/chat/completions",
            json=chat_body(content="Hello"),
            headers=_idem(key, "repl-1"),
        )
        assert second.status_code == 200
        assert second.headers["X-Gateway-Cache"] == "IDEMPOTENT"
        assert len([r for r in mock_a.requests if "messages" in r["body"]]) == 1
        assert len([r for r in mock_b.requests if "messages" in r["body"]]) == 0

        # Entries are namespaced apart from the content cache.
        keys = {k.decode() if isinstance(k, bytes) else k for k in await redis.keys("*")}
        idem_keys = [k for k in keys if ":idem:cache:" in k and "index" not in k]
        assert len(idem_keys) == 1
    finally:
        await client_a.aclose()
        await client_b.aclose()
        await http_a.aclose()
        await http_b.aclose()
        await dispose_engine()
