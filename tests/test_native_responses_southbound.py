"""Tests for provider-native Responses southbound (``native_responses``).

Deployments with ``native_responses: true`` forward requests that arrived
via ``POST /v1/responses`` to the provider's ``/responses`` endpoint in
their original shape, translating the response back into the internal
chat representation — so Responses-specific fields survive the round
trip while guardrails, cache, budgets and logging stay on the chat spine.

The fallback rule is the safety net: when a guardrail or the prompt
policy modified the messages after the bridge parsed them, the adapter
sends the chat-shaped conversion instead — a policy modification is
never silently dropped in favor of wire fidelity. Plain chat requests
and Responses requests on flag-off deployments always use the conversion.
"""

from __future__ import annotations

import json

import pytest

from gateway.config import DeploymentConfig, GatewayConfig, ModelConfig
from gateway.errors import ProviderError
from gateway.providers.responses_native import (
    completion_to_chat,
    native_body,
    stream_events_to_chunks,
)
from gateway.schemas import ChatCompletionRequest

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config


def _native_config(**deployment_extra) -> GatewayConfig:
    """One model, one deployment (native_responses by default)."""
    return make_config(
        models=[
            ModelConfig(
                name="test-model",
                deployments=[
                    DeploymentConfig(
                        name="openai/primary",
                        provider="openai",
                        model="model-a",
                        **deployment_extra,
                    )
                ],
            )
        ],
    )


def _bridge_request(body: dict) -> ChatCompletionRequest:
    """What the Responses bridge hands the pipeline: parsed request plus
    the original body and pristine-messages snapshot as private attrs."""
    internal = ChatCompletionRequest.model_validate(
        {
            "model": body.get("model", "test-model"),
            "messages": [
                {"role": "user", "content": body.get("input", "hello")},
            ],
            "stream": bool(body.get("stream")),
        }
    )
    internal._responses_body = body
    internal._responses_pristine_messages = [
        m.model_dump(exclude_none=True) for m in internal.messages
    ]
    return internal


async def _aiter(events: list[dict]):
    for event in events:
        yield event


# ----------------------------------------------------------------------
# native_body (unit) — the decision + build
# ----------------------------------------------------------------------


def test_native_body_forwards_original_shape():
    request = _bridge_request(
        {"model": "test-model", "input": "hello", "store": False}
    )
    deployment = DeploymentConfig(
        name="d", provider="openai", model="model-a", native_responses=True
    )
    body = native_body(deployment, request, stream=False)
    assert body is not None
    assert body["input"] == "hello"  # original Responses shape, not messages
    assert body["store"] is False  # fields the chat conversion drops survive
    assert body["model"] == "model-a"  # provider-native model id
    assert body["stream"] is False


def test_native_body_off_without_flag_or_body():
    request = _bridge_request({"model": "test-model", "input": "hello"})
    plain = DeploymentConfig(name="d", provider="openai", model="model-a")

    assert native_body(plain, request, stream=False) is None  # flag off

    native = DeploymentConfig(
        name="d", provider="openai", model="model-a", native_responses=True
    )
    no_body = ChatCompletionRequest.model_validate(chat_body(content="hi"))
    assert native_body(native, no_body, stream=False) is None  # not from the bridge


def test_native_body_falls_back_when_messages_modified():
    request = _bridge_request({"model": "test-model", "input": "hello"})
    native = DeploymentConfig(
        name="d", provider="openai", model="model-a", native_responses=True
    )

    # A guardrail/prompt-policy modification (model_copy(update={"messages"}))
    modified = request.model_copy(
        update={"messages": [{"role": "user", "content": "MASKED"}]}
    )
    assert native_body(native, modified, stream=False) is None

    # Model resolution copies (model only) keep the native path.
    resolved = request.model_copy(update={"model": "test-model"})
    assert native_body(native, resolved, stream=False) is not None


# ----------------------------------------------------------------------
# completion_to_chat (unit)
# ----------------------------------------------------------------------


def test_completion_to_chat_text_and_usage():
    data = {
        "id": "resp_1",
        "created_at": 1700000000,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Hel"}, {"type": "output_text", "text": "lo!"}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 7, "total_tokens": 17},
    }
    chat = completion_to_chat(data, "test-model")
    assert chat["object"] == "chat.completion"
    assert chat["model"] == "test-model"  # public name for the client
    assert chat["choices"][0]["message"]["content"] == "Hello!"
    assert chat["choices"][0]["finish_reason"] == "stop"
    assert chat["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 7,
        "total_tokens": 17,
    }


def test_completion_to_chat_function_call():
    data = {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "get_weather",
                "arguments": '{"city": "Paris"}',
            }
        ],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }
    message = completion_to_chat(data, "test-model")["choices"][0]["message"]
    assert message["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }
    ]
    assert message["content"] == ""


