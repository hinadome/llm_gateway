"""Ephemeral check: run the gateway with a mock upstream and call it with the
real OpenAI SDK's Responses API and the real OpenAI Agents SDK — proving the
/v1/responses bridge is SDK-grade (the default transport of the Agents SDK).

Run with:  uv run --with openai-agents python scripts/agents_sdk_check.py
"""

from __future__ import annotations

import json
import threading
import time

import httpx
import uvicorn

from gateway.config import (
    CacheConfig,
    DeploymentConfig,
    GatewayConfig,
    GuardrailConfig,
    ModelConfig,
    ProviderConfig,
    Settings,
)
from gateway.db import dispose_engine, init_engine, reset_engine_for_tests
from gateway.main import create_app

ADMIN_KEY = "sk-gw-admin-sdktest"
PORT = 8725

TEXT_STREAM = (
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"mock-model","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n'
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"mock-model","choices":[{"index":0,"delta":{"content":"Hel"}}]}\n\n'
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"mock-model","choices":[{"index":0,"delta":{"content":"lo SDK"}}]}\n\n'
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"mock-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"mock-model","choices":[],"usage":{"prompt_tokens":4,"completion_tokens":3,"total_tokens":7}}\n\n'
    "data: [DONE]\n\n"
)

TOOL_STREAM = (
    'data: {"id":"c2","object":"chat.completion.chunk","created":1,"model":"mock-model","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}\n\n'
    'data: {"id":"c2","object":"chat.completion.chunk","created":1,"model":"mock-model","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\": \\"Oslo\\"}"}}]},"finish_reason":null}]}\n\n'
    'data: {"id":"c2","object":"chat.completion.chunk","created":1,"model":"mock-model","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    'data: {"id":"c2","object":"chat.completion.chunk","created":1,"model":"mock-model","choices":[],"usage":{"prompt_tokens":6,"completion_tokens":8,"total_tokens":14}}\n\n'
    "data: [DONE]\n\n"
)


def _wants_tool_call(body: dict) -> bool:
    """If tools are offered and no tool result is in the conversation yet,
    the 'model' decides to call get_weather — the classic agent loop."""
    if not body.get("tools"):
        return False
    return not any(m.get("role") == "tool" for m in body.get("messages") or [])


def _text_completion(content: str) -> dict:
    return {
        "id": "chatcmpl-sdk",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "mock-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
    }


def _tool_completion() -> dict:
    return {
        "id": "chatcmpl-tool",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "mock-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city": "Oslo"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 6, "completion_tokens": 8, "total_tokens": 14},
    }


