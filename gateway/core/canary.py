"""Synthetic canary probes: scheduled health checks for deployments.

The gap this closes: the gateway only knows a deployment is healthy
when real traffic succeeds through it. A fallback that's never
tried, a shadow that only sees mirrored traffic, a primary that
went quiet overnight — all of those read as "fine" until the moment
they're needed and fail. The prober sends a tiny chat request
straight to each deployment's provider on a schedule and keeps the
last result per deployment, so health is measured, not inferred.

Probes are direct adapter calls (``adapter.complete``): no pipeline,
no guardrails, no cache, no request-log rows, no spend accounting,
no circuit-breaker accounting. They use the same wire the router
would — with none of the request machinery — so they can neither
affect client traffic nor be mistaken for it in logs/stats, and
they never half-open a breaker: the breaker counts client-visible
failures, the canary counts the wire.

Results live in memory (``self.last``, deployment name -> result)
and surface through ``GET /admin/canaries``; ``POST
/admin/canaries/probe`` (see ``api/admin.py``) runs one wave on
demand. Failed probes emit ``canary.failed`` observer events, and a
deployment that returns to ok after a failure emits
``canary.recovered`` — the two transitions an on-call actually
needs. Probes count on ``gateway_canary_probes_total{deployment,
provider, outcome}`` (ok / timeout / error; timeout = the provider
never answered within ``timeout_seconds``, error = anything else).

The scheduled loop never runs under pytest (ASGITransport skips the
lifespan); tests drive ``probe_once()`` directly — see
``tests/test_canary.py``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from ..config import CanaryConfig
from ..errors import ProviderError
from ..observability.metrics import CANARY_PROBES
from ..schemas import ChatCompletionRequest

if TYPE_CHECKING:  # pragma: no cover — import for typing only
    from ..config import DeploymentConfig
    from .events import EventBus
    from .router import Router

logger = logging.getLogger("gateway.canary")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CanaryProber:
    """Stateful prober: one wave = probe every matching deployment."""

    def __init__(
        self,
        router: "Router",
        config: CanaryConfig,
        events: "EventBus | None" = None,
    ) -> None:
        self.router = router
        self.config = config
        self.events = events
        # deployment name -> last probe result (JSON-safe dict). Kept
        # across waves; entries for deployments removed from the config
        # drop out on the next wave.
        self.last: dict[str, dict] = {}

    def _targets(self) -> list[tuple["DeploymentConfig", str]]:
        """(deployment, public model) for every configured deployment
        matching the allowlist, deduped by name, config order. Read
        from ``router.config`` — the same config the router routes
        with, so a hot reload re-targets the next wave."""
        seen: set[str] = set()
        out: list[tuple["DeploymentConfig", str]] = []
        for model_cfg in self.router.config.models:
            for dep in model_cfg.deployments:
                if dep.name in seen:
                    continue
                seen.add(dep.name)
                if self.config.deployments is not None and not any(
                    fnmatch.fnmatch(dep.name, pattern)
                    for pattern in self.config.deployments
                ):
                    continue
                out.append((dep, model_cfg.name))
        return out

    def _request(self, public_model: str) -> ChatCompletionRequest:
        """The tiny probe: one short message, capped output. The
        adapters translate ``deployment.model`` southbound — the
        public name here only shapes the (nonexistent) log entry."""
        return ChatCompletionRequest(
            model=public_model,
            messages=[{"role": "user", "content": self.config.prompt}],
            max_tokens=self.config.max_tokens,
        )

    async def _probe_one(
        self, deployment: "DeploymentConfig", public_model: str
    ) -> dict:
        started = time.perf_counter()
        outcome = "ok"
        error: str | None = None
        try:
            adapter = self.router.providers[deployment.provider]
            provider_cfg = self.router._provider_cfg(deployment)
            bound = self.config.timeout_seconds
            await adapter.complete(
                deployment,
                self._request(public_model),
                provider_cfg,
                # Bounded by canary timeout, not the routing timeout —
                # a probe's job is to *find* slow/dead deployments, so
                # it carries its own clock.
                httpx.Timeout(
                    connect=bound, read=bound, write=bound, pool=bound
                ),
            )
        except ProviderError as exc:
            # _read_json wraps httpx errors as network_error(...) from
            # exc — a TimeoutException in the cause chain means the
            # provider never answered in time (vs. answered badly).
            outcome = (
                "timeout"
                if isinstance(exc.__cause__, httpx.TimeoutException)
                else "error"
            )
            error = exc.message
        except Exception as exc:  # noqa: BLE001 — a probe never raises
            outcome = "error"
            error = str(exc)
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        CANARY_PROBES.labels(
            deployment=deployment.name,
            provider=deployment.provider,
            outcome=outcome,
        ).inc()
        return {
            "deployment": deployment.name,
            "provider": deployment.provider,
            "model": public_model,
            "outcome": outcome,  # ok | timeout | error
            "healthy": outcome == "ok",
            "latency_ms": latency_ms,
            "checked_at": _now().isoformat(),
            "error": error,
        }

    async def probe_once(self) -> list[dict]:
        """One wave: probe every matching deployment in parallel,
        update ``self.last``, emit transition events. Never raises
        past a single deployment's error. Returns results sorted by
        deployment name."""
        targets = self._targets()
        results = list(
            await asyncio.gather(
                *(self._probe_one(dep, model) for dep, model in targets)
            )
        )
        previous = self.last
        self.last = {result["deployment"]: result for result in results}
        if self.events is not None:
            for result in results:
                prev = previous.get(result["deployment"], {}).get("outcome")
                if result["outcome"] != "ok":
                    self.events.emit_background(
                        "canary.failed",
                        None,
                        deployment=result["deployment"],
                        provider=result["provider"],
                        model=result["model"],
                        outcome=result["outcome"],
                        error=result["error"],
                    )
                elif prev not in (None, "ok"):
                    # First wave has no transitions — recovered only
                    # fires when a previously-failing deployment
                    # comes back.
                    self.events.emit_background(
                        "canary.recovered",
                        None,
                        deployment=result["deployment"],
                        provider=result["provider"],
                        model=result["model"],
                        latency_ms=result["latency_ms"],
                    )
        return sorted(results, key=lambda r: r["deployment"])

    def snapshot(self) -> list[dict]:
        """Every matching deployment's latest result, sorted by name —
        never-probed deployments included with outcome "unknown" so
        the admin view lists the whole fleet, not just what has run."""
        out: list[dict] = []
        for deployment, public_model in self._targets():
            result = self.last.get(deployment.name)
            if result is None:
                out.append(
                    {
                        "deployment": deployment.name,
                        "provider": deployment.provider,
                        "model": public_model,
                        "outcome": "unknown",  # never probed
                        "healthy": None,
                        "latency_ms": None,
                        "checked_at": None,
                        "error": None,
                    }
                )
            else:
                out.append(result)
        return sorted(out, key=lambda r: r["deployment"])
