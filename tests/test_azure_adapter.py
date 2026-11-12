"""Tests for the Azure OpenAI provider adapter.

Azure speaks the OpenAI wire format but addresses *deployments*
(``/openai/deployments/{name}/chat/completions?api-version=...``) and
authenticates with the ``api-key`` header. The adapter subclasses the
OpenAI passthrough and overrides only URL construction and headers, so
the full pipeline (routing, retries, caching, accounting) applies.
"""

from __future__ import annotations

import json

from gateway.config import DeploymentConfig, ModelConfig, ProviderConfig
from gateway.providers import BUILTIN_ADAPTERS
from gateway.providers.azure_adapter import (
    DEFAULT_API_VERSION,
    AzureProviderAdapter,
)

from .conftest import _make_env, auth, chat_body, create_key, make_config
from .test_pipeline import read_sse


def _azure_config(
    models: list[ModelConfig] | None = None, **provider_extra
):
    return make_config(
        providers={
            "azure": ProviderConfig(
                api_base="http://mockupstream.test",
                api_key="azure-key",
                **provider_extra,
            )
        },
        models=models
        or [
            ModelConfig(
                name="test-model",
                deployments=[
                    DeploymentConfig(
                        name="azure/primary", provider="azure", model="gpt-4o-deploy"
                    )
                ],
            )
        ],
        pricing={"gpt-4o-deploy": {"input": 2.0, "output": 4.0}},
    )


# ----------------------------------------------------------------------
# Units: addressing + auth
# ----------------------------------------------------------------------


def test_registry_contains_azure():
    assert BUILTIN_ADAPTERS["azure"] is AzureProviderAdapter


def test_urls_are_deployment_addressed():
    adapter = AzureProviderAdapter(client=None)  # type: ignore[arg-type]
    cfg = ProviderConfig(api_base="https://res.openai.azure.com/", api_key="k")
    deployment = DeploymentConfig(
        name="azure/primary", provider="azure", model="gpt-4o-deploy"
    )
    assert adapter._chat_url(deployment, cfg) == (
        "https://res.openai.azure.com/openai/deployments/gpt-4o-deploy"
        f"/chat/completions?api-version={DEFAULT_API_VERSION}"
    )
    assert adapter._embed_url(deployment, cfg) == (
        "https://res.openai.azure.com/openai/deployments/gpt-4o-deploy"
        f"/embeddings?api-version={DEFAULT_API_VERSION}"
    )


def test_api_version_override_and_deployment_quoting():
    adapter = AzureProviderAdapter(client=None)  # type: ignore[arg-type]
    cfg = ProviderConfig(
        api_base="https://res.openai.azure.com", api_key="k", api_version="2025-01-01"
    )
    deployment = DeploymentConfig(
        name="azure/odd", provider="azure", model="my deploy/v2"
    )
    url = adapter._chat_url(deployment, cfg)
    assert "api-version=2025-01-01" in url
    assert "/openai/deployments/my%20deploy%2Fv2/chat/completions" in url


def test_headers_use_api_key_header():
    adapter = AzureProviderAdapter(client=None)  # type: ignore[arg-type]
    cfg = ProviderConfig(api_base="https://res.openai.azure.com", api_key="secret")
    headers = adapter._headers(cfg)
    assert headers["api-key"] == "secret"
    assert "Authorization" not in headers


# ----------------------------------------------------------------------
# e2e through the app (mock upstream)
# ----------------------------------------------------------------------


async def test_chat_completion_via_azure(tmp_path):
    async for env in _make_env(tmp_path, config=_azure_config()):
        key = (await create_key(env.client, "azure-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello azure"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == "test-model"  # public name to clients
        assert resp.headers["x-gateway-provider"] == "azure"

        sent = env.mock.requests[-1]
        assert sent["url"] == (
            "http://mockupstream.test/openai/deployments/gpt-4o-deploy"
            f"/chat/completions?api-version={DEFAULT_API_VERSION}"
        )
        assert sent["headers"].get("api-key") == "azure-key"
        assert "authorization" not in sent["headers"]
        assert sent["body"]["model"] == "gpt-4o-deploy"  # deployment name up
        break


async def test_custom_api_version_flows_through(tmp_path):
    config = _azure_config(api_version="2024-08-01-preview")
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "versioned"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="versioned call"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert "api-version=2024-08-01-preview" in env.mock.requests[-1]["url"]
        break


async def test_streaming_via_azure(tmp_path):
    async for env in _make_env(tmp_path, config=_azure_config()):
        key = (await create_key(env.client, "azure-stream"))["key"]
        async with env.client.stream(
            "POST",
            "/v1/chat/completions",
            json=chat_body(
                content="stream please",
                stream=True,
                stream_options={"include_usage": True},
            ),
            headers=auth(key),
        ) as resp:
            lines = await read_sse(resp)
        assert lines[-1] == "[DONE]"
        # the parent forces stream_options.include_usage so usage arrives
        sent = env.mock.requests[-1]
        assert sent["body"]["stream_options"] == {"include_usage": True}
        usage_chunks = [json.loads(l) for l in lines[:-1] if json.loads(l).get("usage")]
        assert usage_chunks, "expected a usage chunk"
        assert resp.headers["x-gateway-provider"] == "azure"
        break


async def test_embeddings_via_azure(tmp_path):
    config = _azure_config(
        models=[
            ModelConfig(
                name="embed-model",
                deployments=[
                    DeploymentConfig(
                        name="azure/emb", provider="azure", model="text-emb-deploy"
                    )
                ],
            )
        ],
        pricing={"text-emb-deploy": {"input": 0.02}},
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "azure-embed"))["key"]
        resp = await env.client.post(
            "/v1/embeddings",
            json={"model": "embed-model", "input": "hello"},
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == "embed-model"
        sent = env.mock.requests[-1]
        assert sent["url"] == (
            "http://mockupstream.test/openai/deployments/text-emb-deploy"
            f"/embeddings?api-version={DEFAULT_API_VERSION}"
        )
        assert sent["headers"].get("api-key") == "azure-key"
        break


async def test_azure_falls_over_to_openai(tmp_path):
    config = make_config(
        models=[
            ModelConfig(
                name="test-model",
                deployments=[
                    DeploymentConfig(
                        name="azure/primary", provider="azure", model="gpt-4o-deploy"
                    ),
                    DeploymentConfig(
                        name="openai/fallback",
                        provider="openai",
                        model="model-b",
                        priority=200,
                    ),
                ],
            )
        ],
        pricing={
            "gpt-4o-deploy": {"input": 2.0, "output": 4.0},
            "model-b": {"input": 2.0, "output": 4.0},
        },
    )
    config.providers["azure"] = ProviderConfig(
        api_base="http://mockupstream.test", api_key="azure-key"
    )
    async for env in _make_env(tmp_path, config=config):
        # Exhaust the retry budget on the azure deployment (3 attempts:
        # initial + 2 retries) → router falls over to the next tier.
        env.mock.fail_counts["gpt-4o-deploy"] = 3
        key = (await create_key(env.client, "failover"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="failover me"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.headers["x-gateway-provider"] == "openai"
        assert resp.headers["x-gateway-deployment"] == "openai/fallback"
        break
