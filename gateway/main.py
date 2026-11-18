"""FastAPI application factory and entrypoint.

Run locally with:
    uv run uvicorn gateway.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis import asyncio as aioredis
from sqlalchemy import text

from . import __version__
from .api import admin as admin_api
from .api import anthropic_native
from .api import audio as audio_api
from .api import batches as batches_api
from .api import chat as chat_api
from .api import embeddings as embeddings_api
from .api import feedback as feedback_api
from .api import gemini_native
from .api import images as images_api
from .api import mcp as mcp_api
from .api import models as models_api
from .api import openai_responses as responses_api
from .api import realtime as realtime_api
from .bootstrap import ensure_admin_key, ensure_default_key
from .config import GatewayConfig, Settings, get_settings
from .core.alerts import BudgetAlertManager
from .core.auth import AuthChain
from .core.audit import AuditLogger
from .core.batch_worker import BatchWorker
from .core.canary import CanaryProber
from .core.idempotency import IdempotencyStore
from .core.pii_scan import PIIScanner
from .core.cache import ResponseCache
from .core.events import EventBus
from .core.cost import CostCalculator
from .core.logging_svc import LoggingService, QueuedLoggingService
from .core.mcp import MCPSessionRegistry
from .core.prompt_library import PromptLibrary
from .core.pipeline import GatewayPipeline
from .core.ratelimit import RateLimiter
from .core.runaway import RunawayProtector
from .core.trials import sweep_stale as sweep_stale_trials
from .core.concurrency import ConcurrencyLimiter
from .core.vault import VaultService
from .core.redis_backends import (
    RedisCircuitBreakerRegistry,
    RedisRateLimiter,
    RedisResponseCache,
    RedisVaultInvalidator,
)
from .core.router import Router, embed_fn_for
from .core.semantic_cache import SemanticCache
from .db import dispose_engine, get_sessionmaker, init_engine
from .errors import GatewayError, ModelNotFoundError, ProviderError
from .guardrails.manager import GuardrailManager
from .observability import metrics
from .observability.logs import (
    attach_otel_log_handler,
    configure_logging,
    configure_otel_logs,
)
from .observability.metrics_bridge import start_metrics_bridge
from .observability.tracing import build_resource, configure_tracing
from .observability import wire
from .providers import build_providers

logger = logging.getLogger("gateway")

_STARTED_AT = time.time()


async def reload_gateway(app: FastAPI, source: str = "admin") -> dict:
    """Re-read config.yaml and hot-swap the config-derived components.

    One code path for ``POST /admin/reload`` and SIGHUP. Validation and
    the swap happen inside ``pipeline.apply_config`` — on any error the
    old configuration is still live. Infra-bound settings (provider
    credentials, auth chain, cache/rate-limit backends, telemetry) are
    reported in ``restart_required`` rather than silently ignored.
    """
    settings: Settings = app.state.settings
    config_path = Path(settings.config_path)
    if not config_path.exists():
        # A missing file at *startup* falls back to defaults (first boot);
        # during reload it would silently wipe the running configuration
        # with an empty one — refuse instead.
        raise FileNotFoundError(
            f"config file not found: {config_path} "
            "(reload refuses to fall back to defaults)"
        )
    config = GatewayConfig.load(config_path)
    summary = app.state.pipeline.apply_config(config)
    app.state.config = config
    # Config-derived helpers rebuilt on the successful swap only (a
    # failed reload leaves the old scanner live with the old config).
    app.state.pii_scanner = PIIScanner(config.pii_scan)
    summary["source"] = source
    summary["config_path"] = settings.config_path
    return summary


def create_app(
    settings: Settings | None = None,
    config: GatewayConfig | None = None,
    http_client: httpx.AsyncClient | None = None,
    tracer_provider: trace.TracerProvider | None = None,
    redis_client: aioredis.Redis | None = None,
    alert_http_client: httpx.AsyncClient | None = None,
    semantic_embedder: Callable[[str], Awaitable[list[float]]] | None = None,
    jwks_http_client: httpx.AsyncClient | None = None,
    audit_http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    config = config or GatewayConfig.load(settings.config_path)

    # ---- OpenTelemetry ---------------------------------------------------
    # An explicitly injected provider wins (tests pass one with an in-memory
    # exporter). Otherwise tracing is opt-in via GATEWAY_OTEL_ENABLED=true,
    # which installs the global provider (OTLP exporter when an endpoint is
    # configured, console otherwise). When neither is set the OTel API is a
    # no-op: spans are invalid and cost ~nothing.
    owns_tracing = False
    if tracer_provider is None and settings.otel_enabled:
        tracer_provider = configure_tracing()
        owns_tracing = True
    tracer = (
        tracer_provider.get_tracer("gateway")
        if tracer_provider is not None
        else trace.get_tracer("gateway")
    )

    # Structured logging: JSON stdout records when GATEWAY_LOG_FORMAT=json,
    # plus (when OTel is on and an OTLP endpoint is set) an OTel logs pipeline
    # whose records carry the active span's trace context.
    configure_logging(settings)
    logger_provider = None
    otel_log_handler = None
    meter_provider = None
    if owns_tracing:
        resource = build_resource()
        logger_provider = configure_otel_logs(resource)
        if logger_provider is not None:
            otel_log_handler = attach_otel_log_handler(logger_provider)
        # Mirror the Prometheus series into the OTLP metrics pipeline (the
        # same series remain available by scraping /metrics).
        meter_provider = start_metrics_bridge(resource)

    client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(300.0))
    # Wire capture (request-log rows): hooks on the shared provider client
    # snapshot what goes to / comes back from the provider. Attached to
    # whichever client ends up in use (tests inject their own transport) —
    # idempotent, marked so a second create_app with the same client does
    # not double them. The allowlist/body cap are bound here: rotating
    # them takes a restart (like provider credentials — the adapters hold
    # this client across config reloads).
    if not any(
        getattr(h, "_gateway_wire", False) for h in client.event_hooks["request"]
    ):
        upstream_allowlist = tuple(
            config.logging.upstream_header_allowlist
            or wire.DEFAULT_UPSTREAM_HEADER_ALLOWLIST
        )
        max_body_chars = config.logging.max_body_chars

        async def _wire_request_hook(request: httpx.Request) -> None:
            wire.capture_upstream_request(request, max_body_chars)

        async def _wire_response_hook(response: httpx.Response) -> None:
            wire.capture_upstream_response(response, upstream_allowlist)

        _wire_request_hook._gateway_wire = True  # type: ignore[attr-defined]
        _wire_response_hook._gateway_wire = True  # type: ignore[attr-defined]
        client.event_hooks["request"].append(_wire_request_hook)
        client.event_hooks["response"].append(_wire_response_hook)
    providers = build_providers(config, client)

    # ---- Shared-state backends (cache, rate limiter, circuit breakers) ----
    # In-memory by default (per-process); Redis backends share cache entries,
    # rate-limit windows and circuit-breaker state across workers/replicas.
    # An explicitly injected client wins (tests pass a fakeredis instance).
    redis_client = redis_client
    if redis_client is None and (
        config.cache.backend == "redis"
        or config.rate_limit.backend == "redis"
        or config.routing.breaker.backend == "redis"
        or config.idempotency.backend == "redis"
        or config.vault.backend == "redis"
    ):
        redis_client = aioredis.from_url(config.redis.resolve_url())
    breakers = None
    if config.routing.breaker.backend == "redis":
        breakers = RedisCircuitBreakerRegistry(
            redis_client, config.routing, prefix=config.redis.prefix
        )
    # BYOK vault (tier 31): enabled iff GATEWAY_VAULT_MASTER_KEY is set
    # and the encryption package is importable — otherwise it sits
    # disabled (provider-key endpoints 400 honestly; callers route with
    # the platform's configured keys, exactly as before).
    # vault.backend: redis (tier 34): admin writes also publish an
    # invalidation so every replica drops its cached entry (and its
    # negative twin) immediately — the TTL remains the backstop.
    vault_invalidator: RedisVaultInvalidator | None = None
    if config.vault.backend == "redis" and redis_client is not None:
        vault_invalidator = RedisVaultInvalidator(
            redis_client,
            channel=f"{config.redis.prefix}:vault-invalidation",
        )
    vault = VaultService(
        settings.vault_master_key,
        ttl_seconds=config.vault.ttl_seconds,
        on_invalidation=(
            vault_invalidator.publish if vault_invalidator is not None else None
        ),
    )
    if vault_invalidator is not None:
        vault_invalidator.bind(vault)
    router = Router(config, providers, breakers=breakers, vault=vault)
    guardrails = GuardrailManager.from_config(config.guardrails, tracer=tracer)
    # Lifecycle events: the stage-boundary pub/sub layer (see
    # core/events.py). Inert until handlers are configured — emission
    # is a no-op lookup with an empty registry.
    events_bus = EventBus.from_config(config.events)

    if config.cache.backend == "redis":
        cache = RedisResponseCache(
            redis_client,
            enabled=config.cache.enabled,
            ttl_seconds=config.cache.ttl_seconds,
            max_entries=config.cache.max_entries,
            prefix=config.redis.prefix,
        )
    else:
        cache = ResponseCache(**config.cache.model_dump(exclude={"backend"}))
    # Idempotency-Key replay store: the same backend selection as the
    # response cache (see core/idempotency.py). A distinct prefix
    # namespaces entries + LRU index away from content-cache keys, so
    # the two TTLs/LRU sizes never fight over one namespace.
    if config.idempotency.backend == "redis":
        idempotency_backend = RedisResponseCache(
            redis_client,
            enabled=config.idempotency.enabled,
            ttl_seconds=config.idempotency.ttl_seconds,
            max_entries=config.idempotency.max_entries,
            prefix=f"{config.redis.prefix}:idem",
        )
    else:
        idempotency_backend = ResponseCache(
            **config.idempotency.model_dump(
                exclude={"backend", "inflight_wait_seconds"}
            )
        )
    idempotency = IdempotencyStore(
        idempotency_backend,
        inflight_wait_seconds=config.idempotency.inflight_wait_seconds,
    )
    if config.rate_limit.backend == "redis":
        rate_limiter = RedisRateLimiter(redis_client, prefix=config.redis.prefix)
    else:
        rate_limiter = RateLimiter()
    # Runaway-agent protection: loop detection + escalating cooldown
    # jail. Always constructed — the enabled/threshold knobs are read
    # off the live config per call (hot reload applies; jail state
    # survives), the same trade the router makes with breaker state.
    # Detection state is in-process (like the idempotency in-flight
    # map): a multi-replica fleet detects at per-replica rate.
    runaway = RunawayProtector()
    # Concurrency admission control: per-key/tenant in-flight caps with
    # priority queueing. Always constructed — enabled/wait_seconds are
    # read off the live config per call (hot reload applies, held
    # slots survive), and the caps live on the key/tenant rows anyway.
    # In-process state, like the runaway protector: per-replica shares.
    concurrency = ConcurrencyLimiter()

    cost_calculator = CostCalculator(config.pricing)
    # Budget alerts: own (non-instrumented) HTTP client; disabled unless a
    # webhook URL resolves. An explicitly injected client wins (tests).
    alerts = BudgetAlertManager(config.alerts, client=alert_http_client, events=events_bus)
    # Audit webhooks: same delivery story as alerts, for admin actions.
    audit = AuditLogger(config.audit, client=audit_http_client, events=events_bus)
    # Request logging: synchronous by default (a returned response
    # implies its log row is queryable). logging.background_write moves
    # the row INSERT behind a bounded queue — the response path stops
    # paying the last DB round trip, at the cost of a (milliseconds)
    # delay before the row appears and drops when the queue is full.
    logging_service: LoggingService
    if config.logging.background_write:
        logging_service = QueuedLoggingService(
            config.logging.max_body_chars,
            alerts=alerts,
            store_bodies=config.logging.store_bodies,
            retention_days=config.logging.retention_days,
            queue_size=config.logging.queue_size,
        )
    else:
        logging_service = LoggingService(
            config.logging.max_body_chars,
            alerts=alerts,
            store_bodies=config.logging.store_bodies,
            retention_days=config.logging.retention_days,
        )
    # Semantic cache: optional similarity layer over the exact cache. An
    # injected embedder wins (tests use a deterministic fake) and is not
    # tied to the routing table. Otherwise the embedder routes through
    # the normal embeddings machinery — and a configured model with no
    # deployment disables the layer with one warning instead of failing
    # every request at lookup time.
    semantic = None
    if config.semantic_cache.enabled:
        if semantic_embedder is not None:
            semantic = SemanticCache(config.semantic_cache, semantic_embedder)
        else:
            try:
                router.resolve(config.semantic_cache.model)
            except (ModelNotFoundError, ProviderError):
                logger.warning(
                    "semantic_cache model '%s' has no serving deployment; semantic cache disabled",
                    config.semantic_cache.model,
                )
            else:
                semantic = SemanticCache(
                    config.semantic_cache,
                    embed_fn_for(router, config.semantic_cache.model),
                )
    # Model aliases / default routing: validate targets up front — an
    # alias pointing at nothing would 404 at request time with a message
    # about the *target*, which is confusing to debug. Warn, don't fail:
    # the rest of the gateway stays usable (same policy as semantic cache).
    configured = {m.name for m in config.models}
    for alias, target in config.model_aliases.items():
        if target not in configured:
            logger.warning(
                "model alias '%s' points at unknown model '%s'; requests for it will 404",
                alias,
                target,
            )
    if config.default_model and config.default_model not in configured:
        logger.warning(
            "default_model '%s' is not a configured model; unknown names will 404",
            config.default_model,
        )
    pipeline = GatewayPipeline(
        config,
        guardrails,
        router,
        cache,
        rate_limiter,
        cost_calculator,
        logging_service,
        tracer=tracer,
        semantic=semantic,
        prompt_library=PromptLibrary(
            cache_seconds=config.prompts.library_cache_seconds
        ),
        events=events_bus,
        idempotency=idempotency,
        runaway=runaway,
        concurrency=concurrency,
        vault=vault,
        # The MCP proxy's transport: the app's shared HTTP client (the
        # same one every outbound webhook/client uses; chat traffic
        # still goes through the router's adapters).
        http_client=client,
    )
    # Batch API worker: drives /v1/batches lines in waves (app.state so
    # tests can tick it manually; the lifespan loop is the only driver
    # in production).
    batch_worker = BatchWorker(pipeline, config.batch)
    # Canary prober: synthetic deployment health checks. Built here,
    # attached to app.state after the app exists (tests and
    # POST /admin/canaries/probe drive waves directly; the lifespan
    # loop below is the only *scheduled* driver).
    canary_prober = CanaryProber(router, config.canary, events=events_bus)
    # PII/secret scanner: pure computation over stored log bodies —
    # rebuilt on config swap (detectors depend on pii_scan settings).
    pii_scanner = PIIScanner(config.pii_scan)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await init_engine(settings.database_url)
        app.state.admin_key = await ensure_admin_key(settings)
        await ensure_default_key()
        # Candidate trials (tier 36): a run whose process died stays
        # "running" forever otherwise. The sweep cancels orphans —
        # unconditionally, because a trial created while trials were
        # enabled can outlive a config reload that disables them.
        # Cross-replica honest: a run LIVE on another replica sees the
        # cancel at its next between-wave check and stops within one
        # wave. Under pytest (ASGITransport skips the lifespan) the
        # sweep never runs — tests call it directly.
        try:
            swept = await sweep_stale_trials(get_sessionmaker())
            if swept:
                logger.info("candidate trials: cancelled %d orphaned run(s)", swept)
        except Exception:  # noqa: BLE001 — startup must not die on this
            logger.exception("candidate-trial startup sweep failed")
        # Log retention: prune once at startup, then periodically. Best
        # effort — a failed prune logs and retries on the next interval.
        prune_task: asyncio.Task | None = None
        if config.logging.retention_days:

            async def _retention_loop() -> None:
                while True:
                    try:
                        removed = await logging_service.prune()
                        if removed:
                            logger.info(
                                "log retention: pruned %d request log rows", removed
                            )
                    except Exception:  # noqa: BLE001
                        logger.exception("log retention prune failed")
                    await asyncio.sleep(config.logging.retention_interval_seconds)

            prune_task = asyncio.create_task(_retention_loop())

        # Batch worker: one wave of the oldest runnable batch per tick,
        # forever. Re-reads app.state.config each iteration so a hot
        # reload of batch settings (wave size, enabled) applies live.
        # Under pytest (ASGITransport skips the lifespan) this loop
        # never starts — tests call worker.tick() directly.
        batch_task: asyncio.Task | None = None
        if config.batch.enabled:

            async def _batch_loop() -> None:
                while True:
                    try:
                        current = app.state.config.batch
                        if current.enabled:
                            batch_worker.config = current
                            await batch_worker.tick()
                    except Exception:  # noqa: BLE001
                        logger.exception("batch worker tick failed")
                    await asyncio.sleep(app.state.config.batch.poll_interval_seconds)

            batch_task = asyncio.create_task(_batch_loop())

        # Canary probes: one wave of synthetic deployment health checks
        # per interval. Always started (not gated on canary.enabled)
        # so enabling via hot reload takes effect; when disabled the
        # loop idles at a short re-check instead of the probe interval.
        # Re-reads app.state.config.canary each iteration — enabled /
        # interval / timeout / allowlist apply from the next iteration.
        # Under pytest (ASGITransport skips the lifespan) this loop
        # never starts — tests call probe_once() or the admin endpoint.

        async def _canary_loop() -> None:
            while True:
                current = app.state.config.canary
                if current.enabled:
                    canary_prober.config = current
                    try:
                        await canary_prober.probe_once()
                    except Exception:  # noqa: BLE001
                        logger.exception("canary probe wave failed")
                await asyncio.sleep(
                    current.interval_seconds if current.enabled else 30.0
                )

        canary_task = asyncio.create_task(_canary_loop())

        # MCP session TTL: evict gateway-issued sessions idle past
        # mcp.session_ttl_seconds. The registry is the gateway's OWN
        # state (survives hot-reloads — a client mid-conversation is
        # never stranded by a config swap), while the TTL is read live
        # each pass so a reload applies from the next sweep. Evicted
        # sessions get the closed event and a best-effort upstream
        # DELETE (server removed from the config: the upstream session
        # is left to the server's own TTL). Under pytest
        # (ASGITransport skips the lifespan) this loop never starts —
        # tests call registry.sweep() directly.
        async def _mcp_sweep_loop() -> None:
            while True:
                try:
                    mcp_config = app.state.config.mcp
                    for session in app.state.mcp_registry.sweep(
                        mcp_config.session_ttl_seconds
                    ):
                        await pipeline._mcp_sweep_one(session, mcp_config)
                except Exception:  # noqa: BLE001
                    logger.exception("mcp session sweep failed")
                await asyncio.sleep(60.0)

        mcp_sweep_task = asyncio.create_task(_mcp_sweep_loop())

        # BYOK vault invalidation listener (tier 34): the subscriber
        # half of vault.backend: redis — one loop per process, feeds
        # published (tenant, provider) invalidations into the local
        # cache drop. Best-effort by design (reconnects with backoff
        # on Redis outages; the TTL is the backstop), cancelled at
        # shutdown before the client closes. Under pytest
        # (ASGITransport skips the lifespan) this loop never starts —
        # tests drive listen() directly.
        vault_invalidation_task: asyncio.Task | None = None
        if vault_invalidator is not None:
            vault_invalidation_task = asyncio.create_task(
                vault_invalidator.listen()
            )

        logger.info("LLM Gateway %s ready (models: %s)", __version__, config.public_models())

        # SIGHUP = reload config.yaml without dropping traffic (the same
        # code path as POST /admin/reload). Best effort on platforms /
        # runners that deliver it; uvicorn's own signals (SIGTERM/SIGINT)
        # are untouched.
        async def _reload_on_sighup() -> None:
            logger.info("SIGHUP received — reloading configuration")
            try:
                await reload_gateway(app, source="sighup")
            except Exception:  # noqa: BLE001
                logger.exception("SIGHUP reload failed (old config still live)")

        def _sighup_handler() -> None:
            asyncio.get_running_loop().create_task(_reload_on_sighup())

        try:
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGHUP, _sighup_handler
            )
        except (NotImplementedError, AttributeError, ValueError):
            pass  # not every platform supports it (e.g. Windows)

        # Readiness gate: /readyz serves 200 only once startup finished
        # (engine up, admin key ensured), and flips back off the moment
        # shutdown begins so load balancers stop routing here first.
        app.state.ready = True
        # Event dispatcher (observer deliveries). Lazily started on
        # first emit too; starting here covers emit_background calls
        # that arrive before any request.
        await events_bus.start()
        yield
        app.state.ready = False  # draining: readiness fails before anything closes
        if prune_task is not None:
            prune_task.cancel()
        if batch_task is not None:
            batch_task.cancel()
        canary_task.cancel()
        mcp_sweep_task.cancel()
        if vault_invalidation_task is not None:
            vault_invalidation_task.cancel()
            with suppress(asyncio.CancelledError):
                await vault_invalidation_task
        # In-flight background work (spend accounting, shadow mirrors,
        # prompt A/B) is drained before the resources it depends on go
        # away — bounded, so a wedged upstream cannot hang shutdown.
        drained = await pipeline.drain_background_tasks(timeout=10.0)
        if drained["drained"]:
            logger.info(
                "shutdown: drained %d background task(s)", drained["drained"]
            )
        await client.aclose()
        await alerts.aclose()
        await audit.aclose()
        # Authenticators that own resources (JWKS HTTP client).
        for step in app.state.auth_chain.steps:
            step_aclose = getattr(step, "aclose", None)
            if step_aclose is not None:
                await step_aclose()
        if redis_client is not None:
            await redis_client.aclose()
        # Background log writes (logging.background_write): drain every
        # pending row before the engine goes away, so shutdown loses
        # nothing. Synchronous logging services have no stop().
        logging_stop = getattr(logging_service, "stop", None)
        if logging_stop is not None:
            await logging_stop()
        # Pending observer deliveries go the same way — stop() drains.
        await events_bus.stop()
        if owns_tracing:
            if meter_provider is not None:
                meter_provider.shutdown()
            if logger_provider is not None:
                if otel_log_handler is not None:
                    logging.getLogger("gateway").removeHandler(otel_log_handler)
                logger_provider.shutdown()
            if tracer_provider is not None:
                tracer_provider.shutdown()  # flush pending spans to the exporter
        await dispose_engine()

    app = FastAPI(
        title="LLM Gateway",
        version=__version__,
        description="OpenAI-compatible LLM gateway with guardrails, virtual keys, routing, caching and observability.",
        lifespan=lifespan,
        docs_url="/docs",
    )

    # ---- app state -----------------------------------------------------
    app.state.pipeline = pipeline
    # MCP proxy sessions (gateway-issued Mcp-Session-Id -> upstream
    # session + negotiated protocol version). Created here — once per
    # app — so hot-reloads never strand a live client session; a
    # server removed from the config evicts lazily (next request 404s,
    # the client re-initializes) and the lifespan loop sweeps TTLs.
    app.state.mcp_registry = MCPSessionRegistry()
    # Lifecycle events (bus shared with audit/alerts/pipeline).
    app.state.events = events_bus
    # Batch worker (tick() driven by the lifespan loop; tests call it).
    app.state.batch_worker = batch_worker
    # Canary prober (probe_once() driven by the lifespan loop, the
    # probe-now endpoint, or tests directly).
    app.state.canary = canary_prober
    # PII/secret scanner (admin-plane GETs; rebuilt on config swap).
    app.state.pii_scanner = pii_scanner
    # Prompt library (admin-plane CRUD routes live in api/admin.py).
    app.state.prompt_library = pipeline.prompt_library
    # BYOK vault (admin-plane provider-key CRUD; the serving side lives
    # on the router/pipeline references).
    app.state.vault = vault
    # The Redis invalidation fan-out when vault.backend: redis (None
    # otherwise): tests spawn/drive listen() by hand — the lifespan
    # loop never starts under ASGITransport.
    app.state.vault_invalidator = vault_invalidator
    app.state.config = config
    # Env settings (DB URL, config path, ...): the reload path re-reads
    # config.yaml from settings.config_path; endpoints under /admin use
    # this rather than a stale closure over `settings`.
    app.state.settings = settings
    app.state.http_client = client
    app.state.alerts = alerts
    app.state.audit = audit
    # Authentication chain (auth.chain): admin key / virtual keys / JWT /
    # custom steps. Built after the app exists — the admin-key step reads
    # app.state.admin_key, which the lifespan seeds. The JWKS fetch uses a
    # dedicated non-instrumented client (like alert webhooks) so identity-
    # provider traffic stays out of request traces; an injected one wins
    # (tests).
    app.state.auth_chain = AuthChain.from_config(
        config.auth,
        admin_key_provider=lambda: getattr(app.state, "admin_key", None),
        jwks_http_client=(
            jwks_http_client
            if jwks_http_client is not None
            and "jwt" in config.auth.chain
            else None
        ),
    )

    # ---- middleware ------------------------------------------------------
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex}"
        # Wire capture for the request-log row: the allowlisted inbound
        # headers (allowlist read per request, so config reloads apply
        # live), and a clean slate for the upstream slots — a cache hit or
        # a failure before dispatch must record None, not whatever a
        # previous request left in this task's context.
        try:
            allowlist = request.app.state.config.logging.client_header_allowlist
        except AttributeError:
            allowlist = None
        wire.set_client_headers(request.headers, allowlist)
        wire.reset_upstream()
        response = await call_next(request)
        response.headers.setdefault("X-Request-Id", request.state.request_id)
        return response

    # CORS for browser-based clients (opt-in via settings). Added after the
    # request-id middleware so it ends up outermost: preflight OPTIONS
    # requests are then answered without touching the rest of the stack.
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                origin.strip()
                for origin in settings.cors_allow_origins.split(",")
                if origin.strip()
            ],
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=True,
            expose_headers=[
                "X-Request-Id",
                "X-Gateway-Model",
                "X-Gateway-Provider",
                "X-Gateway-Deployment",
                "X-Gateway-Cache",
                "X-Gateway-Byok-Provider",
                "X-Gateway-Cost-Usd",
                "X-Gateway-Latency-Ms",
            ],
            max_age=600,
        )

    # ---- exception handlers (OpenAI-style envelopes by default; the native
    # Anthropic/Gemini inbound endpoints get their own error format) ----
    def _error_envelope(
        request: Request,
        status_code: int,
        message: str,
        error_type: str,
        param=None,
        code=None,
        headers: dict[str, str] | None = None,
    ) -> JSONResponse:
        headers = headers or {}
        path = request.url.path
        if anthropic_native.is_native_path(path):
            return anthropic_native.error_response(status_code, message, headers)
        if gemini_native.is_native_path(path):
            return gemini_native.error_response(status_code, message, headers)
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "message": message,
                    "type": error_type,
                    "param": param,
                    "code": code,
                }
            },
            headers=headers,
        )

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError):
        return _error_envelope(
            request,
            exc.status_code,
            exc.message,
            exc.error_type,
            exc.param,
            exc.code,
            exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
        )
        return _error_envelope(
            request, 400, f"Invalid request: {problems}", "invalid_request_error"
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        logger.exception("unhandled error")
        return _error_envelope(
            request,
            500,
            "The gateway encountered an internal error.",
            "api_error",
            code="internal_error",
        )

    # ---- routes ----------------------------------------------------------
    app.include_router(chat_api.router)
    app.include_router(embeddings_api.router)
    app.include_router(models_api.router)
    app.include_router(anthropic_native.router)
    app.include_router(gemini_native.router)
    app.include_router(responses_api.router)
    app.include_router(images_api.router)
    app.include_router(audio_api.router)
    app.include_router(realtime_api.router)
    app.include_router(mcp_api.router)
    app.include_router(batches_api.router)
    app.include_router(feedback_api.router)
    app.include_router(admin_api.router)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": __version__,
            "uptime_seconds": int(time.time() - _STARTED_AT),
            "models": config.public_models(),
        }

    @app.get("/healthz")
    async def healthz():
        """Liveness: the process is up and serving. Never checks
        dependencies — a wedged DB must not get the pod killed *and*
        take the recovery action away (that is readiness' job)."""
        return {"status": "ok", "version": __version__,
                "uptime_seconds": int(time.time() - _STARTED_AT)}

    @app.get("/readyz")
    async def readyz():
        """Readiness: should this instance receive traffic?

        200 once startup completed (lifespan sets ``app.state.ready``)
        and the database answers. 503 while starting up, while draining
        on shutdown, or when the DB is unreachable — a load balancer
        should route elsewhere in all three cases. Circuit breakers are
        reported (informational: an open breaker is a per-deployment
        condition the router routes around, not an unhealthy gateway)."""
        started = getattr(app.state, "ready", False)
        db_ok, db_error = True, None
        try:
            sessionmaker = get_sessionmaker()
            async with sessionmaker() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 — any failure = not ready
            db_ok, db_error = False, str(exc)
        breakers_open = 0
        breakers_half_open = 0
        try:
            snapshots = await app.state.pipeline.router.breakers.snapshots()
            breakers_open = sum(
                1 for s in snapshots.values() if s.get("state") == "open"
            )
            breakers_half_open = sum(
                1 for s in snapshots.values() if s.get("state") == "half_open"
            )
        except Exception:  # noqa: BLE001 — informational only
            pass
        ready = started and db_ok
        payload = {
            "ready": ready,
            "draining": not started,
            "db": db_ok,
            "uptime_seconds": int(time.time() - _STARTED_AT),
            "circuit_breakers": {
                "open": breakers_open,
                "half_open": breakers_half_open,
            },
        }
        if db_error is not None:
            payload["db_error"] = db_error
        if not ready:
            return JSONResponse(payload, status_code=503)
        return payload

    @app.get("/metrics")
    async def prometheus_metrics():
        # Circuit gauges: OPEN → HALF_OPEN happens on the clock without a
        # transition event, so re-sync before every scrape (with the Redis
        # backend this also picks up other replicas' trips).
        await app.state.pipeline.router.breakers.refresh_gauges()
        return PlainTextResponse(
            generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST
        )

    # ---- OpenTelemetry instrumentation (opt-in) ---------------------------
    if tracer_provider is not None:
        # Server spans for every request (extracts W3C ``traceparent`` on the
        # way in) and client spans for every upstream call (injects it on the
        # way out) — end-to-end trace propagation through the gateway.
        FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)
        HTTPXClientInstrumentor.instrument_client(client, tracer_provider=tracer_provider)

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "gateway.main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
