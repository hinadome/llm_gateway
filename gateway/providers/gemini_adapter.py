"""Google Gemini adapter: OpenAI <-> Gemini generateContent translation."""

from __future__ import annotations

import base64
import io
import json
import time
import uuid
import wave
from typing import Any, AsyncIterator

import httpx

from ..config import DeploymentConfig, ProviderConfig
from ..errors import ProviderError
from ..schemas import (
    ChatCompletionRequest,
    EmbeddingRequest,
    ImageEditRequest,
    ImageGenerationRequest,
    Message,
    SpeechRequest,
)
from ..utils import (
    audio_mime_for_format as _audio_mime,
    content_to_text,
    estimate_tokens,
)
from .base import ProviderAdapter, network_error, raise_for_provider_status
from .sse import sse_data_lines

# batchEmbedContents accepts at most 100 texts per call.
_GEMINI_EMBED_BATCH = 100


def _pcm_rate(mime_type: str | None) -> int | None:
    """Sample rate from a Gemini audio mimeType (``audio/L16;rate=24000``)."""
    if not mime_type:
        return None
    for param in mime_type.split(";")[1:]:
        param = param.strip()
        if param.startswith("rate="):
            try:
                return int(param[5:])
            except ValueError:
                return None
    return None


def _pcm_to_wav(pcm: bytes, rate: int, channels: int = 1, width: int = 2) -> bytes:
    """Wrap raw PCM in a canonical WAV header (stdlib ``wave``)."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()

_FINISH_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "BLOCKLIST": "content_filter",
    "SPII": "content_filter",
    "OTHER": "stop",
}


def _find_tool_name(messages: list[Message], tool_call_id: str | None) -> str | None:
    """Locate the function name for a tool result by scanning earlier tool_calls."""
    if not tool_call_id:
        return None
    for m in messages:
        for tc in m.tool_calls or []:
            if tc.id == tool_call_id:
                return tc.function.name
    return None


def _is_image_model(native_model: str) -> bool:
    """Heuristic: the chat-capable image models (gemini-2.5-flash-image,
    gemini-2.0-flash-preview-image-generation) carry 'image' in their name
    and need responseModalities TEXT+IMAGE to emit images from chat."""
    return "image" in native_model.lower()


# OpenAI voice names -> Gemini prebuilt voices. Approximate, deterministic
# pairing (there is no official mapping); unknown names pass through, so
# Gemini-native voice names (Kore, Puck, ...) work directly.
_VOICE_MAP = {
    "alloy": "Kore",
    "ash": "Puck",
    "ballad": "Charon",
    "coral": "Aoede",
    "echo": "Fenrir",
    "fable": "Leda",
    "nova": "Zephyr",
    "onyx": "Orus",
    "sage": "Algieba",
    "shimmer": "Enceladus",
    "verse": "Iapetus",
}


def _translate_user_content(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"text": content}]
    if isinstance(content, list):
        parts: list[dict] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append({"text": str(part.get("text", ""))})
                elif part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    if url.startswith("data:"):
                        header, _, data = url.partition(",")
                        mime = header[5:].split(";")[0] or "image/png"
                        parts.append({"inlineData": {"mimeType": mime, "data": data}})
                elif part.get("type") == "input_audio":
                    audio = part.get("input_audio") or {}
                    data = audio.get("data")
                    if data:
                        parts.append(
                            {
                                "inlineData": {
                                    "mimeType": _audio_mime(
                                        str(audio.get("format") or "wav")
                                    ),
                                    "data": data,
                                }
                            }
                        )
        return parts or [{"text": ""}]
    return [{"text": content_to_text(content)}]


def translate_request(request: ChatCompletionRequest, native_model: str) -> dict:
    """OpenAI ChatCompletionRequest -> Gemini generateContent body."""
    system_parts: list[str] = []
    contents: list[dict] = []

    for m in request.messages:
        if m.role in ("system", "developer"):
            text = content_to_text(m.content)
            if text:
                system_parts.append(text)
        elif m.role == "user":
            contents.append({"role": "user", "parts": _translate_user_content(m.content)})
        elif m.role == "assistant":
            parts: list[dict] = []
            text = content_to_text(m.content)
            if text:
                parts.append({"text": text})
            for tc in m.tool_calls or []:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {"_raw": tc.function.arguments}
                parts.append({"functionCall": {"name": tc.function.name, "args": args}})
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
        elif m.role == "tool":
            name = m.name or _find_tool_name(request.messages, m.tool_call_id) or "tool"
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": name,
                                "response": {"result": content_to_text(m.content)},
                            }
                        }
                    ],
                }
            )

    body: dict[str, Any] = {"contents": contents}
    if system_parts:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}

    gen_config: dict[str, Any] = {}
    if request.temperature is not None:
        gen_config["temperature"] = request.temperature
    if request.top_p is not None:
        gen_config["topP"] = request.top_p
    if request.effective_max_tokens is not None:
        gen_config["maxOutputTokens"] = request.effective_max_tokens
    if request.stop is not None:
        gen_config["stopSequences"] = [request.stop] if isinstance(request.stop, str) else list(request.stop)
    if request.n is not None:
        gen_config["candidateCount"] = request.n
    rf = request.response_format or {}
    rf_type = rf.get("type")
    if rf_type == "json_object":
        gen_config["responseMimeType"] = "application/json"
    elif rf_type == "json_schema":
        schema = (rf.get("json_schema") or {}).get("schema")
        if not isinstance(schema, dict):
            raise ProviderError(
                400,
                "response_format.type='json_schema' requires a "
                "'json_schema.schema' object.",
                "invalid_request_error",
                code="response_format_not_supported",
                param="response_format",
                provider="gemini",
                fallback=True,
            )
        gen_config["responseMimeType"] = "application/json"
        gen_config["responseSchema"] = schema
    elif rf_type not in (None, "text"):
        # Same principle as the Anthropic adapter: refuse rather than
        # silently dropping a constraint the model was trusted to keep.
        # fallback=True lets the router try the next deployment.
        raise ProviderError(
            400,
            f"The Gemini provider does not support response_format "
            f"({rf_type!r}).",
            "invalid_request_error",
            code="response_format_not_supported",
            param="response_format",
            provider="gemini",
            fallback=True,
        )
    if gen_config:
        body["generationConfig"] = gen_config

    # Chat with an image-generating model (image-to-image): those models
    # need TEXT+IMAGE response modalities to emit inline images, which the
    # gateway translates back to image_url content parts. An explicit
    # audio-output request (modalities: ["audio"] — gpt-4o-audio style)
    # takes precedence over the heuristic.
    if request.modalities and "audio" in request.modalities:
        audio_out = body.setdefault("generationConfig", {})
        audio_out["responseModalities"] = ["TEXT", "AUDIO"]
        voice = (request.audio or {}).get("voice")
        if voice:
            audio_out["speechConfig"] = {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": _VOICE_MAP.get(voice, voice)}
                }
            }
    elif _is_image_model(native_model):
        body.setdefault("generationConfig", {})["responseModalities"] = ["TEXT", "IMAGE"]

    if request.tools:
        body["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": t.function.name,
                        "description": t.function.description or "",
                        "parameters": t.function.parameters or {"type": "object", "properties": {}},
                    }
                    for t in request.tools
                ]
            }
        ]
        tc = request.tool_choice
        if tc == "required":
            body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
        elif isinstance(tc, dict) and tc.get("type") == "function":
            name = (tc.get("function") or {}).get("name")
            if name:
                body["toolConfig"] = {
                    "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [name]}
                }
        elif tc == "none":
            body["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
    return body


def translate_response(native: dict, public_model: str, audio_format: str = "wav") -> dict:
    """Gemini generateContent response -> OpenAI chat.completion dict.

    ``audio_format`` selects how an audio-output (native-audio-dialog)
    response's raw PCM is served: WAV-wrapped (OpenAI's default) or raw
    (``pcm16``).
    """
    candidates = native.get("candidates") or [{}]
    candidate = candidates[0] or {}
    parts = (candidate.get("content") or {}).get("parts") or []

    text_parts: list[str] = []
    image_parts: list[dict] = []
    audio_data: tuple[str, str] | None = None  # (mimeType, base64)
    tool_calls: list[dict] = []
    for part in parts:
        if "text" in part:
            text_parts.append(part["text"])
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict) and inline.get("data"):
            # Image-generating chat models: the image rides as inlineData
            # (base64) and becomes an image_url content part — the same
            # internal shape images use everywhere else in the gateway.
            # (Audio inlineData has no chat representation and is skipped.)
            mime = inline.get("mimeType") or "image/png"
            if mime.startswith("image/"):
                image_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{inline['data']}"},
                    }
                )
            elif mime.startswith("audio/") and audio_data is None:
                audio_data = (mime, str(inline["data"]))
        if "functionCall" in part:
            fc = part["functionCall"]
            tool_calls.append(
                {
                    "id": f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(fc.get("args") or {}),
                    },
                }
            )

    text = "".join(text_parts)
    if audio_data is not None:
        # Audio-output chat (native audio dialog): the text parts ARE the
        # spoken words, so they live in message.audio.transcript and
        # content stays null — OpenAI's audio-response shape.
        content_out: Any = None
    elif image_parts:
        # Mixed text + image output (or images only) -> content-parts list.
        content_out = ([{"type": "text", "text": text}] if text else []) + image_parts
    else:
        content_out = text if text else None
    message: dict[str, Any] = {"role": "assistant", "content": content_out}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if audio_data is not None:
        # Audio-output chat (native audio dialog): the words live in the
        # text parts and the sound in one inlineData audio part — repackage
        # as OpenAI's message.audio. Raw PCM is WAV-wrapped for wav (the
        # default) and served raw for pcm16.
        mime, data = audio_data
        if audio_format not in ("pcm16", "pcm"):
            try:
                pcm = base64.b64decode(data)
                data = base64.b64encode(
                    _pcm_to_wav(pcm, _pcm_rate(mime) or 24000)
                ).decode()
            except Exception:
                pass  # not decodable PCM — serve the bytes as they came
        message["audio"] = {
            "id": f"audio_{uuid.uuid4().hex[:24]}",
            "data": data,
            "expires_at": None,
            "transcript": text,
        }

    usage_meta = native.get("usageMetadata") or {}
    prompt_tokens = usage_meta.get("promptTokenCount", 0)
    completion_tokens = usage_meta.get("candidatesTokenCount", 0)
    # Gemini's promptTokenCount already INCLUDES cachedContentTokenCount
    # (context-cache hits) — surface the split in the OpenAI shape.
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    cached = usage_meta.get("cachedContentTokenCount", 0) or 0
    if cached:
        usage["prompt_tokens_details"] = {"cached_tokens": int(cached)}

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": public_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _FINISH_MAP.get(candidate.get("finishReason"), "stop"),
            }
        ],
        "usage": usage,
    }


class GeminiStreamTranslator:
    """Stateful Gemini SSE chunk -> OpenAI chunk translator."""

    def __init__(self, public_model: str) -> None:
        self.public_model = public_model
        self.chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
        self.created = int(time.time())
        self.usage: dict | None = None
        self._role_sent = False

    def _chunk(self, delta: dict, finish_reason: str | None = None) -> dict:
        return {
            "id": self.chunk_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.public_model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    def handle_chunk(self, native: dict) -> list[dict]:
        chunks: list[dict] = []
        candidates = native.get("candidates") or [{}]
        candidate = candidates[0] or {}
        parts = (candidate.get("content") or {}).get("parts") or []

        for part in parts:
            if "text" in part:
                delta: dict = {}
                if not self._role_sent:
                    delta = {"role": "assistant", "content": part["text"]}
                    self._role_sent = True
                else:
                    delta = {"content": part["text"]}
                chunks.append(self._chunk(delta))
            if "functionCall" in part:
                fc = part["functionCall"]
                chunks.append(
                    self._chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call_{uuid.uuid4().hex[:24]}",
                                    "type": "function",
                                    "function": {
                                        "name": fc.get("name", ""),
                                        "arguments": json.dumps(fc.get("args") or {}),
                                    },
                                }
                            ]
                        }
                    )
                )

        finish = candidate.get("finishReason")
        if finish:
            chunks.append(self._chunk({}, finish_reason=_FINISH_MAP.get(finish, "stop")))

        usage_meta = native.get("usageMetadata")
        if usage_meta:
            prompt_tokens = usage_meta.get("promptTokenCount", 0)
            completion_tokens = usage_meta.get("candidatesTokenCount", 0)
            self.usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
            # Same split as translate_response: promptTokenCount already
            # includes cachedContentTokenCount.
            cached = usage_meta.get("cachedContentTokenCount", 0) or 0
            if cached:
                self.usage["prompt_tokens_details"] = {"cached_tokens": int(cached)}
            # Emit OpenAI-style trailing usage chunk (empty choices).
            chunks.append(
                {
                    "id": self.chunk_id,
                    "object": "chat.completion.chunk",
                    "created": self.created,
                    "model": self.public_model,
                    "choices": [],
                    "usage": self.usage,
                }
            )
        return chunks


class GeminiProviderAdapter(ProviderAdapter):
    name = "gemini"

    @staticmethod
    def _url(api_base: str, model: str, stream: bool) -> str:
        base = api_base.rstrip("/")
        if stream:
            return f"{base}/v1beta/models/{model}:streamGenerateContent?alt=sse"
        return f"{base}/v1beta/models/{model}:generateContent"

    @staticmethod
    def _headers(api_key: str | None) -> dict[str, str]:
        return {
            "x-goog-api-key": api_key or "",
            "Content-Type": "application/json",
        }

    async def complete(
        self,
        deployment: DeploymentConfig,
        request: ChatCompletionRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        body = translate_request(request, deployment.model)
        data = await self._read_json(
            "POST",
            self._url(provider_cfg.api_base, deployment.model, stream=False),
            self._headers(provider_cfg.resolve_key()),
            body,
            timeout,
            self.name,
        )
        audio_format = str((request.audio or {}).get("format") or "wav")
        return translate_response(data, request.model, audio_format)

    async def generate_image(
        self,
        deployment: DeploymentConfig,
        request: ImageGenerationRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        """OpenAI Images -> Gemini generateContent with responseModalities.

        Gemini image models (gemini-2.0-flash-preview-image-generation,
        gemini-2.5-flash-image) take the prompt as text and return the
        image as an inlineData part (base64). One image per call, so
        ``n > 1`` issues ``n`` sequential calls. Image-size/quality
        parameters have no Gemini equivalent and are ignored.
        """
        images = await self._image_edits_roundtrips(
            deployment,
            provider_cfg,
            timeout,
            prompt=request.prompt,
            inputs=[],
            n=request.n,
        )
        return {"created": int(time.time()), "data": images}

    async def edit_image(
        self,
        deployment: DeploymentConfig,
        request: ImageEditRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        """OpenAI image edits -> Gemini generateContent with the uploaded
        images as inline input parts (the gemini-2.5-flash-image editing
        flow: reference images + instruction). ``mask`` has no Gemini
        equivalent and is ignored (documented); ``n`` edits = ``n`` calls."""
        images = await self._image_edits_roundtrips(
            deployment,
            provider_cfg,
            timeout,
            prompt=request.prompt,
            inputs=request.images,
            n=request.n,
        )
        return {"created": int(time.time()), "data": images}

    async def _image_edits_roundtrips(
        self,
        deployment: DeploymentConfig,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
        prompt: str,
        inputs: list[tuple[bytes, str, str | None]],
        n: int,
    ) -> list[dict]:
        """Shared generateContent-with-image-output loop: text prompt +
        optional inline input images, one image per call."""
        url = self._url(provider_cfg.api_base, deployment.model, stream=False)
        headers = self._headers(provider_cfg.resolve_key())
        input_parts: list[dict] = []
        for content, _filename, content_type in inputs:
            input_parts.append(
                {
                    "inlineData": {
                        "mimeType": content_type or "image/png",
                        "data": base64.b64encode(content).decode(),
                    }
                }
            )
        input_parts.append({"text": prompt})

        images: list[dict] = []
        for _ in range(max(1, n)):
            body = {
                "contents": [{"parts": input_parts}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
            }
            data = await self._read_json("POST", url, headers, body, timeout, self.name)
            for candidate in data.get("candidates") or []:
                for part in (candidate.get("content") or {}).get("parts") or []:
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        images.append({"b64_json": inline["data"]})
        if not images:
            raise ProviderError(
                502,
                f"Gemini returned no image data (deployment '{deployment.name}').",
                "api_error",
                retryable=True,
                fallback=True,
                provider=self.name,
            )
        return images

    async def speak(
        self,
        deployment: DeploymentConfig,
        request: SpeechRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> tuple[AsyncIterator[bytes], str]:
        """OpenAI Speech -> Gemini TTS (generateContent, AUDIO modality).

        Gemini TTS models (gemini-2.5-flash-preview-tts, -pro-preview-tts)
        return raw PCM (24 kHz 16-bit mono) as an inlineData part. The
        gateway serves it as-is for ``response_format: pcm`` and wrapped in
        a WAV header otherwise — mp3 & friends are not transcoded (the
        content type says what you got). ``speed`` has no Gemini
        equivalent and is ignored.
        """
        url = self._url(provider_cfg.api_base, deployment.model, stream=False)
        headers = self._headers(provider_cfg.resolve_key())
        voice = _VOICE_MAP.get(request.voice, request.voice)
        body = {
            "contents": [{"parts": [{"text": request.input}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
                },
            },
        }
        data = await self._read_json("POST", url, headers, body, timeout, self.name)
        pcm = bytearray()
        rate = 24000
        for candidate in data.get("candidates") or []:
            for part in (candidate.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    try:
                        pcm.extend(base64.b64decode(inline["data"]))
                    except Exception as exc:  # invalid base64 from provider
                        raise ProviderError(
                            502,
                            f"Invalid audio data from provider '{self.name}'",
                            "api_error",
                            retryable=True,
                            fallback=True,
                            provider=self.name,
                        ) from exc
                    rate = _pcm_rate(inline.get("mimeType")) or rate
        if not pcm:
            raise ProviderError(
                502,
                f"Gemini returned no audio data (deployment '{deployment.name}').",
                "api_error",
                retryable=True,
                fallback=True,
                provider=self.name,
            )
        if request.response_format == "pcm":
            media_type = f"audio/pcm;rate={rate}"
            payload = bytes(pcm)
        else:
            media_type = "audio/wav"
            payload = _pcm_to_wav(bytes(pcm), rate)

        async def _audio() -> AsyncIterator[bytes]:
            yield payload

        return _audio(), media_type

    async def embed(
        self,
        deployment: DeploymentConfig,
        request: EmbeddingRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        """OpenAI EmbeddingRequest -> Gemini batchEmbedContents -> OpenAI format.

        Gemini's embeddings API has no usage field, so token counts are
        estimated (~chars/4, the same heuristic as pre-flight TPM checks).
        batchEmbedContents accepts at most 100 texts per call, so larger
        input lists are chunked sequentially.
        """
        inputs = [request.input] if isinstance(request.input, str) else request.input
        embeddings: list[list[float]] = []
        url = (
            f"{provider_cfg.api_base.rstrip('/')}/v1beta/models/"
            f"{deployment.model}:batchEmbedContents"
        )
        for start in range(0, len(inputs), _GEMINI_EMBED_BATCH):
            chunk = inputs[start : start + _GEMINI_EMBED_BATCH]
            body = {
                "requests": [
                    {
                        "model": f"models/{deployment.model}",
                        "content": {"parts": [{"text": text}]},
                        **({"outputDimensionality": request.dimensions}
                           if request.dimensions is not None else {}),
                    }
                    for text in chunk
                ]
            }
            data = await self._read_json(
                "POST", url, self._headers(provider_cfg.resolve_key()), body, timeout, self.name
            )
            for item in data.get("embeddings", []):
                embeddings.append(item.get("values") or [])
        prompt_tokens = sum(estimate_tokens(text) for text in inputs)
        return {
            "object": "list",
            "data": [
                {"object": "embedding", "embedding": values, "index": i}
                for i, values in enumerate(embeddings)
            ],
            "model": request.model,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "total_tokens": prompt_tokens,
            },
        }

    async def stream(
        self,
        deployment: DeploymentConfig,
        request: ChatCompletionRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> AsyncIterator[dict]:
        # Audio-output requests: Gemini's native-audio dialog streams
        # raw PCM inlineData, which has no OpenAI chunk-delta
        # representation without re-framing WAV headers mid-stream —
        # refuse rather than silently dropping the audio from the stream
        # (retry with stream: false, where message.audio carries it).
        if request.modalities and "audio" in request.modalities:
            raise ProviderError(
                400,
                "Streaming is not supported for audio-output chat on Gemini "
                f"deployments (deployment '{deployment.name}'); retry with "
                "stream: false to receive the audio in message.audio.",
                "invalid_request_error",
                code="audio_streaming_not_supported",
                param="stream",
                provider=self.name,
                fallback=True,
            )
        # Image-generating chat models stream their text but the image
        # arrives as one opaque inlineData part that has no OpenAI
        # chunk-delta representation — refuse rather than silently
        # dropping the image from the stream (retry with stream: false,
        # where the image lands in message.content).
        if _is_image_model(deployment.model):
            raise ProviderError(
                400,
                "Streaming is not supported for image-generating chat "
                f"models (deployment '{deployment.name}'); retry with "
                "stream: false to receive the image in message.content.",
                "invalid_request_error",
                code="image_streaming_not_supported",
                param="stream",
                provider=self.name,
                fallback=True,
            )
        body = translate_request(request, deployment.model)
        translator = GeminiStreamTranslator(request.model)
        try:
            async with self.client.stream(
                "POST",
                self._url(provider_cfg.api_base, deployment.model, stream=True),
                headers=self._headers(provider_cfg.resolve_key()),
                json=body,
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise_for_provider_status(response, self.name)
                async for line in sse_data_lines(response.aiter_bytes()):
                    if not line:
                        continue
                    try:
                        native = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for chunk in translator.handle_chunk(native):
                        yield chunk
        except httpx.HTTPError as exc:
            raise network_error(self.name, exc) from exc
