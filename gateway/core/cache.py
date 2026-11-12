"""Exact-match response cache (in-memory, TTL + size-bounded LRU).

The cache key is a SHA-256 over the normalized request (model, messages,
sampling params, tools). Guardrail modifications happen *before* lookup, so
redacted requests share cache entries with other redacted ones.

Cached responses are replayed for streaming requests as a synthetic
chunk + finish + [DONE] sequence.

The interface is async so the in-memory and Redis implementations are
interchangeable (``RedisResponseCache`` in ``redis_backends.py`` shares
entries across workers/replicas; the pipeline only depends on the
``get`` / ``put`` / ``clear`` methods).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any

from ..schemas import ChatCompletionRequest

_CACHE_FIELDS = (
    "model",
    "messages",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "presence_penalty",
    "frequency_penalty",
    "stop",
    "tools",
    "tool_choice",
    "response_format",
    "seed",
    "n",
    # Sampling knobs that change the *distribution* (or the response body)
    # must be part of the identity: a logprobs request must never be served
    # a cached plain response, and logit_bias changes what is generated.
    "logprobs",
    "top_logprobs",
    "logit_bias",
)


def cache_payload_for(request: ChatCompletionRequest) -> str:
    """Serialized normalized request — the identity both caches work on."""
    # getattr with a default: fields may arrive as schema extras rather than
    # declared attributes, in which case absent means "not set" (None).
    payload = {field: getattr(request, field, None) for field in _CACHE_FIELDS}
    return json.dumps(payload, sort_keys=True, default=repr)


def cache_key_for(request: ChatCompletionRequest) -> str:
    return hashlib.sha256(cache_payload_for(request).encode()).hexdigest()


class ResponseCache:
    def __init__(self, enabled: bool = True, ttl_seconds: float = 300, max_entries: int = 256) -> None:
        self.enabled = enabled
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._store: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    async def get(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        entry = self._store.get(key)
        if entry is None:
            return None
        stored_at, response = entry
        if time.monotonic() - stored_at > self.ttl_seconds:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return response

    async def put(self, key: str, response: dict) -> None:
        if not self.enabled:
            return
        self._store[key] = (time.monotonic(), response)
        self._store.move_to_end(key)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)

    async def clear(self) -> None:
        self._store.clear()

    @staticmethod
    def response_for_stream_replay(response: dict[str, Any]) -> list[dict[str, Any]]:
        """Convert a cached full response into OpenAI stream chunks."""
        chunks: list[dict[str, Any]] = []
        first_choice = (response.get("choices") or [{}])[0]
        message = first_choice.get("message") or {}
        chunks.append(
            {
                "id": response.get("id"),
                "object": "chat.completion.chunk",
                "created": response.get("created"),
                "model": response.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": message.get("content") or "",
                            **({"tool_calls": message["tool_calls"]} if message.get("tool_calls") else {}),
                        },
                        "finish_reason": None,
                    }
                ],
            }
        )
        chunks.append(
            {
                "id": response.get("id"),
                "object": "chat.completion.chunk",
                "created": response.get("created"),
                "model": response.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": first_choice.get("finish_reason") or "stop",
                    }
                ],
            }
        )
        if response.get("usage"):
            chunks.append(
                {
                    "id": response.get("id"),
                    "object": "chat.completion.chunk",
                    "created": response.get("created"),
                    "model": response.get("model"),
                    "choices": [],
                    "usage": response.get("usage"),
                }
            )
        return chunks
