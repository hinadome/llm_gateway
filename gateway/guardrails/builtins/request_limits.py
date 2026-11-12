"""Request limits guardrail.

Enforces coarse per-request limits (max messages, max characters) to
protect upstream providers from abusive requests. Disabled by default;
enable in config.yaml with params:
    {name: request_limits, enabled: true,
     params: {max_messages: 100, max_chars: 200000}}

When a limit is exceeded the guardrail either blocks (the default) or —
with ``on_exceed: redirect`` — redirects the request to a cheaper model
(``redirect_model``), the reject-and-redirect pattern: "this content is
too big/expensive for the flagship model, send it to the small one"
instead of failing the caller. The redirect target must be allowed for
the calling key (the pipeline re-checks the model allowlist after the
guardrail phase, fail closed) and must be a configured public model
(unknown targets 404 in the router).
"""

from __future__ import annotations

from typing import Any

from ...schemas import ChatCompletionRequest
from ...utils import content_to_text
from ..base import Guardrail, GuardrailResult


class RequestLimitsGuardrail(Guardrail):
    name = "request_limits"
    description = (
        "Enforces max message count / character size on incoming requests — "
        "blocking, or redirecting to a cheaper model with on_exceed: redirect."
    )

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.max_messages: int | None = self.params.get("max_messages")
        self.max_chars: int | None = self.params.get("max_chars")
        self.on_exceed: str = self.params.get("on_exceed", "block")
        self.redirect_model: str | None = self.params.get("redirect_model")
        if self.on_exceed not in ("block", "redirect"):
            raise ValueError(
                f"request_limits: on_exceed must be 'block' or 'redirect', "
                f"got '{self.on_exceed}'"
            )
        if self.on_exceed == "redirect" and not self.redirect_model:
            raise ValueError(
                "request_limits: on_exceed 'redirect' requires a redirect_model "
                "(the public model name oversized requests are sent to)"
            )

    async def check_request(
        self, request: ChatCompletionRequest, ctx
    ) -> GuardrailResult | None:
        exceeded: str | None = None
        if self.max_messages is not None and len(request.messages) > self.max_messages:
            exceeded = (
                f"request contains {len(request.messages)} messages "
                f"(limit {self.max_messages})"
            )
        if exceeded is None and self.max_chars is not None:
            total = sum(len(content_to_text(m.content)) for m in request.messages)
            if total > self.max_chars:
                exceeded = f"request payload is {total} characters (limit {self.max_chars})"
        if exceeded is None:
            return None
        if self.on_exceed == "redirect":
            return GuardrailResult.redirect(
                self.redirect_model or "",
                f"{exceeded} — redirected to {self.redirect_model}",
                limit=exceeded,
            )
        return GuardrailResult.block(exceeded)
