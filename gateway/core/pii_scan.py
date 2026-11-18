"""PII/secret scan over stored request/response bodies.

The audit that answers "did secrets or PII land in the request log?"
Clients paste API keys into prompts; guardrails may be off, in
observe mode, or only masking the *upstream* copy — the log keeps
the original as received (``ctx.request_body`` is captured before
the guardrail phase), so stored rows are where exposure actually
shows. Redaction protects the provider; this tells you what your
retention policy is holding.

Detectors mirror the ``pii_redaction`` guardrail — the same compiled
patterns, imported from it, so "masked" and "found" can never drift
— plus secret shapes a redactor wouldn't bother masking (JWTs,
private-key blocks), all Luhn-validated credit cards. Custom
detectors arrive as ``pii_scan.extra_patterns`` (name → regex) in
config; built-ins drop out via ``exclude_kinds``.

Findings are kind + count only — never the matched text. The audit
must not become its own leak: a report that quotes the API key it
found is a report full of API keys.

Scans are pure computation over rows (no writes, idempotent), so
the endpoints are GETs: ``GET /admin/pii-scan`` (bulk, bounded by
``pii_scan.max_rows``) and ``GET /admin/logs/{id}/pii-scan`` (one
row) — see ``api/admin.py``. Rows whose bodies were not captured
(``logging.store_bodies: false``) report ``captured: false`` —
"not stored" is a different fact from "clean", and the report never
blurs them. NB: stored bodies are truncated at ``logging.
max_body_chars``, so a secret past the boundary is invisible to the
scan — raise the limit where that matters.
"""

from __future__ import annotations

import json
import re
from typing import Any

# The guardrail's own compiled patterns — same-package reuse so the
# audit's idea of "PII" is exactly the redactor's.
from ..guardrails.builtins.pii_redaction import (
    _API_KEY,
    _CREDIT_CARD,
    _EMAIL,
    _IPV4,
    _PHONE,
    _SSN,
    _luhn_ok,
)

# Secret shapes a redaction guardrail doesn't mask but that must
# never sit in stored bodies.
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

# kind -> (pattern, optional candidate validator; None passes all).
# Order is presentation order: secrets first, personal data after.
BUILTIN_DETECTORS: dict[str, tuple[re.Pattern[str], Any]] = {
    "api_key": (_API_KEY, None),
    "private_key": (_PRIVATE_KEY, None),
    "jwt": (_JWT, None),
    "email": (_EMAIL, None),
    "phone": (_PHONE, None),
    "ssn": (_SSN, None),
    "credit_card": (_CREDIT_CARD, _luhn_ok),
    "ipv4": (_IPV4, None),
}


class PIIScanner:
    """Pure scanner: builtins (minus excludes) + configured extras."""

    def __init__(self, config: Any) -> None:
        self.config = config
        detectors: dict[str, tuple[re.Pattern[str], Any]] = {
            kind: spec
            for kind, spec in BUILTIN_DETECTORS.items()
            if kind not in config.exclude_kinds
        }
        for name, pattern in config.extra_patterns.items():
            # Validated at config load (PIIScanConfig._patterns_compile);
            # compiled here once per scanner build.
            detectors[name] = (re.compile(pattern), None)
        self.detectors = detectors

    def scan_text(self, text: str) -> dict[str, int]:
        """kind -> match count over one text. Candidates failing a
        detector's validator (Luhn, say) don't count."""
        found: dict[str, int] = {}
        for kind, (pattern, validator) in self.detectors.items():
            count = sum(
                1
                for match in pattern.finditer(text)
                if validator is None or validator(match.group(0))
            )
            if count:
                found[kind] = count
        return found

    def scan_body(self, body: Any) -> dict[str, int]:
        """Scan one stored body (a JSON column value — dict, list or
        scalar; None means the body was not stored)."""
        if body is None:
            return {}
        if not isinstance(body, str):
            try:
                body = json.dumps(body, ensure_ascii=False)
            except (TypeError, ValueError):  # non-JSON column value
                body = str(body)
        return self.scan_text(body)

    def scan_log(self, row: Any) -> dict:
        """One request-log row -> the report entry (kind + counts
        only, never matched text). ``clean`` and ``captured`` are
        separate facts: bodies-not-stored is a policy answer, not a
        safety one."""
        request = self.scan_body(row.request_body)
        response = self.scan_body(row.response_body)
        captured = row.request_body is not None or row.response_body is not None
        merged = {
            kind: request.get(kind, 0) + response.get(kind, 0)
            for kind in set(request) | set(response)
        }
        return {
            "log_id": row.id,
            "request_id": row.request_id,
            "key_name": row.key_name,
            "tenant_id": row.tenant_id,
            "model": row.model,
            "status": row.status,
            "captured": captured,
            "clean": not merged,
            "findings": {"request": request, "response": response},
        }
