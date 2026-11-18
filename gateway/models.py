"""SQLAlchemy ORM models: tenants, virtual keys, request logs, KV settings."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all gateway ORM models."""


# Portable JSON column type: JSONB on PostgreSQL, plain JSON (text)
# everywhere else (SQLite). JSONB gives semantic equality — the
# budget-alert fired-levels write is a compare-and-swap that matches
# the stored value with `==` — and the better-behaved storage
# (normalized, indexable). On SQLite the JSON type serializes
# deterministically, so the same comparison matches byte-for-byte.
JSONColumn = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


class VirtualKey(Base):
    __tablename__ = "virtual_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Admin grants access to the admin API. With no tenant that is the
    # platform admin (everything); inside a tenant it is a tenant admin
    # — full key/log/stats access *scoped to their own tenant*, and the
    # platform-only endpoints (tenants, prompts, routing, guardrails,
    # reload, backups) stay 403 (see require_platform_admin).
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # Owning tenant (NULL = platform key, no tenant checks apply — the
    # pre-tenancy behavior, unchanged). A member key must pass both its
    # own limits and the tenant's; the tenant can only narrow.
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    allowed_models: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    # Tool-call policy (tier 30, MCP-lite): fnmatch patterns over the
    # tool names this key may declare. None = all tools. A declared tool
    # must pass both this and the tenant's patterns — tenant only narrows.
    allowed_tools: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    # MCP proxy (tier 33): fnmatch patterns over the MCP SERVER names
    # this key may reach through /v1/mcp/{server}. None = all configured
    # servers. A server must pass both this and the tenant's patterns —
    # the tenant only narrows (same mechanics as allowed_models).
    allowed_mcp_servers: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Concurrency admission (tier 27): max simultaneous UPSTREAM
    # requests this key may hold (None = unlimited) and its QoS class
    # — under contention, waiters are admitted highest-priority-first
    # (FIFO within a priority). Priority only matters when a cap is
    # contended. Both snapshot at auth time like the other ceilings.
    # Nullable + default 0 so the auto-upgrade can add it to existing
    # databases (None reads as the default class 0).
    max_concurrency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    max_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    spend_usd: Mapped[float] = mapped_column(Float, default=0.0)
    # Data-residency requirement (tier 28): deployment tags that may
    # serve (and mirror) this key's traffic. JSON list of slugs, None =
    # no requirement. Intersected with the tenant's requirement at auth
    # time — the tenant can only narrow.
    required_residencies: Mapped[list | None] = mapped_column(
        JSONColumn, nullable=True
    )
    # Budget windows: daily/monthly caps alongside the total. Spend and its
    # bucket id live on the row — when the stored bucket differs from the
    # current one the window has rolled and the stored spend counts as 0
    # (lazily reset by the next add_spend). Buckets are UTC calendar days /
    # months; NULL = never spent (treated as 0).
    daily_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_spend_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_bucket: Mapped[str | None] = mapped_column(String(16), nullable=True)
    monthly_spend_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    monthly_bucket: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Budget-alert levels (percent values) already fired for this key —
    # each threshold fires once; raising the budget re-arms below-current
    # levels. Null = never evaluated.
    budget_alerts_fired: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    # Guardrail names that are skipped for requests authenticated with this
    # key — a runtime override of config scoping (see GuardrailScope).
    disabled_guardrails: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    # Per-key model routing (LiteLLM-style per-team config): requested
    # name -> public model name, applied before global aliases — an
    # explicit admin decision for this key wins, even over real public
    # model names (that is what per-team steering means). default_model
    # is this key's catch-all for unknown names; it beats the global one.
    # Both are runtime-settable via the admin API (no restart needed).
    model_mappings: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    default_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Gateway-managed prompt policy (admin API, no restart needed):
    # system_prompt is prepended to this key's requests that carry no
    # system message of their own (overrides the global
    # prompts.system_prompt; NULL = use the global one, "" = none).
    # system_prompt_ref names a library prompt whose ACTIVE version
    # serves instead of any inline text (prompt ops — see Prompt);
    # a missing name is a request-time 400, fail closed.
    # prompt_variables resolve {{placeholders}} in message text and the
    # managed prompt (merged over prompts.variables, key wins).
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_variables: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    # Token scoping (defense in depth beyond model allowlists):
    # allowed_endpoints — data-plane scopes this key may call
    #   ("chat" | "embeddings" | "models"; NULL = all of them), and
    # allowed_ips — plain IPs or CIDR blocks calls must come from
    #   (NULL = any source). The admin key is never scoped.
    allowed_endpoints: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    allowed_ips: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "key_prefix": self.key_prefix,
            "is_active": self.is_active,
            "is_admin": self.is_admin,
            "tenant_id": self.tenant_id,
            "allowed_models": self.allowed_models,
            "allowed_tools": self.allowed_tools,
            "allowed_mcp_servers": self.allowed_mcp_servers,
            "rpm_limit": self.rpm_limit,
            "tpm_limit": self.tpm_limit,
            "max_concurrency": self.max_concurrency,
            "priority": self.priority or 0,
            "max_budget_usd": self.max_budget_usd,
            "spend_usd": self.spend_usd,
            "daily_budget_usd": self.daily_budget_usd,
            "monthly_budget_usd": self.monthly_budget_usd,
            "daily_spend_usd": self.daily_spend_usd or 0.0,
            "daily_bucket": self.daily_bucket,
            "monthly_spend_usd": self.monthly_spend_usd or 0.0,
            "monthly_bucket": self.monthly_bucket,
            "budget_alerts_fired": self.budget_alerts_fired,
            "disabled_guardrails": self.disabled_guardrails,
            "required_residencies": self.required_residencies,
            "model_mappings": self.model_mappings,
            "default_model": self.default_model,
            "system_prompt": self.system_prompt,
            "system_prompt_ref": self.system_prompt_ref,
            "prompt_variables": self.prompt_variables,
            "allowed_endpoints": self.allowed_endpoints,
            "allowed_ips": self.allowed_ips,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Tenant(Base):
    """One tenant (organization) on the gateway.

    Virtual keys reference a tenant via ``tenant_id``; the tenant row
    carries its own policy ceiling — an allowed-models list, RPM/TPM
    limits and the three budget windows (total / daily / monthly) — that
    applies to *every* member key, tenant admins included: a tenant
    admin manages that tenant's keys, not the tenant's policy. A member
    key must pass both its own checks and the tenant's; the tenant can
    only narrow, never widen. Keys with no tenant are platform keys —
    no tenant checks apply to them (the pre-tenancy behavior,
    unchanged).

    The budget machinery mirrors VirtualKey's: spend and window bucket
    ids live on the row and are lazily rolled by ``add_spend``; budget
    alerts fire per threshold with fired levels persisted here.
    """

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    # Slug, validated by the admin API ([A-Za-z0-9][A-Za-z0-9_-]*, at
    # most 64 chars) — it surfaces in API responses, log filters and
    # the X-Gateway-Tenant response header, so header-hostile
    # characters are rejected at creation time, not stripped later.
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # Free-form label for dashboards; the slug is what APIs speak.
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    allowed_models: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    # Pooled tool policy: fnmatch patterns binding every member key.
    # None = the tenant sets no tool policy; a tool must pass both the
    # key's and these patterns.
    allowed_tools: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    # Pooled MCP server policy: fnmatch patterns over server names
    # binding every member key (None = the tenant sets none). A server
    # must pass both the key's and these patterns.
    allowed_mcp_servers: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Cost-plus billing: percent markup on the provider cost of every
    # member-key request (20.0 = bill 1.2x actual). The marked-up cost
    # is what spend, budgets, log rows, exports and the per-key metrics
    # record — the chargeback number. gateway_cost_usd_total{model}
    # keeps the ACTUAL provider cost, so platform economics stay
    # visible beside tenant economics. NULL = no markup (bill at
    # actual — the platform default; auto-upgrade adds the column).
    pricing_markup_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Data-residency requirement, pooled across every member key: the
    # deployment tags that may serve member traffic. Intersected with
    # each member key's own requirement — the tenant can only narrow.
    required_residencies: Mapped[list | None] = mapped_column(
        JSONColumn, nullable=True
    )
    # Pooled concurrency ceiling: max simultaneous UPSTREAM requests
    # across every member key (None = tenant sets none). A member key
    # must pass both its own cap and the tenant's — the tenant can
    # only narrow, like every other ceiling.
    max_concurrency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    spend_usd: Mapped[float] = mapped_column(Float, default=0.0)
    # Budget windows: same scheme as VirtualKey (see there) — bucket ids
    # make the lazily-rolled daily/monthly spends unambiguous.
    daily_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_spend_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_bucket: Mapped[str | None] = mapped_column(String(16), nullable=True)
    monthly_spend_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    monthly_bucket: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Budget-alert levels (percent) already fired for this tenant —
    # same once-per-level semantics as keys.
    budget_alerts_fired: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "display_name": self.display_name,
            "is_active": self.is_active,
            "allowed_models": self.allowed_models,
            "allowed_tools": self.allowed_tools,
            "allowed_mcp_servers": self.allowed_mcp_servers,
            "rpm_limit": self.rpm_limit,
            "tpm_limit": self.tpm_limit,
            "pricing_markup_pct": self.pricing_markup_pct,
            "required_residencies": self.required_residencies,
            "max_concurrency": self.max_concurrency,
            "max_budget_usd": self.max_budget_usd,
            "spend_usd": self.spend_usd,
            "daily_budget_usd": self.daily_budget_usd,
            "monthly_budget_usd": self.monthly_budget_usd,
            "daily_spend_usd": self.daily_spend_usd or 0.0,
            "daily_bucket": self.daily_bucket,
            "monthly_spend_usd": self.monthly_spend_usd or 0.0,
            "monthly_bucket": self.monthly_bucket,
            "budget_alerts_fired": self.budget_alerts_fired,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ProviderKey(Base):
    """BYOK vault (tier 31): one *encrypted* provider credential per
    (tenant, provider). The tenant's stored key replaces the platform's
    configured provider key for every request that tenant makes through
    that provider (see core/vault.py for the security posture). The raw
    key is Fernet-encrypted under GATEWAY_VAULT_MASTER_KEY and exists in
    this row only as ``encrypted_key``; ``to_public_dict`` never exposes
    it (the API speaks ``key_hint`` — the last characters, enough to
    tell stored keys apart, never enough to use)."""

    __tablename__ = "provider_keys"
    __table_args__ = (UniqueConstraint("tenant_id", "provider"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    # Provider id as in config.yaml's providers: section (openai, azure,
    # anthropic, ...) — the same string deployment.provider carries.
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    # Fernet token (base64). Decrypt with the vault master key.
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Last characters of the credential — display only, never a secret.
    key_hint: Mapped[str] = mapped_column(String(12), default="")
    # Free-form label ("acme-prod-openai"), for operators managing many.
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    def to_public_dict(self) -> dict:
        """Metadata only — the encrypted key is deliberately absent, and
        so is anything derived from the raw key beyond the hint."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "provider": self.provider,
            "key_hint": self.key_hint,
            "label": self.label,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class RequestLog(Base):
    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    key_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The calling virtual key's id (NULL for admin-key traffic and rows
    # written before the column existed). The replay harness resolves
    # the *current* key row by id when re-executing captured requests —
    # names are unique today, but ids are the exact-caller identity.
    key_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Which inbound surface served the request ("chat" | "responses" |
    # "messages" | "gemini" | "realtime"; NULL for batch lines and rows
    # written before the column existed). Guardrail route scoping
    # matches on this, so the replay harness re-executes each captured
    # request on its original surface.
    route: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The tenant the calling key belonged to (NULL = platform key / no
    # tenant — also admin-key requests, which carry no key row). Copied
    # from the auth snapshot so per-tenant queries never join.
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    deployment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="success", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    guardrail_actions: Mapped[list | None] = mapped_column(JSONColumn, nullable=True)
    request_body: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    # Wire capture (see observability/wire.py): the allowlisted subset of
    # the client's request headers (user-agent, SDK telemetry, correlation
    # ids — never credentials); the request as actually sent to the
    # provider — method, path and provider-native body, i.e. after model
    # mapping, prompt injection, guardrail edits and provider adaptation;
    # and the allowlisted subset of the provider's response headers
    # (request ids, rate-limit state). Nullable: NULL on rows written
    # before this capture existed (the auto-upgrade adds the columns as
    # NULL), on cache hits (no upstream call happened), and whenever the
    # allowlist matched nothing.
    client_headers: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    upstream_request: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    upstream_headers: Mapped[dict | None] = mapped_column(JSONColumn, nullable=True)
    stream: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Cached *input* tokens the upstream reported (provider-side prompt
    # caching) — a subset of prompt_tokens, billed at the cached_input
    # price. NULL when the upstream reports no detail (also on rows
    # written before the column existed — the auto-upgrade adds it).
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Time-to-first-token for streamed requests (ms from request start
    # to the first content-bearing chunk; see RequestContext.ttft_ms).
    # Nullable: NULL on non-streaming rows and on rows written before
    # the column existed (the auto-upgrade adds it as NULL).
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    # True when this row records a *shadow* (mirrored) call rather than the
    # client-facing request. Shadow rows share the primary's request_id,
    # carry their own cost/bodies, and are excluded from /admin/stats and
    # key spend. Nullable: NULL = False (auto-upgrade on existing DBs).
    shadow: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Prompt A/B mirror rows only: the library prompt variant the row
    # tested ("name vN"). Such rows are shadow rows sharing the primary's
    # request_id; the column is what comparison queries group on.
    prompt_variant: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # BYOK vault (tier 31): the provider whose upstream call was made
    # with the TENANT'S stored credential instead of the platform's
    # configured key (None = platform credential, incl. every row from
    # platform/admin callers and cache hits). Observability only: it
    # answers "why did upstream 401 — because YOUR stored key was
    # rejected" without ever recording the key itself.
    byok_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # OpenTelemetry correlation (hex strings; null when tracing is disabled)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Which chain step authenticated the caller ("virtual_key", "jwt", ...)
    # — null for the admin key and contexts built outside the chain.
    auth_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class SettingKV(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class Prompt(Base):
    """One version of a named, versioned system-prompt library entry.

    A key's ``system_prompt_ref`` names a prompt by ``name``; the row
    with ``is_active`` serves (at most one per name). ``is_candidate``
    marks the A/B challenger (at most one per name): a fraction of the
    successful requests served with the active version is mirrored to
    the candidate in the background for comparison — the candidate never
    serves clients, and promoting it is just activating it. All other
    rows are history (rollback targets).
    """

    __tablename__ = "prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    # What changed in this version (shown in the admin UI).
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    is_candidate: Mapped[bool] = mapped_column(Boolean, default=False)
    # Fraction of successful serving traffic (0.0–1.0, clamped) mirrored
    # to this candidate for comparison. Only meaningful while is_candidate.
    ab_fraction: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "body": self.body,
            "notes": self.notes,
            "is_active": self.is_active,
            "is_candidate": self.is_candidate,
            "ab_fraction": self.ab_fraction,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class StoredFile(Base):
    """A file held by the gateway: batch input JSONL uploaded via
    ``POST /v1/files``, or batch output/error JSONL written by the
    worker at finalization. Content is plain text (JSONL), stored
    inline — batch payloads are bounded by ``batch.max_file_bytes``.
    """

    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    # Owning virtual key (files are private to it; a platform admin key
    # has no key row and cannot own files). Copied tenant_id so tenant
    # scoping never joins (the same denormalization as request_logs).
    key_id: Mapped[str] = mapped_column(String(32), index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    filename: Mapped[str] = mapped_column(String(255), default="input.jsonl")
    # "batch" (input), "batch_output", "batch_errors".
    purpose: Mapped[str] = mapped_column(String(64), default="batch")
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Batch(Base):
    """One OpenAI-style batch: a JSONL of chat requests executed in the
    background by the worker, line by line through the full pipeline.

    Execution state is a cursor (``next_line``) plus one ``BatchResult``
    row per completed line — results append in O(1), a crash loses at
    most the in-flight wave (its lines re-run), and finalization just
    concatenates stored results into the output JSONL.

    Status lifecycle: ``in_progress`` → ``finalizing`` → ``completed``
    (or ``failed`` / ``cancelled`` / ``expired`` from any live state).
    ``finalizing`` is a crash-recovery checkpoint — normally invisible,
    it exists between "all lines have results" and "output files
    written", so a restart resumes finalization instead of redoing
    lines. Validation is synchronous at ``POST /v1/batches`` — there is
    no ``validating`` state.
    """

    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    key_id: Mapped[str] = mapped_column(String(32), index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    input_file_id: Mapped[str] = mapped_column(String(32))
    # OpenAI-compatible fields (endpoint must be /v1/chat/completions;
    # completion_window must be 24h).
    endpoint: Mapped[str] = mapped_column(String(255), default="/v1/chat/completions")
    completion_window: Mapped[str] = mapped_column(String(8), default="24h")
    # Named "meta" in Python because DeclarativeBase reserves .metadata;
    # the column itself is "metadata" (OpenAI's field name).
    meta: Mapped[dict] = mapped_column("metadata", JSONColumn, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="in_progress", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Line counts (request_counts in the API): total is fixed at
    # create; the other two advance with the cursor.
    total_requests: Mapped[int] = mapped_column(Integer, default=0)
    completed_lines: Mapped[int] = mapped_column(Integer, default=0)
    failed_lines: Mapped[int] = mapped_column(Integer, default=0)
    # Cursor: the 0-based input line the next wave starts at. Lines
    # before it all have results.
    next_line: Mapped[int] = mapped_column(Integer, default=0)
    # Deferral: waves do not start before this instant (rate limits and
    # budget ceilings back off instead of failing lines; the
    # completion window bounds how long that can go on).
    retry_not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    output_file_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_file_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Lifecycle timestamps (unix ints in the API; NULL = not reached).
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    in_progress_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finalizing_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the batch must be done by (created_at + completion window).
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BatchResult(Base):
    """Result of one batch input line, appended as the line completes.

    ``response`` is the OpenAI batch-line shape: ``{status_code,
    request_id, body}`` where body is the chat completion (success) or
    the error envelope (failure). ``status`` mirrors OpenAI's
    request_counts split: "succeeded" vs "failed".
    """

    __tablename__ = "batch_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(32), index=True)
    # 0-based position in the input file — the output JSONL preserves
    # input order via this, not insertion order.
    line_index: Mapped[int] = mapped_column(Integer)
    custom_id: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="succeeded")
    response: Mapped[dict] = mapped_column(JSONColumn, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Feedback(Base):
    """Client-reported quality signal attached to one request-log row.

    Rides the chat scope: any key that can chat may rate its own
    requests — ownership is enforced (a foreign or unknown request_id
    404s, the same rule as files/batches; the platform admin may rate
    anything). key/model/tenant are denormalized from the request-log
    row at feedback time so admin stats never join, and so a rating
    still says what model served it even if the model was since
    deleted. Multiple signals per request are allowed (a client may
    correct a rating); there is no uniqueness on request_id.
    """

    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    key_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    key_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Model as logged at feedback time (the request's public model).
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 1..5 — 4/5 read as positive, 1/2 as negative in stats.
    rating: Mapped[int] = mapped_column(Integer)
    # Optional client-supplied text, truncated (never bodies: feedback
    # rows are visible in admin lists, like error messages).
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
