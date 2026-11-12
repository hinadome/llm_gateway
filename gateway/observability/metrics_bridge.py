"""Bridge prometheus_client metrics into an OpenTelemetry metrics pipeline.

Prometheus scraping of ``/metrics`` stays the primary, full-fidelity metrics
path. This bridge *additionally* mirrors the same series — same names, same
labels — into an OTLP metrics pipeline, for backends that ingest OTel
natively. It reads the live ``prometheus_client`` registry from observable
instrument callbacks on each collection interval, so there is no change to
how the gateway records metrics.

Mapping (per prometheus family, ``gateway.*`` only):

- counters  → observable counters (same name)
- gauges    → observable gauges (same name)
- histograms → three observable gauges each: ``<name>_count``,
  ``<name>_sum`` and ``<name>_bucket`` with an ``le`` label — the raw data
  points the collector/OTel spec use for quantiles. (This SDK has no
  observable histogram instrument; the scrape path serves exact histograms.)

Started by ``start_metrics_bridge()`` when tracing is enabled and an OTLP
endpoint is configured.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

from opentelemetry.metrics import CallbackOptions, Meter, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from prometheus_client import REGISTRY
from prometheus_client.metrics_core import Metric

from .tracing import build_resource

logger = logging.getLogger("gateway.observability")

GATEWAY_PREFIX = "gateway"


def _observations(sample_name: str):
    """Callback returning the current values of one prometheus sample name."""

    def callback(options: CallbackOptions) -> Iterable[Observation]:
        for family in REGISTRY.collect():
            for sample in family.samples:
                if sample.name == sample_name:
                    yield Observation(sample.value, dict(sample.labels))

    return callback


def _install_bridge_instruments(meter: Meter) -> None:
    """Create one observable instrument per (gateway-prefixed) sample name.

    Sample names are derived from each family's *type*, not from the samples
    it currently exposes: prometheus_client emits no samples for labeled
    families until the first label combination is used, so a cold-start
    install must not depend on live samples.

    - counter   → ``<family>_total`` as an observable counter
    - gauge     → ``<family>`` as an observable gauge
    - histogram → ``<family>_count`` / ``<family>_sum`` / ``<family>_bucket``
      as observable gauges (``bucket`` carries the ``le`` label) — this SDK
      has no observable histogram instrument
    """
    created: set[str] = set()
    for family in REGISTRY.collect():
        if not family.name.startswith(GATEWAY_PREFIX):
            continue
        if family.type == "counter":
            names_and_kinds = [(family.name + "_total", "counter")]
        elif family.type == "gauge":
            names_and_kinds = [(family.name, "gauge")]
        elif family.type == "histogram":
            names_and_kinds = [
                (family.name + "_count", "gauge"),
                (family.name + "_sum", "gauge"),
                (family.name + "_bucket", "gauge"),
            ]
        else:  # summary / info / stateset — not produced by the gateway
            continue
        for name, kind in names_and_kinds:
            if name in created:
                continue
            created.add(name)
            try:
                if kind == "counter":
                    meter.create_observable_counter(
                        name, callbacks=[_observations(name)],
                        description=family.documentation,
                    )
                else:
                    meter.create_observable_gauge(
                        name, callbacks=[_observations(name)],
                        description=family.documentation,
                    )
            except Exception:  # noqa: BLE001 — a bad name must not break startup
                logger.warning("could not bridge prometheus metric %r", name)


def start_metrics_bridge(
    resource: Resource | None = None, export_interval_millis: int = 30_000
) -> MeterProvider | None:
    """Start the OTLP metrics bridge; returns the provider (None if skipped).

    Skipped when no OTLP endpoint is configured — in that case Prometheus
    scraping of ``/metrics`` is the only (and complete) metrics path.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    if not endpoint:
        return None

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(), export_interval_millis=export_interval_millis
    )
    provider = MeterProvider(
        metric_readers=[reader], resource=resource or build_resource()
    )
    _install_bridge_instruments(provider.get_meter("gateway.prometheus_bridge"))
    logger.info(
        "prometheus -> otel metrics bridge started (interval=%dms)", export_interval_millis
    )
    return provider


__all__ = ["start_metrics_bridge", "_install_bridge_instruments"]
