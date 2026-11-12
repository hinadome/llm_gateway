"""Response schema guardrail.

Validates that the response being sent to the client conforms to the
OpenAI chat.completion schema — a safety net that "inspects outgoing
responses" and catches provider-translation bugs before they leak to
clients. By default invalid schemas are logged (allow); set
``on_invalid: block`` to fail closed.
"""

from __future__ import annotations

from typing import Any

from ...schemas import ChatCompletionRequest
from ..base import Guardrail, GuardrailResult

REQUIRED_TOP = ("id", "object", "created", "model", "choices")


def validate_openai_response(response: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for field in REQUIRED_TOP:
        if field not in response:
            problems.append(f"missing top-level field '{field}'")
    if response.get("object") not in (None, "chat.completion"):
        problems.append(f"unexpected object value {response.get('object')!r}")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        problems.append("choices must be a non-empty list")
        return problems
    for i, choice in enumerate(choices):
        if not isinstance(choice, dict) or "message" not in choice:
            problems.append(f"choice[{i}] missing 'message'")
            continue
        message = choice.get("message") or {}
        if message.get("role") != "assistant":
            problems.append(f"choice[{i}].message.role should be 'assistant'")
    usage = response.get("usage")
    if usage is not None and not isinstance(usage, dict):
        problems.append("usage must be an object")
    return problems


class ResponseSchemaGuardrail(Guardrail):
    name = "response_schema"
    description = "Validates outgoing responses conform to the OpenAI chat.completion schema."

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.on_invalid: str = self.params.get("on_invalid", "allow")

    async def check_response(
        self, request: ChatCompletionRequest, response: dict[str, Any], ctx
    ) -> GuardrailResult | None:
        problems = validate_openai_response(response)
        if not problems:
            return None
        if self.on_invalid == "block":
            return GuardrailResult.block("response failed schema validation: " + "; ".join(problems))
        # Log-only mode: record and pass through.
        return GuardrailResult(
            reason="response schema validation issues (log-only)",
            details={"problems": problems},
        )
