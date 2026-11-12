"""RequestContext: per-request state threaded through the pipeline."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from opentelemetry.trace import Span


@dataclass
class AuthContext:
    """Result of API-key authentication."""

    is_admin: bool = False
    key_id: str | None = None
    key_name: str | None = None
    allowed_models: list[str] | None = None  # fnmatch patterns; None = all
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    max_budget_usd: float | None = None
    spend_usd: float = 0.0
    # Budget windows (UTC calendar day/month), snapshotted at auth time like
    # spend_usd. The *_bucket fields identify which window the *_spend_usd
    # value belongs to — a stale bucket means that window has rolled and the
    # spend counts as 0.
    daily_budget_usd: float | None = None
    monthly_budget_usd: float | None = None
    daily_spend_usd: float | None = None
    daily_bucket: str | None = None
    monthly_spend_usd: float | None = None
    monthly_bucket: str | None = None
    # Guardrail names disabled for this key (admin API, no restart needed).
    # Wins over config scoping; None/empty = no overrides.
    disabled_guardrails: list[str] | None = None
    # Per-key model routing (admin API, no restart needed): requested-name
    # -> public-model map applied before global aliases (wins even over
    # real public names — per-team steering), plus this key's default-model
    # catch-all (beats the global default_model). None/empty = no overrides.
    model_mappings: dict[str, str] | None = None
    default_model: str | None = None
    # Gateway-managed prompt policy: the key's system prompt (None =
    # fall back to the global prompts.system_prompt) and its template
    # variables (merged over the global prompts.variables, key wins).
    system_prompt: str | None = None
    # Named library prompt whose active version serves as the system
    # prompt (wins over inline system_prompt; resolved in the pipeline
    # against the prompt library, fail closed on a missing name).
    system_prompt_ref: str | None = None
    prompt_variables: dict[str, str] | None = None
    # Token scoping: data-plane scopes ("chat" | "embeddings" | "models")
    # this key may call (None = all) and the IPs/CIDRs calls must come from
    # (None = any). Never applied to admin contexts.
    allowed_endpoints: list[str] | None = None
    allowed_ips: list[str] | None = None
    # How the caller authenticated ("admin_key", "virtual_key", "jwt" or
    # a custom authenticator's name) — recorded on request logs and audit
    # events. None for contexts built outside the auth chain.
    auth_method: str | None = None


@dataclass
class RequestContext:
    request_id: str
    auth: AuthContext = field(default_factory=AuthContext)

    model: str = ""
    provider: str | None = None
    deployment: str | None = None
    native_model: str | None = None
    stream: bool = False
    # Inbound API surface this request arrived on: "chat"
    # (/v1/chat/completions), "responses" (/v1/responses), "messages"
    # (native Anthropic) or "gemini" (native Gemini). None for
    # programmatic pipeline calls — guardrail route scopes never match
    # those. Drives GuardrailScope.routes / exclude_routes.
    route: str | None = None

    started_monotonic: float = field(default_factory=time.monotonic)

    # guardrail decisions recorded along the way
    guardrail_actions: list[dict[str, Any]] = field(default_factory=list)

    # accounting / observability
    cache_hit: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float = 0.0
    status: str = "success"  # success | error | blocked
    error_message: str | None = None
    # True for shadow (mirrored) calls: not client-facing. Shadow rows are
    # excluded from /admin/stats and never add to key spend — they carry
    # their own cost/bodies for comparison (see pipeline._queue_shadow).
    shadow: bool = False
    # Prompt A/B mirror rows: the library prompt variant the row tested
    # ("name vN"); None on all client-facing rows.
    prompt_variant: str | None = None
    # The library prompt this request was served with (set at step 0.5
    # when the key references one) — drives prompt A/B mirroring.
    library_prompt: Any | None = None
    # OpenTelemetry span for this request (None when tracing is disabled)
    otel_span: "Span | None" = None
    # Trace correlation ids (hex strings; None when tracing is disabled)
    trace_id: str | None = None
    span_id: str | None = None

    # payloads (original as received; effective = post-guardrail, if modified)
    request_body: dict[str, Any] = field(default_factory=dict)
    effective_request: dict[str, Any] | None = None
    response_body: dict[str, Any] | None = None

    def latency_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)

    def record_guardrail(self, entry: dict[str, Any]) -> None:
        self.guardrail_actions.append(entry)
