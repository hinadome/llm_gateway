"""Unit tests for provider request/response translation (pure functions)."""

from __future__ import annotations

import json

import httpx
import pytest

from gateway.config import DeploymentConfig, ProviderConfig
from gateway.errors import ProviderError
from gateway.providers.anthropic_adapter import (
    AnthropicProviderAdapter,
    AnthropicStreamTranslator,
    translate_request,
    translate_response,
)
from gateway.providers.base import raise_for_provider_status
from gateway.providers.gemini_adapter import (
    GeminiStreamTranslator,
    translate_request as gemini_translate_request,
    translate_response as gemini_translate_response,
)
from gateway.providers.openai_adapter import OpenAIProviderAdapter
from gateway.schemas import ChatCompletionRequest


def make_request(**kwargs) -> ChatCompletionRequest:
    base = {"model": "test-model", "messages": [{"role": "user", "content": "Hello"}]}
    base.update(kwargs)
    return ChatCompletionRequest(**base)


# ----------------------------------------------------------------------
# Anthropic: request translation
# ----------------------------------------------------------------------


def test_anthropic_system_message_extraction():
    request = make_request(
        messages=[
            {"role": "system", "content": "You are helpful."},
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hi"},
        ]
    )
    body = translate_request(request, "claude-sonnet-4-5")
    assert body["system"] == "You are helpful.\n\nBe concise."
    assert body["messages"] == [{"role": "user", "content": "Hi"}]
    assert body["model"] == "claude-sonnet-4-5"


def test_anthropic_max_tokens_default_and_override():
    assert translate_request(make_request(), "m")["max_tokens"] == 4096
    request = make_request(max_tokens=512)
    assert translate_request(request, "m")["max_tokens"] == 512
    request = make_request(max_completion_tokens=99, max_tokens=512)
    assert translate_request(request, "m")["max_tokens"] == 99


def test_anthropic_sampling_params_and_stop():
    request = make_request(temperature=0.7, top_p=0.9, stop=["\n\n", "END"])
    body = translate_request(request, "m")
    assert body["temperature"] == 0.7
    assert body["top_p"] == 0.9
    assert body["stop_sequences"] == ["\n\n", "END"]


def test_anthropic_assistant_tool_calls_translation():
    request = make_request(
        messages=[
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "20C sunny"},
        ]
    )
    body = translate_request(request, "m")
    assistant = body["messages"][1]
    assert assistant["role"] == "assistant"
    tool_use = assistant["content"][0]
    assert tool_use == {
        "type": "tool_use",
        "id": "call_1",
        "name": "get_weather",
        "input": {"city": "Paris"},
    }
    tool_result = body["messages"][2]
    assert tool_result["role"] == "user"
    assert tool_result["content"][0]["type"] == "tool_result"
    assert tool_result["content"][0]["tool_use_id"] == "call_1"
    assert tool_result["content"][0]["content"] == "20C sunny"


def test_anthropic_tools_and_tool_choice():
    request = make_request(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "f",
                    "description": "does f",
                    "parameters": {"type": "object", "properties": {"a": {"type": "string"}}},
                },
            }
        ],
        tool_choice={"type": "function", "function": {"name": "f"}},
    )
    body = translate_request(request, "m")
    assert body["tools"][0]["name"] == "f"
    assert body["tools"][0]["input_schema"]["type"] == "object"
    assert body["tool_choice"] == {"type": "tool", "name": "f"}


def test_anthropic_tool_choice_required_and_auto():
    body = translate_request(make_request(tool_choice="required"), "m")
    assert body["tool_choice"] == {"type": "any"}
    body = translate_request(make_request(tool_choice="auto"), "m")
    assert body["tool_choice"] == {"type": "auto"}
    body = translate_request(make_request(tool_choice="none"), "m")
    assert "tool_choice" not in body


