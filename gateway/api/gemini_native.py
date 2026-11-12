"""Native Google Gemini inbound endpoints (``:generateContent``).

Clients that speak Gemini's REST API can point it directly at the gateway:
``POST /v1/models/{model}:generateContent`` and
``POST /v1/models/{model}:streamGenerateContent`` (SSE) translate to the
internal OpenAI-shaped ``ChatCompletionRequest``, run through the exact
same policy pipeline (keys, guardrails, budgets, routing, caching), and
the OpenAI-shaped response is translated back to Gemini's format.

Auth follows Gemini conventions: ``x-goog-api-key: <key>`` or
``?key=<key>`` (plus the gateway's usual ``Authorization: Bearer`` /
``x-api-key``).
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.responses import Response

from ..core.context import AuthContext
from ..errors import GatewayError
from ..schemas import ChatCompletionRequest, Message, Tool, ToolFunction
from ..utils import content_to_text
from .anthropic_native import openai_sse_payloads, passthrough_headers
from .deps import require_api_key

router = APIRouter()

# OpenAI finish_reason -> Gemini finishReason
_FINISH_TO_REASON = {
    "stop": "STOP",
    "length": "MAX_TOKENS",
    "tool_calls": "STOP",
    "content_filter": "SAFETY",
}

# HTTP status -> google.rpc status code for the error envelope
_RPC_STATUS = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    503: "UNAVAILABLE",
}


def is_native_path(path: str) -> bool:
    """True for endpoints that must answer with Gemini error envelopes."""
    if not path.startswith("/v1/models/") and not path.startswith("/v1beta/models/"):
        return False
    return path.endswith(":generateContent") or path.endswith(":streamGenerateContent")


def error_response(status_code: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    """Render a GatewayError in Gemini's error envelope."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": status_code,
                "message": message,
                "status": _RPC_STATUS.get(status_code, "INTERNAL"),
            }
        },
        headers=headers,
    )


# --------------------------------------------------------------------------
# Request: Gemini -> internal (OpenAI-shaped)
# --------------------------------------------------------------------------


def _tool_call_id(name: str) -> str:
    """Gemini function calls carry no ids; synthesize a stable one.

    The same name maps to the same id in both directions (functionCall ->
    tool_calls and functionResponse -> tool result) so the OpenAI-shaped
    core can correlate them.
    """
    return f"call_{name}"


def _parse_parts(parts: list) -> tuple[list[dict], list[dict]]:
    """Content parts -> (OpenAI content parts, OpenAI tool_calls)."""
    content_parts: list[dict] = []
    tool_calls: list[dict] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("text") is not None:
            content_parts.append({"type": "text", "text": str(part["text"])})
        elif isinstance(part.get("inlineData"), dict):
            inline = part["inlineData"]
            data_url = f"data:{inline.get('mimeType') or 'image/png'};base64,{inline.get('data', '')}"
            content_parts.append({"type": "image_url", "image_url": {"url": data_url}})
        elif isinstance(part.get("functionCall"), dict):
            call = part["functionCall"]
            name = str(call.get("name", ""))
            tool_calls.append(
                {
                    "id": _tool_call_id(name),
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(call.get("args") or {})},
                }
            )
    return content_parts, tool_calls


def _parse_function_response(part: dict) -> Message:
    response = part.get("functionResponse") or {}
    name = str(response.get("name", ""))
    return Message(
        role="tool",
        name=name or None,
        tool_call_id=_tool_call_id(name),
        content=json.dumps(response.get("response") or {}),
    )


