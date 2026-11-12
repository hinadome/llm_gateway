"""OpenAI Responses API inbound bridge (``POST /v1/responses``).

The OpenAI Agents SDK (and newer OpenAI SDK code) speaks the Responses API by
default, not chat completions. This module is a wire-format bridge in the
same spirit as the native Anthropic/Gemini inbound endpoints, but in the
*other* direction: it accepts the Responses request shape, normalizes it to
the internal OpenAI chat-completions shape, runs the standard policy
pipeline (keys, guardrails, budgets, routing, caching), and re-renders the
response — or the SSE event stream — in Responses format.

Supported (the chat-convertible subset — what the Agents SDK sends):
``input`` (string, message items, function_call / function_call_output
items, images), ``instructions``, function ``tools``, ``tool_choice``,
``temperature``, ``top_p``, ``max_output_tokens``, ``stream``, ``text.format``
(json_object / json_schema), and pass-through of ``parallel_tool_calls`` /
``reasoning.effort``.

Explicitly unsupported (rejected with a clear 400): ``previous_response_id``
(the gateway is stateless — send the full conversation each turn), ``background``,
and server-side tools (``web_search``, ``file_search``, ``code_interpreter``,
``computer_use``, ``mcp``). ``store`` / ``truncation`` / ``include`` are
accepted and ignored. ``reasoning`` input items are dropped (no chat
equivalent); their content is not needed to continue a conversation through
this bridge.

Errors use the gateway's default OpenAI envelope — the Responses API shares
it with chat completions.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Annotated, Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.responses import Response

from ..core.context import AuthContext
from ..errors import GatewayError
from ..schemas import ChatCompletionRequest, Message, Tool, ToolFunction
from .anthropic_native import openai_sse_payloads, passthrough_headers
from .deps import require_api_key

router = APIRouter()

# Server-side tool types the bridge cannot execute (they run on OpenAI's
# infrastructure, not on an arbitrary chat-completions upstream).
_UNSUPPORTED_TOOL_TYPES = {
    "web_search", "web_search_preview",
    "file_search",
    "code_interpreter",
    "computer_use_preview",
    "mcp",
    "image_generation",
    "local_shell",
}


def _unsupported(message: str) -> GatewayError:
    return GatewayError(400, message, "invalid_request_error")


# --------------------------------------------------------------------------
# Request: Responses -> internal (OpenAI chat shape)
# --------------------------------------------------------------------------


def _parse_message_item(item: dict) -> Message:
    role = item.get("role")
    if role not in ("user", "assistant", "system", "developer"):
        raise _unsupported(
            f"Invalid input message role: {role!r} "
            "(expected 'user', 'assistant', 'system' or 'developer')."
        )
    content = item.get("content")
    if content is None:
        if role == "user":
            raise _unsupported("Input message items require 'content'.")
        return Message(role=role, content=None)
    if isinstance(content, str):
        return Message(role=role, content=content)
    if not isinstance(content, list):
        raise _unsupported("Invalid input message content: expected a string or an array of parts.")

    parts: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "summary_text"):
            parts.append({"type": "text", "text": str(part.get("text", ""))})
        elif ptype == "input_image":
            url = (part.get("image_url") or {}).get("url")
            if not url:
                raise _unsupported(
                    "input_image parts require an image_url with a url "
                    "(file_id-based images are not supported by the gateway)."
                )
            parts.append({"type": "image_url", "image_url": {"url": url}})
        elif ptype == "input_audio":
            audio = part.get("input_audio") or {}
            if not audio.get("data"):
                raise _unsupported("input_audio parts require input_audio.data.")
            parts.append(
                {
                    "type": "input_audio",
                    "input_audio": {
                        "data": str(audio.get("data", "")),
                        "format": str(audio.get("format") or "wav"),
                    },
                }
            )
        else:
            raise _unsupported(f"Unsupported input content part type: {ptype!r}.")
    text_content: Any = (
        parts[0]["text"] if len(parts) == 1 and parts[0].get("type") == "text" else parts
    )
    return Message(role=role, content=text_content or None)


def _parse_input_items(raw_input: Any) -> list[Message]:
    """Responses ``input`` -> chat messages, in order."""
    if isinstance(raw_input, str):
        return [Message(role="user", content=raw_input)]
    if not isinstance(raw_input, list) or not raw_input:
        raise _unsupported("Invalid request: 'input' is required (string or array of items).")

    messages: list[Message] = []
    for item in raw_input:
        if not isinstance(item, dict):
            raise _unsupported(f"Invalid input item: {item!r}.")
        itype = item.get("type") or ("message" if "role" in item else None)
        if itype == "message":
            messages.append(_parse_message_item(item))
        elif itype == "function_call":
            messages.append(
                Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        {
                            "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                            "type": "function",
                            "function": {
                                "name": str(item.get("name", "")),
                                "arguments": item.get("arguments") or "",
                            },
                        }
                    ],
                )
            )
        elif itype == "function_call_output":
            output = item.get("output")
            if output is None:
                raise _unsupported("function_call_output items require 'output'.")
            if not isinstance(output, str):
                output = json.dumps(output)
            messages.append(
                Message(
                    role="tool",
                    tool_call_id=str(item.get("call_id") or ""),
                    content=output,
                )
            )
        elif itype == "reasoning":
            continue  # model-internal state; nothing to forward to a chat upstream
        elif itype == "item_reference":
            raise _unsupported(
                "item_reference inputs require stored responses "
                "(previous_response_id); the gateway bridge is stateless — "
                "send the full conversation in 'input'."
            )
        else:
            raise _unsupported(f"Unsupported input item type: {itype!r}.")
    return messages


def _parse_tools(raw_tools: Any) -> list[Tool] | None:
    if raw_tools is None:
        return None
    if not isinstance(raw_tools, list):
        raise _unsupported("Invalid request: 'tools' must be an array.")
    tools: list[Tool] = []
    for t in raw_tools:
        if not isinstance(t, dict):
            raise _unsupported(f"Invalid tool: {t!r}.")
        if t.get("type") != "function":
            ttype = t.get("type")
            if ttype in _UNSUPPORTED_TOOL_TYPES:
                raise _unsupported(
                    f"Server-side tool type '{ttype}' is not supported by the "
                    "gateway: it executes on the provider's infrastructure. "
                    "Route such agents directly at the provider, or implement "
                    "the tool client-side."
                )
            raise _unsupported(f"Unsupported tool type: {ttype!r}.")
        if not t.get("name"):
            raise _unsupported("Function tools require a 'name'.")
        tools.append(
            Tool(
                function=ToolFunction(
                    name=str(t["name"]),
                    description=t.get("description"),
                    parameters=t.get("parameters"),
                )
            )
        )
    return tools or None


def _parse_tool_choice(tc: Any) -> Any:
    if tc is None:
        return None
    if tc in ("auto", "none", "required"):
        return tc
    if isinstance(tc, dict):
        if tc.get("type") == "function" and tc.get("name"):
            return {"type": "function", "function": {"name": tc["name"]}}
    raise _unsupported(
        "Invalid tool_choice: expected 'auto', 'none', 'required' or "
        "{'type': 'function', 'name': ...}."
    )


def _parse_text_format(text: Any) -> dict | None:
    """Responses ``text.format`` -> chat ``response_format``."""
    if not isinstance(text, dict):
        return None
    fmt = text.get("format") or {}
    ftype = fmt.get("type")
    if ftype in (None, "text"):
        return None
    if ftype == "json_object":
        return {"type": "json_object"}
    if ftype == "json_schema":
        schema = fmt.get("schema")
        if not isinstance(schema, dict):
            raise _unsupported("text.format.type='json_schema' requires a 'schema' object.")
        return {
            "type": "json_schema",
            "json_schema": {
                "name": fmt.get("name") or "response",
                "schema": schema,
                **({"strict": fmt["strict"]} if fmt.get("strict") is not None else {}),
            },
        }
    raise _unsupported(f"Unsupported text.format type: {ftype!r}.")


def parse_request(body: dict) -> ChatCompletionRequest:
    """Responses API body -> internal ChatCompletionRequest."""
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise _unsupported("Invalid request: 'model' is required.")

    unsupported = [f for f in ("previous_response_id",) if body.get(f)]
    if body.get("background"):
        unsupported.append("background")
    if unsupported:
        raise _unsupported(
            f"The Responses bridge is stateless and synchronous: "
            f"{', '.join(unsupported)} is not supported. Send the full "
            "conversation in 'input' each turn."
        )

    messages: list[Message] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append(Message(role="system", content=instructions))
    messages.extend(_parse_input_items(body.get("input")))

    tools = _parse_tools(body.get("tools"))
    tool_choice = _parse_tool_choice(body.get("tool_choice"))

    stream = bool(body.get("stream"))
    extras: dict[str, Any] = {}
    if body.get("parallel_tool_calls") is not None:
        extras["parallel_tool_calls"] = body["parallel_tool_calls"]
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        extras["reasoning_effort"] = reasoning["effort"]

    try:
        return ChatCompletionRequest(
            model=model,
            messages=messages,
            temperature=body.get("temperature"),
            top_p=body.get("top_p"),
            max_tokens=body.get("max_output_tokens"),
            stream=stream,
            # The pipeline only emits the final usage chunk when requested;
            # response.completed needs it.
            stream_options={"include_usage": True} if stream else None,
            tools=tools,
            tool_choice=tool_choice,
            response_format=_parse_text_format(body.get("text")),
            user=body.get("user"),
            **extras,
        )
    except ValidationError as exc:
        raise _unsupported(f"Invalid request: {exc.errors()[0]['msg']}") from exc


# --------------------------------------------------------------------------
# Response: internal (OpenAI chat shape) -> Responses
# --------------------------------------------------------------------------


def _message_item(text: str, item_id: str, status: str = "completed") -> dict:
    return {
        "type": "message",
        "id": item_id,
        "status": status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _function_call_item(name: str, arguments: str, call_id: str, item_id: str, status: str = "completed") -> dict:
    return {
        "type": "function_call",
        "id": item_id,
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": status,
    }


def _usage_block(usage: dict | None) -> dict:
    prompt = (usage or {}).get("prompt_tokens") or 0
    completion = (usage or {}).get("completion_tokens") or 0
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": (usage or {}).get("total_tokens") or (prompt + completion),
    }


def _status_for(finish_reason: str | None) -> tuple[str, dict | None]:
    if finish_reason == "length":
        return "incomplete", {"reason": "max_output_tokens"}
    if finish_reason == "content_filter":
        return "incomplete", {"reason": "content_filter"}
    return "completed", None


def render_response(completion: dict, echo: dict | None = None) -> dict:
    """OpenAI chat.completion dict -> Responses API response object."""
    echo = echo or {}
    choice = (completion.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason")
    status, incomplete_details = _status_for(finish_reason)

    output: list[dict] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        output.append(_message_item(text, f"msg_{uuid.uuid4().hex[:24]}"))
    elif isinstance(text, list):
        parts = [p for p in text if isinstance(p, dict) and p.get("type") == "text"]
        if parts:
            output.append(
                _message_item(
                    "".join(str(p.get("text", "")) for p in parts),
                    f"msg_{uuid.uuid4().hex[:24]}",
                )
            )
    for tc in message.get("tool_calls") or []:
        function = tc.get("function") or {}
        output.append(
            _function_call_item(
                function.get("name", ""),
                function.get("arguments") or "",
                tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                f"fc_{uuid.uuid4().hex[:24]}",
            )
        )
    if not output:
        output.append(_message_item("", f"msg_{uuid.uuid4().hex[:24]}"))

    return {
        "id": f"resp_{uuid.uuid4().hex[:32]}",
        "object": "response",
        "created_at": completion.get("created") or int(time.time()),
        "status": status,
        "incomplete_details": incomplete_details,
        "error": None,
        "model": completion.get("model", ""),
        "output": output,
        "instructions": echo.get("instructions"),
        "metadata": {},
        "temperature": echo.get("temperature"),
        "top_p": echo.get("top_p"),
        "tool_choice": echo.get("tool_choice") or "auto",
        "tools": echo.get("tools") or [],
        "parallel_tool_calls": echo.get("parallel_tool_calls", True),
        "usage": _usage_block(completion.get("usage")),
        "text": {"format": {"type": "text"}},
    }


class ResponsesStreamRenderer:
    """Stateful OpenAI-chunk -> Responses-SSE-event translator.

    Feeds are the pipeline's OpenAI chunks (dicts parsed from ``data:``
    lines); output is a list of ``(event_name, payload)`` pairs to frame as
    Responses SSE. Event set: response.created / response.in_progress /
    response.output_item.added / response.content_part.added /
    response.output_text.delta / response.function_call_arguments.delta
    (+ their .done counterparts) / response.completed.
    """

    def __init__(self, model: str, echo: dict | None = None) -> None:
        self.model = model
        self.response_id = f"resp_{uuid.uuid4().hex[:32]}"
        self._echo = echo or {}
        self._seq = 0
        self._started = False
        self._errored = False
        # Arrival-ordered output items; output_index == position in the list.
        self._items: list[dict] = []
        self._message: dict | None = None  # the (single) message entry in _items
        self._tool_by_slot: dict[int, dict] = {}  # chat tool-call index -> entry
        self.finish_reason: str | None = None
        self.usage: dict | None = None

    # -- internals ---------------------------------------------------------

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    def _skeleton(self, status: str = "in_progress") -> dict:
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "incomplete_details": None,
            "error": None,
            "model": self.model,
            "output": [],
            "instructions": self._echo.get("instructions"),
            "metadata": {},
            "temperature": self._echo.get("temperature"),
            "top_p": self._echo.get("top_p"),
            "tool_choice": self._echo.get("tool_choice") or "auto",
            "tools": self._echo.get("tools") or [],
            "parallel_tool_calls": self._echo.get("parallel_tool_calls", True),
            "usage": _usage_block(None),
            "text": {"format": {"type": "text"}},
        }

    def _ensure_started(self, events: list[tuple[str, dict]]) -> None:
        if self._started:
            return
        self._started = True
        events.append(
            (
                "response.created",
                {
                    "type": "response.created",
                    "sequence_number": self._next_seq(),
                    "response": self._skeleton(),
                },
            )
        )
        events.append(
            (
                "response.in_progress",
                {
                    "type": "response.in_progress",
                    "sequence_number": self._next_seq(),
                    "response": self._skeleton(),
                },
            )
        )

    def _ensure_message(self, events: list[tuple[str, dict]]) -> None:
        if self._message is not None:
            return
        index = len(self._items)
        item_id = f"msg_{uuid.uuid4().hex[:24]}"
        self._message = {"kind": "message", "index": index, "item_id": item_id, "text": ""}
        self._items.append(self._message)
        events.append(
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "sequence_number": self._next_seq(),
                    "output_index": index,
                    "item": {
                        "type": "message",
                        "id": item_id,
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                },
            )
        )
        events.append(
            (
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "sequence_number": self._next_seq(),
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            )
        )

    # -- public ------------------------------------------------------------

    def handle_chunk(self, chunk: dict) -> list[tuple[str, dict]]:
        if isinstance(chunk.get("error"), dict) and not chunk.get("choices"):
            self._errored = True
            error = chunk["error"]
            return [
                (
                    "error",
                    {
                        "type": "error",
                        "code": error.get("code") or "server_error",
                        "message": error.get("message", "Upstream error."),
                        "param": error.get("param"),
                        "sequence_number": self._next_seq(),
                    },
                )
            ]

        choices = chunk.get("choices") or []
        if not choices:  # usage-only chunk
            if isinstance(chunk.get("usage"), dict):
                self.usage = chunk["usage"]
            return []

        events: list[tuple[str, dict]] = []
        self._ensure_started(events)
        choice = choices[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            self._ensure_message(events)
            text = delta["content"]
            self._message["text"] += text
            events.append(
                (
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "sequence_number": self._next_seq(),
                        "item_id": self._message["item_id"],
                        "output_index": self._message["index"],
                        "content_index": 0,
                        "delta": text,
                    },
                )
            )
        for fragment in delta.get("tool_calls") or []:
            events.extend(self._handle_tool_fragment(fragment))
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]
        return events

    def _handle_tool_fragment(self, fragment: dict) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        function = fragment.get("function") or {}
        slot = fragment.get("index", 0)
        entry = self._tool_by_slot.get(slot)
        if entry is None:
            if not fragment.get("id"):
                return events  # continuation without a start; nothing to attach to
            index = len(self._items)
            entry = {
                "kind": "tool",
                "index": index,
                "item_id": f"fc_{uuid.uuid4().hex[:24]}",
                "call_id": fragment["id"],
                "name": function.get("name", ""),
                "arguments": "",
            }
            self._items.append(entry)
            self._tool_by_slot[slot] = entry
            events.append(
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "sequence_number": self._next_seq(),
                        "output_index": index,
                        "item": _function_call_item(
                            entry["name"], "", entry["call_id"], entry["item_id"], status="in_progress"
                        ),
                    },
                )
            )
        arguments = function.get("arguments")
        if arguments:
            entry["arguments"] += arguments
            events.append(
                (
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "sequence_number": self._next_seq(),
                        "item_id": entry["item_id"],
                        "output_index": entry["index"],
                        "delta": arguments,
                    },
                )
            )
        return events

    def finish(self) -> list[tuple[str, dict]]:
        """Terminal events for the [DONE] sentinel."""
        if self._errored:
            return []
        events: list[tuple[str, dict]] = []
        self._ensure_started(events)
        if not self._items:
            # empty completion: still emit a (empty) message item
            self._ensure_message(events)
        if self._message is not None:
            full_text = self._message["text"]
            events.append(
                (
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "sequence_number": self._next_seq(),
                        "item_id": self._message["item_id"],
                        "output_index": self._message["index"],
                        "content_index": 0,
                        "text": full_text,
                    },
                )
            )
            events.append(
                (
                    "response.content_part.done",
                    {
                        "type": "response.content_part.done",
                        "sequence_number": self._next_seq(),
                        "item_id": self._message["item_id"],
                        "output_index": self._message["index"],
                        "content_index": 0,
                        "part": {"type": "output_text", "text": full_text, "annotations": []},
                    },
                )
            )
            events.append(
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "sequence_number": self._next_seq(),
                        "output_index": self._message["index"],
                        "item": _message_item(full_text, self._message["item_id"]),
                    },
                )
            )
        for entry in self._tools():
            events.append(
                (
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "sequence_number": self._next_seq(),
                        "item_id": entry["item_id"],
                        "output_index": entry["index"],
                        "arguments": entry["arguments"],
                    },
                )
            )
            events.append(
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "sequence_number": self._next_seq(),
                        "output_index": entry["index"],
                        "item": _function_call_item(
                            entry["name"], entry["arguments"], entry["call_id"], entry["item_id"]
                        ),
                    },
                )
            )
        events.append(
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "sequence_number": self._next_seq(),
                    "response": self._final_response(),
                },
            )
        )
        return events

    def _tools(self) -> list[dict]:
        return [e for e in self._items if e["kind"] == "tool"]

    def _final_response(self) -> dict:
        status, incomplete_details = _status_for(self.finish_reason)
        output: list[dict] = []
        for entry in self._items:
            if entry["kind"] == "message":
                output.append(_message_item(entry["text"], entry["item_id"]))
            else:
                output.append(
                    _function_call_item(
                        entry["name"], entry["arguments"], entry["call_id"], entry["item_id"]
                    )
                )
        response = self._skeleton(status)
        response["incomplete_details"] = incomplete_details
        response["output"] = output
        response["usage"] = _usage_block(self.usage)
        return response


# --------------------------------------------------------------------------
# Wire translation: pipeline Response -> Responses Response
# --------------------------------------------------------------------------


def _frame(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def _translated_stream(inner: AsyncIterator, model: str, echo: dict | None) -> AsyncIterator[str]:
    renderer = ResponsesStreamRenderer(model, echo)
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


def translate_response(response: Response, model: str, echo: dict | None = None) -> Response:
    """Re-frame the pipeline's chat-completions response as a Responses response."""
    if isinstance(response, JSONResponse):
        completion = json.loads(response.body)
        return JSONResponse(
            content=render_response(completion, echo),
            headers=passthrough_headers(response),
            status_code=response.status_code,
        )
    if isinstance(response, StreamingResponse):
        media_type = response.media_type or "text/event-stream"
        return StreamingResponse(
            _translated_stream(response.body_iterator, model, echo),
            media_type=media_type,
            headers=passthrough_headers(response),
            background=response.background,
        )
    return response


