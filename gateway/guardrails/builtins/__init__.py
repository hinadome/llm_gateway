"""Built-in guardrail implementations.

These are intentionally self-contained (regex/heuristic based, no external
services) so the gateway works out of the box. For production-grade content
safety, plug in a custom Guardrail that calls a moderation API — the
framework supports it without any core changes.
"""

from __future__ import annotations

from typing import Any

from .keyword_blocklist import KeywordBlocklistGuardrail
from .output_moderation import OutputModerationGuardrail
from .pii_redaction import PIIRedactionGuardrail
from .presidio_pii import PresidioPIIGuardrail
from .prompt_injection import PromptInjectionGuardrail
from .request_limits import RequestLimitsGuardrail
from .response_schema import ResponseSchemaGuardrail

BUILTIN_GUARDRAILS: dict[str, type] = {
    PIIRedactionGuardrail.name: PIIRedactionGuardrail,
    PresidioPIIGuardrail.name: PresidioPIIGuardrail,
    PromptInjectionGuardrail.name: PromptInjectionGuardrail,
    KeywordBlocklistGuardrail.name: KeywordBlocklistGuardrail,
    OutputModerationGuardrail.name: OutputModerationGuardrail,
    ResponseSchemaGuardrail.name: ResponseSchemaGuardrail,
    RequestLimitsGuardrail.name: RequestLimitsGuardrail,
}

__all__ = [
    "BUILTIN_GUARDRAILS",
    "KeywordBlocklistGuardrail",
    "OutputModerationGuardrail",
    "PIIRedactionGuardrail",
    "PromptInjectionGuardrail",
    "RequestLimitsGuardrail",
    "ResponseSchemaGuardrail",
]
