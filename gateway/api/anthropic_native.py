"""Native Anthropic Messages API inbound endpoint (``POST /v1/messages``).

Clients that speak Anthropic's SDK can point it directly at the gateway:
the request is translated to the internal OpenAI-shaped
``ChatCompletionRequest``, runs through the exact same policy pipeline
(keys, guardrails, budgets, routing, caching), and the OpenAI-shaped
response is translated back to Anthropic's format. Streaming responses are
re-framed from OpenAI chunks into Anthropic SSE events.

The translation is intentionally one-way-per-side and pure (no I/O) so it
is unit-testable without the pipeline, mirroring the provider adapters.
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
from .deps import require_api_key

router = APIRouter()

# OpenAI finish_reason -> Anthropic stop_reason
_STOP_TO_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
}

# HTTP status -> Anthropic error type (client-facing envelope)
_ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    503: "overloaded_error",
    529: "overloaded_error",
}


def is_native_path(path: str) -> bool:
    """True for endpoints that must answer with Anthropic error envelopes."""
    return path == "/v1/messages" or path.startswith("/v1/messages/")


def error_response(status_code: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    """Render a GatewayError in Anthropic's error envelope."""
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "error",
            "error": {
                "type": _ERROR_TYPE_BY_STATUS.get(status_code, "api_error"),
                "message": message,
            },
        },
        headers=headers,
    )


# --------------------------------------------------------------------------
# Request: Anthropic -> internal (OpenAI-shaped)
# --------------------------------------------------------------------------


def _source_to_image_part(source: dict) -> dict | None:
    """Anthropic image source block -> OpenAI image_url part."""
    if source.get("type") == "base64":
        media = source.get("media_type") or "image/png"
        return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{source.get('data', '')}"}}
    if source.get("type") == "url":
        return {"type": "image_url", "image_url": {"url": source.get("url", "")}}
    return None


def _parse_user_blocks(blocks: list[dict]) -> list[Message]:
    """One user turn -> OpenAI messages (tool results become tool messages).

    Anthropic allows ``tool_result`` blocks to share a turn with text; the
    OpenAI shape requires them as separate ``role: "tool"`` messages directly
    after the assistant turn that requested them, so tool results are emitted
    first.
    """
    tool_messages: list[Message] = []
    parts: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            raise GatewayError(400, f"Invalid content block: {block!r}", "invalid_request_error")
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "text": str(block.get("text", ""))})
        elif btype == "image":
            image = _source_to_image_part(block.get("source") or {})
            if image:
                parts.append(image)
        elif btype == "tool_result":
            content = block.get("content")
            if isinstance(content, list):  # blocks -> flat text
                content = "".join(
                    str(b.get("text", "")) for b in content if isinstance(b, dict)
                )
            tool_messages.append(
                Message(
                    role="tool",
                    tool_call_id=str(block.get("tool_use_id", "")),
                    content=content_to_text(content),
                )
            )
        elif btype == "tool_use":
            raise GatewayError(
                400, "tool_use blocks are only valid in assistant messages.", "invalid_request_error"
            )
        # unknown block types (e.g. thinking) are dropped
    result = tool_messages
    if parts:
        content: Any = parts[0].get("text") if len(parts) == 1 and parts[0].get("type") == "text" else parts
        result = tool_messages + [Message(role="user", content=content)]
    return result


def _parse_assistant_blocks(blocks: list[dict]) -> Message:
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
    message = Message(
        role="assistant",
        content="".join(text_parts) or None,
        tool_calls=tool_calls or None,
    )
    return message


def _parse_system(system: Any) -> list[Message]:
    if system is None:
        return []
    if isinstance(system, str):
        return [Message(role="system", content=system)] if system else []
    if isinstance(system, list):
        text = "\n\n".join(
            str(b.get("text", "")) for b in system if isinstance(b, dict) and b.get("type") == "text"
        )
        return [Message(role="system", content=text)] if text else []
    raise GatewayError(400, "Invalid system: expected string or content blocks.", "invalid_request_error")


