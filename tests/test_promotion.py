"""Migration promotion — tier 38: turning a go verdict into serving
traffic through config.yaml itself.

The contract pinned here, in one paragraph: POST /admin/migration/plan
renders the exact line-level config.yaml edits (mode, structured
changes, unified diff, canary share) plus the evidence verdict
justifying them, with NO side effects — no write, no backup, no audit.
POST /admin/migration/promote re-builds that plan from the file as it
is at apply time (never a stale plan), gates on the evidence verdict —
a no_go is a 409 promotion_blocked unless force=true; a hold or thin
evidence promotes with the concern attached as a warning — then backs
the file up (timestamped copy), writes, and hot-reloads through the
SAME path POST /admin/reload takes; if the reload refuses, the backup
is restored and the running config never moved. config.yaml stays the
single routing truth: the promoted shadow twin flips to serving at the
requested weight inside the incumbent's priority tier (the canary
ladder — promote again to shift more), a cross-model candidate is
INSERTED as a copy while its original is never touched, and the
incumbent is never demoted or removed.

The text surgery is bounded honestly: block-form entries only —
comments between entries survive byte-for-byte, flow-style entries and
a file that disagrees with the running config are refused with a clear
409, never best-effort-edited. Promotion tests NEVER touch the repo's
./config.yaml: every environment points settings.config_path at a
tmp_path file the test owns.
"""

from __future__ import annotations

import gateway.main  # monkeypatch target for the reload-failure test
import pytest

from gateway.config import AuditConfig, GatewayConfig
from gateway.core.migration import (
    RECOMMENDATION_GO,
    RECOMMENDATION_INSUFFICIENT,
    RECOMMENDATION_NO_GO,
)
from gateway.core.promotion import (
    MODE_FLIP,
    MODE_INSERT,
    MODE_REBALANCE,
    PromotionError,
    build_plan,
)

from .conftest import ADMIN_KEY, _make_env, auth, create_key
from .test_audit_webhooks import AuditSink, _wait_for
from .test_migration import DRIFTED_ANSWER
from .test_trials import _chat, _drain, _rows_for, _start_trial, _trial_detail

# The canonical block-form fixture every unit builds its corner from:
# an incumbent alone in the 100 tier, two shadow candidates (one with
# explicit knobs to normalize away), a 200-tier fallback, and a
# cross-model candidate living on another public model. Comments sit
# BETWEEN entries — the surgery must preserve them byte-for-byte.
BASE_YAML = """\
providers:
  openai:
    api_base: http://mockupstream.test/v1
    api_key: provider-key

models:
  - name: test-model
    context_window: 128000
    deployments:
      # the incumbent: serves alone in the 100 tier (weight defaults to 1)
      - name: openai/primary
        provider: openai
        model: model-a
      # a migration candidate, mirrored until promoted
      - name: openai/twin
        provider: openai
        model: model-shadow
        shadow: true
      # another candidate with explicit knobs the flip normalizes away
      - name: openai/extra
        provider: openai
        model: model-b
        priority: 300
        shadow: true
        shadow_fraction: 0.5
      # the fallback tier
      - name: openai/fallback
        provider: openai
        model: model-c
        priority: 200

  - name: other-model
    deployments:
      # a deployment serving ANOTHER public model — the cross-model candidate
      - name: openai/candidate
        provider: openai
        model: model-b

pricing:
  model-a:
    input: 2.0
    output: 4.0

migration:
  min_pairs: 1
"""

# A model whose ONLY deployment is a shadow candidate — no incumbent
# to align a tier with: the promotion warns and keeps the candidate's
# own priority.
SOLO_YAML = """\
providers:
  openai:
    api_base: http://mockupstream.test/v1
    api_key: provider-key

models:
  - name: solo-model
    deployments:
      # no serving deployment at all — promoting makes it the only one
      - name: openai/loner
        provider: openai
        model: model-a
        shadow: true
        priority: 250

pricing:
  model-a:
    input: 2.0
    output: 4.0
"""

