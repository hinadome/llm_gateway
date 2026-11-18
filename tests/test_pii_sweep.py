"""PII-scan sweep + quarantine + purge (tier 53) — the scan's teeth.

Until tier 53 the scan was a mirror an admin had to look into:
findings evaporated into a metric and an event at scan time, and the
flagged bodies stayed fully readable forever. The contract now:

* findings LAND on the row exactly once (a guarded write — the
  on-demand endpoints and the background sweep race, and a row
  scanned by two paths still fires ``pii.found`` once); re-reads
  report recorded findings without re-counting;
* the sweep finds what landed without an admin asking (bounded per
  pass, bodiless rows marked scanned so the walk converges, quiet
  traffic covered);
* a flagged row is QUARANTINED — the full-body read serves masked
  bodies (kind-tagged placeholders, structure preserved, validators
  respected) and the replay export withholds the bodies with a
  skip_reason;
* ``purge_days`` deletes a flagged row's stored bodies after the
  window (metadata + findings stay — the audit trail outlives the
  exposure), while ``purge_days: null`` keeps them until general
  retention.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from gateway.config import (
    EventsConfig,
    GuardrailConfig,
    LoggingConfig,
    PIIScanConfig,
)
from gateway.core.pii_scan import PIIScanner
from gateway.db import get_sessionmaker
from gateway.models import RequestLog
from gateway.observability.metrics import PII_FINDINGS, PII_PURGED

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config

SECRET = "sk-abcdefabcdefabcdef123"


def _utc(dt: datetime | None) -> datetime | None:
    """Normalize a datetime read back from SQLite (naive) to aware UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


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


async def _sweep(env, now: datetime | None = None) -> int:
    """One sweep pass at the pinned clock — production leaves the
    clock real and the loop running (the lifespan never starts under
    pytest). The scanner + config are handed over fresh, exactly the
    way the lifespan loop does it, so a reload applies from the pass."""
    return await env.app.state.pii_sweep.evaluate_once(
        env.app.state.pii_scanner, env.app.state.config.pii_scan, now=now
    )


async def _row(log_id: int) -> RequestLog:
    """Detached row (loaded attributes survive the closed session)."""
    async with get_sessionmaker()() as session:
        return await session.get(RequestLog, log_id)