def parse_request(body: dict) -> ChatCompletionRequest:
    """Anthropic /v1/messages body -> internal ChatCompletionRequest."""
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise GatewayError(400, "Invalid request: 'model' is required.", "invalid_request_error")
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise GatewayError(400, "Invalid request: 'messages' is required.", "invalid_request_error")

    messages: list[Message] = _parse_system(body.get("system"))
    for m in raw_messages:
        if not isinstance(m, dict):
            raise GatewayError(400, f"Invalid message: {m!r}", "invalid_request_error")
        role = m.get("role")
        content = m.get("content")
        blocks = content if isinstance(content, list) else []
        if role == "user":
            if isinstance(content, str):
                messages.append(Message(role="user", content=content))
            elif isinstance(content, list):
                messages.extend(_parse_user_blocks(content))
            else:
                raise GatewayError(400, "Invalid message content.", "invalid_request_error")
        elif role == "assistant":
            if isinstance(content, str):
                messages.append(Message(role="assistant", content=content or None))
            elif isinstance(content, list):
                messages.append(_parse_assistant_blocks(content))
            else:
                raise GatewayError(400, "Invalid message content.", "invalid_request_error")
        else:
            raise GatewayError(
                400, f"Invalid message role: {role!r} (expected 'user' or 'assistant').", "invalid_request_error"
            )

    tools: list[Tool] | None = None
    for t in body.get("tools") or []:
        if not isinstance(t, dict) or not t.get("name"):
            raise GatewayError(400, "Invalid tool: 'name' is required.", "invalid_request_error")
        tools = tools or []
        tools.append(
            Tool(
                function=ToolFunction(
                    name=str(t["name"]),
                    description=t.get("description"),
                    parameters=t.get("input_schema"),
                )
            )
        )

    tool_choice: Any = None
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        mode = tc.get("type")
        if mode == "auto":
            tool_choice = "auto"
        elif mode == "any":
            tool_choice = "required"
        elif mode == "none":
            tool_choice = "none"
        elif mode == "tool" and tc.get("name"):
            tool_choice = {"type": "function", "function": {"name": tc["name"]}}
    elif tc is not None:
        raise GatewayError(400, "Invalid tool_choice: expected an object.", "invalid_request_error")

    stream = bool(body.get("stream"))
    try:
        return ChatCompletionRequest(
            model=model,
            messages=messages,
            temperature=body.get("temperature"),
            top_p=body.get("top_p"),
            max_tokens=body.get("max_tokens"),
            stop=body.get("stop_sequences") or None,
            stream=stream,
            # The gateway only forwards the final usage chunk when this is set;
            # the Anthropic stream needs it for message_delta usage.
            stream_options={"include_usage": True} if stream else None,
            tools=tools,
            tool_choice=tool_choice,
            user=(
                (body.get("metadata") or {}).get("user_id")
                if isinstance(body.get("metadata"), dict)
                else None
            ),
        )
    except ValidationError as exc:
        raise GatewayError(400, f"Invalid request: {exc.errors()[0]['msg']}", "invalid_request_error") from exc


# --------------------------------------------------------------------------
# Response: internal (OpenAI-shaped) -> Anthropic
# --------------------------------------------------------------------------


def _anthropic_id(openai_id: Any) -> str:
    raw = str(openai_id) if openai_id else uuid.uuid4().hex
    return "msg_" + (raw.removeprefix("chatcmpl-") if raw.startswith("chatcmpl-") else raw)


def _content_blocks(message: dict) -> list[dict]:
    blocks: list[dict] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                blocks.append({"type": "text", "text": str(part.get("text", ""))})
            elif part.get("type") == "image_url":
                # Image-generating models routed behind the Anthropic-native
                # surface: Anthropic assistant messages cannot carry image
                # blocks, so render an honest note instead of dropping the
                # image silently.
                url = (part.get("image_url") or {}).get("url", "")
                mime = url[5:].split(";")[0] if url.startswith("data:") else "image/png"
                blocks.append({"type": "text", "text": f"[generated image: {mime}]"})
    for tc in message.get("tool_calls") or []:
        function = tc.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            arguments = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "name": function.get("name", ""),
                "input": arguments,
            }
        )
    return blocks or [{"type": "text", "text": ""}]


def render_response(completion: dict) -> dict:
    """OpenAI chat.completion dict -> Anthropic message dict."""
    choice = (completion.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = completion.get("usage") or {}
    return {
        "id": _anthropic_id(completion.get("id")),
        "type": "message",
        "role": "assistant",
        "model": completion.get("model", ""),
        "content": _content_blocks(message),
        "stop_reason": _STOP_TO_REASON.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        },
    }