# The e2e shape: one incumbent (weight 1) + one shadow twin (the
# candidate), mirrored live until promoted at weight 100 — the canary
# share then says essentially all traffic, so the behavioral flip is
# observable in the mock's request log.
E2E_YAML = """\
providers:
  openai:
    api_base: http://mockupstream.test/v1
    api_key: provider-key

models:
  - name: test-model
    deployments:
      # the incumbent, serving alone (weight defaults to 1)
      - name: openai/primary
        provider: openai
        model: model-a
      # the migration candidate: mirrored until promoted
      - name: openai/twin
        provider: openai
        model: model-shadow
        shadow: true

pricing:
  model-a:
    input: 2.0
    output: 4.0
  model-shadow:
    input: 2.0
    output: 4.0

# The latency bar is effectively unbounded here: the pins are the
# verdict VOCABULARY (go / no_go) and the file surgery, never a
# machine-speed latency measurement (see tests/test_migration.py for
# the real-bar pins).
migration:
  min_pairs: 1
  max_latency_delta_pct: 10000.0
"""

# The trial-evidence shape: the candidate is a serving deployment of
# ANOTHER public model — no mirror group can ever exist for
# (test-model, openai/candidate), so the promotion is judged on the
# latest completed trial for the pair.
CROSS_YAML = """\
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
  - name: other-model
    deployments:
      # serving on its own model — the cross-model candidate
      - name: openai/candidate
        provider: openai
        model: model-b

pricing:
  model-a:
    input: 2.0
    output: 4.0
  model-b:
    input: 2.0
    output: 4.0

# Same stance as E2E_YAML: the pin is the verdict vocabulary, never a
# machine-speed latency delta.
migration:
  min_pairs: 1
  max_latency_delta_pct: 10000.0
"""


