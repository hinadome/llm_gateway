"""Pydantic schemas: OpenAI-compatible request/response models.

The gateway speaks the OpenAI wire format on its south side (clients) and
translates to/from provider-native formats internally.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


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


class KeyCreateRequest(BaseModel):
    name: str
    # Admin grants access to the admin API. With no tenant: a platform
    # admin (deliberate delegation, same power as the operator). Inside
    # a tenant: a tenant admin, scoped to their own tenant by every
    # endpoint (platform-only endpoints stay 403). Not patchable later.
    is_admin: bool = False
    allowed_models: list[str] | None = None  # fnmatch patterns; None = all
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    max_budget_usd: float | None = None
    daily_budget_usd: float | None = None  # UTC calendar-day cap
    monthly_budget_usd: float | None = None  # UTC calendar-month cap
    expires_at: str | None = None  # ISO-8601
    metadata: dict[str, Any] | None = None
    disabled_guardrails: list[str] | None = None  # guardrail names to skip
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


class KeyUpdateRequest(BaseModel):
    allowed_models: list[str] | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
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


class TenantCreateRequest(BaseModel):
    """POST /admin/tenants — create a tenant (platform admins only)."""

    # Slug: it surfaces in API responses, log filters and the
    # X-Gateway-Tenant response header — the pattern keeps it
    # header-safe (no newlines, spaces or control characters).
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    display_name: str | None = Field(default=None, max_length=128)
    is_active: bool = True
    allowed_models: list[str] | None = None  # fnmatch patterns; None = all
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    max_budget_usd: float | None = None
    daily_budget_usd: float | None = None  # UTC calendar-day cap
    monthly_budget_usd: float | None = None  # UTC calendar-month cap


class TenantUpdateRequest(BaseModel):
    """PATCH /admin/tenants/{id} — every field optional, like keys."""

    display_name: str | None = Field(default=None, max_length=128)
    is_active: bool | None = None
    allowed_models: list[str] | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    max_budget_usd: float | None = None
    daily_budget_usd: float | None = None
    monthly_budget_usd: float | None = None


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