class AnthropicStreamRenderer:
    """Stateful OpenAI-chunk -> Anthropic-SSE-event translator.

    Feeds are the OpenAI chunks the pipeline emits on its SSE stream (dicts
    parsed from ``data:`` lines); output is a list of ``(event_name,
    payload)`` pairs to frame as Anthropic SSE. Anthropic content blocks are
    strictly sequential, so at most one block (text or tool_use) is open at
    a time; text after tool calls or vice versa closes and reopens.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.message_id = _anthropic_id(None)
        self.input_tokens = 0
        self.output_tokens = 0
        self.stop_reason: str | None = None
        self._started = False
        self._errored = False
        self._next_block = 0
        self._open: tuple[int, str] | None = None  # (block index, "text" | "tool")

    # -- internals ---------------------------------------------------------

    def _ensure_started(self, events: list[tuple[str, dict]]) -> None:
        if self._started:
            return
        self._started = True
        events.append(
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.message_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": self.model,
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                },
            )
        )

    def _close_block(self, events: list[tuple[str, dict]]) -> None:
        if self._open is not None:
            events.append(("content_block_stop", {"type": "content_block_stop", "index": self._open[0]}))
            self._open = None

    def _open_block(self, events: list[tuple[str, dict]], block: dict) -> int:
        self._close_block(events)
        index = self._next_block
        self._next_block += 1
        self._open = (index, block["type"])
        events.append(("content_block_start", {"type": "content_block_start", "index": index, "content_block": block}))
        return index

    # -- public ------------------------------------------------------------

    def handle_chunk(self, chunk: dict) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        if isinstance(chunk.get("error"), dict) and not chunk.get("choices"):
            self._errored = True
            error = chunk["error"]
            events.append(
                (
                    "error",
                    {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": error.get("message", "Upstream error."),
                        },
                    },
                )
            )
            return events

        choices = chunk.get("choices") or []
        if not choices:  # usage-only chunk
            usage = chunk.get("usage") or {}
            self.input_tokens = usage.get("prompt_tokens") or 0
            self.output_tokens = usage.get("completion_tokens") or 0
            return events

        self._ensure_started(events)
        choice = choices[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            if self._open is None or self._open[1] != "text":
                self._open_block(events, {"type": "text", "text": ""})
            events.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._open[0],
                        "delta": {"type": "text_delta", "text": delta["content"]},
                    },
                )
            )
        for fragment in delta.get("tool_calls") or []:
            function = fragment.get("function") or {}
            if fragment.get("id"):  # first fragment of a new tool call
                self._open_block(
                    events,
                    {
                        "type": "tool_use",
                        "id": fragment["id"],
                        "name": function.get("name", ""),
                        "input": {},
                    },
                )
            if self._open is not None and self._open[1] == "tool_use" and function.get("arguments"):
                events.append(
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": self._open[0],
                            "delta": {"type": "input_json_delta", "partial_json": function["arguments"]},
                        },
                    )
                )
        if choice.get("finish_reason"):
            self.stop_reason = choice["finish_reason"]
        return events

    def finish(self) -> list[tuple[str, dict]]:
        """Terminal events for the [DONE] sentinel."""
        if self._errored:
            return []
        events: list[tuple[str, dict]] = []
        self._ensure_started(events)
        self._close_block(events)
        events.append(
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": _STOP_TO_REASON.get(self.stop_reason, "end_turn"),
                        "stop_sequence": None,
                    },
                    "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
                },
            )
        )
        events.append(("message_stop", {"type": "message_stop"}))
        return events


# --------------------------------------------------------------------------
# Wire translation: pipeline Response -> Anthropic Response
# --------------------------------------------------------------------------


async def openai_sse_payloads(inner: AsyncIterator) -> AsyncIterator[str]:
    """``data:`` payloads from the gateway's OpenAI-format SSE stream."""
    buffer = ""
    async for raw in inner:
        buffer += raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            for line in event.split("\n"):
                if line.startswith("data:"):
                    yield line[5:].strip()
    for line in buffer.split("\n"):  # unterminated trailing event, if any
        if line.startswith("data:"):
            yield line[5:].strip()


def passthrough_headers(response: Response) -> dict[str, str]:
    """Gateway diagnostic headers (content-length is recomputed for the new body)."""
    return {k: v for k, v in response.headers.items() if k.lower() != "content-length"}


def _frame(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def _translated_stream(inner: AsyncIterator, model: str) -> AsyncIterator[str]:
    renderer = AnthropicStreamRenderer(model)
    try:
        async for payload in openai_sse_payloads(inner):
            if payload == "[DONE]":
                for event, data in renderer.finish():
                    yield _frame(event, data)
                continue
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for event, data in renderer.handle_chunk(chunk):
                yield _frame(event, data)
    finally:
        aclose = getattr(inner, "aclose", None)
        if aclose is not None:
            await aclose()


def translate_response(response: Response, model: str) -> Response:
    """Re-frame the pipeline's OpenAI-format response as Anthropic format."""
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
# Route
# --------------------------------------------------------------------------


@router.post("/v1/messages")
async def create_message(
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
) -> Response:
    try:
        body = await raw_request.json()
    except ValueError:
        raise GatewayError(400, "Invalid request: body must be valid JSON.", "invalid_request_error")
    if not isinstance(body, dict):
        raise GatewayError(400, "Invalid request: expected a JSON object.", "invalid_request_error")

    internal = parse_request(body)
    pipeline = raw_request.app.state.pipeline
    request_id = getattr(raw_request.state, "request_id", None)
    response = await pipeline.handle_chat(
        internal, auth, request_id=request_id, route="messages"
    )
    return translate_response(response, internal.model)
