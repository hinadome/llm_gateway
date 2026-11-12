"""Shared fixtures: a full gateway app wired to a mock upstream provider."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from gateway.api.deps import hash_key
from gateway.config import (
    AlertsConfig,
    AuditConfig,
    AuthConfig,
    CacheConfig,
    DeploymentConfig,
    GatewayConfig,
    GuardrailConfig,
    JWTAuthConfig,
    LoggingConfig,
    MetricsConfig,
    ModelConfig,
    PromptsConfig,
    ProviderConfig,
    RoutingConfig,
    SemanticCacheConfig,
)
from gateway.db import dispose_engine, init_engine, reset_engine_for_tests
from gateway.main import create_app
from gateway.models import VirtualKey
from gateway.config import Settings

ADMIN_KEY = "sk-gw-admin-testkey"

STREAM_TEXT = (
    'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":1700000000,"model":"model-a","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":1700000000,"model":"model-a","choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":1700000000,"model":"model-a","choices":[{"index":0,"delta":{"content":"lo!"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":1700000000,"model":"model-a","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":1700000000,"model":"model-a","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n'
    "data: [DONE]\n\n"
)

# Responses-API native streams (native_responses deployments): text and
# tool-call variants, mirroring the chat stream's "Hel"+"lo!" deltas and
# 5/2/7 usage so assertions stay parallel across both southbound paths.
RESPONSES_STREAM_TEXT = (
    'data: {"type":"response.created","response":{"id":"resp-mock123","object":"response","created_at":1700000000,"model":"model-a","status":"in_progress"}}\n\n'
    'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1","status":"in_progress","role":"assistant","content":[]}}\n\n'
    'data: {"type":"response.content_part.added","item_id":"msg_1","output_index":0,"content_index":0,"part":{"type":"output_text","text":"","annotations":[]}}\n\n'
    'data: {"type":"response.output_text.delta","item_id":"msg_1","output_index":0,"content_index":0,"delta":"Hel"}\n\n'
    'data: {"type":"response.output_text.delta","item_id":"msg_1","output_index":0,"content_index":0,"delta":"lo!"}\n\n'
    'data: {"type":"response.output_text.done","item_id":"msg_1","output_index":0,"content_index":0,"text":"Hello!"}\n\n'
    'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"message","id":"msg_1","status":"completed","role":"assistant","content":[{"type":"output_text","text":"Hello!","annotations":[]}]}}\n\n'
    'data: {"type":"response.completed","response":{"id":"resp-mock123","object":"response","created_at":1700000000,"model":"model-a","status":"completed","usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}}\n\n'
)

RESPONSES_TOOL_STREAM_TEXT = (
    'data: {"type":"response.created","response":{"id":"resp-mock123","object":"response","created_at":1700000000,"model":"model-a","status":"in_progress"}}\n\n'
    'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_mock1","name":"get_weather","arguments":"","status":"in_progress"}}\n\n'
    'data: {"type":"response.function_call_arguments.delta","item_id":"fc_1","output_index":0,"delta":"{\\"city\\":"}\n\n'
    'data: {"type":"response.function_call_arguments.delta","item_id":"fc_1","output_index":0,"delta":" \\"Paris\\"}"}\n\n'
    'data: {"type":"response.function_call_arguments.done","item_id":"fc_1","output_index":0,"arguments":"{\\"city\\": \\"Paris\\"}"}\n\n'
    'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_mock1","name":"get_weather","arguments":"{\\"city\\": \\"Paris\\"}","status":"completed"}}\n\n'
    'data: {"type":"response.completed","response":{"id":"resp-mock123","object":"response","created_at":1700000000,"model":"model-a","status":"completed","usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}}\n\n'
)


def make_config(**cache_overrides) -> GatewayConfig:
    redis_backends = cache_overrides.pop("redis_backends", False)
    routing = cache_overrides.pop("routing", None) or RoutingConfig()
    alerts = cache_overrides.pop("alerts", None) or AlertsConfig()
    semantic_cache = cache_overrides.pop("semantic_cache", None) or SemanticCacheConfig()
    model_aliases = cache_overrides.pop("model_aliases", None) or {}
    default_model = cache_overrides.pop("default_model", None)
    logging_cfg = cache_overrides.pop("logging", None) or LoggingConfig()
    auth = cache_overrides.pop("auth", None) or AuthConfig()
    audit = cache_overrides.pop("audit", None) or AuditConfig()
    metrics_cfg = cache_overrides.pop("metrics", None) or MetricsConfig()
    prompts = cache_overrides.pop("prompts", None) or PromptsConfig()
    guardrails = cache_overrides.pop("guardrails", None) or [
        GuardrailConfig(
            name="pii_redaction", enabled=True, params={"mask_in_response": True}
        ),
        GuardrailConfig(name="prompt_injection", enabled=True, params={"threshold": 3}),
        GuardrailConfig(name="output_moderation", enabled=True),
        GuardrailConfig(name="response_schema", enabled=True),
    ]
    models = cache_overrides.pop("models", None) or [
        ModelConfig(
            name="test-model",
            deployments=[
                DeploymentConfig(
                    name="openai/primary", provider="openai", model="model-a"
                ),
                DeploymentConfig(
                    name="openai/fallback",
                    provider="openai",
                    model="model-b",
                    priority=200,
                ),
            ],
        ),
        ModelConfig(
            name="other-model",
            deployments=[
                DeploymentConfig(
                    name="openai/other", provider="openai", model="model-c"
                )
            ],
        ),
    ]
    pricing = cache_overrides.pop("pricing", None) or {"model-a": {"input": 2.0, "output": 4.0}}
    providers = cache_overrides.pop("providers", None) or {
        "openai": ProviderConfig(
            api_base="http://mockupstream.test/v1", api_key="provider-key"
        )
    }
    config = GatewayConfig(
        providers=providers,
        models=models,
        model_aliases=model_aliases,
        default_model=default_model,
        logging=logging_cfg,
        guardrails=guardrails,
        cache=CacheConfig(enabled=True, ttl_seconds=300, max_entries=128, **cache_overrides),
        routing=routing,
        alerts=alerts,
        auth=auth,
        audit=audit,
        metrics=metrics_cfg,
        prompts=prompts,
        semantic_cache=semantic_cache,
        pricing=pricing,
    )
    if redis_backends:
        config.cache.backend = "redis"
        config.rate_limit.backend = "redis"
    return config


class MockUpstream:
    """Records requests; returns canned OpenAI-style responses."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.fail_counts: dict[str, int] = {"model-a": 0, "model-b": 0, "model-c": 0}
        # Optional override for the SSE body streamed back (tests set this
        # to exercise boundary-spanning content across chunk edges).
        self.stream_text: str | None = None

    @staticmethod
    def _embedding_response(body: dict) -> dict:
        """Deterministic OpenAI-format embeddings: seed per input, fixed dims."""
        inputs = body.get("input")
        inputs = [inputs] if isinstance(inputs, str) else inputs
        dims = body.get("dimensions") or 8
        data = []
        for i, text in enumerate(inputs):
            seed = sum(text.encode()) or 1
            vector = [round(((seed * (j + 1)) % 997) / 997.0, 6) for j in range(dims)]
            data.append({"object": "embedding", "embedding": vector, "index": i})
        prompt_tokens = sum(max(1, len(t) // 4) for t in inputs)
        return {
            "object": "list",
            "data": data,
            "model": body.get("model"),
            "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
        }

    @staticmethod
    def _content_for(text: str) -> str:
        if "PIIPLEASE" in text:
            return "Sure, my email is bot@example.com."
        if "BADOUTPUT" in text:
            return "Here is how to obtain enriched uranium at home."
        return "Hello from the mock!"

    @classmethod
    def _response_content(cls, body: dict) -> str:
        text = " ".join(
            (m.get("content") or "") if isinstance(m.get("content"), str) else ""
            for m in body.get("messages", [])
        )
        return cls._content_for(text)

    @classmethod
    def _responses_route(cls, body: dict) -> httpx.Response:
        """Replies in the provider-native Responses shape (/responses)."""
        inp = body.get("input")
        if isinstance(inp, str):
            text = inp
        else:
            parts: list[str] = []
            for item in inp or []:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if isinstance(content, str):
                    parts.append(content)
                else:
                    for part in content or []:
                        if isinstance(part, dict) and part.get("type") in (
                            "input_text",
                            "text",
                        ):
                            parts.append(part.get("text") or "")
            text = " ".join(parts)
        tools = [t for t in (body.get("tools") or []) if isinstance(t, dict)]
        if body.get("stream"):
            return httpx.Response(
                200,
                text=RESPONSES_TOOL_STREAM_TEXT if tools else RESPONSES_STREAM_TEXT,
            )
        if tools:
            output = [
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_mock1",
                    "name": tools[0].get("name") or "tool",
                    "arguments": '{"city": "Paris"}',
                    "status": "completed",
                }
            ]
        else:
            output = [
                {
                    "type": "message",
                    "id": "msg_1",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": cls._content_for(text),
                            "annotations": [],
                        }
                    ],
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": "resp-mock123",
                "object": "response",
                "created_at": 1700000000,
                "model": body.get("model"),
                "status": "completed",
                "output": output,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 7,
                    "total_tokens": 17,
                },
            },
        )

    @staticmethod
    def _multipart_fields(content: bytes, content_type: str) -> dict[str, str]:
        """Minimal multipart/form-data parse (field name -> value/filename).

        Good enough for assertions about what the provider received —
        not a general MIME parser.
        """
        fields: dict[str, str] = {}
        if "boundary=" not in content_type:
            return fields
        boundary = content_type.split("boundary=")[-1].strip().encode()
        for part in content.split(b"--" + boundary):
            if b'name="' not in part:
                continue
            header, _, value = part.partition(b"\r\n\r\n")
            if not _:
                continue
            name = header.split(b'name="')[1].split(b'"')[0].decode()
            if b'filename="' in header:
                fields[name] = header.split(b'filename="')[1].split(b'"')[0].decode()
            else:
                fields[name] = value.rstrip(b"\r\n").decode(errors="replace")
        return fields

    def handler(self, request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.content) if request.content else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {"_raw": True, "size_bytes": len(request.content)}
        self.requests.append(
            {"url": str(request.url), "headers": dict(request.headers), "body": body}
        )
        model = body.get("model")
        if self.fail_counts.get(model, 0) > 0:
            self.fail_counts[model] -= 1
            return httpx.Response(500, json={"error": {"message": "upstream boom"}})
        path = request.url.path
        if path.endswith("/responses"):
            return self._responses_route(body)
        if path.endswith("/images/generations"):
            n = int(body.get("n") or 1)
            return httpx.Response(
                200,
                json={
                    "created": 1700000000,
                    "data": [{"b64_json": f"aW1hZ2VkYXRh{i}"} for i in range(n)],
                },
            )
        if path.endswith("/audio/transcriptions") or path.endswith(
            "/audio/translations"
        ):
            fields = self._multipart_fields(
                request.content, request.headers.get("content-type", "")
            )
            # Surface the parsed form (not the raw bytes) for assertions.
            self.requests[-1]["body"] = {
                "_multipart": fields,
                "endpoint": path.rsplit("/", 1)[-1],
            }
            if self.fail_counts.get(fields.get("model"), 0) > 0:
                self.fail_counts[fields["model"]] -= 1
                return httpx.Response(500, json={"error": {"message": "boom"}})
            fmt = fields.get("response_format")
            if fmt in ("text", "srt", "vtt"):
                text = (
                    "1\n00:00:00,000 --> 00:00:01,000\ntranscribed words\n"
                    if fmt == "srt"
                    else "transcribed words"
                )
                return httpx.Response(200, text=text)
            out = {"text": "transcribed words", "model": fields.get("model")}
            if fmt == "verbose_json":
                out["duration"] = 42.0  # seconds — drives per-minute cost
            return httpx.Response(200, json=out)
        if path.endswith("/audio/speech"):
            return httpx.Response(
                200,
                content=b"FAKEAUDIODATA",
                headers={"content-type": "audio/mpeg"},
            )
        if path.endswith("/embeddings"):
            return httpx.Response(200, json=self._embedding_response(body))
        if ":batchEmbedContents" in path:
            # Gemini-format embeddings: one vector per sub-request.
            requests_ = body.get("requests", [])
            return httpx.Response(
                200,
                json={
                    "embeddings": [
                        {"values": [0.25, 0.5, 0.75, 1.0]} for _ in requests_
                    ]
                },
            )
        if ":generateContent" in path and "IMAGE" in (
            (body.get("generationConfig") or {}).get("responseModalities") or []
        ):
            # Gemini image generation: one inline image + some text.
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "here is your image"},
                                    {
                                        "inlineData": {
                                            "mimeType": "image/png",
                                            "data": "Z2VtaW5pLWltZw==",
                                        }
                                    },
                                ]
                            }
                        }
                    ]
                },
            )
        if body.get("stream"):
            return httpx.Response(200, text=self.stream_text or STREAM_TEXT)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-mock123",
                "object": "chat.completion",
                "created": 1700000000,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": self._response_content(body),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 7,
                    "total_tokens": 17,
                },
            },
        )

    def bodies(self) -> list[dict]:
        return [r["body"] for r in self.requests]


