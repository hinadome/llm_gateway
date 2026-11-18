"""Lifecycle events: a publish/subscribe layer over the request stages.

The gateway's request path is explicitly staged (auth → policy →
guardrails → cache → routing → provider → guardrails → response →
accounting). This module makes those stage boundaries *subscribable*:
the pipeline (and the surfaces around it — auth, admin actions, budget
alerts, batches) emit named events, and configured handlers run at
them.

Two handler kinds, one contract::

    kind: observer      fire-and-forget. The event is enqueued and
                        delivered by a background dispatcher — a slow or
                        crashing handler can never add latency to (or
                        fail) a request. This is the default.
    kind: interceptor   in-band. Awaited inline at the emission point,
                        on the request path — its latency IS request
                        latency (that is the trade). It may raise
                        ``GatewayError`` to abort the request, which
                        flows through the normal error path (log row,
                        metrics, span, client envelope). Any other
                        exception is swallowed, logged and counted —
                        a buggy optional handler never 500s traffic.

Interceptors may only subscribe (explicitly, by name — no patterns) to
events emitted *before* the request becomes irreversible, and may not
modify content: mutation of requests/responses is guardrails' job, and
keeping one source of truth for content avoids two systems fighting
over the same payload. Interception is inspect + veto.

With no handlers configured, ``emit`` is a dict lookup that finds
nothing and returns — the bus is inert (that is the design goal: enable
the machinery, pay nothing until you subscribe).

Event catalog (payload highlights in brackets):

===============================  =========================================
Event                            Fired / payload
===============================  =========================================
``request.received``             after auth+parse, before policy checks
                                 [model, stream, key_name] — *interceptable*
``request.rejected``             blocked (4xx: policy, guardrail, rate
                                 limit, budget, interceptor veto)
                                 [error, status_code]
``request.failed``               errored (5xx / upstream exhaustion)
                                 [error, status_code]
``request.cache_hit``            exact or semantic cache replay [kind]
``request.routed``               deployment selected [deployment,
                                 provider, native_model, stream]
``response.completed``           final non-streaming response,
                                 post-guardrails, pre-client [response,
                                 usage, cost_usd] — *interceptable*
``stream.started``               first chunk of a streamed body [model]
``stream.completed``             streamed body finished (any outcome —
                                 ctx.status says which) [status]
``auth.rejected``                credential rejected at the edge [path,
                                 reason]
``key.created`` / ``key.updated`` / ``key.revoked``
                                 admin actions (the audit choke point —
                                 same names as the audit webhook) [details]
``tenant.*``, ``breaker.reset``  as above
``budget.threshold``             budget alert fired [key, level, spend]
``feedback.recorded``            client rated a request [request_id,
                                 model, rating]
``batch.created`` / ``batch.cancelled`` / ``batch.completed`` /
``batch.failed`` / ``batch.expired`` / ``batch.deferred``
                                 batch lifecycle [batch_id, ...]
===============================  =========================================

Handlers receive a :class:`GatewayEvent`: the payload dict is an
emission-time snapshot; ``ctx`` (when present) is the *live*
:class:`~gateway.core.context.RequestContext` — by the time a queued
observer is delivered it may have advanced (status, usage, cost filled
in), which is a feature: one object, final state.
"""

from __future__ import annotations

import asyncio
import fnmatch
import importlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..config import EventsConfig, EventHandlerConfig
from ..errors import GatewayError
from ..observability.metrics import EVENTS

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .context import RequestContext

logger = logging.getLogger("gateway.events")

