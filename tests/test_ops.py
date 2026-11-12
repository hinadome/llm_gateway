"""Tests for operational maturity (Tier 9): readiness/liveness, graceful
shutdown of background work, configuration hot-reload, SQLite
backup/restore tooling, and the deployment artifacts.

* **Liveness vs readiness** — ``/healthz`` answers "is the process up"
  (never checks dependencies); ``/readyz`` answers "should traffic route
  here" (startup finished, DB reachable, not draining) and fails 503
  otherwise.
* **Background-task drain** — spend accounting, shadow mirrors and prompt
  A/B are tracked; shutdown drains them before the engine goes away.
* **Hot-reload** — ``POST /admin/reload`` / SIGHUP re-read config.yaml
  and swap guardrails, models/routing, pricing and prompt knobs without
  dropping traffic. An invalid config is rejected and the running one
  stays live. Infra-bound settings are reported as restart-required.
* **Backups** — ``POST /admin/backup`` snapshots the live SQLite database
  (online ``VACUUM INTO``); list + delete manage them. Restore is an
  offline procedure by design (stop, swap the file, start).
"""

from __future__ import annotations

import asyncio
import os
import sqlite3

import pytest
import yaml

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config

# The whole suite can run against a live PostgreSQL server instead of
# per-test SQLite files (GATEWAY_TEST_DATABASE_URL — see conftest).
# The VACUUM INTO backup tests are SQLite by construction.
PG_SUITE = bool(os.environ.get("GATEWAY_TEST_DATABASE_URL"))


# ----------------------------------------------------------------------
# Liveness / readiness
# ----------------------------------------------------------------------


async def test_healthz_liveness(env):
    resp = await env.client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["uptime_seconds"] >= 0


async def test_readyz_ready(env):
    # ASGITransport does not run the lifespan, so the readiness gate is
    # whatever the app state says (the lifespan sets it in production).
    env.app.state.ready = True
    resp = await env.client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["draining"] is False
    assert body["db"] is True
    assert body["circuit_breakers"] == {"open": 0, "half_open": 0}


async def test_readyz_503_while_draining(env):
    """Not-ready (starting or draining) is a 503 so load balancers route
    elsewhere — before any dependency is actually closed."""
    env.app.state.ready = True  # was serving...
    env.app.state.ready = False  # ...now draining
    resp = await env.client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["draining"] is True
    # The DB is still fine — this is purely the drain state.
    assert body["db"] is True


async def test_readyz_503_before_startup(env):
    """Fresh app, lifespan never ran: not ready, and the reason reported."""
    assert getattr(env.app.state, "ready", False) is False
    resp = await env.client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["ready"] is False


async def test_readyz_503_when_db_down(env):
    """An unreachable database fails readiness (but not liveness)."""
    from gateway.db import dispose_engine

    env.app.state.ready = True
    await dispose_engine()
    ready = await env.client.get("/readyz")
    assert ready.status_code == 503
    body = ready.json()
    assert body["db"] is False
    assert body["db_error"]

    live = await env.client.get("/healthz")
    assert live.status_code == 200  # liveness never checks dependencies


async def test_readyz_reports_open_breakers_informationally(env):
    """Open breakers appear in the payload but do NOT fail readiness —
    the router routes around them; killing the pod would take the
    recovery (a probe through half-open) away."""
    env.mock.fail_counts["model-a"] = 99
    key = (await create_key(env.client, "breaker-user"))["key"]
    for i in range(3):  # threshold = 3 request-level failures (unique bodies: no cache hits)
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content=f"probe {i}"),
            headers=auth(key),
        )
        assert resp.status_code in (200, 502, 503)  # however fallback played out
    env.app.state.ready = True
    ready = await env.client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["circuit_breakers"]["open"] >= 1
    env.mock.fail_counts["model-a"] = 0


async def test_health_and_healthz_coexist(env):
    """The original /health endpoint is unchanged (compat)."""
    both = await asyncio.gather(env.client.get("/health"), env.client.get("/healthz"))
    assert all(r.status_code == 200 for r in both)
    assert "models" in both[0].json()
    assert "models" not in both[1].json()


# ----------------------------------------------------------------------
# Background-task drain
# ----------------------------------------------------------------------


async def test_drain_waits_for_tracked_tasks(env):
    """Tracked background work is awaited (bounded); stragglers are
    reported instead of hanging shutdown."""
    pipeline = env.app.state.pipeline
    release = asyncio.Event()

    async def work():
        await release.wait()

    pipeline._spawn(work())
    first = await pipeline.drain_background_tasks(timeout=0.05)
    assert first["timed_out"] == 1

    release.set()
    await asyncio.sleep(0.01)  # let it finish + discard itself
    second = await pipeline.drain_background_tasks(timeout=0.05)
    assert second == {"drained": 0, "timed_out": 0}


