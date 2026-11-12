"""Tests for audit-log webhooks (admin actions -> external audit trail).

Every security-relevant admin operation — virtual key create/update/revoke,
circuit-breaker reset — emits a JSON record identifying *who* did it (the
admin key name and the auth chain step that authenticated them) and *what*
changed. Delivery is best-effort in a background task (the admin operation
never waits on the webhook), with one retry, counted on
``gateway_audit_events_total{event,outcome}``. The raw key of a newly
created virtual key is never part of any record.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time

import httpx
import jwt as pyjwt
import pytest
from prometheus_client.registry import REGISTRY
from sqlalchemy import update

from gateway.config import AuditConfig, AuthConfig, JWTAuthConfig
from gateway.db import get_sessionmaker
from gateway.models import VirtualKey

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config

NOW = int(time.time())
SECRET = "test-shared-secret"


class AuditSink:
    """Captures audit webhook deliveries; configurable failures."""

    def __init__(self, status: int = 200, fail_first: int = 0) -> None:
        self.payloads: list[dict] = []
        self.attempts = 0
        self.status = status
        self.fail_first = fail_first  # N initial responses with 500

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        self.payloads.append(json.loads(request.content))
        if self.attempts <= self.fail_first:
            return httpx.Response(500, json={"error": "sink down"})
        return httpx.Response(self.status, json={"ok": True})


class DeadSink:
    """A transport that cannot connect at all."""

    def __init__(self) -> None:
        self.attempts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        raise httpx.ConnectError("audit endpoint unreachable")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def audit_count(event: str, outcome: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "gateway_audit_events_total", {"event": event, "outcome": outcome}
        )
        or 0
    )


async def _wait_for(check, timeout: float = 2.0, what: str = "condition"):
    """Poll a sync or async predicate until truthy or the deadline hits
    (audit delivery runs in a background task)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def _make_audit_env(tmp_path, audit: AuditConfig, sink):
    config = make_config(audit=audit)
    client = (
        sink.client()
        if isinstance(sink, DeadSink)
        else httpx.AsyncClient(transport=httpx.MockTransport(sink.handler))
    )
    async for ns in _make_env(tmp_path, config=config, audit_client=client):
        ns.audit_sink = sink
        yield ns


# ----------------------------------------------------------------------
# Records: who did what
# ----------------------------------------------------------------------


async def test_key_lifecycle_events_and_payload_shape(tmp_path):
    audit = AuditConfig(webhook_url="https://audit.test/hook")
    async for env in _make_audit_env(tmp_path, audit, AuditSink()):
        created = await create_key(env.client, "mobile-app", rpm_limit=10)

        # update: which fields the admin touched
        patched = await env.client.patch(
            f"/admin/keys/{created['id']}",
            json={"rpm_limit": 5, "max_budget_usd": 20},
            headers=auth(ADMIN_KEY),
        )
        assert patched.status_code == 200

        revoked = await env.client.delete(
            f"/admin/keys/{created['id']}", headers=auth(ADMIN_KEY)
        )
        assert revoked.status_code == 200

        async def _all_arrived() -> bool:
            return len(env.audit_sink.payloads) >= 3

        await _wait_for(_all_arrived, what="3 audit records")

        create_rec, update_rec, revoke_rec = env.audit_sink.payloads[:3]

        # -- create
        assert create_rec["type"] == "audit"
        assert create_rec["event"] == "key.created"
        assert create_rec["actor"] == {"name": "admin", "auth_method": "admin_key"}
        assert create_rec["details"]["key_name"] == "mobile-app"
        assert create_rec["details"]["key_id"] == created["id"]
        assert "key" not in create_rec["details"]  # never the raw key
        assert "sk-gw" not in json.dumps(create_rec)
        assert "timestamp" in create_rec
        assert "mobile-app" in create_rec["message"]

        # -- update
        assert update_rec["event"] == "key.updated"
        assert update_rec["details"]["fields"] == ["max_budget_usd", "rpm_limit"]
        assert "fields" in update_rec["message"]

        # -- revoke
        assert revoke_rec["event"] == "key.revoked"
        assert revoke_rec["details"]["key_name"] == "mobile-app"
        assert revoke_rec["details"]["key_id"] == created["id"]
        break


async def test_actor_reflects_jwt_admin(tmp_path):
    """An admin acting via a JWT carries the token's key name and 'jwt'
    as the auth method — the audit trail says *how* the admin proved it."""
    config = make_config(
        audit=AuditConfig(webhook_url="https://audit.test/hook"),
        auth=AuthConfig(
            chain=["admin_key", "jwt"],
            jwt=JWTAuthConfig(secret=SECRET),
        ),
    )
    sink = AuditSink()
    async for env in _make_env(
        tmp_path,
        config=config,
        audit_client=httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)),
    ):
        env.audit_sink = sink
        # promote a virtual key to admin, then have a JWT naming it act
        bootstrapped = await create_key(env.client, "break-glass")
        async with get_sessionmaker()() as session:
            await session.execute(
                update(VirtualKey)
                .where(VirtualKey.name == "break-glass")
                .values(is_admin=True)
            )
            await session.commit()
        token = pyjwt.encode(
            {"sub": "break-glass", "exp": NOW + 600}, SECRET, algorithm="HS256"
        )
        # a JWT naming an admin virtual key performs the action
        created = (
            await env.client.post(
                "/admin/keys",
                json={"name": "downstream"},
                headers=auth(token),
            )
        ).json()
        assert created["name"] == "downstream"

        async def _arrived() -> bool:
            return any(
                p["details"].get("key_name") == "downstream" for p in sink.payloads
            )

        await _wait_for(_arrived, what="jwt admin audit record")
        rec = next(
            p for p in sink.payloads if p["details"].get("key_name") == "downstream"
        )
        assert rec["actor"] == {"name": "break-glass", "auth_method": "jwt"}
        assert rec["event"] == "key.created"
        assert rec["details"]["key_name"] == "downstream"
        assert bootstrapped["id"] != created["id"]
        break