def test_completion_to_chat_incomplete_is_length():
    data = {
        "id": "resp_1",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [],
        "usage": None,
    }
    chat = completion_to_chat(data, "test-model")
    assert chat["choices"][0]["finish_reason"] == "length"
    assert chat["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


# ----------------------------------------------------------------------
# stream_events_to_chunks (unit)
# ----------------------------------------------------------------------


async def test_stream_events_to_chunk_sequence():
    events = [
        {"type": "response.created", "response": {"id": "resp-1", "created_at": 1700000000}},
        {"type": "response.output_text.delta", "delta": "Hel"},
        {"type": "response.output_text.delta", "delta": "lo!"},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            },
        },
    ]
    chunks = [c async for c in stream_events_to_chunks(_aiter(events), "test-model")]

    # Same sequence shape the chat southbound produces: role chunk,
    # content deltas, finish chunk, usage-only terminal chunk.
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert [c["choices"][0]["delta"].get("content") for c in chunks[1:3]] == ["Hel", "lo!"]
    assert chunks[3]["choices"][0]["finish_reason"] == "stop"
    assert chunks[4]["choices"] == []
    assert chunks[4]["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 2,
        "total_tokens": 7,
    }
    # every chunk presents the public model name
    assert {c["model"] for c in chunks} == {"test-model"}


async def test_stream_failed_event_raises():
    events = [
        {"type": "response.created", "response": {"id": "resp-1"}},
        {"type": "response.failed", "response": {"error": {"message": "boom"}}},
    ]
    with pytest.raises(ProviderError) as exc_info:
        async for _ in stream_events_to_chunks(_aiter(events), "test-model"):
            pass
    assert exc_info.value.status_code == 502


async def test_stream_without_terminal_event_still_closes():
    """A provider stream that never sends response.completed still yields
    a finish chunk, so pipeline accounting sees a terminal chunk."""
    events = [
        {"type": "response.created", "response": {"id": "resp-1", "created_at": 1}},
        {"type": "response.output_text.delta", "delta": "hi"},
    ]
    chunks = [c async for c in stream_events_to_chunks(_aiter(events), "m")]
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


# ----------------------------------------------------------------------
# End to end: the native round trip through the whole gateway
# ----------------------------------------------------------------------


