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
import random
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi.responses import JSONResponse, Response, StreamingResponse
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode, use_span

from ..config import DeploymentConfig, GatewayConfig
from ..errors import (
    BudgetExceededError,
    GatewayError,
    GuardrailBlocked,
    ModelNotAllowedError,
    ProviderError,
    RateLimitError,
)
from .prompt_library import CandidatePrompt, PromptLibrary, ResolvedPrompt
from ..guardrails.manager import GuardrailManager
from ..observability import metrics
from ..observability.metrics import KeyLabeler
from .prompts import apply_prompt_policy, substitute
from ..schemas import (
    ChatCompletionRequest,
    EmbeddingRequest,
    ImageGenerationRequest,
    Message,
    SpeechRequest,
    TranscriptionRequest,
)
from ..utils import content_to_text, daily_bucket, estimate_tokens, monthly_bucket
from .cache import ResponseCache, cache_key_for
from .semantic_cache import SemanticCache
from .context import AuthContext, RequestContext
from .cost import CostCalculator
from .logging_svc import LoggingService
from .router import Router

logger = logging.getLogger("gateway.pipeline")

STREAM_MEDIA_TYPE = "text/event-stream"


def _sse(obj: Any) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def embedding_cache_key_for(request: EmbeddingRequest) -> str:
    """Cache key for embeddings: model + input + dimensions.

    ``encoding_format`` is deliberately not part of the key — the cache
    stores the canonical float form and base64 is (re-)encoded per request.
    Embeddings are deterministic for the same model+input, which makes
    them ideal cache targets.
    """
    payload = {
        "kind": "embedding",
        "model": request.model,
        "input": [request.input] if isinstance(request.input, str) else request.input,
        "dimensions": request.dimensions,
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
        # Optional semantic (similarity) cache layer; None = disabled.
        self.semantic = semantic
        # Versioned prompt library (prompt ops): resolves key
        # system_prompt_ref values and drives prompt A/B mirroring.
        self.prompt_library = prompt_library
        # OTel API is a no-op until a provider is installed (spans are then
        # invalid and cost ~nothing), so this is safe unconditionally.
        self._tracer = tracer or trace.get_tracer("gateway")
        # Tracked fire-and-forget work (spend accounting, shadow mirrors,
        # prompt A/B) — drained on shutdown before the engine goes away.
        self._background_tasks: set = set()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def handle_chat(
        self,
        request: ChatCompletionRequest,
        auth: AuthContext,
        request_id: str | None = None,
        route: str | None = None,
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
                response = await self._handle(request, ctx)
            except GatewayError as exc:
                ctx.status = "blocked" if isinstance(exc, GuardrailBlocked) else "error"
                ctx.error_message = exc.message
                self._request_metric(ctx)
                await self._log(ctx)
                self._finish_span(ctx, exc)
                raise
            except Exception as exc:  # noqa: BLE001
                ctx.status = "error"
                ctx.error_message = f"internal error: {exc}"
                self._request_metric(ctx)
                await self._log(ctx)
                self._finish_span(ctx, exc)
                raise
            if isinstance(response, StreamingResponse):
                # The span and the in-flight gauge are finished by the stream
                # wrapper once the response body completes.
                return response
            self._finish_span(ctx)
            return response

    async def _handle(self, request: ChatCompletionRequest, ctx: RequestContext) -> Response:
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

        # 1. Model permission (admins bypass)
        if not ctx.auth.is_admin:
            self._check_model_permission(ctx.auth, request.model)

        # 2. Rate limit (pre-flight)
        estimated_tokens = sum(
            estimate_tokens(content_to_text(m.content)) for m in request.messages
        )
        await self._check_rate_limit(ctx, estimated_tokens)

        # 3. Budget (pre-flight)
        self._check_budget(ctx)

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
            if not ctx.auth.is_admin:
                self._check_model_permission(ctx.auth, request.model)
            ctx.model = request.model
            if ctx.otel_span is not None:
                ctx.otel_span.set_attribute("gateway.model", request.model)

        # 5. Cache lookup: exact match first, then semantic (similarity)
        key = cache_key_for(request) if self.cache.enabled else None
        semantic_embedding: list[float] | None = None
        cached = await self.cache.get(key) if key is not None else None
        hit_kind = "hit" if cached is not None else None
        if cached is None and self.semantic is not None:
            semantic_embedding = await self.semantic.embed(request)
            if semantic_embedding is not None:
                cached = await self.semantic.get(request.model, semantic_embedding)
                if cached is not None:
                    hit_kind = "semantic"
        if hit_kind is not None:
            metrics.CACHE.labels(hit_kind).inc()
            ctx.cache_hit = True
            usage = cached.get("usage") or {}
            ctx.prompt_tokens = usage.get("prompt_tokens")
            ctx.completion_tokens = usage.get("completion_tokens")
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
        if key is not None or self.semantic is not None:
            metrics.CACHE.labels("miss").inc()

        # 6. + 7. Route to provider (with retries/fallbacks)
        if request.stream:
            deployment, chunk_iterator = await self.router.complete_stream(request)
            ctx.provider = deployment.provider
            ctx.deployment = deployment.name
            ctx.native_model = deployment.model
            headers = self._headers(ctx, "MISS")
            return self._streaming_response(
                self._stream_with_guardrails(
                    request,
                    ctx,
                    chunk_iterator,
                    cache_key=key,
                    semantic_embedding=semantic_embedding,
                ),
                ctx,
                headers,
            )

        deployment, response = await self.router.complete(request)
        ctx.provider = deployment.provider
        ctx.deployment = deployment.name
        ctx.native_model = deployment.model

        # 8. Guardrails: response phase
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
            self._queue_shadow(request, ctx)
            self._queue_prompt_ab(request, ctx)

        if key is not None and ctx.status == "success":
            await self.cache.put(key, response)
        if (
            semantic_embedding is not None
            and self.semantic is not None
            and ctx.status == "success"
        ):
            await self.semantic.put(request.model, request, semantic_embedding, response)

        self._request_metric(ctx)
        metrics.LATENCY.labels(provider=ctx.provider or "none").observe(
            ctx.latency_ms() / 1000
        )
        await self._log(ctx)
        return JSONResponse(content=response, headers=self._headers(ctx, "MISS"))

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
                response = await inner(ctx)
            except GatewayError as exc:
                ctx.status = "error"
                ctx.error_message = exc.message
                self._request_metric(ctx)
                await self._log(ctx, event=event)
                self._finish_span(ctx, exc)
                raise
            except Exception as exc:  # noqa: BLE001
                ctx.status = "error"
                ctx.error_message = f"internal error: {exc}"
                self._request_metric(ctx)
                await self._log(ctx, event=event)
                self._finish_span(ctx, exc)
                raise
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

        # 1. Model permission (admins bypass)
        if not ctx.auth.is_admin:
            self._check_model_permission(ctx.auth, request.model)

        # 2. Rate limit (pre-flight, ~chars/4 per input)
        inputs = [request.input] if isinstance(request.input, str) else request.input
        await self._check_rate_limit(ctx, sum(estimate_tokens(t) for t in inputs))

        # 3. Budget (pre-flight)
        self._check_budget(ctx)

        # 4. Cache lookup — embeddings are deterministic per model+input
        key = embedding_cache_key_for(request) if self.cache.enabled else None
        if key is not None:
            cached = await self.cache.get(key)
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

        # 5. Route to provider (retries/fallbacks/breakers as for chat)
        deployment, response = await self.router.embed(request)
        ctx.provider = deployment.provider
        ctx.deployment = deployment.name
        ctx.native_model = deployment.model

        # 6. Accounting
        usage = response.get("usage") or {}
        await self._account_usage(ctx, usage, deployment.model)
        ctx.response_body = response
        ctx.status = "success"

        if key is not None:
            await self.cache.put(key, response)

        self._request_metric(ctx)
        metrics.LATENCY.labels(provider=ctx.provider or "none").observe(
            ctx.latency_ms() / 1000
        )
        await self._log(ctx, event="embedding request completed")
        return JSONResponse(
            content=self._embedding_out(request, response),
            headers=self._headers(ctx, "MISS"),
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
            lambda ctx: self._handle_image_generation(request, ctx),
        )

    async def _handle_image_generation(
        self, request: ImageGenerationRequest, ctx: RequestContext
    ) -> Response:
        resolved = self._resolve_model(request.model, ctx.auth)
        if resolved != request.model:
            request = request.model_copy(update={"model": resolved})
            ctx.model = resolved
            if ctx.otel_span is not None:
                ctx.otel_span.set_attribute("gateway.model", resolved)

        if not ctx.auth.is_admin:
            self._check_model_permission(ctx.auth, request.model)

        await self._check_rate_limit(ctx, estimate_tokens(request.prompt))
        self._check_budget(ctx)

        deployment, response = await self.router.generate_image(request)
        ctx.provider = deployment.provider
        ctx.deployment = deployment.name
        ctx.native_model = deployment.model

        images = len(response.get("data") or []) or max(1, request.n)
        cost = self.cost_calculator.calculate_images(
            deployment.model, images
        ) or self.cost_calculator.calculate_images(ctx.model, images)
        await self._account_cost(ctx, cost, prompt_tokens=estimate_tokens(request.prompt))
        ctx.response_body = response
        ctx.status = "success"

        self._request_metric(ctx)
        metrics.LATENCY.labels(provider=ctx.provider or "none").observe(
            ctx.latency_ms() / 1000
        )
        await self._log(ctx, event="image generation completed")
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

        if not ctx.auth.is_admin:
            self._check_model_permission(ctx.auth, request.model)

        await self._check_rate_limit(ctx, estimate_tokens(request.prompt or ""))
        self._check_budget(ctx)

        deployment, (content, media_type) = await self.router.transcribe(request)
        ctx.provider = deployment.provider
        ctx.deployment = deployment.name
        ctx.native_model = deployment.model

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

        if not ctx.auth.is_admin:
            self._check_model_permission(ctx.auth, request.model)

        await self._check_rate_limit(ctx, estimate_tokens(request.input))
        self._check_budget(ctx)

        deployment, (audio, media_type) = await self.router.speak(request)
        ctx.provider = deployment.provider
        ctx.deployment = deployment.name
        ctx.native_model = deployment.model

        cost = self.cost_calculator.calculate_speech(
            deployment.model, len(request.input)
        ) or self.cost_calculator.calculate_speech(ctx.model, len(request.input))
        await self._account_cost(ctx, cost, prompt_tokens=estimate_tokens(request.input))
        # Binary audio: the log records a note about it, never the bytes.
        ctx.response_body = {
            "note": "binary audio response",
            "media_type": media_type,
            "bytes": len(audio),
        }
        ctx.status = "success"

        self._request_metric(ctx)
        metrics.LATENCY.labels(provider=ctx.provider or "none").observe(
            ctx.latency_ms() / 1000
        )
        await self._log(ctx, event="speech synthesis completed")
        return Response(content=audio, media_type=media_type, headers=self._headers(ctx, "NONE"))

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
        ctx.cost_usd = cost
        metrics.COST.labels(model=ctx.model).inc(cost)
        if self.config.metrics.per_key:
            metrics.KEY_COST.labels(
                key=self._key_labeler.label(ctx.auth.key_name)
            ).inc(cost)
        self._queue_spend(ctx, cost)

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

        try:
            async for chunk in chunk_iterator:
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

                if delta_text:
                    # Stream guardrails inspect what has been sent so far
                    # (source text — block checks see what the model said,
                    # not the rewrite).
                    accumulated_text += delta_text
                    await self.guardrails.run_stream_phase(
                        request, accumulated_text, delta_text, ctx
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
            if not blocked and ctx.status != "blocked":
                ctx.status = "success" if ctx.error_message is None else ctx.status
            # Mirror to shadow deployments once the stream has completed
            # successfully (never for blocked/errored streams; cache hits
            # replayed earlier never reach here).
            if not blocked and ctx.status == "success":
                self._queue_shadow(request, ctx)
                self._queue_prompt_ab(request, ctx)
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
                    await self.cache.put(cache_key, full_response)
                if semantic_embedding is not None and self.semantic is not None:
                    await self.semantic.put(
                        request.model, request, semantic_embedding, full_response
                    )
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
                            metrics.TTFT.labels(model=ctx.model).observe(
                                time.monotonic() - ctx.started_monotonic
                            )
                        yield chunk
                finally:
                    metrics.INFLIGHT.dec()
                    if span is not None:
                        self._annotate_span(span, ctx)
                        if ctx.status in ("error", "blocked"):
                            span.set_status(
                                Status(StatusCode.ERROR, ctx.error_message or ctx.status)
                            )
                        else:
                            span.set_status(Status(StatusCode.OK))

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
        import fnmatch

        patterns = auth.allowed_models
        if not patterns:
            return
        if any(fnmatch.fnmatch(model, pattern) for pattern in patterns):
            return
        raise ModelNotAllowedError(model)

    async def _check_rate_limit(self, ctx: RequestContext, estimated_tokens: int) -> None:
        key = ctx.auth.key_id
        if key is None:  # admin/env key: unlimited
            return
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

    def _check_budget(self, ctx: RequestContext) -> None:
        if ctx.auth.key_id is None:
            return
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
        now = datetime.now(timezone.utc)
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

    # ------------------------------------------------------------------
    # Accounting helpers
    # ------------------------------------------------------------------

    async def _account_usage(
        self, ctx: RequestContext, usage: dict, native_model: str, spend: bool = True
    ) -> None:
        prompt_tokens = usage.get("prompt_tokens") or 0
        completion_tokens = usage.get("completion_tokens") or 0
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
            # TPM accounting: actual usage of completed (non-cached) calls.
            if ctx.auth.key_id is not None and not ctx.cache_hit:
                await self.rate_limiter.record_usage(
                    ctx.auth.key_id, prompt_tokens + completion_tokens
                )
        if ctx.cache_hit:
            return  # no upstream spend on cache hits
        cost = self.cost_calculator.calculate(usage, native_model) or self.cost_calculator.calculate(
            usage, ctx.model
        )
        ctx.cost_usd = cost
        metrics.COST.labels(model=ctx.model).inc(cost)
        if self.config.metrics.per_key:
            metrics.KEY_COST.labels(
                key=self._key_labeler.label(ctx.auth.key_name)
            ).inc(cost)
        if spend:
            self._queue_spend(ctx, cost)

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
                "rate_limit",
                "routing",
                "redis",
                "semantic_cache",
                "logging",
                "metrics",
                "alerts",
                "audit",
            )
            if getattr(config, field) != getattr(self.config, field)
        ]
        # "routing" only matters for its *backends* (breaker registry is
        # shared state) — timeouts/retries are read live off router.config.
        if restart_required == ["routing"]:
            restart_required = []
        self.config = config
        self.guardrails = guardrails
        # The router keeps its breakers (shared state, incl. Redis) and
        # its provider adapters (they hold the HTTP client); the groups,
        # routing policy and pricing are swapped in place.
        self.router.apply_config(config)
        self.cost_calculator = CostCalculator(config.pricing)
        self._model_names = {m.name for m in config.models}
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
                await self.logging_service.add_spend(ctx.auth.key_id, cost)
            except Exception:  # noqa: BLE001
                logger.exception("failed to record spend for key %s", ctx.auth.key_id)

        try:
            self._spawn(_apply())
        except RuntimeError:  # no loop (tests) — apply inline
            pass

    # ------------------------------------------------------------------
    # Shadow mirroring
    # ------------------------------------------------------------------

    def _queue_shadow(self, request: ChatCompletionRequest, ctx: RequestContext) -> None:
        """Mirror a successfully-served request to the model's shadow
        deployments (fire-and-forget, exactly like spend accounting).

        Shadow deployments exist to receive a copy of real traffic so
        their latency / status / content can be compared before being
        promoted to serving. The mirror never affects the client: no
        guardrails, no rate limits, no budgets, no cache, and its cost
        is logged on its own row instead of the key's spend.
        """
        import asyncio

        shadows = self.router.shadow_deployments(ctx.model)
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
            ) -> None:
                await self._mirror_to_shadow(deployment, request, ctx)

            try:
                self._spawn(_mirror())
            except RuntimeError:  # no running loop — nothing to mirror into
                pass

    async def _mirror_to_shadow(
        self,
        deployment: DeploymentConfig,
        request: ChatCompletionRequest,
        ctx: RequestContext,
    ) -> None:
        """Send one non-streaming copy of the request to a shadow deployment.

        Always ``complete()`` (never ``stream()``) so mirrored calls are
        comparable with each other regardless of how the client asked.
        Outcomes land in the shadow metrics, the deployment's circuit
        breaker, and a shadow request-log row sharing the primary's
        request_id — nothing here can reach the client.
        """
        started = time.monotonic()
        # The copy is non-streaming even for streamed primaries.
        mirror_request = request.model_copy(
            update={"stream": False, "stream_options": None}
        )
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
        )
        try:
            await self.logging_service.write_request_log(shadow_ctx)
        except Exception:  # noqa: BLE001
            logger.exception("failed to write shadow log row for %s", deployment.name)

    # ------------------------------------------------------------------
    # Prompt A/B mirroring (prompt ops)
    # ------------------------------------------------------------------

    def _queue_prompt_ab(
        self, request: ChatCompletionRequest, ctx: RequestContext
    ) -> None:
        """Mirror a fraction of successfully-served requests to the
        library prompt's candidate version (fire-and-forget).

        Only applies when the serving system prompt came from the
        library (a key's system_prompt_ref): the candidate swaps in for
        the active body and the request replays against the *same*
        deployment the primary used — same model, same conditions, only
        the prompt differs. The client is never affected (no guardrails,
        no rate limits, no budgets, no cache, no spend; own log row).
        """
        import asyncio

        library_prompt: ResolvedPrompt | None = ctx.library_prompt
        candidate = library_prompt.candidate if library_prompt else None
        if candidate is None or candidate.fraction <= 0.0:
            return
        if candidate.fraction < 1.0 and random.random() > candidate.fraction:
            return

        async def _mirror() -> None:
            await self._mirror_prompt_variant(request, ctx, library_prompt, candidate)

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
    ) -> None:
        """Send one non-streaming copy of the request with the candidate
        prompt to the deployment that served the primary.

        The swap targets the system message that carries the (rendered)
        active body — the message the prompt policy injected. If the
        client supplied its own system message the library prompt never
        served, and if a guardrail rewrote it the comparison would be
        polluted; both cases skip the mirror.
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
        )
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

    def _headers(self, ctx: RequestContext, cache_state: str) -> dict[str, str]:
        return {
            "X-Request-Id": ctx.request_id,
            "X-Gateway-Model": ctx.model,
            "X-Gateway-Cache": cache_state,
            **({"X-Gateway-Provider": ctx.provider} if ctx.provider else {}),
            **({"X-Gateway-Deployment": ctx.deployment} if ctx.deployment else {}),
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
        # One structured record per request — feeds JSON stdout logging and
        # the OTel logs pipeline (with the current span's trace context).
        logger.info(
            event,
            extra={
                "request_id": ctx.request_id,
                "trace_id": ctx.trace_id,
                "span_id": ctx.span_id,
                "key_name": ctx.auth.key_name,
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
                "error": ctx.error_message,
            },
        )
        try:
            await self.logging_service.write_request_log(ctx)
        except Exception:  # noqa: BLE001
            logger.exception("failed to write request log")
