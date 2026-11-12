"""Configurable keyword/pattern blocklist guardrail.

Runs on the request (prompt) and optionally the response (model output).
``blocked_terms`` are case-insensitive substrings; ``blocked_patterns`` are
regexes.
"""

from __future__ import annotations

import re
from typing import Any

from ...schemas import ChatCompletionRequest
from ...utils import assistant_text, content_to_text
from ..base import Guardrail, GuardrailResult


class KeywordBlocklistGuardrail(Guardrail):
    name = "keyword_blocklist"
    description = "Blocks requests/responses containing configured substrings or regex patterns."

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.blocked_terms: list[str] = [str(t).lower() for t in self.params.get("blocked_terms", [])]
        self.blocked_patterns: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) for p in self.params.get("blocked_patterns", [])
        ]
        self.check_prompt: bool = bool(self.params.get("check_prompt", True))
        self.check_completion: bool = bool(self.params.get("check_completion", True))

    def _match(self, text: str) -> str | None:
        lowered = text.lower()
        for term in self.blocked_terms:
            if term and term in lowered:
                return f"blocked term: {term!r}"
        for pattern in self.blocked_patterns:
            m = pattern.search(text)
            if m:
                return f"blocked pattern matched: {m.group(0)!r}"
        return None

    async def check_request(
        self, request: ChatCompletionRequest, ctx
    ) -> GuardrailResult | None:
        if not self.check_prompt:
            return None
        text = "\n".join(content_to_text(m.content) for m in request.messages)
        reason = self._match(text)
        if reason:
            return GuardrailResult.block(reason)
        return None

    async def check_response(
        self, request: ChatCompletionRequest, response: dict[str, Any], ctx
    ) -> GuardrailResult | None:
        if not self.check_completion:
            return None
        for choice in response.get("choices", []):
            reason = self._match(assistant_text(choice.get("message") or {}))
            if reason:
                return GuardrailResult.block(reason)
        return None

    async def check_stream_delta(
        self, request: ChatCompletionRequest, accumulated_text: str, delta_text: str, ctx
    ) -> GuardrailResult | None:
        if not self.check_completion:
            return None
        reason = self._match(accumulated_text)
        if reason:
            return GuardrailResult.block(reason)
        return None
