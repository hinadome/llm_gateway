"""Runaway-agent protection: loop detection with an escalating cooldown.

An agent stuck in a loop — a retry storm, a tool-call loop that
regenerates forever, a temperature-zero do/while — sends the *same*
request over and over at a rate no legitimate caller produces.
RPM/TPM ceilings cannot see it: every request is individually legal.
What identifies the runaway is the **fingerprint** — the same
normalized request identity the response cache keys on
(``cache.cache_payload_for``) — repeated ``threshold`` times inside
the window.

On trip the *key* enters a cooldown jail (``check`` fails fast with
the remaining seconds); re-trips escalate, a quiet period decays the
offense count. All state is in-process (like the idempotency in-flight
map): a multi-replica fleet detects at per-replica rate — bounded,
just slower to trip. The clock is injectable so tests fast-forward
cooldowns.

Thresholds are passed per call (the pipeline reads them off the live
config), so a hot reload applies instantly while jail/loop state
survives — the same trade the router makes with breaker state.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable


class RunawayProtector:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # (key_id, fingerprint) -> deque of monotonic hit timestamps.
        self._loops: dict[tuple[str, str], deque[float]] = {}
        # key_id -> cooldown_until_monotonic (the active jail).
        self._jails: dict[str, float] = {}
        # Offense ledger — survives jail expiry so a still-looping
        # agent escalates across cooldowns; decayed by the quiet
        # period or cleared by release().
        self._offenses: dict[str, int] = {}
        # key_id -> monotonic time of the last trip (offense decay).
        self._last_trip: dict[str, float] = {}

    # -- jail -----------------------------------------------------------------

    async def check(self, key_id: str) -> tuple[bool, float]:
        """Is this key allowed to proceed? Returns (allowed,
        retry_after_seconds) — the jail's remaining time on refusal."""
        until = self._jails.get(key_id)
        if until is None:
            return True, 0.0
        remaining = until - self._clock()
        if remaining <= 0:
            # Expired: the jail lifts itself, but the offense ledger
            # persists until the quiet period (or a release) decays it.
            del self._jails[key_id]
            return True, 0.0
        return False, remaining

    # -- loop windows -----------------------------------------------------------

    async def observe(
        self,
        key_id: str,
        fingerprint: str,
        threshold: int,
        window_seconds: float,
    ) -> int | None:
        """Record one hit of this fingerprint and return the in-window
        count if it just reached ``threshold`` (the trip signal), else
        None. The tripping request itself is expected to be refused."""
        now = self._clock()
        loop_key = (key_id, fingerprint)
        hits = self._loops.get(loop_key)
        if hits is None:
            hits = deque()
            self._loops[loop_key] = hits
        hits.append(now)
        cutoff = now - window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()
        # Opportunistic housekeeping: drop emptied windows so a fleet
        # of one-shot callers cannot grow the dict forever.
        if not hits:
            del self._loops[loop_key]
            return None
        if len(hits) >= max(1, threshold):
            return len(hits)
        return None

    def _clear_loops(self, key_id: str) -> None:
        """Forget the key's loop windows on trip: the cooldown IS the
        punishment — after it lifts, a still-looping agent re-trips on
        fresh hits (escalating), while a human replaying the request
        once to investigate does not."""
        for loop_key in [k for k in self._loops if k[0] == key_id]:
            del self._loops[loop_key]

    # -- trip / release ---------------------------------------------------------

    async def trip(
        self,
        key_id: str,
        base_seconds: float,
        multiplier: float,
        max_seconds: float,
        reset_after_seconds: float,
    ) -> tuple[float, int]:
        """Jail the key. Returns (cooldown_seconds, offenses). Re-trips
        escalate geometrically; a quiet ``reset_after_seconds`` resets
        the count."""
        now = self._clock()
        last = self._last_trip.get(key_id)
        if last is not None and (now - last) <= reset_after_seconds:
            offenses = self._offenses.get(key_id, 0) + 1
        else:
            offenses = 1
        cooldown = min(
            max(0.0, base_seconds) * max(1.0, multiplier) ** (offenses - 1),
            max(0.0, max_seconds),
        )
        self._jails[key_id] = now + cooldown
        self._offenses[key_id] = offenses
        self._last_trip[key_id] = now
        self._clear_loops(key_id)
        return cooldown, offenses

    async def release(self, key_id: str) -> bool:
        """Clear the jail, offense history and loop windows for a key
        (the admin escape hatch). Returns whether there was anything
        to release."""
        had = (
            self._jails.pop(key_id, None) is not None
            or self._offenses.pop(key_id, None) is not None
        )
        self._last_trip.pop(key_id, None)
        self._clear_loops(key_id)
        return had

    # -- visibility --------------------------------------------------------------

    async def snapshot(self) -> list[dict]:
        """Jailed keys for the admin view: key id, remaining cooldown
        and offense count. Expired jails lift (same rule as check)."""
        now = self._clock()
        view: list[dict] = []
        for key_id, until in list(self._jails.items()):
            remaining = until - now
            if remaining <= 0:
                del self._jails[key_id]
                continue
            view.append(
                {
                    "key_id": key_id,
                    "retry_after_seconds": round(remaining, 1),
                    "offenses": self._offenses.get(key_id, 1),
                }
            )
        return view