def _write_and_load(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path, GatewayConfig.load(path)


def _plan(tmp_path, model: str, deployment: str, weight: int = 10, text: str = BASE_YAML):
    _path, config = _write_and_load(tmp_path, text)
    return build_plan(text, config, model, deployment, weight)


def _reload(tmp_path, plan: dict) -> GatewayConfig:
    """The plan's edited file must parse back into a config that says
    what the plan says it says — every unit round-trips."""
    out = tmp_path / "edited.yaml"
    out.write_text(plan["new_text"], encoding="utf-8")
    return GatewayConfig.load(out)


def _deployment(config: GatewayConfig, model: str, name: str):
    for m in config.models:
        if m.name == model:
            for d in m.deployments:
                if d.name == name:
                    return d
    raise AssertionError(f"{model}/{name} not in config")


def _audit_config(path) -> GatewayConfig:
    """Load the YAML file with audit webhook delivery on, so promotion
    records are observable in a sink."""
    config = GatewayConfig.load(path)
    config.audit = AuditConfig(webhook_url="https://audit.test/hook")
    return config


async def _post_promotion(client, endpoint: str, **fields):
    return await client.post(
        f"/admin/migration/{endpoint}", json=fields, headers=auth(ADMIN_KEY)
    )


# ----------------------------------------------------------------------
# Units: build_plan — the pure line surgery
# ----------------------------------------------------------------------


def test_flip_drops_shadow_sets_weight_and_preserves_comments(tmp_path):
    """The canonical flip: the twin's shadow: line is removed, weight
    lands after its key lines, the incumbent's comments survive
    byte-for-byte, and the edited file parses back with the twin
    serving in the 100 tier. No explicit priority/weight lines on the
    twin, so both are inserts/defaults — the lightest edit."""
    plan = _plan(tmp_path, "test-model", "openai/twin", weight=10)

    assert plan["mode"] == MODE_FLIP
    assert plan["priority"] == 100  # the incumbent's tier
    assert plan["canary_share"] == round(10 / 11, 4)
    by_key = {c["key"]: c for c in plan["changes"] if "key" in c}
    assert by_key["shadow"] == {"action": "remove", "key": "shadow", "value": "true"}
    assert by_key["weight"] == {"action": "add", "key": "weight", "from": 1, "to": 10}
    assert "shadow_fraction" not in by_key  # the twin has none

    # The human preview: the shadow line leaves, the weight line lands.
    assert "-        shadow: true" in plan["diff"]
    assert "+        weight: 10" in plan["diff"]
    # Comments between entries are untouched.
    assert "# the fallback tier" in plan["new_text"]
    assert "# another candidate with explicit knobs" in plan["new_text"]

    cfg = _reload(tmp_path, plan)
    twin = _deployment(cfg, "test-model", "openai/twin")
    assert twin.shadow is False
    assert twin.weight == 10
    assert twin.priority == 100
    # The incumbent and the rest of the file are untouched.
    assert _deployment(cfg, "test-model", "openai/primary").weight == 1
    assert _deployment(cfg, "test-model", "openai/extra").shadow is True


def test_flip_normalizes_explicit_priority_and_fraction(tmp_path):
    """A candidate with explicit knobs gets them normalized: the
    shadow_fraction line goes with the shadow line, the out-of-tier
    priority (300) is rewritten into the incumbent's tier (100), and
    the inserted priority line lands BEFORE the weight line — the
    config's own field order."""
    plan = _plan(tmp_path, "test-model", "openai/extra", weight=10)

    assert plan["mode"] == MODE_FLIP
    assert [
        {k: v for k, v in c.items() if k != "value"} for c in plan["changes"]
    ] == [
        {"action": "remove", "key": "shadow"},
        {"action": "remove", "key": "shadow_fraction"},
        {"action": "set", "key": "priority", "from": 300, "to": 100},
        {"action": "add", "key": "weight", "from": 1, "to": 10},
    ]
    assert plan["canary_share"] == round(10 / 11, 4)

    entry = plan["new_text"].split("openai/extra", 1)[1]
    assert entry.index("priority: 100") < entry.index("weight: 10")
    cfg = _reload(tmp_path, plan)
    extra = _deployment(cfg, "test-model", "openai/extra")
    assert extra.shadow is False
    assert extra.shadow_fraction == 1.0  # the default, with the line gone
    assert extra.priority == 100
    assert extra.weight == 10


def test_rebalance_changes_weight_and_noop_when_unchanged(tmp_path):
    """An already-serving candidate is a plain weight rebalance: one
    edit, nothing removed — and asking for the weight it already has
    is an honest no-op (empty changes, byte-identical file)."""
    plan = _plan(tmp_path, "test-model", "openai/primary", weight=40)
    assert plan["mode"] == MODE_REBALANCE
    assert plan["changes"] == [
        {"action": "add", "key": "weight", "from": 1, "to": 40}
    ]
    # The incumbent is alone in its tier (the twin and extra are still
    # shadow, the fallback is another tier) — so the rebalanced weight
    # IS the tier: share 1.0, honestly.
    assert plan["canary_share"] == 1.0
    cfg = _reload(tmp_path, plan)
    assert _deployment(cfg, "test-model", "openai/primary").weight == 40

    noop = _plan(tmp_path, "test-model", "openai/primary", weight=1)
    assert noop["changes"] == []
    assert noop["new_text"] == BASE_YAML
    assert noop["diff"] == ""


def test_cross_model_insert_copies_candidate_and_leaves_original(tmp_path):
    """A candidate living on ANOTHER model is inserted as a block-form
    copy into the target model's deployments — after its last entry,
    aligned to the serving tier, at the requested weight — while the
    original entry (and its model) is untouched: nothing is ever
    auto-removed, and the diff is pure addition."""
    plan = _plan(tmp_path, "test-model", "openai/candidate", weight=10)

    assert plan["mode"] == MODE_INSERT
    assert plan["changes"] == [
        {
            "action": "insert_entry",
            "model": "test-model",
            "deployment": "openai/candidate",
            "priority": 100,
            "weight": 10,
        }
    ]
    assert plan["canary_share"] == round(10 / 11, 4)

    # Pure addition: no deletions anywhere in the diff, and the inserted
    # entry is there verbatim (context lines ride along, as diffs do).
    body = [
        ln
        for ln in plan["diff"].splitlines()
        if not ln.startswith(("---", "+++", "@@"))
    ]
    assert body
    assert not [ln for ln in body if ln.startswith("-")]
    assert "+      - name: openai/candidate" in body
    # The inserted entry: after the target's LAST deployment entry.
    assert (
        "      - name: openai/candidate\n"
        "        provider: openai\n"
        "        model: model-b\n"
        "        priority: 100\n"
        "        weight: 10"
    ) in plan["new_text"]

    cfg = _reload(tmp_path, plan)
    inserted = _deployment(cfg, "test-model", "openai/candidate")
    assert inserted.shadow is False
    assert inserted.priority == 100
    assert inserted.weight == 10
    # The original is untouched: still serving its own model.
    original = _deployment(cfg, "other-model", "openai/candidate")
    assert original.priority == 100 and original.weight == 1
    # The insert landed after the fallback (the last entry), keeping
    # the entry order stable.
    names = [d.name for d in cfg.models[0].deployments]
    assert names == [
        "openai/primary",
        "openai/twin",
        "openai/extra",
        "openai/fallback",
        "openai/candidate",
    ]


def test_no_incumbent_warns_and_keeps_candidate_priority(tmp_path):
    """A model whose only deployment is the shadow candidate has no
    tier to align with: the plan warns (the candidate becomes its only
    serving deployment), keeps the candidate's own priority, and the
    canary share is honestly 1.0."""
    plan = _plan(tmp_path, "solo-model", "openai/loner", weight=5, text=SOLO_YAML)

    assert plan["mode"] == MODE_FLIP
    assert plan["priority"] == 250  # kept — nothing to align to
    assert plan["canary_share"] == 1.0
    assert len(plan["warnings"]) == 1
    assert "no serving" in plan["warnings"][0]
    cfg = _reload(tmp_path, plan)
    loner = _deployment(cfg, "solo-model", "openai/loner")
    assert loner.shadow is False
    assert loner.priority == 250
    assert loner.weight == 5


def test_unknown_model_and_deployment_are_400s(tmp_path):
    """Promotion targets a configured public model and a configured
    deployment — anything else is a 400 with the specific code."""
    with pytest.raises(PromotionError) as exc:
        _plan(tmp_path, "no-such-model", "openai/twin")
    assert exc.value.status_code == 400
    assert exc.value.code == "unknown_model"

    with pytest.raises(PromotionError) as exc:
        _plan(tmp_path, "test-model", "no/such-deployment")
    assert exc.value.status_code == 400
    assert exc.value.code == "unknown_deployment"


def test_flow_style_entries_are_refused(tmp_path):
    """The surgery only edits canonical block form. A flow-style
    candidate entry and a flow-style deployments list are refused with
    a 409 that says WHY — never guessed at."""
    flow_dash = BASE_YAML.replace(
        "      - name: openai/twin\n"
        "        provider: openai\n"
        "        model: model-shadow\n"
        "        shadow: true",
        "      - {name: openai/twin, provider: openai, "
        "model: model-shadow, shadow: true}",
    )
    assert "- {" in flow_dash
    with pytest.raises(PromotionError) as exc:
        _plan(tmp_path, "test-model", "openai/twin", text=flow_dash)
    assert exc.value.status_code == 409
    assert exc.value.code == "yaml_flow_style"

    # An insert into a flow-style deployments list (the candidate
    # itself lives elsewhere and parses fine) is the same refusal.
    # The whole test-model entry collapses to `deployments: []` —
    # the file must stay VALID yaml; the refusal is about the SHAPE,
    # not a parse error.
    head, other, tail = BASE_YAML.partition("  - name: other-model")
    assert other
    flow_list = head.split("  - name: test-model", 1)[0] + (
        "  - name: test-model\n"
        "    context_window: 128000\n"
        "    deployments: []\n"
        "\n"
    ) + other + tail
    with pytest.raises(PromotionError) as exc:
        _plan(tmp_path, "test-model", "openai/candidate", text=flow_list)
    assert exc.value.status_code == 409
    assert exc.value.code == "yaml_flow_style"


def test_file_disagreeing_with_running_config_is_refused(tmp_path):
    """The running config is the ground truth for WHAT the candidate
    is; the file is what gets edited. When the two disagree — the
    config says shadow, the file's entry has no shadow: line — the
    edit would be a guess, so it is a 409."""
    _path, config = _write_and_load(tmp_path, BASE_YAML)
    twin_entry = (
        "      - name: openai/twin\n"
        "        provider: openai\n"
        "        model: model-shadow\n"
        "        shadow: true\n"
    )
    assert twin_entry in BASE_YAML
    mismatched = BASE_YAML.replace(twin_entry, twin_entry.replace("        shadow: true\n", ""))

    with pytest.raises(PromotionError) as exc:
        build_plan(mismatched, config, "test-model", "openai/twin", 10)
    assert exc.value.status_code == 409
    assert exc.value.code == "yaml_mismatch"
    assert "disagree" in exc.value.message


def test_weight_out_of_range_is_400(tmp_path):
    """Weight is a canary share inside the tier, 1-100 — anything
    outside is rejected before any text is touched."""
    for bad in (0, 101, -5):
        with pytest.raises(PromotionError) as exc:
            _plan(tmp_path, "test-model", "openai/twin", weight=bad)
        assert exc.value.status_code == 400
        assert exc.value.code == "invalid_weight"


# ----------------------------------------------------------------------
# e2e: the plan/promote endpoints
# ----------------------------------------------------------------------


async def test_promote_flips_shadow_to_serving_end_to_end(tmp_path):
    """The whole ladder in one test: mirrored traffic verdicts the twin
    go, plan renders the flip with the file untouched, promote applies
    it — backup written, file edited, hot-reload live, promotion
    audited — and the gateway's behavior actually changes: the
    candidate takes the traffic (its canary share is ~99%), mirroring
    stops (the twin is no longer a shadow deployment), and the
    incumbent is still there for the residual share."""
    import httpx

    path = tmp_path / "config.yaml"
    path.write_text(E2E_YAML, encoding="utf-8")
    config = _audit_config(path)
    sink = AuditSink()
    async for env in _make_env(
        tmp_path,
        config=config,
        config_path=str(path),
        audit_client=httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)),
    ):
        # Stabilize the latency leg (the tier-37 stance): every call
        # sleeps, so the int-ms means the verdict measures are real.
        env.mock.delay_seconds = 0.05

        # Evidence: two mirrored, scored pairs — exact (the mock answers
        # identically on both upstream models) — verdicts go.
        for content in ("first request", "second request"):
            await _chat(env.client, content=content)
        await _drain(env)
        verdicts = (
            await env.client.get("/admin/migration/verdicts", headers=auth(ADMIN_KEY))
        ).json()
        twin_verdict = next(
            r for r in verdicts["data"] if r["deployment"] == "openai/twin"
        )
        assert twin_verdict["recommendation"]["verdict"] == RECOMMENDATION_GO

        original = path.read_text()

        # -- plan: the exact edits, zero side effects ------------------
        plan_resp = await _post_promotion(
            env.client, "plan", model="test-model", deployment="openai/twin", weight=100
        )
        assert plan_resp.status_code == 200, plan_resp.text
        plan = plan_resp.json()
        assert plan["mode"] == MODE_FLIP
        assert plan["recommendation"]["verdict"] == RECOMMENDATION_GO
        assert plan["evidence_source"] == "mirror"
        assert plan["canary_share"] == round(100 / 101, 4)
        assert "+        weight: 100" in plan["diff"]
        assert "new_text" not in plan
        # No side effects: the file is byte-identical, no backup exists.
        assert path.read_text() == original
        assert list(tmp_path.glob("config.yaml.bak-*")) == []

        # -- promote: backup -> write -> hot-reload -> audit ------------
        promote_resp = await _post_promotion(
            env.client, "promote", model="test-model", deployment="openai/twin", weight=100
        )
        assert promote_resp.status_code == 200, promote_resp.text
        result = promote_resp.json()
        assert result["mode"] == MODE_FLIP
        assert result["forced"] is False
        assert result["recommendation"]["verdict"] == RECOMMENDATION_GO
        assert result["reload"]["source"] == "promotion"
        backups = list(tmp_path.glob("config.yaml.bak-*"))
        assert len(backups) == 1
        assert backups[0].name == result["backup"]
        assert backups[0].read_text() == original  # the undo point

        # The file: the flip, comments preserved, shadow gone.
        edited = path.read_text()
        assert "shadow: true" not in edited.split("models:", 1)[1]
        assert "weight: 100" in edited
        assert "# the migration candidate" in edited
        # The reloaded running config: the twin serves at 100.
        live = env.app.state.config
        assert _deployment(live, "test-model", "openai/twin").shadow is False
        assert _deployment(live, "test-model", "openai/twin").weight == 100
        assert _deployment(live, "test-model", "openai/primary").weight == 1

        # The audit record: who promoted what, on which verdict.
        await _wait_for(
            lambda: any(p["event"] == "promotion.applied" for p in sink.payloads),
            what="promotion.applied audit record",
        )
        record = next(
            p for p in sink.payloads if p["event"] == "promotion.applied"
        )
        assert record["actor"] == {"name": "admin", "auth_method": "admin_key"}
        assert record["details"]["model"] == "test-model"
        assert record["details"]["deployment"] == "openai/twin"
        assert record["details"]["weight"] == 100
        assert record["details"]["verdict"] == RECOMMENDATION_GO
        assert record["details"]["force"] is False
        assert record["details"]["backup"] == result["backup"]
        assert record["details"]["mode"] == MODE_FLIP
        assert record["details"]["changes"]

        # -- behavior: the candidate takes the traffic ------------------
        n_before = len(env.mock.requests)
        rids = []
        for i in range(20):
            rids.append(await _chat(env.client, content=f"post-promotion {i}"))
        await _drain(env)
        served = [r["body"].get("model") for r in env.mock.requests[n_before:]]
        # Weight 100 vs the incumbent's 1: essentially all traffic —
        # the honest weighted-routing pin (never exact-pinned).
        assert served.count("model-shadow") >= 15
        assert all(m in ("model-shadow", "model-a") for m in served)
        # Mirroring stopped: every new request has exactly its own
        # primary row — no shadow twin, because the twin serves now.
        for rid in rids:
            rows = await _rows_for(env.client, rid)
            assert len(rows) == 1
            assert rows[0]["deployment"] in ("openai/twin", "openai/primary")
        break


