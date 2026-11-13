"""Prometheus metrics for gateway observability (exposed at /metrics)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "gateway_requests_total",
    "Total chat completion requests.",
    ["provider", "model", "status"],
)

LATENCY = Histogram(
    "gateway_request_latency_seconds",
    "End-to-end request latency.",
    ["provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)

TOKENS = Counter(
    "gateway_tokens_total",
    "Tokens processed.",
    ["type"],  # prompt | completion
)

COST = Counter(
    "gateway_cost_usd_total",
    "Estimated upstream cost in USD.",
    ["model"],
)

GUARDRAIL_BLOCKS = Counter(
    "gateway_guardrail_blocks_total",
    "Requests/chunks blocked by guardrails.",
    ["guardrail", "phase"],
)

ALERTS = Counter(
    "gateway_alerts_total",
    "Alerts fired (budget thresholds, ...). outcome: sent | failed | "
    "skipped (no webhook configured).",
    ["kind", "outcome"],
)

GUARDRAIL_OBSERVED = Counter(
    "gateway_guardrail_observed_total",
    "Verdicts in observe mode that would have blocked/modified "
    "(no enforcement; for measuring before rollout).",
    ["guardrail", "phase", "action"],
)

CACHE = Counter(
    "gateway_cache_hits_total",
    "Response cache lookups.",
    ["result"],  # hit | semantic | miss
)

BATCH_LINES = Counter(
    "gateway_batch_lines_total",
    "Batch input lines executed by the background worker.",
    ["status"],  # completed | failed | deferred (rate limit / budget backoff)
)

TTFT = Histogram(
    "gateway_stream_ttft_seconds",
    "Time from request start to the first streamed chunk delivered to the client.",
    ["model"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

INFLIGHT = Gauge(
    "gateway_requests_inflight",
    "Chat completion requests currently being processed "
    "(streams count until the response body is fully sent).",
)

CIRCUIT_STATE = Gauge(
    "gateway_circuit_state",
    "Circuit breaker state per deployment (0=closed, 1=half_open, 2=open).",
    ["deployment", "provider"],
)

CIRCUIT_TRANSITIONS = Counter(
    "gateway_circuit_transitions_total",
    "Circuit breaker state transitions (trip / recover / reopen).",
    ["deployment", "provider", "state"],
)

LOGS_PRUNED = Counter(
    "gateway_logs_pruned_total",
    "Request log rows deleted by the retention task (logging.retention_days).",
)

LOGS_DROPPED = Counter(
    "gateway_logs_dropped_total",
    "Request log rows dropped because the background write queue "
    "(logging.background_write) was full.",
)

AUTH = Counter(
    "gateway_auth_total",
    "Authentication attempts per chain step (authenticated | rejected).",
    ["authenticator", "result"],
)

AUDIT = Counter(
    "gateway_audit_events_total",
    "Security-relevant admin actions emitted to the audit webhook "
    "(sent | failed | skipped).",
    ["event", "outcome"],
)

SHADOW_REQUESTS = Counter(
    "gateway_shadow_requests_total",
    "Requests mirrored to shadow deployments (never affect the client).",
    ["deployment", "outcome"],  # success | error
)

SHADOW_LATENCY = Histogram(
    "gateway_shadow_latency_seconds",
    "Latency of mirrored (shadow) upstream calls.",
    ["deployment"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)

# Per-key series (metrics.per_key — opt-in: key names are unbounded label
# cardinality). Shadow mirrors are excluded, matching /admin/stats.
KEY_REQUESTS = Counter(
    "gateway_key_requests_total",
    "Requests per virtual key (metrics.per_key). Shadow mirrors excluded.",
    ["key", "status"],
)
KEY_TOKENS = Counter(
    "gateway_key_tokens_total",
    "Tokens per virtual key (metrics.per_key). Shadow mirrors excluded.",
    ["key", "type"],
)
KEY_COST = Counter(
    "gateway_key_cost_usd_total",
    "Upstream cost per virtual key (metrics.per_key). Shadow mirrors excluded.",
    ["key"],
)
KEY_OVERFLOW = Counter(
    "gateway_key_label_overflow_total",
    "Per-key metric labels collapsed into __overflow__ after "
    "metrics.per_key_max_series distinct keys.",
)

# Prompt A/B mirroring (prompt ops): a fraction of the requests served
# with a library prompt's active version is mirrored to its candidate
# version in the background — the candidate never serves clients.
PROMPT_MIRROR_REQUESTS = Counter(
    "gateway_prompt_mirror_requests_total",
    "Requests mirrored to a library prompt's candidate version for A/B "
    "comparison (never client-facing).",
    ["prompt", "outcome"],
)
PROMPT_MIRROR_LATENCY = Histogram(
    "gateway_prompt_mirror_latency_seconds",
    "Latency of mirrored prompt A/B calls per library prompt.",
    ["prompt"],
)


class KeyLabeler:
    """Maps key names to stable per-key metric labels under a cardinality cap.

    The first ``max_series`` distinct key names map to themselves; anything
    after that collapses into ``__overflow__`` (and is counted), so a
    long-lived gateway with churning keys cannot grow the Prometheus
    series forever. Deliberately not thread-safe: the pipeline emits
    metrics from the event loop.
    """

    def __init__(self, max_series: int = 256) -> None:
        self._max = max(1, max_series)
        self._seen: set[str] = set()

    def label(self, key_name: str | None) -> str:
        name = key_name or "unknown"
        if name in self._seen:
            return name
        if len(self._seen) >= self._max:
            KEY_OVERFLOW.inc()
            return "__overflow__"
        self._seen.add(name)
        return name
