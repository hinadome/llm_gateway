"""Promotion rollback — tier 39: promotion's undo, the same discipline
in reverse.

The contract pinned here, in one paragraph: every apply (promote,
rollback) leaves a timestamped ``config.yaml.bak-<stamp>`` next to the
file. GET /admin/migration/backups lists them newest-first — a pure
read — each row explaining the backup (does it still parse, is it
byte-identical to the live file, and the structured ``changes``
restoring it would apply: shadow flags re-set, weight/priority edits,
deployments added/removed). GET /admin/migration/backups/{name} adds
the line-level diff — the human preview. POST /admin/migration/rollback
restores one (None = the latest, "undo the last promotion"): the
backup must parse (never start an edit that cannot land), a
byte-identical one is refused as the no-op it is, the live file is
backed up FIRST (the rollback's own undo point — a rollback is one
rollback away from undone), then the same hot-reload path promote
takes applies; on reload refusal the undo point is restored and the
running config never moved. The act is audited as
``promotion.rolled_back``. No evidence gate, no force flag: rollback
IS the escape hatch — but a deliberate, audited one, never automatic.

Rollback tests NEVER touch the repo's ./config.yaml: every
environment points settings.config_path at a tmp_path file the test
owns, exactly like the promotion tests.
"""

from __future__ import annotations

import gateway.main  # monkeypatch target for the reload-failure test
import pytest

from gateway.config import GatewayConfig
from gateway.core.promotion import (
    compare_configs,
    list_config_backups,
    parse_backup_stamp,
)

from .conftest import ADMIN_KEY, _make_env, auth, create_key
from .test_audit_webhooks import AuditSink, _wait_for
from .test_promotion import E2E_YAML, _audit_config, _post_promotion
from .test_trials import _chat, _drain, _rows_for


