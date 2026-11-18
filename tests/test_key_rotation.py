"""Key rotation & grace windows (tier 51).

``POST /admin/keys/{id}/rotate`` replaces a key's *credential* while the
row keeps its identity — name, tenant, budgets, spend, limits and logs
all stay with the row, whichever credential was presented. The previous
credential keeps authenticating as the same key until ``grace_until``
(``grace_seconds`` at rotation time; 0 = immediate cutover), then dies
with an explicit "rotated" error instead of a generic 401. The row's own
checks fire first: revocation and expiry refuse BOTH credentials. A
second rotation supersedes the previous credential outright, and
PATCH ``grace_until`` is the incident knob — end the old credential's
window early (explicit null) or re-arm it. The new raw key is returned
exactly once, like creation, and never reaches any audit record.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from gateway.config import AuditConfig
from gateway.db import get_sessionmaker
from gateway.models import VirtualKey

from .conftest import ADMIN_KEY, auth, chat_body, create_key
from .test_audit_webhooks import AuditSink, _make_audit_env, _wait_for


async def _rotate(client, key_id: str, *, as_: str = ADMIN_KEY, **body) -> dict:
    resp = await client.post(
        f"/admin/keys/{key_id}/rotate", json=body, headers=auth(as_)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _chat(client, raw_key: str, content: str = "hi") -> int:
    resp = await client.post(
        "/v1/chat/completions", json=chat_body(content=content), headers=auth(raw_key)
    )
    return resp.status_code


async def _set_grace(key_id: str, **values) -> None:
    """Direct row UPDATE — the house pattern for time-traveling a
    column without waiting out a real window."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(update(VirtualKey).where(VirtualKey.id == key_id).values(**values))
        await session.commit()


async def test_rotate_issues_new_credential_old_survives_grace(env):
    created = await create_key(env.client, "rotate-me")
    old_raw = created["key"]

    rotated = await _rotate(env.client, created["id"], grace_seconds=3600)
    new_raw = rotated["key"]
    assert new_raw.startswith("sk-gw-")
    assert new_raw != old_raw
    assert rotated["key_prefix"] == new_raw[:12]
    # the dashboard shows which credential is dying — never its hash
    assert rotated["previous_key_prefix"] == created["key_prefix"]
    assert "key_hash" not in rotated
    assert "previous_key_hash" not in rotated
    grace_until = datetime.fromisoformat(rotated["grace_until"])
    # SQLite hands stored datetimes back naive; the codebase normalizes
    # the same way (_is_expired) before comparing against the clock.
    if grace_until.tzinfo is None:
        grace_until = grace_until.replace(tzinfo=timezone.utc)
    assert grace_until > datetime.now(timezone.utc)

    # inside the window, BOTH credentials are the same key
    assert await _chat(env.client, old_raw, "via old") == 200
    assert await _chat(env.client, new_raw, "via new") == 200


async def test_old_credential_dies_after_grace(env):
    created = await create_key(env.client, "grace-ends")
    rotated = await _rotate(env.client, created["id"], grace_seconds=3600)

    await _set_grace(
        created["id"], grace_until=datetime.now(timezone.utc) - timedelta(seconds=1)
    )

    resp = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers=auth(created["key"]),
    )
    assert resp.status_code == 401
    assert "rotated" in resp.json()["error"]["message"]
    # the current credential never noticed
    assert await _chat(env.client, rotated["key"]) == 200


async def test_immediate_cutover_no_grace(env):
    created = await create_key(env.client, "cutover")
    rotated = await _rotate(env.client, created["id"], grace_seconds=0)

    assert rotated["grace_until"] is None  # no window, by choice

    resp = await env.client.post(
        "/v1/chat/completions", json=chat_body(), headers=auth(created["key"])
    )
    assert resp.status_code == 401
    # the dead credential still gets the honest answer, not unknown-401
    assert "rotated" in resp.json()["error"]["message"]
    assert await _chat(env.client, rotated["key"]) == 200


async def test_rotation_preserves_identity(env):
    created = await create_key(env.client, "same-row", rpm_limit=10)
    assert await _chat(env.client, created["key"], "before rotation") == 200

    rotated = await _rotate(env.client, created["id"], grace_seconds=3600)
    assert await _chat(env.client, rotated["key"], "after rotation") == 200

    # one row, same id, same policy — the credential changed, nothing else
    listing = (await env.client.get("/admin/keys", headers=auth(ADMIN_KEY))).json()[
        "data"
    ]
    rows = [k for k in listing if k["name"] == "same-row"]
    assert len(rows) == 1
    assert rows[0]["id"] == created["id"]
    assert rows[0]["rpm_limit"] == 10

    # logs from BOTH credentials land on the same key name
    async def _both_logged():
        r = await env.client.get(
            "/admin/logs", params={"key_name": "same-row"}, headers=auth(ADMIN_KEY)
        )
        return r.json()["total"] >= 2

    await _wait_for(_both_logged, what="both phases' log rows")
    r = await env.client.get(
        "/admin/logs", params={"key_name": "same-row"}, headers=auth(ADMIN_KEY)
    )
    assert len(r.json()["data"]) == 2

    # and spend accrues to the row, not to a credential
    async def _spend_accrued():
        r = await env.client.get(
            f"/admin/keys/{created['id']}", headers=auth(ADMIN_KEY)
        )
        return r.json()["spend_usd"] > 0

    await _wait_for(_spend_accrued, what="spend accrued on the row")


async def test_revoke_kills_both_credentials(env):
    created = await create_key(env.client, "revoked-mid-grace")
    rotated = await _rotate(env.client, created["id"], grace_seconds=3600)

    resp = await env.client.delete(
        f"/admin/keys/{created['id']}", headers=auth(ADMIN_KEY)
    )
    assert resp.json()["revoked"] is True

    # the row's checks fire before grace decides: revocation is the
    # kill switch for every credential the row ever had
    assert await _chat(env.client, created["key"]) == 401
    assert await _chat(env.client, rotated["key"]) == 401


