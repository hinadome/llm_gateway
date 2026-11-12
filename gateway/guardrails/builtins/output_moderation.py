"""Output moderation guardrail (heuristic keyword categories).

Checks the model's *output* (non-streaming responses and streaming deltas)
against configurable keyword categories. This is a deliberately conservative
placeholder — for real deployments, subclass :class:`Guardrail` and call a
moderation API (OpenAI moderation, AWS Guardrails, Lakera, Azure Content
Safety, ...) from ``check_response``/``check_stream_delta``.
"""

from __future__ import annotations

from typing import Any

from ...schemas import ChatCompletionRequest
from ...utils import assistant_text, content_to_text
from ..base import Guardrail, GuardrailResult

DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "weapons_of_mass_destruction": [
        "enriched uranium",
        "weapons-grade plutonium",
        "nerve agent synthesis",
        "how to synthesize sarin",
        "bioweapon production",
        "build a nuclear bomb",
    ],
    "self_harm_instructions": [
        "how to commit suicide",
        "painless suicide method",
        "how to self-harm without",
    ],
    "child_safety": [
        "child sexual abuse",
        "csam",
    ],
}


class OutputModerationGuardrail(Guardrail):
    name = "output_moderation"
    description = "Flags model output matching configured harmful-content keyword categories."

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.categories: dict[str, list[str]] = self.params.get("categories") or DEFAULT_CATEGORIES
        # Allow fully disabling individual categories via `categories: {}`.

    def _match(self, text: str) -> tuple[str, str] | None:
        lowered = (text or "").lower()
        for category, terms in self.categories.items():
            for term in terms:
                if term.lower() in lowered:
                    return category, term
        return None

    async def check_response(
        self, request: ChatCompletionRequest, response: dict[str, Any], ctx
    ) -> GuardrailResult | None:
        for choice in response.get("choices", []):
            hit = self._match(assistant_text(choice.get("message") or {}))
            if hit:
                category, term = hit
                return GuardrailResult.block(
                    f"output matched '{category}' category (term: {term!r})",
                    category=category,
                    term=term,
                )
        return None

    async def check_stream_delta(
        self, request: ChatCompletionRequest, accumulated_text: str, delta_text: str, ctx
    ) -> GuardrailResult | None:
        hit = self._match(accumulated_text)
        if hit:
            category, term = hit
            return GuardrailResult.block(
                f"output matched '{category}' category (term: {term!r})",
                category=category,
                term=term,
            )
        return None