def parse_request(model: str, body: dict, stream: bool) -> ChatCompletionRequest:
    """Gemini generateContent body -> internal ChatCompletionRequest."""
    messages: list[Message] = []

    system = body.get("systemInstruction")
    if isinstance(system, dict):
        system_text = "\n\n".join(
            str(p.get("text", "")) for p in system.get("parts") or [] if isinstance(p, dict)
        )
        if system_text:
            messages.append(Message(role="system", content=system_text))
    elif system is not None and not isinstance(system, str):
        raise GatewayError(400, "Invalid systemInstruction.", "invalid_request_error")
    elif isinstance(system, str) and system:
        messages.append(Message(role="system", content=system))

    contents = body.get("contents")
    if not isinstance(contents, list) or not contents:
        raise GatewayError(400, "Invalid request: 'contents' is required.", "invalid_request_error")
    for c in contents:
        if not isinstance(c, dict) or not isinstance(c.get("parts"), list):
            raise GatewayError(400, "Invalid content: 'parts' must be a list.", "invalid_request_error")
        role = "assistant" if c.get("role") == "model" else "user"
        function_responses = [
            p for p in c["parts"]
            if isinstance(p, dict) and isinstance(p.get("functionResponse"), dict)
        ]
        plain_parts = [p for p in c["parts"] if p not in function_responses]
        for p in function_responses:  # tool messages first (OpenAI shape:
            # they must follow the assistant turn that made the calls)
            messages.append(_parse_function_response(p))
        content_parts, tool_calls = _parse_parts(plain_parts)
        content: Any = (
            content_parts[0]["text"]
            if len(content_parts) == 1 and content_parts[0].get("type") == "text"
            else content_parts
        )
        if content_parts or tool_calls:
            messages.append(
                Message(role=role, content=content or None, tool_calls=tool_calls or None)
            )

    gen_config = body.get("generationConfig")
    if gen_config is None:
        gen_config = {}
    if not isinstance(gen_config, dict):
        raise GatewayError(400, "Invalid generationConfig.", "invalid_request_error")
    if gen_config.get("candidateCount") not in (None, 1):
        raise GatewayError(
            400, "candidateCount > 1 is not supported (the gateway serves one choice).", "invalid_request_error"
        )

    tools: list[Tool] | None = None
    for t in body.get("tools") or []:
        for decl in (t or {}).get("functionDeclarations") or []:
            if not isinstance(decl, dict) or not decl.get("name"):
                raise GatewayError(400, "Invalid functionDeclaration: 'name' is required.", "invalid_request_error")
            tools = tools or []
            tools.append(
                Tool(
                    function=ToolFunction(
                        name=str(decl["name"]),
                        description=decl.get("description"),
                        parameters=decl.get("parameters"),
                    )
                )
            )

    tool_choice: Any = None
    calling = ((body.get("toolConfig") or {}).get("functionCallingConfig") or {})
    mode = calling.get("mode")
    if mode == "ANY":
        allowed = calling.get("allowedFunctionNames") or []
        tool_choice = (
            {"type": "function", "function": {"name": allowed[0]}}
            if len(allowed) == 1
            else "required"
        )
    elif mode == "NONE":
        tool_choice = "none"
    elif mode == "AUTO":
        tool_choice = "auto"

    response_format = (
        {"type": "json_object"}
        if gen_config.get("responseMimeType") == "application/json"
        else None
    )

    try:
        return ChatCompletionRequest(
            model=model,
            messages=messages,
            temperature=gen_config.get("temperature"),
            top_p=gen_config.get("topP"),
            max_tokens=gen_config.get("maxOutputTokens"),
            stop=gen_config.get("stopSequences") or None,
            n=1,
            stream=stream,
            stream_options={"include_usage": True} if stream else None,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
        )
    except ValidationError as exc:
        raise GatewayError(400, f"Invalid request: {exc.errors()[0]['msg']}", "invalid_request_error") from exc


# --------------------------------------------------------------------------
# Response: internal (OpenAI-shaped) -> Gemini
# --------------------------------------------------------------------------


def _candidate_parts(message: dict) -> list[dict]:
    parts: list[dict] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        parts.append({"text": content})
    elif isinstance(content, list):
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append({"text": str(p.get("text", ""))})
    for tc in message.get("tool_calls") or []:
        function = tc.get("function") or {}
        try:
            args = json.loads(function.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        parts.append({"functionCall": {"name": function.get("name", ""), "args": args}})
    return parts or [{"text": ""}]


def render_response(completion: dict) -> dict:
    """OpenAI chat.completion dict -> Gemini GenerateContentResponse."""
    choice = (completion.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = completion.get("usage") or {}
    prompt = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": _candidate_parts(message)},
                "finishReason": _FINISH_TO_REASON.get(choice.get("finish_reason"), "STOP"),
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": prompt,
            "candidatesTokenCount": completion_tokens,
            "totalTokenCount": prompt + completion_tokens,
        },
        "modelVersion": completion.get("model", ""),
    }


