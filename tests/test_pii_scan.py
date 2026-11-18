"""PII/secret scan over stored request bodies.

The contract: the log keeps the *original* body as received (the
guardrail's masks protect the provider, not the log) — the scan is
the audit that finds what landed in storage. Detectors mirror the
pii_redaction guardrail plus jwt/private-key shapes; findings are
kind + count only (never the matched text — the audit must not
become its own leak); clean and bodies-not-captured are separate
facts; tenant admins are scoped like /admin/logs; the bulk scan is a
pure read that counts rows-with-kind on
gateway_pii_scan_findings_total and emits pii.found events.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gateway.config import (
    GatewayConfig,
    GuardrailConfig,
    LoggingConfig,
    PIIScanConfig,
)
from gateway.observability.metrics import PII_FINDINGS

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config


async def _chat(env, key: str, content: str) -> int:
    resp = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(content=content),
        headers=auth(key),
    )
    assert resp.status_code == 200, resp.text
    logs = await env.client.get(
        "/admin/logs", params={"limit": 1}, headers=auth(ADMIN_KEY)
    )
    return logs.json()["data"][0]["id"]


async def test_scan_sees_original_body_not_the_mask(env):
    # A client pastes an API key into the prompt. The redaction
    # guardrail masks it for the provider — but the log keeps the
    # original as received, so the scan finds it. Redaction protects
    # the upstream; this audit reports what storage holds.
    key = (await create_key(env.client, "paster"))["key"]
    secret = "sk-abcdefabcdefabcdef123"
    log_id = await _chat(env, key, f"my key is {secret} please help")

    # The provider only ever saw the mask.
    upstream = env.mock.requests[-1]["body"]["messages"][0]["content"]
    assert secret not in upstream

    scanned = await env.client.get(
        f"/admin/logs/{log_id}/pii-scan", headers=auth(ADMIN_KEY)
    )
    assert scanned.status_code == 200
    body = scanned.json()
    assert body["clean"] is False
    assert body["captured"] is True
    assert body["findings"]["request"]["api_key"] == 1
    assert body["findings"]["response"] == {}
    # Kind + count only — the secret itself never comes back.
    assert secret not in scanned.text


async def test_scan_finds_response_pii_when_redaction_is_off(tmp_path):
    # The audit also catches "the guardrail wasn't on for this
    # traffic": without pii_redaction, an email in the *response*
    # lands in the log unmasked.
    config = make_config(
        guardrails=[GuardrailConfig(name="prompt_injection", enabled=True,
                                    params={"threshold": 3})]
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "plain"))["key"]
        log_id = await _chat(env, key, "PIIPLEASE")
        scanned = await env.client.get(
            f"/admin/logs/{log_id}/pii-scan", headers=auth(ADMIN_KEY)
        )
        body = scanned.json()
        assert body["findings"]["response"].get("email") == 1
        assert body["findings"]["request"] == {}


async def test_scan_clean_row(tmp_path):
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "clean"))["key"]
        log_id = await _chat(env, key, "Hello")
        scanned = await env.client.get(
            f"/admin/logs/{log_id}/pii-scan", headers=auth(ADMIN_KEY)
        )
        assert scanned.json()["clean"] is True
        assert scanned.json()["captured"] is True


async def test_scan_missing_bodies(tmp_path):
    # store_bodies=false rows scan clean too — but report
    # captured=False so the report never claims "safe" for rows it
    # could not see (nothing stored ≠ nothing sensitive).
    async for env in _make_env(
        tmp_path, config=make_config(logging=LoggingConfig(store_bodies=False))
    ):
        key = (await create_key(env.client, "unstored"))["key"]
        await _chat(env, key, "my key is sk-abcdefabcdefabcdef123")
        bulk = (
            await env.client.get("/admin/pii-scan", headers=auth(ADMIN_KEY))
        ).json()
        assert bulk["scanned"] == 1
        assert bulk["bodies_not_captured"] == 1
        assert bulk["with_findings"] == 0  # nothing visible to find


async def test_scan_bulk_aggregates_and_filters(tmp_path):
    async for env in _make_env(tmp_path):
        leaky = (await create_key(env.client, "leaky"))["key"]
        careful = (await create_key(env.client, "careful"))["key"]
        await _chat(env, leaky, "key: sk-abcdefabcdefabcdef123, card 4111111111111111")
        await _chat(env, leaky, "jwt: eyJhbGciOiJIabcdwxyz.eyJzdWIiOiIxMjM0NTY3ODkwqwerty.hmac_sig_here_12345")
        await _chat(env, careful, "Hello")

        bulk = await env.client.get("/admin/pii-scan", headers=auth(ADMIN_KEY))
        assert bulk.status_code == 200
        body = bulk.json()
        assert body["scanned"] == 3
        assert body["with_findings"] == 2
        assert body["bodies_not_captured"] == 0
        assert body["by_kind"]["api_key"] == 1
        assert body["by_kind"]["credit_card"] == 1
        assert body["by_kind"]["jwt"] == 1
        assert [r["key_name"] for r in body["rows"]] == ["leaky", "leaky"]
        assert all("sk-abcdef" not in str(r) for r in body["rows"])

        # Filters work like /admin/logs.
        only_careful = (
            await env.client.get(
                "/admin/pii-scan", params={"key_name": "careful"},
                headers=auth(ADMIN_KEY),
            )
        ).json()
        assert only_careful["scanned"] == 1
        assert only_careful["with_findings"] == 0


async def test_scan_tenant_scoping(tmp_path):
    from .test_tenants import _create_tenant

    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "acme")
        beta = await _create_tenant(env.client, "beta")
        acme_key = (await create_key(env.client, "acme-key", tenant_id=acme["id"]))["key"]
        beta_key = (await create_key(env.client, "beta-key", tenant_id=beta["id"]))["key"]
        acme_admin = (
            await create_key(env.client, "acme-admin", is_admin=True, tenant_id=acme["id"])
        )["key"]

        acme_log = await _chat(env, acme_key, "secret sk-abcdefabcdefabcdef123")
        await _chat(env, beta_key, "secret sk-ghijklghijklghijkl456")

        # Tenant admin: bulk scan forced to own rows...
        scoped = (
            await env.client.get("/admin/pii-scan", headers=auth(acme_admin))
        ).json()
        assert scoped["scanned"] == 1
        assert scoped["with_findings"] == 1
        # ...and the same 404 wall on foreign single rows as /admin/logs.
        foreign = await env.client.get(
            f"/admin/logs/{acme_log + 1}/pii-scan", headers=auth(acme_admin)
        )
        beta_scoped = await env.client.get(
            f"/admin/logs/{acme_log + 1}/pii-scan", headers=auth(beta_key)
        )
        assert foreign.status_code == 404
        assert beta_scoped.status_code in (401, 403)
        # Their own row scans fine.
        own = await env.client.get(
            f"/admin/logs/{acme_log}/pii-scan", headers=auth(acme_admin)
        )
        assert own.status_code == 200
        assert own.json()["findings"]["request"]["api_key"] == 1
        # Platform admin sees every tenant.
        all_rows = (
            await env.client.get("/admin/pii-scan", headers=auth(ADMIN_KEY))
        ).json()
        assert all_rows["scanned"] == 2


async def test_scan_exclude_and_extra_patterns(tmp_path):
    config = make_config(
        pii_scan=PIIScanConfig(
            exclude_kinds=["email", "ipv4"],
            extra_patterns={"employee_id": r"EMP-\d{6}"},
        )
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "custom"))["key"]
        log_id = await _chat(
            env,
            key,
            "contact bot@example.com about EMP-123456 and IP 10.0.0.1",
        )
        scanned = await env.client.get(
            f"/admin/logs/{log_id}/pii-scan", headers=auth(ADMIN_KEY)
        )
        body = scanned.json()
        assert body["findings"]["request"] == {"employee_id": 1}
        assert "email" not in body["findings"]["request"]
        assert "ipv4" not in body["findings"]["request"]
        # The kinds echo reflects the active detector set.
        bulk = (
            await env.client.get("/admin/pii-scan", headers=auth(ADMIN_KEY))
        ).json()
        assert "employee_id" in bulk["kinds"]
        assert "email" not in bulk["kinds"]


async def test_scan_metric_and_event(tmp_path):
    from .test_events import Recorder
    from gateway.config import EventsConfig, EventHandlerConfig

    config = make_config(
        events=EventsConfig(
            handlers=[EventHandlerConfig(class_path="tests.test_events:Recorder")]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        recorder = env.app.state.events.handlers[0]
        key = (await create_key(env.client, "metered"))["key"]
        await _chat(env, key, "secret sk-abcdefabcdefabcdef123 and sk-zzzzzzzzzzzzzzzzzzz123")

        before = PII_FINDINGS.labels(kind="api_key")._value.get()
        await env.client.get("/admin/pii-scan", headers=auth(ADMIN_KEY))
        await env.app.state.events.drain()
        # One row containing api_key = one finding-row count (not two
        # occurrences).
        assert PII_FINDINGS.labels(kind="api_key")._value.get() == before + 1

        found = [e for e in recorder.events if e.name == "pii.found"]
        assert len(found) == 1
        assert found[0].payload["kinds"] == {"api_key": 2}
        assert found[0].payload["key_name"] == "metered"
        # The event payload carries kinds, not the matched text.
        assert "sk-abcdef" not in str(found[0].payload)


async def test_scan_disabled_404(tmp_path):
    async for env in _make_env(
        tmp_path, config=make_config(pii_scan=PIIScanConfig(enabled=False))
    ):
        key = (await create_key(env.client, "off"))["key"]
        log_id = await _chat(env, key, "Hello")
        assert (
            await env.client.get("/admin/pii-scan", headers=auth(ADMIN_KEY))
        ).status_code == 404
        assert (
            await env.client.get(
                f"/admin/logs/{log_id}/pii-scan", headers=auth(ADMIN_KEY)
            )
        ).status_code == 404


async def test_bad_extra_pattern_rejected():
    with pytest.raises(ValidationError):
        GatewayConfig(pii_scan=PIIScanConfig(extra_patterns={"broken": "("}))