def _write_config(tmp_path, text: str = E2E_YAML):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _write_named(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _deployment(config: GatewayConfig, model: str, name: str):
    for m in config.models:
        if m.name == model:
            for d in m.deployments:
                if d.name == name:
                    return d
    raise AssertionError(f"{model}/{name} not in config")


async def _promote(client, weight: int, **extra) -> object:
    return await _post_promotion(
        client,
        "promote",
        model="test-model",
        deployment="openai/twin",
        weight=weight,
        **extra,
    )


async def _rollback(client, backup: str | None = None) -> object:
    body = {} if backup is None else {"backup": backup}
    return await client.post(
        "/admin/migration/rollback", json=body, headers=auth(ADMIN_KEY)
    )


async def _backups(client) -> dict:
    resp = await client.get("/admin/migration/backups", headers=auth(ADMIN_KEY))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _detail(client, name: str) -> object:
    return await client.get(
        f"/admin/migration/backups/{name}", headers=auth(ADMIN_KEY)
    )


# ----------------------------------------------------------------------
# Units: the pure helpers
# ----------------------------------------------------------------------


def test_parse_backup_stamp_round_trip(tmp_path):
    """The stamp format is self-consistent: write_with_backup's format
    parses back to the same UTC instant, and anything that is not a
    ``.bak-<stamp>`` name parses to None — never a guess."""
    from datetime import datetime, timezone

    from gateway.core.promotion import write_with_backup

    path = _write_config(tmp_path)
    backup = write_with_backup(path, E2E_YAML + "\n# edited\n")
    stamp = parse_backup_stamp(backup.name)
    assert stamp is not None
    assert stamp.tzinfo is not None and stamp.tzinfo.utcoffset(stamp).total_seconds() == 0
    # The stamp is the write moment (UTC), fresh: within a minute of now.
    assert abs((stamp - datetime.now(timezone.utc)).total_seconds()) < 60

    assert parse_backup_stamp("config.yaml") is None
    assert parse_backup_stamp("config.yaml.bak-") is None
    assert parse_backup_stamp("config.yaml.bak-not-a-stamp") is None
    # The stamp is a name-string utility: it extracts whatever .bak-
    # stamp a name carries (which file the name belongs to is
    # list_config_backups' filter, not this parser's job).
    assert parse_backup_stamp("other.bak-20260101T000000000000Z") == (
        datetime(2026, 1, 1, tzinfo=timezone.utc)
    )


def test_list_config_backups_newest_first_and_filtered(tmp_path):
    """Only the config file's own ``.bak-<stamp>`` files list, newest
    first (the stamp sorts chronologically — name order IS time
    order); neighbors, prefix-less strays and directories never do."""
    path = _write_config(tmp_path)
    for stamp in ("20260101T000000000001Z", "20260102T000000000002Z"):
        (tmp_path / f"config.yaml.bak-{stamp}").write_text("x", encoding="utf-8")
    # Not backups of this file / not files at all.
    (tmp_path / "config.yaml.bak").write_text("x", encoding="utf-8")
    (tmp_path / "other.yaml.bak-20260103T000000000003Z").write_text("x", encoding="utf-8")
    (tmp_path / "config.yaml.bak-20260103T000000000003Z.d").mkdir()

    names = [b.name for b in list_config_backups(path)]
    assert names == [
        "config.yaml.bak-20260102T000000000002Z",
        "config.yaml.bak-20260101T000000000001Z",
    ]


def test_compare_configs_tells_the_rollback_story(tmp_path):
    """compare_configs(live, backup) is the auditable story of what
    restoring the backup would do — pure over two parsed configs:
    the un-flipped shadow flag re-set, weight/priority edits with
    from/to, deployments added (a cross-model insert, undone) or
    removed — and identical configs compare to []."""
    live_text = E2E_YAML
    # The backup: the twin still shadow (pre-promotion), plus a
    # cross-model candidate the live config never had.
    backup_text = E2E_YAML.replace(
        "      # the migration candidate: mirrored until promoted\n"
        "      - name: openai/twin\n"
        "        provider: openai\n"
        "        model: model-shadow\n"
        "        shadow: true\n",
        "      # the migration candidate: mirrored until promoted\n"
        "      - name: openai/twin\n"
        "        provider: openai\n"
        "        model: model-shadow\n"
        "        shadow: true\n"
        "      - name: openai/stray\n"
        "        provider: openai\n"
        "        model: model-b\n",
    )
    assert backup_text != live_text
    live = GatewayConfig.load(_write_config(tmp_path, live_text))
    # And the LIVE config the way the second promotion left it: the
    # twin serving at weight 50.
    live.models[0].deployments[1].shadow = False
    live.models[0].deployments[1].weight = 50
    live.models[0].deployments[1].priority = 100
    backup = GatewayConfig.load(_write_named(tmp_path, "for_backup.yaml", backup_text))

    changes = compare_configs(live, backup)
    # Sorted (model, deployment) order — stray alphabetizes before twin.
    assert changes == [
        # the stray: the backup has it, the live config doesn't — the
        # restore re-adds it
        {"model": "test-model", "deployment": "openai/stray", "action": "add_deployment"},
        # the twin: the promotion's flip, undone (back to mirroring),
        # the canary weight gone with it
        {"model": "test-model", "deployment": "openai/twin", "action": "shadow"},
        {"model": "test-model", "deployment": "openai/twin", "action": "set_weight", "from": 50, "to": 1},
    ]

    # Identical configs: the honest empty no-op.
    assert compare_configs(live, live) == []
    # And the reverse direction tells the mirror story (restoring
    # would flip the twin back to serving).
    reverse = compare_configs(backup, live)
    assert {"model": "test-model", "deployment": "openai/twin", "action": "unshadow"} in reverse


# ----------------------------------------------------------------------
# e2e: the backups surface + the rollback apply
# ----------------------------------------------------------------------


async def test_rollback_end_to_end(tmp_path):
    """The full ladder, undone: two promotions climb the canary
    (weight 100, then 50) leaving two backups; the list explains each
    (newest first, parsed changes); rollback to the FIRST — the
    pre-promotion file — restores it byte-for-byte, the running
    config follows (the twin mirrors again: two log rows per chat),
    the act is audited with its own undo point, and that undo point
    holds the file the rollback replaced."""
    import httpx

    path = _write_config(tmp_path)
    config = _audit_config(path)
    sink = AuditSink()
    async for env in _make_env(
        tmp_path,
        config=config,
        config_path=str(path),
        audit_client=httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)),
    ):
        original = path.read_text()  # the pre-promotion file

        # Climb the ladder: two promotes, two backups on the way.
        resp = await _promote(env.client, 100)
        assert resp.status_code == 200, resp.text
        resp = await _promote(env.client, 50)
        assert resp.status_code == 200, resp.text

        # -- the list: both backups, newest first, explained ---------
        listed = await _backups(env.client)
        assert len(listed["data"]) == 2
        newest, oldest = listed["data"]
        assert newest["name"] > oldest["name"]  # stamp order = time order
        assert newest["parses"] is True and oldest["parses"] is True
        assert newest["is_current"] is False and oldest["is_current"] is False
        # The newest backup is the weight-100 state; live is weight 50.
        assert newest["changes"] == [
            {
                "model": "test-model",
                "deployment": "openai/twin",
                "action": "set_weight",
                "from": 50,
                "to": 100,
            }
        ]
        # The oldest is the pre-promotion state: the flip, undone.
        assert oldest["changes"] == [
            {"model": "test-model", "deployment": "openai/twin", "action": "shadow"},
            {
                "model": "test-model",
                "deployment": "openai/twin",
                "action": "set_weight",
                "from": 50,
                "to": 1,
            },
        ]

        # -- the detail: the diff is the human preview ---------------
        detail = await _detail(env.client, oldest["name"])
        assert detail.status_code == 200, detail.text
        row = detail.json()
        # Restoring the pre-promotion file: the canary weight line
        # leaves, the shadow line returns.
        assert "-        weight: 50" in row["diff"]
        assert "+        shadow: true" in row["diff"]

        # -- rollback to the oldest: the promotion ladder, undone ---
        rolled = await _rollback(env.client, backup=oldest["name"])
        assert rolled.status_code == 200, rolled.text
        result = rolled.json()
        assert result["backup"] == oldest["name"]
        assert result["reload"]["source"] == "rollback"
        assert result["changes"] == oldest["changes"]
        # The file is the pre-promotion file again, byte-for-byte.
        assert path.read_text() == original
        # The running config followed: the twin mirrors again.
        twin = _deployment(env.app.state.config, "test-model", "openai/twin")
        assert twin.shadow is True and twin.weight == 1

        # The undo point: the rollback's own backup, holding the file
        # it replaced (the weight-50 state).
        undo = tmp_path / result["undo_point"]
        assert undo.is_file()
        assert "weight: 50" in undo.read_text()

        # The audit record: what was restored, from where, what changed.
        await _wait_for(
            lambda: any(
                p["event"] == "promotion.rolled_back" for p in sink.payloads
            ),
            what="promotion.rolled_back audit record",
        )
        record = next(p for p in sink.payloads if p["event"] == "promotion.rolled_back")
        assert record["actor"] == {"name": "admin", "auth_method": "admin_key"}
        assert record["details"]["backup"] == oldest["name"]
        assert record["details"]["undo_point"] == result["undo_point"]
        assert record["details"]["changes"] == oldest["changes"]

        # -- behavior: mirroring resumed -----------------------------
        rid = await _chat(env.client, content="after the rollback")
        await _drain(env)
        rows = await _rows_for(env.client, rid)
        assert len(rows) == 2  # primary + mirror — the twin shadows again
        assert {r["deployment"] for r in rows} == {"openai/primary", "openai/twin"}
        break


