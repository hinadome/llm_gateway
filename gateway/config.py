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
from pydantic import BaseModel, Field, field_validator, model_validator
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
    # BYOK vault master key (tier 31): a Fernet key (urlsafe-base64,
    # 32 bytes — python -c "from cryptography.fernet import Fernet;
    # print(Fernet.generate_key().decode())"). Comma-separated list =
    # rotation support: the FIRST key encrypts new entries, all keys
    # decrypt (MultiFernet). Never in config.yaml — secrets live in
    # env, and this one encrypts other secrets. None = the vault is
    # disabled: provider-key endpoints answer 400, callers route with
    # the platform's configured keys (nothing changes).
    vault_master_key: str | None = None


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
    # Realtime (WebSocket) base URL (tier 32): where this provider's
    # OpenAI-compatible realtime endpoint lives, e.g.
    # "wss://api.openai.com/v1" — the gateway appends "/realtime" and
    # the deployment's native model. A provider without realtime_base
    # is not realtime-capable: /v1/realtime sessions asking for its
    # deployments fail with a clear error (never a silent HTTP fallback).
    # Credentials reuse the same api_key/api_key_env (BYOK applies) in
    # the provider's own header shape (see ws_headers on the adapter).
    realtime_base: str | None = None

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
    caller's *tenant* name (as shown in /admin/tenants — the whole
    tenant binds, no per-key bookkeeping), the *public* model name, and
    the inbound *route* ("chat" = /v1/chat/completions, "responses" =
    /v1/responses, "messages" = the native Anthropic format, "gemini" =
    the native Gemini format). ``exclude_*`` wins over the matching
    include list; a request must satisfy every present field to run the
    guardrail. A request with no tenant (a platform key) is in *no*
    tenant: it matches no ``tenants`` pattern (even ``*``) and is never
    excluded by ``exclude_tenants`` — the same shape as route scoping
    over a route-less call.

    A guardrail out of scope for a request is skipped entirely — no
    verdict, no log entry, no metric — as if it were not configured.
    """

    keys: list[str] | None = None  # only run for these key names
    exclude_keys: list[str] | None = None  # never run for these key names
    # Tenant scoping (the whole tenant binds — every member key,
    # tenant admins included, since the tenant is a policy ceiling).
    tenants: list[str] | None = None  # only run for keys in these tenants
    exclude_tenants: list[str] | None = None  # never run for these tenants
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

    ``backend`` selects where entries live: ``memory`` (per process —
    near-duplicates embedded and stored by one replica are invisible to
    the next) or ``redis`` (shared: one replica's semantic entry is a
    hit for the whole fleet — see ``RedisSemanticCache``).
    """

    enabled: bool = False
    model: str = "text-embedding-3-small"  # embeddings model for similarity
    threshold: float = 0.95  # cosine similarity required for a hit
    ttl_seconds: float = 300
    max_entries: int = 256
    backend: str = "memory"  # memory | redis


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

    The same webhook also carries key-expiry warnings (tier 52): a
    background sweep walks active keys with an ``expires_at`` and fires
    once per day-level as the deadline approaches — ``expiry_days``
    (default 14/7/1/0; 0 = the page that the key just died). Setting or
    moving a deadline arms silently (levels already crossed never page —
    the operator just chose the deadline); a key the sweep *discovers*
    already near its deadline fires honestly. The fired levels persist
    on the key row beside the budget ones.

    And quiet-key alerts (tier 56): the liveness edition — a key that
    WAS alive (it has a last_used_at, tier 55) and has not been used
    for ``quiet_days`` (default 7/30) fires each day-level once: 7 is
    the early page (a cron stopped, an integration is stuck), 30 the
    churn page (rotate or revoke it). Resumed use re-arms silently on
    the next sweep — a key may go quiet (and page) once per quiet
    period, forever. Never-used keys are NOT quiet-key alerts (they
    were never alive — that finding is the ``idle_days`` listing
    filter's); revoked keys never warn. An empty ``quiet_days`` list
    disables the sweep without touching the other alert families.
    """

    enabled: bool = True
    webhook_url: str | None = None
    url_env: str | None = None  # env var that overrides webhook_url when set
    budget_thresholds: list[int] = Field(default_factory=lambda: [50, 80, 90, 100])
    # Day levels for the expiry-warning sweep: a key inside N days of
    # its expires_at fires each level once, highest first; 0 fires when
    # the deadline passes (the key stops authenticating at that moment).
    expiry_days: list[int] = Field(default_factory=lambda: [14, 7, 1, 0])
    expiry_interval_seconds: float = 3600.0  # sweep cadence (hourly)
    # Day levels for the quiet-key sweep (tier 56): a key that was
    # alive (has a last_used_at) and has been idle >= N days fires each
    # level once, lowest first (the 7-day page never arrives after the
    # 30-day one — a quiet period is a story that unfolds in order).
    # Resumed use re-arms on the next sweep. [] disables this sweep
    # alone.
    quiet_days: list[int] = Field(default_factory=lambda: [7, 30])
    quiet_interval_seconds: float = 3600.0  # sweep cadence (hourly)
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


class VaultConfig(BaseModel):
    """BYOK vault knobs. The vault is ENABLED by the master key
    (GATEWAY_VAULT_MASTER_KEY — env, never config.yaml: it encrypts
    secrets, so it is not itself committed); without a master key the
    provider-key endpoints answer 400 and every request routes with
    the platform's configured keys.

    ``ttl_seconds`` is how long a decrypted credential is cached in
    process memory per (tenant, provider): the hot path is a dict
    read, not a DB round trip. Admin writes invalidate the local
    cache immediately (and a key ROTATED OUT of the master-key list
    simply starts failing decryption — fail closed — until entries
    are re-stored).

    ``backend`` selects how that invalidation crosses a
    multi-process fleet: ``memory`` (per process — other replicas
    converge within the TTL, the documented backstop) or ``redis``
    (admin writes also PUBLISH an invalidation on a Redis channel and
    every replica drops its cached entry — and its negative twin —
    the moment the message lands; the TTL remains the backstop if a
    message is lost). Infra-bound (a client + a listener task), so
    changes report restart_required.

    ``strict`` is the platform-economics stance: tenant traffic may
    ONLY ride credentials the tenant stored — a deployment whose
    provider the tenant has not keyed is removed from its routing
    ladder entirely (retries and fallbacks stay within the tenant's
    keyed providers), and a request whose every deployment is unkeyed
    fails 403 byok_key_required naming the providers to key. Platform
    callers (no tenant) and the platform's own machinery (shadow
    mirrors, canary probes) are untouched. Fail-closed like the rest
    of the vault: strict with no master key configured means tenant
    traffic CANNOT run — set the master key before turning it on.
    """

    backend: str = "memory"  # memory | redis (cache invalidation fan-out)
    strict: bool = False
    ttl_seconds: int = 30


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


class SLOTargetConfig(BaseModel):
    """One named service-level objective: a bar (``target``) on one
    ``metric`` over the client traffic of a ``model`` (optionally
    narrowed to one ``deployment``), measured across a rolling
    ``window_minutes`` and gated by ``min_requests`` — a verdict on
    thin volume is ``insufficient_evidence`` regardless of the
    numbers, because a quiet five-minute window proves nothing about
    the service.

    Metrics are all lower-is-better (the bar is a ceiling):

    * ``error_rate`` — failed + blocked over total, as a fraction
      (``0.01`` = 1%).
    * ``latency_p50_ms`` / ``latency_p95_ms`` / ``latency_p99_ms`` —
      end-to-end latency percentiles, in milliseconds.
    * ``ttft_p95_ms`` — time-to-first-token p95 over the requests
      that streamed (non-streamed rows have nothing to measure;
      a window where nothing streamed is insufficient, honestly).

    What counts is CLIENT EXPERIENCE, not deployment health: every
    non-shadow, non-trial row for the scope — cache replays included
    (a cached answer is a fast answer the client really received)
    and, for a model-scoped target, unattributed error rows too (the
    client experienced the failure even when no deployment completed
    the dispatch to blame). A deployment-scoped target measures only
    the rows attributed to that deployment — the same honesty the
    post-promotion watch uses.

    ``route`` (tier 66) narrows the scope to one inbound surface
    (``chat`` | ``responses`` | ``messages`` | ``gemini`` |
    ``realtime`` | ``mcp``) — None measures every row for the model.
    The MCP use: a target ``model: "jira", route: "mcp"`` measures the
    tool server's rows (whose model column carries the SERVER name),
    so error-rate and latency promises cover tool calls without
    sweeping chat rows that happen to share the name — and a
    no-``route`` target over the same name sees everything, the
    client's whole experience of that name. TTFT over an MCP-scoped
    target is honestly unmeasurable (MCP answers are complete
    JSON-RPC frames, nothing streams), which the verdict says as
    ``insufficient_evidence`` — never a fabricated number.
    """

    name: str
    model: str  # public model name, exact
    deployment: str | None = None  # narrow to one deployment's attributed rows
    route: str | None = None  # narrow to one inbound surface (see above)
    metric: Literal[
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "ttft_p95_ms",
    ]
    target: float  # the ceiling (fraction for error_rate, ms otherwise)
    window_minutes: int = 60
    min_requests: int = 20  # sufficiency gate — thin volume is not a verdict


class SLOBurnRule(BaseModel):
    """One burn-rate rule (tier 43): the Google-SRE multiwindow
    alerting recipe, verdicted on demand beside the bar verdict.

    A burn rate is the *pace* a target consumes its error budget —
    observed bad-event fraction over the bad-event allowance
    (``1% errors`` allows 1%; ``p95 under 800ms`` implies 5% of
    requests may exceed the bar). Burn 1.0 = spending the budget
    exactly as fast as the promise allows; burn 14.4 = 14.4x. Rate
    thresholds alert on the PACE rather than the raw bar, so a burst
    that would take days to move a rolling average still pages in
    minutes — and the two-window AND is the anti-flap: ``long_minutes``
    establishes the pace is real (not one unlucky request),
    ``short_minutes`` confirms it is STILL burning now (a burst that
    ended refuses to page). Defaults are the SRE workbook's canonical
    pairs: page on 14.4x over 1h confirmed by 5m (2% of a 30-day
    budget in an hour), ticket on 3x over 1d confirmed by 2h.
    """

    name: str
    rate: float = Field(gt=0)  # burn-rate bar (observed / allowed)
    long_minutes: int = Field(gt=0)  # the pace-establishing window
    short_minutes: int = Field(gt=0)  # the still-burning confirmation window

    @model_validator(mode="after")
    def _short_within_long(self) -> "SLOBurnRule":
        # The confirmation window must sit inside the pace window —
        # a short window wider than the long one is not a
        # confirmation of anything.
        if self.short_minutes > self.long_minutes:
            raise ValueError(
                f"burn rule '{self.name}': short_minutes ({self.short_minutes}) "
                f"must not exceed long_minutes ({self.long_minutes}) — the "
                "confirmation window is a subset of the pace window"
            )
        return self


class SLOAlertsConfig(BaseModel):
    """SLO alert delivery (tier 44): webhook + events when a target's
    verdict or budget turns bad, and when it recovers. Delivery is
    best-effort with bounded retries (the budget-alert semantics);
    without a ``webhook_url`` (or ``url_env``) the webhook is a no-op
    that still counts as ``skipped`` on ``gateway_alerts_total`` —
    the ``slo.alert`` / ``slo.recovered`` observer events fire either
    way. ``enabled: false`` turns the whole evaluator off (no state
    writes, no events, no webhooks) while SLO *measurement* keeps
    serving at ``GET /admin/slos``."""

    enabled: bool = True
    webhook_url: str | None = None
    url_env: str | None = None  # env var that overrides webhook_url when set
    timeout_seconds: float = 5.0
    max_attempts: int = 2  # 1 retry, 0.5s backoff (budget-alert pacing)

    def resolve_url(self) -> str | None:
        if self.url_env and os.environ.get(self.url_env):
            return os.environ[self.url_env]
        return self.webhook_url


class SLOsConfig(BaseModel):
    """Service level objectives (tier 42): named, deliberate promises
    about the client experience — ``p95 latency under 800ms``, ``error
    rate under 1%`` — measured over the request logs and verdicted
    honestly at ``GET /admin/slos`` (met / breached /
    insufficient_evidence, with the numbers that produced the verdict
    beside it). Pure config: evaluation is on demand at read time, so
    targets are hot-reloadable — tighten a bar and the next read
    re-verdicts. Alert delivery on breach (webhook + re-arm semantics)
    is the next tier; this surface is the measurement and the honest
    answer, never a pager.

    Tier 43 adds the error-budget lens: every metric restated as a
    bad-event fraction against its allowance (``error_rate``'s bad
    events are failed+blocked rows and the allowance is the bar
    itself; a latency/TTFT percentile bar implies the allowance
    ``1 - p`` of requests may exceed it), burn rate = observed /
    allowed, budget remaining, and the ``burn`` rules — multiwindow
    pace verdicts over the same rows. ``burn: []`` disables the
    pace verdicts (the budget math stands on its own).

    Tier 44 adds delivery: ``evaluation_interval_seconds`` paces the
    background evaluator (the canary-loop shape — always started,
    re-reading this config each pass, so hot-reload applies from the
    next pass), and ``alerts`` configures the webhook. Fire-once /
    re-arm per target and condition, persisted on the settings KV
    table so restarts never re-page; recovery fires when a target is
    provably healthy again — a quiet window (insufficient_evidence)
    is *unknown*, never a recovery.
    """

    targets: list[SLOTargetConfig] = Field(default_factory=list)
    # The SRE workbook's canonical pairs: page-fast, ticket-slow.
    burn: list[SLOBurnRule] = Field(
        default_factory=lambda: [
            SLOBurnRule(
                name="fast-burn", rate=14.4, long_minutes=60, short_minutes=5
            ),
            SLOBurnRule(
                name="slow-burn", rate=3.0, long_minutes=1440, short_minutes=120
            ),
        ]
    )
    # The evaluator's pace (tier 44). One pass = the same status() the
    # admin endpoint serves, so an alert is decided by exactly the
    # numbers the dashboard shows.
    evaluation_interval_seconds: float = Field(default=60.0, gt=0)
    alerts: SLOAlertsConfig = Field(default_factory=SLOAlertsConfig)


class FinopsConfig(BaseModel):
    """Cost anomaly flags (tier 46): unusual spending and traffic
    patterns on the request logs, surfaced as *flags* beside the
    invoice — never pager alerts (error-rate health is the SLO
    epic's job; this is the money lens).

    The shape is deliberately simple and honest: a recent **window**
    (``window_hours``) compared against the trailing **baseline**
    (``baseline_days`` immediately before it), per key and per model.
    A spike needs the baseline to have something to say — fewer than
    ``min_baseline_requests`` baseline rows means *no verdict*, not a
    scary ratio on thin evidence. ``spike_ratio`` is the multiple of
    the baseline's daily rate that counts. ``enabled: false`` turns
    detection off (the endpoint serves an empty flag list and says
    so) while the invoice keeps serving.
    """

    enabled: bool = True
    window_hours: float = Field(default=24.0, gt=0)
    baseline_days: float = Field(default=14.0, gt=0)
    spike_ratio: float = Field(default=3.0, gt=0)
    min_baseline_requests: int = Field(default=10, ge=1)


class ForecastAlertsConfig(BaseModel):
    """Budget forecast alert delivery (tier 48): webhook + events when
    a key's or tenant's month-end PROJECTION (tier 47) trends at_risk
    or over its ``monthly_budget_usd`` — paging on where the month is
    going, before the money is spent (actual spend crossings are the
    tier-2 threshold alerts' job). One pass = the same ``forecast()``
    the admin endpoint serves, so an alert is decided by exactly the
    numbers the dashboard shows. Fire-once / re-arm per (month,
    entity), persisted on the settings KV table so restarts never
    re-page; recovery fires when the projection is provably back
    within plan (often because the operator raised the budget — an
    honest fix recovers honestly). ``min_elapsed_days`` is the
    early-month honesty gate: before it, a day-one straight-line
    extrapolation is hours of evidence dressed as a trend and never
    reaches a webhook. Delivery is best-effort with bounded retries;
    without a ``webhook_url`` (or ``url_env``) the webhook is a no-op
    that still counts as ``skipped`` on ``gateway_alerts_total
    {kind="forecast"}`` — the ``forecast.alert`` / ``forecast.recovered``
    observer events fire either way. ``enabled: false`` turns the
    whole evaluator off (no state writes, no events, no webhooks)
    while the forecast keeps serving at ``GET /admin/forecasts``
    (with each section's persisted alert state and this config
    echoed)."""

    enabled: bool = True
    webhook_url: str | None = None
    url_env: str | None = None  # env var that overrides webhook_url when set
    timeout_seconds: float = 5.0
    max_attempts: int = 2  # 1 retry, 0.5s backoff (budget-alert pacing)
    # The early-month gate (default: the forecast's trend threshold —
    # under two days of month there is no honest trend to page on).
    min_elapsed_days: float = Field(default=2.0, ge=0)
    # The evaluator's pace. Money trends move slower than SLO windows;
    # one pass = one full-platform forecast.
    evaluation_interval_seconds: float = Field(default=300.0, gt=0)

    def resolve_url(self) -> str | None:
        if self.url_env and os.environ.get(self.url_env):
            return os.environ[self.url_env]
        return self.webhook_url


class BudgetEnforcementConfig(BaseModel):
    """Budget enforcement stance (tier 50): the projected leg of the
    request-time budget gate. The hard caps — ``max_budget_usd`` and
    the daily/monthly windows, key and tenant — are enforced ALWAYS;
    they are the floor, not a stance. This config adds the trending
    tooth the floor lacks: a key at $8 of a $10 monthly budget on
    day 2, burning at a month-end pace of $120, sails under the hard
    cap until actual spend crosses $10 — then 429s with the money
    already gone. With ``enabled: true``, a key or tenant whose
    month-end RUN-RATE PROJECTION exceeds its ``monthly_budget_usd``
    is denied with the same 429 ``insufficient_quota``, the message
    saying it is the projection, with the numbers.

    The projection is straight-line: auth-snapshot monthly spend over
    elapsed days, scaled to the month — the same COUNTER lens the
    hard cap reads (never the billing lens), so the two legs can
    never disagree about what "spent" means, and it costs no extra
    query on the hot path. It self-heals: spend over elapsed days is
    a ratio, so a caller that genuinely stops spending watches its
    projection decay toward the actual as the month elapses and
    traffic resumes — the hard cap stays the wall. ``min_elapsed_days``
    is the forecast family's early-month honesty gate (tiers 47/48):
    before it, a day-one straight-line extrapolation is hours of
    evidence dressed as a trend and never denies a request.

    Off by default: today's behavior (hard caps only) is exactly
    preserved until the operator opts in."""

    enabled: bool = False
    # The early-month gate (default: the forecast family's trend
    # threshold — under two days of month there is no honest trend
    # to enforce on).
    min_elapsed_days: float = Field(default=2.0, ge=0)


class PIIScanConfig(BaseModel):
    """PII/secret scan over *stored* request/response bodies — the
    audit that answers "did secrets or PII land in the request log?".
    Clients paste API keys into prompts; guardrails may be off, in
    observe mode, or only masking the *upstream* copy — the log keeps
    the original as received, so this is where exposure actually
    shows. On demand: ``GET /admin/pii-scan`` over recent rows,
    ``GET /admin/logs/{id}/pii-scan`` for one; and (tier 53) a
    background sweep that finds what landed without an admin asking —
    findings persist once per row, newly flagged rows fire
    ``pii.found``, flagged rows are *quarantined* (bodies masked on
    the full read, withheld from the replay export) and, with
    ``purge_days``, their bodies are eventually deleted outright.

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
    # Quarantine (mask flagged rows' bodies on the full read + withhold
    # them from the replay export). Opt out to serve raw bodies even
    # for flagged rows — the findings stay visible either way.
    quarantine: bool = True
    # The sweep: one pass per interval over rows not yet scanned
    # (bounded by sweep_max_rows per pass), then the purge leg.
    sweep_interval_seconds: float = 3600.0
    sweep_max_rows: int = 500
    # Delete a flagged row's stored bodies this many days after its
    # findings landed (metadata + findings stay for the audit trail;
    # None = keep until general log retention).
    purge_days: float | None = None
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


class TrialsConfig(BaseModel):
    """Candidate trials (tier 36): the deliberate-replay leg of the
    provider-migration epic. ``POST /admin/trials`` takes a captured
    traffic window (request-log rows of one public model with stored
    bodies) and replays it against a named candidate deployment — real
    upstream calls, unlike the dry-run replay harness — writing shadow
    request-log rows that score with the tier-35 verdict vocabulary
    (``core/shadow_diff``). This is for candidates shadow mirroring
    hasn't seen enough traffic for, or that aren't wired as shadow
    deployments.

    Trial runs are THROTTLED and SPEND-CAPPED, and deliberately
    mirror-like: no guardrails, no rate-limit/budget/idempotency
    counters, no cache, no breaker bookkeeping (a trial must not
    punish a serving deployment), no metrics — the run row plus the
    scored log rows are the evidence surface.

    The three ``max_*`` fields are *ceilings*: per-run values in the
    POST are clamped into range, so a caller can never ask for more
    replay volume, parallelism or spend than the platform allows.
    """

    enabled: bool = True
    # Ceiling for per-run ``limit`` (replayed entries per run).
    max_entries: int = 200
    # Ceiling for per-run ``concurrency`` (upstream calls in flight per
    # wave; waves run to completion before the next starts, and the
    # cost cap + cancel checks happen between waves).
    max_concurrency: int = 2
    # Ceiling for per-run ``max_cost_usd`` (cumulative candidate spend
    # the run may accrue before the next wave is refused).
    max_cost_usd: float = 1.0


class MigrationConfig(BaseModel):
    """Migration verdicts (tier 37): the go/no-go recommendation engine
    over the migration evidence — shadow-diff mirror groups and
    candidate-trial summaries (``core/migration.evaluate``). Pure
    thresholds, read live off the running config (hot-reloadable —
    tightening a bar re-verdicts on the next read, no restart); every
    verdict response echoes the thresholds it used, and the raw numbers
    always ship beside the verdict so the call is auditable, never
    taken on faith.

    A verdict is a RECOMMENDATION, not a gate: ``go`` means the
    evidence clears every bar, ``hold`` means the answers are
    acceptable but a soft concern (marginal similarity, a latency
    regression) needs a business call, ``no_go`` means the evidence
    says the candidate is not ready (answers diverge, or the candidate
    fails the traffic it is given), ``insufficient_evidence`` means
    there is not enough to judge — collect more traffic or run a
    candidate trial.
    """

    # Evidence a verdict needs, applied to two volumes at once: total
    # pairs (the volume a shadow-error rate can be measured on — every
    # mirror call is evidence) and comparable pairs among them (exact
    # + near + divergent — the volume answer equivalence can be judged
    # on). Thin on either, the verdict is insufficient_evidence
    # regardless of the other numbers — a lucky handful of pairs
    # proves nothing. One exception: a shadow-error rate over its bar
    # on a full volume of pairs is a no_go even when nothing was
    # comparable — a candidate that fails the traffic it is given has
    # already answered the question.
    min_pairs: int = 20
    # (exact + near) / comparable at or above this = answers match.
    min_match_rate: float = 0.95
    # shadow_error / pairs at or below this — a candidate that fails
    # the traffic it is given is a no_go however good the rest looks.
    max_shadow_error_rate: float = 0.02
    # Mean similarity over comparable pairs at or above this — catches
    # all-"near" runs (every answer cosmetically drifted yet none
    # divergent) that a bare match rate would wave through.
    min_similarity_mean: float = 0.90
    # Candidate mean latency vs the serving primary's, in percent
    # (positive = candidate slower) at or below this = hold-level
    # concern. Latency is a business trade, not a correctness bar.
    max_latency_delta_pct: float = 25.0


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

    State is in-process by default (like the idempotency in-flight
    map and runaway detection): a multi-replica fleet enforces its
    per-replica share. ``backend: redis`` (tier 62) moves the slot
    count into Redis — one lease per admitted call, cap checks
    fleet-shared — with ``lease_ttl_seconds`` as the crashed-replica
    backstop: a lease that stops renewing expires, and the next
    admission prunes it, so ghost slots self-heal within one TTL. The
    wait queue stays per-replica either way (bounded by
    ``wait_seconds``).
    """

    enabled: bool = True
    # How long a request queues for a slot before failing with 429
    # (+ Retry-After). 0 = fail fast when all slots are taken.
    wait_seconds: float = 10.0
    backend: str = "memory"  # memory | redis
    # Redis backend only: how long an admitted call's lease lives.
    # Release removes it; a renewal sweeper refreshes live leases
    # every ttl/2; a lease that stops renewing (a crashed replica)
    # expires at this bound and the next admission prunes it. Read at
    # construction — caps and wait_seconds hot-reload, the TTL applies
    # from the next restart.
    lease_ttl_seconds: float = 60.0


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


class SSOConfig(BaseModel):
    """OIDC single sign-on for the admin dashboard (``auth.sso``).

    The dashboard's default login is the raw admin key pasted into the
    browser — a long-lived root credential in sessionStorage, shared by
    every human who opens the tab. Enabling this replaces that with the
    corporate identity: an authorization-code + PKCE login against an
    OIDC provider, and the browser only ever holds a short-lived,
    gateway-minted session token (the session ``ttl``). ``sub`` of the
    minted session is the caller's email; sessions authenticate as
    *platform admins* via the chain step ``sso_session`` (auto-inserted
    when enabled — before ``jwt`` so gateway sessions never depend on
    external-token config), and audit events record the human
    (``actor`` = the email, ``auth_method`` = ``sso``) instead of a
    shared key.

    Who may sign in is an explicit allowlist: exact ``allowed_emails``
    or ``allowed_domains`` suffixes (case-insensitive). Everything
    else is refused at the callback with an honest reason. The raw-key
    login keeps working alongside — SSO is additive, for operators.

    Secrets never live in config.yaml (the house rule): the client
    secret and the session-signing secret are environment variables
    (``client_secret_env`` / ``session_secret_env`` — absent client
    secret = a public client, PKCE mandatory, which it is anyway).
    Requires PyJWT (the ``auth`` extra).
    """

    enabled: bool = False
    # Discovery base — GET {issuer}/.well-known/openid-configuration
    # (explicit endpoint overrides below skip the fetch).
    issuer: str = ""
    client_id: str = ""
    # Env var holding the OIDC client secret (confidential clients).
    # None = public client: PKCE carries the proof instead.
    client_secret_env: str | None = None
    # The full callback URL as registered with the IdP. Explicit on
    # purpose — deriving from request hosts behind proxies is how
    # open-redirect bugs are born.
    redirect_uri: str = ""
    # Explicit endpoint overrides — skip discovery (unusual IdPs, or
    # pinned endpoints). Any one unset falls back to discovery for it.
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    jwks_url: str | None = None
    # Who may sign in (either match → platform admin).
    allowed_emails: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    # Lifetime of the minted session token — the browser-held
    # credential's blast radius. Short by design: refresh is one click
    # (the cookie re-issues while it lives).
    session_ttl_seconds: float = 900.0
    session_secret_env: str = "GATEWAY_SSO_SESSION_SECRET"
    # The dashboard button text ("Sign in with Google" &c).
    label: str = "Single sign-on"
    discovery_cache_seconds: float = 3600.0


class AuthConfig(BaseModel):
    """The authentication chain: which strategies may authenticate a
    caller, in order. Built-in steps: ``admin_key``, ``virtual_key``,
    ``jwt``; a ``"pkg.module:ClassName"`` entry loads a custom
    ``Authenticator`` subclass (instantiated with this config). The first
    step that returns a principal wins; a step that positively rejects
    the credential fails the request immediately. ``sso_session`` is
    added automatically when ``sso.enabled`` (before ``jwt``), so the
    dashboard's minted sessions authenticate without touching
    external-token config."""

    chain: list[str] = Field(
        default_factory=lambda: ["admin_key", "virtual_key"]
    )
    jwt: JWTAuthConfig = Field(default_factory=JWTAuthConfig)
    sso: SSOConfig = Field(default_factory=SSOConfig)


class MCPServerConfig(BaseModel):
    """One proxied MCP server (streamable HTTP upstream).

    The gateway is the client toward this URL: every client POST is
    relayed with the headers below — and *only* those plus the protocol
    headers (the client's gateway credential never travels upstream;
    the upstream's auth is the operator's concern, configured here).

    ``headers`` are literal values (dev/tests); ``headers_env`` names an
    environment variable per header, resolved at call time (secrets —
    the same literal/env split ``ProviderConfig`` uses for keys). Env
    entries win over literals on the same name.
    """

    name: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    headers_env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float | None = None

    def resolve_headers(self) -> dict[str, str]:
        import os

        resolved = {k: v for k, v in self.headers.items()}
        for name, env in self.headers_env.items():
            value = os.environ.get(env)
            if value is not None:
                resolved[name] = value
        return resolved


class MCPConfig(BaseModel):
    """The MCP proxy surface (tier 33): ``POST /v1/mcp/{server}`` speaking
    the MCP Streamable HTTP transport, proxying to the configured
    servers. ``enabled: false`` 404s the routes (like ``batch.enabled``).

    Sessions: the gateway issues its OWN ``Mcp-Session-Id`` per
    ``initialize`` and maps it to the upstream session + the negotiated
    protocol version; idle sessions are swept after
    ``session_ttl_seconds`` (a client that comes back re-initializes,
    per the transport spec).

    ``breaker`` (tier 66): one circuit breaker per configured SERVER —
    the same state machine the router runs per deployment
    (``core/breaker.py``; ``backend: redis`` shares trips across the
    fleet, in its own keyspace), but with MCP-specific accounting: a
    failure is an upstream CONNECT error (``httpx.ConnectError`` /
    ``ConnectTimeout`` — the server is unreachable) or an HTTP 5xx
    answer (the server is up and broken). NEVER latency: tools
    legitimately run long, so read/write timeouts and slow answers are
    not evidence of anything. 4xx answers and JSON-RPC error frames in
    200 bodies are the server answering fine — evidence of health, and
    a success in between resets the consecutive count, so an
    intermittently erroring tool never trips anything. There is no
    all-open bypass here: a deployment has a failover ladder, an MCP
    server is the only upstream for its name, so an open circuit
    REFUSES the message in-band (JSON-RPC ``-32004``, HTTP 200) —
    failing fast instead of stalling a concurrency slot for the whole
    upstream timeout. The breaker refuses, never retries: no call is
    ever replayed, so none of the side-effect hazards of retrying a
    tool call exist. ``initialize`` is gated too — after the cooldown it
    is the natural probe (one success closes the circuit)."""

    enabled: bool = False
    session_ttl_seconds: float = 3600.0
    servers: list[MCPServerConfig] = Field(default_factory=list)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)

    def server(self, name: str) -> MCPServerConfig | None:
        for server in self.servers:
            if server.name == name:
                return server
        return None


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
    vault: VaultConfig = Field(default_factory=VaultConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    uploads: UploadsConfig = Field(default_factory=UploadsConfig)
    batch: BatchConfig = Field(default_factory=BatchConfig)
    canary: CanaryConfig = Field(default_factory=CanaryConfig)
    slos: SLOsConfig = Field(default_factory=SLOsConfig)
    finops: FinopsConfig = Field(default_factory=FinopsConfig)
    forecast_alerts: ForecastAlertsConfig = Field(default_factory=ForecastAlertsConfig)
    budget_enforcement: BudgetEnforcementConfig = Field(
        default_factory=BudgetEnforcementConfig
    )
    pii_scan: PIIScanConfig = Field(default_factory=PIIScanConfig)
    idempotency: IdempotencyConfig = Field(default_factory=IdempotencyConfig)
    replay: ReplayConfig = Field(default_factory=ReplayConfig)
    trials: TrialsConfig = Field(default_factory=TrialsConfig)
    migration: MigrationConfig = Field(default_factory=MigrationConfig)
    runaway: RunawayConfig = Field(default_factory=RunawayConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
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
