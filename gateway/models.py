"""SQLAlchemy ORM models: virtual keys, request logs, KV settings."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all gateway ORM models."""


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
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_models: Mapped[list | None] = mapped_column(JSON, nullable=True)
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    spend_usd: Mapped[float] = mapped_column(Float, default=0.0)
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
    budget_alerts_fired: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Guardrail names that are skipped for requests authenticated with this
    # key — a runtime override of config scoping (see GuardrailScope).
    disabled_guardrails: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Per-key model routing (LiteLLM-style per-team config): requested
    # name -> public model name, applied before global aliases — an
    # explicit admin decision for this key wins, even over real public
    # model names (that is what per-team steering means). default_model
    # is this key's catch-all for unknown names; it beats the global one.
    # Both are runtime-settable via the admin API (no restart needed).
    model_mappings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
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
    prompt_variables: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Token scoping (defense in depth beyond model allowlists):
    # allowed_endpoints — data-plane scopes this key may call
    #   ("chat" | "embeddings" | "models"; NULL = all of them), and
    # allowed_ips — plain IPs or CIDR blocks calls must come from
    #   (NULL = any source). The admin key is never scoped.
    allowed_endpoints: Mapped[list | None] = mapped_column(JSON, nullable=True)
    allowed_ips: Mapped[list | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
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
            "allowed_models": self.allowed_models,
            "rpm_limit": self.rpm_limit,
            "tpm_limit": self.tpm_limit,
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


class RequestLog(Base):
    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    key_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    deployment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="success", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    guardrail_actions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    request_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    stream: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
