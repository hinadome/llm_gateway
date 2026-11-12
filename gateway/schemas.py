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
