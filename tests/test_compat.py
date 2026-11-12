"""Tests for the agent-framework compatibility fixes:

* ``GET /v1/models/{model}`` — model retrieve (frameworks validate models
  against this at startup).
* Cache identity includes ``logprobs`` / ``top_logprobs`` / ``logit_bias``.
* ``response_format`` fails loudly on providers that cannot honor it
  (Anthropic) and maps ``json_schema`` on Gemini.
* ``api-key`` header (Azure-style) and config-gated CORS for browsers.
"""

from __future__ import annotations

import pytest

from gateway.core.cache import cache_key_for
from gateway.providers.anthropic_adapter import translate_request as anthropic_translate
from gateway.providers.gemini_adapter import translate_request as gemini_translate
from gateway.schemas import ChatCompletionRequest
from gateway.errors import ProviderError

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key


# ----------------------------------------------------------------------
# GET /v1/models/{model}
# ----------------------------------------------------------------------


async def test_retrieve_model_known(env):
    key = (await create_key(env.client, "retriever"))["key"]
    resp = await env.client.get("/v1/models/test-model", headers=auth(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "test-model"
    assert body["object"] == "model"
    assert body["owned_by"] == "llm-gateway"
    assert isinstance(body["created"], int)


async def test_retrieve_model_unknown_is_openai_404(env):
    key = (await create_key(env.client, "retriever"))["key"]
    resp = await env.client.get("/v1/models/no-such-model", headers=auth(key))
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "model_not_found"
    assert error["type"] == "invalid_request_error"
    assert error["param"] == "model"


async def test_retrieve_model_not_allowed_for_key(env):
    key = (await create_key(env.client, "restricted", allowed_models=["other-*"]))["key"]
    # allowed model -> 200
    ok = await env.client.get("/v1/models/other-model", headers=auth(key))
    assert ok.status_code == 200
    # configured but not allowed for this key -> 404 model_not_found
    denied = await env.client.get("/v1/models/test-model", headers=auth(key))
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "model_not_found"


async def test_retrieve_model_admin_sees_everything(env):
    resp = await env.client.get("/v1/models/test-model", headers=auth(ADMIN_KEY))
    assert resp.status_code == 200
    assert resp.json()["id"] == "test-model"


async def test_retrieve_model_requires_auth(env):
    resp = await env.client.get("/v1/models/test-model")
    assert resp.status_code == 401


# ----------------------------------------------------------------------
# Cache identity: logprobs / top_logprobs / logit_bias
# ----------------------------------------------------------------------


def _request(**extra) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], **extra}
    )


def test_cache_key_ignores_stream_and_user():
    base = cache_key_for(_request())
    assert cache_key_for(_request(stream=True, user="u1")) == base


def test_cache_key_distinguishes_logprobs():
    base = cache_key_for(_request())
    assert cache_key_for(_request(logprobs=True)) != base
    assert cache_key_for(_request(logprobs=True, top_logprobs=5)) != cache_key_for(
        _request(logprobs=True, top_logprobs=10)
    )
    # and None (unset) is its own identity, distinct from explicit values
    assert cache_key_for(_request(logprobs=None)) == base


def test_cache_key_distinguishes_logit_bias():
    base = cache_key_for(_request())
    assert cache_key_for(_request(logit_bias={"50256": -100})) != base


async def test_logprobs_request_not_served_cached_plain_response(env):
    key = (await create_key(env.client, "lp"))["key"]
    # Plain request primes the exact cache...
    first = await env.client.post(
        "/v1/chat/completions", json=chat_body(content="logprobes cache probe"), headers=auth(key)
    )
    assert first.status_code == 200
    assert first.headers["x-gateway-cache"] == "MISS"
    # ...the same request WITH logprobs must not hit it
    second = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content="logprobes cache probe", logprobs=True),
        headers=auth(key),
    )
    assert second.status_code == 200
    assert second.headers["x-gateway-cache"] == "MISS"
    # ...while the plain request still does
    third = await env.client.post(
        "/v1/chat/completions", json=chat_body(content="logprobes cache probe"), headers=auth(key)
    )
    assert third.headers["x-gateway-cache"] == "HIT"


# ----------------------------------------------------------------------
# response_format: fail loudly where it cannot be honored
# ----------------------------------------------------------------------


def _rf_request(rf: dict) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": rf,
        }
    )


