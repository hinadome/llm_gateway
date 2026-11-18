"""RequestContext: per-request state threaded through the pipeline."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..observability import wire

if TYPE_CHECKING:  # pragma: no cover
    from opentelemetry.trace import Span


@dataclass
class AuthContext:
    """Result of API-key authentication."""

    is_admin: bool = False
    key_id: str | None = None
    key_name: str | None = None
    # Tenant membership, snapshotted at auth time from the tenant row
    # (None = platform key — no tenant checks apply). Tenant checks are
    # IN ADDITION to the key's own and apply to every member of the
    # tenant, is_admin keys included: a tenant admin manages that
    # tenant's keys, not the tenant's policy ceiling. allowed_models /
    # rpm/tpm / the three budget windows mirror the key-level fields.
    tenant_id: str | None = None
    tenant_name: str | None = None
    tenant_allowed_models: list[str] | None = None  # fnmatch; None = tenant allows all
    # Pooled tool policy: binds every member key (the ceiling pattern —
    # a tool must pass BOTH the key's and the tenant's patterns, so the
    # tenant can only narrow). None = the tenant sets no tool policy.
    tenant_allowed_tools: list[str] | None = None
    # Pooled MCP server policy (tier 33): fnmatch patterns over the MCP
    # server names member keys may reach (/v1/mcp/{server}). None = the
    # tenant sets none; a server must pass BOTH lists — tenant narrows.
    tenant_allowed_mcp_servers: list[str] | None = None
    tenant_rpm_limit: int | None = None
    tenant_tpm_limit: int | None = None
    tenant_max_budget_usd: float | None = None
    tenant_spend_usd: float = 0.0
    tenant_daily_budget_usd: float | None = None
    tenant_monthly_budget_usd: float | None = None
    tenant_daily_spend_usd: float | None = None
    tenant_daily_bucket: str | None = None
    tenant_monthly_spend_usd: float | None = None
    tenant_monthly_bucket: str | None = None
    # Tenant cost-plus markup (percent, 20.0 = bill 1.2x provider
    # cost), snapshotted at auth time like the other tenant ceilings.
    # The marked-up cost is what spend/budgets/logs/exports record;
    # gateway_cost_usd_total{model} stays ACTUAL. None = no markup
    # (platform keys, or a tenant that sets none).
    tenant_pricing_markup_pct: float | None = None
    # Pooled concurrency ceiling across every member key's UPSTREAM
    # requests (None = tenant sets none). Mirrors tenant_rpm/tpm: the
    # member key must pass both its own cap and the tenant's.
    tenant_max_concurrency: int | None = None
    allowed_models: list[str] | None = None  # fnmatch patterns; None = all
    # Tool-call policy (tier 30, MCP-lite): fnmatch patterns over the
    # tool names a request may DECLARE (the `tools` field, every
    # inbound surface — OpenAI chat, Responses, native Anthropic/Gemini
    # all normalize to it). None = all tools allowed; a request
    # declaring a tool that matches neither the key's nor the tenant's
    # patterns 400s tool_not_allowed. Admin/env keys exempt.
    allowed_tools: list[str] | None = None
    # MCP proxy (tier 33): fnmatch patterns over the MCP server names
    # this key may reach through /v1/mcp/{server}. None = all configured
    # servers; a server must pass both this and the tenant's patterns.
    allowed_mcp_servers: list[str] | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    # Concurrency admission (tier 27): max simultaneous UPSTREAM
    # requests this key may hold (None = unlimited) and its QoS
    # priority — when a cap is contended, waiters are admitted
    # highest-priority-first (FIFO within a priority). Priority only
    # matters under contention. Snapshotted at auth time like the
    # other ceilings.
    max_concurrency: int | None = None
    priority: int = 0
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
    # Data-residency requirement (tier 28): deployment tags this
    # caller's traffic may be served (and mirrored) by — a
    # deployment qualifies only if its `residency` tag is in the
    # effective set. None = no requirement (unrestricted). The
    # effective set is the intersection of the key's and the
    # tenant's requirements: a member must satisfy both, so the
    # tenant can only narrow (an empty intersection means nothing
    # can serve — fail closed, at routing time).
    required_residencies: list[str] | None = None
    tenant_required_residencies: list[str] | None = None
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

    def effective_residencies(self) -> frozenset[str] | None:
        """The deployment-residency tags this caller's traffic may be
        served by: the intersection of the key's and the tenant's
        requirements (a member must satisfy both — the tenant can only
        narrow). None = unrestricted (no filtering; the overwhelmingly
        common case). An EMPTY set means "a requirement exists that
        nothing can satisfy" — routing then fails closed with 404
        residency_not_satisfied."""
        key = self.required_residencies
        tenant = self.tenant_required_residencies
        if key is None and tenant is None:
            return None
        if key is None:
            return frozenset(tenant)
        if tenant is None:
            return frozenset(key)
        return frozenset(key) & frozenset(tenant)


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
    # Time-to-first-token for streamed responses: milliseconds from
    # request start to the first content-bearing chunk off the wire
    # (the first chunk at all when none ever carries content — role
    # chunks arrive back-to-back with content from the provider, so
    # this is what the client perceives as generation starting). None
    # for non-streaming requests, cache-hit replays (nothing went on
    # the wire — the same rule as wire capture) and streams that never
    # yielded a chunk (blocked before dispatch, upstream error at
    # connect). The decode rate derives from it: completion_tokens /
    # seconds AFTER the first token (the generation phase, not the
    # wait).
    ttft_ms: int | None = None
    # True for shadow (mirrored) calls: not client-facing. Shadow rows are
    # excluded from /admin/stats and never add to key spend — they carry
    # their own cost/bodies for comparison (see pipeline._queue_shadow).
    shadow: bool = False
    # Prompt A/B mirror rows: the library prompt variant the row tested
    # ("name vN"); None on all client-facing rows.
    prompt_variant: str | None = None
    # Tier 35 shadow diffing: how this mirror's answer compared to the
    # primary's raw provider text — "exact" | "near" | "divergent" |
    # "shadow_error" | "not_comparable" (core/shadow_diff.py scores at
    # mirror time; None on client-facing rows and pre-tier-35 mirrors).
    shadow_verdict: str | None = None
    # The difflib ratio the verdict was bucketed from, in [0, 1]; None
    # exactly when no comparison happened (error / not comparable), so
    # SQL aggregates skip it. The raw score ships so re-bucketing with
    # different thresholds is possible after the fact.
    shadow_similarity: float | None = None
    # The library prompt this request was served with (set at step 0.5
    # when the key references one) — drives prompt A/B mirroring.
    library_prompt: Any | None = None
    # Idempotency store key ("idem:<key id>:<client key>") when THIS
    # request is the executing original for a caller-supplied
    # Idempotency-Key (waiters leave it None). Held on the ctx so the
    # error paths in handle_chat can release in-flight waiters.
    idempotency_key: str | None = None
    # Fingerprint of the ORIGINAL request (sha256 of the same payload
    # the response cache keys on), computed at phase 2b before
    # guardrails can rebind the request. Stored with the entry so a
    # later retry with different parameters under the same
    # Idempotency-Key is rejected instead of served the wrong answer.
    idempotency_fingerprint: str | None = None
    # Concurrency slot held by this request (pipeline._acquire_slot
    # after the cache miss, before routing). Released wherever the
    # in-flight gauge decrements — _finish_span for non-streams, the
    # stream wrapper's finally for streams — via _release_slot, which
    # is idempotent. Cache hits and idempotent replays never hold one.
    concurrency_slot: Any | None = None
    # The resolved public model's declared context window
    # (ModelConfig.context_window), set at step 0 and re-resolved when
    # a guardrail redirects (4.5). Read by the context_window
    # guardrail; None = the model is unmanaged.
    context_window: int | None = None
    # True only in replay-harness executions (POST /admin/replay):
    # the guardrail manager suppresses its enforcement/observed
    # counters so would-be verdicts never pollute live alert
    # metrics; verdicts still land in guardrail_actions (the replay
    # result is built from them). Never set on client traffic.
    dry_run: bool = False
    # BYOK vault (tier 31): the provider whose upstream call used the
    # TENANT's stored credential instead of the platform's key (None =
    # platform credential — platform/admin callers, cache hits, and
    # everyone without a stored key). Set right after dispatch; flows to
    # the request-log row, the X-Gateway-Byok-Provider header and the
    # gateway_byok_requests_total metric. Observability only: the key
    # itself is never recorded anywhere.
    byok_provider: str | None = None
    # Cached *input* tokens the upstream reported (provider-side
    # prompt caching) — a subset of prompt_tokens, billed at the
    # cached_input price. None when the upstream reports no detail
    # (older rows, cache hits with no stored split, non-chat paths).
    cached_tokens: int | None = None
    # OpenTelemetry span for this request (None when tracing is disabled)
    otel_span: "Span | None" = None
    # Trace correlation ids (hex strings; None when tracing is disabled)
    trace_id: str | None = None
    span_id: str | None = None

    # payloads (original as received; effective = post-guardrail, if modified)
    request_body: dict[str, Any] = field(default_factory=dict)
    effective_request: dict[str, Any] | None = None
    response_body: dict[str, Any] | None = None

    # Wire capture (see observability/wire.py). client_headers is
    # snapshotted here from the request's context (the middleware sets
    # it); the upstream pair is snapshotted by the pipeline in _log,
    # after the provider call, and stays None when nothing went on the
    # wire (cache hit, blocked before dispatch).
    client_headers: dict[str, str] | None = None
    upstream_request: dict[str, Any] | None = None
    upstream_headers: dict[str, str] | None = None

    def __post_init__(self) -> None:
        # Mirror contexts (shadow / A-B) inherit the parent task's copy of
        # the client-headers slot — correct: they serve the same client
        # request. Contexts built outside a request (tests) just get None.
        if self.client_headers is None:
            self.client_headers = wire.client_headers()

    def latency_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)

    def decode_tokens_per_sec(self) -> float | None:
        """Generation-phase speed for streamed responses: completion
        tokens per second AFTER the first token (the decode phase,
        with the time waiting for the model to start excluded). None
        when it cannot be computed (not streamed, no tokens, or a
        decode phase too short to be meaningful)."""
        if (
            self.ttft_ms is None
            or not self.completion_tokens
            or self.latency_ms() <= self.ttft_ms
        ):
            return None
        return round(
            self.completion_tokens / ((self.latency_ms() - self.ttft_ms) / 1000.0), 1
        )

    def record_guardrail(self, entry: dict[str, Any]) -> None:
        self.guardrail_actions.append(entry)
