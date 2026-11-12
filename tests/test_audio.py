"""Audio endpoints — /v1/audio/transcriptions, /v1/audio/translations
(multipart) and /v1/audio/speech (binary TTS out): multipart forwarding,
duration-based metering, char-based speech metering, routing/fallback,
size caps and logging."""

from __future__ import annotations

import pytest

from .conftest import ADMIN_KEY, _make_env, auth, create_key, make_config


def _files(b: bytes = b"fake-audio-bytes", name: str = "audio.webm") -> dict:
    return {"file": (name, b, "audio/webm")}


# ----------------------------------------------------------------------
# Transcription + translation
# ----------------------------------------------------------------------


async def test_transcription_e2e_and_upstream_multipart(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(pricing={"model-a": {"audio_usd_per_minute": 0.006}}),
    ):
        key = (await create_key(env.client, "scribe"))["key"]
        resp = await env.client.post(
            "/v1/audio/transcriptions",
            data={
                "model": "test-model",
                "language": "fr",
                "prompt": "context here",
                "response_format": "verbose_json",
            },
            files=_files(),
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["text"] == "transcribed words"
        assert body["duration"] == 42.0

        # What the provider received: multipart with the NATIVE model and
        # the file under its original name.
        sent = env.mock.requests[-1]
        assert sent["url"].endswith("/audio/transcriptions")
        fields = sent["body"]["_multipart"]
        assert fields["model"] == "model-a"
        assert fields["file"] == "audio.webm"
        assert fields["language"] == "fr"
        assert fields["prompt"] == "context here"

        # Duration-based cost: 42s = 0.7 min * $0.006/min.
        assert float(resp.headers["x-gateway-cost-usd"]) == pytest.approx(0.0042)
        break


async def test_transcription_unknown_fields_pass_through(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "scribe"))["key"]
        resp = await env.client.post(
            "/v1/audio/transcriptions",
            data={"model": "test-model", "custom_param": "x"},
            files=_files(),
            headers=auth(key),
        )
        assert resp.status_code == 200
        sent = env.mock.requests[-1]
        assert sent["body"]["_multipart"]["custom_param"] == "x"
        break


