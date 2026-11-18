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
loop, both best-effort with the vault TTL as the backstop.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable

from redis import asyncio as aioredis
from redis.exceptions import RedisError, WatchError

from ..observability import metrics
from .breaker import CircuitState
from .cache import cache_key_for  # noqa: F401 — re-exported for convenience

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

    async def get(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        entry_key = self._entry_key(key)
        raw = await self._client.get(entry_key)
        if raw is None:
            # do not touch the LRU index for misses (would add phantom
            # entries for keys that were never stored / already expired)
            return None
        await self._client.zadd(self._index_key, {entry_key: self._clock()})  # touch
        return json.loads(raw)

    async def put(self, key: str, response: dict) -> None:
        if not self.enabled:
            return
        entry_key = self._entry_key(key)
        ttl = max(1, int(self.ttl_seconds))
        pipe = self._client.pipeline(transaction=True)
        pipe.set(entry_key, json.dumps(response, default=str), ex=ttl)
        pipe.zadd(self._index_key, {entry_key: self._clock()})
        pipe.zcard(self._index_key)
        _, _, size = await pipe.execute()
        if size > self.max_entries:
            # evict the least-recently-touched entries
            victims = await self._client.zrange(
                self._index_key, 0, size - self.max_entries - 1
            )
            if victims:
                pipe = self._client.pipeline(transaction=True)
                pipe.delete(*victims)
                pipe.zrem(self._index_key, *victims)
                await pipe.execute()

    async def clear(self) -> None:
        members = await self._client.zrange(self._index_key, 0, -1)
        pipe = self._client.pipeline(transaction=True)
        if members:
            pipe.delete(*members)
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


__all__ = [
    "RedisCircuitBreakerRegistry",
    "RedisRateLimiter",
    "RedisResponseCache",
    "RedisVaultInvalidator",
    "cache_key_for",
]