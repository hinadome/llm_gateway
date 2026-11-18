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

Findings persist on the row (tier 53 — the teeth): ``pii_findings`` +
``pii_scanned_at`` are written exactly once per row, by the on-demand
endpoints or the background sweep (``core/pii_sweep.py``), so a
finding is a recorded fact — re-reads report it without re-counting,
a flagged row is *quarantined* (its full-body read is masked via the
same detector set, ``mask_body`` — masked and found can never drift —
and the replay export withholds its bodies), and ``pii_scan.
purge_days`` eventually deletes the flagged bodies outright. Scans
land idempotently: the persisted write is guarded on
``pii_scanned_at IS NULL``, so a row scanned by two paths at once
still fires ``pii.found`` exactly once.

Scans are bounded computation over rows: ``GET /admin/pii-scan``
(bulk, bounded by ``pii_scan.max_rows``) and ``GET /admin/logs/{id}/
pii-scan`` (one row) — see ``api/admin.py``. Rows whose bodies were
not captured (``logging.store_bodies: false``) report ``captured:
false`` — "not stored" is a different fact from "clean", and the
report never blurs them. NB: stored bodies are truncated at
``logging.max_body_chars``, so a secret past the boundary is
invisible to the scan — raise the limit where that matters.
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

    def mask_text(self, text: str) -> str:
        """Mask every detector match in one text — the quarantine leg
        of the scan. The SAME detectors as ``scan_text`` (a kind the
        scan found is exactly a kind this masks — masked and found can
        never drift), validators respected: a Luhn-failing card number
        is neither found nor masked. Replacements are kind-tagged
        placeholders, never the matched text."""
        for kind, (pattern, validator) in self.detectors.items():
            def _sub(match: re.Match[str], _kind: str = kind, _v: Any = validator) -> str:
                if _v is not None and not _v(match.group(0)):
                    return match.group(0)  # candidate failed — not a finding
                return f"[REDACTED:{_kind}]"

            text = pattern.sub(_sub, text)
        return text

    def mask_body(self, body: Any) -> Any:
        """Mask one stored body (the JSON-tree mirror of ``scan_body``):
        string leaves are masked in place, the structure — dicts, lists,
        scalars — is preserved, so a masked read is still a readable
        body, just without the findings."""
        if isinstance(body, str):
            return self.mask_text(body)
        if isinstance(body, dict):
            return {key: self.mask_body(value) for key, value in body.items()}
        if isinstance(body, list):
            return [self.mask_body(item) for item in body]
        return body

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


def recorded_log(row: Any) -> dict:
    """The report entry for a row whose findings already landed — the
    persisted-state mirror of ``scan_log``. Same shape, same keys, so
    callers cannot tell a fresh scan from a recorded one; ``captured``
    stays a live fact (computed from the bodies the row holds *now*,
    so a purged row honestly reports ``captured: false`` alongside
    its recorded findings)."""
    findings = row.pii_findings or {}
    request = findings.get("request") or {}
    response = findings.get("response") or {}
    return {
        "log_id": row.id,
        "request_id": row.request_id,
        "key_name": row.key_name,
        "tenant_id": row.tenant_id,
        "model": row.model,
        "status": row.status,
        "captured": row.request_body is not None or row.response_body is not None,
        "clean": not findings_flagged(findings),
        "findings": {"request": request, "response": response},
    }


def findings_flagged(pii_findings: dict | None) -> bool:
    """Is this recorded findings dict a *flagged* row? The persisted
    shape is ``{"request": {...}, "response": {...}}`` — NULL (never
    scanned) and both-empty (scanned clean, including bodiless rows)
    are not flagged; any recorded count is. One helper, shared by the
    quarantine reads (admin.py) and the sweep's purge leg, so the two
    can never disagree about what quarantine means."""
    if not pii_findings:
        return False
    request = pii_findings.get("request") or {}
    response = pii_findings.get("response") or {}
    return any(request.values()) or any(response.values())
