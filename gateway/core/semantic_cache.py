"""Semantic (approximate-match) response cache.

The exact cache (``cache.py``) serves requests that hash identically.
This layer catches the near-duplicates hashing cannot: the normalized
request is embedded once per request (via the configured embeddings
model) and the best same-model cached response with at least
``threshold`` cosine similarity is served instead of calling upstream.

Semantics deliberately mirror the exact cache so the two compose:

- lookup happens *after* request-phase guardrails (redacted requests
  match redacted ones) and before routing;
- only successful responses with usage are stored;
- hits count toward RPM but not TPM/spend (no upstream call happened);
- entries are global (not per key) and bucketed by model;
- stored responses replay for streaming requests (same synthetic
  chunk sequence as exact-cache replays).

The store is per-process, TTL + size bounded (LRU). The embed call is
best-effort: on any failure the lookup degrades to a miss with a
warning — semantic caching must never break or block a request.

``semantic_cache.backend: redis`` swaps in ``RedisSemanticCache``
(``redis_backends.py``) — the same brute-force cosine semantics over
entries shared across every worker/replica, so a near-duplicate served
by one replica is a hit for the next. Either store satisfies the
pipeline's contract: it only depends on ``embed`` / ``get`` / ``put``.
"""

from __future__ import annotations

import logging
import math
import time
from collections import OrderedDict
from typing import Awaitable, Callable

from ..config import SemanticCacheConfig
from ..schemas import ChatCompletionRequest
from .cache import cache_key_for, cache_payload_for

logger = logging.getLogger("gateway.semantic_cache")

# Embeds the normalized request text -> vector.
# The embedder closure built by router.embed_fn_for: takes the text and
# the caller's effective residency requirement (None = unrestricted).
# The second argument is only supplied when a requirement exists —
# single-argument embedders (the pre-tier-28 shape) keep working for
# unrestricted callers, and a restricted caller whose embedder cannot
# accept it degrades to a cache miss (never a residency violation).
EmbedFn = Callable[..., Awaitable[list[float]]]


async def embed_request(
    embed_fn: EmbedFn,
    request: ChatCompletionRequest,
    residencies: frozenset[str] | None = None,
) -> list[float] | None:
    """Shared embed step of both stores (memory and Redis): embed the
    normalized request, degrade to None (never an exception) on failure.

    ``residencies`` is passed to the embedder ONLY when a requirement
    exists (None callers call single-argument embedders, the
    pre-tier-28 shape, so external embed fns keep working unchanged).
    An embedder that cannot accept the argument raises, which lands
    here as a miss: a restricted caller's prompt simply loses
    semantic-cache hits — degrade, never violate.
    """
    try:
        if residencies is None:
            return await embed_fn(cache_payload_for(request))
        return await embed_fn(cache_payload_for(request), residencies)
    except Exception as exc:  # noqa: BLE001
        logger.warning("semantic cache embed failed, treating as miss: %s", exc)
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors (0 when lengths differ)."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class SemanticCache:
    """Similarity lookup over cached responses, per (model, policy) bucket.

    The bucket key includes the response-policy token (see
    GuardrailManager.response_policy_token): a hit skips response
    guardrails, so near-duplicates may only be served to callers whose
    response policy — and tenant — matches the one that stored the
    entry. Same-policy callers (any key) share entries, exactly like
    the exact cache.
    """

    def __init__(
        self,
        config: SemanticCacheConfig,
        embed_fn: EmbedFn,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._embed_fn = embed_fn
        self._clock = clock
        # (model, policy_token, entry_id) -> (stored_at, entry_ttl,
        # embedding, response); insertion order doubles as LRU order,
        # bounded globally across models. The per-entry TTL carries the
        # per-key cache policy (tier 54): a writer with its own window
        # stamps its entry, and a reader's max_age bounds what they
        # accept (a freshness miss leaves the entry for longer windows).
        self._entries: OrderedDict[
            tuple[str, str, str], tuple[float, float, list[float], dict]
        ] = OrderedDict()

    # -- lookup ---------------------------------------------------------

    async def embed(
        self,
        request: ChatCompletionRequest,
        residencies: frozenset[str] | None = None,
    ) -> list[float] | None:
        """Embed the normalized request; None (never an exception) on failure.

        The embedding is computed once per request and reused for both
        the lookup and (on a miss) the store (see ``embed_request``).
        """
        return await embed_request(self._embed_fn, request, residencies)

    async def get(
        self,
        model: str,
        embedding: list[float],
        policy_token: str = "",
        max_age: float | None = None,
    ) -> dict | None:
        """Best same-(model, policy) entry with similarity >= threshold,
        fresh enough for the entry's own TTL and (when set) the
        caller's ``max_age`` window — the per-key freshness bound."""
        threshold = self.config.threshold
        best_key: tuple[str, str, str] | None = None
        best_score = 0.0
        best_response: dict | None = None
        now = self._clock()
        expired: list[tuple[str, str, str]] = []
        for key, (stored_at, entry_ttl, entry_embedding, response) in self._entries.items():
            age = now - stored_at
            if age > entry_ttl:
                expired.append(key)
                continue
            if max_age is not None and age > max_age:
                continue  # too stale for THIS caller; keep for others
            if key[0] != model or key[1] != policy_token:
                continue
            score = cosine_similarity(embedding, entry_embedding)
            if score >= threshold and (best_key is None or score > best_score):
                best_key, best_score, best_response = key, score, response
        for key in expired:
            self._entries.pop(key, None)
        if best_key is not None:
            self._entries.move_to_end(best_key)
        return best_response

    # -- store ----------------------------------------------------------

    async def put(
        self,
        model: str,
        request: ChatCompletionRequest,
        embedding: list[float],
        response: dict,
        policy_token: str = "",
        ttl_seconds: float | None = None,
    ) -> None:
        """Store a successful response under its normalized-request id,
        stamped with the writer's TTL window (None = the configured
        global — per-key cache policy, tier 54)."""
        entry_id = cache_key_for(request)
        self._entries[(model, policy_token, entry_id)] = (
            self._clock(),
            ttl_seconds or self.config.ttl_seconds,
            embedding,
            response,
        )
        self._entries.move_to_end((model, policy_token, entry_id))
        while len(self._entries) > self.config.max_entries:
            self._entries.popitem(last=False)

    async def clear(self) -> None:
        self._entries.clear()