async def test_breaker_reset_event(tmp_path):
    audit = AuditConfig(webhook_url="https://audit.test/hook")
    async for env in _make_audit_env(tmp_path, audit, AuditSink()):
        reset = await env.client.post(
            "/admin/routing/breakers/openai%2Fprimary/reset",
            headers=auth(ADMIN_KEY),
        )
        assert reset.status_code == 200

        async def _arrived() -> bool:
            return bool(env.audit_sink.payloads)

        await _wait_for(_arrived, what="breaker reset record")
        rec = env.audit_sink.payloads[0]
        assert rec["event"] == "breaker.reset"
        assert rec["details"]["deployment"] == "openai/primary"
        assert rec["actor"]["name"] == "admin"
        assert "openai/primary" in rec["message"]
        break


# ----------------------------------------------------------------------
# Failure paths emit nothing; failed deliveries never break the operation
# ----------------------------------------------------------------------


async def test_duplicate_and_not_found_emit_nothing(tmp_path):
    audit = AuditConfig(webhook_url="https://audit.test/hook")
    async for env in _make_audit_env(tmp_path, audit, AuditSink()):
        await create_key(env.client, "mobile-app")

        # 409: duplicate name — the record is only emitted after a
        # successful commit, so this path creates no delivery task.
        dupe = await env.client.post(
            "/admin/keys", json={"name": "mobile-app"}, headers=auth(ADMIN_KEY)
        )
        assert dupe.status_code == 409

        # 404: unknown key
        missing = await env.client.delete(
            "/admin/keys/does-not-exist", headers=auth(ADMIN_KEY)
        )
        assert missing.status_code == 404

        # 404: unknown deployment
        ghost = await env.client.post(
            "/admin/routing/breakers/nope/reset", headers=auth(ADMIN_KEY)
        )
        assert ghost.status_code == 404

        # ...and a later successful revoke arrives, giving any phantom
        # delivery issued before it ample time to surface.
        await env.client.delete(
            "/admin/keys/" + (await _key_id(env.client, "mobile-app")),
            headers=auth(ADMIN_KEY),
        )
        async def _revoked_recorded() -> bool:
            return any(p["event"] == "key.revoked" for p in env.audit_sink.payloads)

        await _wait_for(_revoked_recorded, what="revoke record")
        events = [p["event"] for p in env.audit_sink.payloads]
        assert events.count("key.created") == 1
        assert "already exists" not in json.dumps(env.audit_sink.payloads)
        break


async def test_failed_delivery_does_not_fail_the_admin_call(tmp_path):
    """An unreachable audit endpoint must not break key management."""
    audit = AuditConfig(webhook_url="https://audit.test/hook", max_attempts=1)
    dead = DeadSink()
    async for env in _make_audit_env(tmp_path, audit, dead):
        before = audit_count("key.created", "failed")
        created = await create_key(env.client, "mobile-app")
        assert created["name"] == "mobile-app"  # 201 + full body

        async def _counted() -> bool:
            return audit_count("key.created", "failed") > before

        await _wait_for(_counted, what="failed delivery metric")
        assert dead.attempts == 1  # tried once (max_attempts=1), never landed
        break


async def test_retry_then_success(tmp_path):
    """A transient 5xx is retried once and then counted as sent."""
    audit = AuditConfig(webhook_url="https://audit.test/hook", max_attempts=2)
    sink = AuditSink(fail_first=1)  # first POST 500s, second succeeds
    async for env in _make_audit_env(tmp_path, audit, sink):
        before = audit_count("key.created", "sent")
        await create_key(env.client, "mobile-app")

        async def _sent() -> bool:
            return audit_count("key.created", "sent") > before

        await _wait_for(_sent, what="retried delivery")
        assert sink.attempts == 2
        assert len(sink.payloads) == 2  # the record was POSTed twice
        assert sink.payloads[0] == sink.payloads[1]
        break


async def test_no_webhook_configured_counts_skipped(tmp_path):
    async for env in _make_audit_env(tmp_path, AuditConfig(), AuditSink()):
        before = audit_count("key.created", "skipped")
        created = await create_key(env.client, "mobile-app")

        async def _skipped() -> bool:
            return audit_count("key.created", "skipped") > before

        await _wait_for(_skipped, what="skipped metric")
        assert env.audit_sink.attempts == 0  # nothing was ever POSTed
        assert created["name"] == "mobile-app"
        break


async def _key_id(client, name: str) -> str:
    listing = await client.get("/admin/keys", headers=auth(ADMIN_KEY))
    return next(k["id"] for k in listing.json()["data"] if k["name"] == name)