async def test_transcription_json_format_has_no_duration_no_cost(tmp_path):
    """Only verbose_json reports a duration — plain json costs nothing (a
    documented trade-off: use verbose_json when billing matters)."""
    async for env in _make_env(
        tmp_path,
        config=make_config(pricing={"model-a": {"audio_usd_per_minute": 0.006}}),
    ):
        key = (await create_key(env.client, "scribe"))["key"]
        resp = await env.client.post(
            "/v1/audio/transcriptions",
            data={"model": "test-model", "response_format": "json"},
            files=_files(),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert "duration" not in resp.json()
        assert "x-gateway-cost-usd" not in resp.headers
        break


async def test_transcription_text_format_passthrough(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "scribe"))["key"]
        resp = await env.client.post(
            "/v1/audio/transcriptions",
            data={"model": "test-model", "response_format": "text"},
            files=_files(),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert "application/json" not in resp.headers["content-type"]
        assert resp.text == "transcribed words"
        break


async def test_translation_uses_translations_endpoint(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "scribe"))["key"]
        resp = await env.client.post(
            "/v1/audio/translations",
            data={"model": "test-model"},
            files=_files(b"bytes-2"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        sent = env.mock.requests[-1]
        assert sent["url"].endswith("/audio/translations")
        assert sent["body"]["endpoint"] == "translations"
        break


async def test_transcription_size_cap_413(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        env.app.state.config.uploads.max_upload_mb = 0  # any file is too large
        key = (await create_key(env.client, "scribe"))["key"]
        resp = await env.client.post(
            "/v1/audio/transcriptions",
            data={"model": "test-model"},
            files=_files(b"x"),
            headers=auth(key),
        )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "audio_file_too_large"
        break


async def test_transcription_missing_file_400(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "scribe"))["key"]
        resp = await env.client.post(
            "/v1/audio/transcriptions",
            data={"model": "test-model"},
            headers=auth(key),
        )
        assert resp.status_code == 400  # the gateway's validation envelope
        assert "file" in resp.json()["error"]["message"]
        break


async def test_transcription_missing_model_400(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "scribe"))["key"]
        resp = await env.client.post(
            "/v1/audio/transcriptions",
            files=_files(),
            headers=auth(key),
        )
        assert resp.status_code == 400
        assert "model" in resp.json()["error"]["message"]
        break


async def test_transcription_model_allowlist(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (
            await create_key(env.client, "restricted", allowed_models=["other-model"])
        )["key"]
        resp = await env.client.post(
            "/v1/audio/transcriptions",
            data={"model": "test-model"},
            files=_files(),
            headers=auth(key),
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "model_not_found"
        break


async def test_transcription_falls_back_from_unsupported_provider(tmp_path):
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
                    name="audio-model",
                    deployments=[
                        DeploymentConfig(
                            name="anthropic/audio",
                            provider="anthropic",
                            model="claude-sonnet-4-5",
                            priority=10,
                        ),
                        DeploymentConfig(
                            name="openai/audio",
                            provider="openai",
                            model="model-a",
                            priority=20,
                        ),
                    ],
                )
            ],
        ),
    ):
        key = (await create_key(env.client, "scribe"))["key"]
        resp = await env.client.post(
            "/v1/audio/transcriptions",
            data={"model": "audio-model", "response_format": "verbose_json"},
            files=_files(),
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["x-gateway-provider"] == "openai"
        break


async def test_transcription_retries_then_falls_back(tmp_path):
    """Request-level failure on the primary trips retries, then failover —
    same contract as chat (one unique file per request)."""
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "scribe"))["key"]
        env.mock.fail_counts["model-a"] = 3  # exhausted on primary, then fallback
        resp = await env.client.post(
            "/v1/audio/transcriptions",
            data={"model": "test-model"},
            files=_files(b"unique-bytes-1"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.headers["x-gateway-deployment"] == "openai/fallback"
        break


async def test_transcription_logged_without_audio_bytes(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "scribe"))["key"]
        resp = await env.client.post(
            "/v1/audio/transcriptions",
            data={"model": "test-model", "response_format": "verbose_json"},
            files=_files(b"secret audio contents"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        logs = await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        row = logs.json()["data"][0]
        assert row["model"] == "test-model"
        assert row["status"] == "success"
        detail = await env.client.get(
            f"/admin/logs/{row['id']}", headers=auth(ADMIN_KEY)
        )
        full = detail.json()
        # Metadata about the file, never the audio itself.
        assert full["request_body"]["file_name"] == "audio.webm"
        assert full["request_body"]["file_size_bytes"] == len(b"secret audio contents")
        assert b"secret audio" not in (full["request_body"] or {}).values()
        assert full["response_body"]["text"] == "transcribed words"
        break


# ----------------------------------------------------------------------
# Speech (TTS)
# ----------------------------------------------------------------------


async def test_speech_e2e_and_cost(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=make_config(pricing={"model-a": {"speech_usd_per_1m_chars": 15.0}}),
    ):
        key = (await create_key(env.client, "speaker"))["key"]
        text = "Say this out loud."
        resp = await env.client.post(
            "/v1/audio/speech",
            json={
                "model": "test-model",
                "input": text,
                "voice": "alloy",
                "response_format": "mp3",
            },
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "audio/mpeg"
        assert resp.content == b"FAKEAUDIODATA"

        sent = env.mock.requests[-1]
        assert sent["url"].endswith("/audio/speech")
        assert sent["body"]["model"] == "model-a"
        assert sent["body"]["voice"] == "alloy"

        # Char-based cost: 19 chars / 1M * $15.
        expected = len(text) / 1_000_000 * 15.0
        assert float(resp.headers["x-gateway-cost-usd"]) == pytest.approx(expected)
        break


async def test_speech_unpriced_zero_cost(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "speaker"))["key"]
        resp = await env.client.post(
            "/v1/audio/speech",
            json={"model": "test-model", "input": "hi", "voice": "alloy"},
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert "x-gateway-cost-usd" not in resp.headers
        break


async def test_speech_falls_back_from_unsupported_provider(tmp_path):
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
                    name="tts-model",
                    deployments=[
                        DeploymentConfig(
                            name="anthropic/tts",
                            provider="anthropic",
                            model="claude-sonnet-4-5",
                            priority=10,
                        ),
                        DeploymentConfig(
                            name="openai/tts",
                            provider="openai",
                            model="model-a",
                            priority=20,
                        ),
                    ],
                )
            ],
        ),
    ):
        key = (await create_key(env.client, "speaker"))["key"]
        resp = await env.client.post(
            "/v1/audio/speech",
            json={"model": "tts-model", "input": "hi", "voice": "alloy"},
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["x-gateway-provider"] == "openai"
        break


async def test_speech_unknown_model_404(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "speaker"))["key"]
        resp = await env.client.post(
            "/v1/audio/speech",
            json={"model": "no-such-model", "input": "hi", "voice": "alloy"},
            headers=auth(key),
        )
        assert resp.status_code == 404
        break


