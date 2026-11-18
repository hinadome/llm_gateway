"""Idempotency: caller-keyed replay for the chat-family surfaces.

The response cache answers "has anyone asked this exact question
recently?" — content identity, global, cheap to share. Idempotency
answers "did *this caller* already get an answer for *this retry*?" —
caller + client-chosen-key identity, private by construction. The
mechanics are the same (a bounded store of completions with TTL),
so this store wraps whichever response-cache backend is configured
(memory LRU or Redis — the same selection switch), namespaced apart
(``idem:``) with its own TTL and size.

The one thing the response cache cannot do is in-flight
deduplication, and that is idempotency's canonical case: a client
times out at 30s and retries while the original is still executing.
The store keeps a per-key in-flight map — the retry awaits the
original's future (bounded by ``inflight_wait_seconds``), then
replays the stored entry; if the original never finishes, or failed
(errors are never stored — a retry *should* re-execute a transient
failure), the waiter proceeds to execute itself.

This map is in-process: with the Redis backend, duplicates racing
across *replicas* both execute (the store only dedups completed
entries cross-replica) — single-process deployments get the full
guarantee.
"""

from __future__ import annotations

import asyncio
from typing import Any


class IdempotencyStore:
    """Entry store + in-flight coordination for idempotent requests.

    ``backend`` is any implementation of the response-cache interface
    (ResponseCache / RedisResponseCache — async get/put/clear)."""

    def __init__(self, backend: Any, inflight_wait_seconds: float = 30.0) -> None:
        self.backend = backend
        self.inflight_wait_seconds = inflight_wait_seconds
        # entry key -> Future resolved when the original's outcome is
        # settled (stored, or failed — waiters then re-check and, on
        # failure, execute themselves).
        self._inflight: dict[str, asyncio.Future] = {}

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.backend, "enabled", False))

    @staticmethod
    def key_for(auth_key_id: str | None, idempotency_key: str) -> str:
        """Store identity: the calling key (the platform admin has no
        key row — 'admin') + the client-chosen key. Entries are
        private per caller by construction; the client-chosen half is
        normalized (strip) at the pipeline edge."""
        return f"idem:{auth_key_id or 'admin'}:{idempotency_key}"

    async def lookup(self, key: str) -> dict | None:
        return await self.backend.get(key)

    async def store(self, key: str, entry: dict) -> None:
        await self.backend.put(key, entry)

    def begin(self, key: str) -> asyncio.Future | None:
        """Mark an execution in flight. Returns the existing original's
        future when one is already running (the caller awaits it);
        None when the caller is the executor."""
        current = self._inflight.get(key)
        if current is not None and not current.done():
            return current
        self._inflight[key] = asyncio.get_running_loop().create_future()
        return None

    def finish(self, key: str) -> None:
        """Settle an execution: release waiters (they re-check the
        store) and drop the marker. Idempotent — safe from every
        error path."""
        future = self._inflight.pop(key, None)
        if future is not None and not future.done():
            future.set_result(True)