async def test_nonstream_native_round_trip(tmp_path):
    async for env in _make_env(tmp_path, config=_native_config(native_responses=True)):
        key = (await create_key(env.client, "native-user"))["key"]
        resp = await env.client.post(
            "/v1/responses",
            json={
                "model": "test-model",
                "input": "hello",
                "temperature": 0.2,
                "store": False,  # Responses-only field: must survive
            },
            headers=auth(key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["output"][0]["content"][0]["text"] == "Hello from the mock!"
        assert body["usage"]["input_tokens"] == 10
        assert body["usage"]["output_tokens"] == 7
        assert body["model"] == "test-model"  # public name to the client

        # The provider got the original Responses shape, natively addressed.
        upstream = env.mock.requests[-1]
        assert upstream["url"].endswith("/responses")
        assert upstream["body"]["input"] == "hello"
        assert upstream["body"]["store"] is False
        assert upstream["body"]["model"] == "model-a"  # native model id
        assert upstream["body"]["stream"] is False
        break


async def test_stream_native_round_trip(tmp_path):
    async for env in _make_env(tmp_path, config=_native_config(native_responses=True)):
        key = (await create_key(env.client, "native-streamer"))["key"]
        resp = await env.client.post(
            "/v1/responses",
            json={"model": "test-model", "input": "hello", "stream": True},
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        events: list[tuple[str, dict]] = []
        for block in resp.text.strip().split("\n\n"):
            lines = block.split("\n")
            name = lines[0][len("event: "):]
            events.append((name, json.loads(lines[1][len("data: "):])))
        names = [n for n, _ in events]
        assert names[0] == "response.created"
        assert names[-1] == "response.completed"
        text = "".join(
            p["delta"] for n, p in events if n == "response.output_text.delta"
        )
        assert text == "Hello!"  # mock native stream chunks "Hel" + "lo!"
        completed = events[-1][1]["response"]
        assert completed["usage"]["input_tokens"] == 5
        assert completed["usage"]["output_tokens"] == 2

        # The provider got the original body, natively streamed.
        upstream = env.mock.requests[-1]
        assert upstream["url"].endswith("/responses")
        assert upstream["body"]["stream"] is True
        assert upstream["body"]["input"] == "hello"

        # Usage accounting ran on the translated terminal chunk.
        listing = await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        row = next(
            e for e in listing.json()["data"] if e["key_name"] == "native-streamer"
        )
        assert row["prompt_tokens"] == 5
        assert row["completion_tokens"] == 2
        break


async def test_guardrail_modification_falls_back_to_chat(tmp_path):
    """PII redaction rewrites the request messages — the adapter must send
    the chat conversion (carrying the redaction) instead of the original
    Responses body, or the policy would be silently dropped."""
    async for env in _make_env(tmp_path, config=_native_config(native_responses=True)):
        key = (await create_key(env.client, "pii-user"))["key"]
        resp = await env.client.post(
            "/v1/responses",
            json={
                "model": "test-model",
                "input": "Reach me at alice@example.com please",
            },
            headers=auth(key),
        )
        assert resp.status_code == 200
        upstream = env.mock.requests[-1]
        assert upstream["url"].endswith("/chat/completions")  # fell back
        assert "alice@example.com" not in json.dumps(upstream["body"])
        assert upstream["body"]["messages"][0]["role"] == "user"
        # the client still gets a Responses-shaped answer
        assert resp.json()["output"][0]["type"] == "message"
        break


async def test_prompt_policy_injection_falls_back_to_chat(tmp_path):
    """A managed system prompt changes the messages after the bridge parse
    — same fallback rule, and the injected prompt must reach the provider."""
    async for env in _make_env(tmp_path, config=_native_config(native_responses=True)):
        key = (await create_key(env.client, "prompted", system_prompt="You are terse."))[
            "key"
        ]
        resp = await env.client.post(
            "/v1/responses",
            json={"model": "test-model", "input": "hello"},
            headers=auth(key),
        )
        assert resp.status_code == 200
        upstream = env.mock.requests[-1]
        assert upstream["url"].endswith("/chat/completions")
        assert upstream["body"]["messages"][0] == {
            "role": "system",
            "content": "You are terse.",
        }
        break


async def test_plain_chat_request_uses_chat_path(tmp_path):
    """A chat request (no bridge body attached) to a native deployment is
    plain chat — the flag only applies to Responses-shaped traffic."""
    async for env in _make_env(tmp_path, config=_native_config(native_responses=True)):
        key = (await create_key(env.client, "chat-user"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="hello"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.json()["object"] == "chat.completion"
        upstream = env.mock.requests[-1]
        assert upstream["url"].endswith("/chat/completions")
        assert upstream["body"]["messages"][0]["content"] == "hello"
        break


async def test_flag_off_uses_conversion(tmp_path):
    """Without native_responses the bridge request is converted to chat,
    and Responses-only fields are dropped (the pre-existing behaviour)."""
    async for env in _make_env(tmp_path, config=_native_config()):
        key = (await create_key(env.client, "converted"))["key"]
        resp = await env.client.post(
            "/v1/responses",
            json={
                "model": "test-model",
                "input": "hello",
                "store": False,
                "temperature": 0.2,
            },
            headers=auth(key),
        )
        assert resp.status_code == 200
        upstream = env.mock.requests[-1]
        assert upstream["url"].endswith("/chat/completions")
        assert upstream["body"]["messages"][0] == {"role": "user", "content": "hello"}
        assert "store" not in upstream["body"]  # conversion dropped it
        assert upstream["body"]["temperature"] == 0.2
        break


async def test_tool_round_trip_native(tmp_path):
    """Tools in Responses format survive verbatim; the function_call
    output item comes back to the client as a Responses function_call."""
    async for env in _make_env(tmp_path, config=_native_config(native_responses=True)):
        key = (await create_key(env.client, "tool-user"))["key"]
        tools = [
            {
                "type": "function",
                "name": "get_weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            }
        ]
        resp = await env.client.post(
            "/v1/responses",
            json={"model": "test-model", "input": "weather in Paris?", "tools": tools},
            headers=auth(key),
        )
        assert resp.status_code == 200
        upstream = env.mock.requests[-1]
        assert upstream["url"].endswith("/responses")
        assert upstream["body"]["tools"] == tools  # Responses tool format kept

        (item,) = resp.json()["output"]
        assert item["type"] == "function_call"
        assert item["name"] == "get_weather"
        assert json.loads(item["arguments"]) == {"city": "Paris"}
        assert item["call_id"] == "call_mock1"
        break


async def test_tool_stream_round_trip_native(tmp_path):
    async for env in _make_env(tmp_path, config=_native_config(native_responses=True)):
        key = (await create_key(env.client, "tool-streamer"))["key"]
        resp = await env.client.post(
            "/v1/responses",
            json={
                "model": "test-model",
                "input": "weather in Paris?",
                "stream": True,
                "tools": [{"type": "function", "name": "get_weather", "parameters": {}}],
            },
            headers=auth(key),
        )
        assert resp.status_code == 200
        events: list[tuple[str, dict]] = []
        for block in resp.text.strip().split("\n\n"):
            lines = block.split("\n")
            events.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
        args = "".join(
            p["delta"]
            for n, p in events
            if n == "response.function_call_arguments.delta"
        )
        assert json.loads(args) == {"city": "Paris"}
        completed = events[-1][1]["response"]
        assert completed["usage"]["input_tokens"] == 5
        break


async def test_cache_replays_native_response(tmp_path):
    """The translated chat shape is what gets cached — an identical
    repeat request is served without a second upstream call, and the
    client still sees a Responses-shaped body."""
    async for env in _make_env(tmp_path, config=_native_config(native_responses=True)):
        key = (await create_key(env.client, "cached-native"))["key"]
        body = {"model": "test-model", "input": "hello there", "store": False}
        first = await env.client.post("/v1/responses", json=body, headers=auth(key))
        assert first.status_code == 200
        assert len(env.mock.requests) == 1

        second = await env.client.post("/v1/responses", json=body, headers=auth(key))
        assert second.status_code == 200
        assert len(env.mock.requests) == 1  # cache hit, no upstream call
        assert (
            second.json()["output"][0]["content"][0]["text"] == "Hello from the mock!"
        )
        break