def test_anthropic_image_data_url_translation():
    request = make_request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,QUJD"},
                    },
                ],
            }
        ]
    )
    body = translate_request(request, "m")
    blocks = body["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "what is this?"}
    assert blocks[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }


# ----------------------------------------------------------------------
# Anthropic: response translation
# ----------------------------------------------------------------------


def test_anthropic_text_response_translation():
    native = {
        "id": "msg_1",
        "content": [{"type": "text", "text": "Hello!"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 8, "output_tokens": 3},
    }
    result = translate_response(native, "public-name")
    assert result["object"] == "chat.completion"
    assert result["model"] == "public-name"
    assert result["choices"][0]["message"]["content"] == "Hello!"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["usage"] == {
        "prompt_tokens": 8,
        "completion_tokens": 3,
        "total_tokens": 11,
    }


def test_anthropic_tool_use_response_translation():
    native = {
        "id": "msg_2",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}}],
        "stop_reason": "tool_use",
    }
    result = translate_response(native, "public")
    message = result["choices"][0]["message"]
    assert message["content"] is None
    assert message["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"city": "Paris"}
    assert result["choices"][0]["finish_reason"] == "tool_calls"


def test_anthropic_stream_translator():
    translator = AnthropicStreamTranslator("public")
    all_chunks = []
    events = [
        {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 9}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ]
    for event in events:
        all_chunks.extend(translator.handle_event(event))

    assert all_chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    contents = [
        c["choices"][0]["delta"]["content"]
        for c in all_chunks
        if c.get("choices") and c["choices"][0]["delta"].get("content")
    ]
    assert contents == ["Hel", "lo"]
    finish_chunks = [c for c in all_chunks if c["choices"] and c["choices"][0]["finish_reason"]]
    assert finish_chunks[0]["choices"][0]["finish_reason"] == "stop"
    usage_chunk = all_chunks[-1]
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"] == {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}


def test_anthropic_stream_translator_tool_use():
    translator = AnthropicStreamTranslator("public")
    chunks = []
    for event in [
        {"type": "message_start", "message": {"usage": {"input_tokens": 4}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "f"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"a":'}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "1}"}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 6}},
        {"type": "message_stop"},
    ]:
        chunks.extend(translator.handle_event(event))

    tool_starts = [c for c in chunks if c.get("choices") and c["choices"][0]["delta"].get("tool_calls")]
    first = tool_starts[0]["choices"][0]["delta"]["tool_calls"][0]
    assert first["id"] == "t1"
    assert first["function"]["name"] == "f"
    joined = "".join(
        tc["function"]["arguments"]
        for c in tool_starts[1:]
        for tc in c["choices"][0]["delta"]["tool_calls"]
    )
    assert json.loads(joined) == {"a": 1}
    finish = [c for c in chunks if c.get("choices") and c["choices"][0]["finish_reason"]]
    assert finish[0]["choices"][0]["finish_reason"] == "tool_calls"


# ----------------------------------------------------------------------
# Gemini translation
# ----------------------------------------------------------------------


def test_gemini_request_translation():
    request = make_request(
        messages=[
            {"role": "system", "content": "Be nice."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "Bye"},
        ],
        temperature=0.5,
        top_p=0.9,
        max_tokens=256,
        stop=["\n"],
        response_format={"type": "json_object"},
    )
    body = gemini_translate_request(request, "gemini-2.5-flash")
    assert body["systemInstruction"] == {"parts": [{"text": "Be nice."}]}
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    gen = body["generationConfig"]
    assert gen["temperature"] == 0.5
    assert gen["topP"] == 0.9
    assert gen["maxOutputTokens"] == 256
    assert gen["stopSequences"] == ["\n"]
    assert gen["responseMimeType"] == "application/json"


def test_gemini_tool_result_name_lookup():
    request = make_request(
        messages=[
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_9", "type": "function",
                     "function": {"name": "get_weather", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_9", "content": "sunny"},
        ]
    )
    body = gemini_translate_request(request, "m")
    fr = body["contents"][2]["parts"][0]["functionResponse"]
    assert fr["name"] == "get_weather"
    assert fr["response"] == {"result": "sunny"}


def test_gemini_response_translation():
    native = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "Hello "},
                        {"text": "world"},
                        {"functionCall": {"name": "f", "args": {"x": 1}}},
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 2},
    }
    result = gemini_translate_response(native, "public")
    message = result["choices"][0]["message"]
    assert message["content"] == "Hello world"
    assert message["tool_calls"][0]["function"]["name"] == "f"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"x": 1}
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["usage"]["total_tokens"] == 8


def test_gemini_stream_translator():
    translator = GeminiStreamTranslator("public")
    chunks = []
    for native in [
        {"candidates": [{"content": {"parts": [{"text": "Hi"}]}}]},
        {"candidates": [{"content": {"parts": [{"text": " there"}]}}]},
        {
            "candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2},
        },
    ]:
        chunks.extend(translator.handle_chunk(native))

    first = chunks[0]["choices"][0]["delta"]
    assert first == {"role": "assistant", "content": "Hi"}
    second = chunks[1]["choices"][0]["delta"]
    assert second == {"content": " there"}
    finish = chunks[2]["choices"][0]["finish_reason"]
    assert finish == "length"
    usage = chunks[-1]["usage"]
    assert usage == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


# ----------------------------------------------------------------------
# OpenAI adapter passthrough
# ----------------------------------------------------------------------


def test_openai_build_body_passthrough():
    request = make_request(temperature=0.3, extra_field="keep-me")
    body = OpenAIProviderAdapter._build_body(request, "model-a")
    assert body["model"] == "model-a"
    assert body["temperature"] == 0.3
    assert body["extra_field"] == "keep-me"
    assert "stream_options" not in body

    streaming = make_request(stream=True, stream_options={"include_usage": False})
    body = OpenAIProviderAdapter._build_body(streaming, "model-a")
    assert body["stream_options"] == {"include_usage": True}


# ----------------------------------------------------------------------
# Error mapping
# ----------------------------------------------------------------------


def test_provider_error_mapping():
    r429 = httpx.Response(429, json={"error": {"message": "slow down"}})
    with pytest.raises(ProviderError) as e:
        raise_for_provider_status(r429, "openai")
    assert e.value.retryable and e.value.fallback
    assert e.value.error_type == "rate_limit_error"

    r401 = httpx.Response(401, json={"error": {"message": "bad key"}})
    with pytest.raises(ProviderError) as e:
        raise_for_provider_status(r401, "openai")
    assert not e.value.retryable and e.value.fallback

    r400 = httpx.Response(400, json={"error": {"message": "bad request"}})
    with pytest.raises(ProviderError) as e:
        raise_for_provider_status(r400, "openai")
    assert not e.value.retryable and not e.value.fallback

    r500 = httpx.Response(500, text="Internal Server Error")
    with pytest.raises(ProviderError) as e:
        raise_for_provider_status(r500, "openai")
    assert e.value.retryable and e.value.fallback
    assert "Internal Server Error" in e.value.message


@pytest.mark.parametrize(
    "status,expected_type",
    [(200, None), (204, None)],
)
def test_provider_success_does_not_raise(status, expected_type):
    response = httpx.Response(status, json={})
    raise_for_provider_status(response, "openai")  # should not raise