async def test_spend_accounting_is_tracked(env):
    """A served request leaves tracked spend work in the registry (what
    shutdown drains) — not an anonymous task the loop kills mid-DB-write."""
    key = (await create_key(env.client, "spend-user"))["key"]
    resp = await env.client.post(
        "/v1/chat/completions", json=chat_body(), headers=auth(key)
    )
    assert resp.status_code == 200
    assert resp.headers["x-gateway-cost-usd"]
    # The registry exists and is a set (drained by shutdown); the task
    # itself may already have completed and discarded itself.
    await asyncio.sleep(0.05)
    assert isinstance(env.app.state.pipeline._background_tasks, set)


# ----------------------------------------------------------------------
# Hot-reload
# ----------------------------------------------------------------------


async def test_reload_swaps_guardrails_without_restart(tmp_path):
    from gateway.config import GuardrailConfig

    async for env in _make_env(
        tmp_path,
        config=make_config(
            guardrails=[
                GuardrailConfig(
                    name="keyword_blocklist",
                    enabled=True,
                    params={"blocked_terms": ["forbidden-word"]},
                )
            ]
        ),
    ):
        key = (await create_key(env.client, "reload-user"))["key"]

        blocked = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="please say forbidden-word"),
            headers=auth(key),
        )
        assert blocked.status_code == 400

        # Reload with guardrails off — same running process.
        summary = env.app.state.pipeline.apply_config(make_config())
        assert summary["models"]
        assert "restart_required" in summary

        allowed = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="please say forbidden-word"),
            headers=auth(key),
        )
        assert allowed.status_code == 200
        break


async def test_reload_invalid_config_keeps_old_config_live(tmp_path):
    from gateway.config import GuardrailConfig

    async for env in _make_env(
        tmp_path,
        config=make_config(
            guardrails=[
                GuardrailConfig(
                    name="keyword_blocklist",
                    enabled=True,
                    params={"blocked_terms": ["forbidden-word"]},
                )
            ]
        ),
    ):
        key = (await create_key(env.client, "still-blocked"))["key"]

        bad = make_config()
        bad.guardrails = [
            GuardrailConfig(name="no_such_guardrail", enabled=True)
        ]
        try:
            env.app.state.pipeline.apply_config(bad)
            raise AssertionError("apply_config should have raised")
        except ValueError as exc:
            assert "no_such_guardrail" in str(exc)

        # The old config is still live — the block still applies.
        still = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="please say forbidden-word"),
            headers=auth(key),
        )
        assert still.status_code == 400
        break


