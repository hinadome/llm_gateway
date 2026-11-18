"""Pydantic schemas: OpenAI-compatible request/response models.

The gateway speaks the OpenAI wire format on its south side (clients) and
translates to/from provider-native formats internally.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from .config import normalize_residencies


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    arguments: str = ""


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str = "function"
    function: FunctionCall


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None  # str | list[content parts] | None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class ToolFunction(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class Tool(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = "function"
    function: ToolFunction


class ChatCompletionRequest(BaseModel):
    """POST /v1/chat/completions body (OpenAI schema, extras allowed)."""

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[Message]
    temperature: float | None = None
    top_p: float | None = None
    n: int | None = None
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    stop: str | list[str] | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    logit_bias: dict[str, float] | None = None
    # Declared (not just passed through as extras) because they are part of
    # the cache identity; providers that ignore them simply never see them.
    logprobs: bool | None = None
    top_logprobs: int | None = None
    user: str | None = None
    tools: list[Tool] | None = None
    tool_choice: Any | None = None
    response_format: dict[str, Any] | None = None
    seed: int | None = None
    # Audio-output chat (gpt-4o-audio style): which output modalities the
    # client wants, and the voice/format to synthesize with. Declared so
    # adapters can act on them (Gemini translates to responseModalities
    # + speechConfig; Anthropic refuses); they ride to OpenAI/Azure
    # verbatim like every other field.
    modalities: list[str] | None = None
    audio: dict[str, Any] | None = None

    # Original Responses-API body when the request arrived via
    # POST /v1/responses (None otherwise), plus the messages snapshot at
    # parse time. Adapters serving native_responses deployments forward
    # the original body southbound instead of the chat-shaped conversion
    # — but only while the messages are still the pristine parse (a
    # guardrail or prompt-policy modification means falling back to the
    # conversion, so modifications are never silently lost). Private
    # attrs: never serialized, excluded from cache identity and logs.
    _responses_body: dict[str, Any] | None = PrivateAttr(default=None)
    _responses_pristine_messages: list[dict[str, Any]] | None = PrivateAttr(default=None)

    @property
    def effective_max_tokens(self) -> int | None:
        return self.max_completion_tokens or self.max_tokens


class EmbeddingRequest(BaseModel):
    """POST /v1/embeddings body (OpenAI schema, extras allowed).

    ``input`` accepts a string or a list of strings. OpenAI additionally
    accepts pre-tokenized input (arrays of token ids); that is rejected
    with a 400 here — tokenize client-side if you need it.
    """

    model_config = ConfigDict(extra="allow")

    model: str
    input: str | list[str]
    encoding_format: str = "float"  # "float" | "base64"
    dimensions: int | None = None
    user: str | None = None


class ImageGenerationRequest(BaseModel):
    """POST /v1/images/generations body (OpenAI Images schema, extras allowed).

    ``n`` defaults to 1. ``size``/``quality``/``style`` pass through to the
    provider; ``response_format`` (``url`` | ``b64_json``) applies to the
    DALL·E API — gpt-image-1 always returns base64. Streaming partial
    images (gpt-image-1 ``stream: true``) are not supported.
    """

    model_config = ConfigDict(extra="allow")

    model: str
    prompt: str
    n: int = 1
    size: str | None = None
    quality: str | None = None
    style: str | None = None
    response_format: str | None = None
    user: str | None = None


class ImageEditRequest(BaseModel):
    """Internal shape for /v1/images/edits (built from the parsed multipart
    form, never directly deserialized from JSON).

    ``images`` carries the uploaded image bytes (one or more — gpt-image-1
    accepts several; a plain-string form value is accepted as base64 image
    data, with an optional ``data:`` URL prefix). ``mask`` is the optional
    transparent-area mask (PNG). ``extra`` holds unknown form fields for
    provider passthrough.
    """

    model: str
    prompt: str
    images: list[tuple[bytes, str, str | None]] = []  # (bytes, filename, content_type)
    mask: tuple[bytes, str, str | None] | None = None
    n: int = 1
    size: str | None = None
    quality: str | None = None
    response_format: str | None = None
    user: str | None = None
    extra: dict[str, list[str]] = {}


class ImageVariationRequest(BaseModel):
    """Internal shape for /v1/images/variations (multipart, no prompt —
    OpenAI's DALL·E 2 variations surface)."""

    model: str
    images: list[tuple[bytes, str, str | None]] = []
    n: int = 1
    size: str | None = None
    response_format: str | None = None
    user: str | None = None
    extra: dict[str, list[str]] = {}


class TranscriptionRequest(BaseModel):
    """Internal shape for /v1/audio/transcriptions + /v1/audio/translations.

    Built by the API layer from the parsed multipart form (never directly
    deserialized from JSON): ``file`` carries the audio bytes,
    ``endpoint`` selects which OpenAI audio endpoint to call, and ``extra``
    holds unknown form fields for provider passthrough.
    """

    model: str
    endpoint: str = "transcriptions"  # "transcriptions" | "translations"
    file: bytes
    file_name: str = "audio"
    file_content_type: str | None = None
    language: str | None = None
    prompt: str | None = None
    response_format: str | None = None  # json | text | srt | verbose_json | vtt
    temperature: float | None = None
    extra: dict[str, list[str]] = {}


class SpeechRequest(BaseModel):
    """POST /v1/audio/speech body (OpenAI Speech schema, extras allowed).

    The response is binary audio (``response_format`` selects the codec —
    mp3 by default), not JSON.
    """

    model_config = ConfigDict(extra="allow")

    model: str
    input: str
    voice: str
    response_format: str | None = None
    speed: float | None = None
    user: str | None = None


class FeedbackRequest(BaseModel):
    """POST /v1/feedback body — client-reported quality for one request.

    ``request_id`` is the value the gateway returned in the response's
    ``X-Request-Id`` header (or the completion's ``id``-adjacent log
    id). ``rating`` is 1..5 (4/5 read as positive, 1/2 negative in
    admin stats). Feedback must come from the key that made the
    request; a foreign or unknown id 404s.
    """

    request_id: str = Field(min_length=1, max_length=64)
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


# --------------------------------------------------------------------------
# Response models
# --------------------------------------------------------------------------


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ResponseMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str = "assistant"
    content: Any = None
    tool_calls: list[ToolCall] | None = None


class Choice(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int = 0
    message: ResponseMessage
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[Choice]
    usage: Usage | None = None
    system_fingerprint: str | None = None


# --------------------------------------------------------------------------
# Streaming models
# --------------------------------------------------------------------------


class DeltaToolCallFunction(BaseModel):
    name: str | None = None
    arguments: str | None = None


class DeltaToolCall(BaseModel):
    index: int
    id: str | None = None
    type: str | None = None
    function: DeltaToolCallFunction | None = None


class DeltaMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str | None = None
    content: str | None = None
    tool_calls: list[DeltaToolCall] | None = None


class StreamChoice(BaseModel):
    index: int = 0
    delta: DeltaMessage
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[StreamChoice] = []
    usage: Usage | None = None
    system_fingerprint: str | None = None


# --------------------------------------------------------------------------
# Admin models
# --------------------------------------------------------------------------


class _ResidencyRequirement(BaseModel):
    """Shared field + normalization for the data-residency requirement
    (tier 28): deployment tags that may serve the caller's traffic.
    Tags are lowercased slugs; None or [] = no requirement (an
    explicit empty list on PATCH clears it, exactly like null)."""

    required_residencies: list[str] | None = None

    @field_validator("required_residencies")
    @classmethod
    def _normalize_residency_tags(cls, value: list[str] | None) -> list[str] | None:
        return normalize_residencies(value)


class KeyCreateRequest(_ResidencyRequirement):
    name: str
    # Admin grants access to the admin API. With no tenant: a platform
    # admin (deliberate delegation, same power as the operator). Inside
    # a tenant: a tenant admin, scoped to their own tenant by every
    # endpoint (platform-only endpoints stay 403). Not patchable later.
    is_admin: bool = False
    allowed_models: list[str] | None = None  # fnmatch patterns; None = all
    # Tool-call policy (tier 30, MCP-lite): fnmatch patterns over tool
    # names this key may declare (None = all). A declared tool must pass
    # both the key's and the tenant's patterns — the tenant narrows.
    allowed_tools: list[str] | None = None
    # MCP proxy (tier 33): fnmatch patterns over the MCP server names
    # this key may reach through /v1/mcp/{server} (None = all). A server
    # must pass both this and the tenant's patterns — tenant narrows.
    allowed_mcp_servers: list[str] | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    # Concurrency admission: max simultaneous upstream requests (None
    # = unlimited) + QoS priority (higher admitted first under
    # contention; FIFO within a priority; default 0).
    max_concurrency: int | None = Field(default=None, ge=1)
    priority: int = Field(default=0, ge=-1000, le=1000)
    max_budget_usd: float | None = None
    daily_budget_usd: float | None = None  # UTC calendar-day cap
    monthly_budget_usd: float | None = None  # UTC calendar-month cap
    expires_at: str | None = None
    metadata: dict[str, Any] | None = None
    disabled_guardrails: list[str] | None = None  # guardrail names to skip
    # Data-residency requirement: deployment tags that may serve this
    # key's traffic (None = unrestricted). Lowercased slugs; intersected
    # with the tenant's requirement — the tenant can only narrow.
    # (Field + normalization inherited from _ResidencyRequirement.)
    # Per-key model routing: requested-name -> public-model (applied before
    # global aliases, wins even over real public names), plus this key's
    # default-model catch-all.
    model_mappings: dict[str, str] | None = None
    default_model: str | None = None
    # Gateway-managed prompt policy: system prompt prepended to requests
    # that carry none (None = global prompts.system_prompt), and
    # {{variable}} substitutions for message text (merged over the
    # global prompts.variables, per-key winning).
    system_prompt: str | None = None
    # Named library prompt whose ACTIVE version serves as this key's
    # system prompt (prompt ops: version bump + activate, no restart).
    # Wins over inline system_prompt; a missing name is a request-time
    # 400 (fail closed, like a broken model mapping).
    system_prompt_ref: str | None = None
    prompt_variables: dict[str, str] | None = None
    # Token scoping: data-plane scopes ("chat" | "embeddings" | "models";
    # None = all) and source addresses (plain IP or CIDR; None = any).
    allowed_endpoints: list[str] | None = None
    allowed_ips: list[str] | None = None
    # Owning tenant (platform admins only; tenant admins' keys are
    # forced into their own tenant — a mismatching explicit value is
    # refused rather than silently overridden).
    tenant_id: str | None = None


class KeyUpdateRequest(_ResidencyRequirement):
    allowed_models: list[str] | None = None
    allowed_tools: list[str] | None = None
    allowed_mcp_servers: list[str] | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    max_concurrency: int | None = Field(default=None, ge=1)
    priority: int | None = Field(default=None, ge=-1000, le=1000)
    max_budget_usd: float | None = None
    daily_budget_usd: float | None = None
    monthly_budget_usd: float | None = None
    is_active: bool | None = None
    expires_at: str | None = None
    disabled_guardrails: list[str] | None = None
    model_mappings: dict[str, str] | None = None
    default_model: str | None = None
    system_prompt: str | None = None
    system_prompt_ref: str | None = None
    prompt_variables: dict[str, str] | None = None
    allowed_endpoints: list[str] | None = None
    allowed_ips: list[str] | None = None
    # Moving a key between tenants is a platform-admin operation
    # (tenant admins are refused — it would let keys escape their scope).
    tenant_id: str | None = None


class TenantCreateRequest(_ResidencyRequirement):
    """POST /admin/tenants — create a tenant (platform admins only)."""

    # Slug: it surfaces in API responses, log filters and the
    # X-Gateway-Tenant response header — the pattern keeps it
    # header-safe (no newlines, spaces or control characters).
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    display_name: str | None = Field(default=None, max_length=128)
    is_active: bool = True
    allowed_models: list[str] | None = None  # fnmatch patterns; None = all
    # Tool-call policy (tier 30, MCP-lite): fnmatch patterns over tool
    # names this key may declare (None = all). A declared tool must pass
    # both the key's and the tenant's patterns — the tenant narrows.
    allowed_tools: list[str] | None = None
    # MCP proxy (tier 33): fnmatch patterns over the MCP server names
    # this key may reach through /v1/mcp/{server} (None = all). A server
    # must pass both this and the tenant's patterns — tenant narrows.
    allowed_mcp_servers: list[str] | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    # Cost-plus billing: percent markup on provider cost (20.0 = bill
    # 1.2x actual). Applies to every member-key request, tenant admins
    # included; snapshots at auth time like the other tenant ceilings.
    # None = bill at actual cost (the platform default).
    pricing_markup_pct: float | None = Field(default=None, ge=0, le=1000)
    # Pooled concurrency ceiling across every member key's upstream
    # requests (None = tenant sets none; a member must pass both its
    # own cap and the tenant's).
    max_concurrency: int | None = Field(default=None, ge=1)
    # Data-residency requirement, pooled across every member key:
    # deployment tags that may serve member traffic. Intersected with
    # each key's own requirement — the tenant can only narrow.
    # (Field + normalization inherited from _ResidencyRequirement.)
    max_budget_usd: float | None = None
    daily_budget_usd: float | None = None  # UTC calendar-day cap
    monthly_budget_usd: float | None = None  # UTC calendar-month cap


class TenantUpdateRequest(_ResidencyRequirement):
    """PATCH /admin/tenants/{id} — every field optional, like keys."""

    display_name: str | None = Field(default=None, max_length=128)
    is_active: bool | None = None
    allowed_models: list[str] | None = None
    allowed_tools: list[str] | None = None
    allowed_mcp_servers: list[str] | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    pricing_markup_pct: float | None = Field(default=None, ge=0, le=1000)
    max_concurrency: int | None = Field(default=None, ge=1)
    max_budget_usd: float | None = None
    daily_budget_usd: float | None = None
    monthly_budget_usd: float | None = None


class ProviderKeyUpsertRequest(BaseModel):
    """PUT /admin/tenants/{id}/provider-keys/{provider} — store (or
    replace) a tenant's credential for one provider (BYOK vault).

    The key exists in this request body and in the vault's encrypted
    storage — nowhere else. It is never echoed back, not even to the
    caller that just sent it: the response carries only the hint (last
    characters), the audit record the same."""

    key: str = Field(min_length=1)
    # Free-form label so an operator managing many stored keys can tell
    # them apart ("acme-prod", "acme-eu", ...).
    label: str | None = Field(default=None, max_length=128)


class PromptCreateRequest(BaseModel):
    """POST /admin/prompts — append a version to a (new or existing)
    named prompt. The first version of a name auto-activates."""

    name: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1)
    notes: str | None = Field(default=None, max_length=500)


class PromptPatchRequest(BaseModel):
    """PATCH /admin/prompts/{id} — activate (promote/rollback), mark as
    the A/B candidate, or retarget its mirror fraction / notes."""

    activate: bool | None = None
    candidate: bool | None = None
    ab_fraction: float | None = None
    notes: str | None = Field(default=None, max_length=500)


class ReplayRequest(BaseModel):
    """POST /admin/replay — capture lines (GET /admin/logs/export
    output, or a JSONL of it parsed into a list) re-executed against
    the current config. Each entry needs ``request_body``; the
    recorded fields a capture line also carries (key identity, status,
    model, deployment, guardrail verdicts, route) enable the diff.
    A bare chat request works too — it replays blind (prediction, no
    diff)."""

    entries: list[dict] = Field(min_length=1)


class TrialCreateRequest(BaseModel):
    """POST /admin/trials (tier 36) — replay a captured traffic window
    against a candidate deployment. ``model`` selects the window (that
    public model's logged chat traffic), ``deployment`` the candidate
    (any configured deployment — it need not belong to the model; the
    adapter maps to its upstream model). ``since``/``until`` (ISO-8601)
    bound the window; the three caps are clamped to the ``trials:``
    ceilings server-side, so asking for more than allowed just runs
    at the ceiling."""

    model: str = Field(min_length=1, max_length=128)
    deployment: str = Field(min_length=1, max_length=128)
    since: str | None = Field(default=None, max_length=40)
    until: str | None = Field(default=None, max_length=40)
    limit: int | None = Field(default=None, ge=1)
    concurrency: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)
