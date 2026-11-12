"""Tests for POST /v1/embeddings (adapters, router fallback, full pipeline)."""

from __future__ import annotations

import base64
import json
import struct

import httpx
import pytest

from gateway.config import DeploymentConfig, ModelConfig, ProviderConfig
from gateway.core.pipeline import embedding_cache_key_for
from gateway.errors import ProviderError
from gateway.providers.anthropic_adapter import AnthropicProviderAdapter
from gateway.providers.gemini_adapter import GeminiProviderAdapter
from gateway.providers.openai_adapter import OpenAIProviderAdapter
from gateway.schemas import EmbeddingRequest

from .conftest import ADMIN_KEY, _make_env, auth, create_key, make_config

EMB_MODEL = ModelConfig(
    name="text-embedding-mock",
    deployments=[
        DeploymentConfig(name="openai/emb-primary", provider="openai", model="embed-mock-a"),
        DeploymentConfig(
            name="openai/emb-fallback", provider="openai", model="embed-mock-b", priority=200
        ),
    ],
)
EMB_PRICING = {"text-embedding-mock": {"input": 0.02, "output": 0.0}}


def emb_body(content="hello world", model="text-embedding-mock", **extra) -> dict:
    return {"model": model, "input": content, **extra}


async def _make_emb_env(tmp_path, models=None, pricing=None, tracer_provider=None):
    config = make_config(
        models=models or [EMB_MODEL],
        pricing=pricing if pricing is not None else EMB_PRICING,
    )
    async for ns in _make_env(tmp_path, config=config, tracer_provider=tracer_provider):
        yield ns


@pytest.fixture
async def emb_env(tmp_path):
    async for ns in _make_emb_env(tmp_path):
        yield ns


# ----------------------------------------------------------------------
# Adapter-level: OpenAI passthrough
# ----------------------------------------------------------------------


async def test_openai_adapter_embed_passthrough():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "embedding": [0.1, 0.2], "index": 0}],
                "model": "embed-mock-a",
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            },
        )

    adapter = OpenAIProviderAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    request = EmbeddingRequest(
        model="text-embedding-mock", input=["a", "b"], dimensions=64,
        encoding_format="base64",
    )
    data = await adapter.embed(
        DeploymentConfig(name="openai/x", provider="openai", model="embed-mock-a"),
        request,
        ProviderConfig(api_base="http://up.test/v1", api_key="k"),
        httpx.Timeout(10),
    )
    sent = json.loads(calls[0].content)
    assert calls[0].url.path == "/v1/embeddings"
    assert sent["model"] == "embed-mock-a"  # native name upstream
    assert sent["input"] == ["a", "b"]
    assert sent["dimensions"] == 64
    # Canonical float form regardless of what the client asked for:
    assert sent["encoding_format"] == "float"
    assert data["model"] == "text-embedding-mock"  # public name downstream


# ----------------------------------------------------------------------
# Adapter-level: Gemini batchEmbedContents translation
# ----------------------------------------------------------------------


def _gemini_adapter(captured: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "embeddings": [
                    {"values": [0.1, 0.2, 0.3]} for _ in captured[-1]["requests"]
                ]
            },
        )

    return GeminiProviderAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_gemini_adapter_embed_translation():
    captured: list[dict] = []
    adapter = _gemini_adapter(captured)
    request = EmbeddingRequest(
        model="text-embedding-mock", input=["hello", "world"], dimensions=512
    )
    data = await adapter.embed(
        DeploymentConfig(name="gemini/x", provider="gemini", model="gemini-embedding-001"),
        request,
        ProviderConfig(api_base="http://g.test", api_key="k"),
        httpx.Timeout(10),
    )
    assert len(captured) == 1
    sub = captured[0]["requests"]
    assert sub[0] == {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": "hello"}]},
        "outputDimensionality": 512,
    }
    # OpenAI-format response with estimated usage (no usage from Gemini)
    assert data["object"] == "list"
    assert [d["index"] for d in data["data"]] == [0, 1]
    assert data["data"][0]["embedding"] == [0.1, 0.2, 0.3]
    assert data["model"] == "text-embedding-mock"
    assert data["usage"]["prompt_tokens"] > 0
    assert "completion_tokens" not in data["usage"]


async def test_gemini_adapter_embed_single_string():
    captured: list[dict] = []
    adapter = _gemini_adapter(captured)
    request = EmbeddingRequest(model="m", input="just one")
    data = await adapter.embed(
        DeploymentConfig(name="gemini/x", provider="gemini", model="gem"),
        request,
        ProviderConfig(api_base="http://g.test", api_key="k"),
        httpx.Timeout(10),
    )
    assert len(captured[0]["requests"]) == 1
    assert len(data["data"]) == 1
    # No dimensions -> no outputDimensionality key
    assert "outputDimensionality" not in captured[0]["requests"][0]