async def test_rollback_latest_by_default_noop_refused_redo(tmp_path):
    """An empty body rolls back to the LATEST backup — the common
    "undo the last promotion". A backup byte-identical to the live
    file is refused as the no-op it is (nothing written, nothing
    audited). And the semantics the undo point makes true: after a
    rollback, the latest backup is the rollback's own undo point —
    rolling back to IT redoes the promotion. A rollback is one
    rollback away from undone, in both directions."""
    import httpx

    path = _write_config(tmp_path)
    config = _audit_config(path)
    sink = AuditSink()
    async for env in _make_env(
        tmp_path,
        config=config,
        config_path=str(path),
        audit_client=httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)),
    ):
        original = path.read_text()  # the pre-promotion file
        assert (await _promote(env.client, 10)).status_code == 200
        listed = await _backups(env.client)
        assert len(listed["data"]) == 1

        # No body: the latest backup (the only one) = the pre-promotion file.
        rolled = await _rollback(env.client)
        assert rolled.status_code == 200, rolled.text
        assert rolled.json()["backup"] == listed["data"][0]["name"]
        assert path.read_text() == original
        await _wait_for(
            lambda: any(
                p["event"] == "promotion.rolled_back" for p in sink.payloads
            ),
            what="promotion.rolled_back audit record",
        )
        rolled_back_records = [
            p for p in sink.payloads if p["event"] == "promotion.rolled_back"
        ]
        assert len(rolled_back_records) == 1

        # Now the backups are: the rollback's undo point (the promoted
        # weight-10 state — NEWEST) and the original (byte-identical
        # to the live file).
        current = (await _backups(env.client))["data"]
        assert len(current) == 2
        undo_point, same = current[0], current[1]
        assert undo_point["is_current"] is False
        assert same["is_current"] is True

        # Rolling back to the byte-identical one is a no-op: refused,
        # nothing written, nothing more audited. Its detail agrees
        # with an empty diff.
        noop = await _rollback(env.client, backup=same["name"])
        assert noop.status_code == 409, noop.text
        assert noop.json()["error"]["code"] == "rollback_noop"
        assert path.read_text() == original
        detail = await _detail(env.client, same["name"])
        assert detail.json()["diff"] == ""
        await _drain(env)
        assert len(
            [p for p in sink.payloads if p["event"] == "promotion.rolled_back"]
        ) == 1

        # Rolling back to the LATEST (no body) picks the undo point —
        # the redo: the promotion is back, live again.
        redo = await _rollback(env.client)
        assert redo.status_code == 200, redo.text
        assert redo.json()["backup"] == undo_point["name"]
        assert "weight: 10" in path.read_text()
        twin = _deployment(env.app.state.config, "test-model", "openai/twin")
        assert twin.shadow is False and twin.weight == 10
        break