async def test_no_go_blocks_until_forced(tmp_path):
    """Divergent evidence — the candidate answers differently — verdicts
    no_go, and promote refuses with 409 promotion_blocked: no file
    write, no backup, no audit. force=true promotes anyway, and BOTH
    the response and the audit record say it was forced and why."""
    import httpx

    path = tmp_path / "config.yaml"
    path.write_text(E2E_YAML, encoding="utf-8")
    config = _audit_config(path)
    sink = AuditSink()
    async for env in _make_env(
        tmp_path,
        config=config,
        config_path=str(path),
        audit_client=httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)),
    ):
        env.mock.delay_seconds = 0.05
        env.mock.model_content["model-shadow"] = DRIFTED_ANSWER
        for content in ("divergent one", "divergent two"):
            await _chat(env.client, content=content)
        await _drain(env)
        original = path.read_text()

        blocked = await _post_promotion(
            env.client, "promote", model="test-model", deployment="openai/twin", weight=10
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"]["code"] == "promotion_blocked"
        assert "no_go" in blocked.json()["error"]["message"]
        assert path.read_text() == original
        assert list(tmp_path.glob("config.yaml.bak-*")) == []
        assert not any(p["event"] == "promotion.applied" for p in sink.payloads)

        forced = await _post_promotion(
            env.client,
            "promote",
            model="test-model",
            deployment="openai/twin",
            weight=10,
            force=True,
        )
        assert forced.status_code == 200, forced.text
        result = forced.json()
        assert result["forced"] is True
        assert result["recommendation"]["verdict"] == RECOMMENDATION_NO_GO
        assert any("no_go" in w for w in result["warnings"])
        assert "weight: 10" in path.read_text()

        await _wait_for(
            lambda: any(
                p["event"] == "promotion.applied" and p["details"].get("force")
                for p in sink.payloads
            ),
            what="forced promotion audit record",
        )
        record = next(
            p for p in sink.payloads if p["event"] == "promotion.applied"
        )
        assert record["details"]["force"] is True
        assert record["details"]["verdict"] == RECOMMENDATION_NO_GO
        break


async def test_thin_evidence_promotes_with_warning(tmp_path):
    """No evidence at all (no mirrors, no trial) verdicts
    insufficient_evidence — which does NOT block: the promotion carries
    the verdict as a warning, the evidence source says none, and the
    audit record says what it was judged on."""
    import httpx

    path = tmp_path / "config.yaml"
    path.write_text(E2E_YAML, encoding="utf-8")
    config = _audit_config(path)
    sink = AuditSink()
    async for env in _make_env(
        tmp_path,
        config=config,
        config_path=str(path),
        audit_client=httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)),
    ):
        resp = await _post_promotion(
            env.client, "promote", model="test-model", deployment="openai/twin", weight=25
        )
        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["recommendation"]["verdict"] == RECOMMENDATION_INSUFFICIENT
        assert result["evidence_source"] == "none"
        assert any(
            RECOMMENDATION_INSUFFICIENT in w for w in result["warnings"]
        )
        assert "weight: 25" in path.read_text()

        await _wait_for(
            lambda: any(p["event"] == "promotion.applied" for p in sink.payloads),
            what="thin-evidence promotion audit record",
        )
        record = next(p for p in sink.payloads if p["event"] == "promotion.applied")
        assert record["details"]["verdict"] == RECOMMENDATION_INSUFFICIENT
        assert record["details"]["evidence_source"] == "none"
        break


