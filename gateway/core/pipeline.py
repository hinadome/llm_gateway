"""GatewayPipeline: orchestrates the full life of a chat completion request
(and, via handle_embedding, an embeddings request — same pre-flight checks,
no guardrails, never streamed).

    client
      -> auth (virtual key)                      [api/deps.py]
      -> model permission check
      -> rate limit (RPM/TPM)
      -> budget check
      -> guardrails: request phase               (modify / block)
      -> cache lookup                            (exact match)
      -> router: deployment selection
          -> retries + fallbacks between deployments
          -> provider adapter (OpenAI <-> native translation)
      -> guardrails: response phase              (modify / block -> content_filter)
      -> guardrails: stream phase                 (block -> content_filter)
      -> usage accounting, cost, spend, logging, metrics
      -> client (OpenAI schema, streaming SSE or JSON)
"""

from __future__ import annotations

import hashlib
import json
import logging
import asyncio
import random
import time
import uuid
from contextlib import nullcontext, suppress
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode, use_span

from ..config import DeploymentConfig, GatewayConfig, MCPConfig
from ..errors import (
    BudgetExceededError,
    BudgetProjectionError,
    GatewayError,
    GuardrailBlocked,
    MCPDisabledError,
    MCPServerNotAllowedError,
    ModelNotAllowedError,
    ModelNotFoundError,
    ProviderError,
    RateLimitError,
    ToolsNotAllowedError,
)
from .prompt_library import CandidatePrompt, PromptLibrary, ResolvedPrompt
from ..guardrails.manager import GuardrailManager
from ..observability import metrics, wire
from ..observability.metrics import KeyLabeler
from .prompts import apply_prompt_policy, substitute
from ..schemas import (
    ChatCompletionRequest,
    EmbeddingRequest,
    ImageEditRequest,
    ImageGenerationRequest,
    ImageVariationRequest,
    Message,
    SpeechRequest,
    TranscriptionRequest,
)
from ..utils import content_to_text, daily_bucket, estimate_tokens, monthly_bucket
from .cache import ResponseCache, cache_payload_for, cache_key_for
from .idempotency import IdempotencyStore
from .semantic_cache import SemanticCache
from .context import AuthContext, RequestContext
from .shadow_diff import extract_content_text, score_pair
from .forecasts import run_rate_month_end
from .cost import CostCalculator, cached_tokens_from_usage
from .mcp import (
    CIRCUIT_OPEN,
    GUARDRAIL_BLOCKED,
    MCP_CONTENT_METHODS,
    MCPToolBlocked,
    MCPServerConfig,
    MCPSession,
    MCPSessionRegistry,
    SESSION_REQUIRED,
    SESSION_TERMINATED,
    TOOL_NOT_ALLOWED,
    SSEReader,
    classify,
    jsonrpc_error,
    parse_jsonrpc_message,
    sse_frame,
    upstream_headers,
)
from .breaker import CircuitBreakerRegistry
from .realtime import RealtimeRelay
from .runaway import RunawayProtector
from .concurrency import ConcurrencyLimiter
from .vault import VaultService
from .events import EventBus
from .logging_svc import LoggingService
from .router import Router

if TYPE_CHECKING:
    # Annotation-only (tier 62): the Redis limiter carries the same
    # acquire/release contract as the in-memory one, so the pipeline
    # stays backend-agnostic — and free of a runtime redis import.
    from .redis_backends import RedisConcurrencyLimiter

logger = logging.getLogger("gateway.pipeline")

STREAM_MEDIA_TYPE = "text/event-stream"


def _sse(obj: Any) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def embedding_cache_key_for(
    request: EmbeddingRequest,
    tenant_id: str | None = None,
    residencies: frozenset[str] | None = None,
) -> str:
    """Cache key for embeddings: model + input + dimensions (+ tenant).

    ``encoding_format`` is deliberately not part of the key — the cache
    stores the canonical float form and base64 is (re-)encoded per request.
    Embeddings are deterministic for the same model+input, which makes
    them ideal cache targets. No response guardrails run on embeddings,
    so the salt is the tenant alone: data isolation between tenants,
    sharing preserved for everyone else (platform keys, same-tenant
    keys — an embedding is an embedding).

    ``residencies`` (tier 28): the caller's effective data-residency
    requirement. An entry produced under one requirement may have been
    computed on a deployment another class may not use, so residency
    classes never share entries — the same rule the chat path's
    response-policy token applies. Only salted in when a requirement
    exists: unrestricted callers keep their exact pre-tier-28 keys and
    existing entries stay valid.
    """
    payload = {
        "kind": "embedding",
        "model": request.model,
        "input": [request.input] if isinstance(request.input, str) else request.input,
        "dimensions": request.dimensions,
        **({"tenant_id": tenant_id} if tenant_id else {}),
        **({"required_residencies": sorted(residencies)} if residencies else {}),
    }
    serialized = json.dumps(payload, sort_keys=True, default=repr)
    return hashlib.sha256(serialized.encode()).hexdigest()


def _encode_embeddings_base64(response: dict) -> dict:
    """Convert an OpenAI embeddings response to encoding_format=base64.

    Each vector is packed as little-endian float32 — the OpenAI wire
    format for base64 embeddings.
    """
    import base64
    import struct

    out = dict(response)
    out["data"] = [
        {
            **item,
            "embedding": base64.b64encode(
                struct.pack(f"<{len(item['embedding'])}f", *item["embedding"])
            ).decode("ascii"),
        }
        if isinstance(item.get("embedding"), list)
        else item
        for item in response.get("data", [])
    ]
    return out


def _image_edit_log_body(
    request: ImageEditRequest | ImageVariationRequest,
) -> dict:
    """Log body for the multipart image surfaces: metadata about the
    uploaded images, never the bytes themselves."""
    body: dict = {
        "model": request.model,
        "images": [
            {"file_name": name, "size_bytes": len(content)}
            for content, name, _ in request.images
        ],
        "n": request.n,
    }
    if request.size is not None:
        body["size"] = request.size
    if getattr(request, "prompt", None):
        body["prompt"] = request.prompt
    if getattr(request, "mask", None):
        body["mask"] = {"file_name": request.mask[1], "size_bytes": len(request.mask[0])}
    return body