async def _make_env(
    tmp_path,
    tracer_provider=None,
    redis_client=None,
    config=None,
    alert_client=None,
    semantic_embedder=None,
    cors_allow_origins=None,
    jwks_client=None,
    audit_client=None,
):
    """Build a full app (fresh DB + mock upstream) as an async generator."""
    reset_engine_for_tests()
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    await init_engine(db_url)

    mock = MockUpstream()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock.handler))
    settings = Settings(
        admin_key=ADMIN_KEY,
        database_url=db_url,
        cors_allow_origins=cors_allow_origins,
    )
    app = create_app(
        settings=settings,
        config=config or make_config(),
        http_client=http_client,
        tracer_provider=tracer_provider,
        redis_client=redis_client,
        alert_http_client=alert_client,
        semantic_embedder=semantic_embedder,
        jwks_http_client=jwks_client,
        audit_http_client=audit_client,
    )
    app.state.admin_key = ADMIN_KEY  # ASGITransport does not run the lifespan

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as client:
        yield SimpleNamespace(app=app, client=client, mock=mock)

    # Background log writes (logging.background_write): drain the queue
    # before the engine goes away (mirrors the lifespan teardown).
    logging_stop = getattr(app.state.pipeline.logging_service, "stop", None)
    if logging_stop is not None:
        await logging_stop()
    await http_client.aclose()
    await dispose_engine()


@pytest.fixture
async def env(tmp_path):
    async for ns in _make_env(tmp_path):
        yield ns


# ----------------------------------------------------------------------
# OpenTelemetry fixtures: a TracerProvider with an in-memory exporter,
# injected into the app via create_app(tracer_provider=...). No global
# state is touched, so this composes with the plain fixtures above.
# ----------------------------------------------------------------------


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider(span_exporter: InMemorySpanExporter) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    return provider


@pytest.fixture
async def otel_env(tmp_path, tracer_provider):
    """Like ``env`` but with tracing instrumented into the app."""
    async for ns in _make_env(tmp_path, tracer_provider):
        yield ns


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def chat_body(
    model: str = "test-model", content: str = "Hello", stream: bool = False, **extra
) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "stream": stream,
        **extra,
    }


async def create_key(client: httpx.AsyncClient, name: str = "test-key", **fields) -> dict:
    resp = await client.post(
        "/admin/keys", json={"name": name, **fields}, headers=auth(ADMIN_KEY)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
