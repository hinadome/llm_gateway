"""Prompt-injection detection guardrail (heuristic, weighted patterns).

Each pattern carries a weight; the summed score of all matches is compared
against ``threshold`` (default 3). At or above the threshold the request is
blocked; below it the match is recorded for observability but allowed.
"""

from __future__ import annotations

import re
from typing import Any

from ...schemas import ChatCompletionRequest
from ...utils import content_to_text
from ..base import Guardrail, GuardrailAction, GuardrailResult

_PATTERNS: list[tuple[str, int]] = [
    # strong indicators
    (r"ignore\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|preceding|earlier|past)?\s*(?:instructions?|prompts?|directions?|rules?|context)", 3),
    (r"disregard\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|preceding|earlier)?\s*(?:instructions?|prompts?|directions?|rules?|guidelines?)", 3),
    (r"forget\s+(?:everything|all|your\s+(?:previous|prior)\s+(?:instructions?|training))", 3),
    (r"(?:reveal|show|print|repeat|output|expose)\s+(?:your|the)\s+(?:system|initial|original)\s+(?:prompt|message|instructions?)", 3),
    (r"override\s+(?:your\s+)?(?:system|developer|safety)\s+(?:instructions?|prompts?|settings?)", 3),
    (r"\b(?:jailbreak|DAN\s+mode|developer\s+mode\s+enabled)\b", 3),
    # medium indicators
    (r"you\s+are\s+now\s+(?:DAN|an?\s+unrestricted|unfiltered|uncensored)\b", 2),
    (r"pretend\s+(?:you\s+are|to\s+be)\s+(?:an?\s+)?(?:unrestricted|unfiltered|uncensored|evil)\b", 2),
    (r"(?:from\s+now\s+on|new\s+instructions?)\s*[:,]\s*(?:you\s+(?:are|must|will|can))", 2),
    (r"bypass\s+(?:all\s+|any\s+|your\s+)?(?:restrictions?|filters?|guardrails?|content\s+policy)", 2),
    (r"respond\s+outside\s+(?:of\s+)?(?:your|the)\s+(?:guidelines|boundaries|policy)", 2),
    # weak indicators
    (r"\bsystem\s+prompt\b", 1),
    (r"\bignore\s+the\s+rules\b", 1),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), w) for p, w in _PATTERNS]


class PromptInjectionGuardrail(Guardrail):
    name = "prompt_injection"
    description = "Blocks requests matching weighted prompt-injection / jailbreak heuristics."

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.threshold: int = int(self.params.get("threshold", 3))

    def _score(self, text: str) -> tuple[int, list[str]]:
        score = 0
        matched: list[str] = []
        for pattern, weight in _COMPILED:
            if pattern.search(text):
                score += weight
                matched.append(pattern.pattern)
        return score, matched

    async def check_request(
        self, request: ChatCompletionRequest, ctx
    ) -> GuardrailResult | None:
        text = "\n".join(
            content_to_text(m.content) for m in request.messages
        )
        if not text:
            return None
        score, matched = self._score(text)
        if score == 0:
            return None
        if score >= self.threshold:
            return GuardrailResult.block(
                f"possible prompt injection (score {score} >= threshold {self.threshold})",
                score=score,
                matched_patterns=matched,
            )
        # Below threshold: allow but record for observability.
        return GuardrailResult(
            action=GuardrailAction.ALLOW,
            reason=f"possible prompt injection below threshold (score {score})",
            details={"score": score, "matched_patterns": matched},
        )