class GeminiStreamRenderer:
    """Stateful OpenAI-chunk -> Gemini-SSE-chunk translator."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.usage: dict | None = None
        self.finish_reason: str | None = None
        self._final_sent = False
        self._errored = False

    def handle_chunk(self, chunk: dict) -> list[dict]:
        if isinstance(chunk.get("error"), dict) and not chunk.get("choices"):
            self._errored = True
            error = chunk["error"]
            return [
                {
                    "error": {
                        "code": 500,
                        "message": error.get("message", "Upstream error."),
                        "status": "INTERNAL",
                    }
                }
            ]
        choices = chunk.get("choices") or []
        if not choices:  # usage-only chunk
            self.usage = chunk.get("usage") or {}
            return []
        choice = choices[0]
        delta = choice.get("delta") or {}
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]
        text = delta.get("content")
        if not text:
            return []
        return [
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": text}]},
                        "index": 0,
                    }
                ],
                "modelVersion": self.model,
            }
        ]

    def finish(self) -> list[dict]:
        """Terminal chunk(s) for the [DONE] sentinel (consumed, not sent on)."""
        if self._errored or self._final_sent:
            return []
        self._final_sent = True
        final: dict[str, Any] = {
            "candidates": [
                {
                    "content": {"role": "model", "parts": []},
                    "finishReason": _FINISH_TO_REASON.get(self.finish_reason, "STOP"),
                    "index": 0,
                }
            ],
            "modelVersion": self.model,
        }
        if self.usage:
            prompt = self.usage.get("prompt_tokens") or 0
            completion_tokens = self.usage.get("completion_tokens") or 0
            final["usageMetadata"] = {
                "promptTokenCount": prompt,
                "candidatesTokenCount": completion_tokens,
                "totalTokenCount": prompt + completion_tokens,
            }
        return [final]


# --------------------------------------------------------------------------
# Wire translation: pipeline Response -> Gemini Response
# --------------------------------------------------------------------------


async def _translated_stream(inner: AsyncIterator, model: str) -> AsyncIterator[str]:
    renderer = GeminiStreamRenderer(model)
    try:
        async for payload in openai_sse_payloads(inner):
            if payload == "[DONE]":
                for chunk in renderer.finish():
                    yield f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
                continue
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for out in renderer.handle_chunk(chunk):
                yield f"data: {json.dumps(out, separators=(',', ':'))}\n\n"
    finally:
        aclose = getattr(inner, "aclose", None)
        if aclose is not None:
            await aclose()


def translate_response(response: Response, model: str) -> Response:
    """Re-frame the pipeline's OpenAI-format response as Gemini format."""
    if isinstance(response, JSONResponse):
        completion = json.loads(response.body)
        return JSONResponse(
            content=render_response(completion),
            headers=passthrough_headers(response),
            status_code=response.status_code,
        )
    if isinstance(response, StreamingResponse):
        media_type = response.media_type or "text/event-stream"
        return StreamingResponse(
            _translated_stream(response.body_iterator, model),
            media_type=media_type,
            headers=passthrough_headers(response),
            background=response.background,
        )
    return response


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


async def _handle(raw_request: Request, auth: AuthContext, model: str, stream: bool) -> Response:
    try:
        body = await raw_request.json()
    except ValueError:
        raise GatewayError(400, "Invalid request: body must be valid JSON.", "invalid_request_error")
    if not isinstance(body, dict):
        raise GatewayError(400, "Invalid request: expected a JSON object.", "invalid_request_error")

    internal = parse_request(model, body, stream)
    pipeline = raw_request.app.state.pipeline
    request_id = getattr(raw_request.state, "request_id", None)
    response = await pipeline.handle_chat(
        internal, auth, request_id=request_id, route="gemini"
    )
    return translate_response(response, internal.model)


@router.post("/v1/models/{model}:generateContent")
@router.post("/v1beta/models/{model}:generateContent")
async def generate_content(
    model: str,
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
) -> Response:
    return await _handle(raw_request, auth, model, stream=False)


@router.post("/v1/models/{model}:streamGenerateContent")
@router.post("/v1beta/models/{model}:streamGenerateContent")
async def stream_generate_content(
    model: str,
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
) -> Response:
    # The gateway always answers with SSE (what the google SDKs request via
    # alt=sse); the JSON-array streaming variant is not offered.
    return await _handle(raw_request, auth, model, stream=True)