async def test_gemini_adapter_embed_chunks_over_100():
    captured: list[dict] = []
    adapter = _gemini_adapter(captured)
    request = EmbeddingRequest(model="m", input=[f"t{i}" for i in range(205)])
    data = await adapter.embed(
        DeploymentConfig(name="gemini/x", provider="gemini", model="gem"),
        request,
        ProviderConfig(api_base="http://g.test", api_key="k"),
        httpx.Timeout(10),
    )
    # batchEmbedContents accepts at most 100 texts per call
    assert [len(c["requests"]) for c in captured] == [100, 100, 5]
    assert len(data["data"]) == 205
    assert [d["index"] for d in data["data"]] == list(range(205))


# ----------------------------------------------------------------------
# Adapter-level: unsupported provider
# ----------------------------------------------------------------------


async def test_anthropic_adapter_embed_not_supported():
    adapter = AnthropicProviderAdapter(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={})
    )))
    request = EmbeddingRequest(model="m", input="hello")
    with pytest.raises(ProviderError) as excinfo:
        await adapter.embed(
            DeploymentConfig(name="anthropic/x", provider="anthropic", model="claude"),
            request,
            ProviderConfig(api_base="http://a.test", api_key="k"),
            httpx.Timeout(10),
        )
    assert excinfo.value.code == "embeddings_not_supported"
    assert not excinfo.value.retryable
    assert excinfo.value.fallback  # router falls through to the next deployment


# ----------------------------------------------------------------------
# Full pipeline (OpenAI-compatible upstream)
# ----------------------------------------------------------------------


