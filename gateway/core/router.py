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
from ..errors import GatewayError, ModelNotFoundError, ProviderError
from ..schemas import (
    ChatCompletionRequest,
    EmbeddingRequest,
    ImageEditRequest,
    ImageGenerationRequest,
    ImageVariationRequest,
    SpeechRequest,
    TranscriptionRequest,
)
from ..observability import metrics
from .breaker import CircuitBreakerRegistry
from .context import AuthContext
from .cost import CostCalculator
from .vault import ByokKeyRequiredError


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

    The embedder accepts an optional ``residencies`` argument (the
    CALLER's effective data-residency requirement) so a restricted
    caller's prompt is only embedded compliantly: the router filters
    to residency-tagged deployments, and a model with no compliant
    deployment raises — which the semantic cache treats as a miss
    (restricted callers simply lose semantic-cache hits, never
    correctness).

    BYOK (tier 31): no ``auth`` is passed — the semantic cache is the
    platform's own machinery, so the platform's configured provider
    key embeds (the caller's stored credential, if any, is NOT used;
    it also must never BLOCK an embed: an embedder leg on an unkeyed
    provider simply rides the platform key, same as shadow mirrors
    and canary probes).
    """

    async def embed(
        text: str, residencies: frozenset[str] | None = None
    ) -> list[float]:
        request = EmbeddingRequest(model=model, input=text)
        _, response = await router.embed(request, residencies)
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
        vault=None,
    ) -> None:
        """``breakers``: a pre-built registry to use instead of the
        default in-memory one (main.py passes the Redis-backed registry
        when ``routing.breaker.backend: redis``; the interface is the
        same). The clock is injectable so tests can fast-forward
        cooldowns.

        ``vault`` (tier 31): the BYOK vault service. When a dispatch
        carries the caller's auth, a tenant with a stored credential
        for the deployment's provider rides THEIR key instead of the
        platform's configured one — see _resolve_provider_cfg. None =
        platform credentials everywhere (the vault is off or absent).
        """
        self.config = config
        self.providers = providers
        # Per-deployment circuit breakers (health-aware routing).
        self.breakers = breakers or CircuitBreakerRegistry(config.routing, clock=clock)
        # BYOK vault (optional — main.py always passes one; it is
        # disabled unless GATEWAY_VAULT_MASTER_KEY is set).
        self.vault = vault
        # Pricing view for cost-aware routing (_pick); rebuilt on config
        # swap so pricing hot-reloads apply. Same CostCalculator billing
        # uses, looked up on the deployment's upstream model.
        self._cost = CostCalculator(config.pricing)
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
        self._cost = CostCalculator(config.pricing)
        self._groups = {
            model.name: sorted(model.deployments, key=lambda d: d.priority)
            for model in config.models
        }
        for model in config.models:
            for deployment in model.deployments:
                self.breakers.register(deployment.name, deployment.provider)

    # -- selection -------------------------------------------------------------

    def resolve(
        self, public_model: str, residencies: frozenset[str] | None = None
    ) -> list[DeploymentConfig]:
        """Serving deployments for a public model, priority-sorted.

        Shadow deployments are excluded: they never serve client traffic
        (use ``shadow_deployments`` to find the mirroring targets for a
        model). A model whose deployments are *all* shadow cannot serve
        and fails loudly with a 503 rather than a confusing 404.

        ``residencies`` (the caller's effective data-residency
        requirement, None = unrestricted) filters the list to
        deployments whose ``residency`` tag is in the set — BEFORE the
        priority ladder, retries and fallbacks, so a constrained
        request can never escape to a non-compliant deployment mid-
        failover. An untagged deployment satisfies no requirement
        (fail closed: no claim, no compliance). A model with no
        compliant deployment fails with 404 residency_not_satisfied.
        """
        deployments = self._groups.get(public_model)
        if not deployments:
            raise ModelNotFoundError(public_model)
        serving = [d for d in deployments if not d.shadow]
        if residencies is not None:
            serving = [d for d in serving if d.residency in residencies]
            if not serving:
                raise GatewayError(
                    404,
                    f"Model '{public_model}' has no deployment satisfying "
                    f"the data-residency requirement "
                    f"({', '.join(sorted(residencies)) or 'none'}).",
                    "invalid_request_error",
                    code="residency_not_satisfied",
                    param="model",
                )
        if not serving:
            raise ProviderError(
                503,
                f"Model '{public_model}' has no serving deployments "
                f"(all are shadow).",
                "api_error",
            )
        return serving

    def shadow_deployments(
        self, public_model: str, residencies: frozenset[str] | None = None
    ) -> list[DeploymentConfig]:
        """Shadow (mirroring-target) deployments attached to a model —
        filtered by the caller's residency requirement too: a mirror is
        a copy of the caller's data, so it may only land on compliant
        deployments. Non-compliant shadows silently receive nothing (the
        mirror is best-effort by design)."""
        deployments = self._groups.get(public_model)
        if not deployments:
            return []
        shadows = [d for d in deployments if d.shadow]
        if residencies is not None:
            shadows = [d for d in shadows if d.residency in residencies]
        return shadows

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
        """Weighted pick among same-priority deployments.

        With ``routing.cost_aware`` the tier is cost-ordered instead:
        the cheapest *priced* upstream model wins (unpriced sort last —
        unknown cost gets no discount), and weight only breaks ties
        between equal prices. The dispatch loop removes exhausted
        deployments and re-picks, so the price ladder doubles as the
        failover ladder.
        """
        first_priority = deployments[0].priority
        tier = [d for d in deployments if d.priority == first_priority]
        if len(tier) == 1:
            return tier[0]
        if self.config.routing.cost_aware:
            prices = [self._cost.input_price(d.model) for d in tier]
            priced = [p for p in prices if p is not None]
            if priced:
                cheapest = min(priced)
                tied = [d for d, p in zip(tier, prices) if p == cheapest]
                if len(tied) == 1:
                    return tied[0]
                tier = tied  # equal prices: weighted-random among them
            # No priced deployment in the tier: unknown costs all
            # "tie" — plain weighted-random, as configured.
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

    def _strict_on(self, auth: "AuthContext | None") -> bool:
        """vault.strict applies to TENANT traffic only — platform
        callers (auth None: admin/env machinery) and platform admins
        (no tenant) ride the platform's configured keys, exactly as
        before. Shadow mirrors and canary probes never reach this
        check: they resolve through the sync ``_provider_cfg``
        (platform machinery rides platform credentials by design)."""
        return bool(
            self.config.vault.strict
            and auth is not None
            and auth.tenant_id is not None
        )

    async def _strict_filter(
        self,
        deployments: list[DeploymentConfig],
        auth: "AuthContext | None",
    ) -> list[DeploymentConfig]:
        """The vault.strict ladder filter (tier 34): keep only
        deployments whose provider the caller's tenant has a stored
        key for, so retries and fallbacks can never cross to an
        unkeyed provider — a strict tenant's request either rides
        THEIR credentials or fails. The lookup is the vault's cached
        one (a dict read after the first miss); an entry that exists
        but cannot be decrypted still raises byok_key_unreadable
        (fail closed, unchanged). Returns the list untouched when
        strict is off or the caller is not tenant-scoped."""
        if not self._strict_on(auth):
            return deployments
        # No vault wired (only direct constructions): strict means
        # nothing is keyed — an empty ladder, the honest refusal.
        if self.vault is None or not self.vault.enabled:
            return []
        keyed: list[DeploymentConfig] = []
        for deployment in deployments:
            entry = await self.vault.get(auth.tenant_id, deployment.provider)
            if entry is not None:
                keyed.append(deployment)
        return keyed

    async def _enforce_strict(
        self,
        deployments: list[DeploymentConfig],
        auth: "AuthContext | None",
        model: str,
    ) -> list[DeploymentConfig]:
        """Strict wrapper for the ladder head: filter, then refuse —
        with the exact providers to key — when nothing remains. The
        breaker filter runs FIRST and keeps its own all-open rule (an
        open-but-keyed deployment still serves; strict only removes
        deployments, never re-adds them)."""
        eligible = await self._strict_filter(deployments, auth)
        if not eligible and self._strict_on(auth):
            raise ByokKeyRequiredError(
                tenant_id=auth.tenant_id,
                tenant_name=auth.tenant_name,
                model=model,
                providers=sorted({d.provider for d in deployments}),
            )
        return eligible

    async def _resolve_provider_cfg(
        self,
        deployment: DeploymentConfig,
        auth: "AuthContext | None" = None,
    ) -> ProviderConfig:
        """The provider config for a serving call, with the BYOK vault
        applied (tier 31): when the caller belongs to a tenant that
        stored a credential for this deployment's provider, that key
        replaces the platform's — a copy with ``api_key`` set and
        ``api_key_env`` cleared, so ``resolve_key()`` (the single place
        every adapter reads credentials) returns the tenant's key. The
        copy is never cached on the config itself; the vault caches the
        decrypted entry for ttl_seconds, so the hot path is a dict read.

        Platform callers (auth None, admin/env traffic) and tenants
        without a stored key get the platform config untouched. An
        entry that cannot be decrypted fails the request
        (byok_key_unreadable, fail closed) rather than silently
        billing the platform. Shadow mirrors and canary probes call
        the sync ``_provider_cfg`` instead — they are the platform's
        own machinery and ride platform credentials by design."""
        cfg = self._provider_cfg(deployment)
        if (
            self.vault is None
            or not self.vault.enabled
            or auth is None
            or auth.tenant_id is None
        ):
            return cfg
        entry = await self.vault.get(auth.tenant_id, deployment.provider)
        if entry is None:
            return cfg
        return cfg.model_copy(update={"api_key": entry.key, "api_key_env": None})

    async def resolve_realtime(
        self,
        model: str,
        residencies: frozenset[str] | None = None,
    ) -> list[DeploymentConfig]:
        """The candidate ladder for a realtime (WebSocket) session
        (tier 32): resolve → breaker-filter, in priority order. The
        CALLER owns failover — a long-lived session cannot be retried
        mid-stream the way HTTP dispatch can, so realtime walks
        candidates at CONNECT time only (a deployment whose WS
        handshake fails moves the ladder to the next one) and stays
        on the deployment it connected to for the session's life.
        Callers further filter to providers with realtime_base set —
        the realtime-capability requirement is a provider property."""
        deployments = self.resolve(model, residencies)
        return await self._available(deployments)

    @staticmethod
    def _backoff(attempt: int, base: float) -> float:
        return min(8.0, base * (2**attempt)) * (0.5 + random.random())

    # -- non-streaming -----------------------------------------------------------

    async def _dispatch(
        self,
        model: str,
        call: Callable[..., Awaitable],
        residencies: frozenset[str] | None = None,
        auth: AuthContext | None = None,
    ) -> tuple[DeploymentConfig, Any]:
        """Shared non-streaming executor for every modality: resolve →
        breaker filter → retry/backoff per deployment → fallback across
        deployments. ``call(adapter, deployment, provider_cfg, timeout)``
        performs the provider call and returns its response.
        ``residencies`` is the caller's effective data-residency
        requirement (None = unrestricted) — it filters the candidate
        list before anything else, so retries and fallbacks can never
        escape to a non-compliant deployment.
        ``auth`` is the caller's snapshot (None = platform traffic):
        each candidate deployment's provider config is resolved through
        the BYOK vault, so a tenant's stored key follows the request
        through EVERY retry and fallback — the fallback may cross to a
        provider the tenant did not key, and that leg (alone) rides
        the platform's credential (see _resolve_provider_cfg).
        vault.strict (tier 34) narrows the ladder FIRST: a strict
        tenant's fallback never crosses to an unkeyed provider —
        those deployments are removed before the walk, and an empty
        ladder refuses with 403 byok_key_required (see
        _enforce_strict)."""
        deployments = self.resolve(model, residencies)
        remaining = await self._available(deployments)
        # vault.strict: only providers the tenant keyed may serve it.
        remaining = await self._enforce_strict(remaining, auth, model)
        last_error: ProviderError | None = None
        tried: set[str] = set()

        while remaining:
            deployment = self._pick(remaining)
            if deployment.name in tried:
                remaining.remove(deployment)
                continue
            tried.add(deployment.name)
            adapter = self.providers[deployment.provider]
            provider_cfg = await self._resolve_provider_cfg(deployment, auth)
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

    async def complete(
        self,
        request: ChatCompletionRequest,
        residencies: frozenset[str] | None = None,
        auth: AuthContext | None = None,
    ) -> tuple[DeploymentConfig, dict]:
        return await self._dispatch(
            request.model,
            lambda a, d, pc, t: a.complete(d, request, pc, t),
            residencies,
            auth,
        )

    async def embed(
        self,
        request: EmbeddingRequest,
        residencies: frozenset[str] | None = None,
        auth: AuthContext | None = None,
    ) -> tuple[DeploymentConfig, dict]:
        """Route an embeddings request — same retry/fallback/breaker
        contract as ``complete`` (embeddings are never streamed).
        ``residencies``: the caller's effective data-residency
        requirement (None = unrestricted) — filters the candidate
        deployments before the priority ladder."""
        return await self._dispatch(
            request.model,
            lambda a, d, pc, t: a.embed(d, request, pc, t),
            residencies,
            auth,
        )

    async def generate_image(
        self,
        request: ImageGenerationRequest,
        residencies: frozenset[str] | None = None,
        auth: AuthContext | None = None,
    ) -> tuple[DeploymentConfig, dict]:
        """Route an image-generation request (same contract as embed)."""
        return await self._dispatch(
            request.model,
            lambda a, d, pc, t: a.generate_image(d, request, pc, t),
            residencies,
            auth,
        )

    async def edit_image(
        self,
        request: ImageEditRequest,
        residencies: frozenset[str] | None = None,
        auth: AuthContext | None = None,
    ) -> tuple[DeploymentConfig, dict]:
        """Route an image-edit request (uploaded image(s) + prompt)."""
        return await self._dispatch(
            request.model,
            lambda a, d, pc, t: a.edit_image(d, request, pc, t),
            residencies,
            auth,
        )

    async def vary_image(
        self,
        request: ImageVariationRequest,
        residencies: frozenset[str] | None = None,
        auth: AuthContext | None = None,
    ) -> tuple[DeploymentConfig, dict]:
        """Route an image-variation request (uploaded image, no prompt)."""
        return await self._dispatch(
            request.model,
            lambda a, d, pc, t: a.vary_image(d, request, pc, t),
            residencies,
            auth,
        )

    async def transcribe(
        self,
        request: TranscriptionRequest,
        residencies: frozenset[str] | None = None,
        auth: AuthContext | None = None,
    ) -> tuple[DeploymentConfig, tuple[dict | str, str]]:
        """Route an audio transcription/translation request."""
        return await self._dispatch(
            request.model,
            lambda a, d, pc, t: a.transcribe(d, request, pc, t),
            residencies,
            auth,
        )

    async def speak(
        self,
        request: SpeechRequest,
        residencies: frozenset[str] | None = None,
        auth: AuthContext | None = None,
    ) -> tuple[DeploymentConfig, tuple[bytes, str]]:
        """Route a text-to-speech request (buffered; audio is not streamed
        through the gateway in this version)."""
        return await self._dispatch(
            request.model,
            lambda a, d, pc, t: a.speak(d, request, pc, t),
            residencies,
            auth,
        )

    # -- streaming ---------------------------------------------------------------

    async def complete_stream(
        self,
        request: ChatCompletionRequest,
        residencies: frozenset[str] | None = None,
        auth: AuthContext | None = None,
    ) -> tuple[DeploymentConfig, AsyncIterator[dict]]:
        """Start a streamed completion.

        Failover happens until the *first chunk* has been received; once the
        stream is committed, upstream errors surface to the client.
        ``residencies`` filters the candidate ladder before anything
        else — same fail-closed contract as ``_dispatch``. ``auth`` is
        the caller's snapshot: a tenant's BYOK key follows the request
        through every failover attempt (per-candidate resolution, the
        same contract as ``_dispatch``), and vault.strict narrows the
        ladder identically (unkeyed deployments removed before the
        walk; an empty ladder refuses with 403 byok_key_required).
        """
        deployments = self.resolve(request.model, residencies)
        remaining = await self._available(deployments)
        # vault.strict: only providers the tenant keyed may serve it.
        remaining = await self._enforce_strict(remaining, auth, request.model)
        last_error: ProviderError | None = None
        tried: set[str] = set()

        while remaining:
            deployment = self._pick(remaining)
            if deployment.name in tried:
                remaining.remove(deployment)
                continue
            tried.add(deployment.name)
            adapter = self.providers[deployment.provider]
            provider_cfg = await self._resolve_provider_cfg(deployment, auth)
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
