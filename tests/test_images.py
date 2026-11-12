"""Image generation (/v1/images/generations) — routing, fallback, metering,
logging and the Gemini translation."""

from __future__ import annotations

import pytest

from gateway.core.cost import CostCalculator

from .conftest import ADMIN_KEY, _make_env, auth, create_key, make_config


def _image_body(prompt: str = "a cat", **extra) -> dict:
    return {"model": "test-model", "prompt": prompt, **extra}


# ----------------------------------------------------------------------
# CostCalculator units
# ----------------------------------------------------------------------


def test_unit_pricing_lookup_and_overrides():
    calc = CostCalculator(
        {"gpt-image-1": {"image_usd": 0.02}, "my-image": {"image_usd": 1.0}}
    )
    # Override wins.
    assert calc.calculate_images("gpt-image-1", 3) == pytest.approx(0.06)
    # Config-only model.
    assert calc.calculate_images("my-image", 2) == pytest.approx(2.0)
    # Built-in defaults, longest-prefix.
    assert calc.calculate_images("whisper-1", 0) == 0.0  # audio model: no image price
    assert calc.calculate_images("dall-e-3-preview", 1) == pytest.approx(0.04)
    assert calc.calculate_audio("whisper-1", 1.0) == pytest.approx(0.006)  # 1 min
    assert calc.calculate_audio("whisper-1", 60.0) == pytest.approx(0.36)  # an hour
    assert calc.calculate_speech("tts-1", 1_000_000) == pytest.approx(15.0)
    # Unknown model: no price, no cost.
    assert calc.calculate_images("no-such-model", 5) == 0.0


def test_unit_pricing_token_fields_untouched():
    calc = CostCalculator({"model-a": {"input": 2.0, "output": 4.0, "image_usd": 0.5}})
    assert calc.lookup("model-a") == (2.0, 4.0)  # token prices still parse
    assert calc.calculate_images("model-a", 2) == pytest.approx(1.0)


# ----------------------------------------------------------------------
# e2e through the app
# ----------------------------------------------------------------------