async def test_reload_failure_restores_file_and_config(tmp_path, monkeypatch):
    """The safety net: when the edited file does not reload, the
    backup is restored, the running config never moved, and the
    operator gets a 400 saying exactly that."""
    path = tmp_path / "config.yaml"
    path.write_text(E2E_YAML, encoding="utf-8")
    config = GatewayConfig.load(path)

    async def _boom(app, source="admin"):
        raise RuntimeError("simulated reload refusal")

    async for env in _make_env(tmp_path, config=config, config_path=str(path)):
        original = path.read_text()
        monkeypatch.setattr(gateway.main, "reload_gateway", _boom)
        resp = await _post_promotion(
            env.client,
            "promote",
            model="test-model",
            deployment="openai/twin",
            weight=10,
        )
        monkeypatch.undo()

        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "config_invalid"
        assert "restored" in resp.json()["error"]["message"]
        # The file is byte-identical again, and the running config is
        # the very same object — nothing moved.
        assert path.read_text() == original
        assert env.app.state.config is config
        # The backup from the attempt still exists — the operator's
        # record of what was tried.
        assert len(list(tmp_path.glob("config.yaml.bak-*"))) == 1
        break


async def test_trial_evidence_feeds_cross_model_promotion(tmp_path):
    """A candidate serving ANOTHER model can never have mirror
    evidence — the promotion is judged on the latest completed trial
    for the pair, and the apply is the cross-model insert: the
    candidate is copied into the target model's deployments, its
    original untouched."""
    path = tmp_path / "config.yaml"
    path.write_text(CROSS_YAML, encoding="utf-8")
    config = GatewayConfig.load(path)
    async for env in _make_env(tmp_path, config=config, config_path=str(path)):
        # Stabilize the latency leg (the tier-37 stance): the captured
        # primaries and the trial's replays sleep the same, so the
        # means the verdict compares are real and equal.
        env.mock.delay_seconds = 0.05
        # The captured window the trial replays.
        for content in ("capture one", "capture two"):
            await _chat(env.client, content=content)
        trial = await _start_trial(
            env.client, model="test-model", deployment="openai/candidate"
        )
        await _drain(env)
        detail = await _trial_detail(env.client, trial["id"])
        assert detail["status"] == "completed"
        assert detail["recommendation"]["verdict"] == RECOMMENDATION_GO

        plan_resp = await _post_promotion(
            env.client, "plan", model="test-model", deployment="openai/candidate", weight=100
        )
        assert plan_resp.status_code == 200, plan_resp.text
        plan = plan_resp.json()
        assert plan["mode"] == MODE_INSERT
        assert plan["evidence_source"] == f"trial:{trial['id']}"
        assert plan["recommendation"]["verdict"] == RECOMMENDATION_GO

        promote_resp = await _post_promotion(
            env.client,
            "promote",
            model="test-model",
            deployment="openai/candidate",
            weight=100,
        )
        assert promote_resp.status_code == 200, promote_resp.text
        assert "weight: 100" in path.read_text()

        # The insert is live: the candidate serves test-model now —
        # weighted 100 vs the incumbent's 1, so a handful of chats
        # must reach the candidate's upstream model (never exact-pinned:
        # weighted routing is a share, not a guarantee).
        live = env.app.state.config
        inserted = _deployment(live, "test-model", "openai/candidate")
        assert inserted.shadow is False
        assert inserted.weight == 100
        n_before = len(env.mock.requests)
        rids = [
            await _chat(env.client, content=f"cross-model serving {i}")
            for i in range(5)
        ]
        served = [r["body"].get("model") for r in env.mock.requests[n_before:]]
        assert all(m in ("model-a", "model-b") for m in served)
        assert served.count("model-b") >= 1
        for rid in rids:
            rows = await _rows_for(env.client, rid)
            assert len(rows) == 1  # no mirror rows — nothing is shadow here
        break


