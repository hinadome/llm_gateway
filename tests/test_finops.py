"""Cost anomaly flags — tier 46: unusual money and traffic patterns,
surfaced as flags beside the invoice, never pager alerts.

The contract pinned here, in one paragraph: the invoice says what
each key and tenant consumed this month; the anomalies answer the
follow-up — is any of it *unusual*? The comparison is a recent
window (``window_hours``, default 24h) against the trailing baseline
(``baseline_days``, default 14d immediately before it), over the
tier-45 client-consumed lens (shadow mirrors and trial replays
excluded, cache hits included). Four flag shapes, each a billing
surprise in its own way: ``cost_spike`` (a key spending several
times its trailing daily rate), ``traffic_spike`` (the request-rate
version), ``traffic_stop`` (a key with real baseline traffic that
went silent — the churn/broken-integration signal a spend dashboard
structurally cannot show) and ``first_seen_model`` (something new
on the bill). The honesty rule is the SRE one: **a thin baseline is
not a verdict** — fewer than ``min_baseline_requests`` baseline rows
means no spike and no silence, never a scary ratio on thin
evidence; a brand-new key cannot spike at all; a baseline that cost
nothing with window spend fires with ``ratio: null`` and a message
saying so — never a fabricated infinity. Every flag carries its own
numbers (window vs baseline requests and cost, the ratio where one
exists, the bounds in the envelope), read at request time — no
loop, no state, no delivery. ``finops.enabled: false`` serves an
honest empty list while the invoice keeps serving; tenant admins
see their own tenant only (forced), plain keys get the 403.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gateway.config import FinopsConfig
from gateway.core.finops import detect_flags
from gateway.db import get_sessionmaker
from gateway.models import RequestLog

from .conftest import ADMIN_KEY, _make_env, auth, create_key
from .test_tenants import _create_tenant

# A config with small gates so units can be exact.
_UNIT = FinopsConfig(
    window_hours=24, baseline_days=14, spike_ratio=3.0, min_baseline_requests=10
)


def _row(
    key_id: str,
    name: str,
    model: str,
    requests: int,
    cost: float,
    tenant: str | None = "t1",
) -> dict:
    return {
        "tenant_id": tenant,
        "key_id": key_id,
        "key_name": name,
        "model": model,
        "requests": requests,
        "cost_usd": cost,
    }


# -- pure units: the detection -----------------------------------------------


def test_detect_flags_all_four_shapes():
    """One pass, every shape: a cost spike (with its ratio), a traffic
    spike, a silence and a first-seen model — each carrying its own
    numbers, ordered deterministically (cost spikes by ratio, then
    traffic, then silences, then new models by name)."""
    baseline = [
        # alpha: 14 days at 1/day, $0.10/day.
        _row("k1", "alpha", "gpt-4o", 14, 1.4),
        # silent: real baseline traffic, gone from the window.
        _row("k2", "silent", "gpt-4o", 40, 0.4),
        # steady: the same pace in both windows — no flag.
        _row("k3", "steady", "gpt-4o", 28, 2.8),
    ]
    window = [
        # alpha: 4x the request rate, 30x the cost rate.
        _row("k1", "alpha", "gpt-4o", 4, 4.2),
        # steady: 2 requests, $0.20 — its baseline pace.
        _row("k3", "steady", "gpt-4o", 2, 0.2),
        # A model nobody used in the baseline.
        _row("k4", "new-key", "claude-3-7", 3, 0.9),
    ]
    flags = detect_flags(window, baseline, _UNIT)
    kinds = [f["kind"] for f in flags]
    # Cost spikes first, traffic spikes second, silences third, new
    # models last — the money shapes outrank the volume ones.
    assert kinds == [
        "cost_spike", "traffic_spike", "traffic_stop", "first_seen_model",
    ]
    cost, traffic, stop, new = flags

    assert cost["key_name"] == "alpha"
    assert cost["ratio"] == 42.0  # (4.2 / 1) / (1.4 / 14)
    assert cost["window"] == {"requests": 4, "cost_usd": 4.2}
    assert cost["baseline"] == {"requests": 14, "cost_usd": 1.4}
    assert "42.0x its baseline rate" in cost["message"]

    assert traffic["key_name"] == "alpha"
    assert traffic["ratio"] == 4.0  # (4 / 1) / (14 / 14)
    assert "4.0x" in traffic["message"]

    assert stop["key_name"] == "silent"
    assert stop["ratio"] is None
    assert stop["window"] == {"requests": 0, "cost_usd": 0.0}
    assert "went silent" in stop["message"]

    assert new["model"] == "claude-3-7"
    # A model flag is not a key flag: no tenant/key fields at all —
    # it is about the line item, not the spender.
    assert "tenant_id" not in new and "key_id" not in new
    assert new["window"]["requests"] == 3
    assert "appears for the first time" in new["message"]


def test_detect_flags_thin_baseline_is_not_a_verdict():
    """The SRE honesty rule, money edition: a baseline with fewer than
    ``min_baseline_requests`` rows justifies nothing — no spike on
    any ratio, however scary, and no silence either. A key with NO
    baseline history (brand-new) cannot spike at all: it will appear
    on the invoice with its spend, not on the anomalies with a
    fabricated verdict."""
    baseline = [
        # thin: 4 baseline rows — under the gate of 10.
        _row("k1", "thin", "gpt-4o", 4, 0.01),
        # quiet-thin: 9 baseline rows, gone from the window — still
        # under the gate, so not a silence either.
        _row("k2", "quiet-thin", "gpt-4o", 9, 0.09),
    ]
    window = [
        # thin explodes — 100x the requests, 1000x the cost — but on
        # four baseline rows that is an anecdote, not an anomaly.
        _row("k1", "thin", "gpt-4o", 400, 100.0),
        # brand-new: no baseline at all, real money — no spike.
        _row("k3", "brand-new", "gpt-4o", 50, 500.0),
    ]
    flags = detect_flags(window, baseline, _UNIT)
    # The only honest thing to say: gpt-4o itself is baseline
    # traffic, so no new-model flag either. Nothing fires.
    assert flags == []


def test_detect_flags_zero_cost_baseline_never_invents_infinity():
    """A baseline that cost nothing (free models, all-cached replays)
    with window spend is a spike the ratio cannot express: the flag
    fires with ``ratio: null`` and a message saying the baseline cost
    nothing — an honest undefined, never a fabricated infinity. And
    it sorts first among the cost spikes: spending-against-zero is
    the loudest signal on the page."""
    baseline = [
        _row("k1", "free-rider", "gpt-4o", 20, 0.0),
        _row("k2", "priced", "gpt-4o", 20, 1.0),
    ]
    window = [
        _row("k1", "free-rider", "gpt-4o", 2, 6.0),
        _row("k2", "priced", "gpt-4o", 2, 6.0),
    ]
    flags = detect_flags(window, baseline, _UNIT)
    spikes = [f for f in flags if f["kind"] == "cost_spike"]
    assert [f["key_name"] for f in spikes] == ["free-rider", "priced"]
    zero = spikes[0]
    assert zero["ratio"] is None
    assert "against a baseline that cost nothing" in zero["message"]
    assert spikes[1]["ratio"] == 84.0  # (6.0/1) / (1.0/14)


def test_detect_flags_quiet_is_quiet():
    """The null case: a key keeping its baseline pace in both windows
    produces no flags at all — the detector has no opinions about
    spending that is simply *normal*."""
    baseline = [_row("k1", "steady", "gpt-4o", 28, 2.8)]
    window = [_row("k1", "steady", "gpt-4o", 2, 0.2)]
    assert detect_flags(window, baseline, _UNIT) == []


# -- e2e: the endpoint -------------------------------------------------------


async def _seed_anomaly_rows(env, acme, spiking, silent, steady, now):
    """The four-key story spread across the two windows: baseline rows
    3 days ago (inside the trailing 14d), window rows 30 minutes ago
    (inside the last 24h)."""
    async with get_sessionmaker()() as session:
        # Baseline (14 days ending 24h ago): all four keys have traffic.
        for i in range(14):
            session.add(RequestLog(
                request_id=f"ab-s{i}", key_id=spiking["id"], key_name="spiking",
                tenant_id=acme["id"], model="test-model", status="success",
                prompt_tokens=10, completion_tokens=5, total_tokens=15,
                cost_usd=0.01, created_at=now - timedelta(days=3, minutes=i),
            ))
            session.add(RequestLog(
                request_id=f"ab-q{i}", key_id=silent["id"], key_name="silent",
                tenant_id=acme["id"], model="test-model", status="success",
                prompt_tokens=10, completion_tokens=5, total_tokens=15,
                cost_usd=0.01, created_at=now - timedelta(days=3, minutes=i),
            ))
            session.add(RequestLog(
                request_id=f"ab-c{i}", key_id=steady["id"], key_name="steady",
                tenant_id=acme["id"], model="test-model", status="success",
                prompt_tokens=10, completion_tokens=5, total_tokens=15,
                cost_usd=0.01, created_at=now - timedelta(days=3, minutes=i),
            ))
        # Window (last 24h): spiking explodes, steady keeps its pace,
        # silent is gone — and a brand-new model appears.
        for i in range(10):
            session.add(RequestLog(
                request_id=f"aw-s{i}", key_id=spiking["id"], key_name="spiking",
                tenant_id=acme["id"], model="test-model", status="success",
                prompt_tokens=10, completion_tokens=5, total_tokens=15,
                cost_usd=3.0, created_at=now - timedelta(minutes=30, seconds=i),
            ))
        session.add(RequestLog(
            request_id="aw-c0", key_id=steady["id"], key_name="steady",
            tenant_id=acme["id"], model="test-model", status="success",
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
            cost_usd=0.01, created_at=now - timedelta(minutes=30),
        ))
        session.add(RequestLog(
            request_id="aw-n0", key_id=spiking["id"], key_name="spiking",
            tenant_id=acme["id"], model="other-model", status="success",
            prompt_tokens=5, completion_tokens=5, total_tokens=10,
            cost_usd=0.05, created_at=now - timedelta(minutes=30),
        ))
        await session.commit()


async def test_anomalies_endpoint_shapes(tmp_path):
    """The seeded story end to end: the spiking key fires cost AND
    traffic flags with the numbers, the silent key fires a stop, the
    steady key fires nothing (normal is not a flag), and the brand-new
    model fires first_seen_model — with the window/baseline bounds in
    the envelope so every number is auditable."""
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "Acme")
        spiking = await create_key(env.client, "spiking", tenant_id=acme["id"])
        silent = await create_key(env.client, "silent", tenant_id=acme["id"])
        steady = await create_key(env.client, "steady", tenant_id=acme["id"])
        now = datetime.now(timezone.utc)
        await _seed_anomaly_rows(env, acme, spiking, silent, steady, now)

        resp = await env.client.get("/admin/anomalies", headers=auth(ADMIN_KEY))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["object"] == "anomalies"
        assert body["config"] == {
            "enabled": True, "window_hours": 24.0, "baseline_days": 14.0,
            "spike_ratio": 3.0, "min_baseline_requests": 10,
        }
        # The bounds: the window is the last 24h, the baseline the 14
        # days before it, both anchored at read time.
        since = datetime.fromisoformat(body["window"]["since"])
        assert body["window"]["hours"] == 24.0
        assert now - since <= timedelta(hours=24)
        baseline_since = datetime.fromisoformat(body["baseline"]["since"])
        assert body["baseline"]["days"] == 14.0
        assert baseline_since < since
        assert "generated_at" in body

        by_kind: dict[str, list[dict]] = {}
        for flag in body["flags"]:
            by_kind.setdefault(flag["kind"], []).append(flag)

        cost = by_kind["cost_spike"][0]
        assert cost["key_name"] == "spiking"
        assert cost["tenant_id"] == acme["id"]
        assert cost["window"]["requests"] == 11  # 10 + the new-model row
        assert cost["window"]["cost_usd"] == 30.05
        assert cost["baseline"]["requests"] == 14
        # (30.05/day) / (0.14/14 = 0.01/day) = 3005x — well over the bar.
        assert cost["ratio"] == 3005.0
        assert "3005.0x" in cost["message"]

        traffic = by_kind["traffic_spike"][0]
        assert traffic["key_name"] == "spiking"
        assert traffic["ratio"] == 11.0  # (11/day) / (1/day)

        (stop,) = by_kind["traffic_stop"]
        assert stop["key_name"] == "silent"
        assert stop["window"]["requests"] == 0
        assert "went silent" in stop["message"]

        (new_model,) = by_kind["first_seen_model"]
        assert new_model["model"] == "other-model"
        assert new_model["window"]["requests"] == 1
        assert "first time" in new_model["message"]

        # The steady key: its pace held in both windows — no flag.
        assert not [f for f in body["flags"] if f.get("key_name") == "steady"]
        break


async def test_anomalies_scoping(tmp_path):
    """The invoice's scoping rule: tenant admins see their own
    tenant's flags only (forced, even when they ask for another
    tenant's), plain keys get the admin 403, and platform admins may
    filter ``?tenant_id=`` or read the whole platform — platform
    keys' flags included."""
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "Acme")
        globex = await _create_tenant(env.client, "Globex")
        acme_admin = await create_key(
            env.client, "acme-admin", is_admin=True, tenant_id=acme["id"]
        )
        member = await create_key(env.client, "member", tenant_id=acme["id"])
        spiking = await create_key(env.client, "spiking", tenant_id=acme["id"])
        silent = await create_key(env.client, "silent", tenant_id=globex["id"])
        now = datetime.now(timezone.utc)
        async with get_sessionmaker()() as session:
            for i in range(14):
                for key, tenant, name in (
                    (spiking, acme["id"], "spiking"),
                    (silent, globex["id"], "silent"),
                ):
                    session.add(RequestLog(
                        request_id=f"sc-b{i}-{name}", key_id=key["id"],
                        key_name=name, tenant_id=tenant, model="test-model",
                        status="success", prompt_tokens=10, completion_tokens=5,
                        total_tokens=15, cost_usd=0.01,
                        created_at=now - timedelta(days=3, minutes=i),
                    ))
            # acme's key spikes; globex's goes silent.
            for i in range(10):
                session.add(RequestLog(
                    request_id=f"sc-w{i}", key_id=spiking["id"],
                    key_name="spiking", tenant_id=acme["id"], model="test-model",
                    status="success", prompt_tokens=10, completion_tokens=5,
                    total_tokens=15, cost_usd=2.0,
                    created_at=now - timedelta(minutes=30, seconds=i),
                ))
            await session.commit()

        # A plain key: not an admin at all.
        resp = await env.client.get(
            "/admin/anomalies", headers=auth(member["key"])
        )
        assert resp.status_code == 403

        # The tenant admin: their tenant's spike only — even when
        # asking for globex's silence.
        resp = await env.client.get(
            "/admin/anomalies",
            params={"tenant_id": globex["id"]},
            headers=auth(acme_admin["key"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert {f["kind"] for f in body["flags"]} == {"cost_spike", "traffic_spike"}
        assert all(f["tenant_id"] == acme["id"] for f in body["flags"])
        assert not [f for f in body["flags"] if f.get("key_name") == "silent"]

        # The platform admin: one tenant on request — globex's
        # silence, and nothing of acme's.
        body = (
            await env.client.get(
                "/admin/anomalies",
                params={"tenant_id": globex["id"]},
                headers=auth(ADMIN_KEY),
            )
        ).json()
        assert [f["kind"] for f in body["flags"]] == ["traffic_stop"]
        assert body["flags"][0]["key_name"] == "silent"

        # ...and both tenants without a filter.
        body = (
            await env.client.get("/admin/anomalies", headers=auth(ADMIN_KEY))
        ).json()
        assert len(body["flags"]) == 3  # acme spike+traffic, globex stop
        break


async def test_anomalies_lens_excludes_shadow_and_trial(tmp_path):
    """The client-consumed lens on both windows: shadow mirrors and
    trial replays count toward neither baseline nor window — an
    expensive mirror storm is comparison traffic, not a billing
    surprise, and a replayed window is not a spike."""
    async for env in _make_env(tmp_path):
        acme = await _create_tenant(env.client, "Acme")
        alpha = await create_key(env.client, "alpha", tenant_id=acme["id"])
        now = datetime.now(timezone.utc)
        async with get_sessionmaker()() as session:
            # A modest baseline.
            for i in range(14):
                session.add(RequestLog(
                    request_id=f"ln-b{i}", key_id=alpha["id"], key_name="alpha",
                    tenant_id=acme["id"], model="test-model", status="success",
                    prompt_tokens=10, completion_tokens=5, total_tokens=15,
                    cost_usd=0.01, created_at=now - timedelta(days=3, minutes=i),
                ))
            # One ordinary client row in the window, at alpha's
            # baseline pace — without it the window would be genuinely
            # empty in the client lens and a traffic_stop would be
            # the honest verdict, muddying what the test proves.
            session.add(RequestLog(
                request_id="ln-w0", key_id=alpha["id"], key_name="alpha",
                tenant_id=acme["id"], model="test-model", status="success",
                prompt_tokens=10, completion_tokens=5, total_tokens=15,
                cost_usd=0.01, created_at=now - timedelta(minutes=30),
            ))
            # An expensive shadow storm + trial replay in the window:
            # would be a 10000x spike if the lens counted them.
            for i in range(50):
                session.add(RequestLog(
                    request_id=f"ln-sh{i}", key_id=alpha["id"], key_name="alpha",
                    tenant_id=acme["id"], model="test-model", status="success",
                    prompt_tokens=10, completion_tokens=5, total_tokens=15,
                    cost_usd=10.0, shadow=True,
                    created_at=now - timedelta(minutes=10, seconds=i),
                ))
            session.add(RequestLog(
                request_id="ln-tr", key_id=alpha["id"], key_name="alpha",
                tenant_id=acme["id"], model="test-model", status="success",
                prompt_tokens=10, completion_tokens=5, total_tokens=15,
                cost_usd=99.0, trial_id="trial-1",
                created_at=now - timedelta(minutes=5),
            ))
            await session.commit()

        body = (
            await env.client.get("/admin/anomalies", headers=auth(ADMIN_KEY))
        ).json()
        # alpha kept its baseline pace in the client lens: no spike on
        # the storm, no spike on the replay, and no silence either
        # (the window still holds its one ordinary row). If the lens
        # leaked, the shadow rows alone would be a ~10000x cost_spike.
        assert not [f for f in body["flags"] if f.get("key_name") == "alpha"]
        break


async def test_anomalies_disabled_serves_an_honest_empty(tmp_path):
    """``finops.enabled: false`` is the kill switch: detection is
    skipped entirely — an empty flag list, the bounds still served,
    the config echo saying so — while the invoice keeps serving
    untouched. The switch is read at request time, so flipping it on
    the live config applies on the very next read."""
    async for env in _make_env(tmp_path):
        env.app.state.config.finops = FinopsConfig(enabled=False)
        resp = await env.client.get("/admin/anomalies", headers=auth(ADMIN_KEY))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["flags"] == []
        assert body["config"]["enabled"] is False
        assert "window" in body and "baseline" in body  # bounds still served

        # The invoice is untouched by the switch.
        invoice = await env.client.get(
            "/admin/invoices", headers=auth(ADMIN_KEY)
        )
        assert invoice.status_code == 200
        assert invoice.json()["object"] == "invoice"
        break