async def test_image_generation_e2e(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(pricing={"model-a": {"image_usd": 0.02}})
    ):
        key = (await create_key(env.client, "artist"))["key"]
        resp = await env.client.post(
            "/v1/images/generations",
            json=_image_body(),
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"]
        assert len(body["data"]) == 1
        assert body["data"][0]["b64_json"]
        # Served by the primary deployment, priced per image.
        assert resp.headers["x-gateway-deployment"] == "openai/primary"
        assert resp.headers["x-gateway-cost-usd"] == f"{0.02:.8f}"
        # The provider got the native model + prompt.
        sent = env.mock.requests[-1]
        assert sent["url"].endswith("/images/generations")
        assert sent["body"]["model"] == "model-a"
        assert sent["body"]["prompt"] == "a cat"
        break


async def test_image_generation_n_bills_per_image(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(pricing={"model-a": {"image_usd": 0.02}})
    ):
        key = (await create_key(env.client, "artist"))["key"]
        resp = await env.client.post(
            "/v1/images/generations",
            json=_image_body(n=3),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 3
        assert float(resp.headers["x-gateway-cost-usd"]) == pytest.approx(0.06)
        break


async def test_image_generation_unpriced_model_zero_cost(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "artist"))["key"]
        resp = await env.client.post(
            "/v1/images/generations", json=_image_body(), headers=auth(key)
        )
        assert resp.status_code == 200
        assert "x-gateway-cost-usd" not in resp.headers
        break


async def test_image_generation_falls_back_from_unsupported_provider(tmp_path):
    """A chat-only provider (Anthropic) 501s on images and the routing group
    falls through to the OpenAI deployment."""
    from gateway.config import DeploymentConfig, ModelConfig, ProviderConfig

    async for env in _make_env(
        tmp_path,
        config=make_config(
            providers={
                "openai": ProviderConfig(
                    api_base="http://mockupstream.test/v1", api_key="k"
                ),
                "anthropic": ProviderConfig(
                    api_base="http://mockupstream.test", api_key="k"
                ),
            },
            models=[
                ModelConfig(
                    name="image-model",
                    deployments=[
                        DeploymentConfig(
                            name="anthropic/images",
                            provider="anthropic",
                            model="claude-sonnet-4-5",
                            priority=10,
                        ),
                        DeploymentConfig(
                            name="openai/images",
                            provider="openai",
                            model="model-a",
                            priority=20,
                        ),
                    ],
                )
            ],
        ),
    ):
        key = (await create_key(env.client, "artist"))["key"]
        resp = await env.client.post(
            "/v1/images/generations",
            json={"model": "image-model", "prompt": "a cat"},
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["x-gateway-provider"] == "openai"
        assert resp.headers["x-gateway-deployment"] == "openai/images"
        break


async def test_image_generation_breaker_and_fallback(tmp_path):
    from gateway.config import DeploymentConfig, ModelConfig

    async for env in _make_env(
        tmp_path,
        config=make_config(
            models=[
                ModelConfig(
                    name="test-model",
                    deployments=[
                        DeploymentConfig(
                            name="openai/primary", provider="openai", model="model-a"
                        ),
                        DeploymentConfig(
                            name="openai/fallback",
                            provider="openai",
                            model="model-b",
                            priority=200,
                        ),
                    ],
                )
            ]
        ),
    ):
        key = (await create_key(env.client, "artist"))["key"]
        env.mock.fail_counts["model-a"] = 99
        for i in range(3):  # threshold 3 (unique prompts: no cache concerns)
            resp = await env.client.post(
                "/v1/images/generations",
                json=_image_body(prompt=f"cat {i}"),
                headers=auth(key),
            )
            assert resp.status_code == 200  # fallback served every time
            assert resp.headers["x-gateway-deployment"] == "openai/fallback"
        breakers = env.app.state.pipeline.router.breakers
        snapshots = await breakers.snapshots()
        assert snapshots["openai/primary"]["state"] == "open"
        break


async def test_image_generation_rate_limited(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "limited", rpm_limit=1))["key"]
        first = await env.client.post(
            "/v1/images/generations", json=_image_body(), headers=auth(key)
        )
        assert first.status_code == 200
        second = await env.client.post(
            "/v1/images/generations",
            json=_image_body(prompt="another cat"),
            headers=auth(key),
        )
        assert second.status_code == 429
        break


async def test_image_generation_model_allowlist(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (
            await create_key(env.client, "restricted", allowed_models=["other-model"])
        )["key"]
        resp = await env.client.post(
            "/v1/images/generations", json=_image_body(), headers=auth(key)
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "model_not_found"
        break


async def test_image_generation_endpoint_scope(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (
            await create_key(env.client, "chat-only", allowed_endpoints=["chat"])
        )["key"]
        resp = await env.client.post(
            "/v1/images/generations", json=_image_body(), headers=auth(key)
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "endpoint_not_allowed"
        break


async def test_image_generation_stream_rejected(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "artist"))["key"]
        resp = await env.client.post(
            "/v1/images/generations",
            json=_image_body(stream=True),
            headers=auth(key),
        )
        assert resp.status_code == 400
        assert "stream" in resp.json()["error"]["message"].lower()
        break


async def test_image_generation_requires_auth(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        resp = await env.client.post("/v1/images/generations", json=_image_body())
        assert resp.status_code == 401
        break


async def test_image_generation_logged(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(pricing={"model-a": {"image_usd": 0.02}})
    ):
        key = (await create_key(env.client, "artist"))["key"]
        resp = await env.client.post(
            "/v1/images/generations", json=_image_body(), headers=auth(key)
        )
        assert resp.status_code == 200
        logs = await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        rows = logs.json()["data"]
        assert rows, "expected a log row"
        row = rows[0]  # newest first
        assert row["model"] == "test-model"
        assert row["provider"] == "openai"
        assert row["status"] == "success"
        assert row["cost_usd"] == pytest.approx(0.02)
        detail = await env.client.get(
            f"/admin/logs/{row['id']}", headers=auth(ADMIN_KEY)
        )
        full = detail.json()
        assert full["request_body"]["prompt"] == "a cat"
        assert full["response_body"]["data"][0]["b64_json"]
        break


async def test_image_generation_gemini_translation(tmp_path):
    from gateway.config import DeploymentConfig, ModelConfig, ProviderConfig

    async for env in _make_env(
        tmp_path,
        config=make_config(
            providers={
                "gemini": ProviderConfig(
                    api_base="http://mockupstream.test", api_key="gk"
                )
            },
            models=[
                ModelConfig(
                    name="image-model",
                    deployments=[
                        DeploymentConfig(
                            name="gemini/images",
                            provider="gemini",
                            model="gemini-2.5-flash-image",
                        )
                    ],
                )
            ],
        ),
    ):
        key = (await create_key(env.client, "artist"))["key"]
        resp = await env.client.post(
            "/v1/images/generations",
            json={"model": "image-model", "prompt": "a cat", "n": 2},
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert len(data) == 2  # n=2 -> two sequential generateContent calls
        assert data[0]["b64_json"] == "Z2VtaW5pLWltZw=="
        # Both calls carried the image modality config.
        gen_calls = [
            r for r in env.mock.requests if ":generateContent" in r["url"]
        ]
        assert len(gen_calls) == 2
        for call in gen_calls:
            assert "IMAGE" in call["body"]["generationConfig"]["responseModalities"]
            assert call["body"]["contents"][0]["parts"][0]["text"] == "a cat"
        break
