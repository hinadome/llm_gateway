"""Multimodal chat — audio input (input_audio parts) across every inbound
format and provider translation, plus image-to-image chat (Gemini image
models on the chat surface)."""

from __future__ import annotations

import pytest

from gateway.config import DeploymentConfig, ModelConfig, ProviderConfig
from gateway.errors import ProviderError
from gateway.providers import anthropic_adapter, gemini_adapter
from gateway.schemas import ChatCompletionRequest, Message
from gateway.utils import audio_format_for_mime, audio_mime_for_format, content_to_text

from .conftest import ADMIN_KEY, _make_env, auth, create_key, make_config


def _gemini_config(model: str = "gemini-2.5-flash", name: str = "chat-model") -> ProviderConfig:
    """A config with a single Gemini chat deployment."""
    return make_config(
        providers={
            "gemini": ProviderConfig(api_base="http://mockupstream.test", api_key="gk")
        },
        models=[
            ModelConfig(
                name=name,
                deployments=[
                    DeploymentConfig(
                        name=f"gemini/{model}", provider="gemini", model=model
                    )
                ],
            )
        ],
        # Routing groups resolve per model; keep chat semantics.
    )


def _audio_part(data: str = "QUJDREVG", fmt: str = "wav") -> dict:
    return {"type": "input_audio", "input_audio": {"data": data, "format": fmt}}


def _chat_request(content, model: str = "m") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model, messages=[Message(role="user", content=content)]
    )


# ----------------------------------------------------------------------
# Units: helpers and translations
# ----------------------------------------------------------------------


def test_content_to_text_audio_placeholder():
    content = [
        {"type": "text", "text": "listen "},
        _audio_part(),
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}},
    ]
    assert content_to_text(content) == "listen [audio][image]"


def test_audio_format_mime_round_trip():
    assert audio_mime_for_format("wav") == "audio/wav"
    assert audio_mime_for_format("mp3") == "audio/mpeg"
    assert audio_mime_for_format("ogg") == "audio/ogg"
    assert audio_format_for_mime("audio/wav") == "wav"
    assert audio_format_for_mime("audio/mpeg") == "mp3"
    assert audio_format_for_mime("audio/mp3") == "mp3"
    # Parameters ride along and round-trip.
    assert audio_format_for_mime("audio/L16;rate=24000") == "L16;rate=24000"
    assert audio_mime_for_format("L16;rate=24000") == "audio/L16;rate=24000"
    assert audio_format_for_mime(None) == "wav"


def test_gemini_audio_input_translation():
    body = gemini_adapter.translate_request(
        _chat_request([{"type": "text", "text": "what do you hear?"},
                       _audio_part(fmt="mp3"),
                       _audio_part(fmt="wav", data="WFo=")]),
        "gemini-2.5-flash",
    )
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"text": "what do you hear?"}
    assert parts[1] == {"inlineData": {"mimeType": "audio/mpeg", "data": "QUJDREVG"}}
    assert parts[2] == {"inlineData": {"mimeType": "audio/wav", "data": "WFo="}}


def test_gemini_text_and_image_input_unchanged():
    """Non-audio parts (text, images) translate exactly as before."""
    body = gemini_adapter.translate_request(
        _chat_request([
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}},
        ]),
        "gemini-2.5-flash",
    )
    assert body["contents"][0]["parts"] == [
        {"text": "hi"},
        {"inlineData": {"mimeType": "image/png", "data": "QQ=="}},
    ]
    # Regular chat models get no responseModalities.
    assert "responseModalities" not in (body.get("generationConfig") or {})


def test_anthropic_audio_input_refused():
    """Anthropic has no audio input: the request is refused loudly (with
    fallback, so a mixed routing group still serves it elsewhere)."""
    with pytest.raises(ProviderError) as excinfo:
        anthropic_adapter.translate_request(
            _chat_request([{"type": "text", "text": "hi"}, _audio_part()]),
            "claude-sonnet-4-5",
        )
    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "audio_input_not_supported"
    assert excinfo.value.fallback is True


