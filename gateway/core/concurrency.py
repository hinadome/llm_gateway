"""Concurrent-request admission control with priority queueing (QoS).

RPM/TPM bound the *rate* of requests; this bounds how many may be
in-flight against the upstream at once — the number that actually
protects a provider quota, a small deployment fleet or a human's
attention. The slot wraps the **upstream call**: acquired after the
cache miss (hits and idempotent replays never touch upstream, never
hold a slot), released wherever the in-flight gauge decrements —
``_finish_span`` for non-streams, the stream wrapper's ``finally``
for streams, so a slow client reading a stream holds its slot, and an
error path never leaks one.

Caps live on the rows, snapshotted at auth time like every other
ceiling: ``VirtualKey.max_concurrency`` and the pooled
``Tenant.max_concurrency`` (a member key must pass both — the tenant
can only narrow). A caller with neither cap skips admission entirely.

When all slots are taken, a request does not fail — it **queues** for
up to ``concurrency.wait_seconds`` (0 = fail fast), and admission is
priority-ordered: ``VirtualKey.priority`` (higher first, FIFO within a
priority — default 0). A waiter whose *own* key is saturated does not
block waiters on other keys: the drain scans in priority order and
skips, not stops, at a waiter that does not fit. A queue wait that
expires answers 429 ``rate_limit_exceeded`` + ``Retry-After`` — the
pipeline raises, emits a ``concurrency.timeout`` observer event and
counts ``gateway_concurrency_rejected_total{key}``.

All state is in-process here (like the idempotency in-flight map and
runaway detection): a multi-replica fleet enforces its per-replica
share — run N replicas behind a load balancer with cap/N, accept the
approximation, or move the slot count into Redis with
``concurrency.backend: redis`` (``RedisConcurrencyLimiter``,
redis_backends.py — same acquire/release/snapshot contract, same
metrics, fleet-shared caps; tier 62). No locks anywhere: everything
runs on the event loop, so each acquire/release/drain is atomic by
construction.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools

from ..observability import metrics


class _Slot:
    """One admission — handed to the caller on acquire, given back on
    release. Carries what release needs: which counters to decrement
    and which metric label the gauge decrements under. ``released``
    makes double-release a no-op (counters would otherwise go
    negative and admit extra waiters)."""

    __slots__ = ("key_id", "tenant_id", "key_label", "released")

    def __init__(self, key_id: str, tenant_id: str | None, key_label: str) -> None:
        self.key_id = key_id
        self.tenant_id = tenant_id
        self.key_label = key_label
        self.released = False


class _Waiter:
    """A queued admission request. Heap-ordered by (-priority, seq):
    higher priority first, arrival order (seq) within a priority."""

    __slots__ = ("priority", "seq", "key_id", "tenant_id", "key_label",
                 "key_cap", "tenant_cap", "future")

    def __init__(
        self,
        priority: int,
        seq: int,
        key_id: str,
        tenant_id: str | None,
        key_label: str,
        key_cap: int | None,
        tenant_cap: int | None,
        future: asyncio.Future,
    ) -> None:
        self.priority = priority
        self.seq = seq
        self.key_id = key_id
        self.tenant_id = tenant_id
        self.key_label = key_label
        self.key_cap = key_cap
        self.tenant_cap = tenant_cap
        self.future = future

    def __lt__(self, other: "_Waiter") -> bool:
        # heapq is a min-heap: lower tuple sorts first, so higher
        # priority must yield a LOWER value.
        return (-self.priority, self.seq) < (-other.priority, other.seq)


class ConcurrencyLimiter:
    def __init__(self) -> None:
        # key_id / tenant_id -> slots currently held (always counted,
        # cap or not — the snapshot is more honest for it).
        self._inflight: dict[str, int] = {}
        self._tenant_inflight: dict[str, int] = {}
        # key_id -> requests currently queued (gauge + snapshot).
        self._queued: dict[str, int] = {}
        self._waiters: list[_Waiter] = []
        self._seq = itertools.count()

    # -- admission -----------------------------------------------------------

    def _admit(self, w: _Waiter) -> _Slot | None:
        """Take a key + tenant slot if both fit, else None. No locking:
        callers run on the event loop, so this is atomic by construction."""
        if w.key_cap is not None and self._inflight.get(w.key_id, 0) >= w.key_cap:
            return None
        if (
            w.tenant_cap is not None
            and w.tenant_id is not None
            and self._tenant_inflight.get(w.tenant_id, 0) >= w.tenant_cap
        ):
            return None
        self._inflight[w.key_id] = self._inflight.get(w.key_id, 0) + 1
        if w.tenant_id is not None:
            self._tenant_inflight[w.tenant_id] = (
                self._tenant_inflight.get(w.tenant_id, 0) + 1
            )
        metrics.CONCURRENCY_INFLIGHT.labels(key=w.key_label).inc()
        return _Slot(w.key_id, w.tenant_id, w.key_label)

    def _enqueue(self, w: _Waiter) -> None:
        heapq.heappush(self._waiters, w)
        self._queued[w.key_id] = self._queued.get(w.key_id, 0) + 1
        metrics.CONCURRENCY_QUEUED.labels(key=w.key_label).inc()

    def _dequeue(self, w: _Waiter) -> None:
        try:
            self._waiters.remove(w)
        except ValueError:
            return  # already removed
        heapq.heapify(self._waiters)
        remaining = self._queued.get(w.key_id, 0) - 1
        if remaining > 0:
            self._queued[w.key_id] = remaining
        else:
            self._queued.pop(w.key_id, None)
        metrics.CONCURRENCY_QUEUED.labels(key=w.key_label).dec()

    def _drain(self) -> None:
        """Admit every waiter that fits, highest priority first. A
        waiter whose resources are still full is SKIPPED, not removed —
        a waiter on a different (unsaturated) key must not block behind
        it, and skipping cannot starve anyone: each waiter only ever
        waits for its own key's or tenant's capacity."""
        if not self._waiters:
            return
        skipped: list[_Waiter] = []
        while self._waiters:
            w = heapq.heappop(self._waiters)
            slot = self._admit(w)
            if slot is None:
                skipped.append(w)
                continue
            # Dequeue bookkeeping (the heap pop already happened).
            remaining = self._queued.get(w.key_id, 0) - 1
            if remaining > 0:
                self._queued[w.key_id] = remaining
            else:
                self._queued.pop(w.key_id, None)
            metrics.CONCURRENCY_QUEUED.labels(key=w.key_label).dec()
            w.future.set_result(slot)
        for w in skipped:
            heapq.heappush(self._waiters, w)

    # -- public API ----------------------------------------------------------

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
    ) -> _Slot | None:
        """Admit this request now, or queue it for up to
        ``wait_seconds``. Returns the slot, or None when the wait
        expired (the caller answers 429 + Retry-After)."""
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
        slot = self._admit(waiter)
        if slot is not None:
            return slot
        self._enqueue(waiter)
        try:
            done, _pending = await asyncio.wait(
                {waiter.future}, timeout=max(0.0, wait_seconds)
            )
        except BaseException:
            # The waiting task itself was cancelled (client gone while
            # queued): leave the queue cleanly, hand back any slot the
            # drain may have granted in the same tick, re-raise.
            self._dequeue(waiter)
            if waiter.future.done():
                self.release(waiter.future.result())
            raise
        if done:
            return waiter.future.result()
        self._dequeue(waiter)
        if waiter.future.done():
            # Resolved in the same tick the timeout fired: a slot was
            # granted but nobody is waiting for it — give it back (the
            # drain will pass it to the next waiter, or the count just
            # frees).
            self.release(waiter.future.result())
        metrics.CONCURRENCY_REJECTED.labels(key=waiter.key_label).inc()
        return None

    def release(self, slot: _Slot) -> None:
        """Give a slot back and admit whoever fits. Double release is a
        no-op (the pipeline's release is idempotent via the ctx slot;
        this guard keeps the counters honest even if a refactor slips)."""
        if slot.released:
            return
        slot.released = True
        count = self._inflight.get(slot.key_id, 0) - 1
        if count > 0:
            self._inflight[slot.key_id] = count
        else:
            self._inflight.pop(slot.key_id, None)
        if slot.tenant_id is not None:
            tcount = self._tenant_inflight.get(slot.tenant_id, 0) - 1
            if tcount > 0:
                self._tenant_inflight[slot.tenant_id] = tcount
            else:
                self._tenant_inflight.pop(slot.tenant_id, None)
        metrics.CONCURRENCY_INFLIGHT.labels(key=slot.key_label).dec()
        self._drain()

    async def snapshot(self) -> dict:
        """Admin view: every key/tenant with slots held or waiters
        queued right now, plus totals. In-process state — a
        multi-replica fleet reports its own share here. (Async so the
        Redis backend's fleet read carries the same signature — the
        same trade the breaker registries' ``snapshots`` made.)"""
        keys: dict[str, dict] = {}
        for key_id, inflight in self._inflight.items():
            keys[key_id] = {"inflight": inflight, "queued": 0}
        for key_id, queued in self._queued.items():
            keys.setdefault(key_id, {"inflight": 0, "queued": 0})["queued"] = queued
        return {
            "keys": [
                {"key_id": key_id, **info}
                for key_id, info in sorted(keys.items())
            ],
            "tenants": [
                {"tenant_id": tenant_id, "inflight": inflight}
                for tenant_id, inflight in sorted(self._tenant_inflight.items())
            ],
            "queued_total": sum(self._queued.values()),
        }