class GatewayPipeline:
    def __init__(
        self,
        config: GatewayConfig,
        guardrails: GuardrailManager,
        router: Router,
        cache: ResponseCache,
        rate_limiter,
        cost_calculator: CostCalculator,
        logging_service: LoggingService,
        tracer: trace.Tracer | None = None,
        semantic: SemanticCache | None = None,
        prompt_library: PromptLibrary | None = None,
        events: EventBus | None = None,
        idempotency: IdempotencyStore | None = None,
        runaway: RunawayProtector | None = None,
        concurrency: ConcurrencyLimiter | RedisConcurrencyLimiter | None = None,
        vault: "VaultService | None" = None,
        # The app's shared HTTP client (create_app always passes one) —
        # the MCP proxy's only transport, so the pipeline for the first
        # time needs direct upstream reach (chat/embeddings/images/audio
        # go through the router's adapters, which hold their own).
        http_client: httpx.AsyncClient | None = None,
        # Per-MCP-server circuit breakers (tier 66). None = built here
        # from ``mcp.breaker`` (in-process); create_app passes the
        # Redis-backed registry when ``mcp.breaker.backend: redis`` —
        # same interface, fleet-shared trips (router's pattern).
        mcp_breakers: CircuitBreakerRegistry | None = None,
    ) -> None:
        self.config = config
        # Per-key metric series (metrics.per_key — opt-in; the labeler's
        # cap keeps key-name cardinality bounded).
        self._key_labeler = KeyLabeler(config.metrics.per_key_max_series)
        self.guardrails = guardrails
        self.router = router
        self.cache = cache
        self.rate_limiter = rate_limiter
        self.cost_calculator = cost_calculator
        self.logging_service = logging_service
        # Configured public model names (for alias resolution: a real name
        # always wins over an alias of the same name).
        self._model_names = {m.name for m in config.models}
        # Declared context windows per public model (ModelConfig.
        # context_window) — stashed on the ctx at model resolution for
        # the context_window guardrail. Rebuilt on apply_config.
        self._model_windows = {m.name: m.context_window for m in config.models}
        # Optional semantic (similarity) cache layer; None = disabled.
        self.semantic = semantic
        # Versioned prompt library (prompt ops): resolves key
        # system_prompt_ref values and drives prompt A/B mirroring.
        self.prompt_library = prompt_library
        # Lifecycle events (see core/events.py): None = this pipeline
        # emits nothing (standalone constructions); create_app always
        # passes the app-wide bus, where no handlers configured means
        # emit() is an inert lookup.
        self.events = events
        # Idempotency-Key replay store (None = the header is ignored
        # entirely). Wraps a response-cache backend with caller-keyed
        # entries + in-flight dedup — see core/idempotency.py.
        self.idempotency = idempotency
        # Runaway-agent protection (None = disabled — standalone
        # constructions): loop detection + escalating cooldown jail —
        # see core/runaway.py.
        self.runaway = runaway
        # Concurrency admission control (None = disabled — standalone
        # constructions): per-key/tenant in-flight caps with priority
        # queueing — see core/concurrency.py. Either backend carries
        # the same acquire/release contract; the Redis one
        # (RedisConcurrencyLimiter) counts slots fleet-shared via
        # expiring leases instead of per-process.
        self.concurrency = concurrency
        # BYOK vault (tier 31). None = standalone constructions get a
        # DISABLED service (lookups no-op, platform credentials); the
        # real one is built in create_app from GATEWAY_VAULT_MASTER_KEY.
        # Serving paths consult it for the tenant's stored provider key
        # (inside the router's _resolve_provider_cfg); here it is only
        # peeked — after dispatch — to mark the ctx when the call rode
        # the tenant's credential (log row, header, metric).
        self.vault = vault or VaultService(None)
        # The MCP proxy's upstream transport (the app's shared client;
        # None = standalone constructions, where the MCP surface refuses
        # with a clear 503 rather than pretending to work).
        self._http_client = http_client
        # Per-server MCP circuit breakers (tier 66): the same registry
        # class the router runs per deployment, keyed by SERVER name
        # with provider "mcp" (the gauge family is shared — one
        # dashboard panel for every circuit, the provider label keeps
        # the series apart). An injected registry (Redis) wins, exactly
        # like the router's. Registering on construction creates the
        # gauge series from boot; apply_config re-registers so servers
        # added by a reload get breakers (existing ones keep their
        # state — a reload is not a health event).
        self.mcp_breakers = mcp_breakers or CircuitBreakerRegistry(
            config.mcp.breaker
        )
        for _server in config.mcp.servers:
            self.mcp_breakers.register(_server.name, "mcp")
        # OTel API is a no-op until a provider is installed (spans are then
        # invalid and cost ~nothing), so this is safe unconditionally.
        self._tracer = tracer or trace.get_tracer("gateway")
        # Tracked fire-and-forget work (spend accounting, shadow mirrors,
        # prompt A/B) — drained on shutdown before the engine goes away.
        self._background_tasks: set = set()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def _emit(
        self, name: str, ctx: RequestContext | None = None, **payload
    ) -> None:
        """Emit a lifecycle event (no-op without a bus — standalone
        pipelines; create_app always wires one). Interceptor aborts
        (GatewayError) propagate to the caller's error handling."""
        if self.events is not None:
            await self.events.emit(name, ctx, **payload)

    async def handle_chat(
        self,
        request: ChatCompletionRequest,
        auth: AuthContext,
        request_id: str | None = None,
        route: str | None = None,
        idempotency_key: str | None = None,
    ) -> Response:
        ctx = RequestContext(
            request_id=request_id or f"req_{uuid.uuid4().hex}",
            auth=auth,
            model=request.model,
            stream=request.stream,
            route=route,
            request_body=request.model_dump(exclude_none=True),
        )
        metrics.INFLIGHT.inc()
        # One span per request. For streaming it stays open until the
        # response body has been fully sent (ended in the stream wrapper).
        span = self._tracer.start_span("gateway.chat", kind=SpanKind.INTERNAL)
        ctx.otel_span = span
        span_context = span.get_span_context()
        if span_context.is_valid:  # invalid = tracing disabled (no-op span)
            ctx.trace_id = format(span_context.trace_id, "032x")
            ctx.span_id = format(span_context.span_id, "016x")
        span.set_attribute("gateway.request_id", ctx.request_id)
        span.set_attribute("gateway.model", request.model)
        span.set_attribute("gateway.stream", request.stream)
        span.set_attribute("gateway.key_name", ctx.auth.key_name or "admin")
        if route is not None:
            span.set_attribute("gateway.route", route)
        # Everything below runs with the span current — logs emitted here
        # (incl. the per-request record in _log) carry its trace context.
        with use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                response = await self._handle(request, ctx, idempotency_key)
            except GatewayError as exc:
                ctx.status = "blocked" if isinstance(exc, GuardrailBlocked) else "error"
                ctx.error_message = exc.message
                # A failed execution is never stored, and its waiters
                # must not sit out their in-flight timeout — release
                # them to re-execute (the retry semantics for errors).
                if ctx.idempotency_key is not None and self.idempotency is not None:
                    self.idempotency.finish(ctx.idempotency_key)
                self._request_metric(ctx)
                await self._log(ctx)
                self._finish_span(ctx, exc)
                await self._emit(
                    "request.rejected"
                    if (isinstance(exc, GuardrailBlocked) or exc.status_code < 500)
                    else "request.failed",
                    ctx,
                    error=exc.message,
                    status_code=exc.status_code,
                )
                raise
            except Exception as exc:  # noqa: BLE001
                ctx.status = "error"
                ctx.error_message = f"internal error: {exc}"
                if ctx.idempotency_key is not None and self.idempotency is not None:
                    self.idempotency.finish(ctx.idempotency_key)
                self._request_metric(ctx)
                await self._log(ctx)
                self._finish_span(ctx, exc)
                await self._emit("request.failed", ctx, error=ctx.error_message)
                raise
            if isinstance(response, StreamingResponse):
                # The span and the in-flight gauge are finished by the stream
                # wrapper once the response body completes.
                return response
            self._finish_span(ctx)
            return response

    async def replay_request(
        self,
        request: ChatCompletionRequest,
        auth: AuthContext,
        route: str | None = None,
    ) -> dict:
        """Dry-run one captured request through the request-policy
        phases — the capture-replay harness's executor (see
        core/replay.py and POST /admin/replay).

        Runs the exact shared code a live request runs (``_prepare_
        request_policy`` then ``_apply_request_guardrails``) and
        nothing else: no upstream call, no rate-limit/budget/
        idempotency evaluation, no cache access, no spend, no
        request-log row, no lifecycle events, and (via ctx.dry_run)
        no guardrail-counter pollution. The caller is the *current*
        key policy — the harness re-resolved it fresh from the
        database, so edited or revoked keys replay under what they
        are now, which is the regression being hunted.

        Returns the predicted decision record: the final model (post
        resolution + redirects), whether the managed prompt policy
        applied, the guardrail verdicts, the predicted status
        (success / blocked / error, mirroring the statuses a live
        request logs) and, on success, the deployment ladder that
        would serve it (priority order — membership is the honest
        diff signal, since weighted tiers pick randomly).
        """
        ctx = RequestContext(
            request_id=f"replay_{uuid.uuid4().hex}",
            auth=auth,
            model=request.model,
            stream=request.stream,
            route=route,
            request_body=request.model_dump(exclude_none=True),
            dry_run=True,
        )
        try:
            request = await self._prepare_request_policy(request, ctx)
            request = await self._apply_request_guardrails(request, ctx)
        except GuardrailBlocked as exc:
            return {
                "model": ctx.model,
                "prompt_policy_applied": ctx.effective_request is not None,
                "guardrail_actions": list(ctx.guardrail_actions),
                "status": "blocked",
                "error": {"code": exc.code, "message": exc.message},
                "deployments": None,
            }
        except GatewayError as exc:
            return {
                "model": ctx.model,
                "prompt_policy_applied": ctx.effective_request is not None,
                "guardrail_actions": list(ctx.guardrail_actions),
                "status": "error",
                "error": {"code": exc.code, "message": exc.message},
                "deployments": None,
            }
        # Routing selection without dispatch: the priority ladder the
        # request would draw from (a 404/503 here is a prediction too —
        # an unknown model or all-shadow deployments).
        try:
            ladder = [
                d.name
                for d in self.router.resolve(
                    request.model, ctx.auth.effective_residencies()
                )
            ]
        except GatewayError as exc:
            return {
                "model": ctx.model,
                "prompt_policy_applied": ctx.effective_request is not None,
                "guardrail_actions": list(ctx.guardrail_actions),
                "status": "error",
                "error": {"code": exc.code, "message": exc.message},
                "deployments": None,
            }
        return {
            "model": ctx.model,
            "prompt_policy_applied": ctx.effective_request is not None,
            "guardrail_actions": list(ctx.guardrail_actions),
            "status": "success",
            "error": None,
            "deployments": ladder,
        }

    async def replay_mcp_message(
        self,
        message: dict[str, Any],
        auth: AuthContext,
        server_name: str,
    ) -> dict:
        """Dry-run one captured MCP message through the request-policy
        phases — the capture-replay harness's MCP executor (the
        ``replay_request`` twin; see core/replay.py).

        Runs the exact policy code a live message runs before anything
        goes upstream and nothing else: the ``mcp.enabled`` / configured
        server / server-allowlist gates, then — per message — the tool
        policy and the request-phase guardrails of tier 49 (tools/call
        arguments; the whole params object on the content methods), a
        modify verdict predicted only when the rewrite is still a JSON
        object, exactly as live. Deliberately NOT re-run: the circuit
        breaker, the rate limit and the concurrency slot are state, not
        policy (the chat twin's precedent — counters are skipped), the
        session gate is gateway state a dry run holds none of, the
        upstream call is execution, and the response phase judges
        server output a dry run never sees. No log rows, no lifecycle
        events; ctx.dry_run suppresses the guardrail counters.

        Returns the predicted decision record shaped like the chat
        twin's — ``model`` is the SERVER name (the log row's model
        column), ``deployments`` is None (no routing on this surface) —
        plus ``method``: which JSON-RPC method replayed. Denials use
        the chat vocabulary (``content_policy_violation`` /
        ``tool_not_allowed``) so the report reads one language across
        surfaces.
        """
        kind = classify(message)
        method = message.get("method") if isinstance(message.get("method"), str) else None
        method_label = method or kind or "unknown"
        ctx = RequestContext(
            request_id=f"replay_{uuid.uuid4().hex}",
            auth=auth,
            model=server_name,
            stream=False,
            route="mcp",
            request_body=message,
            dry_run=True,
        )
        ctx.provider = "mcp"

        def predicted(status: str, code: str | None, reason: str | None) -> dict:
            return {
                "model": server_name,
                "method": method_label,
                "guardrail_actions": list(ctx.guardrail_actions),
                "status": status,
                "error": {"code": code, "message": reason} if code else None,
                "deployments": None,
            }

        if kind is None:
            # The live route 400s unparseable envelopes before a row is
            # ever written, so real captures never hold one — but a
            # hand-made entry gets an honest prediction, not a crash.
            return predicted(
                "error", "invalid_request_error", "Not a JSON-RPC 2.0 message."
            )
        try:
            # The gates, in the HTTP order — all policy, no state.
            if not self.config.mcp.enabled:
                raise MCPDisabledError()
            server = self.config.mcp.server(server_name)
            if server is None:
                raise MCPServerNotAllowedError(server_name)
            self._check_mcp_server_policy_for(server_name, auth)

            params = (
                message.get("params")
                if isinstance(message.get("params"), dict)
                else {}
            )
            if kind == "request" and method == "tools/call":
                name = params.get("name")
                if not isinstance(name, str):
                    # Fail closed when a policy exists — the tier-30 rule.
                    if not auth.is_admin and (
                        auth.allowed_tools or auth.tenant_allowed_tools
                    ):
                        return predicted(
                            "blocked",
                            "tool_not_allowed",
                            "Tool call rejected: no tool name in params, "
                            "and this API key has a tool policy (fail "
                            "closed).",
                        )
                elif not self._tool_name_allowed(name, ctx):
                    return predicted(
                        "blocked",
                        "tool_not_allowed",
                        f"Tool '{name}' is not allowed for this API key "
                        "(allowed_tools policy).",
                    )
                arguments = params.get("arguments")
                serialized = (
                    json.dumps(arguments, sort_keys=True)
                    if arguments is not None
                    else "{}"
                )
                _, new_text, blocked = await self._mcp_guard_request(
                    ctx, server, serialized, tool=name, method="tools/call"
                )
                if blocked is not None:
                    return predicted("blocked", "content_policy_violation", blocked.reason)
                if new_text is not None:
                    # The rewrite must still be a JSON object — the live
                    # fail-closed rule, predicted identically.
                    try:
                        if not isinstance(json.loads(new_text), dict):
                            raise ValueError("not a JSON object")
                    except ValueError:
                        return predicted(
                            "blocked",
                            "content_policy_violation",
                            "Blocked: a guardrail rewrote the tool arguments "
                            "into something that is no longer a valid JSON "
                            "object — failing closed rather than corrupting "
                            "the call.",
                        )
            elif kind == "request" and method in MCP_CONTENT_METHODS:
                serialized = json.dumps(params, sort_keys=True) if params else "{}"
                _, new_text, blocked = await self._mcp_guard_request(
                    ctx, server, serialized, method=method
                )
                if blocked is not None:
                    return predicted("blocked", "content_policy_violation", blocked.reason)
                if new_text is not None:
                    try:
                        if not isinstance(json.loads(new_text), dict):
                            raise ValueError("not a JSON object")
                    except ValueError:
                        return predicted(
                            "blocked",
                            "content_policy_violation",
                            "Blocked: a guardrail rewrote the message params "
                            "into something that is no longer a valid JSON "
                            "object — failing closed rather than corrupting "
                            "the call.",
                        )
        except GatewayError as exc:
            return predicted("error", exc.code, exc.message)
        # Everything else — initialize, ping, catalog reads,
        # notifications and client responses — carries no request-side
        # policy beyond the gates: relayed untouched live, success here.
        return predicted("success", None, None)

    async def _prepare_request_policy(
        self, request: ChatCompletionRequest, ctx: RequestContext
    ) -> ChatCompletionRequest:
        """Steps 0-1 of the chat pipeline: model resolution (with the
        context window), prompt policy and the model-permission check.

        Shared by ``_handle`` and the replay harness
        (``replay_request``) so a replayed request passes through the
        *exact* policy code a live one does — that is the point of the
        harness: what would change about this traffic under the current
        config. Counter phases (rate limit, idempotency, budget) are
        deliberately NOT here; live execution runs them between this
        and the guardrail phase.
        """
        # 0. Model aliases / default routing. Resolved before every check
        # so permissions, rate limits, guardrail model scopes, cache
        # identity, logs and the echoed response "model" all see one
        # canonical public name. ctx.request_body (already captured) keeps
        # the client's original name for debugging. The calling key's
        # model_mappings/default_model (per-key routing) take precedence
        # over the global aliases/default (see _resolve_model).
        resolved = self._resolve_model(request.model, ctx.auth)
        if resolved != request.model:
            request = request.model_copy(update={"model": resolved})
            ctx.model = resolved
            if ctx.otel_span is not None:
                ctx.otel_span.set_attribute("gateway.model", resolved)
        # The resolved model's declared context window (None when the
        # model doesn't declare one) — what the context_window guardrail
        # enforces against.
        ctx.context_window = self._model_windows.get(resolved)

        # 0.5 Prompt policy: the gateway-managed system prompt (per-key,
        # else prompts.system_prompt) is prepended when the request
        # carries no system message of its own, and {{variables}} (global
        # prompts.variables merged with the key's prompt_variables) resolve
        # in message text. Before guardrails and cache lookup on purpose:
        # guardrails must see the prompt that actually goes out, and the
        # injected/resolved prompt is part of cache identity (same rule
        # as guardrail modifications). ctx.request_body keeps what the
        # client sent; ctx.effective_request records the final form.
        # A key referencing the prompt library serves its ACTIVE version
        # (fail closed on a missing name — same rule as a broken model
        # mapping) and remembers the resolution for prompt A/B mirroring.
        variables = dict(self.config.prompts.variables)
        if ctx.auth.prompt_variables:
            variables.update(ctx.auth.prompt_variables)
        system_prompt: str | None
        if ctx.auth.system_prompt_ref:
            resolved_prompt = (
                await self.prompt_library.resolve(ctx.auth.system_prompt_ref)
                if self.prompt_library is not None
                else None
            )
            if resolved_prompt is None:
                raise GatewayError(
                    400,
                    f"The key's system_prompt_ref '{ctx.auth.system_prompt_ref}' "
                    f"does not name a prompt with an active version in the "
                    f"library. Create it via POST /admin/prompts or clear the "
                    f"reference on the key.",
                    "invalid_request_error",
                    code="prompt_not_found",
                    param="system_prompt_ref",
                )
            system_prompt = resolved_prompt.body
            ctx.library_prompt = resolved_prompt
        else:
            system_prompt = ctx.auth.system_prompt or self.config.prompts.system_prompt
        request, prompt_changed = apply_prompt_policy(
            request, system_prompt=system_prompt, variables=variables
        )
        if prompt_changed:
            ctx.effective_request = request.model_dump(exclude_none=True)

        # 1. Model permission: key allowlist (platform admins bypass
        # inside) + the tenant's allowlist, which binds every member.
        self._check_model_permission(ctx.auth, request.model)
        # 1.1 Tool-call policy: which tools this key may DECLARE. Same
        # two-layer shape as model permission (key + tenant, tenant
        # only narrows, admins exempt) — one check shared by every
        # inbound surface, since all of them normalize tools into the
        # OpenAI shape before the pipeline.
        self._check_tool_policy(ctx, request)
        return request

    async def _apply_request_guardrails(
        self, request: ChatCompletionRequest, ctx: RequestContext
    ) -> ChatCompletionRequest:
        """Steps 4-4.5: the request-phase guardrails (with modification
        chaining) and the redirect allowlist re-check. Shared by
        ``_handle`` and ``replay_request`` for the same reason as
        ``_prepare_request_policy``."""
        # 4. Guardrails: request phase
        pre_guardrail_model = request.model
        effective_request = await self.guardrails.run_request_phase(request, ctx)
        if effective_request is not request:
            ctx.effective_request = effective_request.model_dump(exclude_none=True)
            request = effective_request

        # 4.5 Guardrail redirect re-validation. A guardrail may have
        # redirected the model (reject-and-redirect instead of blocking).
        # The allowlist is a hard boundary: redirect must never bypass it,
        # so permissions are re-checked against the new model, fail closed
        # (admins bypass, as at step 1). ctx.model and the span attribute
        # are updated so rate-limit series, logs, response-phase guardrail
        # scopes, accounting and cache identity all see the final model —
        # the redirect is simply where the request goes. The redirected
        # name is used as-is (no alias re-resolution: a guardrail redirect
        # targets a public model); an unknown target 404s in the router.
        if request.model != pre_guardrail_model:
            self._check_model_permission(ctx.auth, request.model)
            ctx.model = request.model
            # The redirected model's window now applies (the guardrail
            # that redirected ran against the original's — ordering is
            # documented: guardrails within the phase are ordered).
            ctx.context_window = self._model_windows.get(request.model)
            if ctx.otel_span is not None:
                ctx.otel_span.set_attribute("gateway.model", request.model)
        # 4.6 Tool-policy re-check: a guardrail may have overlaid
        # ``tools`` (modified_request_fields). The policy is a hard
        # boundary — a guardrail must never be able to inject tools
        # past the key's allowlist, exactly like model redirects above.
        self._check_tool_policy(ctx, request)
        return request

    async def _handle(
        self,
        request: ChatCompletionRequest,
        ctx: RequestContext,
        idempotency_key: str | None = None,
    ) -> Response:
        # Lifecycle event: authenticated + parsed, before any policy. The
        # one in-band veto point before the request starts — an
        # interceptor raising here aborts through the normal error path.
        await self._emit(
            "request.received",
            ctx,
            model=request.model,
            stream=request.stream,
            key_name=ctx.auth.key_name,
        )
        request = await self._prepare_request_policy(request, ctx)

        # 1.5 Runaway-agent protection (pre-flight): a key in a runaway
        # cooldown fails fast — nothing a stuck agent sends is useful,
        # not even an idempotent replay — and this request's fingerprint
        # joins its loop window. The fingerprint is the same normalized
        # request identity the response cache keys on, so an agent
        # looping across routes (chat / responses / native) trips as
        # one loop. Admin/env-key traffic (no key_id) is exempt, like
        # every key-layer ceiling.
        await self._check_runaway(ctx, request)

        # 2. Rate limit (pre-flight)
        estimated_tokens = sum(
            estimate_tokens(content_to_text(m.content)) for m in request.messages
        )
        await self._check_rate_limit(ctx, estimated_tokens)

        # 2b. Idempotency-Key: a replay returns the stored successful
        # response for (caller, client key) without re-executing — no
        # double generation, no double billing (the stored usage was
        # billed at first execution). Runs BEFORE budget on purpose:
        # a retry must still serve even if the ceiling was hit in
        # between — the original execution was already authorized.
        # Only chat-family surfaces pass a key (embeddings/images/
        # audio ignore the header; batch lines have none); disabled
        # config ignores the header entirely.
        if (
            idempotency_key is not None
            and self.idempotency is not None
            and self.idempotency.enabled
        ):
            idempotency_key = idempotency_key.strip()
            if not idempotency_key or len(idempotency_key) > 255:
                metrics.IDEMPOTENCY.labels("rejected").inc()
                raise GatewayError(
                    400,
                    "Idempotency-Key must be 1-255 non-whitespace characters.",
                    "invalid_request_error",
                    code="invalid_idempotency_key",
                )
            replayed = await self._idempotent_replay(ctx, request, idempotency_key)
            if replayed is not None:
                return replayed
        # else: fall through and execute; ctx.idempotency_key marks the
        # executor for the store sites + the error paths in handle_chat.

        # 3. Budget (pre-flight)
        self._check_budget(ctx)

        request = await self._apply_request_guardrails(request, ctx)

        # 5. Cache lookup: exact match first, then semantic (similarity).
        #    Both are salted by the caller's response-policy token: a hit
        #    skips response guardrails, so an entry may only serve a
        #    caller whose response policy (and tenant) matches the one
        #    that stored it — keys with identical policies still share.
        #    Per-key cache policy (tier 54): the caller's snapshot can
        #    opt out of both caches (cache_enabled=False — narrowing
        #    only; a global-off cache stays off) or set its own window
        #    (cache_ttl_seconds — a read-side freshness bound here; the
        #    writer's window stamps entries at put-time). A bypassed
        #    caller skips the lookup AND the stores (key stays None),
        #    never counts a miss, and answers X-Gateway-Cache: BYPASS.
        policy_token = self.guardrails.response_policy_token(ctx)
        # Per-key cache policy (tier 54). The exact cache serves this
        # caller unless the global switch is off or the key opted out —
        # narrowing only; a global-off cache stays off for every key.
        # The semantic cache has its own global switch
        # (semantic_cache.enabled, which builds self.semantic); the
        # key's opt-out gates it too (a bypass is BOTH caches), but a
        # global cache.enabled=false never silenced semantic lookups
        # before and still doesn't — the pre-tier-54 scope stands.
        cache_active = self.cache.enabled and ctx.auth.cache_enabled is not False
        semantic_active = ctx.auth.cache_enabled is not False
        key = cache_key_for(request, policy_token) if cache_active else None
        semantic_embedding: list[float] | None = None
        cached = (
            await self.cache.get(key, max_age=ctx.auth.cache_ttl_seconds)
            if key is not None
            else None
        )
        hit_kind = "hit" if cached is not None else None
        if cached is None and self.semantic is not None and semantic_active:
            # The caller's residency requirement rides along: a
            # restricted caller's prompt is only embedded compliantly,
            # and a model with no compliant deployment is a miss here
            # (the semantic cache degrades away, never violates).
            semantic_embedding = await self.semantic.embed(
                request, ctx.auth.effective_residencies()
            )
            if semantic_embedding is not None:
                cached = await self.semantic.get(
                    request.model,
                    semantic_embedding,
                    policy_token,
                    max_age=ctx.auth.cache_ttl_seconds,
                )
                if cached is not None:
                    hit_kind = "semantic"
        if hit_kind is not None:
            metrics.CACHE.labels(hit_kind).inc()
            ctx.cache_hit = True
            await self._emit("request.cache_hit", ctx, kind=hit_kind)
            usage = cached.get("usage") or {}
            ctx.prompt_tokens = usage.get("prompt_tokens")
            ctx.completion_tokens = usage.get("completion_tokens")
            ctx.cached_tokens = cached_tokens_from_usage(usage) or None
            ctx.provider = "cache"
            ctx.status = "success"
            self._request_metric(ctx)
            await self._log(ctx)
            headers = self._headers(ctx, "HIT" if hit_kind == "hit" else "SEMANTIC")
            if request.stream:
                return self._streaming_response(
                    self._replay_cached_stream(cached), ctx, headers
                )
            return JSONResponse(content=cached, headers=headers)
        # A bypassed caller (per-key opt-out) counts no miss — it never
        # consulted the caches, so there is nothing to count; the miss
        # vocabulary stays for callers the caches could have served.
        if key is not None or (self.semantic is not None and semantic_active):
            metrics.CACHE.labels("miss").inc()

        # 5.5 Concurrency admission (pre-routing): the slot wraps the
        # upstream call — acquired after the cache miss so hits and
        # idempotent replays (no upstream, no slot) bypass it, and
        # released wherever the in-flight gauge decrements. A key at
        # its cap queues (priority-ordered) for up to
        # concurrency.wait_seconds, then answers 429 + Retry-After.
        # Keys/tenants with no cap skip admission entirely; admin/env
        # traffic (no key_id) is exempt like every key-layer ceiling.
        # Chat-family only: embeddings/images/audio run through
        # _serve_simple, which never acquires a slot.
        await self._acquire_slot(ctx)

        # 6. + 7. Route to provider (with retries/fallbacks). The
        # caller's residency requirement filters the candidate ladder
        # inside resolve — before retries and fallbacks, so a
        # constrained request can never escape to a non-compliant
        # deployment mid-failover.
        if request.stream:
            deployment, chunk_iterator = await self.router.complete_stream(
                request, ctx.auth.effective_residencies(), auth=ctx.auth
            )
            ctx.provider = deployment.provider
            ctx.deployment = deployment.name
            ctx.native_model = deployment.model
            await self._note_byok(ctx, deployment)
            await self._emit(
                "request.routed",
                ctx,
                deployment=deployment.name,
                provider=deployment.provider,
                native_model=deployment.model,
                stream=True,
            )
            headers = self._headers(
                ctx, "BYPASS" if self.cache.enabled and ctx.auth.cache_enabled is False else "MISS"
            )
            return self._streaming_response(
                self._stream_with_guardrails(
                    request,
                    ctx,
                    chunk_iterator,
                    cache_key=key,
                    semantic_embedding=semantic_embedding,
                    policy_token=policy_token,
                ),
                ctx,
                headers,
            )

        deployment, response = await self.router.complete(
            request, ctx.auth.effective_residencies(), auth=ctx.auth
        )
        ctx.provider = deployment.provider
        ctx.deployment = deployment.name
        ctx.native_model = deployment.model
        await self._note_byok(ctx, deployment)
        await self._emit(
            "request.routed",
            ctx,
            deployment=deployment.name,
            provider=deployment.provider,
            native_model=deployment.model,
            stream=False,
        )

        # 8. Guardrails: response phase
        # Tier 35: the PRE-phase answer is the raw provider text — the
        # baseline shadow diffing scores against. Extracted HERE, as an
        # immutable string, so no response-phase rewrite can leak into
        # the baseline (ctx.response_body below is what the CLIENT saw,
        # rewrites and all). Mirrors run no guardrails, so raw-vs-raw is
        # the apples-to-apples pairing — a primary-leg rewrite never
        # reads as provider drift.
        primary_raw_text = extract_content_text(response)
        try:
            response = await self.guardrails.run_response_phase(request, response, ctx)
        except GuardrailBlocked as exc:
            ctx.status = "blocked"
            ctx.error_message = exc.message
            response = self._content_filter_response(request.model, exc.message)

        # 9. Accounting
        usage = response.get("usage") or {}
        await self._account_usage(ctx, usage, deployment.model)
        ctx.response_body = response
        if ctx.status != "blocked":
            ctx.status = "success"

        # 10. Mirror to shadow deployments (background; never blocks or
        # affects the response). Only for successful upstream responses —
        # cache hits returned earlier, blocked ones skip mirroring.
        if ctx.status == "success":
            self._queue_shadow(request, ctx, primary_raw_text)
            self._queue_prompt_ab(request, ctx, primary_raw_text)

        if key is not None and ctx.status == "success":
            # The writer's window stamps the entry (per-key cache
            # policy): a key with a longer window than the global one
            # gets it; unset means the cache's configured default.
            await self.cache.put(key, response, ttl_seconds=ctx.auth.cache_ttl_seconds)
        if (
            semantic_embedding is not None
            and self.semantic is not None
            and ctx.status == "success"
        ):
            await self.semantic.put(
                request.model,
                request,
                semantic_embedding,
                response,
                policy_token,
                ttl_seconds=ctx.auth.cache_ttl_seconds,
            )

        # 10.5 Idempotency: store the successful completion under
        # (caller, client key) so retries replay instead of re-executing.
        # Blocked requests flow on through here too (a response-phase
        # block doesn't raise) — never stored, and their in-flight
        # waiters are still released: a retry SHOULD re-execute a block.
        if ctx.idempotency_key is not None and self.idempotency is not None:
            if ctx.status == "success":
                await self.idempotency.store(
                    ctx.idempotency_key,
                    {
                        "response": response,
                        "fingerprint": ctx.idempotency_fingerprint,
                    },
                )
                metrics.IDEMPOTENCY.labels("stored").inc()
            self.idempotency.finish(ctx.idempotency_key)

        self._request_metric(ctx)
        metrics.LATENCY.labels(provider=ctx.provider or "none").observe(
            ctx.latency_ms() / 1000
        )
        await self._log(ctx)
        # Lifecycle event: the response is final (guardrails applied,
        # usage accounted). The last in-band veto point — an interceptor
        # raising here fails the request instead of returning this body.
        await self._emit(
            "response.completed",
            ctx,
            response=response,
            usage=usage,
            cost_usd=ctx.cost_usd,
        )
        return JSONResponse(
            content=response,
            headers=self._headers(
                ctx,
                "BYPASS"
                if self.cache.enabled and ctx.auth.cache_enabled is False
                else "MISS",
            ),
        )

    async def _idempotent_replay(
        self,
        ctx: RequestContext,
        request: ChatCompletionRequest,
        idempotency_key: str,
    ) -> Response | None:
        """Phase 2b worker: replay-or-mark for a caller-supplied
        Idempotency-Key (already validated + store enabled).

        Returns the replayed Response when a stored (or in-flight, then
        stored) completion exists; None when this request must execute —
        with ``ctx.idempotency_key`` then marking it the executor for
        the store sites and ``handle_chat``'s error paths.

        The fingerprint is the ORIGINAL request — the same normalized
        payload the response cache keys on — computed here, before the
        guardrail phase can rebind ``request``. A client retrying sends
        the same bytes; a client *changing* parameters under the same key
        gets a 400 rather than the wrong answer.
        """
        store = self.idempotency
        assert store is not None  # the 2b gate checked
        store_key = store.key_for(ctx.auth.key_id, idempotency_key)
        fingerprint = hashlib.sha256(cache_payload_for(request).encode()).hexdigest()

        entry = await store.lookup(store_key)
        if entry is None:
            in_flight = store.begin(store_key)
            if in_flight is not None:
                # A duplicate while the original executes (the canonical
                # case: the client timed out and retried). Wait for it —
                # bounded, and shielded so our timeout can never cancel
                # the original — then re-check the store.
                try:
                    await asyncio.wait_for(
                        asyncio.shield(in_flight), store.inflight_wait_seconds
                    )
                except asyncio.TimeoutError:
                    pass  # wedged original: execute ourselves (last writer wins)
                entry = await store.lookup(store_key)
                if entry is None:
                    # The original failed — errors are never stored, and a
                    # retry SHOULD re-execute them. Re-mark in flight so a
                    # third duplicate waits for us instead of triple-firing.
                    store.begin(store_key)
        if entry is not None:
            if entry.get("fingerprint") != fingerprint:
                metrics.IDEMPOTENCY.labels("rejected").inc()
                raise GatewayError(
                    400,
                    "Idempotency-Key was already used with different request "
                    "parameters.",
                    "invalid_request_error",
                    code="idempotency_key_reuse",
                )
            # Replay: the cache-hit semantics, private to this caller.
            # Zero cost — the stored usage was billed at first execution.
            metrics.IDEMPOTENCY.labels("replayed").inc()
            ctx.cache_hit = True
            await self._emit("request.cache_hit", ctx, kind="idempotency")
            usage = entry["response"].get("usage") or {}
            ctx.prompt_tokens = usage.get("prompt_tokens")
            ctx.completion_tokens = usage.get("completion_tokens")
            ctx.cached_tokens = cached_tokens_from_usage(usage) or None
            ctx.provider = "idempotency"
            ctx.status = "success"
            self._request_metric(ctx)
            await self._log(ctx)
            headers = self._headers(ctx, "IDEMPOTENT")
            if request.stream:
                return self._streaming_response(
                    self._replay_cached_stream(entry["response"]), ctx, headers
                )
            return JSONResponse(content=entry["response"], headers=headers)
        # Miss: this request is the executor. The store sites put the
        # successful completion here; every error path releases the
        # in-flight waiters via finish().
        ctx.idempotency_key = store_key
        ctx.idempotency_fingerprint = fingerprint
        return None

    # ------------------------------------------------------------------
    # Realtime (WebSocket) sessions — tier 32
    # ------------------------------------------------------------------

    async def handle_realtime(
        self,
        ws: "Any",
        model: str,
        auth: AuthContext,
        request_id: str | None = None,
    ) -> None:
        """One realtime session, end to end (``ws`` is a Starlette
        WebSocket). The open gates mirror the HTTP surfaces exactly —
        model permission, rate limit (one RPM hit at open; tokens
        accrue per response), budget, a concurrency slot held for the
        session's life — then routing walks the residency- and
        breaker-filtered ladder at CONNECT time (a long-lived session
        cannot fail over mid-stream), authenticating the upstream
        handshake with the BYOK-resolved credential in the provider's
        own header shape. The relay (core/realtime.py) owns everything
        per-event afterwards.

        Raises GatewayError for every gate/routing failure — the API
        route translates those into an OpenAI-shaped realtime error
        event + close, the session-shaped equivalent of an HTTP error
        envelope. Returns normally on any disconnect; the finally-block
        writes the log row, the metrics and gives the slot back.
        """
        import websockets  # local: the realtime-only dependency

        ctx = RequestContext(
            request_id=request_id or f"req_{uuid.uuid4().hex}",
            auth=auth,
            model=model,
            stream=True,
            # Surface NAME, like every other inbound route ("chat",
            # "responses", ...) — guardrail scope.routes patterns match
            # on this value.
            route="realtime",
            request_body={"model": model},
        )
        metrics.INFLIGHT.inc()
        span = self._tracer.start_span("gateway.realtime", kind=SpanKind.INTERNAL)
        ctx.otel_span = span
        span.set_attribute("gateway.request_id", ctx.request_id)
        span.set_attribute("gateway.model", ctx.model)
        span.set_attribute("gateway.stream", True)
        span.set_attribute("gateway.route", ctx.route)
        span.set_attribute("gateway.key_name", auth.key_name or "admin")
        span_context = span.get_span_context()
        if span_context.is_valid:  # invalid = tracing disabled (no-op span)
            ctx.trace_id = format(span_context.trace_id, "032x")
            ctx.span_id = format(span_context.span_id, "016x")
        # Initialized before the try so the finally can close it on
        # every exit path (a gate failure must not leak the handshake,
        # and a normal disconnect must not leak the session upstream).
        upstream = None
        with use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                # The open gates, in the HTTP order.
                ctx.model = self._resolve_model(model, auth)
                self._check_model_permission(auth, ctx.model)
                await self._check_rate_limit(ctx, 0)
                self._check_budget(ctx)
                await self._acquire_slot(ctx)

                # Routing: the candidate ladder for a long-lived
                # session, filtered to realtime-capable providers.
                residencies = auth.effective_residencies()
                candidates = await self.router.resolve_realtime(ctx.model, residencies)

                def _realtime_base(candidate) -> str | None:
                    cfg = self.router.config.providers.get(candidate.provider)
                    return getattr(cfg, "realtime_base", None) if cfg is not None else None

                capable = [d for d in candidates if _realtime_base(d)]
                if not candidates:
                    raise ModelNotFoundError(ctx.model)
                if not capable:
                    raise GatewayError(
                        404,
                        f"Model '{ctx.model}' has no realtime-capable deployment — "
                        "no provider in its routing group has realtime_base "
                        "configured. Realtime sessions are WebSocket-only; "
                        "use /v1/chat/completions for this model.",
                        "invalid_request_error",
                        code="realtime_unavailable",
                    )
                # vault.strict (tier 34): the same ladder narrowing as
                # HTTP, at the one point a realtime session routes — a
                # strict tenant's session may only open on a provider
                # it keyed. The refusal raises ByokKeyRequiredError,
                # which this surface delivers in-band (error event +
                # close 1008) exactly like every other gate.
                capable = await self.router._enforce_strict(
                    capable, auth, ctx.model
                )

                # Connect-time failover: walk the ladder, first
                # handshake that accepts wins. The session then stays
                # on that deployment for its life.
                upstream = None
                attempts: list[str] = []
                deployment = None
                for candidate in capable:
                    cfg = await self.router._resolve_provider_cfg(candidate, auth)
                    adapter = self.router.providers[candidate.provider]
                    url = f"{cfg.realtime_base}/realtime?model={candidate.model}"
                    try:
                        upstream = await websockets.connect(
                            url, additional_headers=adapter.ws_headers(cfg)
                        )
                        deployment = candidate
                        break
                    except Exception as exc:
                        attempts.append(f"{candidate.name}: {exc}")
                        continue
                if upstream is None:
                    raise ProviderError(
                        502,
                        "All realtime-capable deployments refused the WebSocket "
                        "handshake ("
                        + "; ".join(attempts)[:800]
                        + ").",
                        "api_error",
                    )
                ctx.provider = deployment.provider
                ctx.deployment = deployment.name
                ctx.native_model = deployment.model
                await self._note_byok(ctx, deployment)
                await self._emit(
                    "request.routed",
                    ctx,
                    deployment=deployment.name,
                    provider=deployment.provider,
                    realtime=True,
                )
                gauge = metrics.REALTIME_SESSIONS.labels(
                    provider=ctx.provider, model=ctx.model
                )
                gauge.inc()
                try:
                    relay = RealtimeRelay(
                        self, ctx, ws, upstream, deployment.model
                    )
                    await relay.run()
                finally:
                    gauge.dec()
            finally:
                # Session teardown: the row, the metric, the slot, the
                # span — on any exit path (gate failure, disconnect,
                # upstream death). The upstream WebSocket closes first:
                # the provider's session ends with the gateway's, never
                # after the log row says the session is over.
                if upstream is not None:
                    with suppress(Exception):
                        await upstream.close()
                try:
                    if ctx.provider:
                        self._request_metric(ctx)
                finally:
                    self._release_slot(ctx)
                    metrics.INFLIGHT.dec()
                    if ctx.status == "success" and ctx.error_message:
                        ctx.status = "error"
                    try:
                        await self._log(ctx, "realtime session completed")
                    except Exception:
                        logger.exception("realtime log write failed")
                    if span is not None:
                        self._annotate_span(span, ctx)
                        if ctx.status in ("error", "blocked"):
                            span.set_status(
                                Status(StatusCode.ERROR, ctx.error_message or ctx.status)
                            )
                        else:
                            span.set_status(Status(StatusCode.OK))
                        span.end()

    # ------------------------------------------------------------------
    # MCP proxy (tier 33)
    # ------------------------------------------------------------------

    async def handle_mcp(
        self,
        message: dict[str, Any],
        auth: AuthContext,
        server_name: str,
        registry: MCPSessionRegistry,
        *,
        session_header: str | None = None,
        request_id: str | None = None,
    ) -> Response:
        """One POST /v1/mcp/{server}: one JSON-RPC message in, the
        server's answer out — JSON or SSE, with the response frame
        intercepted where policy applies (tools/list filtering,
        tools/call and the content-method guardrails of tier 49). The transport follows MCP 2025-06-18
        Streamable HTTP: the gateway issues its OWN Mcp-Session-Id at
        initialize and maps it to the upstream session + negotiated
        protocol version; notifications and client responses relay as
        202; upstream HTTP failures relay verbatim (the client's
        JSON-RPC layer sees the server's own protocol errors) while
        GATEWAY failures keep the gateway envelope — the documented
        split: HTTP-gate failures (auth, scope, config, rate limits)
        are the operator's concern, JSON-RPC-level failures are the
        caller's.

        Accounting: one request-log row per message (route ``mcp``,
        provider ``mcp``, model = the SERVER name), INFLIGHT, and
        gateway_mcp_requests_total{server,method,outcome}. No tokens
        and no cost — MCP servers are not language models; RPM applies
        (the realtime precedent), TPM/budget do not, by design.
        """
        kind = classify(message)
        method = message.get("method")
        method_label = method or kind or "unknown"
        if self._http_client is None:  # standalone pipeline — never via create_app
            raise GatewayError(
                503,
                "The MCP proxy has no HTTP client configured "
                "(create_app must receive one).",
                "api_error",
            )
        ctx = RequestContext(
            request_id=request_id or f"req_{uuid.uuid4().hex}",
            auth=auth,
            # The server name rides the log row's model column — the
            # same role a model name plays on every other surface
            # (what the caller chose to talk to), which also means
            # model-scoped guardrails scope to servers on this route.
            model=server_name,
            stream=False,
            route="mcp",
            # Tier 67: the VERBATIM JSON-RPC message, params included —
            # the request-policy phases read the caller-controlled
            # content (tools/call arguments, the content methods'
            # params), and the capture-replay harness needs exactly
            # that stored to re-run them. The write is still gated by
            # logging.store_bodies (PII-safe mode keeps the column
            # NULL) and truncated by logging.max_body_chars.
            request_body=message,
        )
        ctx.provider = "mcp"
        metrics.INFLIGHT.inc()
        span = self._tracer.start_span("gateway.mcp", kind=SpanKind.INTERNAL)
        ctx.otel_span = span
        span.set_attribute("gateway.request_id", ctx.request_id)
        span.set_attribute("gateway.model", ctx.model)
        span.set_attribute("gateway.route", ctx.route)
        span.set_attribute("gateway.key_name", auth.key_name or "admin")
        span.set_attribute("gateway.mcp.method", method_label)
        span_context = span.get_span_context()
        if span_context.is_valid:
            ctx.trace_id = format(span_context.trace_id, "032x")
            ctx.span_id = format(span_context.span_id, "016x")
        # The SSE relay finishes its own accounting (the response body
        # outlives this call); every other exit — return or raise — is
        # accounted exactly once by the finally below.
        deferred = False
        with use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                # The gates, in the HTTP order. All gateway-envelope
                # failures (the documented split).
                if not self.config.mcp.enabled:
                    raise MCPDisabledError()
                server = self.config.mcp.server(server_name)
                if server is None:
                    raise MCPServerNotAllowedError(server_name)
                self._check_mcp_server_policy(ctx, server_name)
                # Per-server circuit breaker (tier 66): refuse BEFORE
                # the rate limit and the concurrency slot — an open
                # circuit fails fast and holds nothing. Every message
                # is gated, initialize included (after the cooldown it
                # is the natural probe). The refusal is in-band — HTTP
                # 200 with a JSON-RPC error — the same rationale as a
                # guardrail denial: the client's JSON-RPC layer must
                # see it. No all-open bypass here: this server is the
                # only upstream for its name.
                if not await self.mcp_breakers.allow(server_name):
                    ctx.status = "error"
                    ctx.error_message = "circuit breaker open"
                    ctx.mcp_circuit_refused = True
                    return JSONResponse(
                        jsonrpc_error(
                            message.get("id"),
                            CIRCUIT_OPEN,
                            f"MCP server '{server_name}' refused: its "
                            "circuit breaker is open after consecutive "
                            "connect failures or 5xx answers. Retry "
                            "after the cooldown.",
                        )
                    )
                await self._check_rate_limit(ctx, 0)
                await self._acquire_slot(ctx)

                async def _dispatch() -> Response:
                    # Session lifecycle: initialize creates; everything
                    # else (notifications included, per the transport
                    # spec) requires the session header and must map.
                    if kind == "request" and method == "initialize":
                        return await self._mcp_initialize(
                            ctx, server, registry, message
                        )
                    if not session_header:
                        ctx.status = "error"
                        return JSONResponse(
                            jsonrpc_error(
                                None,
                                SESSION_REQUIRED,
                                "Bad Request: Mcp-Session-Id header is required. "
                                "Send an 'initialize' request first.",
                            ),
                            status_code=400,
                        )
                    session = registry.get(session_header)
                    if session is None:
                        ctx.status = "error"
                        return JSONResponse(
                            jsonrpc_error(
                                None,
                                SESSION_TERMINATED,
                                "Session not found or expired. Re-initialize.",
                            ),
                            status_code=404,
                        )
                    registry.touch(session)

                    if kind in ("notification", "response"):
                        return await self._mcp_relay(
                            ctx, server, registry, session, message
                        )
                    if method == "tools/list":
                        return await self._mcp_tools_list(
                            ctx, server, registry, session, message
                        )
                    if method == "tools/call":
                        return await self._mcp_tools_call(
                            ctx, server, registry, session, message
                        )
                    if method in MCP_CONTENT_METHODS:
                        return await self._mcp_content_call(
                            ctx, server, registry, session, message, method
                        )
                    return await self._mcp_exchange(
                        ctx,
                        server,
                        registry,
                        session,
                        message,
                        on_response=None,
                        kind=kind,
                        method_label=method_label,
                    )

                response = await _dispatch()
                if isinstance(response, StreamingResponse):
                    # The relay generator owns the stream — and the
                    # accounting with it.
                    deferred = True
                return response
            except GatewayError as exc:
                ctx.status = "blocked" if isinstance(exc, GuardrailBlocked) else "error"
                ctx.error_message = exc.message
                raise
            except Exception as exc:  # noqa: BLE001
                ctx.status = "error"
                ctx.error_message = f"internal error: {exc}"
                raise
            finally:
                if not deferred:
                    await self._mcp_account(ctx, span, kind, method_label)

    async def mcp_terminate(
        self,
        server_name: str,
        session_header: str | None,
        registry: MCPSessionRegistry,
        auth: AuthContext,
    ) -> Response:
        """DELETE /v1/mcp/{server}: the transport's session termination.
        The LOCAL mapping always drops first (idempotent state the
        client can rely on), then a best-effort upstream DELETE relays
        its status — including 405, which the spec explicitly allows a
        server to answer when it does not support termination. No log
        row: DELETE carries no JSON-RPC message; the mcp.session.closed
        event and the gauge carry the observability."""
        config = self.config.mcp
        if not config.enabled:
            raise MCPDisabledError()
        server = config.server(server_name)
        if server is not None:
            # Only fail on the config gate when the server exists — a
            # client terminating the session of a since-removed server
            # should still clean up local state.
            self._check_mcp_server_policy_for(server_name, auth)
        if self._http_client is None:
            raise GatewayError(
                503,
                "The MCP proxy has no HTTP client configured.",
                "api_error",
            )
        if not session_header:
            return JSONResponse(
                jsonrpc_error(
                    None,
                    SESSION_REQUIRED,
                    "Bad Request: Mcp-Session-Id header is required.",
                ),
                status_code=400,
            )
        session = registry.drop(session_header)
        if session is None:
            return JSONResponse(
                jsonrpc_error(
                    None,
                    SESSION_TERMINATED,
                    "Session not found or already terminated.",
                ),
                status_code=404,
            )
        await self._emit(
            "mcp.session.closed",
            None,
            server=session.server_name,
            gateway_session_id=session.gateway_session_id,
            reason="client",
        )
        status = 200
        if server is not None:
            try:
                status = await self._mcp_delete_upstream(server, session)
            except GatewayError:
                # Local state is already dropped — the client got what
                # it asked for — but be honest about the upstream leg.
                return Response(status_code=502)
        if status in (200, 202, 204):
            return Response(status_code=200)
        return Response(status_code=status, headers={"Allow": "POST, DELETE"})

    def _check_mcp_server_policy(self, ctx: RequestContext, server: str) -> None:
        self._check_mcp_server_policy_for(server, ctx.auth)

    @staticmethod
    def _check_mcp_server_policy_for(server: str, auth: AuthContext) -> None:
        """Two-layer MCP server permission — the model-permission
        mechanics, verbatim: the key's ``allowed_mcp_servers`` fnmatch
        patterns (platform admins bypass — they are the operators),
        then the tenant's pooled patterns (no bypass; a tenant can only
        narrow). None = that layer sets no restriction."""
        import fnmatch

        if auth.is_admin:
            return
        patterns = auth.allowed_mcp_servers
        if patterns and not any(fnmatch.fnmatch(server, p) for p in patterns):
            raise MCPServerNotAllowedError(server)
        tenant_patterns = auth.tenant_allowed_mcp_servers
        if (
            auth.tenant_id is not None
            and tenant_patterns
            and not any(fnmatch.fnmatch(server, p) for p in tenant_patterns)
        ):
            raise MCPServerNotAllowedError(server)

    async def _mcp_initialize(
        self,
        ctx: RequestContext,
        server: MCPServerConfig,
        registry: MCPSessionRegistry,
        message: dict[str, Any],
    ) -> Response:
        """initialize: the only session-creating message. The upstream
        answer (JSON or SSE — the server chooses) is consumed HERE and
        re-emitted as JSON to the client, which then quotes OUR
        Mcp-Session-Id — mapped to the upstream's session and the
        negotiated protocol version — on every following message."""
        response = await self._mcp_post(ctx, server, None, message, stream=False)
        try:
            if response.status_code >= 400:
                ctx.status = "error"
                ctx.error_message = f"upstream initialize answered {response.status_code}"
                return Response(
                    status_code=response.status_code,
                    content=response.content,
                    media_type=response.headers.get(
                        "content-type", "application/json"
                    ),
                )
            body = parse_jsonrpc_message(
                response.content,
                response.headers.get("content-type", ""),
            )
            if body is None:
                ctx.status = "error"
                ctx.error_message = "upstream initialize was not a JSON-RPC message"
                raise ProviderError(
                    502,
                    f"MCP server '{server.name}' did not answer the "
                    "initialize request with a JSON-RPC message.",
                    "api_error",
                )
            if "error" in body:
                ctx.status = "error"
                ctx.error_message = "upstream refused the initialize request"
                return JSONResponse(body)
            result = body.get("result") if isinstance(body.get("result"), dict) else {}
            session = registry.create(
                server.name,
                response.headers.get("mcp-session-id"),
                result.get("protocolVersion"),
            )
            await self._emit(
                "mcp.session.opened",
                ctx,
                server=server.name,
                gateway_session_id=session.gateway_session_id,
                protocol_version=session.protocol_version,
            )
            ctx.status = "success"
            return JSONResponse(
                body, headers={"Mcp-Session-Id": session.gateway_session_id}
            )
        finally:
            with suppress(Exception):
                await response.aclose()

    async def _mcp_relay(
        self,
        ctx: RequestContext,
        server: MCPServerConfig,
        registry: MCPSessionRegistry,
        session: MCPSession,
        message: dict[str, Any],
    ) -> Response:
        """Notifications (no id) and client responses (answering a
        server-initiated request): relayed verbatim. A 202 is the
        transport's 'accepted, no reply' — mirrored exactly; anything
        else relays status + body so the client sees the server's own
        protocol error. No guardrails here by design: policy on the
        MCP surface attaches to tool traffic (tools/list, tools/call),
        the two messages that move caller-controlled content."""
        response = await self._mcp_post(ctx, server, session, message, stream=False)
        try:
            if response.status_code == 404:
                registry.drop(session.gateway_session_id)
                await self._emit(
                    "mcp.session.closed",
                    ctx,
                    server=session.server_name,
                    gateway_session_id=session.gateway_session_id,
                    reason="upstream_gone",
                )
            if response.status_code >= 400:
                ctx.status = "error"
                ctx.error_message = f"upstream answered {response.status_code}"
                return Response(
                    status_code=response.status_code,
                    content=response.content,
                    media_type=response.headers.get(
                        "content-type", "application/json"
                    ),
                )
            ctx.status = "success"
            if response.status_code == 202 or response.status_code == 204 or not response.content:
                return Response(status_code=202)
            # A body on a notification/response POST is off-spec but not
            # ours to judge — relay it and let the client decide.
            return Response(
                status_code=response.status_code,
                content=response.content,
                media_type=response.headers.get(
                    "content-type", "application/json"
                ),
            )
        finally:
            with suppress(Exception):
                await response.aclose()

    async def _mcp_tools_list(
        self,
        ctx: RequestContext,
        server: MCPServerConfig,
        registry: MCPSessionRegistry,
        session: MCPSession,
        message: dict[str, Any],
    ) -> Response:
        """tools/list through the tier-30 tool policy: the key sees the
        tools it could actually CALL. The upstream catalog arrives
        unfiltered (the server is not gateway-aware); the filter is
        applied on the response frame — JSON body or SSE frame — so a
        restricted key never learns the names it may not use."""

        async def on_response(body: dict[str, Any]) -> dict[str, Any]:
            if isinstance(body.get("result"), dict) and isinstance(
                body["result"].get("tools"), list
            ):
                body["result"]["tools"] = [
                    tool
                    for tool in body["result"]["tools"]
                    if isinstance(tool, dict)
                    and self._tool_name_allowed(str(tool.get("name") or ""), ctx)
                ]
            return body

        return await self._mcp_exchange(
            ctx,
            server,
            registry,
            session,
            message,
            on_response=on_response,
            kind="request",
            method_label="tools/list",
        )

    async def _mcp_guard_request(
        self,
        ctx: RequestContext,
        server: MCPServerConfig,
        text: str,
        *,
        tool: str | None = None,
        method: str | None = None,
    ) -> tuple[ChatCompletionRequest, str | None, MCPToolBlocked | None]:
        """The request-phase guardrails over one piece of caller-
        controlled MCP content, factored from tools/call (tier 49):
        the serialized params run through run_request_phase as a
        synthetic chat request — model = the server name (what the
        caller chose to talk to, and what model-scoped guardrails
        match on this surface), one user message = the text.

        Returns ``(modified_request, new_text, blocked)``:

        * ``blocked`` — a denial, a model-redirect verdict (there is
          no model to redirect on this surface — fail closed, never
          silently drop a verdict) or a NON-TEXT rewrite (content the
          caller cannot consume is a corruption, not a policy). The
          caller answers with the in-band error; nothing travels
          upstream.
        * ``new_text`` — the rewritten text when a modify verdict
          changed it, ``None`` when it did not. Whether the rewrite is
          still the right SHAPE (a JSON object) is the caller's to
          validate — each message type knows its own.
        """
        synthetic = ChatCompletionRequest(
            model=server.name,
            messages=[Message(role="user", content=text)],
        )
        try:
            modified = await self.guardrails.run_request_phase(synthetic, ctx)
        except GuardrailBlocked as exc:
            return (
                synthetic,
                None,
                MCPToolBlocked(
                    GUARDRAIL_BLOCKED,
                    f"Request blocked by guardrail '{exc.guardrail}': {exc.reason}",
                    tool=tool,
                    guardrail=exc.guardrail,
                    method=method,
                ),
            )
        if modified.model != server.name:
            return (
                synthetic,
                None,
                MCPToolBlocked(
                    GUARDRAIL_BLOCKED,
                    "Blocked: a guardrail issued a model redirect, which the "
                    "MCP surface cannot honor — failing closed.",
                    tool=tool,
                    method=method,
                ),
            )
        new_content = modified.messages[-1].content if modified.messages else None
        if new_content is not None and not isinstance(new_content, str):
            return (
                synthetic,
                None,
                MCPToolBlocked(
                    GUARDRAIL_BLOCKED,
                    "Blocked: a guardrail rewrote the MCP message content "
                    "into a non-text message — failing closed.",
                    tool=tool,
                    method=method,
                ),
            )
        return modified, (new_content if new_content != text else None), None

    async def _mcp_guard_response_text(
        self,
        request_for_response: ChatCompletionRequest,
        ctx: RequestContext,
        server_name: str,
        text: str,
        *,
        tool: str | None = None,
        method: str | None = None,
    ) -> str:
        """One piece of server-returned MCP text through the
        response-phase guardrails, factored from tools/call (tier 49):
        a synthetic chat.completion — one assistant message = the
        text — run over the request the (possibly rewritten) inbound
        message produced, so response policies judge the content that
        actually went upstream. Returns the new text when a modify
        verdict produced one, the original otherwise (a non-text
        rewrite is ignored, never a corruption); raises
        MCPToolBlocked on a denial — the exchange turns it into the
        in-band error."""
        synthetic_response = {
            "id": f"chatcmpl-mcp-{ctx.request_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": server_name,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ],
        }
        try:
            rewritten = await self.guardrails.run_response_phase(
                request_for_response, synthetic_response, ctx
            )
        except GuardrailBlocked as exc:
            raise MCPToolBlocked(
                GUARDRAIL_BLOCKED,
                f"Request blocked by guardrail '{exc.guardrail}': {exc.reason}",
                tool=tool,
                guardrail=exc.guardrail,
                method=method,
            ) from exc
        new_text = (
            rewritten.get("choices", [{}])[0].get("message", {}).get("content")
        )
        return new_text if isinstance(new_text, str) else text

    async def _mcp_guard_result_items(
        self,
        request_for_response: ChatCompletionRequest,
        ctx: RequestContext,
        server_name: str,
        items: list[Any],
        *,
        tool: str | None = None,
        method: str | None = None,
    ) -> None:
        """Content blocks (tools/call results, prompt messages) through
        the response-phase guardrails, in place: text items and the
        TEXT of embedded resources are guarded; image/audio items and
        resource blobs carry base64, not text — skipped by design, and
        a text guardrail run over base64 would only mangle it."""
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                item["text"] = await self._mcp_guard_response_text(
                    request_for_response,
                    ctx,
                    server_name,
                    item["text"],
                    tool=tool,
                    method=method,
                )
            elif (
                item.get("type") == "resource"
                and isinstance(item.get("resource"), dict)
                and isinstance(item["resource"].get("text"), str)
            ):
                item["resource"]["text"] = await self._mcp_guard_response_text(
                    request_for_response,
                    ctx,
                    server_name,
                    item["resource"]["text"],
                    tool=tool,
                    method=method,
                )

    async def _mcp_tools_call(
        self,
        ctx: RequestContext,
        server: MCPServerConfig,
        registry: MCPSessionRegistry,
        session: MCPSession,
        message: dict[str, Any],
    ) -> Response:
        """tools/call: the full policy pass on the one message type
        that moves caller-controlled content both ways.

        Before upstream — the tool policy, then the request-phase
        guardrails over the serialized arguments (a synthetic chat
        request: model = server name, one user message = the sorted-
        JSON arguments). A modify verdict is applied ONLY if the
        rewritten text is still a valid JSON object — otherwise the
        call fails closed as blocked; a model-redirect verdict has no
        meaning on this surface and is treated the same way. A denial
        is an IN-BAND JSON-RPC error (HTTP 200): the agent at the other
        end must see which tool and why, and a transport 4xx would hide
        that from its JSON-RPC layer. Nothing upstream sees a denied
        call.

        After upstream — the response-phase guardrails over the
        result's text: each text item, the text of embedded resource
        items, and structuredContent guarded as serialized JSON (a
        rewrite applied only if it is still a JSON object — the
        same fail-closed rule as the arguments). Base64 payloads —
        image/audio items, resource blobs — are binary: no text to
        scan, skipped by design, and a text guardrail run over
        base64 would only mangle it. A blocked result becomes the
        same in-band error, upstream's answer discarded."""
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        name = params.get("name")
        message_id = message.get("id")

        def denied(blocked: MCPToolBlocked) -> Response:
            return JSONResponse(
                jsonrpc_error(message_id, blocked.code, blocked.reason)
            )

        if not isinstance(name, str):
            # Fail closed when a policy exists (the tier-30 rule: a
            # policy a request can't prove it passes is a policy it
            # fails); unrestricted callers fall through to the server,
            # which rejects the malformed call in its own shape.
            if not ctx.auth.is_admin and (
                ctx.auth.allowed_tools or ctx.auth.tenant_allowed_tools
            ):
                blocked = MCPToolBlocked(
                    TOOL_NOT_ALLOWED,
                    "Tool call rejected: no tool name in params, and this "
                    "API key has a tool policy (fail closed).",
                )
                await self._mcp_apply_block(ctx, blocked)
                return denied(blocked)
        elif not self._tool_name_allowed(name, ctx):
            blocked = MCPToolBlocked(
                TOOL_NOT_ALLOWED,
                f"Tool '{name}' is not allowed for this API key "
                "(allowed_tools policy).",
                tool=name,
            )
            await self._mcp_apply_block(ctx, blocked)
            return denied(blocked)

        arguments = params.get("arguments")
        serialized = json.dumps(arguments, sort_keys=True) if arguments is not None else "{}"
        modified, new_text, blocked = await self._mcp_guard_request(
            ctx, server, serialized, tool=name, method="tools/call"
        )
        if blocked is not None:
            await self._mcp_apply_block(ctx, blocked)
            return denied(blocked)
        if new_text is not None:
            # Applied ONLY if the rewrite is still a JSON object — the
            # fail-closed rule: a corrupted call is worse than a
            # blocked one, and a blocked call at least explains
            # itself.
            try:
                new_arguments = json.loads(new_text)
                if not isinstance(new_arguments, dict):
                    raise ValueError("not a JSON object")
                params["arguments"] = new_arguments
            except ValueError:
                blocked = MCPToolBlocked(
                    GUARDRAIL_BLOCKED,
                    "Blocked: a guardrail rewrote the tool arguments into "
                    "something that is no longer a valid JSON object — "
                    "failing closed rather than corrupting the call.",
                    tool=name,
                    method="tools/call",
                )
                await self._mcp_apply_block(ctx, blocked)
                return denied(blocked)

        request_for_response = modified

        async def on_response(body: dict[str, Any]) -> dict[str, Any]:
            if not isinstance(body.get("result"), dict):
                return body
            result = body["result"]
            content = result.get("content")
            if isinstance(content, list):
                await self._mcp_guard_result_items(
                    request_for_response,
                    ctx,
                    server.name,
                    content,
                    tool=name,
                    method="tools/call",
                )
            structured = result.get("structuredContent")
            if isinstance(structured, dict):
                # The typed-result companion: guarded as serialized
                # JSON, a rewrite applied only if it is still a JSON
                # object — the same fail-closed rule as the arguments.
                serialized_structured = json.dumps(structured, sort_keys=True)
                new_structured = await self._mcp_guard_response_text(
                    request_for_response,
                    ctx,
                    server.name,
                    serialized_structured,
                    tool=name,
                    method="tools/call",
                )
                if new_structured != serialized_structured:
                    try:
                        parsed = json.loads(new_structured)
                        if not isinstance(parsed, dict):
                            raise ValueError("not a JSON object")
                        result["structuredContent"] = parsed
                    except ValueError:
                        raise MCPToolBlocked(
                            GUARDRAIL_BLOCKED,
                            "Blocked: a guardrail rewrote the tool's "
                            "structuredContent into something that is no "
                            "longer a valid JSON object — failing closed "
                            "rather than corrupting the result.",
                            tool=name,
                            method="tools/call",
                        ) from None
            return body

        return await self._mcp_exchange(
            ctx,
            server,
            registry,
            session,
            message,
            on_response=on_response,
            kind="request",
            method_label="tools/call",
        )

    async def _mcp_content_call(
        self,
        ctx: RequestContext,
        server: MCPServerConfig,
        registry: MCPSessionRegistry,
        session: MCPSession,
        message: dict[str, Any],
        method: str,
    ) -> Response:
        """The content-bearing methods (tier 49): resources/read,
        prompts/get, completion/complete — everything that moves
        caller-controlled text in and server-generated text out,
        besides tools/call's dedicated path (tool policy + the
        arguments shape).

        Before upstream — the request-phase guardrails over the
        serialized params (the whole object: uri, name, arguments,
        ref, argument — everything the caller controls on these
        methods, as a synthetic chat request). A modify verdict is
        applied ONLY if the rewrite is still a JSON object — the
        arguments rule; a redirect verdict cannot apply here either
        and fails closed. Denials are in-band JSON-RPC errors
        (HTTP 200); nothing upstream sees them.

        After upstream — the response-phase guardrails over every
        text shape the method can answer with, each piece its own
        synthetic chat.completion: resources/read's
        ``contents[].text`` (a ``blob`` twin is binary — skipped by
        design), prompts/get's message content blocks (the field in
        BOTH its shapes — a single block or a list — because the
        spec widened it and servers ship both), and
        completion/complete's ``completion.values[]``. A denial
        replaces the whole answer with the in-band error."""
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        message_id = message.get("id")

        def denied(blocked: MCPToolBlocked) -> Response:
            return JSONResponse(
                jsonrpc_error(message_id, blocked.code, blocked.reason)
            )

        serialized = json.dumps(params, sort_keys=True) if params else "{}"
        modified, new_text, blocked = await self._mcp_guard_request(
            ctx, server, serialized, method=method
        )
        if blocked is not None:
            await self._mcp_apply_block(ctx, blocked)
            return denied(blocked)
        if new_text is not None:
            try:
                new_params = json.loads(new_text)
                if not isinstance(new_params, dict):
                    raise ValueError("not a JSON object")
                message["params"] = new_params
            except ValueError:
                blocked = MCPToolBlocked(
                    GUARDRAIL_BLOCKED,
                    "Blocked: a guardrail rewrote the message params into "
                    "something that is no longer a valid JSON object — "
                    "failing closed rather than corrupting the call.",
                    method=method,
                )
                await self._mcp_apply_block(ctx, blocked)
                return denied(blocked)

        request_for_response = modified

        async def on_response(body: dict[str, Any]) -> dict[str, Any]:
            result = body.get("result")
            if not isinstance(result, dict):
                return body
            if method == "resources/read":
                contents = result.get("contents")
                if isinstance(contents, list):
                    for item in contents:
                        if (
                            isinstance(item, dict)
                            and isinstance(item.get("text"), str)
                        ):
                            item["text"] = await self._mcp_guard_response_text(
                                request_for_response,
                                ctx,
                                server.name,
                                item["text"],
                                method=method,
                            )
            elif method == "prompts/get":
                messages = result.get("messages")
                if isinstance(messages, list):
                    for entry in messages:
                        if not isinstance(entry, dict):
                            continue
                        block = entry.get("content")
                        # A single block or a list of them — the walk
                        # handles both shapes.
                        blocks = block if isinstance(block, list) else [block]
                        await self._mcp_guard_result_items(
                            request_for_response,
                            ctx,
                            server.name,
                            blocks,
                            method=method,
                        )
            elif method == "completion/complete":
                completion = result.get("completion")
                if isinstance(completion, dict):
                    values = completion.get("values")
                    if isinstance(values, list):
                        for i, value in enumerate(values):
                            if isinstance(value, str):
                                values[i] = await self._mcp_guard_response_text(
                                    request_for_response,
                                    ctx,
                                    server.name,
                                    value,
                                    method=method,
                                )
            return body

        return await self._mcp_exchange(
            ctx,
            server,
            registry,
            session,
            message,
            on_response=on_response,
            kind="request",
            method_label=method,
        )

    async def _mcp_apply_block(
        self, ctx: RequestContext, blocked: MCPToolBlocked
    ) -> None:
        """Record a policy denial: the row's status/error_message and
        the mcp.tool.blocked event (observer-only — the in-band JSON-RPC
        error is the decision; handlers watch, they don't veto)."""
        ctx.status = "blocked"
        ctx.error_message = blocked.reason
        await self._emit(
            "mcp.tool.blocked",
            ctx,
            server=ctx.model,
            tool=blocked.tool,
            code=blocked.code,
            guardrail=blocked.guardrail,
            method=blocked.method,
        )

    async def _mcp_exchange(
        self,
        ctx: RequestContext,
        server: MCPServerConfig,
        registry: MCPSessionRegistry,
        session: MCPSession,
        message: dict[str, Any],
        on_response: Any = None,
        kind: str | None = None,
        method_label: str | None = None,
    ) -> Response:
        """One request upstream, answered in whatever shape the server
        chose: application/json (intercepted via ``on_response`` and
        re-serialized) or text/event-stream (relayed live, the response
        frame intercepted, everything else VERBATIM). Upstream HTTP
        failures relay status + body unchanged — the client's JSON-RPC
        layer should see the server's own errors — and a 404 (the
        upstream forgot our session) also drops the local mapping so
        the NEXT request 404s at the gateway and the client
        re-initializes."""
        message_id = message.get("id")
        response = await self._mcp_post(ctx, server, session, message, stream=True)
        streamed = False
        try:
            if response.status_code == 404:
                registry.drop(session.gateway_session_id)
                await self._emit(
                    "mcp.session.closed",
                    ctx,
                    server=session.server_name,
                    gateway_session_id=session.gateway_session_id,
                    reason="upstream_gone",
                )
            if "text/event-stream" in response.headers.get("content-type", ""):
                streamed = True
                return StreamingResponse(
                    self._mcp_sse_relay(
                        ctx, response, on_response, message_id, kind, method_label
                    ),
                    media_type="text/event-stream",
                )
            # Everything else is consumed inline. The POST went out with
            # stream=True so the SSE relay could have the raw stream —
            # here that means .content is empty until it is read.
            await response.aread()
            if response.status_code >= 400:
                ctx.status = "error"
                ctx.error_message = f"upstream answered {response.status_code}"
                return Response(
                    status_code=response.status_code,
                    content=response.content,
                    media_type=response.headers.get(
                        "content-type", "application/json"
                    ),
                )
            try:
                body = json.loads(response.content)
            except ValueError:
                ctx.status = "error"
                ctx.error_message = "upstream sent a non-JSON body"
                raise ProviderError(
                    502,
                    f"MCP server '{server.name}' answered with a non-JSON "
                    f"body ({response.headers.get('content-type')}).",
                    "api_error",
                ) from None
            if isinstance(body, dict):
                if "error" in body:
                    ctx.status = "error"
                    ctx.error_message = "upstream returned a JSON-RPC error"
                if on_response is not None:
                    try:
                        body = await on_response(body)
                    except MCPToolBlocked as blocked:
                        await self._mcp_apply_block(ctx, blocked)
                        return JSONResponse(
                            jsonrpc_error(message_id, blocked.code, blocked.reason)
                        )
            if ctx.status != "error":
                ctx.status = "success"
            return JSONResponse(body)
        finally:
            if not streamed:
                with suppress(Exception):
                    await response.aclose()

    async def _mcp_sse_relay(
        self,
        ctx: RequestContext,
        response: httpx.Response,
        on_response: Any,
        message_id: Any,
        kind: str | None,
        method_label: str | None,
    ) -> AsyncIterator[bytes]:
        """Relay the upstream's SSE answer live, in-band: notifications
        and server-initiated requests pass through VERBATIM (raw frame
        bytes), keepalives survive, and THE response frame is handed to
        ``on_response`` — re-serialized when policy rewrites it, or
        replaced by an in-band JSON-RPC error FRAME when policy blocks
        (the client's JSON-RPC layer sees the denial; the stream then
        ends). Server traffic after the response frame belongs to the
        standalone GET stream, which this proxy does not open."""
        reader = SSEReader()
        try:
            async for chunk in response.aiter_bytes():
                for frame in reader.feed(chunk):
                    if frame.data is None:
                        yield frame.raw.encode()
                        continue
                    try:
                        parsed = json.loads(frame.data)
                    except ValueError:
                        yield frame.raw.encode()
                        continue
                    if classify(parsed) != "response":
                        yield frame.raw.encode()
                        continue
                    # THE response frame.
                    rewritten = parsed
                    if on_response is not None:
                        try:
                            rewritten = await on_response(parsed)
                        except MCPToolBlocked as blocked:
                            await self._mcp_apply_block(ctx, blocked)
                            yield sse_frame(
                                jsonrpc_error(message_id, blocked.code, blocked.reason),
                                event=frame.event,
                            )
                            return
                    payload = (
                        json.dumps(rewritten)
                        if isinstance(rewritten, dict)
                        else frame.data
                    )
                    yield (
                        f"event: {frame.event or 'message'}\ndata: {payload}\n\n"
                    ).encode()
                    return
            # The server closed the stream without answering.
            ctx.status = "error"
            ctx.error_message = "upstream SSE stream ended without a JSON-RPC response"
        finally:
            with suppress(Exception):
                await response.aclose()
            await self._mcp_account(ctx, ctx.otel_span, kind, method_label)

    async def _mcp_post(
        self,
        ctx: RequestContext,
        server: MCPServerConfig,
        session: MCPSession | None,
        message: dict[str, Any],
        *,
        stream: bool,
    ) -> httpx.Response:
        """POST one JSON-RPC message upstream. ``stream=True`` returns a
        response whose body must be consumed (aiter_bytes/aread) and
        closed (aclose) by the caller — the SSE relay needs incremental
        chunks; a plain awaited post would buffer the whole stream
        first. Connection failures become the gateway's 502 envelope
        (the documented split: talking-to-the-server is ours, the
        protocol above it is the server's).

        Breaker accounting (tier 66) happens here — every upstream POST
        funnels through this one method, initialize included. A
        CONNECT failure (ConnectError/ConnectTimeout — the server is
        unreachable) or a 5xx answer (up and broken) is one failure; a
        sub-500 answer is a SUCCESS (4xx and JSON-RPC error frames in
        200 bodies are the server answering fine — client-caused, the
        same rule the deployment breaker lives by); latency is NEVER
        evidence (read/write/pool timeouts are not recorded — tools
        legitimately run long). Streaming answers record at header
        time: the connect succeeded, the server spoke — a mid-stream
        death is the relay's problem, not the breaker's."""
        request = self._http_client.build_request(
            "POST",
            server.url,
            content=json.dumps(message),
            headers=upstream_headers(server, session),
        )
        kwargs: dict[str, Any] = {}
        if server.timeout_seconds is not None:
            kwargs["timeout"] = httpx.Timeout(server.timeout_seconds)
        try:
            response = await self._http_client.send(request, stream=stream, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Unreachable — the breaker's evidence.
            await self.mcp_breakers.record_failure(server.name)
            ctx.status = "error"
            ctx.error_message = f"mcp upstream unreachable: {exc}"
            raise ProviderError(
                502,
                f"MCP server '{server.name}' is unreachable: {exc}",
                "api_error",
            ) from exc
        except httpx.HTTPError as exc:
            # Timeout mid-request, pool exhaustion, protocol quirks —
            # latency and local limits, not server health. Never trip
            # on these (the client still gets its 502 envelope).
            ctx.status = "error"
            ctx.error_message = f"mcp upstream unreachable: {exc}"
            raise ProviderError(
                502,
                f"MCP server '{server.name}' is unreachable: {exc}",
                "api_error",
            ) from exc
        # The server answered: 5xx is evidence it is broken, anything
        # less is evidence it is alive.
        if response.status_code >= 500:
            await self.mcp_breakers.record_failure(server.name)
        else:
            await self.mcp_breakers.record_success(server.name)
        return response

    async def _mcp_delete_upstream(
        self, server: MCPServerConfig, session: MCPSession
    ) -> int:
        """The transport's upstream session termination (best-effort
        from the sweep; relayed from DELETE). 405 is a legitimate
        answer (the spec lets servers refuse termination) — the caller
        relays it."""
        request = self._http_client.build_request(
            "DELETE", server.url, headers=upstream_headers(server, session)
        )
        try:
            response = await self._http_client.send(request)
        except httpx.HTTPError as exc:
            raise ProviderError(
                502,
                f"MCP server '{server.name}' is unreachable: {exc}",
                "api_error",
            ) from exc
        return response.status_code

    async def _mcp_account(
        self,
        ctx: RequestContext,
        span: trace.Span,
        kind: str | None,
        method_label: str | None,
    ) -> None:
        """The MCP surface's one teardown — metric, row, slot, gauge,
        span — run exactly once per message (the SSE relay finishes its
        own; every inline path and the exception path run it in
        handle_mcp's finally). Outcome vocabulary: success / error /
        blocked / relayed (notifications and client responses — a 202
        exchange has no result to succeed at)."""
        outcome = "success"
        if ctx.status == "blocked":
            outcome = "blocked"
        elif ctx.mcp_circuit_refused:
            # Tier 66: refused by the per-server breaker — nothing went
            # upstream, and neither "error" (nothing was attempted) nor
            # "relayed" says what happened.
            outcome = "circuit_open"
        elif kind in ("notification", "response") and ctx.status == "success":
            outcome = "relayed"
        elif ctx.status == "error" or (ctx.status == "success" and ctx.error_message):
            outcome = "error"
        metrics.MCP_REQUESTS.labels(
            server=ctx.model, method=method_label or "unknown", outcome=outcome
        ).inc()
        if ctx.status == "success" and ctx.error_message:
            ctx.status = "error"
        try:
            try:
                await self._log(ctx, "mcp request completed")
            except Exception:  # noqa: BLE001
                logger.exception("mcp log write failed")
        finally:
            self._release_slot(ctx)
            metrics.INFLIGHT.dec()
            if span is not None:
                self._annotate_span(span, ctx)
                if ctx.status in ("error", "blocked"):
                    span.set_status(
                        Status(StatusCode.ERROR, ctx.error_message or ctx.status)
                    )
                else:
                    span.set_status(Status(StatusCode.OK))
                span.end()

    async def _mcp_sweep_one(
        self, session: MCPSession, mcp_config: "MCPConfig"
    ) -> None:
        """Post-eviction work for one TTL-expired session (the lifespan
        sweep loop): the closed event, and a best-effort upstream DELETE
        when the server still exists — a server removed by hot-reload
        leaves its session to the server's own TTL."""
        await self._emit(
            "mcp.session.closed",
            None,
            server=session.server_name,
            gateway_session_id=session.gateway_session_id,
            reason="ttl",
        )
        if self._http_client is None or session.upstream_session_id is None:
            return
        server = mcp_config.server(session.server_name)
        if server is None:
            return
        with suppress(Exception):
            await self._mcp_delete_upstream(server, session)

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    async def _serve_simple(
        self,
        model: str,
        request_body: dict,
        auth: AuthContext,
        request_id: str | None,
        span_name: str,
        event: str,
        inner: Callable[[RequestContext], Awaitable[Response]],
    ) -> Response:
        """Shared wrapper for the non-chat surfaces (embeddings, images,
        audio): context, in-flight metric, span, error-path logging —
        ``inner(ctx)`` runs the modality's own pipeline phase."""
        ctx = RequestContext(
            request_id=request_id or f"req_{uuid.uuid4().hex}",
            auth=auth,
            model=model,
            stream=False,
            request_body=request_body,
        )
        metrics.INFLIGHT.inc()
        span = self._tracer.start_span(span_name, kind=SpanKind.INTERNAL)
        ctx.otel_span = span
        span_context = span.get_span_context()
        if span_context.is_valid:
            ctx.trace_id = format(span_context.trace_id, "032x")
            ctx.span_id = format(span_context.span_id, "016x")
        span.set_attribute("gateway.request_id", ctx.request_id)
        span.set_attribute("gateway.model", model)
        span.set_attribute("gateway.key_name", ctx.auth.key_name or "admin")
        with use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                # Lifecycle event: same contract as chat — the one
                # in-band veto point before the request starts.
                await self._emit(
                    "request.received",
                    ctx,
                    model=model,
                    key_name=ctx.auth.key_name,
                )
                response = await inner(ctx)
            except GatewayError as exc:
                ctx.status = "error"
                ctx.error_message = exc.message
                self._request_metric(ctx)
                await self._log(ctx, event=event)
                self._finish_span(ctx, exc)
                await self._emit(
                    "request.rejected" if exc.status_code < 500 else "request.failed",
                    ctx,
                    error=exc.message,
                    status_code=exc.status_code,
                )
                raise
            except Exception as exc:  # noqa: BLE001
                ctx.status = "error"
                ctx.error_message = f"internal error: {exc}"
                self._request_metric(ctx)
                await self._log(ctx, event=event)
                self._finish_span(ctx, exc)
                await self._emit("request.failed", ctx, error=ctx.error_message)
                raise
            if isinstance(response, StreamingResponse):
                # The span and the in-flight gauge are finished by the
                # stream wrapper once the response body completes (streamed
                # speech does this — see _handle_speech).
                return response
            self._finish_span(ctx)
            return response

    async def handle_embedding(
        self, request: EmbeddingRequest, auth: AuthContext, request_id: str | None = None
    ) -> Response:
        """POST /v1/embeddings — same policy pipeline as chat, minus the
        chat-specific parts (guardrails, streaming).

        Guardrails are not applied to embedding inputs in this version: the
        guardrail phases are typed over chat messages (see ARCHITECTURE §4.1).
        Rate limits, budgets, model allowlists, caching, retries, fallbacks,
        circuit breakers, cost tracking and logging all apply unchanged.
        """
        return await self._serve_simple(
            request.model,
            request.model_dump(exclude_none=True),
            auth,
            request_id,
            "gateway.embedding",
            "embedding request completed",
            lambda ctx: self._handle_embedding(request, ctx),
        )

    async def _handle_embedding(
        self, request: EmbeddingRequest, ctx: RequestContext
    ) -> Response:
        # 0. Model aliases / default routing (same rule as chat, incl. the
        # calling key's model_mappings/default_model)
        resolved = self._resolve_model(request.model, ctx.auth)
        if resolved != request.model:
            request = request.model_copy(update={"model": resolved})
            ctx.model = resolved
            if ctx.otel_span is not None:
                ctx.otel_span.set_attribute("gateway.model", resolved)

        # 1. Model permission: key allowlist (platform admins bypass
        # inside) + the tenant's allowlist, which binds every member.
        self._check_model_permission(ctx.auth, request.model)

        # 2. Rate limit (pre-flight, ~chars/4 per input)
        inputs = [request.input] if isinstance(request.input, str) else request.input
        await self._check_rate_limit(ctx, sum(estimate_tokens(t) for t in inputs))

        # 3. Budget (pre-flight)
        self._check_budget(ctx)

        # 4. Cache lookup — embeddings are deterministic per model+input.
        #    Salted by the caller's residency requirement like the chat
        #    path's policy token: residency classes never share entries.
        #    Per-key cache policy (tier 54) gates here too: an opted-out
        #    caller skips the lookup and the store, and its window is
        #    the read-side freshness bound (see the chat path, phase 5).
        cache_active = self.cache.enabled and ctx.auth.cache_enabled is not False
        key = (
            embedding_cache_key_for(
                request, ctx.auth.tenant_id, ctx.auth.effective_residencies()
            )
            if cache_active
            else None
        )
        if key is not None:
            cached = await self.cache.get(key, max_age=ctx.auth.cache_ttl_seconds)
            if cached is not None:
                metrics.CACHE.labels("hit").inc()
                ctx.cache_hit = True
                usage = cached.get("usage") or {}
                ctx.prompt_tokens = usage.get("prompt_tokens")
                ctx.provider = "cache"
                ctx.status = "success"
                self._request_metric(ctx)
                await self._log(ctx, event="embedding request completed")
                return JSONResponse(
                    content=self._embedding_out(request, cached),
                    headers=self._headers(ctx, "HIT"),
                )
            metrics.CACHE.labels("miss").inc()

        # 5. Route to provider (retries/fallbacks/breakers as for chat).
        #    Residency requirement filters the ladder, same as chat.
        deployment, response = await self.router.embed(
            request, ctx.auth.effective_residencies(), auth=ctx.auth
        )
        ctx.provider = deployment.provider
        ctx.deployment = deployment.name
        ctx.native_model = deployment.model
        await self._note_byok(ctx, deployment)

        # 6. Accounting
        usage = response.get("usage") or {}
        await self._account_usage(ctx, usage, deployment.model)
        ctx.response_body = response
        ctx.status = "success"

        if key is not None:
            # The writer's window stamps the entry (tier 54).
            await self.cache.put(key, response, ttl_seconds=ctx.auth.cache_ttl_seconds)

        self._request_metric(ctx)
        metrics.LATENCY.labels(provider=ctx.provider or "none").observe(
            ctx.latency_ms() / 1000
        )
        await self._log(ctx, event="embedding request completed")
        await self._emit(
            "response.completed",
            ctx,
            response=response,
            usage=usage,
            cost_usd=ctx.cost_usd,
            surface="embedding",
        )
        return JSONResponse(
            content=self._embedding_out(request, response),
            headers=self._headers(
                ctx,
                "BYPASS"
                if self.cache.enabled and ctx.auth.cache_enabled is False
                else "MISS",
            ),
        )

    def _embedding_out(self, request: EmbeddingRequest, response: dict) -> dict:
        """Apply the client's encoding_format to the canonical float form."""
        if request.encoding_format == "base64":
            return _encode_embeddings_base64(response)
        return response

    # ------------------------------------------------------------------
    # Image generation / audio (same pre-flight checks as embeddings:
    # guardrails are chat-typed and do not run; no caching — image and
    # speech generation are non-deterministic, transcription is cheap
    # relative to its upload; retries/fallbacks/breakers/cost/logging
    # all apply unchanged)
    # ------------------------------------------------------------------

    async def handle_image_generation(
        self,
        request: ImageGenerationRequest,
        auth: AuthContext,
        request_id: str | None = None,
    ) -> Response:
        """POST /v1/images/generations — OpenAI Images API."""
        return await self._serve_simple(
            request.model,
            request.model_dump(exclude_none=True),
            auth,
            request_id,
            "gateway.image_generation",
            "image generation completed",
            lambda ctx: self._serve_image(
                ctx,
                request,
                lambda r: self.router.generate_image(
                    r, residencies=ctx.auth.effective_residencies(), auth=ctx.auth
                ),
                request.prompt,
                "image generation completed",
            ),
        )

    async def handle_image_edit(
        self,
        request: ImageEditRequest,
        auth: AuthContext,
        request_id: str | None = None,
    ) -> Response:
        """POST /v1/images/edits — OpenAI image edits (multipart). The log's
        request_body carries metadata only (image names + sizes, mask name,
        never the bytes)."""
        return await self._serve_simple(
            request.model,
            _image_edit_log_body(request),
            auth,
            request_id,
            "gateway.image_edit",
            "image edit completed",
            lambda ctx: self._serve_image(
                ctx,
                request,
                lambda r: self.router.edit_image(
                    r, residencies=ctx.auth.effective_residencies(), auth=ctx.auth
                ),
                request.prompt,
                "image edit completed",
            ),
        )

    async def handle_image_variation(
        self,
        request: ImageVariationRequest,
        auth: AuthContext,
        request_id: str | None = None,
    ) -> Response:
        """POST /v1/images/variations — OpenAI image variations (multipart,
        no prompt; DALL·E 2 on OpenAI/Azure)."""
        return await self._serve_simple(
            request.model,
            _image_edit_log_body(request),
            auth,
            request_id,
            "gateway.image_variation",
            "image variation completed",
            lambda ctx: self._serve_image(
                ctx,
                request,
                lambda r: self.router.vary_image(
                    r, residencies=ctx.auth.effective_residencies(), auth=ctx.auth
                ),
                None,
                "image variation completed",
            ),
        )

    async def _serve_image(
        self,
        ctx: RequestContext,
        request: ImageGenerationRequest | ImageEditRequest | ImageVariationRequest,
        dispatch: Callable[[Any], Awaitable[tuple[Any, dict]]],
        prompt: str | None,
        event: str,
    ) -> Response:
        """Shared image-pipeline tail: resolve → permission → rate limit →
        budget → dispatch (per-surface router call) → per-image cost →
        log → JSON response."""
        resolved = self._resolve_model(request.model, ctx.auth)
        if resolved != request.model:
            request = request.model_copy(update={"model": resolved})
            ctx.model = resolved
            if ctx.otel_span is not None:
                ctx.otel_span.set_attribute("gateway.model", resolved)

        # Model permission: key allowlist (platform admins bypass
        # inside) + the tenant's allowlist, which binds every member.
        self._check_model_permission(ctx.auth, request.model)

        await self._check_rate_limit(ctx, estimate_tokens(prompt or ""))
        self._check_budget(ctx)

        deployment, response = await dispatch(request)
        ctx.provider = deployment.provider
        ctx.deployment = deployment.name
        ctx.native_model = deployment.model
        await self._note_byok(ctx, deployment)

        images = len(response.get("data") or []) or max(1, request.n)
        cost = self.cost_calculator.calculate_images(
            deployment.model, images
        ) or self.cost_calculator.calculate_images(ctx.model, images)
        await self._account_cost(ctx, cost, prompt_tokens=estimate_tokens(prompt or ""))
        ctx.response_body = response
        ctx.status = "success"

        self._request_metric(ctx)
        metrics.LATENCY.labels(provider=ctx.provider or "none").observe(
            ctx.latency_ms() / 1000
        )
        await self._log(ctx, event=event)
        await self._emit(
            "response.completed",
            ctx,
            response=response,
            cost_usd=ctx.cost_usd,
            surface="images",
        )
        return JSONResponse(content=response, headers=self._headers(ctx, "NONE"))

    async def handle_transcription(
        self,
        request: TranscriptionRequest,
        auth: AuthContext,
        request_id: str | None = None,
    ) -> Response:
        """POST /v1/audio/transcriptions + /v1/audio/translations (multipart).

        The log's request_body carries metadata only (file name + size,
        never the audio bytes). Cost is duration-based when the provider
        reports a duration (``verbose_json``); audio bytes deliberately do
        not count as text tokens for TPM — RPM still applies.
        """
        body = {
            "model": request.model,
            "endpoint": request.endpoint,
            "language": request.language,
            "prompt": request.prompt,
            "response_format": request.response_format,
            "file_name": request.file_name,
            "file_size_bytes": len(request.file),
        }
        return await self._serve_simple(
            request.model,
            {k: v for k, v in body.items() if v is not None},
            auth,
            request_id,
            "gateway.transcription",
            "audio transcription completed",
            lambda ctx: self._handle_transcription(request, ctx),
        )

    async def _handle_transcription(
        self, request: TranscriptionRequest, ctx: RequestContext
    ) -> Response:
        resolved = self._resolve_model(request.model, ctx.auth)
        if resolved != request.model:
            request = request.model_copy(update={"model": resolved})
            ctx.model = resolved
            if ctx.otel_span is not None:
                ctx.otel_span.set_attribute("gateway.model", resolved)

        # Model permission: key allowlist (platform admins bypass
        # inside) + the tenant's allowlist, which binds every member.
        self._check_model_permission(ctx.auth, request.model)

        await self._check_rate_limit(ctx, estimate_tokens(request.prompt or ""))
        self._check_budget(ctx)

        deployment, (content, media_type) = await self.router.transcribe(
            request, ctx.auth.effective_residencies(), auth=ctx.auth
        )
        ctx.provider = deployment.provider
        ctx.deployment = deployment.name
        ctx.native_model = deployment.model
        await self._note_byok(ctx, deployment)

        duration = content.get("duration") if isinstance(content, dict) else None
        minutes = float(duration) / 60.0 if duration else 0.0
        cost = self.cost_calculator.calculate_audio(
            deployment.model, minutes
        ) or self.cost_calculator.calculate_audio(ctx.model, minutes)
        text = (
            content.get("text") if isinstance(content, dict) else content
        ) or ""
        await self._account_cost(ctx, cost, completion_tokens=estimate_tokens(text))
        ctx.response_body = content if isinstance(content, dict) else {"text": text}
        ctx.status = "success"

        self._request_metric(ctx)
        metrics.LATENCY.labels(provider=ctx.provider or "none").observe(
            ctx.latency_ms() / 1000
        )
        await self._log(ctx, event="audio transcription completed")
        await self._emit(
            "response.completed",
            ctx,
            response=ctx.response_body,
            cost_usd=ctx.cost_usd,
            surface="transcription",
        )
        if isinstance(content, dict):
            return JSONResponse(content=content, headers=self._headers(ctx, "NONE"))
        return Response(content=content, media_type=media_type, headers=self._headers(ctx, "NONE"))

    async def handle_speech(
        self,
        request: SpeechRequest,
        auth: AuthContext,
        request_id: str | None = None,
    ) -> Response:
        """POST /v1/audio/speech — OpenAI Speech API (binary audio out)."""
        return await self._serve_simple(
            request.model,
            request.model_dump(exclude_none=True),
            auth,
            request_id,
            "gateway.speech",
            "speech synthesis completed",
            lambda ctx: self._handle_speech(request, ctx),
        )

    async def _handle_speech(
        self, request: SpeechRequest, ctx: RequestContext
    ) -> Response:
        resolved = self._resolve_model(request.model, ctx.auth)
        if resolved != request.model:
            request = request.model_copy(update={"model": resolved})
            ctx.model = resolved
            if ctx.otel_span is not None:
                ctx.otel_span.set_attribute("gateway.model", resolved)

        # Model permission: key allowlist (platform admins bypass
        # inside) + the tenant's allowlist, which binds every member.
        self._check_model_permission(ctx.auth, request.model)

        await self._check_rate_limit(ctx, estimate_tokens(request.input))
        self._check_budget(ctx)

        deployment, (audio, media_type) = await self.router.speak(
            request, ctx.auth.effective_residencies(), auth=ctx.auth
        )
        ctx.provider = deployment.provider
        ctx.deployment = deployment.name
        ctx.native_model = deployment.model
        await self._note_byok(ctx, deployment)

        cost = self.cost_calculator.calculate_speech(
            deployment.model, len(request.input)
        ) or self.cost_calculator.calculate_speech(ctx.model, len(request.input))
        # Known before the stream starts (char-based): the response header
        # can carry it even though the actual accounting waits for the
        # body to complete.
        ctx.cost_usd = cost
        prompt_tokens = estimate_tokens(request.input)
        headers = self._headers(ctx, "NONE")

        async def _stream_audio() -> AsyncIterator[bytes]:
            """Stream the provider's audio as it arrives; account, log and
            end the span once the body completes (the same shape as the
            chat stream wrapper — the finally-path also runs on client
            disconnects, so spend is never lost)."""
            span = ctx.otel_span
            total = 0
            first = True
            with (
                use_span(
                    span,
                    end_on_exit=True,
                    record_exception=False,
                    set_status_on_exception=False,
                )
                if span is not None
                else nullcontext()
            ):
                try:
                    async for chunk in audio:
                        if first:
                            first = False
                            metrics.TTFT.labels(model=ctx.model).observe(
                                time.monotonic() - ctx.started_monotonic
                            )
                        total += len(chunk)
                        yield chunk
                except GatewayError as exc:
                    # The response is already committed (200 + audio);
                    # HTTP cannot change status mid-body — mark, log, abort.
                    ctx.status = "error"
                    ctx.error_message = exc.message
                    raise
                finally:
                    metrics.INFLIGHT.dec()
                    await self._account_cost(ctx, cost, prompt_tokens=prompt_tokens)
                    # Binary audio: the log records a note about it, never
                    # the bytes.
                    ctx.response_body = {
                        "note": "binary audio response",
                        "media_type": media_type,
                        "bytes": total,
                    }
                    if ctx.status not in ("error", "blocked"):
                        ctx.status = "success"
                    self._request_metric(ctx)
                    metrics.LATENCY.labels(provider=ctx.provider or "none").observe(
                        ctx.latency_ms() / 1000
                    )
                    await self._log(ctx, event="speech synthesis completed")
                    if span is not None:
                        self._annotate_span(span, ctx)
                        if ctx.status in ("error", "blocked"):
                            span.set_status(
                                Status(StatusCode.ERROR, ctx.error_message or ctx.status)
                            )
                        else:
                            span.set_status(Status(StatusCode.OK))

        return StreamingResponse(
            _stream_audio(),
            media_type=media_type,
            headers=headers,
        )

    def _billed_cost(self, ctx: RequestContext, provider_cost: float) -> float:
        """The caller-billed cost: provider cost plus the tenant's
        pricing markup (a percent, snapshotted at auth time like the
        other tenant ceilings). This is the chargeback number — what
        spend, budgets, log rows, exports and the per-key metrics
        record — while ``gateway_cost_usd_total{model}`` keeps the
        ACTUAL provider cost, so platform economics stay visible
        beside tenant economics. Platform keys (no tenant) and tenants
        without a markup bill at actual cost."""
        pct = ctx.auth.tenant_pricing_markup_pct
        if not pct:
            return provider_cost
        billed = provider_cost * (1.0 + pct / 100.0)
        markup = billed - provider_cost
        if markup > 0 and ctx.auth.tenant_name:
            metrics.MARKUP_USD.labels(tenant=ctx.auth.tenant_name).inc(markup)
        return round(billed, 8)

    async def _account_cost(
        self,
        ctx: RequestContext,
        cost: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """Accounting for the non-token modalities (images, audio): the cost
        comes from unit prices (per image / per minute / per 1M chars), the
        token counters are estimates feeding the log row and TPM. Mirrors
        ``_account_usage`` minus the usage-dict path."""
        ctx.prompt_tokens = prompt_tokens or ctx.prompt_tokens
        ctx.completion_tokens = completion_tokens or ctx.completion_tokens
        if prompt_tokens or completion_tokens:
            metrics.TOKENS.labels("prompt").inc(prompt_tokens)
            metrics.TOKENS.labels("completion").inc(completion_tokens)
            if self.config.metrics.per_key:
                key = self._key_labeler.label(ctx.auth.key_name)
                metrics.KEY_TOKENS.labels(key=key, type="prompt").inc(prompt_tokens)
                metrics.KEY_TOKENS.labels(key=key, type="completion").inc(
                    completion_tokens
                )
            if ctx.auth.key_id is not None and not ctx.cache_hit:
                await self.rate_limiter.record_usage(
                    ctx.auth.key_id, prompt_tokens + completion_tokens
                )
        # Billed cost (tenant markup included) is what the log row,
        # spend and the per-key chargeback metrics record; the
        # model-labeled cost series stays ACTUAL provider cost.
        ctx.cost_usd = self._billed_cost(ctx, cost)
        metrics.COST.labels(model=ctx.model).inc(cost)
        if self.config.metrics.per_key:
            metrics.KEY_COST.labels(
                key=self._key_labeler.label(ctx.auth.key_name)
            ).inc(ctx.cost_usd)
        self._queue_spend(ctx, ctx.cost_usd)

    # ------------------------------------------------------------------
    # Streaming machinery
    # ------------------------------------------------------------------

    async def _stream_with_guardrails(
        self,
        request: ChatCompletionRequest,
        ctx: RequestContext,
        chunk_iterator: AsyncIterator[dict],
        cache_key: str | None,
        semantic_embedding: list[float] | None = None,
        policy_token: str | None = None,
    ) -> AsyncIterator[str]:
        """Wrap provider chunks with stream guardrails + rewriting + accounting + logging.

        Block-style stream guardrails (``check_stream_delta``) see the
        *source* text from the provider. Rewriting guardrails
        (``rewrite_stream_text``) run after them and change what the client
        receives: held-back text (a match that might still grow) flushes
        right before the finish chunk, and again at the iterator's end —
        so nothing a rewriter wants masked ever reaches the client, no
        matter how the provider chunked the output.
        """
        include_usage_requested = bool((request.stream_options or {}).get("include_usage"))
        accumulated_text = ""  # source text (what the model said)
        emitted_text = ""  # client text (after stream rewriting)
        finish_reason: str | None = None
        usage: dict | None = None
        blocked = False
        rewrite_state = self.guardrails.new_stream_rewrite_state(request, ctx)
        # TTFT bookkeeping: the first chunk received off the wire (any
        # chunk — role chunks arrive back-to-back with content) and
        # the first chunk actually carrying content/audio (the token
        # the client perceives generation starting at).
        first_chunk_at: float | None = None
        first_token_at: float | None = None

        try:
            async for chunk in chunk_iterator:
                if first_chunk_at is None:
                    first_chunk_at = time.monotonic()
                if isinstance(chunk.get("usage"), dict) and not chunk.get("choices"):
                    # provider/translator usage summary chunk
                    usage = chunk["usage"]
                    if include_usage_requested:
                        yield _sse(chunk)
                    continue

                choices = chunk.get("choices") or []
                if not choices:
                    yield _sse(chunk)
                    continue
                choice = choices[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}
                delta_text = delta.get("content") or ""
                # Audio-output chunks carry the spoken words in
                # delta.audio.transcript (content stays null) — guardrails
                # must see them and the log's emitted text must include
                # them. Rewriting still targets content only: audio
                # bytes are never rewritten.
                transcript = str((delta.get("audio") or {}).get("transcript") or "")
                guardrail_text = delta_text or transcript
                if guardrail_text and first_token_at is None:
                    first_token_at = time.monotonic()

                if guardrail_text:
                    # Stream guardrails inspect what has been sent so far
                    # (source text — block checks see what the model said,
                    # not the rewrite).
                    accumulated_text += guardrail_text
                    await self.guardrails.run_stream_phase(
                        request, accumulated_text, guardrail_text, ctx
                    )

                if rewrite_state is not None:
                    if delta_text:
                        out = await self.guardrails.run_stream_rewrite(
                            request, ctx, rewrite_state, delta_text
                        )
                        emitted_text += out
                        if (
                            out == ""
                            and not (delta.keys() - {"content"})
                            and not choice.get("finish_reason")
                        ):
                            continue  # fully held back — nothing to emit
                        if out != delta_text:
                            chunk = {
                                **chunk,
                                "choices": [{**choice, "delta": {**delta, "content": out}}],
                            }
                    if choice.get("finish_reason"):
                        # Flush everything rewriters held back *before* the
                        # finish signal reaches the client (final=True —
                        # no more text will arrive; redact all matches).
                        flushed = await self.guardrails.run_stream_rewrite(
                            request, ctx, rewrite_state, "", final=True
                        )
                        if flushed:
                            emitted_text += flushed
                            yield _sse(self._delta_chunk(request.model, flushed))
                elif delta_text:
                    emitted_text += delta_text
                elif transcript:
                    emitted_text += transcript

                yield _sse(chunk)

            if rewrite_state is not None:
                # End of the provider stream without a finish chunk (or a
                # rewriter still holding text despite the final flush —
                # this one is idempotent). Flush, then record verdicts.
                flushed = await self.guardrails.run_stream_rewrite(
                    request, ctx, rewrite_state, "", final=True
                )
                if flushed:
                    emitted_text += flushed
                    yield _sse(self._delta_chunk(request.model, flushed))
                self.guardrails.record_stream_rewrite(ctx, rewrite_state)

            yield "data: [DONE]\n\n"
        except GuardrailBlocked as exc:
            blocked = True
            ctx.status = "blocked"
            ctx.error_message = exc.message
            yield _sse(self._content_filter_chunk(request.model))
            yield "data: [DONE]\n\n"
        except ProviderError as exc:
            ctx.status = "error"
            ctx.error_message = exc.message
            yield _sse(
                {
                    "error": {
                        "message": exc.message,
                        "type": exc.error_type,
                        "param": exc.param,
                        "code": exc.code,
                    }
                }
            )
            yield "data: [DONE]\n\n"
        finally:
            # TTFT: first content-bearing chunk, falling back to the
            # first chunk at all (a stream that never carries content —
            # blocked or errored — still measured to its first byte;
            # streams that never yielded stay None).
            if first_token_at is not None or first_chunk_at is not None:
                ctx.ttft_ms = int(
                    ((first_token_at or first_chunk_at) - ctx.started_monotonic) * 1000
                )
            if not blocked and ctx.status != "blocked":
                ctx.status = "success" if ctx.error_message is None else ctx.status
            # Mirror to shadow deployments once the stream has completed
            # successfully (never for blocked/errored streams; cache hits
            # replayed earlier never reach here). accumulated_text is the
            # SOURCE text (line 2560) — what the model said, pre-rewrite —
            # captured by value here so the mirror task (tier 35) scores
            # raw-vs-raw no matter when the loop schedules it; emitted_text
            # is only what the client saw.
            if not blocked and ctx.status == "success":
                self._queue_shadow(request, ctx, accumulated_text)
                self._queue_prompt_ab(request, ctx, accumulated_text)
            if usage is not None:
                await self._account_usage(
                    ctx, usage, ctx.native_model or ctx.model, spend=True
                )
            # The response body / cache entry record what the CLIENT saw
            # (rewritten text), not the provider's source text.
            ctx.response_body = {
                "content": emitted_text,
                "finish_reason": finish_reason,
                "usage": usage,
            }
            self._request_metric(ctx)
            metrics.LATENCY.labels(provider=ctx.provider or "none").observe(
                ctx.latency_ms() / 1000
            )
            if not blocked and usage is not None:
                full_response = self._synthesized_response(
                    request.model, emitted_text, finish_reason, usage
                )
                if cache_key is not None:
                    # The writer's window stamps the entry (tier 54) —
                    # the streaming path stores exactly like the
                    # non-streaming one.
                    await self.cache.put(
                        cache_key,
                        full_response,
                        ttl_seconds=ctx.auth.cache_ttl_seconds,
                    )
                if semantic_embedding is not None and self.semantic is not None:
                    await self.semantic.put(
                        request.model,
                        request,
                        semantic_embedding,
                        full_response,
                        policy_token or "",
                        ttl_seconds=ctx.auth.cache_ttl_seconds,
                    )
                # Idempotency: same success rule as the cache (a complete,
                # unblocked stream with usage) — a client that retries a
                # disconnected stream re-executes rather than replaying
                # a half-finished one.
                if ctx.idempotency_key is not None and self.idempotency is not None:
                    await self.idempotency.store(
                        ctx.idempotency_key,
                        {
                            "response": full_response,
                            "fingerprint": ctx.idempotency_fingerprint,
                        },
                    )
                    metrics.IDEMPOTENCY.labels("stored").inc()
            # Release in-flight idempotency waiters on every stream exit —
            # success, block, mid-stream error, client disconnect alike.
            if ctx.idempotency_key is not None and self.idempotency is not None:
                self.idempotency.finish(ctx.idempotency_key)
            await self._log(ctx)

    async def _replay_cached_stream(self, cached: dict) -> AsyncIterator[str]:
        for chunk in ResponseCache.response_for_stream_replay(cached):
            yield _sse(chunk)
        yield "data: [DONE]\n\n"

    def _streaming_response(
        self, body: AsyncIterator[str], ctx: RequestContext, headers: dict[str, str]
    ) -> StreamingResponse:
        async def stream_body() -> AsyncIterator[str]:
            span = ctx.otel_span
            # The gateway.chat span stays open for the whole stream and is
            # ended here — including on client disconnects and cancellation,
            # because this finally-path always runs when the generator closes.
            with (
                use_span(
                    span,
                    end_on_exit=True,
                    record_exception=False,
                    set_status_on_exception=False,
                )
                if span is not None
                else nullcontext()
            ):
                first = True
                try:
                    async for chunk in body:
                        if first:
                            first = False
                            await self._emit("stream.started", ctx, model=ctx.model)
                            metrics.TTFT.labels(model=ctx.model).observe(
                                time.monotonic() - ctx.started_monotonic
                            )
                        yield chunk
                finally:
                    metrics.INFLIGHT.dec()
                    # The stream held its concurrency slot until the body
                    # completed (a slow reader occupies upstream capacity
                    # for exactly as long as it reads).
                    self._release_slot(ctx)
                    if span is not None:
                        self._annotate_span(span, ctx)
                        if ctx.status in ("error", "blocked"):
                            span.set_status(
                                Status(StatusCode.ERROR, ctx.error_message or ctx.status)
                            )
                        else:
                            span.set_status(Status(StatusCode.OK))
                    # Observer-only by design: bytes may already be with
                    # the client, so there is nothing left to veto —
                    # ctx.status says how the stream ended.
                    await self._emit("stream.completed", ctx, status=ctx.status)

        return StreamingResponse(
            stream_body(),
            media_type=STREAM_MEDIA_TYPE,
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                **headers,
            },
        )

    # ------------------------------------------------------------------
    # Pre-flight checks
    # ------------------------------------------------------------------

    def _resolve_model(self, model: str, auth: AuthContext | None = None) -> str:
        """Normalize the requested model name to a public model name.

        Precedence, most specific first:

        1. the calling key's ``model_mappings`` — an explicit admin
           decision for this key, which wins even over real public model
           names (that is what per-team steering means);
        2. a configured public model (an alias with the same name is
           ignored);
        3. the global ``model_aliases``;
        4. the calling key's ``default_model`` catch-all;
        5. the global ``default_model`` catch-all;

        otherwise the name passes through unchanged (and fails
        resolution in the router with a clear 404). A mapping to a
        name that is not a configured model behaves like a broken
        global alias: the target is what the request 404s on.
        """
        if auth is not None and auth.model_mappings and model in auth.model_mappings:
            return auth.model_mappings[model]
        if model in self._model_names:
            return model
        if model in self.config.model_aliases:
            return self.config.model_aliases[model]
        if auth is not None and auth.default_model:
            return auth.default_model
        return self.config.default_model or model

    def _check_model_permission(self, auth: AuthContext, model: str) -> None:
        """Model allowlists. Two layers:

        - the key's own ``allowed_models`` — platform admins bypass it
          (they are the operators); tenant admins do not, see below;
        - the tenant's ``allowed_models`` — applies to every member of
          the tenant, tenant admins included (a tenant admin manages
          that tenant's keys, not the tenant's policy ceiling), and
          only narrows: the model must pass both lists.
        """
        import fnmatch

        if not auth.is_admin:
            patterns = auth.allowed_models
            if patterns and not any(
                fnmatch.fnmatch(model, pattern) for pattern in patterns
            ):
                raise ModelNotAllowedError(model)
        # The tenant layer has no admin bypass — that is the point of
        # tenancy: the ceiling binds everyone the tenant contains.
        tenant_patterns = auth.tenant_allowed_models
        if auth.tenant_id is not None and tenant_patterns:
            if not any(fnmatch.fnmatch(model, pattern) for pattern in tenant_patterns):
                raise ModelNotAllowedError(model)

    @staticmethod
    def _tool_names(request: "ChatCompletionRequest") -> list[str]:
        """Names of the tools a request declares. OpenAI shape (every
        inbound surface normalizes to it): ``function`` tools carry the
        name on ``function.name``; custom tool types may carry a
        top-level ``name`` extra. Unnameable tools surface as
        "<unnamed>" so a policy still fails closed against them."""
        names: list[str] = []
        for tool in request.tools or []:
            if tool.function is not None and tool.function.name:
                names.append(tool.function.name)
                continue
            extra = tool.model_extra or {}
            name = extra.get("name")
            names.append(str(name) if name else "<unnamed>")
        return names

    def _tool_name_allowed(self, name: str, ctx: RequestContext) -> bool:
        """The single-tool predicate under the tool policy (tiers 30 +
        33): platform admins bypass; otherwise the name must match the
        key's ``allowed_tools`` patterns AND the tenant's pooled
        patterns (either None = that layer sets no restriction; a
        tenant can only narrow). Shared by the chat surface — which
        checks a request's DECLARED tools in one batch, with the
        two-layer error reporting preserved in ``_check_tool_policy`` —
        and the MCP proxy, which needs a per-name verdict: tools/list
        filters the catalog to the names this key could actually call,
        tools/call admits or denies one name at a time."""
        import fnmatch

        if ctx.auth.is_admin:
            return True
        patterns = ctx.auth.allowed_tools
        if patterns and not any(fnmatch.fnmatch(name, p) for p in patterns):
            return False
        tenant_patterns = ctx.auth.tenant_allowed_tools
        if ctx.auth.tenant_id is not None and tenant_patterns:
            if not any(fnmatch.fnmatch(name, p) for p in tenant_patterns):
                return False
        return True

    def _check_tool_policy(
        self, ctx: RequestContext, request: "ChatCompletionRequest"
    ) -> None:
        """Tool-call policy (tier 30, MCP-lite): which tool names a
        request may DECLARE. Same two-layer shape as model permission —
        the key's ``allowed_tools`` (platform admins bypass; they are
        the operators) and the tenant's pooled ``allowed_tools`` (no
        bypass; binds every member — the tenant can only narrow). A
        tool must match BOTH lists. None = no restriction: callers
        without the field anywhere are completely unchanged. Requests
        with no tools pass trivially — the check costs two fnmatch
        lists against an empty set.

        Fail-closed detail: a tool whose name cannot be extracted is
        treated as non-compliant — a policy the request can't prove it
        passes is a policy it fails.
        """
        import fnmatch

        names = self._tool_names(request)
        if not names:
            return
        if not ctx.auth.is_admin:
            patterns = ctx.auth.allowed_tools
            if patterns and not all(
                any(fnmatch.fnmatch(name, pattern) for pattern in patterns)
                for name in names
            ):
                raise ToolsNotAllowedError(
                    [
                        name
                        for name in names
                        if not any(
                            fnmatch.fnmatch(name, pattern) for pattern in patterns
                        )
                    ]
                )
        # The tenant layer has no admin bypass — same as models.
        tenant_patterns = ctx.auth.tenant_allowed_tools
        if ctx.auth.tenant_id is not None and tenant_patterns:
            if not all(
                any(fnmatch.fnmatch(name, pattern) for pattern in tenant_patterns)
                for name in names
            ):
                raise ToolsNotAllowedError(
                    [
                        name
                        for name in names
                        if not any(
                            fnmatch.fnmatch(name, pattern)
                            for pattern in tenant_patterns
                        )
                    ]
                )

    async def _check_runaway(
        self, ctx: RequestContext, request: ChatCompletionRequest
    ) -> None:
        """Runaway-agent loop detection (pre-flight, chat-family). A key
        in a runaway cooldown fails fast with Retry-After — nothing a
        stuck agent sends is useful, not even an idempotent replay or a
        request the response cache would serve free. Otherwise this
        request's fingerprint (the same normalized identity the
        response cache and idempotency key on — identical across the
        chat/responses/native routes) joins the key's loop window; when
        it reaches the configured identical-request threshold inside
        the window, the key is jailed for an escalating cooldown and
        the tripping request is refused."""
        protector = self.runaway
        cfg = self.config.runaway
        key_id = ctx.auth.key_id
        if protector is None or not cfg.enabled or key_id is None:
            return
        allowed, retry_after = await protector.check(key_id)
        if not allowed:
            raise RateLimitError(
                "Runaway-agent protection: this key is in a "
                f"loop-detection cooldown for another {retry_after:.0f}s — "
                "identical-request retry loops are being refused. If this "
                "is legitimate traffic, see the runaway: config section.",
                retry_after=retry_after,
            )
        fingerprint = hashlib.sha256(cache_payload_for(request).encode()).hexdigest()
        count = await protector.observe(
            key_id,
            fingerprint,
            threshold=cfg.identical_request_threshold,
            window_seconds=cfg.identical_request_window_seconds,
        )
        if count is None:
            return
        cooldown, offenses = await protector.trip(
            key_id,
            base_seconds=cfg.cooldown_seconds,
            multiplier=cfg.cooldown_multiplier,
            max_seconds=cfg.cooldown_max_seconds,
            reset_after_seconds=cfg.reset_after_seconds,
        )
        metrics.RUNAWAY.labels(key=self._key_labeler.label(ctx.auth.key_name)).inc()
        # Observer-only lifecycle event: an agent-orchestration layer
        # can alarm (or revoke the key) on it. Deliberately not
        # interceptable — the jail is the decision.
        await self._emit(
            "runaway.trip",
            ctx,
            key_name=ctx.auth.key_name,
            model=ctx.model,
            count=count,
            threshold=cfg.identical_request_threshold,
            window_seconds=cfg.identical_request_window_seconds,
            cooldown_seconds=cooldown,
            offenses=offenses,
        )
        raise RateLimitError(
            "Runaway-agent protection: "
            f"{count} identical requests to model '{ctx.model}' within "
            f"{cfg.identical_request_window_seconds:.0f}s — a stuck agent "
            f"loop. The key is in a {cooldown:.0f}s cooldown (repeats "
            "escalate). Fix the agent's retry/tool loop, or tune the "
            "runaway: config if this is legitimate load.",
            retry_after=cooldown,
        )

    async def _acquire_slot(self, ctx: RequestContext) -> None:
        """Concurrent-request admission (chat-family). A caller with
        neither a key cap nor a tenant cap skips this entirely — no
        queueing, no metrics. Otherwise: admit immediately if a slot
        fits, else queue (highest-priority first, FIFO within a
        priority) for up to concurrency.wait_seconds and fail the
        request with 429 + Retry-After when the wait expires."""
        limiter = self.concurrency
        cfg = self.config.concurrency
        key_id = ctx.auth.key_id
        if (
            limiter is None
            or not cfg.enabled
            or key_id is None
            or (ctx.auth.max_concurrency is None and ctx.auth.tenant_max_concurrency is None)
        ):
            return
        started = time.monotonic()
        slot = await limiter.acquire(
            key_id=key_id,
            tenant_id=ctx.auth.tenant_id,
            key_label=self._key_labeler.label(ctx.auth.key_name),
            key_cap=ctx.auth.max_concurrency,
            tenant_cap=ctx.auth.tenant_max_concurrency,
            priority=ctx.auth.priority,
            wait_seconds=cfg.wait_seconds,
        )
        if slot is None:
            waited = time.monotonic() - started
            cap = ctx.auth.max_concurrency
            if cap is None:
                cap = ctx.auth.tenant_max_concurrency  # tenant cap is what held it
            # Observer-only: admission already failed, there is nothing
            # left to veto — the 429 is the decision.
            await self._emit(
                "concurrency.timeout",
                ctx,
                key_name=ctx.auth.key_name,
                model=ctx.model,
                max_concurrency=cap,
                wait_seconds=cfg.wait_seconds,
                waited_seconds=round(waited, 3),
            )
            raise RateLimitError(
                f"Concurrency limit reached for this key (max {cap} "
                f"in-flight request{'s' if cap != 1 else ''}) — no slot freed "
                f"within {cfg.wait_seconds:.0f}s. Reduce parallelism or "
                "raise the key's max_concurrency.",
                retry_after=1.0,
            )
        ctx.concurrency_slot = slot

    def _release_slot(self, ctx: RequestContext) -> None:
        """Give this request's slot back (idempotent — called from every
        exit that decrements the in-flight gauge; a request holds at
        most one). Never raises: a release failure must not break the
        response path that is already committed."""
        slot = ctx.concurrency_slot
        if slot is None:
            return
        ctx.concurrency_slot = None
        try:
            self.concurrency.release(slot)
        except Exception:  # noqa: BLE001
            logger.exception("failed to release concurrency slot")

    async def _check_rate_limit(self, ctx: RequestContext, estimated_tokens: int) -> None:
        key = ctx.auth.key_id
        if key is not None:  # admin/env key: unlimited (at the key layer)
            allowed, retry_after = await self.rate_limiter.check(
                key, ctx.auth.rpm_limit, ctx.auth.tpm_limit, estimated_tokens
            )
            if not allowed:
                raise RateLimitError(
                    "Rate limit reached for this key "
                    f"(rpm={ctx.auth.rpm_limit}, tpm={ctx.auth.tpm_limit}). "
                    f"Retry after {retry_after:.1f}s.",
                    retry_after=retry_after,
                )
            await self.rate_limiter.record_request(key)
        # Tenant layer: a second, pooled window under its own namespace —
        # every member key's requests count together. No admin bypass:
        # the tenant ceiling binds tenant admins like everyone else in
        # the tenant (tenant-less callers skip this entirely).
        if ctx.auth.tenant_id is not None:
            tenant_key = f"tenant:{ctx.auth.tenant_id}"
            allowed, retry_after = await self.rate_limiter.check(
                tenant_key,
                ctx.auth.tenant_rpm_limit,
                ctx.auth.tenant_tpm_limit,
                estimated_tokens,
            )
            if not allowed:
                raise RateLimitError(
                    "Rate limit reached for tenant "
                    f"'{ctx.auth.tenant_name or ctx.auth.tenant_id}' "
                    f"(rpm={ctx.auth.tenant_rpm_limit}, "
                    f"tpm={ctx.auth.tenant_tpm_limit}). "
                    f"Retry after {retry_after:.1f}s.",
                    retry_after=retry_after,
                )
            await self.rate_limiter.record_request(tenant_key)

    def _check_budget(
        self, ctx: RequestContext, *, now: datetime | None = None
    ) -> None:
        """The request-time budget gate, both legs. The floor: the
        three hard caps per level (total / daily / monthly windows)
        against the auth-time spend snapshot — enforced ALWAYS, key
        and tenant. The stance (tier 50): when
        ``budget_enforcement.enabled``, a monthly window whose
        month-end RUN-RATE PROJECTION exceeds its budget is denied
        the same 429 — the trending tooth, before the money is spent;
        the hard cap raised first when actual spend has already
        crossed, so the two messages can never lie about each other.
        ``now`` is injectable so the month clock is pinnable in
        tests; every caller passes nothing and gets the real one."""
        if now is None:
            now = datetime.now(timezone.utc)
        if ctx.auth.key_id is not None:
            if ctx.auth.max_budget_usd is not None:
                if ctx.auth.spend_usd >= ctx.auth.max_budget_usd:
                    raise BudgetExceededError(
                        ctx.auth.key_name or ctx.auth.key_id,
                        ctx.auth.max_budget_usd,
                        ctx.auth.spend_usd,
                    )
            # Daily / monthly windows: like the total, evaluated against the
            # auth-time snapshot. A stored bucket that differs from the current
            # one means the window has rolled — that spend counts as 0.
            if ctx.auth.daily_budget_usd is not None:
                today = daily_bucket(now)
                spent = (
                    ctx.auth.daily_spend_usd or 0.0
                    if ctx.auth.daily_bucket == today
                    else 0.0
                )
                if spent >= ctx.auth.daily_budget_usd:
                    raise BudgetExceededError(
                        ctx.auth.key_name or ctx.auth.key_id,
                        ctx.auth.daily_budget_usd,
                        spent,
                        period="daily",
                    )
            if ctx.auth.monthly_budget_usd is not None:
                month = monthly_bucket(now)
                spent = (
                    ctx.auth.monthly_spend_usd or 0.0
                    if ctx.auth.monthly_bucket == month
                    else 0.0
                )
                if spent >= ctx.auth.monthly_budget_usd:
                    raise BudgetExceededError(
                        ctx.auth.key_name or ctx.auth.key_id,
                        ctx.auth.monthly_budget_usd,
                        spent,
                        period="monthly",
                    )
                # Tier 50: the projected leg — the trending tooth. Only
                # reached with 0 <= spent < budget (the hard cap above
                # raised otherwise), so a denial here is honestly a
                # projection, never an over-budget spend wearing a
                # trend's clothes.
                self._check_projected_budget(
                    ctx.auth.key_name or ctx.auth.key_id,
                    budget=ctx.auth.monthly_budget_usd,
                    spent=spent,
                    now=now,
                )
        # Tenant budgets: the same three windows against the pooled tenant
        # spend (auth-time snapshot). Like the tenant rate limit, this has
        # no admin bypass — a tenant admin is still a member of the tenant.
        if ctx.auth.tenant_id is not None:
            tenant_label = ctx.auth.tenant_name or ctx.auth.tenant_id
            if ctx.auth.tenant_max_budget_usd is not None:
                if ctx.auth.tenant_spend_usd >= ctx.auth.tenant_max_budget_usd:
                    raise BudgetExceededError(
                        tenant_label,
                        ctx.auth.tenant_max_budget_usd,
                        ctx.auth.tenant_spend_usd,
                        kind="tenant",
                    )
            if ctx.auth.tenant_daily_budget_usd is not None:
                today = daily_bucket(now)
                spent = (
                    ctx.auth.tenant_daily_spend_usd or 0.0
                    if ctx.auth.tenant_daily_bucket == today
                    else 0.0
                )
                if spent >= ctx.auth.tenant_daily_budget_usd:
                    raise BudgetExceededError(
                        tenant_label,
                        ctx.auth.tenant_daily_budget_usd,
                        spent,
                        period="daily",
                        kind="tenant",
                    )
            if ctx.auth.tenant_monthly_budget_usd is not None:
                month = monthly_bucket(now)
                spent = (
                    ctx.auth.tenant_monthly_spend_usd or 0.0
                    if ctx.auth.tenant_monthly_bucket == month
                    else 0.0
                )
                if spent >= ctx.auth.tenant_monthly_budget_usd:
                    raise BudgetExceededError(
                        tenant_label,
                        ctx.auth.tenant_monthly_budget_usd,
                        spent,
                        period="monthly",
                        kind="tenant",
                    )
                # Tier 50: the tenant leg of the projected stance — the
                # same projection over the pooled spend, so a tenant's
                # shared ceiling trends as honestly as any member's.
                self._check_projected_budget(
                    tenant_label,
                    budget=ctx.auth.tenant_monthly_budget_usd,
                    spent=spent,
                    now=now,
                    kind="tenant",
                )

    def _check_projected_budget(
        self,
        label: str,
        *,
        budget: float,
        spent: float,
        now: datetime,
        kind: str = "key",
    ) -> None:
        """The tier-50 projected leg, key and tenant legs shared (one
        rulebook, like every two-level check in the gate). Straight-
        line run-rate over the auth snapshot — the same counter lens
        the hard cap just read, no extra query — denied at
        ``projected > budget``. The rules, in order: the stance is
        off → nothing; nothing spent → nothing to project (zero spend
        projects zero, honestly); the month's first instant → nothing
        to divide by; the honesty gate (``min_elapsed_days``) → hours
        of evidence never deny; and the projection self-heals — spend
        over elapsed is a ratio, so a caller that stops spending
        watches its projection decay under the budget as the month
        elapses and traffic resumes without any operator action."""
        cfg = self.config.budget_enforcement
        if not cfg.enabled or spent <= 0:
            return
        projection = run_rate_month_end(now, spent)
        if projection is None:
            return  # elapsed 0 — the month's first instant
        if projection["elapsed_days"] < cfg.min_elapsed_days:
            return  # the honesty gate: too young a month to mean anything
        if projection["projected_usd"] > budget:
            raise BudgetProjectionError(
                label,
                budget=budget,
                spent=spent,
                projected_usd=projection["projected_usd"],
                elapsed_days=projection["elapsed_days"],
                days_in_month=projection["days_in_month"],
                kind=kind,
            )

    # ------------------------------------------------------------------
    # Accounting helpers
    # ------------------------------------------------------------------

    async def _account_usage(
        self, ctx: RequestContext, usage: dict, native_model: str, spend: bool = True
    ) -> None:
        prompt_tokens = usage.get("prompt_tokens") or 0
        completion_tokens = usage.get("completion_tokens") or 0
        cached_tokens = cached_tokens_from_usage(usage)
        ctx.prompt_tokens = prompt_tokens or ctx.prompt_tokens
        ctx.completion_tokens = completion_tokens or ctx.completion_tokens
        if cached_tokens:
            ctx.cached_tokens = cached_tokens
        if prompt_tokens or completion_tokens:
            metrics.TOKENS.labels("prompt").inc(prompt_tokens)
            metrics.TOKENS.labels("completion").inc(completion_tokens)
            # Provider-side prompt-cache reads: their own series, so a
            # cache-heavy traffic pattern is visible (and the uncached
            # prompt total stays comparable with pre-tier-24 data).
            if cached_tokens:
                metrics.TOKENS.labels("cached_input").inc(cached_tokens)
            if self.config.metrics.per_key:
                key = self._key_labeler.label(ctx.auth.key_name)
                metrics.KEY_TOKENS.labels(key=key, type="prompt").inc(prompt_tokens)
                metrics.KEY_TOKENS.labels(key=key, type="completion").inc(
                    completion_tokens
                )
                if cached_tokens:
                    metrics.KEY_TOKENS.labels(key=key, type="cached_input").inc(
                        cached_tokens
                    )
            # TPM accounting: actual usage of completed (non-cached) calls.
            if ctx.auth.key_id is not None and not ctx.cache_hit:
                await self.rate_limiter.record_usage(
                    ctx.auth.key_id, prompt_tokens + completion_tokens
                )
            # ... pooled into the tenant's window too, same condition.
            if ctx.auth.tenant_id is not None and not ctx.cache_hit:
                await self.rate_limiter.record_usage(
                    f"tenant:{ctx.auth.tenant_id}",
                    prompt_tokens + completion_tokens,
                )
        if ctx.cache_hit:
            return  # no upstream spend on cache hits
        cost = self.cost_calculator.calculate(usage, native_model) or self.cost_calculator.calculate(
            usage, ctx.model
        )
        # Billed cost (tenant markup included) is what the log row,
        # spend/budgets and the per-key chargeback metrics record; the
        # model-labeled cost series stays ACTUAL provider cost.
        ctx.cost_usd = self._billed_cost(ctx, cost)
        metrics.COST.labels(model=ctx.model).inc(cost)
        if self.config.metrics.per_key:
            metrics.KEY_COST.labels(
                key=self._key_labeler.label(ctx.auth.key_name)
            ).inc(ctx.cost_usd)
        if spend:
            self._queue_spend(ctx, ctx.cost_usd)

    # ------------------------------------------------------------------
    # Background work + graceful shutdown
    # ------------------------------------------------------------------

    def _spawn(self, coro) -> None:
        """Fire-and-forget background work, but *tracked*: shutdown drains
        the set before the engine goes away, so the last spend accounting
        or shadow mirror of a shutting-down gateway is not lost."""
        import asyncio

        task = asyncio.get_running_loop().create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def drain_background_tasks(self, timeout: float = 10.0) -> dict:
        """Wait for in-flight background work (spend accounting, shadow
        mirrors, prompt A/B). Called from the lifespan shutdown path
        *before* clients, the log queue and the engine are closed; bounded
        so a wedged upstream cannot hang shutdown forever."""
        import asyncio

        pending = [t for t in self._background_tasks if not t.done()]
        if not pending:
            return {"drained": 0, "timed_out": 0}
        done, still_pending = await asyncio.wait(pending, timeout=timeout)
        if still_pending:
            logger.warning(
                "shutdown: %d background task(s) still in flight after %.1fs "
                "(left to die with the loop)",
                len(still_pending),
                timeout,
            )
        return {"drained": len(done), "timed_out": len(still_pending)}

    # ------------------------------------------------------------------
    # Configuration hot-reload
    # ------------------------------------------------------------------

    def apply_config(self, config: GatewayConfig) -> dict:
        """Swap the config-derived policy components in place (no restart).

        Reloads: guardrails, models/deployments/routing policy, pricing,
        model aliases / default_model, and prompt-policy knobs — everything
        that is a pure function of ``config.yaml`` with no external
        resources attached. Built before anything is assigned, so a bad
        config raises with the old one still live.

        NOT reloaded (they own processes/sockets/connections): provider
        credentials & URLs, the auth chain, cache/rate-limit/breaker
        backends, the semantic cache, logging/alerts/audit delivery,
        telemetry. A change there is reported in ``restart_required``.
        """
        # Build first — from_config validates guardrail names/entrypoints
        # and raises ValueError on anything unknown.
        guardrails = GuardrailManager.from_config(
            config.guardrails, tracer=self._tracer
        )
        restart_required = [
            field
            for field in (
                "providers",
                "auth",
                "cache",
                "idempotency",
                "rate_limit",
                "routing",
                "redis",
                "semantic_cache",
                "logging",
                "metrics",
                "alerts",
                "audit",
                "vault",
            )
            if getattr(config, field) != getattr(self.config, field)
        ]
        # "routing" only matters for its *backends* (breaker registry is
        # shared state) — timeouts/retries are read live off router.config.
        if restart_required == ["routing"]:
            restart_required = []
        self.config = config
        self.guardrails = guardrails
        # Lifecycle-event handlers reload too (same build-first pattern;
        # a bad handler class/subscription raises with the old set live).
        # Queue size changes apply at restart — the queue survives.
        if self.events is not None:
            self.events.apply_config(config.events)
        # The router keeps its breakers (shared state, incl. Redis) and
        # its provider adapters (they hold the HTTP client); the groups,
        # routing policy and pricing are swapped in place.
        self.router.apply_config(config)
        # MCP breakers (tier 66): same shared-state rule — the registry
        # (possibly Redis) survives; re-registering creates breakers for
        # servers the reload ADDED (state preserved for the rest — a
        # reload is not a health event).
        for _server in config.mcp.servers:
            self.mcp_breakers.register(_server.name, "mcp")
        self.cost_calculator = CostCalculator(config.pricing)
        self._model_names = {m.name for m in config.models}
        self._model_windows = {m.name: m.context_window for m in config.models}
        if self.prompt_library is not None:
            self.prompt_library.cache_seconds = config.prompts.library_cache_seconds
        logger.info(
            "config reloaded: %d model(s), %d guardrail(s)%s",
            len(config.models),
            len(guardrails.describe()),
            f" (restart required for: {', '.join(restart_required)})"
            if restart_required
            else "",
        )
        return {
            "models": config.public_models(),
            "guardrails": [g["name"] for g in guardrails.describe()],
            "restart_required": restart_required,
        }

    def _queue_spend(self, ctx: RequestContext, cost: float) -> None:
        if not ctx.auth.key_id or cost <= 0:
            return

        async def _apply() -> None:
            try:
                await self.logging_service.add_spend(
                    ctx.auth.key_id, cost, tenant_id=ctx.auth.tenant_id
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "failed to record spend for key %s", ctx.auth.key_id
                )

        try:
            self._spawn(_apply())
        except RuntimeError:  # no loop (tests) — apply inline
            pass

    # ------------------------------------------------------------------
    # Shadow mirroring
    # ------------------------------------------------------------------

    def _queue_shadow(
        self,
        request: ChatCompletionRequest,
        ctx: RequestContext,
        primary_text: str | None = None,
    ) -> None:
        """Mirror a successfully-served request to the model's shadow
        deployments (fire-and-forget, exactly like spend accounting).

        Shadow deployments exist to receive a copy of real traffic so
        their latency / status / content can be compared before being
        promoted to serving. The mirror never affects the client: no
        guardrails, no rate limits, no budgets, no cache, and its cost
        is logged on its own row instead of the key's spend.

        ``primary_text`` (tier 35) is the primary's RAW provider text
        (pre-rewrite), captured by value at queue time; the mirror task
        scores its own answer against it (core/shadow_diff) and the
        verdict + similarity land on the shadow row — the migration
        evidence /admin/shadow-diff aggregates.
        """
        import asyncio

        shadows = self.router.shadow_deployments(
            ctx.model, ctx.auth.effective_residencies()
        )
        if not shadows:
            return
        for deployment in shadows:
            if deployment.shadow_fraction <= 0.0:
                continue
            if deployment.shadow_fraction < 1.0 and (
                random.random() > deployment.shadow_fraction
            ):
                continue

            async def _mirror(
                deployment: DeploymentConfig = deployment,
                primary_text: str | None = primary_text,
            ) -> None:
                await self._mirror_to_shadow(
                    deployment, request, ctx, primary_text
                )

            try:
                self._spawn(_mirror())
            except RuntimeError:  # no running loop — nothing to mirror into
                pass

    async def _mirror_to_shadow(
        self,
        deployment: DeploymentConfig,
        request: ChatCompletionRequest,
        ctx: RequestContext,
        primary_text: str | None = None,
    ) -> None:
        """Send one non-streaming copy of the request to a shadow deployment.

        Always ``complete()`` (never ``stream()``) so mirrored calls are
        comparable with each other regardless of how the client asked.
        Outcomes land in the shadow metrics, the deployment's circuit
        breaker, and a shadow request-log row sharing the primary's
        request_id — nothing here can reach the client.

        Tier 35: the row also carries the pair's score. The mirror's
        answer is diffed against ``primary_text`` (the primary's raw
        provider text, captured at queue time) — verdict + similarity
        via core/shadow_diff.score_pair — so /admin/shadow-diff can
        aggregate migration evidence straight off the log rows without
        re-reading or re-diffing bodies.
        """
        started = time.monotonic()
        # The copy is non-streaming even for streamed primaries.
        mirror_request = request.model_copy(
            update={"stream": False, "stream_options": None}
        )
        # This task inherited a copy of the primary's context — clear the
        # upstream wire slots so the shadow row records THIS call's wire
        # data (or None if it never reached the provider), never the
        # primary's.
        wire.reset_upstream()
        outcome = "success"
        error_message: str | None = None
        response: dict | None = None
        try:
            adapter = self.router.providers[deployment.provider]
            provider_cfg = self.router._provider_cfg(deployment)
            response = await adapter.complete(
                deployment,
                mirror_request,
                provider_cfg,
                self.router._timeout(deployment),
            )
        except GatewayError as exc:
            outcome, error_message = "error", exc.message
        except Exception as exc:  # noqa: BLE001 — a mirror must never crash the app
            outcome, error_message = "error", f"internal error: {exc}"
        latency_ms = int((time.monotonic() - started) * 1000)

        # Breaker bookkeeping: informational for shadow deployments (they
        # are never picked for serving), but a recovered shadow closes its
        # own circuit and a failing one is visible in /admin/routing.
        if outcome == "success":
            await self.router.breakers.record_success(deployment.name)
        else:
            await self.router.breakers.record_failure(deployment.name)

        metrics.SHADOW_REQUESTS.labels(
            deployment=deployment.name, outcome=outcome
        ).inc()
        metrics.SHADOW_LATENCY.labels(deployment=deployment.name).observe(
            latency_ms / 1000
        )

        usage = (response or {}).get("usage") or {}
        cost = (
            self.cost_calculator.calculate(usage, deployment.model)
            or self.cost_calculator.calculate(usage, ctx.model)
            if response is not None
            else None
        )
        # Tier 35: score the pair in memory (works even when bodies are
        # not persisted — only the drilldown excerpts need stored bodies).
        verdict, similarity = score_pair(
            primary_text, extract_content_text(response), outcome
        )
        shadow_ctx = RequestContext(
            request_id=ctx.request_id,  # correlation: same id as the primary
            auth=AuthContext(
                key_name=ctx.auth.key_name, is_admin=ctx.auth.is_admin
            ),  # no key_id — shadow rows never touch spend
            model=ctx.model,
            provider=deployment.provider,
            deployment=deployment.name,
            native_model=deployment.model,
            stream=False,
            request_body=mirror_request.model_dump(exclude_none=True),
            response_body=response,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            cost_usd=cost,
            status=outcome,
            error_message=error_message,
            shadow=True,
            shadow_verdict=verdict,
            shadow_similarity=similarity,
            # Honest latency (tier 37): the row measures from THIS call's
            # start, not the context's birth after it — the shadow row
            # used to log ~0ms, which made every latency delta read as
            # an implausible speedup in the evidence aggregate.
            started_monotonic=started,
        )
        # Snapshot this mirror's own wire capture (its task context, not
        # the primary's — see the reset above).
        shadow_ctx.upstream_request = wire.upstream_request()
        shadow_ctx.upstream_headers = wire.upstream_headers()
        try:
            await self.logging_service.write_request_log(shadow_ctx)
        except Exception:  # noqa: BLE001
            logger.exception("failed to write shadow log row for %s", deployment.name)

    # ------------------------------------------------------------------
    # Prompt A/B mirroring (prompt ops)
    # ------------------------------------------------------------------

    def _queue_prompt_ab(
        self,
        request: ChatCompletionRequest,
        ctx: RequestContext,
        primary_text: str | None = None,
    ) -> None:
        """Mirror a fraction of successfully-served requests to the
        library prompt's candidate version (fire-and-forget).

        Only applies when the serving system prompt came from the
        library (a key's system_prompt_ref): the candidate swaps in for
        the active body and the request replays against the *same*
        deployment the primary used — same model, same conditions, only
        the prompt differs. The client is never affected (no guardrails,
        no rate limits, no budgets, no cache, no spend; own log row).

        ``primary_text`` (tier 35): the primary's raw answer, scored
        against at mirror time — the variant row carries a verdict +
        similarity too (the prompt workflow's evidence; the deployment
        aggregate excludes prompt_variant rows by design).
        """
        import asyncio

        library_prompt: ResolvedPrompt | None = ctx.library_prompt
        candidate = library_prompt.candidate if library_prompt else None
        if candidate is None or candidate.fraction <= 0.0:
            return
        if candidate.fraction < 1.0 and random.random() > candidate.fraction:
            return

        async def _mirror(primary_text: str | None = primary_text) -> None:
            await self._mirror_prompt_variant(
                request, ctx, library_prompt, candidate, primary_text
            )

        try:
            self._spawn(_mirror())
        except RuntimeError:  # no running loop — nothing to mirror into
            pass

    async def _mirror_prompt_variant(
        self,
        request: ChatCompletionRequest,
        ctx: RequestContext,
        library_prompt: ResolvedPrompt,
        candidate: CandidatePrompt,
        primary_text: str | None = None,
    ) -> None:
        """Send one non-streaming copy of the request with the candidate
        prompt to the deployment that served the primary.

        The swap targets the system message that carries the (rendered)
        active body — the message the prompt policy injected. If the
        client supplied its own system message the library prompt never
        served, and if a guardrail rewrote it the comparison would be
        polluted; both cases skip the mirror.

        Tier 35: the variant row is scored against ``primary_text``
        (the primary's raw answer) like a deployment mirror — the
        verdict/similarity on the row are the prompt A/B evidence.
        Divergence here is EXPECTED (a different prompt was tested);
        /admin/shadow-diff therefore excludes prompt_variant rows.
        """
        variables = dict(self.config.prompts.variables)
        if ctx.auth.prompt_variables:
            variables.update(ctx.auth.prompt_variables)
        rendered_active = substitute(library_prompt.body, variables)
        rendered_candidate = substitute(candidate.body, variables)

        new_messages = []
        swapped = False
        for message in request.messages:
            if (
                not swapped
                and message.role in ("system", "developer")
                and message.content == rendered_active
            ):
                new_messages.append(Message(role="system", content=rendered_candidate))
                swapped = True
            else:
                new_messages.append(message)
        if not swapped:
            return  # client system message (or guardrail rewrite) — nothing to A/B

        deployment = self.router.find_deployment(ctx.deployment or "")
        if deployment is None:
            return
        variant_request = request.model_copy(
            update={
                "messages": new_messages,
                "stream": False,  # mirrored calls are comparable with each other
                "stream_options": None,
            }
        )

        started = time.monotonic()
        # Inherited the primary's context — record THIS call's wire data
        # (or None), never the primary's.
        wire.reset_upstream()
        outcome = "success"
        error_message: str | None = None
        response: dict | None = None
        try:
            adapter = self.router.providers[deployment.provider]
            provider_cfg = self.router._provider_cfg(deployment)
            response = await adapter.complete(
                deployment,
                variant_request,
                provider_cfg,
                self.router._timeout(deployment),
            )
        except GatewayError as exc:
            outcome, error_message = "error", exc.message
        except Exception as exc:  # noqa: BLE001 — a mirror must never crash the app
            outcome, error_message = "error", f"internal error: {exc}"
        latency_ms = int((time.monotonic() - started) * 1000)

        # Breaker bookkeeping: the mirror rides the primary's deployment,
        # so a failing mirror says something about that deployment.
        if outcome == "success":
            await self.router.breakers.record_success(deployment.name)
        else:
            await self.router.breakers.record_failure(deployment.name)

        metrics.PROMPT_MIRROR_REQUESTS.labels(
            prompt=library_prompt.name, outcome=outcome
        ).inc()
        metrics.PROMPT_MIRROR_LATENCY.labels(prompt=library_prompt.name).observe(
            latency_ms / 1000
        )

        usage = (response or {}).get("usage") or {}
        cost = (
            self.cost_calculator.calculate(usage, deployment.model)
            or self.cost_calculator.calculate(usage, ctx.model)
            if response is not None
            else None
        )
        # Tier 35: score the candidate's answer against the primary's.
        verdict, similarity = score_pair(
            primary_text, extract_content_text(response), outcome
        )
        variant_ctx = RequestContext(
            request_id=ctx.request_id,  # correlation: same id as the primary
            auth=AuthContext(
                key_name=ctx.auth.key_name, is_admin=ctx.auth.is_admin
            ),  # no key_id — mirror rows never touch spend
            model=ctx.model,
            provider=deployment.provider,
            deployment=deployment.name,
            native_model=deployment.model,
            stream=False,
            request_body=variant_request.model_dump(exclude_none=True),
            response_body=response,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            cost_usd=cost,
            status=outcome,
            error_message=error_message,
            shadow=True,  # excluded from /admin/stats and key spend
            prompt_variant=f"{library_prompt.name} v{candidate.version}",
            shadow_verdict=verdict,
            shadow_similarity=similarity,
            # Honest latency (tier 37): call start, not ctx birth — the
            # same fix as the shadow mirror above.
            started_monotonic=started,
        )
        # Snapshot this mirror's own wire capture (its task context).
        variant_ctx.upstream_request = wire.upstream_request()
        variant_ctx.upstream_headers = wire.upstream_headers()
        try:
            await self.logging_service.write_request_log(variant_ctx)
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to write prompt A/B log row for %s", library_prompt.name
            )

    # ------------------------------------------------------------------
    # Response construction helpers
    # ------------------------------------------------------------------

    def _content_filter_response(self, model: str, reason: str) -> dict:
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                    },
                    "finish_reason": "content_filter",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "_gateway_note": reason,
        }

    def _content_filter_chunk(self, model: str) -> dict:
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "content_filter"}
            ],
        }

    def _delta_chunk(self, model: str, text: str) -> dict:
        """A plain content-delta chunk — used to flush stream-rewriter output
        the guardrails held back until the finish signal."""
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "delta": {"content": text}, "finish_reason": None}
            ],
        }

    def _synthesized_response(
        self, model: str, text: str, finish_reason: str | None, usage: dict | None
    ) -> dict:
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text or None},
                    "finish_reason": finish_reason or "stop",
                }
            ],
            "usage": usage
            or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    # ------------------------------------------------------------------
    # Observability helpers
    # ------------------------------------------------------------------

    def _annotate_span(self, span, ctx: RequestContext) -> None:
        """Copy the request outcome onto the span just before it ends."""
        span.set_attribute("gateway.provider", ctx.provider or "none")
        span.set_attribute("gateway.deployment", ctx.deployment or "none")
        span.set_attribute("gateway.status", ctx.status)
        span.set_attribute("gateway.cache_hit", ctx.cache_hit)
        span.set_attribute("gateway.latency_ms", ctx.latency_ms())
        span.set_attribute("gateway.cost_usd", ctx.cost_usd)
        if ctx.prompt_tokens is not None:
            span.set_attribute("gateway.prompt_tokens", ctx.prompt_tokens)
        if ctx.completion_tokens is not None:
            span.set_attribute("gateway.completion_tokens", ctx.completion_tokens)

    def _finish_span(self, ctx: RequestContext, error: Exception | None = None) -> None:
        """Final accounting for one request: span attributes, status, end + in-flight gauge."""
        metrics.INFLIGHT.dec()
        # Concurrency slot (if this request held one) — every non-stream
        # exit lands here, success and error paths alike.
        self._release_slot(ctx)
        span = ctx.otel_span
        if span is None:
            return
        self._annotate_span(span, ctx)
        if error is not None:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, ctx.error_message or str(error)))
        elif ctx.status in ("error", "blocked"):
            span.set_status(Status(StatusCode.ERROR, ctx.error_message or ctx.status))
        else:
            span.set_status(Status(StatusCode.OK))
        span.end()

    async def _note_byok(self, ctx: RequestContext, deployment) -> None:
        """Mark the ctx when the upstream call rode the TENANT'S stored
        credential (BYOK vault): the provider name only — the key itself
        is never recorded anywhere. Flows to the request-log row, the
        X-Gateway-Byok-Provider header and the byok metric.

        Cache-only peek: right after a dispatch the entry is always hot
        (the dispatch resolved it), so this is a dict read, never a DB
        query. A cold miss just means no marker — never a wrong
        credential. Shadow mirrors skip this note entirely (they ride
        platform credentials by design)."""
        if not self.vault.enabled or ctx.auth.tenant_id is None:
            return
        provider = await self.vault.peek(ctx.auth.tenant_id, deployment.provider)
        ctx.byok_provider = provider
        if provider is not None:
            metrics.BYOK_REQUESTS.labels(provider=provider).inc()

    def _headers(self, ctx: RequestContext, cache_state: str) -> dict[str, str]:
        return {
            "X-Request-Id": ctx.request_id,
            "X-Gateway-Model": ctx.model,
            "X-Gateway-Cache": cache_state,
            **({"X-Gateway-Tenant": ctx.auth.tenant_name} if ctx.auth.tenant_id else {}),
            **({"X-Gateway-Provider": ctx.provider} if ctx.provider else {}),
            **({"X-Gateway-Deployment": ctx.deployment} if ctx.deployment else {}),
            **(
                {"X-Gateway-Byok-Provider": ctx.byok_provider}
                if ctx.byok_provider
                else {}
            ),
            **({"X-Gateway-Cost-Usd": f"{ctx.cost_usd:.8f}"} if ctx.cost_usd else {}),
            **({"X-Gateway-Latency-Ms": str(ctx.latency_ms())}),
        }

    def _request_metric(self, ctx: RequestContext) -> None:
        status = "success"
        if ctx.status == "blocked":
            status = "blocked"
        elif ctx.status == "error":
            status = "error"
        elif ctx.cache_hit:
            status = "cache_hit"
        metrics.REQUESTS.labels(
            provider=ctx.provider or "none", model=ctx.model or "unknown", status=status
        ).inc()
        if self.config.metrics.per_key:
            metrics.KEY_REQUESTS.labels(
                key=self._key_labeler.label(ctx.auth.key_name), status=status
            ).inc()

    async def _log(self, ctx: RequestContext, event: str = "chat request completed") -> None:
        # Snapshot the wire capture into ctx here, in the request's own
        # task: the background queued writer (logging.background_write)
        # would otherwise read the contextvars from the consumer task's
        # own — empty — context. Both slots stay None when nothing went
        # on the wire (cache hit; blocked before dispatch).
        ctx.upstream_request = wire.upstream_request()
        ctx.upstream_headers = wire.upstream_headers()
        # One structured record per request — feeds JSON stdout logging and
        # the OTel logs pipeline (with the current span's trace context).
        logger.info(
            event,
            extra={
                "request_id": ctx.request_id,
                "trace_id": ctx.trace_id,
                "span_id": ctx.span_id,
                "key_name": ctx.auth.key_name,
                "tenant": ctx.auth.tenant_name,
                "model": ctx.model,
                "provider": ctx.provider,
                "deployment": ctx.deployment,
                "status": ctx.status,
                "stream": ctx.stream,
                "cache_hit": ctx.cache_hit,
                "prompt_tokens": ctx.prompt_tokens,
                "completion_tokens": ctx.completion_tokens,
                "cost_usd": round(ctx.cost_usd, 8) or None,
                "latency_ms": ctx.latency_ms(),
                "ttft_ms": ctx.ttft_ms,
                "error": ctx.error_message,
            },
        )
        try:
            await self.logging_service.write_request_log(ctx)
        except Exception:  # noqa: BLE001
            logger.exception("failed to write request log")
