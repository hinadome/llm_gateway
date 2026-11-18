"""Context-window management guardrail.

Fits the incoming request to the *model's* advertised context window
*before* it burns upstream quota: estimated input tokens plus the
response budget (``max_tokens`` when the caller set one, else
``reserve_output_tokens``) must fit ``ModelConfig.context_window``.
Over-window requests get an OpenAI-style 400
(``context_length_exceeded`` wording, with the actual numbers) — or,
with ``on_exceed: redirect``, are sent to a bigger model instead (the
reject-and-redirect pattern: "this document doesn't fit the small
model, serve it on the big one" rather than failing the caller).

The window is *model metadata* declared on the public model
(``context_window:``), not a guardrail param — one number per model
that other surfaces can reuse; the pipeline resolves it onto the
request context (step 0, re-resolved when another guardrail
redirects). Params here are policy: an explicit ``window`` override, a
``default_window`` for models that don't declare one, and the
response-budget reserve.

The estimate is the same ~4-chars/token heuristic the rate limiter
uses on the *outgoing* message list (the managed system prompt is
prepended before this phase, so it's counted). Approximate by design:
this is pre-flight protection for the common case, not a tokenizer —
the provider's own 400 remains the final word at the boundary, and
image/audio parts are counted as their ``[image]``/``[audio]``
placeholders (documented under-count, same trade-off as TPM).
"""

from __future__ import annotations

from typing import Any

from ...schemas import ChatCompletionRequest
from ...utils import content_to_text, estimate_tokens
from ..base import Guardrail, GuardrailResult


class ContextWindowGuardrail(Guardrail):
    name = "context_window"
    description = (
        "Fits incoming requests to the model's declared context window "
        "pre-flight — blocking with OpenAI-style context_length_exceeded "
        "wording, or redirecting to a bigger model with on_exceed: redirect."
    )

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        # Policy knobs. window: force a specific window (wins over the
        # model's declared one — testing, or managing a model that
        # declares none). default_window: applies to models without a
        # declared window; None (default) leaves them unmanaged.
        self.window: int | None = self.params.get("window")
        self.default_window: int | None = self.params.get("default_window")
        # Assumed response budget when the caller doesn't set max_tokens
        # — the request must leave at least this much room.
        self.reserve_output_tokens: int = self.params.get(
            "reserve_output_tokens", 1024
        )
        self.on_exceed: str = self.params.get("on_exceed", "block")
        self.redirect_model: str | None = self.params.get("redirect_model")
        if self.on_exceed not in ("block", "redirect"):
            raise ValueError(
                f"context_window: on_exceed must be 'block' or 'redirect', "
                f"got '{self.on_exceed}'"
            )
        if self.on_exceed == "redirect" and not self.redirect_model:
            raise ValueError(
                "context_window: on_exceed 'redirect' requires a redirect_model "
                "(the public model name over-window requests are sent to)"
            )

    def _window_for(self, ctx) -> int | None:
        return (
            self.window
            if self.window is not None
            else (ctx.context_window if ctx.context_window is not None else self.default_window)
        )

    async def check_request(
        self, request: ChatCompletionRequest, ctx
    ) -> GuardrailResult | None:
        window = self._window_for(ctx)
        if window is None:
            return None  # model unmanaged and no default: nothing to enforce

        # The outgoing message list — the managed system prompt (prepended
        # at step 0.5, before guardrails) is included.
        estimated_input = sum(
            estimate_tokens(content_to_text(m.content)) for m in request.messages
        )
        # Response budget: the caller's cap when set, else the reserve.
        output_budget = (
            request.max_tokens
            if request.max_tokens is not None
            else self.reserve_output_tokens
        )
        if estimated_input + output_budget <= window:
            return None

        # OpenAI-style wording, with the actual numbers so the caller
        # can fix the request without a support ticket.
        output_note = (
            f"max_tokens={request.max_tokens}"
            if request.max_tokens is not None
            else f"an assumed {self.reserve_output_tokens} output tokens "
            "(reserve_output_tokens)"
        )
        reason = (
            f"This model's maximum context length is {window} tokens. "
            f"However, your messages were estimated at {estimated_input} "
            f"tokens plus {output_note} — {estimated_input + output_budget} "
            f"total. Reduce the length of the messages or the requested "
            f"output."
        )
        if self.on_exceed == "redirect":
            return GuardrailResult.redirect(
                self.redirect_model or "",
                f"{reason} — redirected to {self.redirect_model}",
                estimated_input_tokens=estimated_input,
                output_tokens=output_budget,
                window=window,
            )
        return GuardrailResult.block(
            reason,
            estimated_input_tokens=estimated_input,
            output_tokens=output_budget,
            window=window,
        )
