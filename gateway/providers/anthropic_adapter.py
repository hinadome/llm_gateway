"""Anthropic adapter: OpenAI <-> Anthropic Messages API translation.

Non-streaming: ``translate_request`` / ``translate_response`` are pure
functions (easy to test). Streaming: :class:`AnthropicStreamTranslator`
converts Anthropic SSE events into OpenAI chunks incrementally.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from ..config import DeploymentConfig, ProviderConfig
from ..errors import ProviderError
from ..schemas import ChatCompletionRequest
from ..utils import content_to_text
from .base import ProviderAdapter, network_error, raise_for_provider_status
from .sse import sse_data_lines

ANTHROPIC_VERSION = "2023-06-01"

_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


def _data_url_to_source(url: str) -> dict | None:
    """Convert an OpenAI image_url (data: or http:) to an Anthropic source block."""
    if url.startswith("data:"):
        try:
            header, _, data = url.partition(",")
            media_type = header[5:].split(";")[0] or "image/png"
            return {"type": "base64", "media_type": media_type, "data": data}
        except Exception:
            return None
    if url.startswith("http://") or url.startswith("https://"):
        return {"type": "url", "url": url}
    return None


def _translate_user_content(content: Any) -> Any:
    if isinstance(content, str) or content is None:
        return content if content is not None else ""
    if isinstance(content, list):
        blocks: list[dict] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    blocks.append({"type": "text", "text": str(part.get("text", ""))})
                elif part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    source = _data_url_to_source(url)
                    if source:
                        blocks.append({"type": "image", "source": source})
        return blocks or [{"type": "text", "text": ""}]
    return [{"type": "text", "text": content_to_text(content)}]


def _contains_audio_input(messages: list) -> bool:
    """True if any message carries an input_audio part."""
    for m in messages:
        content = getattr(m, "content", None)
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "input_audio":
                    return True
    return False


def translate_request(request: ChatCompletionRequest, native_model: str) -> dict:
    """OpenAI ChatCompletionRequest -> Anthropic /v1/messages body."""
    # Anthropic has no audio input. Silently dropping the audio would let
    # the request "succeed" while the model never hears it — so refuse
    # loudly instead (fallback=True lets the router move on to a
    # deployment whose provider can accept it, e.g. OpenAI or Gemini).
    if _contains_audio_input(request.messages):
        raise ProviderError(
            400,
            "The Anthropic provider does not support audio input "
            "(input_audio parts); the Messages API accepts text, images "
            "and PDFs only. Route audio-capable models to an OpenAI or "
            "Gemini deployment.",
            "invalid_request_error",
            code="audio_input_not_supported",
            param="messages",
            provider="anthropic",
            fallback=True,
        )

    # Anthropic has no audio output either. Silently dropping the
    # modality would let the request "succeed" while the client waits for
    # audio that never comes — refuse loudly (fallback=True lets the
    # router move on to a deployment whose provider can synthesize it).
    if request.modalities and "audio" in request.modalities:
        raise ProviderError(
            400,
            "The Anthropic provider does not support audio output "
            "(modalities: [\"audio\"]); the Messages API returns text "
            "only. Route audio-output models to an OpenAI or Gemini "
            "deployment.",
            "invalid_request_error",
            code="audio_output_not_supported",
            param="modalities",
            provider="anthropic",
            fallback=True,
        )

    # Anthropic has no JSON mode. Silently dropping response_format would let
    # the request "succeed" while the model is free to answer in prose, which
    # breaks structured-output consumers downstream — so refuse loudly
    # instead (fallback=True lets the router move on to a deployment whose
    # provider can honor it, e.g. OpenAI or Gemini).
    rf_type = (request.response_format or {}).get("type")
    if rf_type not in (None, "text"):
        raise ProviderError(
            400,
            f"The Anthropic provider does not support response_format "
            f"({rf_type!r}); it has no JSON mode. Route models that need "
            "structured output to an OpenAI or Gemini deployment.",
            "invalid_request_error",
            code="response_format_not_supported",
            param="response_format",
            provider="anthropic",
            fallback=True,
        )

    system_parts: list[str] = []
    messages: list[dict] = []

    for m in request.messages:
        if m.role in ("system", "developer"):
            text = content_to_text(m.content)
            if text:
                system_parts.append(text)
        elif m.role == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id or "",
                            "content": content_to_text(m.content),
                        }
                    ],
                }
            )
        elif m.role == "assistant":
            blocks: list[dict] = []
            if isinstance(m.content, str) and m.content:
                blocks.append({"type": "text", "text": m.content})
            elif isinstance(m.content, list):
                blocks.extend(_translate_user_content(m.content))
            for tc in m.tool_calls or []:
                try:
                    arguments = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {"_raw": tc.function.arguments}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.function.name,
                        "input": arguments,
                    }
                )
            messages.append(
                {"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]}
            )
        else:  # user
            messages.append({"role": "user", "content": _translate_user_content(m.content)})

    body: dict[str, Any] = {
        "model": native_model,
        "messages": messages,
        "max_tokens": request.effective_max_tokens or 4096,
    }
    if request.stream:
        body["stream"] = True
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.top_p is not None:
        body["top_p"] = request.top_p
    stop = request.stop
    if stop is not None:
        body["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    if request.tools:
        body["tools"] = [
            {
                "name": t.function.name,
                "description": t.function.description or "",
                "input_schema": t.function.parameters or {"type": "object", "properties": {}},
            }
            for t in request.tools
        ]
    if request.tool_choice is not None:
        tc = request.tool_choice
        if tc == "auto":
            body["tool_choice"] = {"type": "auto"}
        elif tc == "required":
            body["tool_choice"] = {"type": "any"}
        elif isinstance(tc, dict) and tc.get("type") == "function":
            name = (tc.get("function") or {}).get("name")
            if name:
                body["tool_choice"] = {"type": "tool", "name": name}
        # "none": no direct Anthropic equivalent; omit tool_choice.
    if request.user:
        body["metadata"] = {"user_id": request.user}
    return body


def translate_response(native: dict, public_model: str) -> dict:
    """Anthropic /v1/messages response -> OpenAI chat.completion dict."""
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in native.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
    text = "".join(text_parts)
    message: dict[str, Any] = {
        "role": "assistant",
        "content": text if text else None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage_native = native.get("usage") or {}
    # Anthropic reports cache reads/writes OUTSIDE input_tokens; the
    # gateway's internal contract is OpenAI's — prompt_tokens is the
    # FULL input (cache reads are a subset of it), cached reads surface
    # in prompt_tokens_details. Cache writes are input tokens too
    # (billed at Anthropic's 1.25x write rate upstream; the gateway
    # bills them at the input price — the pricing table's job).
    cache_read = usage_native.get("cache_read_input_tokens", 0) or 0
    cache_write = usage_native.get("cache_creation_input_tokens", 0) or 0
    prompt_tokens = (
        usage_native.get("input_tokens", 0) + cache_read + cache_write
    )
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": usage_native.get("output_tokens", 0),
        "total_tokens": prompt_tokens
        + usage_native.get("output_tokens", 0),
    }
    if cache_read:
        usage["prompt_tokens_details"] = {"cached_tokens": int(cache_read)}
    return {
        "id": f"chatcmpl-{native.get('id', uuid.uuid4().hex)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": public_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _STOP_REASON_MAP.get(native.get("stop_reason"), "stop"),
            }
        ],
        "usage": usage,
    }


class AnthropicStreamTranslator:
    """Stateful Anthropic-SSE -> OpenAI-chunk translator."""

    def __init__(self, public_model: str, chunk_id: str | None = None) -> None:
        self.public_model = public_model
        self.chunk_id = chunk_id or f"chatcmpl-{uuid.uuid4().hex}"
        self.created = int(time.time())
        self.finish_reason: str | None = None
        self.usage: dict | None = None
        self._tool_by_block: dict[int, tuple[str, str, int]] = {}  # block idx -> (id, name, tc idx)
        self._role_sent = False

    def _chunk(self, delta: dict, finish_reason: str | None = None, usage: dict | None = None) -> dict:
        return {
            "id": self.chunk_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.public_model,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason}
            ],
            **({"usage": usage} if usage is not None else {}),
        }

    def handle_event(self, event: dict) -> list[dict]:
        etype = event.get("type")
        chunks: list[dict] = []

        if etype == "message_start":
            message = event.get("message") or {}
            usage_native = message.get("usage") or {}
            # Same fold as translate_response: Anthropic keeps cache
            # reads outside input_tokens; the trailing usage chunk
            # carries the OpenAI shape with cached reads in details.
            cache_read = usage_native.get("cache_read_input_tokens", 0) or 0
            cache_write = usage_native.get("cache_creation_input_tokens", 0) or 0
            prompt_tokens = (
                usage_native.get("input_tokens", 0) + cache_read + cache_write
            )
            self.usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 0,
                "total_tokens": prompt_tokens,
            }
            if cache_read:
                self.usage["prompt_tokens_details"] = {"cached_tokens": int(cache_read)}
            chunks.append(self._chunk({"role": "assistant", "content": ""}))

        elif etype == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                block_idx = event.get("index", 0)
                tc_idx = len(self._tool_by_block)
                self._tool_by_block[block_idx] = (block.get("id", ""), block.get("name", ""), tc_idx)
                chunks.append(
                    self._chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": tc_idx,
                                    "id": block.get("id", ""),
                                    "type": "function",
                                    "function": {"name": block.get("name", ""), "arguments": ""},
                                }
                            ]
                        }
                    )
                )
            elif block.get("type") == "text" and not self._role_sent:
                self._role_sent = True

        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            block_idx = event.get("index", 0)
            if delta.get("type") == "text_delta":
                chunks.append(self._chunk({"content": delta.get("text", "")}))
            elif delta.get("type") == "input_json_delta":
                tool = self._tool_by_block.get(block_idx)
                if tool:
                    _, _, tc_idx = tool
                    chunks.append(
                        self._chunk(
                            {
                                "tool_calls": [
                                    {
                                        "index": tc_idx,
                                        "function": {
                                            "arguments": delta.get("partial_json", "")
                                        },
                                    }
                                ]
                            }
                        )
                    )

        elif etype == "message_delta":
            d = event.get("delta") or {}
            self.finish_reason = _STOP_REASON_MAP.get(d.get("stop_reason"), "stop")
            usage_native = event.get("usage") or {}
            if usage_native.get("output_tokens") is not None and self.usage is not None:
                self.usage["completion_tokens"] = usage_native["output_tokens"]
                self.usage["total_tokens"] = (
                    self.usage["prompt_tokens"] + usage_native["output_tokens"]
                )

        elif etype == "message_stop":
            chunks.append(self._chunk({}, finish_reason=self.finish_reason or "stop"))
            if self.usage is not None:
                # Mirror OpenAI's include_usage shape: a final, empty-choices chunk.
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


class AnthropicProviderAdapter(ProviderAdapter):
    name = "anthropic"

    @staticmethod
    def _url(api_base: str) -> str:
        return f"{api_base.rstrip('/')}/v1/messages"

    @staticmethod
    def _headers(api_key: str | None) -> dict[str, str]:
        return {
            "x-api-key": api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
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
            self._url(provider_cfg.api_base),
            self._headers(provider_cfg.resolve_key()),
            body,
            timeout,
            self.name,
        )
        return translate_response(data, request.model)

    async def stream(
        self,
        deployment: DeploymentConfig,
        request: ChatCompletionRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> AsyncIterator[dict]:
        body = translate_request(request, deployment.model)
        body["stream"] = True
        translator = AnthropicStreamTranslator(request.model)
        try:
            async with self.client.stream(
                "POST",
                self._url(provider_cfg.api_base),
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
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for chunk in translator.handle_event(event):
                        yield chunk
        except httpx.HTTPError as exc:
            raise network_error(self.name, exc) from exc