def upstream(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    CALLS.append(body)
    if body.get("stream"):
        return httpx.Response(200, text=TOOL_STREAM if _wants_tool_call(body) else TEXT_STREAM)
    if _wants_tool_call(body):
        return httpx.Response(200, json=_tool_completion())
    return httpx.Response(200, json=_text_completion("It is 12 degrees and raining in Oslo."))


CALLS: list[dict] = []


def main() -> None:
    reset_engine_for_tests()
    import asyncio

    asyncio.run(init_engine("sqlite+aiosqlite:////tmp/gw-agents-sdk-test.db"))

    config = GatewayConfig(
        providers={"openai": ProviderConfig(api_base="http://mock/v1", api_key="k")},
        models=[
            ModelConfig(
                name="gateway-model",
                deployments=[DeploymentConfig(name="mock", provider="openai", model="mock-model")],
            )
        ],
        guardrails=[GuardrailConfig(name="prompt_injection", params={"threshold": 3})],
        cache=CacheConfig(enabled=False),
    )
    app = create_app(
        settings=Settings(
            admin_key=ADMIN_KEY,
            database_url="sqlite+aiosqlite:////tmp/gw-agents-sdk-test.db",
        ),
        config=config,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    app.state.admin_key = ADMIN_KEY

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)

    # The Agents SDK reads these env vars when constructing its default client.
    import os

    os.environ["OPENAI_BASE_URL"] = f"http://127.0.0.1:{PORT}/v1"
    os.environ["OPENAI_API_KEY"] = ADMIN_KEY

    from openai import OpenAI
    from openai import NotFoundError as OpenAINotFound

    client = OpenAI(base_url=f"http://127.0.0.1:{PORT}/v1", api_key=ADMIN_KEY)

    # --- Responses API, non-streaming ------------------------------------------
    resp = client.responses.create(
        model="gateway-model",
        instructions="Be terse.",
        input="hello",
        max_output_tokens=64,
    )
    assert resp.object == "response"
    assert resp.status == "completed"
    assert resp.output_text == "It is 12 degrees and raining in Oslo.", resp.output_text
    assert resp.usage.input_tokens == 4
    assert resp.usage.output_tokens == 5
    assert resp.output[0].type == "message"
    print("OK  responses.create (text):", resp.output_text)

    # --- Responses API, streaming ----------------------------------------------
    collected = ""
    final = None
    for event in client.responses.create(
        model="gateway-model", input="hello", stream=True
    ):
        if event.type == "response.output_text.delta":
            collected += event.delta
        elif event.type == "response.completed":
            final = event.response
    assert collected == "Hello SDK", collected
    assert final is not None and final.status == "completed"
    assert final.usage.input_tokens == 4 and final.usage.output_tokens == 3
    print("OK  responses.create (stream):", repr(collected))

    # --- Responses API, tool call roundtrip ------------------------------------
    resp = client.responses.create(
        model="gateway-model",
        input="weather?",
        tools=[
            {
                "type": "function",
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                    "additionalProperties": False,
                },
            }
        ],
    )
    fc = next(o for o in resp.output if o.type == "function_call")
    assert fc.name == "get_weather"
    assert json.loads(fc.arguments) == {"city": "Oslo"}
    print("OK  responses.create (function_call):", fc.name, fc.arguments)

    # --- Errors -----------------------------------------------------------------
    try:
        client.responses.create(model="no-such-model", input="x")
        raise SystemExit("expected NotFoundError")
    except OpenAINotFound:
        print("OK  unknown model -> NotFoundError")

    # --- OpenAI Agents SDK ------------------------------------------------------
    from agents import Agent, Runner, function_tool, set_tracing_disabled

    set_tracing_disabled(True)  # tracing exports would hit api.openai.com

    tool_calls_made: list[str] = []

    @function_tool
    def get_weather(city: str) -> str:
        tool_calls_made.append(city)
        return "12 degrees and raining"

    agent = Agent(
        name="Weather assistant",
        instructions="Use the get_weather tool exactly once, then answer.",
        model="gateway-model",
        tools=[get_weather],
    )

    async def run_agents_sdk() -> None:
        result = await Runner.run(agent, "What is the weather in Oslo?")
        assert "12" in result.final_output, result.final_output
        # the loop went: model -> function_call -> tool output -> model -> text
        assert tool_calls_made == ["Oslo"]
        print("OK  Agents SDK run():", result.final_output)

        del tool_calls_made[:]
        del CALLS[:]
        streamed = Runner.run_streamed(agent, "What is the weather in Oslo?")
        text_deltas = 0
        async for event in streamed.stream_events():
            if event.type == "raw_response_event" and getattr(event, "data", None) is not None:
                if event.data.type == "response.output_text.delta":
                    text_deltas += 1
        # the same loop, streamed: tool call event, tool executed, then text
        assert tool_calls_made == ["Oslo"], tool_calls_made
        streamed_rounds = [
            any(m.get("role") == "tool" for m in c.get("messages") or [])
            for c in CALLS
        ]
        assert streamed_rounds == [False, True], streamed_rounds
        assert text_deltas >= 2  # "Hel" + "lo SDK" deltas came through
        assert streamed.final_output == "Hello SDK", streamed.final_output
        print("OK  Agents SDK run_streamed():", streamed.final_output)

    asyncio.run(run_agents_sdk())

    server.should_exit = True
    thread.join(timeout=5)
    print("ALL RESPONSES-BRIDGE SDK CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        import asyncio

        asyncio.run(dispose_engine())