async def test_second_rotation_supersedes_previous(env):
    created = await create_key(env.client, "rotate-twice")
    first = created["key"]
    second = (await _rotate(env.client, created["id"], grace_seconds=3600))["key"]
    third = (await _rotate(env.client, created["id"], grace_seconds=3600))["key"]

    # only ONE previous credential is remembered: the first is gone
    # the instant the second rotation lands, mid-window or not
    assert await _chat(env.client, first) == 401
    assert await _chat(env.client, second) == 200
    assert await _chat(env.client, third) == 200


async def test_tenant_admin_rotate_scoping(env):
    acme = (
        await env.client.post(
            "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
        )
    ).json()
    beta = (
        await env.client.post(
            "/admin/tenants", json={"name": "beta"}, headers=auth(ADMIN_KEY)
        )
    ).json()
    acme_admin = await create_key(
        env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
    )
    acme_key = await create_key(env.client, "acme-member", tenant_id=acme["id"])
    beta_key = await create_key(env.client, "beta-member", tenant_id=beta["id"])

    # own tenant: the tenant admin rotates fine
    rotated = await _rotate(
        env.client, acme_key["id"], as_=acme_admin["key"], grace_seconds=60
    )
    assert await _chat(env.client, rotated["key"]) == 200

    # foreign tenant: a 404 byte-identical to the unknown-key case
    resp = await env.client.post(
        f"/admin/keys/{beta_key['id']}/rotate",
        json={"grace_seconds": 60},
        headers=auth(acme_admin["key"]),
    )
    assert resp.status_code == 404

    # platform admin sees it all
    assert (
        await _rotate(env.client, acme_key["id"], grace_seconds=0)
    )["key"].startswith("sk-gw-")


async def test_rotate_revoked_key_refused(env):
    created = await create_key(env.client, "already-revoked")
    await env.client.delete(f"/admin/keys/{created['id']}", headers=auth(ADMIN_KEY))

    resp = await env.client.post(
        f"/admin/keys/{created['id']}/rotate",
        json={"grace_seconds": 3600},
        headers=auth(ADMIN_KEY),
    )
    assert resp.status_code == 400
    assert "revoked" in resp.json()["error"]["message"]


async def test_negative_grace_seconds_rejected(env):
    created = await create_key(env.client, "no-negative")

    resp = await env.client.post(
        f"/admin/keys/{created['id']}/rotate",
        json={"grace_seconds": -5},
        headers=auth(ADMIN_KEY),
    )
    assert resp.status_code == 400  # the app's 400 envelope, not a bare 422
    assert "grace_seconds" in resp.json()["error"]["message"]


async def test_patch_grace_until_early_kill_and_rearm(env):
    created = await create_key(env.client, "grace-knob")
    rotated = await _rotate(env.client, created["id"], grace_seconds=3600)
    old_raw = created["key"]
    assert await _chat(env.client, old_raw) == 200

    # explicit null: the incident knob — the old credential dies NOW
    resp = await env.client.patch(
        f"/admin/keys/{created['id']}",
        json={"grace_until": None},
        headers=auth(ADMIN_KEY),
    )
    assert resp.status_code == 200
    assert resp.json()["grace_until"] is None
    assert await _chat(env.client, old_raw) == 401

    # a future timestamp re-arms the window (the row still remembers the
    # previous credential's hash; only the clock decided against it)
    resp = await env.client.patch(
        f"/admin/keys/{created['id']}",
        json={"grace_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
        headers=auth(ADMIN_KEY),
    )
    assert resp.status_code == 200
    assert await _chat(env.client, old_raw) == 200
    assert await _chat(env.client, rotated["key"]) == 200

    # and a past timestamp ends it again
    await env.client.patch(
        f"/admin/keys/{created['id']}",
        json={"grace_until": "2020-01-01T00:00:00Z"},
        headers=auth(ADMIN_KEY),
    )
    assert await _chat(env.client, old_raw) == 401


async def test_rotation_does_not_resurrect_expired_key(env):
    created = await create_key(
        env.client, "still-expired", expires_at="2020-01-01T00:00:00Z"
    )
    rotated = await _rotate(env.client, created["id"], grace_seconds=3600)

    # rotation changes the credential, never the deadline: both
    # credentials of an expired row are expired
    for raw in (created["key"], rotated["key"]):
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(raw)
        )
        assert resp.status_code == 401
        assert "expired" in resp.json()["error"]["message"]


async def test_rotation_audit_record(tmp_path):
    audit = AuditConfig(webhook_url="https://audit.test/hook")
    async for env in _make_audit_env(tmp_path, audit, AuditSink()):
        created = await create_key(env.client, "audit-rotate")
        rotated = await _rotate(env.client, created["id"], grace_seconds=3600)

        async def _rotated_delivered():
            return any(
                p["event"] == "key.rotated" for p in env.audit_sink.payloads
            )

        await _wait_for(_rotated_delivered, what="key.rotated audit record")
        rec = next(
            p for p in env.audit_sink.payloads if p["event"] == "key.rotated"
        )

        assert rec["type"] == "audit"
        assert rec["actor"] == {"name": "admin", "auth_method": "admin_key"}
        assert rec["details"]["key_name"] == "audit-rotate"
        assert rec["details"]["key_id"] == created["id"]
        assert rec["details"]["grace_seconds"] == 3600
        assert "rotated" in rec["message"]

        # never either raw key — old or new — in any record
        blob = json.dumps(rec)
        assert created["key"] not in blob
        assert rotated["key"] not in blob