def test_anthropic_text_and_images_still_translate():
    body = anthropic_adapter.translate_request(
        _chat_request([
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}},
        ]),
        "claude-sonnet-4-5",
    )
    assert body["messages"][0]["content"] == [
        {"type": "text", "text": "hi"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QQ=="}},
    ]


def test_gemini_image_output_translation():
    native = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "here is your image"},
                        {"inlineData": {"mimeType": "image/png", "data": "aW1n"}},
                    ]
                },
                "finishReason": "STOP",
            }
        ]
    }
    out = gemini_adapter.translate_response(native, "image-model")
    message = out["choices"][0]["message"]
    assert message["content"] == [
        {"type": "text", "text": "here is your image"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1n"}},
    ]


def test_gemini_image_model_requests_image_modality():
    body = gemini_adapter.translate_request(
        _chat_request([
            {"type": "text", "text": "make it a watercolor"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}},
        ]),
        "gemini-2.5-flash-image",
    )
    assert body["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
    # The input image rides as an inline part next to the prompt.
    assert body["contents"][0]["parts"] == [
        {"text": "make it a watercolor"},
        {"inlineData": {"mimeType": "image/png", "data": "QQ=="}},
    ]


# ----------------------------------------------------------------------
# e2e: audio input through every inbound format
# ----------------------------------------------------------------------


async def test_openai_surface_audio_input_passthrough(tmp_path):
    """OpenAI surface -> OpenAI upstream: the input_audio part rides along
    verbatim (the adapter is a passthrough for content parts)."""
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "listener"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "what do you hear?"},
                            _audio_part(fmt="mp3"),
                        ],
                    }
                ],
            },
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        sent = env.mock.requests[-1]["body"]
        assert sent["messages"][0]["content"] == [
            {"type": "text", "text": "what do you hear?"},
            {"type": "input_audio", "input_audio": {"data": "QUJDREVG", "format": "mp3"}},
        ]
        break


async def test_audio_input_falls_back_from_anthropic(tmp_path):
    """Anthropic (primary) refuses audio input; the routing group falls
    through to the OpenAI deployment, which accepts it."""
    config = make_config(
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
                name="test-model",
                deployments=[
                    DeploymentConfig(
                        name="anthropic/primary", provider="anthropic",
                        model="claude-sonnet-4-5", priority=10,
                    ),
                    DeploymentConfig(
                        name="openai/fallback", provider="openai",
                        model="model-b", priority=200,
                    ),
                ],
            )
        ],
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "listener"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [
                    {"role": "user", "content": [_audio_part(), {"type": "text", "text": "hi"}]}
                ],
            },
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["x-gateway-deployment"] == "openai/fallback"
        break


async def test_audio_input_anthropic_only_is_400(tmp_path):
    """Nothing in the routing group accepts audio -> the clear 400 surfaces."""
    config = make_config(
        providers={
            "anthropic": ProviderConfig(
                api_base="http://mockupstream.test", api_key="k"
            )
        },
        models=[
            ModelConfig(
                name="claude-only",
                deployments=[
                    DeploymentConfig(
                        name="anthropic/only", provider="anthropic",
                        model="claude-sonnet-4-5",
                    )
                ],
            )
        ],
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "listener"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-only",
                "messages": [{"role": "user", "content": [_audio_part()]}],
            },
            headers=auth(key),
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "audio_input_not_supported"
        assert "audio" in resp.json()["error"]["message"].lower()
        break


async def test_gemini_audio_input_e2e(tmp_path):
    """OpenAI surface -> Gemini upstream: input_audio becomes inlineData."""
    async for env in _make_env(tmp_path, config=_gemini_config()):
        key = (await create_key(env.client, "listener"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json={
                "model": "chat-model",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "what do you hear?"},
                            _audio_part(fmt="mp3"),
                        ],
                    }
                ],
            },
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["choices"][0]["message"]["content"] == "heard: what do you hear?"
        sent = env.mock.requests[-1]["body"]
        parts = sent["contents"][0]["parts"]
        assert parts[0] == {"text": "what do you hear?"}
        assert parts[1] == {"inlineData": {"mimeType": "audio/mpeg", "data": "QUJDREVG"}}
        break


async def test_gemini_native_audio_round_trip(tmp_path):
    """Gemini-native inbound audio (inlineData) survives the whole trip:
    parsed to input_audio internally, sent back out as inlineData."""
    async for env in _make_env(tmp_path, config=_gemini_config()):
        key = (await create_key(env.client, "listener"))["key"]
        resp = await env.client.post(
            "/v1/models/chat-model:generateContent",
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": "transcribe this"},
                            {
                                "inlineData": {
                                    "mimeType": "audio/wav",
                                    "data": "QUJDREVG",
                                }
                            },
                        ],
                    }
                ]
            },
            headers={**auth(key), "x-goog-api-key": key},
        )
        assert resp.status_code == 200, resp.text
        native = resp.json()
        assert native["candidates"][0]["content"]["parts"] == [
            {"text": "heard: transcribe this"}
        ]
        sent = env.mock.requests[-1]["body"]
        assert sent["contents"][0]["parts"][1] == {
            "inlineData": {"mimeType": "audio/wav", "data": "QUJDREVG"}
        }
        break


async def test_responses_bridge_audio_input(tmp_path):
    """The Responses bridge accepts input_audio parts (the Agents SDK shape)."""
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "listener"))["key"]
        resp = await env.client.post(
            "/v1/responses",
            json={
                "model": "test-model",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "what do you hear?"},
                            {
                                "type": "input_audio",
                                "input_audio": {"data": "QUJDREVG", "format": "wav"},
                            },
                        ],
                    }
                ],
            },
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        sent = env.mock.requests[-1]["body"]
        assert sent["messages"][0]["content"] == [
            {"type": "text", "text": "what do you hear?"},
            {"type": "input_audio", "input_audio": {"data": "QUJDREVG", "format": "wav"}},
        ]
        break