async def _age_findings(log_id: int, scanned_at: datetime) -> None:
    """Time-travel a row's scan clock — the purge tests decide what
    'older than purge_days' means without waiting."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(RequestLog)
            .where(RequestLog.id == log_id)
            .values(pii_scanned_at=scanned_at)
        )
        await session.commit()


def _events(env, name: str = "pii.found") -> list:
    return [e for e in env.app.state.events.handlers[0].events if e.name == name]


# ----------------------------------------------------------------------
# The sweep: findings land, once
# ----------------------------------------------------------------------


async def test_sweep_scans_persists_and_fires(tmp_path):
    from gateway.config import EventHandlerConfig

    config = make_config(
        events=EventsConfig(
            handlers=[EventHandlerConfig(class_path="tests.test_events:Recorder")]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "swept"))["key"]
        log_id = await _chat(env, key, f"my key is {SECRET} please help")

        t0 = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
        before = PII_FINDINGS.labels(kind="api_key")._value.get()
        newly = await _sweep(env, now=t0)
        await env.app.state.events.drain()

        # The row the sweep never saw an admin look at is found anyway.
        assert newly == 1
        assert PII_FINDINGS.labels(kind="api_key")._value.get() == before + 1
        found = _events(env)
        assert len(found) == 1
        assert found[0].payload["kinds"] == {"api_key": 1}
        assert found[0].payload["key_name"] == "swept"
        assert found[0].payload["log_id"] == log_id
        # The audit never quotes what it found.
        assert SECRET not in str(found[0].payload)

        # Findings landed on the row, at the pinned clock.
        row = await _row(log_id)
        assert row.pii_findings == {"request": {"api_key": 1}, "response": {}}
        assert row.pii_flagged is True
        assert _utc(row.pii_scanned_at) == t0


async def test_sweep_idempotent_no_recount(tmp_path):
    from gateway.config import EventHandlerConfig

    config = make_config(
        events=EventsConfig(
            handlers=[EventHandlerConfig(class_path="tests.test_events:Recorder")]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "once"))["key"]
        await _chat(env, key, f"leak {SECRET}")

        t0 = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
        assert await _sweep(env, now=t0) == 1
        await env.app.state.events.drain()
        metric_after_first = PII_FINDINGS.labels(kind="api_key")._value.get()

        # Second pass: nothing unscanned, no re-count, no re-emit.
        assert await _sweep(env, now=t0 + timedelta(hours=1)) == 0
        await env.app.state.events.drain()
        assert PII_FINDINGS.labels(kind="api_key")._value.get() == metric_after_first
        assert len(_events(env)) == 1


async def test_sweep_respects_max_rows(tmp_path):
    config = make_config(pii_scan=PIIScanConfig(sweep_max_rows=1))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "paced"))["key"]
        first = await _chat(env, key, f"leak {SECRET}")
        await _chat(env, key, f"leak again {SECRET}")

        t0 = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
        # One row per pass, oldest first.
        assert await _sweep(env, now=t0) == 1
        assert await _sweep(env, now=t0 + timedelta(minutes=1)) == 1
        # Converged: the third pass has nothing left.
        assert await _sweep(env, now=t0 + timedelta(minutes=2)) == 0
        assert (await _row(first)).pii_flagged is True


async def test_sweep_marks_bodiless_rows_scanned(tmp_path):
    # Rows with nothing stored must not starve the walk: the sweep
    # records "nothing visible" once, so the pass converges instead of
    # revisiting them forever.
    config = make_config(logging=LoggingConfig(store_bodies=False))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "unstored"))["key"]
        log_id = await _chat(env, key, "Hello")

        t0 = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
        assert await _sweep(env, now=t0) == 0  # clean, not flagged

        row = await _row(log_id)
        assert _utc(row.pii_scanned_at) == t0
        assert row.pii_flagged is False
        assert row.pii_findings == {"request": {}, "response": {}}

        # The walk converged — and the recorded state reports the
        # honest fact (not stored, not "safe").
        assert await _sweep(env, now=t0 + timedelta(minutes=1)) == 0
        scanned = (
            await env.client.get(
                f"/admin/logs/{log_id}/pii-scan", headers=auth(ADMIN_KEY)
            )
        ).json()
        assert scanned["captured"] is False
        assert scanned["clean"] is True


async def test_sweep_disabled_noop(tmp_path):
    config = make_config(pii_scan=PIIScanConfig(enabled=False))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "off"))["key"]
        log_id = await _chat(env, key, f"leak {SECRET}")

        assert await _sweep(env, now=datetime.now(timezone.utc)) == 0
        row = await _row(log_id)
        assert row.pii_scanned_at is None  # nothing landed
        assert row.pii_flagged is None


# ----------------------------------------------------------------------
# Landing: on-demand scans land the same state, exactly once
# ----------------------------------------------------------------------


async def test_on_demand_scan_lands_findings_once(tmp_path):
    from gateway.config import EventHandlerConfig

    config = make_config(
        events=EventsConfig(
            handlers=[EventHandlerConfig(class_path="tests.test_events:Recorder")]
        )
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "asked"))["key"]
        log_id = await _chat(env, key, f"leak {SECRET}")

        before = PII_FINDINGS.labels(kind="api_key")._value.get()
        first = (
            await env.client.get(
                f"/admin/logs/{log_id}/pii-scan", headers=auth(ADMIN_KEY)
            )
        ).json()
        await env.app.state.events.drain()
        assert first["findings"]["request"] == {"api_key": 1}
        assert PII_FINDINGS.labels(kind="api_key")._value.get() == before + 1
        assert (await _row(log_id)).pii_flagged is True

        # Re-scan: recorded findings, same answer, no re-count, no
        # re-emit — the event fired exactly once for this row.
        second = (
            await env.client.get(
                f"/admin/logs/{log_id}/pii-scan", headers=auth(ADMIN_KEY)
            )
        ).json()
        assert second["findings"] == first["findings"]
        assert second["clean"] == first["clean"] is False
        assert PII_FINDINGS.labels(kind="api_key")._value.get() == before + 1
        assert len(_events(env)) == 1

        # The sweep agrees: already scanned, nothing new to fire.
        assert await _sweep(env, now=datetime.now(timezone.utc)) == 0
        assert len(_events(env)) == 1


# ----------------------------------------------------------------------
# Quarantine: masked on read, withheld from export
# ----------------------------------------------------------------------


async def test_quarantine_masks_request_on_read(tmp_path):
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "leaky"))["key"]
        log_id = await _chat(env, key, f"my key is {SECRET} please help")
        await env.client.get(f"/admin/logs/{log_id}/pii-scan", headers=auth(ADMIN_KEY))

        detail = (
            await env.client.get(f"/admin/logs/{log_id}", headers=auth(ADMIN_KEY))
        ).json()
        assert detail["pii_quarantined"] is True
        content = detail["request_body"]["messages"][0]["content"]
        # The secret is gone; the placeholder carries the kind.
        assert SECRET not in detail["request_body"]["messages"][0]["content"]
        assert "[REDACTED:api_key]" in content
        # Structure preserved — a masked read is still a readable body.
        assert detail["request_body"]["model"]
        assert isinstance(detail["request_body"]["messages"], list)
        assert detail["response_body"]["choices"]  # clean body unmangled
        # Nothing anywhere in the response quotes the finding.
        assert SECRET not in str(detail)


async def test_quarantine_masks_response_side(tmp_path):
    # Redaction off: the *response* holds the PII, and the masked read
    # covers it too — quarantine is not request-only.
    config = make_config(
        guardrails=[GuardrailConfig(name="prompt_injection", enabled=True,
                                    params={"threshold": 3})]
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "plain"))["key"]
        log_id = await _chat(env, key, "PIIPLEASE")
        await env.client.get(f"/admin/logs/{log_id}/pii-scan", headers=auth(ADMIN_KEY))

        detail = (
            await env.client.get(f"/admin/logs/{log_id}", headers=auth(ADMIN_KEY))
        ).json()
        assert detail["pii_quarantined"] is True
        content = detail["response_body"]["choices"][0]["message"]["content"]
        assert "bot@example.com" not in content
        assert "[REDACTED:email]" in content


async def test_quarantine_opt_out(tmp_path):
    config = make_config(pii_scan=PIIScanConfig(quarantine=False))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "raw"))["key"]
        log_id = await _chat(env, key, f"leak {SECRET}")
        await env.client.get(f"/admin/logs/{log_id}/pii-scan", headers=auth(ADMIN_KEY))

        # Opted out: the flagged row still serves raw bodies (the
        # findings stay visible either way).
        detail = (
            await env.client.get(f"/admin/logs/{log_id}", headers=auth(ADMIN_KEY))
        ).json()
        assert SECRET in detail["request_body"]["messages"][0]["content"]
        assert "pii_quarantined" not in detail
        assert detail["pii_findings"]["request"] == {"api_key": 1}


async def test_scan_disabled_disables_the_teeth(tmp_path):
    # enabled: false gates the whole apparatus: the sweep is a no-op,
    # and even a row flagged before the feature was turned off serves
    # raw bodies and exports normally.
    config = make_config(pii_scan=PIIScanConfig(enabled=False))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "off"))["key"]
        log_id = await _chat(env, key, f"leak {SECRET}")
        # A pre-flagged row (flagged while enabled, disabled since).
        await _age_findings(log_id, datetime.now(timezone.utc))
        async with get_sessionmaker()() as session:
            await session.execute(
                update(RequestLog)
                .where(RequestLog.id == log_id)
                .values(
                    pii_findings={"request": {"api_key": 1}, "response": {}},
                    pii_flagged=True,
                )
            )
            await session.commit()

        detail = (
            await env.client.get(f"/admin/logs/{log_id}", headers=auth(ADMIN_KEY))
        ).json()
        assert SECRET in detail["request_body"]["messages"][0]["content"]
        assert "pii_quarantined" not in detail

        export = (
            await env.client.get("/admin/logs/export", headers=auth(ADMIN_KEY))
        ).text
        assert SECRET in export


async def test_export_withholds_flagged_bodies(tmp_path):
    async for env in _make_env(tmp_path):
        leaky = (await create_key(env.client, "leaky"))["key"]
        careful = (await create_key(env.client, "careful"))["key"]
        bad = await _chat(env, leaky, f"leak {SECRET}")
        await _chat(env, careful, "Hello")
        await env.client.get("/admin/pii-scan", headers=auth(ADMIN_KEY))

        lines = [
            json.loads(line)
            for line in (await env.client.get(
                "/admin/logs/export", headers=auth(ADMIN_KEY)
            )).text.splitlines()
            if line.strip()
        ]
        flagged = next(line for line in lines if line["id"] == bad)
        clean = next(line for line in lines if line["key_name"] == "careful")

        # The flagged row appears in history — but its bodies never
        # leave in a capture, and the line says why.
        assert "request_body" not in flagged
        assert "pii quarantine" in flagged["skip_reason"]
        assert flagged["comparable"] is False
        assert flagged["pii_findings"]["request"] == {"api_key": 1}
        # The clean row exports normally.
        assert clean["request_body"]["messages"]
        assert clean["skip_reason"] is None


# ----------------------------------------------------------------------
# The purge leg
# ----------------------------------------------------------------------


async def test_purge_after_purge_days(tmp_path):
    config = make_config(pii_scan=PIIScanConfig(purge_days=30))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "purged"))["key"]
        log_id = await _chat(env, key, f"leak {SECRET}")

        t0 = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
        assert await _sweep(env, now=t0) == 1
        row = await _row(log_id)
        assert row.request_body is not None  # inside the window: kept

        # Time-travel the findings 31 days old; the next pass purges.
        await _age_findings(log_id, t0 - timedelta(days=31))
        before = PII_PURGED._value.get()
        assert await _sweep(env, now=t0) == 0  # no NEW findings
        assert PII_PURGED._value.get() == before + 1

        row = await _row(log_id)
        assert row.request_body is None
        assert row.response_body is None
        # The audit trail outlives the exposure.
        assert row.pii_findings == {"request": {"api_key": 1}, "response": {}}
        assert row.pii_flagged is True
        assert _utc(row.pii_purged_at) == t0

        # Idempotent: a purged row left the candidate set.
        assert await _sweep(env, now=t0 + timedelta(hours=1)) == 0
        assert PII_PURGED._value.get() == before + 1

        # The honest read: bodies gone, findings kept, still
        # quarantined (there is nothing left to mask, but the flag
        # says what this row was).
        detail = (
            await env.client.get(f"/admin/logs/{log_id}", headers=auth(ADMIN_KEY))
        ).json()
        assert detail["request_body"] is None
        assert detail["response_body"] is None
        assert detail["pii_quarantined"] is True
        scanned = (
            await env.client.get(
                f"/admin/logs/{log_id}/pii-scan", headers=auth(ADMIN_KEY)
            )
        ).json()
        assert scanned["captured"] is False  # bodies gone now
        assert scanned["findings"]["request"] == {"api_key": 1}


async def test_purge_days_none_keeps_bodies(tmp_path):
    # The default: quarantine contains, retention decides when to
    # delete — flagged bodies stay until general log retention.
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "kept"))["key"]
        log_id = await _chat(env, key, f"leak {SECRET}")

        t0 = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
        assert await _sweep(env, now=t0) == 1
        await _age_findings(log_id, t0 - timedelta(days=3650))
        before = PII_PURGED._value.get()
        assert await _sweep(env, now=t0) == 0
        assert PII_PURGED._value.get() == before

        row = await _row(log_id)
        assert row.request_body is not None
        assert row.pii_purged_at is None


async def test_mask_respects_validators_and_excludes(tmp_path):
    # The masker is the scanner's own detector set: a Luhn-failing
    # card is neither found nor masked, and an excluded kind is
    # neither found nor masked.
    scanner = PIIScanner(PIIScanConfig())
    text = "card 4111111111111111 bad 4111111111111112"
    masked = scanner.mask_text(text)
    assert "[REDACTED:credit_card]" in masked
    assert "4111111111111112" in masked  # failed Luhn — untouched
    body = scanner.mask_body({"messages": [{"content": "mail bot@example.com"}], "n": 2})
    assert body["n"] == 2
    assert body["messages"][0]["content"] == "mail [REDACTED:email]"

    config = make_config(pii_scan=PIIScanConfig(exclude_kinds=["email"]))
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "partial"))["key"]
        log_id = await _chat(env, key, f"key {SECRET} mail bot@example.com")
        scanned = (
            await env.client.get(
                f"/admin/logs/{log_id}/pii-scan", headers=auth(ADMIN_KEY)
            )
        ).json()
        assert scanned["findings"]["request"] == {"api_key": 1}  # email excluded

        detail = (
            await env.client.get(f"/admin/logs/{log_id}", headers=auth(ADMIN_KEY))
        ).json()
        content = detail["request_body"]["messages"][0]["content"]
        assert "[REDACTED:api_key]" in content
        assert "bot@example.com" in content  # excluded kind stays raw