async def test_reload_adds_model_and_alias(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "model-user"))["key"]

        before = await env.client.get("/v1/models", headers=auth(key))
        assert before.status_code == 200
        assert "extra-model" not in [m["id"] for m in before.json()["data"]]

        new_config = make_config()
        new_config.models.append(
            type(new_config.models[0])(
                name="extra-model",
                deployments=[
                    type(new_config.models[0].deployments[0])(
                        name="openai/extra", provider="openai", model="model-c"
                    )
                ],
            )
        )
        new_config.model_aliases = {"alias-model": "extra-model"}
        env.app.state.pipeline.apply_config(new_config)
        # apply_config is the pipeline-internal half; app-level views
        # (GET /v1/models reads app.state.config) follow the reload the
        # same way reload_gateway does.
        env.app.state.config = new_config

        after = await env.client.get("/v1/models", headers=auth(key))
        names = [m["id"] for m in after.json()["data"]]
        assert "extra-model" in names
        assert "alias-model" in names  # aliases are listed too

        # ...and the new model actually serves (through the alias).
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(model="alias-model", content="hi"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == "extra-model"
        break


async def test_reload_preserves_breaker_state(tmp_path):
    from gateway.config import DeploymentConfig, ModelConfig

    async for env in _make_env(
        tmp_path,
        config=make_config(
            models=[
                ModelConfig(
                    name="test-model",
                    deployments=[
                        DeploymentConfig(
                            name="openai/primary",
                            provider="openai",
                            model="model-a",
                            priority=10,
                        ),
                        DeploymentConfig(
                            name="openai/fallback",
                            provider="openai",
                            model="model-b",
                            priority=20,
                        ),
                    ],
                )
            ]
        ),
    ):
        key = (await create_key(env.client, "breaker-user"))["key"]
        env.mock.fail_counts["model-a"] = 99
        for i in range(3):  # threshold 3 (unique bodies: no cache hits)
            resp = await env.client.post(
                "/v1/chat/completions",
                json=chat_body(content=f"trip probe {i}"),
                headers=auth(key),
            )
            assert resp.status_code == 200  # served by the fallback
        breakers = env.app.state.pipeline.router.breakers
        snapshots = await breakers.snapshots()
        assert snapshots["openai/primary"]["state"] == "open"

        # Reload an equivalent config: the open breaker must survive (a
        # config reload is not a health event).
        env.app.state.pipeline.apply_config(make_config())
        snapshots = await breakers.snapshots()
        assert snapshots["openai/primary"]["state"] == "open"
        break


async def test_reload_reports_restart_required_for_infra(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        new_config = make_config()
        new_config.cache.max_entries = 999  # cache backend settings: infra
        summary = env.app.state.pipeline.apply_config(new_config)
        assert "cache" in summary["restart_required"]
        break


async def test_reload_endpoint_e2e(tmp_path):
    """POST /admin/reload re-reads the YAML file from
    settings.config_path and swaps the live config."""
    config_yaml = """
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
    config_file = tmp_path / "reload-config.yaml"
    config_file.write_text(config_yaml)

    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "e2e-reload"))["key"]
        env.app.state.settings.config_path = str(config_file)

        blocked_before = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="please say forbidden-word"),
            headers=auth(key),
        )
        assert blocked_before.status_code == 200  # guardrails off initially

        resp = await env.client.post("/admin/reload", headers=auth(ADMIN_KEY))
        assert resp.status_code == 200, resp.text
        summary = resp.json()
        assert summary["source"] == "admin"
        assert summary["config_path"] == str(config_file)
        assert "test-model" in summary["models"]
        assert "keyword_blocklist" in summary["guardrails"]
        # app.state.config follows the reload.
        assert env.app.state.config is env.app.state.pipeline.config

        blocked_after = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content="please say forbidden-word"),
            headers=auth(key),
        )
        assert blocked_after.status_code == 400  # new guardrail is live
        break


async def test_reload_endpoint_bad_path_is_400(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        env.app.state.settings.config_path = str(tmp_path / "does-not-exist.yaml")
        resp = await env.client.post("/admin/reload", headers=auth(ADMIN_KEY))
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "config_invalid"
        # And the running config is untouched.
        key = (await create_key(env.client, "after-bad"))["key"]
        ok = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert ok.status_code == 200
        break


async def test_reload_endpoint_bad_yaml_is_400(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        bad = tmp_path / "bad.yaml"
        bad.write_text("providers: [this: is: not: valid: yaml")
        env.app.state.settings.config_path = str(bad)
        resp = await env.client.post("/admin/reload", headers=auth(ADMIN_KEY))
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "config_invalid"
        break


async def test_reload_gateway_function_direct(tmp_path):
    """The SIGHUP path shares reload_gateway() with the endpoint."""
    from gateway.main import reload_gateway

    async for env in _make_env(tmp_path, config=make_config()):
        config_yaml = """
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
guardrails: []
"""
        config_file = tmp_path / "sighup-config.yaml"
        config_file.write_text(config_yaml)
        env.app.state.settings.config_path = str(config_file)

        summary = await reload_gateway(env.app, source="sighup")
        assert summary["source"] == "sighup"
        assert env.app.state.config is env.app.state.pipeline.config
        assert env.app.state.pipeline.config.guardrails == []
        break


async def test_reload_requires_admin(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "plain-user"))["key"]
        denied = await env.client.post("/admin/reload", headers=auth(key))
        assert denied.status_code == 403
        anon = await env.client.post("/admin/reload")
        assert anon.status_code == 401
        break


# ----------------------------------------------------------------------
# SQLite backup / restore tooling
# ----------------------------------------------------------------------


@pytest.mark.skipif(PG_SUITE, reason="VACUUM INTO snapshot is SQLite-only")
async def test_backup_creates_valid_sqlite_snapshot(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        backup_dir = tmp_path / "backups"
        env.app.state.settings.backup_dir = str(backup_dir)

        # Generate some state worth backing up.
        key = (await create_key(env.client, "backed-up"))["key"]
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(), headers=auth(key)
        )
        assert resp.status_code == 200

        created = await env.client.post("/admin/backup", headers=auth(ADMIN_KEY))
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["size_bytes"] > 0
        assert body["name"].startswith("gateway-")
        assert body["name"].endswith(".db")

        # The snapshot is a valid SQLite database containing the rows.
        snapshot = sqlite3.connect(backup_dir / body["name"])
        try:
            rows = snapshot.execute("SELECT COUNT(*) FROM request_logs").fetchone()
            assert rows[0] >= 1
            keys = snapshot.execute("SELECT COUNT(*) FROM virtual_keys").fetchone()
            assert keys[0] >= 1
        finally:
            snapshot.close()
        break


@pytest.mark.skipif(PG_SUITE, reason="VACUUM INTO snapshot is SQLite-only")
async def test_backup_list_and_delete(tmp_path):
    import os

    async for env in _make_env(tmp_path, config=make_config()):
        backup_dir = tmp_path / "backups"
        env.app.state.settings.backup_dir = str(backup_dir)

        first = (await env.client.post("/admin/backup", headers=auth(ADMIN_KEY))).json()
        second = (
            await env.client.post("/admin/backup", headers=auth(ADMIN_KEY))
        ).json()

        # Backups can land in the same clock second; give the first an
        # older mtime so newest-first ordering is deterministic.
        os.utime(backup_dir / first["name"], (1_000_000, 1_000_000))

        listed = await env.client.get("/admin/backups", headers=auth(ADMIN_KEY))
        assert listed.status_code == 200
        names = [b["name"] for b in listed.json()["data"]]
        assert second["name"] in names and first["name"] in names
        # Newest first.
        assert names.index(second["name"]) < names.index(first["name"])
        for entry in listed.json()["data"]:
            assert entry["size_bytes"] > 0
            assert entry["created_at"]

        deleted = await env.client.delete(
            f"/admin/backups/{first['name']}", headers=auth(ADMIN_KEY)
        )
        assert deleted.status_code == 200
        gone = await env.client.delete(
            f"/admin/backups/{first['name']}", headers=auth(ADMIN_KEY)
        )
        assert gone.status_code == 404

        remaining = await env.client.get("/admin/backups", headers=auth(ADMIN_KEY))
        assert [b["name"] for b in remaining.json()["data"]] == [second["name"]]
        break


async def test_backup_delete_rejects_bad_names(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        env.app.state.settings.backup_dir = str(tmp_path / "backups")
        bad = await env.client.delete(
            "/admin/backups/bad$name.db", headers=auth(ADMIN_KEY)
        )
        assert bad.status_code == 400
        missing = await env.client.delete(
            "/admin/backups/nope.db", headers=auth(ADMIN_KEY)
        )
        assert missing.status_code == 404
        break


async def test_backup_requires_admin(tmp_path):
    async for env in _make_env(tmp_path, config=make_config()):
        key = (await create_key(env.client, "no-backups-for-you"))["key"]
        denied = await env.client.post("/admin/backup", headers=auth(key))
        assert denied.status_code == 403
        listed = await env.client.get("/admin/backups", headers=auth(key))
        assert listed.status_code == 403
        break


async def test_backup_refused_on_non_sqlite(monkeypatch, tmp_path):
    """Online backups are SQLite-only: any other backend is refused
    with an honest 400 pointing at the platform's own tooling, never
    a half-copy through a different mechanism."""
    import gateway.db as db_module

    class _FakeDialect:
        name = "postgresql"

    class _FakeEngine:
        dialect = _FakeDialect()

    async for env in _make_env(tmp_path, config=make_config()):
        env.app.state.settings.backup_dir = str(tmp_path / "backups")
        monkeypatch.setattr(db_module, "get_engine", lambda: _FakeEngine())
        resp = await env.client.post("/admin/backup", headers=auth(ADMIN_KEY))
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "backup_not_supported"
        assert "pg_dump" in body["error"]["message"]
        # Nothing was written — the refusal happens before any I/O.
        assert not (tmp_path / "backups").exists()
        break


# ----------------------------------------------------------------------
# Deployment artifacts (parse-level checks — no docker daemon needed)
# ----------------------------------------------------------------------


def test_compose_files_parse_and_use_readyz():
    for path in (
        "deploy/docker-compose.yml",
        "deploy/docker-compose.observability.yml",
    ):
        with open(path) as fh:
            doc = yaml.safe_load(fh)
        assert "services" in doc
    with open("deploy/docker-compose.yml") as fh:
        doc = yaml.safe_load(fh)
    gateway = doc["services"]["gateway"]
    # Readiness (not liveness) is what orchestrators should act on.
    assert "readyz" in "".join(gateway["healthcheck"]["test"])
    # Graceful shutdown window for the in-app drain.
    assert gateway["stop_grace_period"] == "35s"
    # SQLite state and backups live on volumes.
    assert any(str(v).startswith("gateway-data:") for v in gateway["volumes"])
    assert any(str(v).startswith("gateway-backups:") for v in gateway["volumes"])
    # The multi-replica shared-state option exists, off by default.
    assert "redis" in doc["services"]
    assert "redis" in doc["services"]["redis"]["profiles"]


def test_dockerfile_hardened():
    with open("Dockerfile") as fh:
        dockerfile = fh.read()
    assert "USER gateway" in dockerfile  # non-root
    assert "HEALTHCHECK" in dockerfile and "readyz" in dockerfile
    # uvicorn's own graceful window mirrors the in-app drain.
    assert "--timeout-graceful-shutdown" in dockerfile