async def test_unparseable_backup_is_listed_honestly_and_refused(tmp_path):
    """A backup that no longer parses (schema drift, a hand edit gone
    wrong) is LISTED — parses: false, no changes story — but rolling
    back to it is refused before anything is touched: never start an
    edit that cannot land."""
    path = _write_config(tmp_path)
    config = GatewayConfig.load(path)
    async for env in _make_env(tmp_path, config=config, config_path=str(path)):
        bad = tmp_path / "config.yaml.bak-20260101T000000000000Z"
        bad.write_text("models: {{{{\n", encoding="utf-8")
        original = path.read_text()

        listed = await _backups(env.client)
        row = next(r for r in listed["data"] if r["name"] == bad.name)
        assert row["parses"] is False
        assert row["changes"] is None
        assert row["is_current"] is False

        detail = await _detail(env.client, bad.name)
        assert detail.status_code == 200
        assert detail.json()["parses"] is False

        refused = await _rollback(env.client, backup=bad.name)
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "backup_invalid"
        assert "does not parse" in refused.json()["error"]["message"]
        assert path.read_text() == original
        break


async def test_rollback_name_validation_and_missing(tmp_path):
    """Names are flat and prefixed: a path separator or the wrong
    prefix is a 400 (the resolution can never leave the config
    directory), a well-formed name that doesn't exist is a 404, and
    an empty string is a 400 — never a silent 'latest'."""
    path = _write_config(tmp_path)
    config = GatewayConfig.load(path)
    async for env in _make_env(tmp_path, config=config, config_path=str(path)):
        assert (await _promote(env.client, 10)).status_code == 200

        for bad_name in ("../config.yaml.bak-x", "gateway.db", ""):
            resp = await _rollback(env.client, backup=bad_name)
            assert resp.status_code == 400, (bad_name, resp.text)
            assert resp.json()["error"]["code"] == "invalid_backup_name"

        missing = await _rollback(
            env.client, backup="config.yaml.bak-20990101T000000000000Z"
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "backup_not_found"

        # The detail endpoint validates the same way.
        detail = await _detail(env.client, "gateway.db")
        assert detail.status_code == 400
        assert detail.json()["error"]["code"] == "invalid_backup_name"
        break


async def test_rollback_reload_failure_restores_file_and_config(tmp_path, monkeypatch):
    """The same safety net promote has: when the restored config does
    not reload, the rollback's undo point is restored, the running
    config never moved, and the operator gets a 400 saying exactly
    that."""
    path = _write_config(tmp_path)
    config = GatewayConfig.load(path)

    async def _boom(app, source="admin"):
        raise RuntimeError("simulated reload refusal")

    async for env in _make_env(tmp_path, config=config, config_path=str(path)):
        assert (await _promote(env.client, 10)).status_code == 200
        promoted_text = path.read_text()  # what the ladder left live
        live = env.app.state.config    # the promote reloaded — this is
        assert live is not config      # the object that must not move now

        monkeypatch.setattr(gateway.main, "reload_gateway", _boom)
        resp = await _rollback(env.client)
        monkeypatch.undo()

        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "config_invalid"
        assert "restored" in resp.json()["error"]["message"]
        # The file is the promoted state again (the failed rollback
        # undone), and the running config is the very same object.
        assert path.read_text() == promoted_text
        assert "weight: 10" in promoted_text
        assert env.app.state.config is live
        break


async def test_rollback_permissions_and_missing_config(tmp_path):
    """Restoring routing configuration is platform-admin-only — the
    list, the detail and the apply all 403 for tenant admins and
    data-plane keys — and a missing config file is the same 409 the
    promotion endpoints give (rollback reads what the gateway loads)."""
    path = _write_config(tmp_path)
    config = GatewayConfig.load(path)
    async for env in _make_env(tmp_path, config=config, config_path=str(path)):
        tenant = (
            await env.client.post(
                "/admin/tenants", json={"name": "acme"}, headers=auth(ADMIN_KEY)
            )
        ).json()
        tadmin = (
            await create_key(env.client, "tadmin", is_admin=True, tenant_id=tenant["id"])
        )["key"]
        plain = (await create_key(env.client, "plain"))["key"]

        for key in (tadmin, plain):
            listed = await env.client.get("/admin/migration/backups", headers=auth(key))
            assert listed.status_code == 403, listed.text
            detail = await env.client.get(
                "/admin/migration/backups/config.yaml.bak-x", headers=auth(key)
            )
            assert detail.status_code == 403
            rolled = await env.client.post(
                "/admin/migration/rollback", json={}, headers=auth(key)
            )
            assert rolled.status_code == 403

        # No backups yet: the rollback says 404, honestly.
        empty = await _rollback(env.client)
        assert empty.status_code == 404
        assert empty.json()["error"]["code"] == "backup_not_found"

        # A missing config file: 409 config_file_missing on all three.
        path.unlink()
        listed = await env.client.get(
            "/admin/migration/backups", headers=auth(ADMIN_KEY)
        )
        assert listed.status_code == 409
        assert listed.json()["error"]["code"] == "config_file_missing"
        rolled = await _rollback(env.client)
        assert rolled.status_code == 409
        assert rolled.json()["error"]["code"] == "config_file_missing"
        break
