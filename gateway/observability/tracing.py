"""OpenTelemetry tracing configuration.

The gateway emits spans through the OTel *API*, which is a no-op until a real
TracerProvider is installed. Production installs one via ``configure_tracing()``
when ``GATEWAY_OTEL_ENABLED=true``; tests inject a provider with an in-memory
exporter through ``create_app(tracer_provider=...)``.

Span tree for one request (a single trace, propagated via W3C ``traceparent``
headers on the way in and on the way out):

    POST /v1/chat/completions       ASGI server span (FastAPI instrumentation)
    └─ gateway.chat                 pipeline span; for streams it stays open
       │                            until the response body is fully sent
       ├─ gateway.guardrails.request
       ├─ POST <upstream>            httpx client span (provider call, incl.
       │                            per-retry attempts)
       ├─ gateway.guardrails.response
       └─ stream guardrail verdicts → events on gateway.chat

Retries, fallbacks and guardrail verdicts are recorded as *events* on
``gateway.chat``.

Sampling & batching (honoured when tracing is enabled):

- ``OTEL_TRACES_SAMPLER`` / ``OTEL_TRACES_SAMPLER_ARG`` — head-based sampling
  (``always_on``, ``always_off``, ``traceidratio``,
  ``parentbased_always_on``, ``parentbased_always_off``,
  ``parentbased_traceidratio``). Tail-based sampling (keep errors/slow traces)
  belongs at the collector — see ``deploy/otel-collector/config.yaml``.
- ``OTEL_BSP_SCHEDULE_DELAY`` / ``OTEL_BSP_MAX_QUEUE_SIZE`` /
  ``OTEL_BSP_MAX_EXPORT_BATCH_SIZE`` / ``OTEL_BSP_EXPORT_TIMEOUT`` — batch
  span processor tuning (ms / count / count / ms).
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)

from .. import __version__

logger = logging.getLogger("gateway.observability")

_SERVICE_NAME_DEFAULT = "llm-gateway"


def build_resource(service_name: str = _SERVICE_NAME_DEFAULT) -> Resource:
    """Resource shared by the trace, metric and log pipelines."""
    return Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", service_name),
            "service.version": __version__,
        }
    )


def resolve_sampler() -> Sampler | None:
    """Resolve the sampler from the standard OTEL_TRACES_SAMPLER(_ARG) env vars.

    Returns None to use the SDK default (parentbased_always_on) — also the
    fallback for unknown/misconfigured values (with a warning).
    """
    name = (os.environ.get("OTEL_TRACES_SAMPLER") or "").strip().lower()
    if not name:
        return None
    arg = os.environ.get("OTEL_TRACES_SAMPLER_ARG")
    try:
        if name == "always_on":
            return ALWAYS_ON
        if name == "always_off":
            return ALWAYS_OFF
        if name == "traceidratio":
            return TraceIdRatioBased(float(arg or "1"))
        if name == "parentbased_always_on":
            return ParentBased(ALWAYS_ON)
        if name == "parentbased_always_off":
            return ParentBased(ALWAYS_OFF)
        if name == "parentbased_traceidratio":
            return ParentBased(TraceIdRatioBased(float(arg or "1")))
    except ValueError:
        logger.warning("invalid OTEL_TRACES_SAMPLER_ARG %r — using SDK default", arg)
        return None
    logger.warning("unknown OTEL_TRACES_SAMPLER %r — using SDK default", name)
    return None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        logger.warning("invalid %s=%r — using default %s", name, raw, default)
        return default


def build_batch_processor(exporter: SpanExporter) -> BatchSpanProcessor:
    """BatchSpanProcessor honouring the standard OTEL_BSP_* env vars (ms/count)."""
    return BatchSpanProcessor(
        exporter,
        schedule_delay_millis=_env_int("OTEL_BSP_SCHEDULE_DELAY", 5000),
        max_queue_size=_env_int("OTEL_BSP_MAX_QUEUE_SIZE", 2048),
        max_export_batch_size=_env_int("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", 512),
        export_timeout_millis=_env_int("OTEL_BSP_EXPORT_TIMEOUT", 30000),
    )


def configure_tracing(service_name: str = _SERVICE_NAME_DEFAULT) -> TracerProvider:
    """Install and return the global TracerProvider.

    Exporter selection:

    - ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``)
      set → OTLP over HTTP (an OpenTelemetry Collector, Jaeger, Tempo, ...)
    - otherwise → console, so a plain ``GATEWAY_OTEL_ENABLED=true`` run is
      still visibly tracing.

    The standard ``OTEL_SERVICE_NAME`` variable overrides ``service_name``.
    """
    provider = TracerProvider(
        resource=build_resource(service_name), sampler=resolve_sampler()
    )
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    if endpoint:
        # Local import: keeps the protobuf-backed exporter out of the import
        # path of processes that only use the (no-op) OTel API.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(build_batch_processor(OTLPSpanExporter()))
    else:
        provider.add_span_processor(build_batch_processor(ConsoleSpanExporter()))
    # No-op (with a warning) if a provider is already installed — the OTel
    # global can only be set once per process.
    trace.set_tracer_provider(provider)
    return provider