async def test_speech_logged_binary_note(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "speaker"))["key"]
        resp = await env.client.post(
            "/v1/audio/speech",
            json={"model": "test-model", "input": "hi", "voice": "alloy"},
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
        assert full["request_body"]["input"] == "hi"
        # Binary audio is never persisted — a note about it is.
        assert full["response_body"]["bytes"] == len(b"FAKEAUDIODATA")
        assert full["response_body"]["media_type"] == "audio/mpeg"
        break


# ----------------------------------------------------------------------
# Cross-cutting
# ----------------------------------------------------------------------


async def test_audio_endpoint_scope(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (
            await create_key(env.client, "chat-only", allowed_endpoints=["chat"])
        )["key"]
        denied = await env.client.post(
            "/v1/audio/speech",
            json={"model": "test-model", "input": "hi", "voice": "alloy"},
            headers=auth(key),
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "endpoint_not_allowed"
        break


async def test_audio_requires_auth(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        resp = await env.client.post(
            "/v1/audio/speech",
            json={"model": "test-model", "input": "hi", "voice": "alloy"},
        )
        assert resp.status_code == 401
        resp = await env.client.post(
            "/v1/audio/transcriptions",
            data={"model": "test-model"},
            files=_files(),
        )
        assert resp.status_code == 401
        break


# ----------------------------------------------------------------------
# Streaming TTS + Gemini native TTS
# ----------------------------------------------------------------------


async def test_speech_streams_and_accounts_after_body(tmp_path):
    """The mock upstream yields the audio in two chunks; the gateway
    forwards the concatenated stream, and the cost/log/accounting land
    once the body completes (the log row says the streamed byte count)."""
    async for env in _make_env(
        tmp_path,
        config=make_config(pricing={"model-a": {"speech_usd_per_1m_chars": 15.0}}),
    ):
        key = (await create_key(env.client, "speaker"))["key"]
        resp = await env.client.post(
            "/v1/audio/speech",
            json={"model": "test-model", "input": "hi", "voice": "alloy"},
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "audio/mpeg"
        assert resp.content == b"FAKEAUDIODATA"  # FAKEAUDIO + DATA chunks
        expected = 2 / 1_000_000 * 15.0
        assert float(resp.headers["x-gateway-cost-usd"]) == pytest.approx(expected)

        logs = await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        row = logs.json()["data"][0]
        assert row["status"] == "success"
        assert row["cost_usd"] == pytest.approx(expected)
        detail = await env.client.get(
            f"/admin/logs/{row['id']}", headers=auth(ADMIN_KEY)
        )
        assert detail.json()["response_body"]["bytes"] == 13  # 9 + 4 streamed
        break


async def test_speech_upstream_error_fails_over(tmp_path):
    """A provider 500 before any audio is sent maps to the normal retry +
    fallback contract (the error surfaces as an HTTP status, not a broken
    stream)."""
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "speaker"))["key"]
        env.mock.fail_counts["model-a"] = 99
        resp = await env.client.post(
            "/v1/audio/speech",
            json={"model": "test-model", "input": "hi", "voice": "alloy"},
            headers=auth(key),
        )
        assert resp.status_code == 200  # served by the fallback deployment
        assert resp.headers["x-gateway-deployment"] == "openai/fallback"
        break


def _gemini_tts_env_config():
    from gateway.config import DeploymentConfig, ModelConfig, ProviderConfig

    return make_config(
        providers={
            "gemini": ProviderConfig(
                api_base="http://mockupstream.test", api_key="gk"
            )
        },
        models=[
            ModelConfig(
                name="tts-model",
                deployments=[
                    DeploymentConfig(
                        name="gemini/tts",
                        provider="gemini",
                        model="gemini-2.5-flash-preview-tts",
                    )
                ],
            )
        ],
    )


async def test_speech_gemini_tts_wav_default(tmp_path):
    """Gemini TTS: voice maps (alloy -> Kore), raw PCM comes back wrapped
    in a WAV header unless pcm was asked for."""
    async for env in _make_env(
        tmp_path, config=_gemini_tts_env_config()
    ):
        key = (await create_key(env.client, "speaker"))["key"]
        resp = await env.client.post(
            "/v1/audio/speech",
            json={"model": "tts-model", "input": "hello there", "voice": "alloy"},
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "audio/wav"
        body = resp.content
        assert body[:4] == b"RIFF" and body[8:12] == b"WAVE"
        assert len(body) == 44 + 200  # WAV header + the mock's PCM

        call = env.mock.requests[-1]
        gen = call["body"]["generationConfig"]
        assert gen["responseModalities"] == ["AUDIO"]
        assert (
            gen["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]
            == "Kore"
        )
        assert call["body"]["contents"][0]["parts"][0]["text"] == "hello there"
        break


async def test_speech_gemini_tts_pcm_passthrough(tmp_path):
    async for env in _make_env(
        tmp_path, config=_gemini_tts_env_config()
    ):
        key = (await create_key(env.client, "speaker"))["key"]
        resp = await env.client.post(
            "/v1/audio/speech",
            json={
                "model": "tts-model",
                "input": "hi",
                "voice": "alloy",
                "response_format": "pcm",
            },
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/pcm;rate=24000"
        assert resp.content == b"\x01\x00" * 100  # raw PCM, no WAV header
        break


async def test_speech_gemini_voice_passthrough_and_cost(tmp_path):
    """Unknown (Gemini-native) voice names pass through unmapped; speech
    cost is per-1M-chars like any other provider."""
    async for env in _make_env(
        tmp_path,
        config=_gemini_tts_env_config(),
    ):
        # price the public model name
        from gateway.config import DeploymentConfig, ModelConfig, ProviderConfig

        env.app.state.pipeline.cost_calculator.unit_pricing["tts-model"] = {
            "speech_usd_per_1m_chars": 15.0
        }
        key = (await create_key(env.client, "speaker"))["key"]
        resp = await env.client.post(
            "/v1/audio/speech",
            json={"model": "tts-model", "input": "hey", "voice": "Puck"},
            headers=auth(key),
        )
        assert resp.status_code == 200, resp.text
        assert (
            env.mock.requests[-1]["body"]["generationConfig"]["speechConfig"][
                "voiceConfig"
            ]["prebuiltVoiceConfig"]["voiceName"]
            == "Puck"
        )
        assert float(resp.headers["x-gateway-cost-usd"]) == pytest.approx(
            3 / 1_000_000 * 15.0
        )
        break
