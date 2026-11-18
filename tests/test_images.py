"""Image generation (/v1/images/generations) — routing, fallback, metering,
logging and the Gemini translation."""

from __future__ import annotations

import base64

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
    assert calc.lookup("model-a") == (2.0, 4.0, 1.0)  # token prices still parse; cached defaults to 50% of input
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


# ----------------------------------------------------------------------
# Image edits (/v1/images/edits)
# ----------------------------------------------------------------------


def _image_file(b: bytes = b"pngbytes-here", name: str = "cat.png") -> dict:
    return {"image": (name, b, "image/png")}


async def test_image_edit_e2e(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(pricing={"model-a": {"image_usd": 0.02}})
    ):
        key = (await create_key(env.client, "editor"))["key"]
        resp = await env.client.post(
            "/v1/images/edits",
            data={"model": "test-model", "prompt": "make it a watercolor"},
            files=_image_file(),
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["data"][0]["b64_json"]
        assert resp.headers["x-gateway-cost-usd"] == f"{0.02:.8f}"

        sent = env.mock.requests[-1]
        assert sent["url"].endswith("/images/edits")
        fields = sent["body"]["_multipart"]
        assert fields["model"] == "model-a"  # native model
        assert fields["prompt"] == "make it a watercolor"
        assert fields["image"] == "cat.png"  # single image -> `image` field
        assert sent["body"]["_files"]["image"] == [b"pngbytes-here"]
        break


async def test_image_edit_b64_string_input(tmp_path):
    """A plain-string `image` form value is accepted as base64 data (with or
    without a data: URL prefix) and forwarded as a file."""
    import base64

    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "editor"))["key"]
        raw = b"secret-image-bytes"
        for value in (base64.b64encode(raw).decode(), f"data:image/png;base64,{base64.b64encode(raw).decode()}"):
            resp = await env.client.post(
                "/v1/images/edits",
                data={"model": "test-model", "prompt": "edit", "image": value},
                headers=auth(key),
            )
            assert resp.status_code == 200, resp.text
            sent = env.mock.requests[-1]
            assert sent["body"]["_files"]["image"] == [raw]
        break


async def test_image_edit_invalid_b64_rejected(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "editor"))["key"]
        resp = await env.client.post(
            "/v1/images/edits",
            data={"model": "test-model", "prompt": "edit", "image": "not base64!!"},
            headers=auth(key),
        )
        assert resp.status_code == 400
        assert "image" in resp.json()["error"]["message"]
        break


