"""The ``gateway`` CLI: the admin API from a terminal.

The contract under test — ``gateway/cli.py`` is a *thin HTTP client*
of the JSON admin API, so these tests drive the async core
(``cli.run``) against the same in-process app as every other test,
through the fixture's ``httpx.AsyncClient``. What is asserted:

* **Output conventions** — tables for lists, key–value dumps for
  single objects, ``--json`` printing the *exact* API payload, raw
  credentials printed exactly once, exports to stdout or ``--out``.
* **Honest marshaling** — absent flags are absent body fields (a
  patch touches only what the operator named), and the boolean
  tri-state fields get dedicated flags (``--cache-bypass`` sends an
  explicit false, ``--cache-inherit`` an explicit null).
* **Error rendering** — the API's own error-envelope message verbatim
  as ``gateway: <message>`` on stderr, exit 1; a 401 without a
  credential appends the ``--key``/``GATEWAY_ADMIN_KEY`` hint.
* **Scoping passes through** — a tenant admin gets the same 403 the
  API would give the dashboard, a missing id the same 404.

Nothing here writes the repo's ``config.yaml``: the reload test
points ``settings.config_path`` at a YAML under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import io
import json

import httpx
import pytest
from cryptography.fernet import Fernet

from gateway import cli

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

MASTER = Fernet.generate_key().decode()


class FakeTTY(io.StringIO):
    """stdin that claims to be a terminal (for the vault TTY refusal)."""

    def isatty(self) -> bool:  # pragma: no cover - trivial
        return True


async def _recording_client(env):
    """An httpx client that records every request body it forwards to
    the in-process app — the tool for asserting the CLI marshals
    flags into exactly the body the API should see."""
    asgi = httpx.ASGITransport(app=env.app)
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return await asgi.handle_async_request(request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://gateway.test"
    )
    return client, seen


def _body_of(request: httpx.Request) -> dict:
    return json.loads(request.content)


async def _chat_once(env, key_name="app") -> str:
    """One chat through a fresh key; returns the raw key used."""
    row = await create_key(env.client, key_name)
    resp = await env.client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers={"Authorization": f"Bearer {row['key']}"},
    )
    assert resp.status_code == 200
    return row["key"]


# ----------------------------------------------------------------------
# status / stats
# ----------------------------------------------------------------------


async def test_status_prints_identity_and_readiness(tmp_path, capsys):
    """Plain status: whoami as key–value lines, then a one-line health
    summary. ASGITransport skips the lifespan, so the app is draining:
    the NOT-ready line must say WHY (draining: yes, db: yes)."""
    async for env in _make_env(tmp_path):
        code = await cli.run(["status"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "name: admin" in out
        assert "auth_method: admin_key" in out
        assert "gateway: 1.0.0" in out
        assert "NOT ready (draining: yes, db: yes)" in out
        break


async def test_status_json_is_the_exact_payloads(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["status", "--json"], client=env.client, key=ADMIN_KEY)
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["whoami"]["is_admin"] is True
        assert payload["whoami"]["platform_admin"] is True
        assert payload["health"]["version"] == "1.0.0"
        assert payload["readyz"]["ready"] is False
        assert payload["readyz"]["draining"] is True
        break


async def test_stats_after_traffic(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        await _chat_once(env)
        code = await cli.run(["stats"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "Requests (24h): 1" in out
        assert "test-model" in out
        assert "Spend: $" in out
        break


# ----------------------------------------------------------------------
# keys
# ----------------------------------------------------------------------


async def test_keys_create_prints_raw_key_once(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["keys", "create", "svc"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "Created key 'svc'" in out
        raw = [l for l in out.splitlines() if "sk-gw-" in l]
        assert len(raw) == 1  # the credential, exactly once
        assert "shown ONCE" in out
        break


async def test_keys_create_json_admin_and_flags(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(
            ["keys", "create", "svc", "--admin", "--rpm", "120", "--json"],
            client=env.client,
            key=ADMIN_KEY,
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["is_admin"] is True
        assert payload["rpm_limit"] == 120
        assert payload["key"].startswith("sk-gw-")
        assert payload["name"] == "svc"
        break


async def test_keys_create_absent_flags_are_absent_fields(tmp_path, capsys):
    """The marshaling rule with teeth: no --admin must mean the body
    carries NO is_admin field at all (a plain store_true would have
    sent an explicit false — harmless for is_admin, the same bug on a
    nullable-tri-state field would silently rewrite policy)."""
    async for env in _make_env(tmp_path):
        client, seen = await _recording_client(env)
        try:
            code = await cli.run(["keys", "create", "svc"], client=client, key=ADMIN_KEY)
            assert code == 0
            body = _body_of(seen[0])
            assert body == {"name": "svc"}
        finally:
            await client.aclose()
        break


async def test_keys_edit_cache_tri_state_bodies(tmp_path, capsys):
    """--cache-bypass sends an explicit false; --cache-inherit an
    explicit null (reset to the global policy); neither flag sends
    nothing at all."""
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "svc")
        kid = row["id"]
        client, seen = await _recording_client(env)
        try:
            await cli.run(
                ["keys", "edit", kid, "--cache-bypass"], client=client, key=ADMIN_KEY
            )
            capsys.readouterr()
            assert _body_of(seen[0]) == {"cache_enabled": False}
            await cli.run(
                ["keys", "edit", kid, "--cache-inherit"], client=client, key=ADMIN_KEY
            )
            capsys.readouterr()
            assert _body_of(seen[1]) == {"cache_enabled": None}
        finally:
            await client.aclose()
        # and the server-side effect is visible through the API
        code = await cli.run(
            ["keys", "edit", kid, "--cache-bypass", "--json"],
            client=env.client,
            key=ADMIN_KEY,
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["cache_enabled"] is False
        code = await cli.run(
            ["keys", "edit", kid, "--cache-inherit", "--json"],
            client=env.client,
            key=ADMIN_KEY,
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["cache_enabled"] is None
        break


async def test_keys_list_and_show(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "svc", rpm_limit=42)
        code = await cli.run(["keys", "list"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "Name" in out and "svc" in out
        assert "(1 key(s))" in out
        code = await cli.run(["keys", "show", row["id"]], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "name: svc" in out
        assert "rpm_limit: 42" in out
        break


async def test_keys_edit_rpm_then_show(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "svc")
        code = await cli.run(
            ["keys", "edit", row["id"], "--rpm", "99"],
            client=env.client,
            key=ADMIN_KEY,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Updated key 'svc'" in out
        code = await cli.run(
            ["keys", "show", row["id"]], client=env.client, key=ADMIN_KEY
        )
        assert "rpm_limit: 99" in capsys.readouterr().out
        break


async def test_keys_edit_without_flags_is_an_error(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "svc")
        code = await cli.run(
            ["keys", "edit", row["id"]], client=env.client, key=ADMIN_KEY
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "nothing to change" in err
        break


async def test_keys_rotate_prints_new_credential_once(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "svc")
        code = await cli.run(
            ["keys", "rotate", row["id"], "--grace-seconds", "60"],
            client=env.client,
            key=ADMIN_KEY,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Rotated key 'svc'" in out
        assert "60s" in out
        raw = [l for l in out.splitlines() if "sk-gw-" in l]
        assert len(raw) == 1
        assert raw[0].strip() != row["key"]  # a NEW credential
        break


async def test_keys_revoke_deactivates(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "svc")
        code = await cli.run(
            ["keys", "revoke", row["id"]], client=env.client, key=ADMIN_KEY
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Revoked key 'svc'" in out
        code = await cli.run(
            ["keys", "show", row["id"], "--json"], client=env.client, key=ADMIN_KEY
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["is_active"] is False
        break


async def test_keys_create_duplicate_name_conflict(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        await create_key(env.client, "svc")
        code = await cli.run(["keys", "create", "svc"], client=env.client, key=ADMIN_KEY)
        err = capsys.readouterr().err
        assert code == 1
        assert "already exists" in err
        assert err.startswith("gateway: ")
        break


async def test_keys_edit_unknown_id_not_found(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(
            ["keys", "edit", "nope", "--rpm", "5"], client=env.client, key=ADMIN_KEY
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "Key 'nope' not found." in err
        break


async def test_keys_edit_null_words_clear_expiry(tmp_path, capsys):
    """--expires null (and friends) marshal to a JSON null — the only
    way a flag can mean 'clear the field'."""
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "svc")
        client, seen = await _recording_client(env)
        try:
            await cli.run(
                ["keys", "edit", row["id"], "--expires", "null"],
                client=client,
                key=ADMIN_KEY,
            )
            assert _body_of(seen[0]) == {"expires_at": None}
        finally:
            await client.aclose()
        break


async def test_keys_edit_json_obj_flags(tmp_path, capsys):
    """--prompt-vars / --model-mappings parse JSON object strings;
    garbage is a clean CLI error, not a stack trace."""
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "svc")
        client, seen = await _recording_client(env)
        try:
            await cli.run(
                ["keys", "edit", row["id"], "--model-mappings", '{"gpt": "test-model"}'],
                client=client,
                key=ADMIN_KEY,
            )
            capsys.readouterr()
            assert _body_of(seen[0]) == {"model_mappings": {"gpt": "test-model"}}
            code = await cli.run(
                ["keys", "edit", row["id"], "--prompt-vars", "not-json{"],
                client=client,
                key=ADMIN_KEY,
            )
            assert code == 1
            assert "invalid JSON" in capsys.readouterr().err
            code = await cli.run(
                ["keys", "edit", row["id"], "--prompt-vars", "[1, 2]"],
                client=client,
                key=ADMIN_KEY,
            )
            assert code == 1
            assert "expected a JSON object" in capsys.readouterr().err
        finally:
            await client.aclose()
        break


# ----------------------------------------------------------------------
# tenants
# ----------------------------------------------------------------------


async def test_tenants_crud_roundtrip(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(
            ["tenants", "create", "acme", "--display", "Acme Co", "--markup", "20", "--json"],
            client=env.client,
            key=ADMIN_KEY,
        )
        tenant = json.loads(capsys.readouterr().out)
        assert code == 0
        tid = tenant["id"]
        assert tenant["name"] == "acme"

        code = await cli.run(["tenants", "list"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "acme" in out and "Acme Co" in out
        assert "(1 tenant(s))" in out

        code = await cli.run(
            ["tenants", "edit", tid, "--rpm", "99"], client=env.client, key=ADMIN_KEY
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Updated tenant 'acme'" in out

        code = await cli.run(
            ["tenants", "delete", tid], client=env.client, key=ADMIN_KEY
        )
        out = capsys.readouterr().out
        assert code == 0
        assert f"Deleted tenant '{tid}'" in out
        break


async def test_tenants_are_identified_by_id_not_name(tmp_path, capsys):
    """The API routes tenants by id; the CLI passes the operator's
    token through, so a name where an id belongs is the API's own
    404 — honest, not translated."""
    async for env in _make_env(tmp_path):
        await env.client.post("/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY))
        code = await cli.run(
            ["tenants", "edit", "acme", "--rpm", "5"], client=env.client, key=ADMIN_KEY
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "Tenant 'acme' not found." in err
        break


async def test_tenants_delete_refused_while_keys_remain(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        resp = await env.client.post(
            "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
        )
        tid = resp.json()["id"]
        await create_key(env.client, "member", tenant_id=tid)
        code = await cli.run(
            ["tenants", "delete", tid], client=env.client, key=ADMIN_KEY
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "still has 1 key(s)" in err
        break


# ----------------------------------------------------------------------
# BYOK vault
# ----------------------------------------------------------------------


async def test_vault_roundtrip(tmp_path, capsys):
    async for env in _make_env(tmp_path, vault_master_key=MASTER):
        resp = await env.client.post(
            "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
        )
        tid = resp.json()["id"]

        code = await cli.run(["vault", "status"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "enabled: yes" in out

        code = await cli.run(
            ["vault", "put", tid, "openai", "--value", "sk-secret-12345"],
            client=env.client,
            key=ADMIN_KEY,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Stored openai credential" in out
        assert "key_hint: …2345" in out
        assert "sk-secret-12345" not in out  # the value itself never echoes

        code = await cli.run(["vault", "list", tid], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "openai" in out
        assert "…2345" in out

        code = await cli.run(
            ["vault", "delete", tid, "openai"], client=env.client, key=ADMIN_KEY
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Deleted openai credential" in out
        break


async def test_vault_put_reads_stdin_when_not_a_tty(tmp_path, capsys, monkeypatch):
    async for env in _make_env(tmp_path, vault_master_key=MASTER):
        resp = await env.client.post(
            "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
        )
        tid = resp.json()["id"]
        monkeypatch.setattr("sys.stdin", io.StringIO("sk-stdin-98765\n"))
        code = await cli.run(
            ["vault", "put", tid, "openai"], client=env.client, key=ADMIN_KEY
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Stored openai credential" in out
        assert "…8765" in out
        break


async def test_vault_put_refuses_interactive_tty_stdin(tmp_path, capsys, monkeypatch):
    """A secret typed at an interactive prompt lands in shell history
    and scrollback — the CLI refuses, and says what to do instead."""
    async for env in _make_env(tmp_path, vault_master_key=MASTER):
        monkeypatch.setattr("sys.stdin", FakeTTY("sk-typed-secret\n"))
        code = await cli.run(
            ["vault", "put", "acme", "openai"], client=env.client, key=ADMIN_KEY
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "refusing to read a secret" in err
        assert "--value" in err
        break


async def test_vault_put_empty_value_is_an_error(tmp_path, capsys, monkeypatch):
    async for env in _make_env(tmp_path, vault_master_key=MASTER):
        monkeypatch.setattr("sys.stdin", io.StringIO("   \n"))
        code = await cli.run(
            ["vault", "put", "acme", "openai"], client=env.client, key=ADMIN_KEY
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "empty provider key" in err
        break


# ----------------------------------------------------------------------
# prompt library
# ----------------------------------------------------------------------


async def test_prompts_lifecycle(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(
            ["prompts", "create", "greeter", "--body", "You are terse.", "--notes", "v1"],
            client=env.client,
            key=ADMIN_KEY,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Version 1 of 'greeter' created and activated" in out

        code = await cli.run(["prompts", "list"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "greeter" in out
        assert " 1 " in out  # versions: 1, active: 1

        code = await cli.run(["prompts", "show", "greeter"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "v1" in out  # the notes column
        assert "yes" in out  # active

        # a second version, from a file, does NOT auto-activate
        body_file = tmp_path / "v2.txt"
        body_file.write_text("You are verbose.")
        code = await cli.run(
            ["prompts", "create", "greeter", "--body-file", str(body_file), "--json"],
            client=env.client,
            key=ADMIN_KEY,
        )
        v2 = json.loads(capsys.readouterr().out)
        assert code == 0
        assert v2["version"] == 2
        assert v2["is_active"] is False

        # mark it candidate, then promote it to the active version
        code = await cli.run(
            ["prompts", "edit", str(v2["id"]), "--candidate", "true", "--ab-fraction", "0.5"],
            client=env.client,
            key=ADMIN_KEY,
        )
        assert "Updated version 2" in capsys.readouterr().out
        code = await cli.run(
            ["prompts", "edit", str(v2["id"]), "--activate"],
            client=env.client,
            key=ADMIN_KEY,
        )
        assert "Updated version 2" in capsys.readouterr().out

        code = await cli.run(["prompts", "list", "--json"], client=env.client, key=ADMIN_KEY)
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        row = payload["data"][0]
        assert row["active"]["version"] == 2
        break


async def test_prompts_delete_active_version_refused(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        await env.client.post(
            "/admin/prompts",
            json={"name": "greeter", "body": "One."},
            headers=auth(ADMIN_KEY),
        )
        row = (
            await env.client.get("/admin/prompts/greeter", headers=auth(ADMIN_KEY))
        ).json()
        active_id = row["data"][0]["id"]
        code = await cli.run(
            ["prompts", "delete", str(active_id)], client=env.client, key=ADMIN_KEY
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "active" in err
        break


# ----------------------------------------------------------------------
# logs, capture/replay, feedback
# ----------------------------------------------------------------------


async def test_logs_list_show_and_export(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        await _chat_once(env)
        code = await cli.run(["logs", "list"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "app" in out and "test-model" in out
        assert "(1 row(s) total)" in out

        code = await cli.run(["logs", "show", "1"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "request_id: req_" in out
        assert "request_body:" in out

        cap_file = tmp_path / "cap.jsonl"
        code = await cli.run(
            ["logs", "export", "--out", str(cap_file)], client=env.client, key=ADMIN_KEY
        )
        out = capsys.readouterr().out
        assert code == 0
        assert f"wrote {cap_file}" in out
        lines = [l for l in cap_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["model"] == "test-model"

        # stdout is the pipe-friendly default
        code = await cli.run(["logs", "export"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "test-model" in out
        break


async def test_logs_status_filter_and_json(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        await _chat_once(env)
        code = await cli.run(
            ["logs", "list", "--status", "success", "--limit", "5", "--json"],
            client=env.client,
            key=ADMIN_KEY,
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["total"] == 1
        assert payload["data"][0]["status"] == "success"
        break


async def test_replay_capture_roundtrip(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        await _chat_once(env)
        cap_file = tmp_path / "cap.jsonl"
        await cli.run(
            ["logs", "export", "--out", str(cap_file)], client=env.client, key=ADMIN_KEY
        )
        capsys.readouterr()
        code = await cli.run(
            ["replay", str(cap_file)], client=env.client, key=ADMIN_KEY
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "replayed 1 · changed 0 · unchanged 1" in out
        assert "nothing would change" in out
        break


async def test_replay_missing_file_and_garbage(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["replay", str(tmp_path / "nope.jsonl")], client=env.client, key=ADMIN_KEY)
        assert code == 1
        assert "capture file not found" in capsys.readouterr().err

        bad = tmp_path / "bad.jsonl"
        bad.write_text("this is not json\n")
        code = await cli.run(["replay", str(bad)], client=env.client, key=ADMIN_KEY)
        assert code == 1
        assert "not JSON" in capsys.readouterr().err

        empty = tmp_path / "empty.jsonl"
        empty.write_text("\n\n")
        code = await cli.run(["replay", str(empty)], client=env.client, key=ADMIN_KEY)
        assert code == 1
        assert "no capture lines" in capsys.readouterr().err
        break


async def test_feedback_list_and_stats(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        row = await create_key(env.client, "app")
        hdrs = {"Authorization": f"Bearer {row['key']}"}
        resp = await env.client.post("/v1/chat/completions", json=chat_body(), headers=hdrs)
        rid = resp.headers["x-request-id"]
        posted = await env.client.post(
            "/v1/feedback",
            json={"request_id": rid, "rating": 5, "comment": "great"},
            headers=hdrs,
        )
        assert posted.status_code == 200

        code = await cli.run(["feedback", "list"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "great" in out
        assert "(1 row(s) total)" in out

        code = await cli.run(["feedback", "stats"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "1 total" in out
        assert "average 5.0" in out
        assert "100.0% positive" in out
        assert "[model] test-model" in out
        break


# ----------------------------------------------------------------------
# fleet state
# ----------------------------------------------------------------------


async def test_routing_table_and_breaker_reset(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["routing"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "openai/primary" in out and "openai/fallback" in out
        assert "closed" in out

        code = await cli.run(
            ["breakers", "reset", "openai/primary"], client=env.client, key=ADMIN_KEY
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "force-closed" in out

        code = await cli.run(
            ["breakers", "reset", "nope/none"], client=env.client, key=ADMIN_KEY
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "does not exist" in err
        break


async def test_guardrails_inventory(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["guardrails"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        for name in ("pii_redaction", "prompt_injection", "output_moderation", "response_schema"):
            assert name in out
        assert "enforce" in out
        break


async def test_canaries_list_then_probe(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["canaries", "list"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "disabled" in out  # canary is off in the default config
        assert "openai/primary" in out

        code = await cli.run(["canaries", "probe"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "probed 3 deployment(s), 3 healthy." in out
        break


async def test_runaway_list_and_release_unknown(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["runaway", "list"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "runaway protection: on" in out
        assert "no key is jailed" in out

        code = await cli.run(
            ["runaway", "release", "no-such-key"], client=env.client, key=ADMIN_KEY
        )
        assert code == 1
        break


async def test_concurrency_view(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["concurrency"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "admission: on" in out
        assert "no key or tenant is holding slots" in out
        break


async def test_mcp_view(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["mcp"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "mcp proxy: off" in out
        assert "no MCP servers configured." in out
        break


async def test_slos_json_payload(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["slos", "--json"], client=env.client, key=ADMIN_KEY)
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert "data" in payload
        assert "config" in payload
        break


async def test_pii_scan_bulk_and_one_row(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        await _chat_once(env)
        code = await cli.run(["pii-scan"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "scanned 1 row(s)" in out
        assert "no findings" in out

        code = await cli.run(["pii-scan", "--id", "1"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "clean: yes" in out
        break


# ----------------------------------------------------------------------
# billing & finops
# ----------------------------------------------------------------------


async def test_invoices_json_and_csv(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        await _chat_once(env)
        code = await cli.run(["invoices", "--json"], client=env.client, key=ADMIN_KEY)
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert "month" in payload
        assert "totals" in payload
        assert payload["totals"]["requests"] == 1

        csv_file = tmp_path / "invoice.csv"
        code = await cli.run(
            ["invoices", "--csv", "--out", str(csv_file)],
            client=env.client,
            key=ADMIN_KEY,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert f"wrote {csv_file}" in out
        assert "model" in csv_file.read_text()
        break


async def test_anomalies_quiet(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["anomalies"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "no flags" in out
        break


async def test_forecasts_shape(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["forecasts"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "days elapsed" in out
        assert "totals:" in out
        assert "run rate:" in out
        break


async def test_usage_export_csv_and_json(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        await _chat_once(env)
        code = await cli.run(["usage-export"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert out.startswith("request_id,created_at")

        code = await cli.run(
            ["usage-export", "--format", "json"], client=env.client, key=ADMIN_KEY
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["object"] == "usage_export"
        rows = payload["requests"]
        assert len(rows) == 1
        assert rows[0]["model"] == "test-model"

        out_file = tmp_path / "usage.csv"
        code = await cli.run(
            ["usage-export", "--out", str(out_file)], client=env.client, key=ADMIN_KEY
        )
        assert code == 0
        assert f"wrote {out_file}" in capsys.readouterr().out
        assert "request_id" in out_file.read_text()
        break


# ----------------------------------------------------------------------
# migration evidence + trials
# ----------------------------------------------------------------------


async def test_migration_reads_json_keys(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["shadow-diff", "--json"], client=env.client, key=ADMIN_KEY)
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert "groups" in payload

        code = await cli.run(["verdicts", "--json"], client=env.client, key=ADMIN_KEY)
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert "data" in payload

        code = await cli.run(["watch", "--json"], client=env.client, key=ADMIN_KEY)
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert "data" in payload
        # every watch row carries its nested recommendation
        assert all("recommendation" in r for r in payload["data"])
        break


async def test_pairs_needs_model_and_deployment(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["pairs"], client=env.client, key=ADMIN_KEY)
        err = capsys.readouterr().err
        assert code == 1
        assert "--model and --deployment" in err
        break


async def test_pairs_mirror_empty_table(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(
            ["pairs", "--model", "test-model", "--deployment", "openai/fallback"],
            client=env.client,
            key=ADMIN_KEY,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "(0 pair(s) total)" in out
        break


async def test_trials_lifecycle_with_pairs(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        await _chat_once(env)
        code = await cli.run(
            [
                "trials", "create",
                "--model", "test-model",
                "--deployment", "openai/fallback",
                "--json",
            ],
            client=env.client,
            key=ADMIN_KEY,
        )
        trial = json.loads(capsys.readouterr().out)
        assert code == 0
        assert trial["status"] in ("pending", "running")

        for _ in range(100):  # no artificial delay: finishes in ~50ms
            page = (
                await env.client.get("/admin/trials", headers=auth(ADMIN_KEY))
            ).json()
            if page["data"][0]["status"] not in ("pending", "running"):
                break
            await asyncio.sleep(0.05)
        tid = trial["id"]
        assert page["data"][0]["status"] == "completed"

        code = await cli.run(["trials", "list"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "(1 trial(s) total)" in out

        code = await cli.run(["trials", "show", tid], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "recommendation:" in out
        assert "insufficient_evidence" in out

        code = await cli.run(
            ["pairs", "--trial-id", tid], client=env.client, key=ADMIN_KEY
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "exact" in out
        assert "(1 pair(s) total)" in out

        # a finished trial cannot be cancelled — the API's 409, verbatim
        code = await cli.run(
            ["trials", "cancel", tid], client=env.client, key=ADMIN_KEY
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "already finished" in err
        break


async def test_trials_cancel_mid_run_wins(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        await _chat_once(env)
        env.mock.delay_seconds = 0.2
        code = await cli.run(
            [
                "trials", "create",
                "--model", "test-model",
                "--deployment", "openai/fallback",
                "--json",
            ],
            client=env.client,
            key=ADMIN_KEY,
        )
        trial = json.loads(capsys.readouterr().out)
        assert code == 0
        code = await cli.run(
            ["trials", "cancel", trial["id"]], client=env.client, key=ADMIN_KEY
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "cancelled trial" in out
        for _ in range(100):
            page = (
                await env.client.get("/admin/trials", headers=auth(ADMIN_KEY))
            ).json()
            if page["data"][0]["status"] not in ("pending", "running"):
                break
            await asyncio.sleep(0.05)
        assert page["data"][0]["stop_reason"] == "cancelled"
        break


# ----------------------------------------------------------------------
# operations: reload + backups
# ----------------------------------------------------------------------


async def test_reload_from_tmp_config(tmp_path, capsys):
    """Hot-reload reads settings.config_path — pointed at a YAML under
    tmp_path here, NEVER the repo's ./config.yaml."""
    async for env in _make_env(tmp_path):
        config_file = tmp_path / "reload-config.yaml"
        config_file.write_text(
            """
providers:
  openai:
    api_base: http://mockupstream.test/v1
    api_key: provider-key
models:
  - name: test-model
    deployments:
      - name: openai/primary
        provider: openai
        model: model-a
        priority: 10
      - name: openai/fallback
        provider: openai
        model: model-b
        priority: 20
guardrails:
  - name: keyword_blocklist
    enabled: true
    params:
      blocked_terms: [forbidden-word]
"""
        )
        env.app.state.settings.config_path = str(config_file)
        code = await cli.run(["reload"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "reloaded from" in out
        assert str(config_file) in out
        assert "1 model(s), 1 guardrail(s)." in out
        break


async def test_reload_bad_path_is_the_api_error(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        env.app.state.settings.config_path = str(tmp_path / "missing.yaml")
        code = await cli.run(["reload"], client=env.client, key=ADMIN_KEY)
        err = capsys.readouterr().err
        assert code == 1
        assert "Configuration reload failed" in err
        break


async def test_backups_roundtrip(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        env.app.state.settings.backup_dir = str(tmp_path / "backups")
        code = await cli.run(["backups", "list"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "Backup" in out

        code = await cli.run(["backups", "create"], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert "created backup" in out

        code = await cli.run(["backups", "list", "--json"], client=env.client, key=ADMIN_KEY)
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert len(payload["data"]) == 1
        name = payload["data"][0]["name"]

        code = await cli.run(["backups", "delete", name], client=env.client, key=ADMIN_KEY)
        out = capsys.readouterr().out
        assert code == 0
        assert f"deleted backup {name}" in out

        code = await cli.run(
            ["backups", "delete", "no-such.db"], client=env.client, key=ADMIN_KEY
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "does not exist" in err
        break


# ----------------------------------------------------------------------
# auth: scoping, hints, transport failures
# ----------------------------------------------------------------------


async def test_tenant_admin_sees_the_api_scoping(tmp_path, capsys):
    """A tenant admin gets the platform endpoints' own 403 — the CLI
    adds no second policy of its own."""
    async for env in _make_env(tmp_path):
        resp = await env.client.post(
            "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
        )
        tid = resp.json()["id"]
        row = await create_key(env.client, "tenant-admin", tenant_id=tid, is_admin=True)

        code = await cli.run(
            ["tenants", "list"], client=env.client, key=row["key"]
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "Platform administrator access required" in err

        # whoami still works for them, and names their tenant
        code = await cli.run(["status"], client=env.client, key=row["key"])
        out = capsys.readouterr().out
        assert code == 0
        assert "platform_admin: no" in out
        assert tid in out  # the tenant line carries the id
        break


async def test_missing_credential_hint(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(["keys", "list"], client=env.client, key="")
        err = capsys.readouterr().err
        assert code == 1
        assert "(no credential: pass --key or set GATEWAY_ADMIN_KEY)" in err
        break


async def test_unreachable_gateway(tmp_path, capsys):
    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(refuse), base_url="http://gateway.test"
    )
    try:
        code = await cli.run(["status"], client=client, key="sk-x")
        err = capsys.readouterr().err
        assert code == 1
        assert "unreachable" in err
    finally:
        await client.aclose()


async def test_bogus_key_is_the_api_401(tmp_path, capsys):
    async for env in _make_env(tmp_path):
        code = await cli.run(
            ["keys", "list"], client=env.client, key="sk-gw-not-a-real-key"
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "gateway:" in err
        # a wrong credential gets the API's message, NOT the no-credential hint
        assert "no credential" not in err
        break


# ----------------------------------------------------------------------
# parser + entry points
# ----------------------------------------------------------------------


def test_env_credentials_and_flag_precedence(monkeypatch):
    """GATEWAY_URL/GATEWAY_ADMIN_KEY are the defaults; --url/--key
    override them; run()'s key parameter overrides the parsed flag."""
    monkeypatch.setenv("GATEWAY_URL", "http://from-env:8000")
    monkeypatch.setenv("GATEWAY_ADMIN_KEY", "sk-env")
    parser = cli.build_parser()
    args = parser.parse_args(["status"])
    assert args.url == "http://from-env:8000"
    assert args.key == "sk-env"
    args = parser.parse_args(["status", "--key", "sk-flag", "--url", "http://flag:1"])
    assert args.key == "sk-flag"
    assert args.url == "http://flag:1"


def test_help_and_bad_group_exit_codes(capsys):
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help"])
    assert exc.value.code == 0
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["nope"])
    assert exc.value.code == 2


def test_cache_flags_are_mutually_exclusive(capsys):
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["keys", "edit", "k", "--cache-bypass", "--cache-inherit"])
    assert exc.value.code == 2


def test_main_console_entry_reports_unreachable():
    """The console-script shim: parse, connect, fail, exit 1 — without
    a traceback. (Sync test: main() calls asyncio.run itself.)"""
    code = cli.main(["status", "--url", "http://127.0.0.1:9", "--timeout", "0.2"])
    assert code == 1
