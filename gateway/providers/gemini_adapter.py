"""Google Gemini adapter: OpenAI <-> Gemini generateContent translation."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from ..config import DeploymentConfig, ProviderConfig
from ..errors import ProviderError
from ..schemas import ChatCompletionRequest, EmbeddingRequest, Message
from ..utils import content_to_text, estimate_tokens
from .base import ProviderAdapter, network_error, raise_for_provider_status
from .sse import sse_data_lines

# batchEmbedContents accepts at most 100 texts per call.
_GEMINI_EMBED_BATCH = 100

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


def translate_response(native: dict, public_model: str) -> dict:
    """Gemini generateContent response -> OpenAI chat.completion dict."""
    candidates = native.get("candidates") or [{}]
    candidate = candidates[0] or {}
    parts = (candidate.get("content") or {}).get("parts") or []

    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for part in parts:
        if "text" in part:
            text_parts.append(part["text"])
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
    message: dict[str, Any] = {"role": "assistant", "content": text if text else None}
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage_meta = native.get("usageMetadata") or {}
    prompt_tokens = usage_meta.get("promptTokenCount", 0)
    completion_tokens = usage_meta.get("candidatesTokenCount", 0)

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
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
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
        return translate_response(data, request.model)

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