async def test_image_edit_multiple_images(tmp_path):
    """Several images ride along as repeated image[] fields (gpt-image-1
    multi-reference form)."""
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "editor"))["key"]
        resp = await env.client.post(
            "/v1/images/edits",
            data={"model": "test-model", "prompt": "combine"},
            files=[("image", ("a.png", b"aaaa", "image/png")), ("image", ("b.png", b"bbbb", "image/png"))],
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        sent = env.mock.requests[-1]
        assert sent["body"]["_multipart"]["image[]"] == "b.png"
        assert sent["body"]["_files"]["image[]"] == [b"aaaa", b"bbbb"]
        break


async def test_image_edit_mask_forwarded(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "editor"))["key"]
        resp = await env.client.post(
            "/v1/images/edits",
            data={"model": "test-model", "prompt": "edit"},
            files={"image": ("cat.png", b"img", "image/png"), "mask": ("mask.png", b"msk", "image/png")},
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        sent = env.mock.requests[-1]
        assert sent["body"]["_multipart"]["mask"] == "mask.png"
        assert sent["body"]["_files"]["mask"] == [b"msk"]
        break


async def test_image_edit_gemini_translation(tmp_path):
    """Edits via Gemini: uploaded image goes as an inlineData input part
    next to the text prompt (the gemini-2.5-flash-image editing flow)."""
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
        key = (await create_key(env.client, "editor"))["key"]
        resp = await env.client.post(
            "/v1/images/edits",
            data={"model": "image-model", "prompt": "make it a watercolor"},
            files=_image_file(b"gemini-input-image"),
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"][0]["b64_json"] == "Z2VtaW5pLWltZw=="
        call = env.mock.requests[-1]
        assert "IMAGE" in call["body"]["generationConfig"]["responseModalities"]
        parts = call["body"]["contents"][0]["parts"]
        assert parts[0]["inlineData"]["data"] == base64.b64encode(
            b"gemini-input-image"
        ).decode()
        assert parts[1]["text"] == "make it a watercolor"
        break


async def test_image_edit_size_cap_413(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        env.app.state.config.uploads.max_upload_mb = 0
        key = (await create_key(env.client, "editor"))["key"]
        resp = await env.client.post(
            "/v1/images/edits",
            data={"model": "test-model", "prompt": "edit"},
            files=_image_file(b"x"),
            headers=auth(key),
        )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "upload_too_large"
        break


async def test_image_edit_missing_fields_400(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "editor"))["key"]
        no_image = await env.client.post(
            "/v1/images/edits",
            data={"model": "test-model", "prompt": "edit"},
            headers=auth(key),
        )
        assert no_image.status_code == 400
        assert "image" in no_image.json()["error"]["message"]
        no_prompt = await env.client.post(
            "/v1/images/edits",
            data={"model": "test-model"},
            files=_image_file(),
            headers=auth(key),
        )
        assert no_prompt.status_code == 400
        assert "prompt" in no_prompt.json()["error"]["message"]
        no_model = await env.client.post(
            "/v1/images/edits",
            data={"prompt": "edit"},
            files=_image_file(),
            headers=auth(key),
        )
        assert no_model.status_code == 400
        assert "model" in no_model.json()["error"]["message"]
        break


async def test_image_edit_falls_back_from_unsupported_provider(tmp_path):
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
                            name="anthropic/images", provider="anthropic",
                            model="claude-sonnet-4-5", priority=10,
                        ),
                        DeploymentConfig(
                            name="openai/images", provider="openai",
                            model="model-a", priority=20,
                        ),
                    ],
                )
            ],
        ),
    ):
        key = (await create_key(env.client, "editor"))["key"]
        resp = await env.client.post(
            "/v1/images/edits",
            data={"model": "image-model", "prompt": "edit"},
            files=_image_file(),
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["x-gateway-deployment"] == "openai/images"
        break


async def test_image_edit_scope_covers_edits(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (
            await create_key(env.client, "chat-only", allowed_endpoints=["chat"])
        )["key"]
        resp = await env.client.post(
            "/v1/images/edits",
            data={"model": "test-model", "prompt": "edit"},
            files=_image_file(),
            headers=auth(key),
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "endpoint_not_allowed"
        break


async def test_image_edit_logged_without_bytes(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "editor"))["key"]
        resp = await env.client.post(
            "/v1/images/edits",
            data={"model": "test-model", "prompt": "edit"},
            files={"image": ("cat.png", b"image-bytes", "image/png"), "mask": ("m.png", b"mask-bytes", "image/png")},
            headers=auth(key),
        )
        assert resp.status_code == 200
        logs = await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        row = logs.json()["data"][0]
        assert row["status"] == "success"
        detail = await env.client.get(
            f"/admin/logs/{row['id']}", headers=auth(ADMIN_KEY)
        )
        full = detail.json()
        # Metadata about the uploads, never the bytes.
        assert full["request_body"]["images"] == [
            {"file_name": "cat.png", "size_bytes": 11}
        ]
        assert full["request_body"]["mask"] == {"file_name": "m.png", "size_bytes": 10}
        assert full["request_body"]["prompt"] == "edit"
        assert full["response_body"]["data"]
        break


# ----------------------------------------------------------------------
# Image variations (/v1/images/variations)
# ----------------------------------------------------------------------


async def test_image_variation_e2e(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(pricing={"model-a": {"image_usd": 0.02}})
    ):
        key = (await create_key(env.client, "editor"))["key"]
        resp = await env.client.post(
            "/v1/images/variations",
            data={"model": "test-model", "n": "2"},
            files=_image_file(),
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["data"]) == 2
        assert float(resp.headers["x-gateway-cost-usd"]) == pytest.approx(0.04)

        sent = env.mock.requests[-1]
        assert sent["url"].endswith("/images/variations")
        fields = sent["body"]["_multipart"]
        assert fields["model"] == "model-a"
        assert "prompt" not in fields  # variations carry no prompt
        assert sent["body"]["_files"]["image"] == [b"pngbytes-here"]
        break


async def test_image_variation_gemini_unsupported_501(tmp_path):
    """Gemini has no variations API: a gemini-only model surfaces the
    modality gap as a clear 501 (nothing to fall through to)."""
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
                            name="gemini/images", provider="gemini",
                            model="gemini-2.5-flash-image",
                        )
                    ],
                )
            ],
        ),
    ):
        key = (await create_key(env.client, "editor"))["key"]
        resp = await env.client.post(
            "/v1/images/variations",
            data={"model": "image-model"},
            files=_image_file(),
            headers=auth(key),
        )
        assert resp.status_code == 501
        assert "does not support image variations" in resp.json()["error"]["message"]
        break
