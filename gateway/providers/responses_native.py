"""Native Responses-API southbound translation.

When a deployment sets ``native_responses`` and the request arrived via
``POST /v1/responses``, the OpenAI adapter forwards the *original*
Responses body to the provider's ``/responses`` endpoint instead of the
chat-shaped conversion — Responses-specific fields the conversion drops
survive the round trip. The internal chat representation remains the
spine either way: policy (guardrails, prompt policy, budgets, cache,
logging) runs on it, and the native response is translated *back* into
the internal chat shape here, so everything downstream of the adapter is
unchanged.

The fallback rule (see ``native_body``): if a guardrail or the prompt
policy modified the request after the bridge parsed it, the adapter
sends the chat-shaped conversion instead — policy modifications are
never silently dropped in favor of wire fidelity.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, AsyncIterator

from ..errors import ProviderError

if TYPE_CHECKING:  # pragma: no cover
    from ..config import DeploymentConfig
    from ..schemas import ChatCompletionRequest


def _dump_messages(messages: Any) -> list[dict[str, Any]]:
    """Comparable form of a message list — handles ``Message`` objects and
    plain dicts (``model_copy`` does not validate, so a custom guardrail
    may have set either)."""
    return [
        m.model_dump(exclude_none=True) if hasattr(m, "model_dump") else dict(m)
        for m in messages
    ]


def native_body(
    deployment: "DeploymentConfig",
    request: "ChatCompletionRequest",
    stream: bool,
) -> dict[str, Any] | None:
    """The Responses body to forward natively, or None to use the chat path.

    None when the deployment is not ``native_responses``, the request
    did not arrive via the Responses bridge, or the messages changed
    since parsing (guardrail/prompt-policy modification — the chat
    conversion carries those; the original body would not).
    """
    raw = getattr(request, "_responses_body", None)
    if raw is None or not getattr(deployment, "native_responses", False):
        return None
    pristine = getattr(request, "_responses_pristine_messages", None)
    if pristine is None or pristine != _dump_messages(request.messages):
        return None
    body = dict(raw)
    body["model"] = deployment.model  # provider-native model id
    body["stream"] = stream
    return body


def _usage(usage: dict[str, Any] | None) -> dict[str, int]:
    usage = usage or {}
    prompt = usage.get("input_tokens") or 0
    completion = usage.get("output_tokens") or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": usage.get("total_tokens") or (prompt + completion),
    }


def _finish_reason(data: dict[str, Any], has_tool_calls: bool) -> str:
    if data.get("status") == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason")
        if reason == "max_output_tokens":
            return "length"
    return "tool_calls" if has_tool_calls else "stop"


def completion_to_chat(data: dict[str, Any], public_model: str) -> dict[str, Any]:
    """A Responses-native completion object -> the internal chat shape."""
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in data.get("output") or []:
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") in ("output_text", "text") and part.get("text"):
                    texts.append(part["text"])
        elif item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or "call_native",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "",
                    },
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": "".join(texts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": data.get("id") or "resp-native",
        "object": "chat.completion",
        "created": data.get("created_at") or int(time.time()),
        "model": public_model,  # present the public model name to clients
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _finish_reason(data, bool(tool_calls)),
            }
        ],
        "usage": _usage(data.get("usage")),
    }


def _chunk(
    chunk_id: str | None, created: int, model: str, choice: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "id": chunk_id or "resp-native",
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [choice] if choice is not None else [],
    }


async def stream_events_to_chunks(
    events: AsyncIterator[dict[str, Any]], public_model: str
) -> AsyncIterator[dict[str, Any]]:
    """Responses-native SSE events -> internal chat.completion.chunk dicts.

    Produces the same chunk sequence the chat southbound does: an initial
    role chunk, content/tool_call deltas, a finish chunk, and a final
    usage-only chunk (``choices: []``) that the pipeline reads for token
    accounting.
    """
    chunk_id: str | None = None
    created = int(time.time())
    finish_reason = "stop"
    # output_index (Responses) -> index within delta.tool_calls (chat)
    tool_indexes: dict[int, int] = {}
    emitted_usage = False

    async for event in events:
        etype = event.get("type")
        if etype == "response.created":
            response = event.get("response") or {}
            chunk_id = response.get("id")
            created = response.get("created_at") or created
            yield _chunk(
                chunk_id, created, public_model,
                {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None},
            )
        elif etype == "response.output_text.delta":
            yield _chunk(
                chunk_id, created, public_model,
                {"index": 0, "delta": {"content": event.get("delta") or ""}, "finish_reason": None},
            )
        elif etype == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                output_index = event.get("output_index") or 0
                index = tool_indexes.setdefault(output_index, len(tool_indexes))
                yield _chunk(
                    chunk_id, created, public_model,
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": item.get("call_id") or item.get("id"),
                                    "type": "function",
                                    "function": {
                                        "name": item.get("name") or "",
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    },
                )
        elif etype == "response.function_call_arguments.delta":
            index = tool_indexes.get(event.get("output_index") or 0, 0)
            yield _chunk(
                chunk_id, created, public_model,
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": index,
                                "function": {"arguments": event.get("delta") or ""},
                            }
                        ]
                    },
                    "finish_reason": None,
                },
            )
        elif etype == "response.output_item.done":
            if (event.get("item") or {}).get("type") == "function_call":
                finish_reason = "tool_calls"
        elif etype in ("response.completed", "response.incomplete"):
            response = event.get("response") or {}
            if etype == "response.incomplete":
                finish_reason = _finish_reason(response, bool(tool_indexes))
            yield _chunk(
                chunk_id, created, public_model,
                {"index": 0, "delta": {}, "finish_reason": finish_reason},
            )
            usage = _usage(response.get("usage"))
            emitted_usage = True
            yield {**_chunk(chunk_id, created, public_model, None), "usage": usage}
        elif etype == "response.failed":
            error = (event.get("response") or {}).get("error") or {}
            raise ProviderError(
                502,
                error.get("message")
                or "The provider's Responses stream failed before completing.",
                "api_error",
                provider="openai",
                code="responses_stream_failed",
            )

    if not emitted_usage:
        # A well-formed stream always ends with a usage chunk; if the
        # provider never sent one, still close the sequence so the
        # pipeline's accounting sees a terminal chunk.
        yield _chunk(
            chunk_id, created, public_model,
            {"index": 0, "delta": {}, "finish_reason": finish_reason},
        )