# --------------------------------------------------------------------------
# Route
# --------------------------------------------------------------------------


@router.post("/v1/responses")
async def create_response(
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
) -> Response:
    try:
        body = await raw_request.json()
    except ValueError:
        raise _unsupported("Invalid request: body must be valid JSON.")
    if not isinstance(body, dict):
        raise _unsupported("Invalid request: expected a JSON object.")

    internal = parse_request(body)
    # Native Responses southbound (deployment.native_responses): keep the
    # original body with the request, plus the messages snapshot at parse
    # time. The adapter forwards the body verbatim only while the messages
    # are still pristine — a guardrail/prompt-policy modification falls
    # back to the chat conversion, so nothing is silently lost.
    internal._responses_body = body
    internal._responses_pristine_messages = [
        m.model_dump(exclude_none=True) for m in internal.messages
    ]
    echo = {
        "instructions": body.get("instructions") if isinstance(body.get("instructions"), str) else None,
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "tool_choice": body.get("tool_choice") if isinstance(body.get("tool_choice"), str) else "auto",
        "tools": [
            t for t in (body.get("tools") or []) if isinstance(t, dict) and t.get("type") == "function"
        ],
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
    }
    pipeline = raw_request.app.state.pipeline
    request_id = getattr(raw_request.state, "request_id", None)
    response = await pipeline.handle_chat(
        internal, auth, request_id=request_id, route="responses"
    )
    return translate_response(response, internal.model, echo)