# Events an interceptor may subscribe to: emitted before anything
# irreversible (client bytes sent, provider call placed in flight
# inside retry machinery) and carrying enough context to veto.
INTERCEPTABLE_EVENTS = frozenset({"request.received", "response.completed"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class GatewayEvent:
    """One emitted lifecycle event.

    ``payload`` is an emission-time snapshot (strings, numbers, plain
    dicts/lists — safe to hold); ``ctx`` is the live request context and
    may advance past the emission point by delivery time.
    """

    name: str
    ts: datetime = field(default_factory=_now)
    ctx: RequestContext | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class EventHandler:
    """Base class for event handlers. Override ``on_event``; set ``name``
    (metrics and logs identify the handler by it). ``params`` comes from
    ``events.handlers[].params`` in config.yaml."""

    name: str = "handler"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = params or {}

    async def on_event(self, event: GatewayEvent) -> None:
        raise NotImplementedError


def _load_class(class_path: str) -> type[EventHandler]:
    """Load a handler class from a dotted 'pkg.module:ClassName' path
    (the same convention as custom guardrails)."""
    module_path, _, class_name = class_path.partition(":")
    if not module_path or not class_name:
        raise ValueError(
            f"Invalid event handler class path '{class_path}' "
            "(expected 'pkg.module:ClassName')"
        )
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    if not (isinstance(cls, type) and issubclass(cls, EventHandler)):
        raise TypeError(f"{class_path} is not an EventHandler subclass")
    return cls


class _Stop:
    __slots__ = ()


class _Flush:
    """Drain marker: the dispatcher sets ``done`` after everything
    enqueued before it has been delivered."""

    __slots__ = ("done",)

    def __init__(self) -> None:
        self.done = asyncio.Event()


class EventBus:
    """Registry + dispatcher for lifecycle-event handlers.

    Construct via :meth:`from_config` (or ``register`` by hand in
    code/tests). ``emit`` is a no-op until someone subscribes.
    """

    def __init__(self, config: EventsConfig) -> None:
        self.config = config
        # (handler, compiled patterns | None, kind) — None = all events.
        self._handlers: list[tuple[EventHandler, list[str] | None, str]] = []
        # name -> matched ((handler, kind), ...) — built lazily, cleared
        # on any registration change.
        self._match_cache: dict[str, tuple[tuple[EventHandler, str], ...]] = {}
        self._queue: asyncio.Queue = asyncio.Queue(
            maxsize=max(1, config.queue_size)
        )
        self._task: asyncio.Task | None = None

    # -- registration ------------------------------------------------------

    def register(
        self,
        handler: EventHandler,
        events: list[str] | None = None,
        kind: str = "observer",
    ) -> None:
        """Register a handler (programmatic path — config is the other
        one). ``events`` is a list of names or shell patterns
        (``"request.*"``); None subscribes to everything. ``kind`` is
        ``"observer"`` (default) or ``"interceptor"``."""
        if kind not in ("observer", "interceptor"):
            raise ValueError(f"Unknown handler kind '{kind}'")
        if kind == "interceptor":
            if not events:
                raise ValueError(
                    f"Interceptor handler '{handler.name}' must list its "
                    f"events explicitly (one of: "
                    f"{', '.join(sorted(INTERCEPTABLE_EVENTS))})"
                )
            unknown = [e for e in events if e not in INTERCEPTABLE_EVENTS]
            if unknown:
                raise ValueError(
                    f"Interceptor handler '{handler.name}' subscribes to "
                    f"non-interceptable event(s): {', '.join(unknown)}. "
                    "Interceptors run in-band only at "
                    f"{', '.join(sorted(INTERCEPTABLE_EVENTS))}; use an "
                    "observer for everything else."
                )
        self._handlers.append((handler, list(events) if events else None, kind))
        self._match_cache.clear()

    @classmethod
    def from_config(cls, config: EventsConfig) -> "EventBus":
        bus = cls(config)
        if not config.enabled:
            return bus
        for cfg in config.handlers:
            handler_cls = _load_class(cfg.class_path)
            bus.register(
                handler_cls(cfg.params),
                events=cfg.events,
                kind=cfg.kind,
            )
        return bus

    def apply_config(self, config: EventsConfig) -> None:
        """Hot-reload: rebuild handlers from config (built first — a bad
        entry raises with the old registrations still live). The queue
        and dispatcher survive; ``queue_size`` applies at restart."""
        rebuilt = EventBus(config)
        if config.enabled:
            for cfg in config.handlers:
                handler_cls = _load_class(cfg.class_path)
                rebuilt.register(
                    handler_cls(cfg.params), events=cfg.events, kind=cfg.kind
                )
        self.config = rebuilt.config
        self._handlers = rebuilt._handlers
        self._match_cache.clear()
        logger.info(
            "events config reloaded: %d handler(s)",
            len(self._handlers),
        )

    @property
    def handlers(self) -> list[EventHandler]:
        """The registered handler objects (introspection / tests)."""
        return [h for h, _, _ in self._handlers]

    # -- matching ----------------------------------------------------------

    def _match(self, name: str) -> tuple[tuple[EventHandler, str], ...]:
        cached = self._match_cache.get(name)
        if cached is None:
            matched = tuple(
                (handler, kind)
                for handler, patterns, kind in self._handlers
                if patterns is None
                or any(fnmatch.fnmatchcase(name, p) for p in patterns)
            )
            self._match_cache[name] = matched
            return matched
        return cached

    # -- emission ----------------------------------------------------------

    async def emit(
        self, name: str, ctx: RequestContext | None = None, **payload: Any
    ) -> None:
        """Emit one event. Interceptors subscribed to ``name`` run
        inline (and may raise ``GatewayError`` to abort the request —
        after every observer has been enqueued, so subscribers still see
        the event that was vetoed). Observers are enqueued for the
        background dispatcher."""
        if not self.config.enabled:
            return
        matched = self._match(name)
        if not matched:
            return
        event = GatewayEvent(name=name, ctx=ctx, payload=payload)
        abort: GatewayError | None = None
        observers: list[EventHandler] = []
        for handler, kind in matched:
            if kind != "interceptor":
                observers.append(handler)
                continue
            try:
                await handler.on_event(event)
                EVENTS.labels(handler.name, name, "delivered").inc()
            except GatewayError as exc:
                # Deliberate veto — first one wins.
                EVENTS.labels(handler.name, name, "aborted").inc()
                abort = abort or exc
            except Exception:  # noqa: BLE001 — never fail traffic
                EVENTS.labels(handler.name, name, "error").inc()
                logger.exception(
                    "event handler '%s' failed on %s", handler.name, name
                )
        for handler in observers:
            self._enqueue(handler, event)
        if abort is not None:
            raise abort

    def emit_background(
        self, name: str, ctx: RequestContext | None = None, **payload: Any
    ) -> None:
        """Synchronous fire-and-forget for observer-only events, callable
        from sync code on the event loop (admin actions, alerts). No
        interceptor logic: admin-plane events are not interceptable."""
        if not self.config.enabled:
            return
        if not any(
            kind == "observer" for _, kind in self._match(name)
        ):
            return
        event = GatewayEvent(name=name, ctx=ctx, payload=payload)
        for handler, kind in self._match(name):
            if kind == "observer":
                self._enqueue(handler, event)

    def _enqueue(self, handler: EventHandler, event: GatewayEvent) -> None:
        # item = (single subscriber, event) — the dispatcher re-matches by
        # name so a handler registered between emit and delivery sees
        # consistent behaviour.
        try:
            self._ensure_dispatcher()
            self._queue.put_nowait((handler, event))
        except asyncio.QueueFull:
            EVENTS.labels(handler.name, event.name, "dropped").inc()
            logger.warning(
                "event queue full (size %d) — %s dropped for handler '%s'",
                self._queue.maxsize,
                event.name,
                handler.name,
            )
        except RuntimeError:  # pragma: no cover — no running loop
            # Sync context without a loop: the event waits in the queue
            # until the dispatcher starts (lifespan / first async emit).
            self._queue.put_nowait((handler, event))

    # -- dispatcher --------------------------------------------------------

    def _ensure_dispatcher(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._dispatch())

    async def _dispatch(self) -> None:
        while True:
            item = await self._queue.get()
            if isinstance(item, _Stop):
                return
            if isinstance(item, _Flush):
                item.done.set()
                continue
            handler, event = item
            try:
                await handler.on_event(event)
                EVENTS.labels(handler.name, event.name, "delivered").inc()
            except Exception:  # noqa: BLE001 — observers never fail requests
                EVENTS.labels(handler.name, event.name, "error").inc()
                logger.exception(
                    "event handler '%s' failed on %s", handler.name, event.name
                )

    async def start(self) -> None:
        """Start the background dispatcher (idempotent). Called by the
        lifespan; also started lazily on first enqueue."""
        if self._handlers:
            self._ensure_dispatcher()

    async def stop(self) -> None:
        """Stop the dispatcher after draining everything enqueued."""
        task = self._task
        if task is None:
            return
        await self._queue.put(_Stop())  # waits for space; drains ahead
        await task
        self._task = None

    async def drain(self) -> None:
        """Wait until everything enqueued before now has been delivered
        (deterministic await for tests and operational scripts)."""
        if self._task is None or self._task.done():
            return
        flush = _Flush()
        await self._queue.put(flush)
        await flush.done.wait()
