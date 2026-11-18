"""Configuration loading.

Two layers:

* ``Settings`` — env-driven runtime settings (secrets, DB URL, config path).
* ``GatewayConfig`` — the YAML file describing providers, models,
  guardrails, cache, routing, logging and pricing.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# --------------------------------------------------------------------------
# Env settings
# --------------------------------------------------------------------------


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    admin_key: str | None = None
    # SQLite by default; PostgreSQL works too (the postgres extra —
    # asyncpg — plus a postgresql+asyncpg:// URL). JSON columns map to
    # JSONB there; pooled connections are pre-pinged so server-side
    # drops recycle instead of surfacing as 500s.
    database_url: str = "sqlite+aiosqlite:///./gateway.db"
    config_path: str = "./config.yaml"
    # Where POST /admin/backup writes SQLite snapshots (VACUUM INTO).
    # Must be writable by the gateway process; created on first backup.
    # SQLite-only: on PostgreSQL the endpoint refuses with a 400
    # pointing at pg_dump (a half-copy would be a false sense of
    # safety).
    backup_dir: str = "./backups"
    log_level: str = "INFO"
    # "text" (default) or "json" — single-line JSON records on stdout
    log_format: str = "text"
    # OpenTelemetry tracing is opt-in: when false, spans are no-ops.
    otel_enabled: bool = False
    # CORS, opt-in for browser-based clients: comma-separated origin list,
    # e.g. GATEWAY_CORS_ALLOW_ORIGINS="https://app.example.com,http://localhost:5173".
    # Empty/None (the default) keeps CORS off entirely.
    cors_allow_origins: str | None = None
    # HTTPS, served by `python -m gateway` / scripts/gateway.sh (plain
    # uvicorn ignores these — pass --ssl-certfile/--ssl-keyfile there
    # instead). PEM certificate chain + matching private key; both must
    # be set together. Any pair works: private/organizational CA-issued,
    # Let's Encrypt (point at /etc/letsencrypt/live/<domain>/fullchain.pem
    # + privkey.pem, or use the --letsencrypt flag), or a self-signed
    # dev pair (--self-signed generates one). Restart after renewing.
    ssl_cert: str | None = None  # GATEWAY_SSL_CERT
    ssl_key: str | None = None  # GATEWAY_SSL_KEY
    # Bind address for `python -m gateway` (plain uvicorn takes
    # --host/--port on its own command line).
    host: str = "127.0.0.1"  # GATEWAY_HOST
    port: int = 8000  # GATEWAY_PORT


@lru_cache
def get_settings() -> Settings:
    # Provider credentials (each provider's api_key_env, e.g.
    # OPENAI_API_KEY) and the OTEL_* endpoints are read from
    # os.environ, which pydantic-settings does not populate from the
    # dotenv file — make the file visible to them first (real
    # environment variables still win).
    load_dotenv_exports(Settings.model_config.get("env_file") or ".env")
    return Settings()


def load_dotenv_exports(env_file: str | Path = ".env") -> None:
    """Export dotenv values into the process environment.

    pydantic-settings reads ``.env`` into the ``Settings`` model only —
    the ``GATEWAY_*`` fields. Two other groups are resolved straight
    from ``os.environ`` at runtime: provider credentials (each
    provider's ``api_key_env``, e.g. ``OPENAI_API_KEY`` — see
    ``ProviderConfig.resolve_key``) and the ``OTEL_*`` endpoints the
    observability stack reads itself. Without this pass neither group
    ever sees the file: the documented quick start (copy
    ``.env.example``, fill in the keys, run) would silently send empty
    provider credentials.

    Real environment variables win (``setdefault`` semantics, the
    dotenv convention), so a container or compose file that injects
    them directly is never shadowed. Empty values are treated as
    unset — the placeholder lines in ``.env.example`` must not shadow
    anything either. A missing or unreadable file is a no-op.
    """
    try:
        content = Path(env_file).read_text(encoding="utf-8")
    except OSError:
        return
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # One pair of matching surrounding quotes, either style.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and value and key not in os.environ:
            os.environ[key] = value


# --------------------------------------------------------------------------
# YAML config
# --------------------------------------------------------------------------


class ProviderConfig(BaseModel):
    api_base: str
    api_key_env: str | None = None
    api_key: str | None = None  # literal key (for tests; prefer env)
    # Provider API version (Azure OpenAI: the ?api-version= query param).
    # Adapters that don't version ignore it.
    api_version: str | None = None

    def resolve_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return None


# Data-residency vocabulary (tier 28): tags are lowercase slugs the
# operator asserts on deployments and requires on keys/tenants — no
# enum on purpose (the vocabulary is deployment-specific), but the
# format is strict so both sides match exactly. Shared by
# DeploymentConfig (config.yaml) and the key/tenant schemas (admin API).
_RESIDENCY_TAG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def normalize_residency_tag(tag: str) -> str:
    """Strip + lowercase one residency tag; ValueError on anything that
    is not a plain slug. Tags are matched EXACTLY, so the format must
    be unambiguous ('EU' and 'eu' are the same tag)."""
    normalized = tag.strip().lower()
    if not _RESIDENCY_TAG.match(normalized):
        raise ValueError(
            f"invalid residency tag {tag!r} — use lowercase slugs "
            "like 'eu' or 'eu-west'"
        )
    return normalized


def normalize_residencies(tags: list[str] | None) -> list[str] | None:
    """Normalize a residency requirement list: strip + lowercase each
    tag, drop duplicates, drop empties. None or an empty list means
    "no requirement" and returns None (PATCH semantics: an explicit
    [] clears the requirement, exactly like null)."""
    if not tags:
        return None
    seen: list[str] = []
    for tag in tags:
        normalized = normalize_residency_tag(tag)
        if normalized not in seen:
            seen.append(normalized)
    return seen or None


class DeploymentConfig(BaseModel):
    name: str
    provider: str
    model: str  # provider-native model id
    # Data-residency claim (tier 28): where this deployment processes
    # data, as a tag the operator asserts (e.g. "eu", "us", "eu-west").
    # A caller with a residency requirement may only be served by (and
    # mirrored to) deployments whose tag is in their effective set;
    # None = NO CLAIM — which satisfies no requirement (fail closed:
    # an untagged deployment can never prove compliance). Tags are
    # lowercased slugs ("eu", "eu-west"); requirements match exactly.
    residency: str | None = None
    priority: int = 100  # lower = tried first
    weight: int = 1  # weighted pick among same-priority deployments
    timeout_seconds: float | None = None
    max_retries: int | None = None
    # Shadow deployments never serve client traffic — the router excludes
    # them from selection. Instead a fraction of *successful* requests are
    # mirrored to them in the background (see pipeline._queue_shadow), so
    # their latency / status / content can be compared before promotion.
    # The client is never affected. Canary-style rollouts, by contrast,
    # are what ``weight`` is for: real traffic, shifted gradually within a
    # same-priority tier.
    shadow: bool = False
    # Fraction of successful requests to mirror (0.0 = none, 1.0 = all).
    shadow_fraction: float = 1.0
    # Provider-native Responses API southbound: requests that arrived via
    # POST /v1/responses are forwarded to the provider's /responses
    # endpoint in their original shape (OpenAI and OpenAI-compatible
    # services) instead of being converted to chat-completions —
    # Responses-specific fields survive that the chat conversion drops.
    # Policy still runs on the internal representation; when a guardrail
    # or prompt policy modified the request, the adapter falls back to
    # the chat conversion so modifications are never silently lost.
    # Requests that did not arrive in Responses shape ignore the flag.
    native_responses: bool = False

    @field_validator("shadow_fraction")
    @classmethod
    def _clamp_shadow_fraction(cls, value: float) -> float:
        return min(1.0, max(0.0, value))

    @field_validator("residency")
    @classmethod
    def _normalize_residency(cls, value: str | None) -> str | None:
        # An empty string is "no claim", same as null.
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        return normalize_residency_tag(value)


class ModelConfig(BaseModel):
    name: str  # public name clients use
    deployments: list[DeploymentConfig]
    # Advertised context window (total tokens: input + output) for the
    # public model — the number the context_window guardrail enforces
    # pre-flight (blocked/redirected *before* upstream quota is spent).
    # None = unmanaged. The window is a property of the model, declared
    # once here rather than in guardrail params, so other surfaces can
    # reuse it. Heterogeneous deployments: set the window of the one
    # that matters (e.g. the smallest fallback a request must fit).
    context_window: int | None = None


class GuardrailScope(BaseModel):
    """Where a guardrail applies. Omitted fields mean "everywhere".

    Patterns are fnmatch globs, matched against the calling key's *name*
    (as shown in /admin/keys — the admin key is named "admin"), the
    *public* model name, and the inbound *route* ("chat" =
    /v1/chat/completions, "responses" = /v1/responses, "messages" =
    the native Anthropic format, "gemini" = the native Gemini format).
    ``exclude_*`` wins over the matching include list; a request must
    satisfy every present field to run the guardrail.

    A guardrail out of scope for a request is skipped entirely — no
    verdict, no log entry, no metric — as if it were not configured.
    """

    keys: list[str] | None = None  # only run for these key names
    exclude_keys: list[str] | None = None  # never run for these key names
    models: list[str] | None = None  # only run for these public models
    exclude_models: list[str] | None = None  # never run for these public models
    routes: list[str] | None = None  # only run on these inbound routes
    exclude_routes: list[str] | None = None  # never run on these inbound routes


class GuardrailConfig(BaseModel):
    name: str
    enabled: bool = True
    class_path: str | None = None  # "pkg.module:ClassName" for custom guardrails
    params: dict[str, Any] = Field(default_factory=dict)
    # enforce: block/modify verdicts take effect. observe: verdicts are
    # logged and counted (gateway_guardrail_observed_total) but the request
    # flows on untouched — for measuring false-positive rates before rollout.
    mode: Literal["enforce", "observe"] = "enforce"
    # Optional scoping: run this guardrail only for certain keys / models
    # (see GuardrailScope). Per-key *runtime* overrides live on the virtual
    # key instead (VirtualKey.disabled_guardrails), so they can change
    # without a restart; config scope and key overrides compose (a key's
    # disabled list always wins).
    scope: GuardrailScope | None = None


class CacheConfig(BaseModel):
    enabled: bool = True
    ttl_seconds: float = 300
    max_entries: int = 256
    backend: str = "memory"  # memory | redis


class SemanticCacheConfig(BaseModel):
    """Approximate-match response cache (semantic caching).

    Requests that *exactly* match a cached one are served by the exact
    cache first. When that misses, this layer embeds the normalized
    request (via the configured embeddings model, routed like any other
    embeddings call) and serves the best cached response whose embedding
    has at least ``threshold`` cosine similarity — so near-duplicate
    prompts ("summarise this" vs "please summarize this") share answers.

    Scope matches the exact cache: entries are global (not per key), and
    only same-model requests can match. The similarity threshold trades
    false-positive hits (wrong-but-similar answer served, cheaper)
    against false negatives (cache never hits). Off by default.
    """

    enabled: bool = False
    model: str = "text-embedding-3-small"  # embeddings model for similarity
    threshold: float = 0.95  # cosine similarity required for a hit
    ttl_seconds: float = 300
    max_entries: int = 256


class RateLimitConfig(BaseModel):
    backend: str = "memory"  # memory | redis


class BreakerConfig(BaseModel):
    """Per-deployment circuit breaker (health-aware routing).

    A deployment whose circuit is open is skipped by the router until the
    cooldown elapses, then probed with one request (half-open): success
    closes the circuit, failure re-opens it. If every deployment of a
    model is open, the router routes anyway (availability beats
    protection). Failures counted are request-level: a request that
    exhausts its retries on the deployment counts once. Client-caused
    errors never trip anything.

    ``backend`` selects where state lives: ``memory`` (per process — each
    worker/replica trips and recovers on its own evidence) or ``redis``
    (shared: one replica's trip opens the circuit for the whole fleet —
    see ``RedisCircuitBreakerRegistry``).
    """

    backend: str = "memory"  # memory | redis
    enabled: bool = True
    failure_threshold: int = 3  # consecutive request-level failures -> open
    cooldown_seconds: float = 30.0


class RoutingConfig(BaseModel):
    timeout_seconds: float = 300
    max_retries: int = 2
    retry_backoff_base: float = 0.5
    # Cost-aware routing: within a priority tier, prefer the deployment
    # whose upstream model has the cheapest INPUT price (per the pricing
    # table — the same lookup billing uses, on the deployment's model).
    # The price ladder is also the failover ladder: an exhausted cheap
    # deployment falls through to the next cheapest. weight only breaks
    # ties between equally-priced (or equally unpriced) deployments;
    # unpriced deployments sort last — unknown cost gets no discount.
    # Priority still rules across tiers: cost never outranks a
    # deliberately lower-priority deployment.
    cost_aware: bool = False
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)


class LoggingConfig(BaseModel):
    max_body_chars: int = 20000
    # PII-safe mode: when false, request/response bodies are not persisted
    # to request logs (columns stay NULL) — for deployments where raw
    # prompts must not be stored. Metadata (tokens, cost, latency, status,
    # guardrail verdicts) still is. NB: guardrail_actions.details and
    # error_message may still contain fragments of traffic.
    store_bodies: bool = True
    # Retention: request log rows older than this many days are deleted by
    # a background task (at startup, then every retention_interval_seconds).
    # None (default) keeps rows forever.
    retention_days: float | None = None
    retention_interval_seconds: float = 3600.0
    # Background queued log writes (opt-in hot-path optimization): instead
    # of awaiting the row INSERT in the response path — the last DB round
    # trip there — request-log writes are handed to a bounded queue drained
    # by a background consumer. Semantics change: a returned response no
    # longer implies its log row is queryable (typically milliseconds;
    # admin UI auto-refresh and exports cover the gap). A full queue drops
    # rows (counted on gateway_logs_dropped_total) rather than blocking,
    # and shutdown drains what is pending.
    background_write: bool = False
    queue_size: int = 1000
    # Wire capture (request-log rows): which headers to persist, as
    # ALLOWLISTS (allow, never block — a new credential-bearing header
    # can never leak into stored rows by default; Authorization /
    # x-api-key / x-goog-api-key / api-key are deliberately absent).
    # Entries are lowercase header names; a trailing "*" matches prefixes
    # (e.g. "x-ratelimit-*"). None = the built-in defaults. The client
    # list is read per request (reload applies live); the upstream list
    # is bound into the provider HTTP client's capture hooks (restart to
    # change — same rule as rotating provider credentials).
    client_header_allowlist: list[str] | None = None
    upstream_header_allowlist: list[str] | None = None


class MetricsConfig(BaseModel):
    """Prometheus behaviour. ``per_key`` (opt-in — key names are unbounded
    label cardinality) adds per-key series: ``gateway_key_requests_total``,
    ``gateway_key_tokens_total`` and ``gateway_key_cost_usd_total``.
    Distinct keys beyond ``per_key_max_series`` collapse into the
    ``__overflow__`` label (counted on ``gateway_key_label_overflow_total``)
    instead of growing the series forever."""

    per_key: bool = False
    per_key_max_series: int = 256


class AlertsConfig(BaseModel):
    """Spend alerting: fire a webhook when a virtual key's cumulative
    spend crosses a percentage of its ``max_budget_usd``.

    Each level fires once per key (state persists on the key row, so it
    survives restarts and applies across workers). Raising a key's budget
    re-arms thresholds: levels already crossed are marked fired, so only
    future crossings alert. Without a ``webhook_url`` (or ``url_env``)
    alerting is a no-op.
    """

    enabled: bool = True
    webhook_url: str | None = None
    url_env: str | None = None  # env var that overrides webhook_url when set
    budget_thresholds: list[int] = Field(default_factory=lambda: [50, 80, 90, 100])
    timeout_seconds: float = 5.0
    max_attempts: int = 2  # delivery attempts (1 retry)

    def resolve_url(self) -> str | None:
        if self.url_env and os.environ.get(self.url_env):
            return os.environ[self.url_env]
        return self.webhook_url


class AuditConfig(BaseModel):
    """Audit-log webhooks: POST a JSON record of security-relevant admin
    actions — virtual key create/update/revoke and circuit-breaker resets —
    to an external audit trail / SIEM.

    Delivery is best-effort and never blocks the admin operation (records
    are delivered by a background task). Without a ``webhook_url`` (or
    ``url_env``) auditing is a no-op, counted as
    ``gateway_audit_events_total{outcome="skipped"}``.
    """

    enabled: bool = True
    webhook_url: str | None = None
    url_env: str | None = None  # env var that overrides webhook_url when set
    timeout_seconds: float = 5.0
    max_attempts: int = 2  # delivery attempts (1 retry)

    def resolve_url(self) -> str | None:
        if self.url_env and os.environ.get(self.url_env):
            return os.environ[self.url_env]
        return self.webhook_url


class PromptsConfig(BaseModel):
    """Gateway-managed prompt policy (applied before guardrails/cache).

    ``system_prompt`` is prepended to requests that carry no system
    message of their own (a per-key ``system_prompt`` overrides it — and
    a client-supplied system message always wins over both: the gateway
    manages the prompt for callers that don't provide one).
    ``variables`` resolve ``{{name}}`` placeholders in message text and
    in the managed system prompt (a key's ``prompt_variables`` override
    these); unknown placeholders are left untouched.

    ``library_cache_seconds`` bounds how long the pipeline's resolution
    of a key's ``system_prompt_ref`` (named library prompt) is cached:
    writes through this process's admin API invalidate immediately, the
    TTL is the cross-replica backstop.
    """

    system_prompt: str | None = None
    variables: dict[str, str] = Field(default_factory=dict)
    library_cache_seconds: float = 30.0


class UploadsConfig(BaseModel):
    """Multipart upload policy for the file-upload surfaces
    (/v1/audio/transcriptions|translations, /v1/images/edits|variations).

    ``max_upload_mb`` caps the uploaded payload (413 beyond it) — the
    gateway buffers uploads to route/retry them, so a bound protects
    memory. 25 MB matches OpenAI's own limits.
    """

    max_upload_mb: int = 25


class EventHandlerConfig(BaseModel):
    """One event handler registration.

    ``class_path`` uses the guardrails convention: ``pkg.module:ClassName``
    (the class subclasses ``gateway.core.events.EventHandler``). ``kind``:
    ``observer`` (default — background delivery, cannot affect the
    request) or ``interceptor`` (in-band, may abort — restricted to
    explicitly-listed interceptable events, no patterns). ``events``:
    names or shell patterns (``"request.*"``); None = all events.
    """

    class_path: str
    kind: Literal["observer", "interceptor"] = "observer"
    events: list[str] | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class EventsConfig(BaseModel):
    """Lifecycle events: named events at the request-stage boundaries
    (see ``core/events.py`` for the catalog), delivered to configured
    handlers.

    Two kinds: ``observer`` (fire-and-forget, background queue — the
    default) and ``interceptor`` (awaited in-band, may abort the
    request by raising ``GatewayError``; restricted to events emitted
    before the request becomes irreversible). With no handlers, the
    bus is inert — emission is a no-op lookup.
    """

    enabled: bool = True
    # Bound on queued observer deliveries (full queue drops, counted).
    queue_size: int = 10_000
    handlers: list[EventHandlerConfig] = Field(default_factory=list)


class BatchConfig(BaseModel):
    """OpenAI-style Batch API: ``/v1/files`` upload + ``/v1/batches``
    execution by a background worker.

    ``enabled: false`` turns the surfaces off (404) and stops the
    worker — e.g. to force callers onto interactive traffic only.
    ``max_file_bytes``/``max_requests`` bound the buffered input
    (OpenAI's own limits are 20 MB / 50k; requests default lower).
    ``wave_size`` lines of the oldest runnable batch execute per
    worker tick, ``poll_interval_seconds`` apart — the burst a batch
    can send upstream is wave-shaped, not a wall of 10k requests.
    """

    enabled: bool = True
    max_file_bytes: int = 20 * 1024 * 1024  # 20 MB
    max_requests: int = 10_000  # input lines per batch
    wave_size: int = 16
    poll_interval_seconds: float = 2.0


class CanaryConfig(BaseModel):
    """Synthetic canary probes: a background loop sends a tiny chat
    request straight to every configured deployment's provider on a
    schedule, so upstream health is *measured* rather than inferred
    from traffic (a dead fallback that receives no traffic otherwise
    looks healthy until the moment it's needed; a shadow deployment
    only ever sees mirrored traffic).

    Probes are direct adapter calls — no pipeline, no guardrails, no
    cache, no request-log rows, no spend accounting, no
    circuit-breaker accounting — so they can neither affect nor be
    mistaken for client traffic, and cost ``max_tokens`` of provider
    quota per deployment per wave. Disabled by default (probes spend
    real quota); ``POST /admin/canaries/probe`` runs a wave on demand
    regardless.

    ``deployments`` is an optional fnmatch allowlist of deployment
    names ("openai/*"); None = every deployment under every model,
    shadows included. Hot-reloadable: enabled / interval / timeout /
    allowlist apply from the next loop iteration.
    """

    enabled: bool = False
    interval_seconds: float = 300.0
    timeout_seconds: float = 10.0
    max_tokens: int = 1
    prompt: str = "ping"
    deployments: list[str] | None = None  # fnmatch patterns; None = all


class PIIScanConfig(BaseModel):
    """PII/secret scan over *stored* request/response bodies — the
    audit that answers "did secrets or PII land in the request log?".
    Clients paste API keys into prompts; guardrails may be off, in
    observe mode, or only masking the *upstream* copy — the log keeps
    the original as received, so this is where exposure actually
    shows. On-demand (no loop): ``GET /admin/pii-scan`` over recent
    rows, ``GET /admin/logs/{id}/pii-scan`` for one.

    Detectors mirror the ``pii_redaction`` guardrail (the same
    compiled patterns — "masked" and "found" never drift) plus
    secret shapes a redactor wouldn't mask (JWTs, private-key
    blocks). ``extra_patterns`` adds name → regex detectors;
    ``exclude_kinds`` disables built-ins (e.g. leave noisy ``ipv4``
    out). Findings never include the matched text — only kind +
    count — so the audit never becomes its own leak.
    """

    enabled: bool = True
    max_rows: int = 500  # bulk scan bound
    extra_patterns: dict[str, str] = Field(default_factory=dict)  # name -> regex
    exclude_kinds: list[str] = Field(default_factory=list)  # built-ins to drop

    @field_validator("extra_patterns")
    @classmethod
    def _patterns_compile(cls, value: dict[str, str]) -> dict[str, str]:
        for name, pattern in value.items():
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"pii_scan.extra_patterns[{name!r}]: invalid regex: {exc}"
                ) from exc
        return value


class IdempotencyConfig(BaseModel):
    """Idempotency-Key replay for the chat-family surfaces (OpenAI
    chat, the Responses bridge, native Anthropic/Gemini inbound —
    everything that routes through the completions machinery).

    A client supplies an ``Idempotency-Key`` header; the *successful*
    response is stored under (caller, key) and replays on retry — no
    double generation, no double billing (the stored usage was
    billed at first execution; replay rows log as cache hits with
    zero cost). Only successes are stored: errors and guardrail
    blocks re-execute on retry, which is what a transient failure
    needs. Reuse of a key with different request parameters is
    rejected (400 ``idempotency_key_reuse``) — the fingerprint is
    the original request as received, the same identity the response
    cache uses.

    In-flight duplicates (the canonical case: retrying a timed-out
    request while the original still runs) wait up to
    ``inflight_wait_seconds`` for the original, then replay it —
    or execute themselves if it never finishes. In-process wait;
    cross-replica dedup covers only completed entries (Redis
    backend). Replays answer ``X-Gateway-Cache: IDEMPOTENT``.

    No header = zero behavior change; ``enabled: false`` = the
    header is ignored entirely. Same backend selection as the
    response cache (memory LRU / Redis) with its own TTL and size.
    """

    enabled: bool = True
    ttl_seconds: float = 86400.0  # 24h, Stripe's idempotency window
    max_entries: int = 10_000
    inflight_wait_seconds: float = 30.0
    backend: str = "memory"  # memory | redis


class ReplayConfig(BaseModel):
    """Capture-to-replay harness: a config-regression safety net.

    ``GET /admin/logs/export`` writes the replayable request log
    (rows with stored bodies) as JSONL — the *capture*. ``POST
    /admin/replay`` re-executes captured requests through the
    request-policy phases *only* — model resolution, prompt policy,
    permissions, guardrails, routing selection — with no upstream
    calls, no rate-limit/budget/idempotency counters, no cache, no
    spend, no request-log rows and no lifecycle events, as the
    recorded caller (the key row is re-loaded fresh, so a key edited
    or revoked since capture replays under its *current* policy).
    Each line is diffed against what the capture recorded (model,
    status, guardrail verdicts, deployment) — the diff is what a
    candidate config would change about real traffic, *before* it
    is hot-reloaded onto it.

    Rows recorded as ``error`` are exported but marked not
    comparable (the replay cannot re-run transient upstream or
    counter outcomes); their policy outcome is simply reported.
    """

    enabled: bool = True
    # Upper bound on capture lines accepted per replay call (and
    # rows per export page): the harness is for sampling real
    # traffic, not re-executing history.
    max_entries: int = 500


class RunawayConfig(BaseModel):
    """Runaway-agent protection: loop detection with an escalating
    cooldown jail.

    An agent stuck in a loop — a retry storm, a tool-call loop that
    regenerates forever, a temperature-zero do/while — sends the SAME
    request over and over at a rate no legitimate caller produces.
    RPM/TPM ceilings cannot see it (each request is individually
    legal); what identifies the runaway is the *fingerprint* — the
    same normalized request identity the response cache keys on —
    repeated ``identical_request_threshold`` times inside
    ``identical_request_window_seconds``.

    On trip the *key* enters a cooldown jail: every request from it
    answers 429 (``rate_limit_exceeded`` + ``Retry-After``) until the
    cooldown expires — including requests that would otherwise hit
    the response cache or replay an idempotency entry, because
    nothing a stuck agent sends is useful. Re-trips escalate
    (``cooldown_multiplier`` per offense, capped at
    ``cooldown_max_seconds``); a quiet ``reset_after_seconds`` decays
    the offense count back to zero. Detection state is in-process
    (like the idempotency in-flight map): a multi-replica fleet
    detects at per-replica rate — bounded, just slower to trip.
    """

    enabled: bool = True
    # Identical fingerprints (>= threshold) within the window = a
    # stuck loop. The tripping request itself is refused. Deliberately
    # above any sane retry/backoff pattern (<=5/min) — raise it if you
    # run legitimate identical-request fan-out (benchmarks) through
    # the gateway.
    identical_request_threshold: int = 10
    identical_request_window_seconds: float = 60.0
    # Cooldown jail: first offense lasts cooldown_seconds, each
    # repeat multiplies (capped). While jailed, ALL the key's requests
    # 429 with Retry-After.
    cooldown_seconds: float = 30.0
    cooldown_multiplier: float = 2.0
    cooldown_max_seconds: float = 3600.0
    # Quiet period (no trips) after which the offense count resets.
    reset_after_seconds: float = 600.0


class ConcurrencyConfig(BaseModel):
    """Concurrent-request admission control with priority queueing.

    RPM/TPM bound the *rate* of requests; this bounds how many may be
    in-flight against the upstream at once — the number that protects
    a provider quota or a small deployment fleet. Caps live on the
    rows, snapshotted at auth time like every other ceiling:
    ``VirtualKey.max_concurrency`` and the pooled
    ``Tenant.max_concurrency`` (a member key must pass both). A caller
    with neither cap skips admission entirely.

    The slot wraps the upstream call: acquired after the cache miss
    (hits and idempotent replays never hold one), released when the
    response completes — including a stream's full body. When every
    slot is taken, requests QUEUE for up to ``wait_seconds``, admitted
    highest-``priority``-first (``VirtualKey.priority``, FIFO within a
    priority); an expired wait answers 429 + Retry-After.

    State is in-process (like the idempotency in-flight map and
    runaway detection): a multi-replica fleet enforces its
    per-replica share.
    """

    enabled: bool = True
    # How long a request queues for a slot before failing with 429
    # (+ Retry-After). 0 = fail fast when all slots are taken.
    wait_seconds: float = 10.0


class RedisConfig(BaseModel):
    """Connection for the Redis-backed cache / rate limiter.

    ``url_env`` mirrors the providers' ``api_key_env`` pattern: name an
    environment variable (e.g. ``REDIS_URL``) whose value overrides
    ``url`` when set, keeping credentials out of the YAML.
    """

    url: str = "redis://localhost:6379/0"
    url_env: str | None = None
    prefix: str = "llm-gateway"

    def resolve_url(self) -> str:
        if self.url_env and os.environ.get(self.url_env):
            return os.environ[self.url_env]
        return self.url


class JWTAuthConfig(BaseModel):
    """JWT/OAuth bearer-token verification (``auth.chain`` step ``jwt``).

    A valid token authenticates the caller as the virtual key named by its
    ``key_claim`` (default ``sub``) — the key row stays the single source
    of policy. Exactly one verification source must be configured: a
    shared ``secret`` (HS256) or a ``jwks_url`` (asymmetric keys, e.g. an
    OIDC provider's published JWKS — cached, refreshed on unknown ``kid``).
    Requires PyJWT (the ``auth`` extra).
    """

    secret: str | None = None  # HS256 shared secret
    jwks_url: str | None = None  # e.g. https://idp/.well-known/jwks.json
    # Enforce these claims when set (exp is always required).
    issuer: str | None = None
    audience: str | None = None
    # Claim carrying the virtual key name.
    key_claim: str = "sub"
    # Accepted signature algorithms (default: HS256 with a secret, RS256
    # with a JWKS; "none" is never accepted).
    algorithms: list[str] | None = None
    # Clock-skew tolerance for exp / nbf.
    leeway_seconds: float = 30.0
    jwks_cache_seconds: float = 300.0


class AuthConfig(BaseModel):
    """The authentication chain: which strategies may authenticate a
    caller, in order. Built-in steps: ``admin_key``, ``virtual_key``,
    ``jwt``; a ``"pkg.module:ClassName"`` entry loads a custom
    ``Authenticator`` subclass (instantiated with this config). The first
    step that returns a principal wins; a step that positively rejects
    the credential fails the request immediately."""

    chain: list[str] = Field(
        default_factory=lambda: ["admin_key", "virtual_key"]
    )
    jwt: JWTAuthConfig = Field(default_factory=JWTAuthConfig)


class GatewayConfig(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: list[ModelConfig] = Field(default_factory=list)
    # Requested name -> public model name. Lets clients keep using hardcoded
    # provider model names (gpt-4o, claude-3-5-sonnet-...) against the
    # gateway without code changes. A requested name that is itself a
    # configured model always wins over its alias; everything downstream
    # (permissions, cache identity, guardrail model scopes, logs, response
    # "model") sees the resolved public name — the client's original name
    # is preserved in the log row's request_body.
    model_aliases: dict[str, str] = Field(default_factory=dict)
    # Catch-all for unknown model names (opt-in; use with care — typos
    # silently route here too). Ignored when the name is a real model or
    # an alias. Unset (default): unknown names 404.
    default_model: str | None = None
    guardrails: list[GuardrailConfig] = Field(default_factory=list)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    semantic_cache: SemanticCacheConfig = Field(default_factory=SemanticCacheConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    uploads: UploadsConfig = Field(default_factory=UploadsConfig)
    batch: BatchConfig = Field(default_factory=BatchConfig)
    canary: CanaryConfig = Field(default_factory=CanaryConfig)
    pii_scan: PIIScanConfig = Field(default_factory=PIIScanConfig)
    idempotency: IdempotencyConfig = Field(default_factory=IdempotencyConfig)
    replay: ReplayConfig = Field(default_factory=ReplayConfig)
    runaway: RunawayConfig = Field(default_factory=RunawayConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    pricing: dict[str, dict[str, float]] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "GatewayConfig":
        path = Path(path)
        if not path.exists():
            return cls()
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls.model_validate(data)

    def public_models(self) -> list[str]:
        """Public model names clients may send — configured models plus
        alias names (aliases shadowed by a real model of the same name are
        omitted; the real model wins)."""
        names = [m.name for m in self.models]
        seen = set(names)
        for alias in self.model_aliases:
            if alias not in seen:
                names.append(alias)
                seen.add(alias)
        return names
