"""Model router: deployment selection, retries and fallbacks.

A *deployment* is one concrete upstream (provider + native model). All
deployments sharing a public model name form a routing group. Groups are
tried in ascending ``priority``; within the same priority, deployments are
weighted-random load balanced. Retryable failures are retried (with backoff)
on the same deployment, then the group falls through to the next one.

Each deployment also has a circuit breaker (see ``breaker.py``): a
deployment that repeatedly fails requests is skipped entirely for a
cooldown period, then probed with real traffic before being readmitted —
so failover stops paying the full retry budget on every request.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
from opentelemetry import trace

from ..config import DeploymentConfig, GatewayConfig, ProviderConfig
from ..errors import ModelNotFoundError, ProviderError
from ..schemas import (
    ChatCompletionRequest,
    EmbeddingRequest,
    ImageGenerationRequest,
    SpeechRequest,
    TranscriptionRequest,
)
from ..observability import metrics
from .breaker import CircuitBreakerRegistry


def _trace_retry(deployment: DeploymentConfig, attempt: int, error: str) -> None:
    """Record a retry as an event on the enclosing gateway.chat span."""
    trace.get_current_span().add_event(
        "gateway.retry",
        {
            "gateway.deployment": deployment.name,
            "gateway.provider": deployment.provider,
            "attempt": attempt,
            "error": error,
        },
    )


def _trace_fallback(deployment: DeploymentConfig, error: str) -> None:
    """Record a cross-deployment fallback as an event on gateway.chat."""
    trace.get_current_span().add_event(
        "gateway.fallback",
        {
            "gateway.from_deployment": deployment.name,
            "gateway.provider": deployment.provider,
            "error": error,
        },
    )


def _trace_circuit_open(deployment: DeploymentConfig) -> None:
    """Record 'deployment skipped, circuit open' on gateway.chat."""
    trace.get_current_span().add_event(
        "gateway.circuit_open",
        {
            "gateway.deployment": deployment.name,
            "gateway.provider": deployment.provider,
        },
    )


def embed_fn_for(router: "Router", model: str) -> Callable[[str], Awaitable[list[float]]]:
    """Build the semantic-cache embedder over the router's embeddings path.

    Routes a one-input embeddings request through the normal machinery
    (retries, fallbacks, breakers). The call is internal: it is billed
    to the gateway's provider key, not the caller's virtual key, and it
    bypasses the response cache by design (``Router.embed`` never
    consults it, so there is no recursion).
    """

    async def embed(text: str) -> list[float]:
        request = EmbeddingRequest(model=model, input=text)
        _, response = await router.embed(request)
        data = response.get("data") or [{}]
        embedding = (data[0] or {}).get("embedding")
        if not embedding:
            raise ValueError(f"embedder model '{model}' returned no vector")
        return embedding

    return embed


class Router:
    def __init__(
        self,
        config: GatewayConfig,
        providers: dict,
        clock: Callable[[], float] = time.monotonic,
        breakers=None,
    ) -> None:
        """``breakers``: a pre-built registry to use instead of the
        default in-memory one (main.py passes the Redis-backed registry
        when ``routing.breaker.backend: redis``; the interface is the
        same). The clock is injectable so tests can fast-forward
        cooldowns."""
        self.config = config
        self.providers = providers
        # Per-deployment circuit breakers (health-aware routing).
        self.breakers = breakers or CircuitBreakerRegistry(config.routing, clock=clock)
        self._groups: dict[str, list[DeploymentConfig]] = {}
        for model in config.models:
            self._groups[model.name] = sorted(
                model.deployments, key=lambda d: d.priority
            )
            for deployment in model.deployments:
                self.breakers.register(deployment.name, deployment.provider)

    def apply_config(self, config: GatewayConfig) -> None:
        """Hot-reload: swap models/deployments and routing policy in place.

        Circuit-breaker state and provider adapters survive the swap —
        breakers are shared state (possibly Redis-backed, and an open
        breaker should not reset just because config changed) and the
        adapters hold the HTTP client. Called by
        ``GatewayPipeline.apply_config``; provider credentials are NOT
        picked up here (they live on the adapters — restart to rotate
        those, or point them at a proxy)."""
        self.config = config
        self._groups = {
            model.name: sorted(model.deployments, key=lambda d: d.priority)
            for model in config.models
        }
        for model in config.models:
            for deployment in model.deployments:
                self.breakers.register(deployment.name, deployment.provider)

    # -- selection -------------------------------------------------------------

    def resolve(self, public_model: str) -> list[DeploymentConfig]:
        """Serving deployments for a public model, priority-sorted.

        Shadow deployments are excluded: they never serve client traffic
        (use ``shadow_deployments`` to find the mirroring targets for a
        model). A model whose deployments are *all* shadow cannot serve
        and fails loudly with a 503 rather than a confusing 404.
        """
        deployments = self._groups.get(public_model)
        if not deployments:
            raise ModelNotFoundError(public_model)
        serving = [d for d in deployments if not d.shadow]
        if not serving:
            raise ProviderError(
                503,
                f"Model '{public_model}' has no serving deployments "
                f"(all are shadow).",
                "api_error",
            )
        return serving

    def shadow_deployments(self, public_model: str) -> list[DeploymentConfig]:
        """Shadow (mirroring-target) deployments attached to a model."""
        deployments = self._groups.get(public_model)
        if not deployments:
            return []
        return [d for d in deployments if d.shadow]

    def find_deployment(self, name: str) -> DeploymentConfig | None:
        """A deployment by its config name, across all models (used by the
        prompt A/B mirror to re-address the deployment that served the
        primary — same model, same conditions, only the prompt differs)."""
        for deployments in self._groups.values():
            for deployment in deployments:
                if deployment.name == name:
                    return deployment
        return None

    async def _available(self, deployments: list[DeploymentConfig]) -> list[DeploymentConfig]:
        """Drop deployments whose circuit is open.

        If every circuit is open, the full list is returned: the router
        would rather retry a known-bad deployment than fail without
        trying (an open circuit is evidence, not proof).
        """
        if not self.breakers.enabled:
            return deployments
        available = [
            d for d in deployments if await self.breakers.allow(d.name)
        ]
        if not available:
            return deployments
        available_names = {d.name for d in available}
        for deployment in deployments:
            if deployment.name not in available_names:
                _trace_circuit_open(deployment)
        return available

    def _pick(self, deployments: list[DeploymentConfig]) -> DeploymentConfig:
        """Weighted pick among same-priority deployments."""
        first_priority = deployments[0].priority
        tier = [d for d in deployments if d.priority == first_priority]
        if len(tier) == 1:
            return tier[0]
        weights = [max(1, d.weight) for d in tier]
        return random.choices(tier, weights=weights, k=1)[0]

    def _timeout(self, deployment: DeploymentConfig) -> httpx.Timeout:
        seconds = deployment.timeout_seconds or self.config.routing.timeout_seconds
        return httpx.Timeout(connect=10.0, read=seconds, write=30.0, pool=10.0)

    def _provider_cfg(self, deployment: DeploymentConfig) -> ProviderConfig:
        cfg = self.config.providers.get(deployment.provider)
        if cfg is None:
            raise ProviderError(
                500,
                f"No provider configuration for '{deployment.provider}' "
                f"(deployment '{deployment.name}')",
                "api_error",
            )
        return cfg

    @staticmethod
    def _backoff(attempt: int, base: float) -> float:
        return min(8.0, base * (2**attempt)) * (0.5 + random.random())

    # -- non-streaming -----------------------------------------------------------

    async def _dispatch(
        self, model: str, call: Callable[..., Awaitable]
    ) -> tuple[DeploymentConfig, Any]:
        """Shared non-streaming executor for every modality: resolve →
        breaker filter → retry/backoff per deployment → fallback across
        deployments. ``call(adapter, deployment, provider_cfg, timeout)``
        performs the provider call and returns its response."""
        deployments = self.resolve(model)
        remaining = await self._available(deployments)
        last_error: ProviderError | None = None
        tried: set[str] = set()

        while remaining:
            deployment = self._pick(remaining)
            if deployment.name in tried:
                remaining.remove(deployment)
                continue
            tried.add(deployment.name)
            adapter = self.providers[deployment.provider]
            provider_cfg = self._provider_cfg(deployment)
            max_retries = (
                deployment.max_retries
                if deployment.max_retries is not None
                else self.config.routing.max_retries
            )

            for attempt in range(max_retries + 1):
                try:
                    response = await call(
                        adapter, deployment, provider_cfg, self._timeout(deployment)
                    )
                    await self.breakers.record_success(deployment.name)
                    return deployment, response
                except ProviderError as exc:
                    last_error = exc
                    metrics.REQUESTS.labels(
                        provider=deployment.provider, model=model, status="retry"
                    ).inc()
                    if not exc.retryable or attempt >= max_retries:
                        break
                    _trace_retry(deployment, attempt + 1, exc.message)
                    await asyncio.sleep(self._backoff(attempt, self.config.routing.retry_backoff_base))

            if last_error is not None and not last_error.fallback:
                raise last_error
            # The request exhausted its retry budget on this deployment:
            # one request-level failure for the circuit breaker.
            await self.breakers.record_failure(deployment.name)
            if len(remaining) > 1:  # only when there is something to fall back to
                _trace_fallback(deployment, last_error.message if last_error else "")
            remaining.remove(deployment)

        raise last_error or ProviderError(502, "All deployments failed", "api_error")

    async def complete(self, request: ChatCompletionRequest) -> tuple[DeploymentConfig, dict]:
        return await self._dispatch(
            request.model, lambda a, d, pc, t: a.complete(d, request, pc, t)
        )

    async def embed(self, request: EmbeddingRequest) -> tuple[DeploymentConfig, dict]:
        """Route an embeddings request — same retry/fallback/breaker
        contract as ``complete`` (embeddings are never streamed)."""
        return await self._dispatch(
            request.model, lambda a, d, pc, t: a.embed(d, request, pc, t)
        )

    async def generate_image(
        self, request: ImageGenerationRequest
    ) -> tuple[DeploymentConfig, dict]:
        """Route an image-generation request (same contract as embed)."""
        return await self._dispatch(
            request.model, lambda a, d, pc, t: a.generate_image(d, request, pc, t)
        )

    async def transcribe(
        self, request: TranscriptionRequest
    ) -> tuple[DeploymentConfig, tuple[dict | str, str]]:
        """Route an audio transcription/translation request."""
        return await self._dispatch(
            request.model, lambda a, d, pc, t: a.transcribe(d, request, pc, t)
        )

    async def speak(self, request: SpeechRequest) -> tuple[DeploymentConfig, tuple[bytes, str]]:
        """Route a text-to-speech request (buffered; audio is not streamed
        through the gateway in this version)."""
        return await self._dispatch(
            request.model, lambda a, d, pc, t: a.speak(d, request, pc, t)
        )

    # -- streaming ---------------------------------------------------------------

    async def complete_stream(
        self, request: ChatCompletionRequest
    ) -> tuple[DeploymentConfig, AsyncIterator[dict]]:
        """Start a streamed completion.

        Failover happens until the *first chunk* has been received; once the
        stream is committed, upstream errors surface to the client.
        """
        deployments = self.resolve(request.model)
        remaining = await self._available(deployments)
        last_error: ProviderError | None = None
        tried: set[str] = set()

        while remaining:
            deployment = self._pick(remaining)
            if deployment.name in tried:
                remaining.remove(deployment)
                continue
            tried.add(deployment.name)
            adapter = self.providers[deployment.provider]
            provider_cfg = self._provider_cfg(deployment)
            max_retries = (
                deployment.max_retries
                if deployment.max_retries is not None
                else self.config.routing.max_retries
            )

            for attempt in range(max_retries + 1):
                try:
                    generator = adapter.stream(
                        deployment, request, provider_cfg, self._timeout(deployment)
                    )
                    first_chunk = await generator.__anext__()
                except StopAsyncIteration:
                    break  # empty stream — try next deployment
                except ProviderError as exc:
                    last_error = exc
                    metrics.REQUESTS.labels(
                        provider=deployment.provider, model=request.model, status="retry"
                    ).inc()
                    if not exc.retryable or attempt >= max_retries:
                        break
                    _trace_retry(deployment, attempt + 1, exc.message)
                    await asyncio.sleep(self._backoff(attempt, self.config.routing.retry_backoff_base))
                    continue
                except httpx.HTTPError as exc:
                    last_error = ProviderError(
                        503,
                        f"Could not reach provider '{deployment.provider}': {exc}",
                        "api_error",
                        retryable=True,
                        fallback=True,
                        provider=deployment.provider,
                    )
                    metrics.REQUESTS.labels(
                        provider=deployment.provider, model=request.model, status="retry"
                    ).inc()
                    if attempt >= max_retries:
                        break
                    _trace_retry(deployment, attempt + 1, last_error.message)
                    await asyncio.sleep(self._backoff(attempt, self.config.routing.retry_backoff_base))
                    continue

                async def chained(
                    first: dict = first_chunk, gen: AsyncIterator[dict] = generator
                ) -> AsyncIterator[dict]:
                    yield first
                    async for chunk in gen:
                        yield chunk

                # First chunk received: the deployment is serving traffic —
                # close/re-close its circuit. (Failures *after* this point
                # surface to the client per the failover contract and do not
                # trip the breaker.)
                await self.breakers.record_success(deployment.name)
                return deployment, chained()

            if last_error is not None and not last_error.fallback:
                raise last_error
            # Retry budget exhausted (or empty stream) on this deployment:
            # one request-level failure for the circuit breaker.
            await self.breakers.record_failure(deployment.name)
            if len(remaining) > 1:  # only when there is something to fall back to
                _trace_fallback(deployment, last_error.message if last_error else "")
            remaining.remove(deployment)

        raise last_error or ProviderError(502, "All deployments failed", "api_error")
