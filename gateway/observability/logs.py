"""Structured logging: JSON output + an OpenTelemetry logs pipeline.

Two independent pieces, both configured from ``GATEWAY_LOG_FORMAT`` /
``GATEWAY_OTEL_ENABLED``:

1. **JSON logs** (``GATEWAY_LOG_FORMAT=json``) — the gateway's loggers emit
   single-line JSON records to stdout, friendly to Loki/Elastic/any JSON
   ingester. The pipeline writes one such record per request ("chat request
   completed") carrying request_id, trace_id, model, provider, tokens,
   cost, status, ...
2. **OTel logs pipeline** (``GATEWAY_OTEL_ENABLED=true`` + an OTLP endpoint) —
   a ``LoggerProvider`` with an OTLP log exporter, fed by ``OTelLogHandler``,
   a small ``logging.Handler`` that translates Python records to OTel log
   records and stamps them with the *current span* context. Because the
   pipeline emits its per-request record while the ``gateway.chat`` span is
   active, exported log records carry the right trace/span ids and join up
   with traces in OTel-native backends.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

from opentelemetry._logs import LogRecord as OTelLogRecord
from opentelemetry._logs import get_logger as get_otel_logger
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.trace import get_current_span

from ..config import Settings
from .tracing import build_resource

logger = logging.getLogger("gateway.observability")

# Standard LogRecord attributes that are not interesting as JSON/OTel fields.
_RESERVED_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)

# Python level name -> OTel severity number (see the OTel severity table).
_SEVERITY_NUMBERS = {
    "TRACE": 1, "DEBUG": 5, "INFO": 9, "WARNING": 13, "WARN": 13,
    "ERROR": 17, "CRITICAL": 21, "FATAL": 21,
}


class JsonFormatter(logging.Formatter):
    """Serialize log records as single-line JSON objects.

    All non-standard record attributes (``logger.info(..., extra={...})``)
    become top-level JSON fields; ``None`` values are dropped.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            )
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in vars(record).items():
            if key in _RESERVED_ATTRS or key.startswith("_") or value is None:
                continue
            payload[key] = value
        return json.dumps(payload, default=str, ensure_ascii=False)


class OTelLogHandler(logging.Handler):
    """Forward Python log records into an OTel logs pipeline.

    Unlike the (deprecated) SDK ``LoggingHandler``, this stamps each record
    with the *current span*'s context explicitly — so records emitted while a
    request span is active carry that trace/span id.
    """

    def __init__(self, logger_provider: LoggerProvider) -> None:
        super().__init__()
        self._logger_provider = logger_provider

    @staticmethod
    def _attributes(record: logging.LogRecord) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        for key, value in vars(record).items():
            if key in _RESERVED_ATTRS or key.startswith("_") or value is None:
                continue
            if isinstance(value, (str, bool, int, float)):
                attributes[key] = value
        return attributes

    def emit(self, record: logging.LogRecord) -> None:
        try:
            otel_logger = get_otel_logger(
                record.name, logger_provider=self._logger_provider
            )
            span_context = get_current_span().get_span_context()
            context_kwargs: dict[str, Any] = {}
            if span_context.is_valid:
                context_kwargs = {
                    "trace_id": span_context.trace_id,
                    "span_id": span_context.span_id,
                    "trace_flags": span_context.trace_flags,
                }
            otel_logger.emit(
                OTelLogRecord(
                    timestamp=int(record.created * 1e9),
                    observed_timestamp=time.time_ns(),
                    severity_text=record.levelname,
                    severity_number=_SEVERITY_NUMBERS.get(
                        record.levelname, 9
                    ),
                    body=record.getMessage(),
                    attributes=self._attributes(record),
                    **context_kwargs,
                )
            )
        except Exception:  # noqa: BLE001 — never break the caller's logging
            self.handleError(record)


def configure_logging(settings: Settings) -> None:
    """Apply GATEWAY_LOG_FORMAT / GATEWAY_LOG_LEVEL to the gateway's loggers.

    - ``json``: single-line JSON to stdout (propagation off, so records are
      not double-printed by any root handlers).
    - ``text`` (default): stdlib/uvicorn behaviour, untouched.

    Idempotent — safe to call for every ``create_app``.
    """
    gw = logging.getLogger("gateway")
    try:
        gw.setLevel((settings.log_level or "INFO").upper())
    except ValueError:
        gw.setLevel(logging.INFO)

    # Remove handlers a previous create_app installed (tests create apps
    # repeatedly in one process).
    for handler in list(gw.handlers):
        if getattr(handler, "_gateway_json", False):
            gw.removeHandler(handler)
            handler.close()

    if (settings.log_format or "text").lower() == "json":
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        handler._gateway_json = True  # type: ignore[attr-defined]
        gw.addHandler(handler)
        gw.propagate = False
    else:
        gw.propagate = True


def configure_otel_logs(
    resource=None, logger_provider: LoggerProvider | None = None
) -> LoggerProvider | None:
    """Start an OTLP logs pipeline; returns the provider (None if not started).

    Only runs when an OTLP endpoint is configured (``OTEL_EXPORTER_OTLP_ENDPOINT``
    or ``OTEL_EXPORTER_OTLP_LOGS_ENDPOINT``) — without one there is no sink for
    OTel log records and JSON stdout logging is the visible artifact instead.
    The returned provider is also returned *without* an attached handler when
    ``logger_provider`` is passed (tests inject their own).
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    if not endpoint:
        return logger_provider

    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

    provider = logger_provider or LoggerProvider(resource=resource or build_resource())
    provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    return provider


def attach_otel_log_handler(logger_provider: LoggerProvider) -> OTelLogHandler:
    """Attach an ``OTelLogHandler`` to the gateway logger tree.

    Records from every ``gateway.*`` logger are exported to the OTel logs
    pipeline with their trace context.
    """
    handler = OTelLogHandler(logger_provider)
    gw = logging.getLogger("gateway")
    gw.addHandler(handler)
    return handler