async def test_plan_is_side_effect_free_and_repeatable(tmp_path):
    """Plan never writes: same file bytes, no backups, no audit — and
    the same request plans the same edit twice (no clock, no state)."""
    import httpx

    path = tmp_path / "config.yaml"
    path.write_text(E2E_YAML, encoding="utf-8")
    config = _audit_config(path)
    sink = AuditSink()
    async for env in _make_env(
        tmp_path,
        config=config,
        config_path=str(path),
        audit_client=httpx.AsyncClient(transport=httpx.MockTransport(sink.handler)),
    ):
        await _chat(env.client, content="some traffic")
        await _drain(env)
        original = path.read_text()

        first = await _post_promotion(
            env.client, "plan", model="test-model", deployment="openai/twin", weight=10
        )
        second = await _post_promotion(
            env.client, "plan", model="test-model", deployment="openai/twin", weight=10
        )
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert path.read_text() == original
        assert list(tmp_path.glob("config.yaml.bak-*")) == []
        await _drain(env)
        assert not any(p["event"] == "promotion.applied" for p in sink.payloads)
        break


async def test_promotion_permissions_and_validation(tmp_path):
    """Rewriting the routing configuration is platform-admin-only —
    tenant admins and data-plane keys get 403 on both endpoints — and
    the body validates: weight outside 1-100 and a missing field are
    the standard 400s."""
    path = tmp_path / "config.yaml"
    path.write_text(E2E_YAML, encoding="utf-8")
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
            for endpoint in ("plan", "promote"):
                resp = await env.client.post(
                    f"/admin/migration/{endpoint}",
                    json={"model": "test-model", "deployment": "openai/twin"},
                    headers=auth(key),
                )
                assert resp.status_code == 403, (key, endpoint, resp.status_code)

        # Validation: weight bounds + the required model field.
        for bad_weight in (0, 101):
            resp = await _post_promotion(
                env.client,
                "promote",
                model="test-model",
                deployment="openai/twin",
                weight=bad_weight,
            )
            assert resp.status_code == 400, resp.text
            assert "weight" in resp.json()["error"]["message"]
        missing = await env.client.post(
            "/admin/migration/plan",
            json={"deployment": "openai/twin"},
            headers=auth(ADMIN_KEY),
        )
        assert missing.status_code == 400
        assert "model" in missing.json()["error"]["message"]
        # Nothing above ever touched the file.
        assert path.read_text() == E2E_YAML
        break
