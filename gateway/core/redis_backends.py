"""Redis-backed rate limiter, response cache and circuit-breaker registry.

Drop-in counterparts of the in-memory ``RateLimiter`` / ``ResponseCache`` /
``CircuitBreakerRegistry`` that share state across workers and replicas
(the in-memory versions are per-process, so limits multiply, caches split
and circuits trip independently with each replica).

Semantics are preserved exactly:

- **Rate limiting** stays a true sliding window: RPM counts request
  timestamps in the last 60s (sorted set, one member per request, score =
  arrival time); TPM sums the token counts of completed requests in the
  last 60s (sorted set of arrivals + a hash carrying each request's token
  count). Wall-clock time is used because ``time.monotonic()`` values are
  not comparable across processes.
- **Cache** stays exact-match with TTL (server-side ``SETEX``) and a
  size bound enforced by evicting the least-recently-*touched* entry
  (an index sorted set scored by last access; ``get`` re-scores).
- **Circuit breakers** keep the same three-state machine (closed / open /
  half-open derived from ``opened_at`` + cooldown), failure threshold and
  request-level accounting: one small hash per deployment, every
  read-modify-write step inside a WATCH/MULTI transaction so concurrent
  replicas cannot interleave a trip with a success. One replica's trip
  opens the circuit for the whole fleet.

Keys are namespaced with a configurable prefix and expire shortly after
their window/TTL, so idle keys clean themselves up. ``check`` and the
record methods are a handful of round trips on a LAN Redis — the atomic
multi-command steps run inside ``transaction=True`` pipelines.

If Redis itself is unavailable, the breaker fails *open* (routes anyway,
logs a warning): the breaker is protection, not correctness, and a Redis
outage should not take down routing. Rate limiting and cache calls
propagate their errors instead.

The pipeline depends only on the method interfaces (check /
record_request / record_usage, get / put / clear, and the async breaker
facade), so selecting backends is a one-line config change
(``rate_limit.backend: redis``, ``cache.backend: redis``,
``routing.breaker.backend: redis``) — see ``main.py``. Tier 34 adds
``RedisVaultInvalidator``, the BYOK vault's cache-invalidation fan-out
(``vault.backend: redis``): publish-on-write + a per-replica subscriber
loop, both best-effort with the vault TTL as the backstop. Tier 59
adds ``RedisSemanticCache`` (``semantic_cache.backend: redis``): the
similarity layer's store shared across replicas — same brute-force
cosine semantics, same embed/get/put contract as the in-memory
``SemanticCache``. Tier 62 adds ``RedisConcurrencyLimiter``
(``concurrency.backend: redis``): the last per-process admission state
goes fleet-shared — one expiring lease per admitted call, the same
acquire/release/snapshot contract as the in-memory limiter, the wait
queue still per-replica.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import logging
import time
import uuid
from contextlib import suppress
from typing import Any, Callable

from redis import asyncio as aioredis
from redis.exceptions import RedisError, WatchError

from ..config import ConcurrencyConfig, SemanticCacheConfig
from ..observability import metrics
from ..schemas import ChatCompletionRequest
from .breaker import CircuitState
from .cache import cache_key_for
from .concurrency import _Waiter
from .semantic_cache import EmbedFn, cosine_similarity, embed_request

logger = logging.getLogger("gateway.redis")


class RedisRateLimiter:
    """Sliding-window RPM/TPM limiter backed by Redis sorted sets."""

    WINDOW_SECONDS = 60.0

    def __init__(
        self,
        client: aioredis.Redis,
        prefix: str = "llm-gateway",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._prefix = f"{prefix}:rl"
        self._clock = clock

    def _keys(self, key: str) -> tuple[str, str, str]:
        return (
            f"{self._prefix}:{key}:rpm",
            f"{self._prefix}:{key}:tpm",
            f"{self._prefix}:{key}:tpm:tokens",
        )

    async def check(
        self,
        key: str,
        rpm_limit: int | None,
        tpm_limit: int | None,
        estimated_tokens: int = 0,
    ) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        if rpm_limit is None and tpm_limit is None:
            return True, 0.0
        now = self._clock()
        cutoff = now - self.WINDOW_SECONDS
        rpm_key, tpm_key, tokens_key = self._keys(key)

        pipe = self._client.pipeline(transaction=True)
        if rpm_limit is not None:
            pipe.zremrangebyscore(rpm_key, "-inf", cutoff)
            pipe.zcard(rpm_key)
            pipe.zrange(rpm_key, 0, 0, withscores=True)  # oldest request
        if tpm_limit is not None:
            pipe.zremrangebyscore(tpm_key, "-inf", cutoff)
            pipe.zrange(tpm_key, 0, 0, withscores=True)  # oldest usage
            pipe.zrange(tpm_key, 0, -1)  # arrival ids still in the window
        results = await pipe.execute()

        if rpm_limit is not None:
            rpm_count = results[1]
            if rpm_count >= rpm_limit:
                oldest = results[2][0][1] if results[2] else now
                return False, max(0.0, self.WINDOW_SECONDS - (now - oldest))

        if tpm_limit is not None:
            # results layout: rpm branch occupies indices 0-2 when present;
            # the tpm commands are [zremrangebyscore, oldest, ids-in-window]
            base = 4 if rpm_limit is not None else 1
            oldest_usage = results[base]
            ids = results[base + 1]
            used = 0
            if ids:
                counts = await self._client.hmget(tokens_key, ids)
                used = sum(int(c) for c in counts if c is not None)
            if used + estimated_tokens > tpm_limit:
                oldest = oldest_usage[0][1] if oldest_usage else now
                return False, max(0.0, self.WINDOW_SECONDS - (now - oldest))
        return True, 0.0

    async def record_request(self, key: str) -> None:
        now = self._clock()
        cutoff = now - self.WINDOW_SECONDS
        rpm_key, _, _ = self._keys(key)
        pipe = self._client.pipeline(transaction=True)
        pipe.zadd(rpm_key, {uuid.uuid4().hex: now})
        pipe.zremrangebyscore(rpm_key, "-inf", cutoff)
        pipe.expire(rpm_key, int(self.WINDOW_SECONDS * 2))
        await pipe.execute()

    async def record_usage(self, key: str, tokens: int) -> None:
        if tokens <= 0:
            return
        now = self._clock()
        cutoff = now - self.WINDOW_SECONDS
        _, tpm_key, tokens_key = self._keys(key)
        member = uuid.uuid4().hex
        pipe = self._client.pipeline(transaction=True)
        pipe.zadd(tpm_key, {member: now})
        pipe.hset(tokens_key, member, tokens)
        pipe.zremrangebyscore(tpm_key, "-inf", cutoff)
        pipe.expire(tpm_key, int(self.WINDOW_SECONDS * 2))
        pipe.expire(tokens_key, int(self.WINDOW_SECONDS * 2))
        await pipe.execute()


class RedisResponseCache:
    """Exact-match response cache backed by Redis (TTL + LRU size bound)."""

    def __init__(
        self,
        client: aioredis.Redis,
        enabled: bool = True,
        ttl_seconds: float = 300,
        max_entries: int = 256,
        prefix: str = "llm-gateway",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self.enabled = enabled
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._key_prefix = f"{prefix}:cache:"
        self._index_key = f"{prefix}:cache:index"
        self._clock = clock

    def _entry_key(self, key: str) -> str:
        return f"{self._key_prefix}{key}"

    def _stored_at_key(self, entry_key: str | bytes) -> str | bytes:
        # Companion to the entry: the write-time wall-clock timestamp
        # (per-key cache policy, tier 54), expiring with its entry. A
        # reader with a freshness window computes the entry's age from
        # it — client-side, where Redis's TTL alone cannot recover the
        # write time. Bytes-in/bytes-out because the LRU index
        # (zrange) speaks bytes under redis-py's default
        # decode_responses=False — str()-ing an index member would
        # build a repr-garbage key (b'…') and silently orphan the real
        # companion on clear/evict.
        if isinstance(entry_key, bytes):
            return entry_key + b":t"
        return f"{entry_key}:t"

    async def get(self, key: str, max_age: float | None = None) -> dict | None:
        if not self.enabled:
            return None
        entry_key = self._entry_key(key)
        raw = await self._client.get(entry_key)
        if raw is None:
            # do not touch the LRU index for misses (would add phantom
            # entries for keys that were never stored / already expired)
            return None
        if max_age is not None:
            ts = await self._client.get(self._stored_at_key(entry_key))
            if ts is not None:
                if self._clock() - float(ts) > max_age:
                    # Fresh enough for the entry's own TTL, too stale
                    # for THIS caller — a per-key freshness miss. The
                    # entry stays for callers with longer windows; no
                    # index touch, like any miss.
                    return None
            else:
                # No companion: a default-window write (or a pre-tier-54
                # entry) — either way it was written with the global
                # TTL, so the remaining TTL recovers its age.
                remaining = await self._client.ttl(entry_key)
                if remaining <= 0 or self.ttl_seconds - remaining > max_age:
                    return None
        await self._client.zadd(self._index_key, {entry_key: self._clock()})  # touch
        return json.loads(raw)

    async def put(self, key: str, response: dict, ttl_seconds: float | None = None) -> None:
        if not self.enabled:
            return
        entry_key = self._entry_key(key)
        # Per-entry EX carries the writer's window (tier 54): None = the
        # configured global TTL, a per-key window stamps its own entry.
        ttl = max(1, int(ttl_seconds or self.ttl_seconds))
        companion = self._stored_at_key(entry_key)
        pipe = self._client.pipeline(transaction=True)
        pipe.set(entry_key, json.dumps(response, default=str), ex=ttl)
        if ttl_seconds is not None:
            # A companion timestamp (the reader-side age source) exists
            # only for custom-window writes: default-window entries are
            # all written with the global TTL, so the remaining TTL
            # recovers their age (see get) — and idempotency, which
            # wraps this backend with default args, keeps its exact
            # one-key-per-entry namespace (retry safety ≠ caching; its
            # semantics deliberately untouched by tier 54).
            pipe.set(companion, str(self._clock()), ex=ttl)
        else:
            # A previous custom-window write of this entry may have
            # left a companion; drop it or it would mis-date the new
            # (global-window) entry for freshness readers.
            pipe.delete(companion)
        pipe.zadd(self._index_key, {entry_key: self._clock()})
        pipe.zcard(self._index_key)
        _, _, _, size = await pipe.execute()
        if size > self.max_entries:
            # evict the least-recently-touched entries (companions go
            # with their entries — they expire together anyway, but a
            # same-pipe delete keeps the namespace tidy under churn)
            victims = await self._client.zrange(
                self._index_key, 0, size - self.max_entries - 1
            )
            if victims:
                pipe = self._client.pipeline(transaction=True)
                pipe.delete(*victims, *[self._stored_at_key(v) for v in victims])
                pipe.zrem(self._index_key, *victims)
                await pipe.execute()

    async def clear(self) -> None:
        members = await self._client.zrange(self._index_key, 0, -1)
        pipe = self._client.pipeline(transaction=True)
        if members:
            pipe.delete(*members)
            # Companions ride along (entries' :t timestamps, tier 54) —
            # note members added before the feature have none, and this
            # delete is idempotent on absent keys.
            pipe.delete(*[self._stored_at_key(m) for m in members])
        pipe.delete(self._index_key)
        await pipe.execute()


class RedisSemanticCache:
    """Semantic (similarity) cache backed by Redis (tier 59).

    The in-memory ``SemanticCache`` is per-process, so a near-duplicate
    that replica A embedded and stored was a guaranteed miss on replica
    B. This class puts the whole store in Redis so every replica shares
    entries — the multi-replica leg the exact cache, rate limiter,
    breakers, vault and idempotency already had. The contract is
    identical (``embed`` / ``get`` / ``put`` / ``clear``; the pipeline
    knows no difference), and so are the semantics:

    * buckets are (model, response-policy token): a hit skips response
      guardrails, so near-duplicates only serve callers whose policy —
      and tenant — match the writer's;
    * per-entry TTL rides the entry's own Redis EX (the per-key cache
      policy, tier 54: a writer's own window stamps its entry), and a
      reader's ``max_age`` bounds what it accepts from the stored
      write time — a freshness miss leaves the entry for wider windows;
    * LRU is a global bound across models, scored by touch time.

    Layout: one string key per entry holding a JSON payload (embedding,
    response, write time), plus one zset index whose members are
    ``model|policy_token|entry_id`` — the bucket is a member PREFIX, so
    a lookup filters same-bucket candidates from the index before any
    payload fetch, then brute-forces cosine similarity locally (same
    math, same threshold, over shared entries). Expired entries (their
    EX passed server-side) leave tombstones in the index; lookups
    prune the ones they walk, and eviction/clear sweep the rest.
    """

    def __init__(
        self,
        client: aioredis.Redis,
        config: SemanticCacheConfig,
        embed_fn: EmbedFn,
        prefix: str = "llm-gateway",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self.config = config
        self._embed_fn = embed_fn
        self._clock = clock
        self._key_prefix = f"{prefix}:sem:"
        self._index_key = f"{prefix}:sem:index"

    # -- keys ---------------------------------------------------------
    # Members are "model|policy_token|entry_id"; the entry key is the
    # prefix + member. The index (zrange) speaks bytes under redis-py's
    # default decode_responses=False, so bucket filtering and entry-key
    # building are byte-wise (the stored-at companion's lesson: str()-ing
    # an index member builds repr-garbage keys and silently orphans the
    # real entry).
    def _member(self, model: str, policy_token: str, entry_id: str) -> str:
        return f"{model}|{policy_token}|{entry_id}"

    def _entry_key(self, member: str | bytes) -> str | bytes:
        prefix = self._key_prefix.encode() if isinstance(member, bytes) else self._key_prefix
        return prefix + member

    async def embed(
        self,
        request: ChatCompletionRequest,
        residencies: frozenset[str] | None = None,
    ) -> list[float] | None:
        return await embed_request(self._embed_fn, request, residencies)

    async def get(
        self,
        model: str,
        embedding: list[float],
        policy_token: str = "",
        max_age: float | None = None,
    ) -> dict | None:
        """Best same-(model, policy) entry with similarity >= threshold,
        fresh enough for the reader's ``max_age`` window — the payload's
        stored write time is the age source (the entry's own EX already
        enforced the writer's window server-side)."""
        threshold = self.config.threshold
        bucket = f"{model}|{policy_token}|".encode()
        members = await self._client.zrange(self._index_key, 0, -1)
        candidates = [m for m in members if m.startswith(bucket)]
        if not candidates:
            return None
        now = self._clock()
        raw_entries = await self._client.mget([self._entry_key(m) for m in candidates])
        best_member: bytes | None = None
        best_score = 0.0
        best_response: dict | None = None
        tombstones: list[bytes] = []
        for member, raw in zip(candidates, raw_entries):
            if raw is None:
                tombstones.append(member)  # EX passed (or evicted): dead index entry
                continue
            entry = json.loads(raw)
            if max_age is not None and now - float(entry["stored_at"]) > max_age:
                continue  # too stale for THIS caller; keep for wider windows
            score = cosine_similarity(embedding, entry["embedding"])
            if score >= threshold and (best_member is None or score > best_score):
                best_member, best_score, best_response = member, score, entry["response"]
        if tombstones:
            # Prune the index entries whose payloads are gone — the
            # lookup is the natural sweeper (bounded by what it walked);
            # eviction and clear are the backstops.
            await self._client.zrem(self._index_key, *tombstones)
        if best_member is not None:
            await self._client.zadd(self._index_key, {best_member: now})  # touch
        return best_response

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
        with the writer's TTL window as the entry's own Redis EX (None =
        the configured global — per-key cache policy, tier 54)."""
        member = self._member(model, policy_token, cache_key_for(request))
        entry_key = self._entry_key(member)
        ttl = max(1, int(ttl_seconds or self.config.ttl_seconds))
        now = self._clock()
        pipe = self._client.pipeline(transaction=True)
        pipe.set(
            entry_key,
            json.dumps(
                {"embedding": embedding, "response": response, "stored_at": now},
                default=str,
            ),
            ex=ttl,
        )
        pipe.zadd(self._index_key, {member: now})
        pipe.zcard(self._index_key)
        _, _, size = await pipe.execute()
        if size > self.config.max_entries:
            # Evict the least-recently-touched members — globally across
            # models, like the in-memory OrderedDict bound.
            victims = await self._client.zrange(
                self._index_key, 0, size - self.config.max_entries - 1
            )
            if victims:
                pipe = self._client.pipeline(transaction=True)
                pipe.delete(*[self._entry_key(v) for v in victims])
                pipe.zrem(self._index_key, *victims)
                await pipe.execute()

    async def clear(self) -> None:
        members = await self._client.zrange(self._index_key, 0, -1)
        pipe = self._client.pipeline(transaction=True)
        if members:
            pipe.delete(*[self._entry_key(m) for m in members])
        pipe.delete(self._index_key)
        await pipe.execute()


class RedisCircuitBreakerRegistry:
    """Circuit-breaker state shared across workers/replicas via Redis.

    Same interface and state machine as the in-memory
    ``CircuitBreakerRegistry`` (breaker.py), selected with
    ``routing.breaker.backend: redis``: one replica tripping a deployment's
    circuit opens it for the whole fleet, and a recovery closes it
    everywhere. State lives in one small hash per deployment
    (``{prefix}:cb:{name}``):

    - ``state``      — raw state (closed / open; half-open is *derived*
      from ``opened_at`` + cooldown, exactly like the in-memory breaker,
      so a stale replica cannot pin a circuit half-open forever);
    - ``failures``   — consecutive request-level failures (closed state);
    - ``opened_at``  — wall-clock seconds when the circuit last tripped
      (wall clock, not monotonic: values must compare across processes);
    - ``opened_count`` — lifetime trips (ops signal, kept across resets).

    Every read-modify-write step runs inside a WATCH/MULTI transaction, so
    two replicas can never interleave e.g. a trip with a success, and the
    trip counter cannot double-count. The hash carries a TTL well beyond
    the cooldown, so idle deployments clean themselves up — a missing
    hash reads as closed, which is what a fresh process assumes anyway.

    If Redis is unavailable, ``allow`` fails open (returns True) and
    record calls are dropped with a warning: the breaker is protection,
    not correctness. Admin operations (snapshots / reset) surface the
    error instead.
    """

    def __init__(
        self,
        client: aioredis.Redis,
        routing,
        prefix: str = "llm-gateway",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.enabled = routing.breaker.enabled
        self._threshold = routing.breaker.failure_threshold
        self._cooldown = routing.breaker.cooldown_seconds
        self._client = client
        self._prefix = f"{prefix}:cb"
        self._clock = clock
        self._providers: dict[str, str] = {}
        # Generous TTL: an OPEN hash must outlive the cooldown by far;
        # everything else is reconstructible from defaults.
        self._ttl = int(max(self._cooldown * 2, 3600))

    # -- registration (config-time, like the in-memory registry) ------------

    def register(self, name: str, provider: str) -> None:
        """Track a deployment and create its gauge series from boot.

        Registration is local (config is identical across replicas); the
        state itself lives in Redis.
        """
        self._providers[name] = provider
        metrics.CIRCUIT_STATE.labels(deployment=name, provider=provider).set(
            CircuitState.CLOSED
        )

    # -- router-facing API (no-ops when disabled) ---------------------------

    async def allow(self, name: str) -> bool:
        if not self.enabled or name not in self._providers:
            return True
        try:
            return await self._txn(name, self._decide_allow)
        except RedisError:
            logger.warning("breaker allow(%r) failed; failing open", name)
            return True

    async def record_success(self, name: str) -> None:
        if not self.enabled or name not in self._providers:
            return
        try:
            await self._txn(name, self._decide_success)
        except RedisError:
            logger.warning("breaker record_success(%r) failed; skipped", name)

    async def record_failure(self, name: str) -> None:
        if not self.enabled or name not in self._providers:
            return
        try:
            await self._txn(name, self._decide_failure)
        except RedisError:
            logger.warning("breaker record_failure(%r) failed; skipped", name)

    # -- admin ----------------------------------------------------------------

    async def reset(self, name: str) -> None:
        """Force-close (admin action); lifetime trip count is kept."""
        if name not in self._providers:
            raise KeyError(name)
        await self._txn(name, self._decide_reset)

    async def snapshots(self) -> dict[str, dict]:
        names = list(self._providers)
        if not names:
            return {}
        pipe = self._client.pipeline(transaction=False)
        for name in names:
            pipe.hgetall(self._key(name))
        rows = await pipe.execute()
        return {name: self._snapshot(self._norm(raw)) for name, raw in zip(names, rows)}

    # -- observability ------------------------------------------------------

    async def refresh_gauges(self) -> None:
        """Re-sync gauges from shared state (called on every /metrics scrape).

        Reads what every replica last wrote, so scrapes are fleet-accurate
        even for trips this process never observed.
        """
        try:
            for name, snapshot in (await self.snapshots()).items():
                metrics.CIRCUIT_STATE.labels(
                    deployment=name, provider=self._providers[name]
                ).set({"closed": 0, "half_open": 1, "open": 2}[snapshot["state"]])
        except RedisError:
            logger.warning("breaker gauge refresh failed; gauges stale")

    # -- internals ---------------------------------------------------------

    def _key(self, name: str) -> str:
        return f"{self._prefix}:{name}"

    @staticmethod
    def _norm(raw: dict) -> dict[str, str]:
        """Decode a hash to str→str (bytes or str depending on client)."""
        out: dict[str, str] = {}
        for k, v in raw.items():
            key = k.decode() if isinstance(k, bytes) else str(k)
            out[key] = v.decode() if isinstance(v, bytes) else str(v)
        return out

    def _raw_state(self, data: dict[str, str]) -> CircuitState:
        return CircuitState(int(data.get("state") or int(CircuitState.CLOSED)))

    async def _txn(self, name: str, decide: Callable[[dict[str, str]], Any]) -> Any:
        """Read-modify-write one breaker hash under WATCH; retry on conflict.

        ``decide(data)`` sees the decoded current state and returns
        ``(writes, result)`` — hash fields to write (or None) plus the
        value to hand back. The write only lands if nobody else touched
        the hash in between; otherwise WatchError retries with fresh
        state, so concurrent replicas serialize instead of interleaving.
        State transitions fire the Prometheus series after the write is
        confirmed.
        """
        key = self._key(name)
        for _ in range(5):
            async with self._client.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    data = self._norm(await pipe.hgetall(key))
                    writes, result = decide(data)
                    old = self._raw_state(data)
                    pipe.multi()
                    if writes:
                        pipe.hset(key, mapping=writes)
                        pipe.expire(key, self._ttl)
                    await pipe.execute()
                    new = CircuitState(int(writes["state"])) if "state" in (writes or {}) else old
                    self._fire_metrics(name, old, new)
                    return result
                except WatchError:
                    continue
        raise WatchError(f"breaker state for {name!r} kept changing")

    def _fire_metrics(self, name: str, old: CircuitState, new: CircuitState) -> None:
        if new is old:
            return
        provider = self._providers[name]
        metrics.CIRCUIT_STATE.labels(deployment=name, provider=provider).set(int(new))
        metrics.CIRCUIT_TRANSITIONS.labels(
            deployment=name, provider=provider, state=new.name.lower()
        ).inc()

    # -- state-machine decisions (pure; run inside the transaction) ---------

    def _decide_allow(self, data: dict[str, str]):
        state = self._raw_state(data)
        if state is not CircuitState.OPEN:
            return None, True
        opened_at = float(data.get("opened_at") or 0.0)
        if self._clock() - opened_at < self._cooldown:
            return None, False
        # cooldown elapsed: promote to half-open (the next request probes)
        return {"state": int(CircuitState.HALF_OPEN)}, True

    def _decide_success(self, data: dict[str, str]):
        state = self._raw_state(data)
        if state is not CircuitState.CLOSED:
            return {"state": int(CircuitState.CLOSED), "failures": 0}, None
        if int(data.get("failures") or 0):
            return {"failures": 0}, None  # stale count from earlier failures
        return None, None  # already closed and clean: nothing to write

    def _decide_failure(self, data: dict[str, str]):
        state = self._raw_state(data)
        if state is CircuitState.OPEN:
            # All-open bypass (the router records for deployments it picked
            # even when circuits were open): already open, and the cooldown
            # must not be extended by concurrent failures.
            return None, None
        if state is CircuitState.HALF_OPEN:
            return self._trip_writes(data), None
        failures = int(data.get("failures") or 0) + 1
        if failures >= self._threshold:
            return self._trip_writes(data), None
        return {"failures": failures}, None

    def _decide_reset(self, data: dict[str, str]):
        return {"state": int(CircuitState.CLOSED), "failures": 0}, None

    def _trip_writes(self, data: dict[str, str]) -> dict:
        return {
            "state": int(CircuitState.OPEN),
            "opened_at": self._clock(),
            "failures": 0,
            "opened_count": int(data.get("opened_count") or 0) + 1,
        }

    def _snapshot(self, data: dict[str, str]) -> dict:
        raw = self._raw_state(data)
        opened_at = float(data.get("opened_at") or 0.0)
        cooldown_remaining = 0.0
        if raw is CircuitState.OPEN:
            elapsed = self._clock() - opened_at
            if elapsed >= self._cooldown:
                state = CircuitState.HALF_OPEN  # derived, like the local breaker
            else:
                state = CircuitState.OPEN
                cooldown_remaining = max(0.0, self._cooldown - elapsed)
        else:
            state = raw
        return {
            "state": state.name.lower(),
            "consecutive_failures": int(data.get("failures") or 0),
            "opened_count": int(data.get("opened_count") or 0),
            "cooldown_remaining_seconds": round(cooldown_remaining, 3),
        }


class RedisVaultInvalidator:
    """Cross-replica cache invalidation for the BYOK vault (tier 34).

    The vault's decrypted-key cache (and its negative twin) is
    per-process by design — process memory is the trust boundary the
    master key already lives in. With ``vault.backend: redis``, every
    admin write (``VaultService.put`` / ``.delete``) fires this
    object's ``publish`` and every replica's ``listen()`` loop drops
    the named entry the moment the message lands, instead of serving
    it — or serving "absent", the negative cache — for up to
    ``vault.ttl_seconds`` more.

    Both directions are best-effort and never fail the admin write:
    a failed publish only slows convergence back to the TTL (the
    documented backstop), and a lost or malformed message is skipped
    with a warning. Delivery is at-most-once per replica: dropping an
    entry twice is a no-op, so self-delivery (the writing replica is
    also subscribed) is harmless — its entry was already dropped
    locally before the publish.

    The listener reconnects on Redis errors with a short backoff:
    invalidation is a liveness optimization, so the loop treats
    outages as "converge via TTL for a while" rather than crashing
    the process.
    """

    def __init__(self, client: aioredis.Redis, channel: str) -> None:
        self._client = client
        self._channel = channel
        # The vault whose cache entries the listener drops. Bound
        # after construction (the vault is built with this object's
        # ``publish`` as its invalidation callback — the two hold each
        # other, so one link is wired by hand in main.py).
        self._vault: Any | None = None

    def bind(self, vault: Any) -> None:
        """Attach the vault this listener invalidates."""
        self._vault = vault

    async def publish(self, tenant_id: str, provider: str) -> None:
        """Announce one (tenant, provider) invalidation to the fleet.
        Never raises: the local drop has already happened, and other
        replicas converge within the TTL when this fails."""
        payload = json.dumps({"tenant_id": tenant_id, "provider": provider})
        try:
            await self._client.publish(self._channel, payload)
        except Exception:  # noqa: BLE001
            logger.warning(
                "vault invalidation publish failed for (%s, %s) — "
                "replicas converge within ttl_seconds",
                tenant_id,
                provider,
                exc_info=True,
            )

    async def listen(self) -> None:
        """The subscriber loop (one background task per process).

        Runs forever; cancelled by shutdown. Malformed payloads are
        skipped with a warning (a foreign publisher on the channel
        must not wedge the loop); Redis outages reconnect after a
        short backoff.
        """
        import asyncio

        if self._vault is None:
            return
        while True:
            pubsub = self._client.pubsub()
            try:
                await pubsub.subscribe(self._channel)
                while True:
                    message = await pubsub.get_message(timeout=1.0)
                    if message is None or message.get("type") != "message":
                        continue
                    self._apply(message.get("data"))
            except asyncio.CancelledError:
                try:
                    await pubsub.aclose()
                except Exception:  # noqa: BLE001
                    pass
                raise
            except Exception:  # noqa: BLE001
                logger.warning(
                    "vault invalidation listener error — reconnecting in 5s "
                    "(replicas converge via ttl_seconds meanwhile)",
                    exc_info=True,
                )
                try:
                    await pubsub.aclose()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(5.0)

    def _apply(self, data: Any) -> None:
        """Drop the named local cache entry. A malformed or foreign
        payload is skipped loudly — never a crash, never a
        wrong-credential risk (an unparseable name simply matches
        nothing)."""
        try:
            if isinstance(data, (bytes, bytearray, str)):
                payload = json.loads(data)
            else:
                payload = data
            if not isinstance(payload, dict):
                raise ValueError("not a JSON object")
            tenant_id = payload["tenant_id"]
            provider = payload["provider"]
            if not isinstance(tenant_id, str) or not isinstance(provider, str):
                raise ValueError("tenant_id/provider must be strings")
        except Exception:  # noqa: BLE001
            logger.warning(
                "vault invalidation listener: skipping malformed message %r", data
            )
            return
        self._vault.invalidate(tenant_id, provider)


class _RedisSlot:
    """One admitted in-flight call (the Redis limiter's slot).

    ``token`` is the lease member in the fleet zsets; ``local`` marks
    a fail-open admission during a Redis outage (counted in-process
    so release stays symmetric — the fleet never saw it). ``released``
    makes double release a no-op, exactly like the in-memory slot.
    """

    __slots__ = ("key_id", "tenant_id", "key_label", "token", "local", "released")

    def __init__(
        self,
        key_id: str,
        tenant_id: str | None,
        key_label: str,
        token: str,
        *,
        local: bool = False,
    ) -> None:
        self.key_id = key_id
        self.tenant_id = tenant_id
        self.key_label = key_label
        self.token = token
        self.local = local
        self.released = False


class RedisConcurrencyLimiter:
    """Fleet-shared concurrency admission: ``concurrency.backend: redis``.

    The in-memory ``ConcurrencyLimiter`` (concurrency.py) counts held
    slots per process, so a fleet behind a load balancer enforced
    cap/N per replica. This class moves the slot count into Redis so
    the cap binds the whole fleet:

    - **Layout**: one sorted set per key (``{prefix}:conc:key:{id}``)
      and one per tenant (``{prefix}:conc:tnt:{id}``) — members are
      lease tokens (one per admitted call), scores the lease's expiry
      epoch. Wall clock, not monotonic: scores must compare across
      processes. An emptied zset disappears by itself, so idle keys
      leave no registry behind.
    - **Admission** is the breaker registry's optimistic-transaction
      pattern: prune expired leases, WATCH both zsets, read the
      counts, cap-check, MULTI + ZADD the lease. A concurrent
      admission on any replica that lands between our read and our
      EXEC aborts ours (WatchError) and the attempt retries with
      fresh counts — two replicas can never both take the last slot.
      The prune runs before every admission so a full-looking zset is
      never carrying ghosts: a crashed replica's slots self-heal at
      the next admission, bounded by the lease TTL.
    - **Renewal**: one sweeper task per limiter (not per slot)
      refreshes every lease this replica holds every
      ``lease_ttl / 2``, so a live call — a slow stream, a long
      completion — never expires under its replica. A renewal batch
      that keeps failing only risks leases expiring early: the cap is
      protection, not correctness.
    - **The wait queue stays per-replica** — same heapq
      ``(-priority, seq)`` semantics as the in-memory limiter, because
      a waiter's future is an in-process object. A poller task retries
      fleet admission every ``poll_interval`` while waiters exist (the
      only way this replica notices another replica's release), and
      this replica's own release retries immediately after its ZREM
      lands. Cross-replica fairness is therefore approximate — FIFO
      holds within a replica, races decide between replicas; waiters
      are bounded by their wait_seconds either way.
    - **Outage**: fail OPEN, the breaker's stance — admission proceeds
      anyway, counted locally so release stays symmetric
      (``slot.local``): a Redis outage must not queue or 429 every
      capped request. Waiters queued before the outage stay queued
      (bounded by wait_seconds) until Redis returns or the wait
      expires.

    ``snapshot()`` reads the fleet: SCAN + prune + ZCARD per lease
    zset (never ``KEYS``) plus this replica's own queued waiters (the
    queue is per-replica by design) and outage-mode admissions. A
    Redis outage propagates out of it — the admin view surfaces the
    error instead of lying, the breaker-snapshot precedent.
    """

    def __init__(
        self,
        client: aioredis.Redis,
        config: ConcurrencyConfig,
        *,
        prefix: str = "llm-gateway",
        clock: Callable[[], float] = time.time,
        poll_interval: float = 0.25,
        renew_interval: float | None = None,
    ) -> None:
        self._client = client
        self._prefix = f"{prefix}:conc"
        self._lease_ttl = max(1.0, float(config.lease_ttl_seconds))
        self._clock = clock
        self._poll_interval = poll_interval
        self._renew_interval = (
            renew_interval if renew_interval is not None else self._lease_ttl / 2.0
        )
        # Per-replica wait queue: same heap semantics as the in-memory
        # limiter (_Waiter comes from there — one definition of the
        # priority ordering, two limiter backends over it).
        self._waiters: list[_Waiter] = []
        self._queued: dict[str, int] = {}
        self._seq = itertools.count()
        # Held leases on THIS replica (token -> slot): the renewal
        # sweeper iterates it, release pops from it.
        self._held: dict[str, _RedisSlot] = {}
        # Fail-open admissions during an outage: counted per key and
        # per tenant in-process so release stays symmetric.
        self._outage: dict[str, int] = {}
        self._outage_tenants: dict[str, int] = {}
        # One drain at a time: the pop → admission round trip → re-push
        # cycle must not interleave with another, or priority inverts
        # (a lower-priority waiter's round trip could land first and
        # take the last slot).
        self._drain_lock = asyncio.Lock()
        # Background tasks (strong refs — the event loop keeps none).
        self._renewer: asyncio.Task | None = None
        self._poller: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()

    # -- naming ---------------------------------------------------------------

    def _key_zset(self, key_id: str) -> str:
        return f"{self._prefix}:key:{key_id}"

    def _tenant_zset(self, tenant_id: str) -> str:
        return f"{self._prefix}:tnt:{tenant_id}"

    # -- fleet admission --------------------------------------------------------

    async def _admit(self, w: _Waiter) -> str | None:
        """One atomic fleet admission: the lease token, or None when a
        cap is full. Raises RedisError when Redis is unreachable — the
        caller decides whether that means fail-open or stay-queued."""
        now = self._clock()
        token = uuid.uuid4().hex
        key_zset = self._key_zset(w.key_id)
        tenant_zset = (
            self._tenant_zset(w.tenant_id) if w.tenant_id is not None else None
        )
        # 1) Prune expired leases (crashed replicas) OUTSIDE the
        #    transaction — idempotent, and it keeps the cap check
        #    honest: a full-looking zset must never be hiding ghosts
        #    behind a genuinely free slot.
        prune = self._client.pipeline(transaction=False)
        prune.zremrangebyscore(key_zset, "-inf", now)
        if tenant_zset is not None:
            prune.zremrangebyscore(tenant_zset, "-inf", now)
        await prune.execute()
        # 2) Optimistic read-decide-write, the breaker registry's
        #    WATCH/MULTI pattern: a concurrent admission on any replica
        #    that touches either zset between our read and our EXEC
        #    aborts ours; retry with fresh counts.
        for _ in range(5):
            async with self._client.pipeline(transaction=True) as pipe:
                try:
                    if tenant_zset is not None:
                        await pipe.watch(key_zset, tenant_zset)
                    else:
                        await pipe.watch(key_zset)
                    key_count = (
                        await pipe.zcard(key_zset) if w.key_cap is not None else 0
                    )
                    tenant_count = (
                        await pipe.zcard(tenant_zset)
                        if tenant_zset is not None and w.tenant_cap is not None
                        else 0
                    )
                    fits = not (
                        w.key_cap is not None and key_count >= w.key_cap
                    ) and not (
                        w.tenant_cap is not None
                        and tenant_zset is not None
                        and tenant_count >= w.tenant_cap
                    )
                    pipe.multi()
                    if fits:
                        expiry = now + self._lease_ttl
                        pipe.zadd(key_zset, {token: expiry})
                        if tenant_zset is not None:
                            pipe.zadd(tenant_zset, {token: expiry})
                    # An empty transaction still EXECs — that is what
                    # releases the watch on the refuse path.
                    await pipe.execute()
                    return token if fits else None
                except WatchError:
                    continue
        raise WatchError("concurrency zsets kept changing")

    def _make_slot(self, w: _Waiter, token: str) -> _RedisSlot:
        slot = _RedisSlot(w.key_id, w.tenant_id, w.key_label, token)
        self._held[token] = slot
        self._ensure_renewer()
        metrics.CONCURRENCY_INFLIGHT.labels(key=w.key_label).inc()
        return slot

    def _admit_local(self, w: _Waiter) -> _RedisSlot:
        """Fail-open admission during a Redis outage: the request runs,
        counted in-process so release stays symmetric — the fleet never
        saw it (snapshot() reports it as this replica's share)."""
        slot = _RedisSlot(
            w.key_id, w.tenant_id, w.key_label, uuid.uuid4().hex, local=True
        )
        self._outage[w.key_id] = self._outage.get(w.key_id, 0) + 1
        if w.tenant_id is not None:
            self._outage_tenants[w.tenant_id] = (
                self._outage_tenants.get(w.tenant_id, 0) + 1
            )
        metrics.CONCURRENCY_INFLIGHT.labels(key=w.key_label).inc()
        return slot

    # -- the per-replica wait queue ---------------------------------------------

    def _enqueue(self, w: _Waiter) -> None:
        heapq.heappush(self._waiters, w)
        self._queued[w.key_id] = self._queued.get(w.key_id, 0) + 1
        metrics.CONCURRENCY_QUEUED.labels(key=w.key_label).inc()
        self._ensure_poller()

    def _queued_dec(self, w: _Waiter) -> None:
        remaining = self._queued.get(w.key_id, 0) - 1
        if remaining > 0:
            self._queued[w.key_id] = remaining
        else:
            self._queued.pop(w.key_id, None)
        metrics.CONCURRENCY_QUEUED.labels(key=w.key_label).dec()

    def _dequeue(self, w: _Waiter) -> None:
        try:
            self._waiters.remove(w)
        except ValueError:
            # Not in the heap — a drain already popped it, and owns
            # this waiter's gauge decrement from that point on.
            return
        heapq.heapify(self._waiters)
        self._queued_dec(w)

    async def _drain(self) -> None:
        """Admit every queued waiter that fits, highest priority first
        — skipping, not stopping, at a waiter that does not fit (a
        waiter on a saturated key never blocks an unrelated one), the
        in-memory drain's semantics. Serialized by a lock: the pop →
        admission round trip → re-push cycle must not interleave with
        another, or priority inverts."""
        if not self._waiters:
            return
        async with self._drain_lock:
            if not self._waiters:
                return
            skipped: list[_Waiter] = []
            while self._waiters:
                w = heapq.heappop(self._waiters)
                if w.future.done():
                    # Defensive: an abandoned waiter should never be in
                    # the heap (its own dequeue removes it) — if one
                    # ever is, the gauge is still ours to fix.
                    self._queued_dec(w)
                    continue
                try:
                    token = await self._admit(w)
                except RedisError:
                    # Outage mid-drain: the waiter stays queued, bounded
                    # by its own wait_seconds; the poller keeps trying.
                    skipped.append(w)
                    continue
                if token is None:
                    skipped.append(w)
                    continue
                if w.future.done():
                    # The waiter gave up while the admission round trip
                    # was in flight: hand the lease straight back —
                    # never hand a slot to a caller that stopped waiting.
                    # Its own dequeue was a no-op (this drain already
                    # popped it), so the gauge decrement is ours.
                    self._queued_dec(w)
                    self._schedule_release(w.key_id, w.tenant_id, token)
                    continue
                self._queued_dec(w)
                w.future.set_result(self._make_slot(w, token))
            for w in skipped:
                if w.future.done():
                    # Abandoned while it sat in this drain's skipped
                    # list — its dequeue found nothing to remove (the
                    # pop had taken it out of the heap), so the gauge
                    # decrement lands here.
                    self._queued_dec(w)
                else:
                    heapq.heappush(self._waiters, w)

    async def _poll_loop(self) -> None:
        """Retry fleet admission for queued waiters — the only way this
        replica notices another replica's release. Exits when the
        queue empties; _enqueue restarts it."""
        try:
            while self._waiters:
                await asyncio.sleep(self._poll_interval)
                if not self._waiters:
                    break
                await self._drain()
        except asyncio.CancelledError:
            pass

    def _ensure_poller(self) -> None:
        if self._poller is None or self._poller.done():
            self._poller = asyncio.create_task(self._poll_loop())

    # -- lease renewal ------------------------------------------------------------

    def _ensure_renewer(self) -> None:
        if self._renewer is None or self._renewer.done():
            self._renewer = asyncio.create_task(self._renew_loop())

    async def _renew_loop(self) -> None:
        """Refresh every held lease's expiry so a live call never ages
        out of its own slot. One sweeper for the whole limiter: admits
        stay cheap, and the sweeper stops with the last release."""
        try:
            while self._held:
                await asyncio.sleep(self._renew_interval)
                if not self._held:
                    break
                expiry = self._clock() + self._lease_ttl
                try:
                    pipe = self._client.pipeline(transaction=False)
                    for slot in self._held.values():
                        pipe.zadd(self._key_zset(slot.key_id), {slot.token: expiry})
                        if slot.tenant_id is not None:
                            pipe.zadd(
                                self._tenant_zset(slot.tenant_id), {slot.token: expiry}
                            )
                    await pipe.execute()
                except RedisError:
                    # Includes construction: a client that is down
                    # raises at pipeline() too. The sweeper survives the
                    # outage and refreshes again next tick.
                    logger.warning(
                        "concurrency lease renewal failed (redis unavailable);"
                        " held leases expire within %.0fs",
                        self._lease_ttl,
                    )
        except asyncio.CancelledError:
            pass

    # -- public API (the in-memory limiter's contract) ------------------------------

    async def acquire(
        self,
        *,
        key_id: str,
        tenant_id: str | None,
        key_label: str,
        key_cap: int | None,
        tenant_cap: int | None,
        priority: int,
        wait_seconds: float,
    ) -> _RedisSlot | None:
        """Admit this request now, or queue it for up to
        ``wait_seconds``. Returns the slot, or None when the wait
        expired (the caller answers 429 + Retry-After) — the in-memory
        limiter's contract, plus one stance of its own: a Redis
        outage admits anyway (fail open)."""
        waiter = _Waiter(
            priority=priority,
            seq=next(self._seq),
            key_id=key_id,
            tenant_id=tenant_id,
            key_label=key_label,
            key_cap=key_cap,
            tenant_cap=tenant_cap,
            future=asyncio.get_running_loop().create_future(),
        )
        # Fast path: uncontended requests never touch the queue.
        try:
            token = await self._admit(waiter)
        except RedisError:
            logger.warning(
                "concurrency admission failed open (redis unavailable):"
                " request admitted without fleet accounting"
            )
            return self._admit_local(waiter)
        if token is not None:
            return self._make_slot(waiter, token)
        if wait_seconds <= 0:
            # 0 = fail fast: no queueing, no poller — straight 429.
            metrics.CONCURRENCY_REJECTED.labels(key=key_label).inc()
            return None
        self._enqueue(waiter)
        try:
            done, _pending = await asyncio.wait(
                {waiter.future}, timeout=max(0.0, wait_seconds)
            )
        except BaseException:
            # The waiting task itself was cancelled (client gone while
            # queued): mark the waiter abandoned — a drain with an
            # admission round trip in flight must see it done and hand
            # the lease straight back — then leave the queue, hand
            # back anything already granted, re-raise.
            if not waiter.future.done():
                waiter.future.set_result(None)
            self._dequeue(waiter)
            if waiter.future.done():
                slot = waiter.future.result()
                if slot is not None:
                    self.release(slot)
            raise
        if done:
            return waiter.future.result()
        # Timed out: mark abandoned first — the same handshake, so a
        # mid-air drain hands the lease back instead of setting a
        # result nobody collects — then leave the queue honestly.
        waiter.future.set_result(None)
        self._dequeue(waiter)
        metrics.CONCURRENCY_REJECTED.labels(key=waiter.key_label).inc()
        return None

    def release(self, slot: _RedisSlot) -> None:
        """Give a slot back and admit whoever fits (sync, like the
        in-memory limiter — the pipeline calls this from response
        tails). Double release is a no-op. The fleet ZREM is
        fire-and-forget: no round trip on the response path, and the
        lease's expiry is the backstop if it never lands. The drain it
        triggers retries this replica's queued waiters immediately;
        other replicas' waiters notice via their pollers."""
        if slot.released:
            return
        slot.released = True
        metrics.CONCURRENCY_INFLIGHT.labels(key=slot.key_label).dec()
        if slot.local:
            # Outage-mode admission: counted here, released here — the
            # fleet never saw it, so there is nothing to ZREM.
            count = self._outage.get(slot.key_id, 0) - 1
            if count > 0:
                self._outage[slot.key_id] = count
            else:
                self._outage.pop(slot.key_id, None)
            if slot.tenant_id is not None:
                tcount = self._outage_tenants.get(slot.tenant_id, 0) - 1
                if tcount > 0:
                    self._outage_tenants[slot.tenant_id] = tcount
                else:
                    self._outage_tenants.pop(slot.tenant_id, None)
            return
        if self._held.pop(slot.token, None) is not None and not self._held:
            # Last release: nothing left to renew — stop the sweeper
            # instead of letting it sleep out its interval. The
            # reference is dropped BEFORE cancelling: a task that is
            # cancelled-but-not-yet-done must not block _ensure_renewer
            # from arming its replacement (a release → drain → admit in
            # the same tick would otherwise leave the new lease
            # unrenewed).
            renewer = self._renewer
            self._renewer = None
            if renewer is not None and not renewer.done():
                renewer.cancel()
        self._schedule_release(slot.key_id, slot.tenant_id, slot.token)

    def _schedule_release(
        self, key_id: str, tenant_id: str | None, token: str
    ) -> None:
        """Fire-and-forget lease removal (release's contract is sync):
        needs a running loop; without one there is nothing to schedule
        and the lease's expiry is the backstop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._remote_release(key_id, tenant_id, token))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _remote_release(
        self, key_id: str, tenant_id: str | None, token: str
    ) -> None:
        try:
            pipe = self._client.pipeline(transaction=False)
            pipe.zrem(self._key_zset(key_id), token)
            if tenant_id is not None:
                pipe.zrem(self._tenant_zset(tenant_id), token)
            await pipe.execute()
        except RedisError:
            logger.warning(
                "concurrency lease release dropped (redis unavailable);"
                " it expires within %.0fs",
                self._lease_ttl,
            )
        await self._drain()

    async def snapshot(self) -> dict:
        """Admin view, same shape as the in-memory limiter's: keys and
        tenants holding slots, queued waiters, totals. Held slots are
        the fleet truth (SCAN the lease zsets, prune expired, count);
        the QUEUE is per-replica by design, so queued counts are this
        process's waiters only — as is the queued_total. Outage-mode
        admissions ride along (real in-flight calls the fleet cannot
        see). A Redis outage propagates — the admin endpoint surfaces
        the error instead of lying, the breaker-snapshot precedent."""
        now = self._clock()
        key_names = await self._scan(f"{self._prefix}:key:*")
        key_counts = await self._prune_and_count(key_names, now)
        tnt_names = await self._scan(f"{self._prefix}:tnt:*")
        tnt_counts = await self._prune_and_count(tnt_names, now)
        keys = {
            self._zset_id(name): {"inflight": count, "queued": 0}
            for name, count in zip(key_names, key_counts)
        }
        tenants = {
            self._zset_id(name): count for name, count in zip(tnt_names, tnt_counts)
        }
        for key_id, count in self._outage.items():
            keys.setdefault(key_id, {"inflight": 0, "queued": 0})["inflight"] += count
        for tenant_id, count in self._outage_tenants.items():
            tenants[tenant_id] = tenants.get(tenant_id, 0) + count
        for key_id, queued in self._queued.items():
            keys.setdefault(key_id, {"inflight": 0, "queued": 0})["queued"] = queued
        return {
            "keys": [
                {"key_id": key_id, **info} for key_id, info in sorted(keys.items())
            ],
            "tenants": [
                {"tenant_id": tenant_id, "inflight": inflight}
                for tenant_id, inflight in sorted(tenants.items())
            ],
            "queued_total": sum(self._queued.values()),
        }

    async def _scan(self, pattern: str) -> list[str | bytes]:
        """SCAN, never KEYS — the house rule on a production Redis."""
        names: list[str | bytes] = []
        cursor = 0
        while True:
            cursor, batch = await self._client.scan(
                cursor=cursor, match=pattern, count=100
            )
            names.extend(batch)
            if not cursor:
                return names

    async def _prune_and_count(self, names: list, now: float) -> list[int]:
        """Prune expired leases and count the survivors, one pipelined
        round trip — the admin view is honest about ghosts too."""
        if not names:
            return []
        pipe = self._client.pipeline(transaction=False)
        for name in names:
            pipe.zremrangebyscore(name, "-inf", now)
            pipe.zcard(name)
        results = await pipe.execute()
        return results[1::2]  # the zcards; the prune results interleave

    @staticmethod
    def _zset_id(name: str | bytes) -> str:
        raw = name.decode() if isinstance(name, bytes) else str(name)
        return raw.rsplit(":", 1)[-1]

    async def aclose(self) -> None:
        """Stop the renewal sweeper and the queue poller (the lifespan
        teardown; the in-memory backend owns no tasks). Held leases
        are left to expire on their own — in-flight requests finish on
        the response path's schedule, and their releases stay
        fire-and-forget — but lease removals already scheduled get a
        brief, bounded window to land before the client goes away (the
        lease TTL is the backstop for any that do not)."""
        for task in (self._renewer, self._poller):
            if task is not None:
                task.cancel()
        for task in (self._renewer, self._poller):
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task
        self._renewer = self._poller = None
        pending = list(self._pending)
        if pending:
            await asyncio.wait(pending, timeout=2.0)


__all__ = [
    "RedisCircuitBreakerRegistry",
    "RedisConcurrencyLimiter",
    "RedisRateLimiter",
    "RedisResponseCache",
    "RedisVaultInvalidator",
    "cache_key_for",
]