@pytest.mark.parametrize("rf", [{"type": "json_object"}, {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}])
def test_anthropic_rejects_response_format(rf):
    with pytest.raises(ProviderError) as exc:
        anthropic_translate(_rf_request(rf), "claude-x")
    assert exc.value.status_code == 400
    assert exc.value.code == "response_format_not_supported"
    assert exc.value.fallback is True  # the router may try the next deployment


def test_anthropic_accepts_text_response_format():
    body = anthropic_translate(_rf_request({"type": "text"}), "claude-x")
    assert "response_format" not in str(body)


def test_gemini_maps_json_object():
    body = gemini_translate(_rf_request({"type": "json_object"}), "gem-x")
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_gemini_maps_json_schema():
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }
    body = gemini_translate(
        _rf_request({"type": "json_schema", "json_schema": {"name": "weather", "schema": schema}}),
        "gem-x",
    )
    gen = body["generationConfig"]
    assert gen["responseMimeType"] == "application/json"
    assert gen["responseSchema"] == schema


def test_gemini_rejects_json_schema_without_schema():
    with pytest.raises(ProviderError) as exc:
        gemini_translate(_rf_request({"type": "json_schema", "json_schema": {"name": "x"}}), "gem-x")
    assert exc.value.status_code == 400
    assert exc.value.code == "response_format_not_supported"


def test_gemini_rejects_unknown_response_format():
    with pytest.raises(ProviderError):
        gemini_translate(_rf_request({"type": "yaml_object"}), "gem-x")


async def test_anthropic_route_falls_back_and_fails_loudly(tmp_path):
    """json_object on a model whose only deployment is Anthropic -> 400
    (not a silent 200 with an unstructured answer)."""
    from gateway.config import (
        CacheConfig,
        DeploymentConfig,
        GatewayConfig,
        ModelConfig,
        ProviderConfig,
    )

    config = GatewayConfig(
        providers={
            "anthropic": ProviderConfig(api_base="http://mock/v1", api_key="k"),
        },
        models=[
            ModelConfig(
                name="claude-only",
                deployments=[
                    DeploymentConfig(name="anth/claude", provider="anthropic", model="claude-x")
                ],
            )
        ],
        cache=CacheConfig(enabled=False),
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "json-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="claude-only", response_format={"type": "json_object"}),
            headers=auth(key),
        )
        assert resp.status_code == 400
        error = resp.json()["error"]
        assert error["code"] == "response_format_not_supported"
        assert "Anthropic" in error["message"]
        break


# ----------------------------------------------------------------------
# api-key header (Azure-style) + CORS
# ----------------------------------------------------------------------


async def test_api_key_header_authenticates(env):
    key = (await create_key(env.client, "azure-user"))["key"]
    resp = await env.client.get("/v1/models", headers={"api-key": key})
    assert resp.status_code == 200
    chat = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content="via api-key header"),
        headers={"api-key": key},
    )
    assert chat.status_code == 200
    admin = await env.client.get("/admin/keys", headers={"api-key": ADMIN_KEY})
    assert admin.status_code == 200


async def test_cors_disabled_by_default(env):
    preflight = await env.client.options(
        "/v1/chat/completions",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in preflight.headers


async def test_cors_enabled_for_configured_origins(tmp_path):
    async for env in _make_env(
        tmp_path, cors_allow_origins="https://app.example.com, http://localhost:5173"
    ):
        # Preflight from an allowed origin is answered without auth
        preflight = await env.client.options(
            "/v1/chat/completions",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "https://app.example.com"
        assert "authorization" in preflight.headers["access-control-allow-headers"]

        # Actual request carries CORS headers on the response too
        key = (await create_key(env.client, "browser"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="cors probe"),
            headers={"Authorization": f"Bearer {key}", "Origin": "https://app.example.com"},
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "https://app.example.com"
        assert "x-request-id" in {h.lower() for h in resp.headers["access-control-expose-headers"].split(",")}
        break


async def test_cors_rejects_unlisted_origin(tmp_path):
    async for env in _make_env(tmp_path, cors_allow_origins="https://app.example.com"):
        resp = await env.client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {ADMIN_KEY}", "Origin": "https://evil.example"},
        )
        assert resp.status_code == 200  # the request itself succeeds...
        # ...but no CORS headers: the browser will block the response body
        assert "access-control-allow-origin" not in resp.headers
        break