async def test_embedding_success(emb_env):
    key = (await create_key(emb_env.client, "emb"))["key"]
    resp = await emb_env.client.post(
        "/v1/embeddings", json=emb_body("hello gateway"), headers=auth(key)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    assert data["model"] == "text-embedding-mock"
    item = data["data"][0]
    assert item["object"] == "embedding"
    assert isinstance(item["embedding"], list)
    assert all(isinstance(v, float) for v in item["embedding"])
    assert data["usage"]["prompt_tokens"] > 0
    # The provider saw the native model name; the client sees the public one.
    sent = emb_env.mock.bodies()[-1]
    assert sent["model"] == "embed-mock-a"
    assert resp.headers["X-Gateway-Model"] == "text-embedding-mock"
    assert resp.headers["X-Gateway-Deployment"] == "openai/emb-primary"
    assert resp.headers["X-Gateway-Cache"] == "MISS"


async def test_embedding_multiple_inputs(emb_env):
    key = (await create_key(emb_env.client, "emb-multi"))["key"]
    resp = await emb_env.client.post(
        "/v1/embeddings", json=emb_body(["alpha", "beta", "gamma"]), headers=auth(key)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert [d["index"] for d in data["data"]] == [0, 1, 2]
    vectors = [d["embedding"] for d in data["data"]]
    assert vectors[0] != vectors[1]  # deterministic per input
    sent = emb_env.mock.bodies()[-1]
    assert sent["input"] == ["alpha", "beta", "gamma"]


async def test_embedding_dimensions_forwarded(emb_env):
    key = (await create_key(emb_env.client, "emb-dims"))["key"]
    resp = await emb_env.client.post(
        "/v1/embeddings", json=emb_body("hello", dimensions=32), headers=auth(key)
    )
    assert resp.status_code == 200
    assert len(resp.json()["data"][0]["embedding"]) == 32
    assert emb_env.mock.bodies()[-1]["dimensions"] == 32


async def test_embedding_token_array_input_rejected(emb_env):
    key = (await create_key(emb_env.client, "emb-tokens"))["key"]
    resp = await emb_env.client.post(
        "/v1/embeddings",
        json={"model": "text-embedding-mock", "input": [1, 2, 3, 4]},
        headers=auth(key),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


async def test_embedding_unknown_model_404(emb_env):
    key = (await create_key(emb_env.client, "emb-unknown"))["key"]
    resp = await emb_env.client.post(
        "/v1/embeddings", json=emb_body("hi", model="nope"), headers=auth(key)
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


async def test_embedding_models_listed(emb_env):
    key = (await create_key(emb_env.client, "emb-list"))["key"]
    resp = await emb_env.client.get("/v1/models", headers=auth(key))
    assert resp.status_code == 200
    assert "text-embedding-mock" in [m["id"] for m in resp.json()["data"]]


# ----------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------


async def test_embedding_cache_hit(emb_env):
    key = (await create_key(emb_env.client, "emb-cache"))["key"]
    first = await emb_env.client.post(
        "/v1/embeddings", json=emb_body("cache me"), headers=auth(key)
    )
    assert first.headers["X-Gateway-Cache"] == "MISS"
    assert first.headers["X-Gateway-Provider"] == "openai"
    calls_after_first = len(emb_env.mock.requests)

    second = await emb_env.client.post(
        "/v1/embeddings", json=emb_body("cache me"), headers=auth(key)
    )
    assert second.status_code == 200
    assert second.headers["X-Gateway-Cache"] == "HIT"
    assert second.headers["X-Gateway-Provider"] == "cache"
    assert len(emb_env.mock.requests) == calls_after_first  # no upstream call
    assert second.json() == first.json()  # identical vectors

    # A different input is a different cache entry.
    third = await emb_env.client.post(
        "/v1/embeddings", json=emb_body("different"), headers=auth(key)
    )
    assert third.headers["X-Gateway-Cache"] == "MISS"
    assert len(emb_env.mock.requests) == calls_after_first + 1


async def test_embedding_cache_key_excludes_encoding_format():
    float_req = EmbeddingRequest(model="m", input="same text")
    b64_req = EmbeddingRequest(model="m", input="same text", encoding_format="base64")
    assert embedding_cache_key_for(float_req) == embedding_cache_key_for(b64_req)
    other = EmbeddingRequest(model="m", input="other text")
    assert embedding_cache_key_for(float_req) != embedding_cache_key_for(other)
    dims = EmbeddingRequest(model="m", input="same text", dimensions=64)
    assert embedding_cache_key_for(float_req) != embedding_cache_key_for(dims)


async def test_embedding_base64_encoding_and_cache(emb_env):
    key = (await create_key(emb_env.client, "emb-b64"))["key"]
    float_resp = await emb_env.client.post(
        "/v1/embeddings", json=emb_body("encode me"), headers=auth(key)
    )
    b64_resp = await emb_env.client.post(
        "/v1/embeddings",
        json=emb_body("encode me", encoding_format="base64"),
        headers=auth(key),
    )
    assert b64_resp.status_code == 200
    assert b64_resp.headers["X-Gateway-Cache"] == "HIT"  # same canonical entry

    raw = b64_resp.json()["data"][0]["embedding"]
    assert isinstance(raw, str)
    decoded = base64.b64decode(raw)
    floats = struct.unpack(f"<{len(decoded) // 4}f", decoded)
    # base64 encodes float32: tiny precision loss vs the JSON float form is
    # inherent (same as OpenAI's wire format).
    assert list(floats) == pytest.approx(
        float_resp.json()["data"][0]["embedding"], abs=1e-6
    )


# ----------------------------------------------------------------------
# Policy: allowlists, rate limits, budgets
# ----------------------------------------------------------------------


async def test_embedding_model_allowlist(emb_env):
    key = (
        await create_key(emb_env.client, "emb-allow", allowed_models=["test-model"])
    )["key"]
    resp = await emb_env.client.post(
        "/v1/embeddings", json=emb_body("hi"), headers=auth(key)
    )
    # Wait: the default models in this env are embeddings-only; the key is
    # restricted to test-model (a chat model), so this must be denied.
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


async def test_embedding_rate_limit_rpm(emb_env):
    key = (await create_key(emb_env.client, "emb-rpm", rpm_limit=1))["key"]
    first = await emb_env.client.post(
        "/v1/embeddings", json=emb_body("one"), headers=auth(key)
    )
    assert first.status_code == 200
    second = await emb_env.client.post(
        "/v1/embeddings", json=emb_body("two"), headers=auth(key)
    )
    assert second.status_code == 429
    assert second.headers["Retry-After"]
    assert second.json()["error"]["code"] == "rate_limit_exceeded"


async def test_embedding_rate_limit_tpm(emb_env):
    key = (await create_key(emb_env.client, "emb-tpm", tpm_limit=1))["key"]
    # ~chars/4 estimate exceeds tpm=1 on the first request already
    resp = await emb_env.client.post(
        "/v1/embeddings", json=emb_body("a fairly long input text"), headers=auth(key)
    )
    assert resp.status_code == 429


async def test_embedding_budget_exceeded(emb_env):
    key = (
        await create_key(emb_env.client, "emb-budget", max_budget_usd=0.0)
    )["key"]
    resp = await emb_env.client.post(
        "/v1/embeddings", json=emb_body("hello"), headers=auth(key)
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "insufficient_quota"


# ----------------------------------------------------------------------
# Routing: fallback, cost accounting, logs
# ----------------------------------------------------------------------


async def test_embedding_fallback_on_failure(emb_env):
    key = (await create_key(emb_env.client, "emb-fallback"))["key"]
    emb_env.mock.fail_counts["embed-mock-a"] = 99
    resp = await emb_env.client.post(
        "/v1/embeddings", json=emb_body("hello"), headers=auth(key)
    )
    assert resp.status_code == 200
    assert resp.headers["X-Gateway-Deployment"] == "openai/emb-fallback"
    sent = emb_env.mock.bodies()[-1]
    assert sent["model"] == "embed-mock-b"


async def test_embedding_cost_and_log_row(emb_env):
    key = (await create_key(emb_env.client, "emb-cost"))["key"]
    resp = await emb_env.client.post(
        "/v1/embeddings", json=emb_body("hello world"), headers=auth(key)
    )
    assert resp.status_code == 200
    usage = resp.json()["usage"]
    # pricing: 0.02 USD per 1M input tokens
    expected = usage["prompt_tokens"] * 0.02 / 1_000_000
    assert abs(float(resp.headers["X-Gateway-Cost-Usd"]) - expected) < 1e-9

    logs = await emb_env.client.get(
        "/admin/logs",
        params={"request_id": resp.headers["X-Request-Id"]},
        headers=auth(ADMIN_KEY),
    )
    row = logs.json()["data"][0]
    assert row["model"] == "text-embedding-mock"
    assert row["prompt_tokens"] == usage["prompt_tokens"]
    assert row["deployment"] == "openai/emb-primary"
    assert row["cost_usd"] > 0


async def test_embedding_guardrails_not_applied(emb_env):
    """Guardrails are chat-typed; embedding input must pass through unchanged."""
    key = (await create_key(emb_env.client, "emb-no-guard"))["key"]
    resp = await emb_env.client.post(
        "/v1/embeddings",
        json=emb_body("my email is jane.doe@example.com"),
        headers=auth(key),
    )
    assert resp.status_code == 200
    sent = emb_env.mock.bodies()[-1]
    assert sent["input"] == "my email is jane.doe@example.com"  # not redacted


# ----------------------------------------------------------------------
# Gemini through the full app
# ----------------------------------------------------------------------


@pytest.fixture
async def gemini_emb_env(tmp_path):
    models = [
        ModelConfig(
            name="gemini-embedding",
            deployments=[
                DeploymentConfig(
                    name="gemini/emb", provider="gemini", model="gemini-embedding-001"
                )
            ],
        )
    ]
    providers = {
        "openai": ProviderConfig(
            api_base="http://mockupstream.test/v1", api_key="provider-key"
        ),
        "gemini": ProviderConfig(api_base="http://mockupstream.test", api_key="gk"),
    }
    config = make_config(models=models, pricing={})
    config.providers = providers
    async for ns in _make_env(tmp_path, config=config):
        yield ns


async def test_gemini_embedding_e2e(gemini_emb_env):
    key = (await create_key(gemini_emb_env.client, "g-emb"))["key"]
    resp = await gemini_emb_env.client.post(
        "/v1/embeddings",
        json={"model": "gemini-embedding", "input": ["a", "b"], "dimensions": 8},
        headers=auth(key),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "gemini-embedding"
    assert [d["index"] for d in data["data"]] == [0, 1]
    assert data["data"][0]["embedding"] == [0.25, 0.5, 0.75, 1.0]
    assert data["usage"]["prompt_tokens"] > 0  # estimated
    # The upstream call was a batchEmbedContents with the resource name.
    sent = gemini_emb_env.mock.bodies()[-1]
    assert sent["requests"][0]["model"] == "models/gemini-embedding-001"
    assert sent["requests"][0]["outputDimensionality"] == 8
    assert resp.headers["X-Gateway-Provider"] == "gemini"


# ----------------------------------------------------------------------
# Tracing
# ----------------------------------------------------------------------


async def test_embedding_span(tmp_path, tracer_provider, span_exporter):
    async for ns in _make_emb_env(tmp_path, tracer_provider=tracer_provider):
        key = (await create_key(ns.client, "emb-span"))["key"]
        resp = await ns.client.post(
            "/v1/embeddings", json=emb_body("trace me"), headers=auth(key)
        )
        assert resp.status_code == 200
        spans = [s for s in span_exporter.get_finished_spans() if s.name == "gateway.embedding"]
        assert len(spans) == 1
        assert spans[0].attributes["gateway.model"] == "text-embedding-mock"
        assert spans[0].attributes["gateway.status"] == "success"