async def test_responses_bridge_audio_requires_data(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "listener"))["key"]
        resp = await env.client.post(
            "/v1/responses",
            json={
                "model": "test-model",
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_audio", "input_audio": {"format": "wav"}}],
                    }
                ],
            },
            headers=auth(key),
        )
        assert resp.status_code == 400
        assert "input_audio" in resp.json()["error"]["message"]
        break


# ----------------------------------------------------------------------
# e2e: image-to-image chat (Gemini image models on the chat surface)
# ----------------------------------------------------------------------


async def test_image_to_image_chat(tmp_path):
    """Chat with a Gemini image model: image in (image_url) + text -> image
    out (image_url data part in the assistant content)."""
    async for env in _make_env(
        tmp_path, config=_gemini_config("gemini-2.5-flash-image", "image-model")
    ):
        key = (await create_key(env.client, "artist"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json={
                "model": "image-model",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "make it a watercolor"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,QQ=="},
                            },
                        ],
                    }
                ],
            },
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        message = resp.json()["choices"][0]["message"]
        assert message["content"] == [
            {"type": "text", "text": "here is your image"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,Z2VtaW5pLWltZw=="},
            },
        ]
        # The upstream call asked for image output and carried the input image.
        sent = env.mock.requests[-1]["body"]
        assert sent["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
        assert sent["contents"][0]["parts"] == [
            {"text": "make it a watercolor"},
            {"inlineData": {"mimeType": "image/png", "data": "QQ=="}},
        ]
        break


async def test_image_chat_streaming_refused(tmp_path):
    """stream: true on an image model would drop the image from the stream —
    the gateway refuses with a clear 400 instead."""
    async for env in _make_env(
        tmp_path, config=_gemini_config("gemini-2.5-flash-image", "image-model")
    ):
        key = (await create_key(env.client, "artist"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json={
                "model": "image-model",
                "stream": True,
                "messages": [{"role": "user", "content": "make it a watercolor"}],
            },
            headers=auth(key),
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "image_streaming_not_supported"
        assert "stream: false" in resp.json()["error"]["message"]
        break


async def test_gemini_native_image_round_trip(tmp_path):
    """Gemini-native surface, image model: inline image in -> inline image
    out, with responseModalities set on the southbound call."""
    async for env in _make_env(
        tmp_path, config=_gemini_config("gemini-2.5-flash-image", "image-model")
    ):
        key = (await create_key(env.client, "artist"))["key"]
        resp = await env.client.post(
            "/v1/models/image-model:generateContent",
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": "make it a watercolor"},
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": "QQ==",
                                }
                            },
                        ],
                    }
                ]
            },
            headers={**auth(key), "x-goog-api-key": key},
        )
        assert resp.status_code == 200, resp.text
        parts = resp.json()["candidates"][0]["content"]["parts"]
        assert {"text": "here is your image"} in parts
        assert {
            "inlineData": {"mimeType": "image/png", "data": "Z2VtaW5pLWltZw=="}
        } in parts
        sent = env.mock.requests[-1]["body"]
        assert sent["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
        break


async def test_anthropic_native_image_note(tmp_path):
    """Anthropic-native surface, Gemini image model: assistant messages
    cannot carry image blocks, so the gateway renders an honest text note."""
    async for env in _make_env(
        tmp_path, config=_gemini_config("gemini-2.5-flash-image", "image-model")
    ):
        key = (await create_key(env.client, "artist"))["key"]
        resp = await env.client.post(
            "/v1/messages",
            json={
                "model": "image-model",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "make it a watercolor"}],
            },
            headers={"x-api-key": key},
        )
        assert resp.status_code == 200, resp.text
        blocks = resp.json()["content"]
        assert {"type": "text", "text": "here is your image"} in blocks
        assert {"type": "text", "text": "[generated image: image/png]"} in blocks
        break


async def test_audio_chat_logged_faithfully(tmp_path):
    """Chat request bodies are logged verbatim when store_bodies is on —
    an input_audio part appears exactly as sent (same policy as image data
    URLs in chat); guardrails and token estimation only ever see the
    [audio] placeholder (content_to_text, unit-tested above)."""
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "listener"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "secret-unicorn"},
                            _audio_part(data="U0VDUkVUQVVESU8="),
                        ],
                    }
                ],
            },
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        logs = await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        row = logs.json()["data"][0]
        detail = await env.client.get(
            f"/admin/logs/{row['id']}", headers=auth(ADMIN_KEY)
        )
        body = detail.json()["request_body"]
        assert body["messages"][0]["content"] == [
            {"type": "text", "text": "secret-unicorn"},
            {
                "type": "input_audio",
                "input_audio": {"data": "U0VDUkVUQVVESU8=", "format": "wav"},
            },
        ]
        break
