"""VM deployment artifacts (tier 65): the deploy script, the TLS compose
overlay (a Caddy reverse proxy), the rendered-file templates (Caddyfile +
systemd unit; vm.env is generated inline), the dormant GitHub Actions
deploy workflow, and the cross-artifact contracts that keep the container
plain-HTTP behind the proxy.

The deploy script is exercised through ``bash -n``, ``--help`` and
``--dry-run`` — the dry run prints every remote command it would run and
renders every file the real deploy writes, so the tests pin the whole plan
without a VM, an SSH connection or a docker daemon. Parse-level YAML checks
(no docker needed) follow tests/test_ops.py's deployment-artifact pattern.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "deploy_vm.sh"


def run_script(*args: str) -> subprocess.CompletedProcess:
    """Run the deploy script in a subprocess, capturing everything."""
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60
    )


def read_repo(relpath: str) -> str:
    return (ROOT / relpath).read_text()


# ----------------------------------------------------------------- the script


def test_script_is_valid_executable_bash():
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert read_repo("scripts/deploy_vm.sh").startswith("#!/usr/bin/env bash")
    # The operator runs it directly (README shows scripts/deploy_vm.sh ...).
    assert SCRIPT.stat().st_mode & 0o111, "deploy_vm.sh must be executable"


def test_help_is_the_header_and_documents_every_flag():
    proc = run_script("--help")
    assert proc.returncode == 0
    for flag in (
        "--host", "--ssh-key", "--app-dir", "--image", "--tls",
        "--domain", "--email", "--dry-run",
    ):
        assert flag in proc.stdout
    # The three TLS modes, with their meaning, in one place.
    for mode in ("none", "self-signed", "letsencrypt"):
        assert mode in proc.stdout
    assert "llm-gateway.service" in proc.stdout


def test_dry_run_plain_http_plan():
    proc = run_script("--dry-run")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # The plan banner + no-changes promise.
    assert "=== deploy plan ===" in out
    assert "=== DRY-RUN: no changes will be made" in out
    assert "=== dry run complete ===" in out
    # Local deploy: no ssh anywhere in the printed plan.
    assert "ssh" not in out
    # Plain HTTP: the base compose file ONLY (no tls overlay, no Caddyfile).
    assert "-f deploy/docker-compose.yml" in out
    assert "docker-compose.tls.yml" not in out
    assert "----- deploy/Caddyfile -----" not in out
    # The gateway stays publicly published on :8000...
    assert "GATEWAY_HTTP_PORT=8000" in out
    # ... and the health check goes straight to it.
    assert "'http://127.0.0.1:8000/readyz'" in out
    # Bootstrap + sync + service steps are all part of the printed plan.
    assert "[dry-run]" in out and "rsync" in out
    assert "systemctl restart llm-gateway.service" in out


def test_dry_run_self_signed_pins_gateway_and_renders_caddyfile():
    proc = run_script("--dry-run", "--tls", "self-signed")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # The TLS overlay is merged and the gateway is pinned to the loopback,
    # so Caddy is the only public entry point.
    assert "-f deploy/docker-compose.yml -f deploy/docker-compose.tls.yml" in out
    assert "GATEWAY_HTTP_PORT=127.0.0.1:8000" in out
    # The rendered Caddyfile: internal-CA directive for the default site.
    assert "----- deploy/Caddyfile -----" in out
    assert "\n\ttls internal" in out  # the directive, not the template's legend
    assert "localhost {" in out
    assert "default_sni localhost" in out
    assert "reverse_proxy gateway:8000" in out
    # Health check goes through the proxy, verification-skipped.
    assert "-k 'https://localhost/readyz'" in out


def test_dry_run_letsencrypt_renders_acme_site():
    proc = run_script(
        "--dry-run", "--tls", "letsencrypt",
        "--domain", "api.example.com", "--email", "ops@example.com",
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "api.example.com {" in out
    assert "email ops@example.com" in out
    assert "default_sni api.example.com" in out
    # ACME issuance, not the internal CA. (The template's comment legend
    # mentions `tls internal`; the rendered directive must be absent.)
    assert "\n\ttls internal" not in out
    # Health check through the local proxy regardless of public DNS, and
    # certificate verification ON (the cert is public).
    assert "--connect-to api.example.com:443:127.0.0.1:443" in out
    assert "'https://api.example.com/readyz'" in out
    assert " -k " not in out


def test_dry_run_builds_on_the_vm_by_default():
    out = run_script("--dry-run").stdout
    assert "docker compose -f deploy/docker-compose.yml build --pull" in out
    assert "pull gateway" not in out


def test_dry_run_prebuilt_image_pulls_instead_of_building():
    image = "ghcr.io/acme/llm-gateway:sha-a1b2c3"
    out = run_script("--dry-run", "--image", image).stdout
    assert "docker compose -f deploy/docker-compose.yml pull gateway" in out
    assert "build --pull" not in out
    # The image ref rides in deploy/vm.env so the systemd unit uses it too.
    assert f"GATEWAY_IMAGE={image}" in out


def test_dry_run_with_host_uses_ssh_and_defaults_the_domain_to_it():
    out = run_script("--dry-run", "--host", "root@203.0.113.9", "--tls", "self-signed").stdout
    assert "root@203.0.113.9" in out
    assert "ssh" in out  # rsync -e "ssh ..." / ssh remote commands
    # The site address defaults to the --host address for self-signed.
    assert "https://203.0.113.9" in out
    assert "203.0.113.9 {" in out


# ------------------------------------------------------- argument validation


def test_letsencrypt_requires_domain():
    proc = run_script("--tls", "letsencrypt")
    assert proc.returncode != 0
    assert "--domain" in proc.stderr


def test_letsencrypt_requires_email():
    proc = run_script("--tls", "letsencrypt", "--domain", "api.example.com")
    assert proc.returncode != 0
    assert "--email" in proc.stderr


def test_invalid_tls_mode_lists_the_valid_ones():
    proc = run_script("--tls", "bogus")
    assert proc.returncode != 0
    assert "none | self-signed | letsencrypt" in proc.stderr


def test_malformed_flags_are_rejected():
    # Unterminated host, host with a space, relative app dir, app dir with
    # a space, unknown flag, flag without a value.
    for args in (
        ("--host", "root@"),
        ("--host", "root@vm with space"),
        ("--app-dir", "opt/gateway"),
        ("--app-dir", "/opt/my app"),
        ("--bogus",),
        ("--domain",),
    ):
        proc = run_script(*args)
        assert proc.returncode != 0, args


def test_missing_repo_artifacts_are_caused_by_a_bad_checkout():
    # The preflight names the artifact it wanted — the test suite runs from
    # a full checkout, so prove the check exists by matching its message.
    script = read_repo("scripts/deploy_vm.sh")
    assert "missing repo artifact" in script
    for artifact in (
        "Dockerfile", "config.yaml", "deploy/docker-compose.yml",
        "deploy/docker-compose.tls.yml", "deploy/Caddyfile.template",
        "deploy/gateway.service.template",
    ):
        assert artifact in script


# ------------------------------------------------- compose files (parse level)


def test_base_compose_exposes_the_deploy_knobs():
    doc = yaml.safe_load(read_repo("deploy/docker-compose.yml"))
    gateway = doc["services"]["gateway"]
    # The published port is env-configurable: deploy_vm.sh pins it to the
    # VM loopback in TLS mode; everyone else gets the old behavior.
    assert gateway["ports"] == ["${GATEWAY_HTTP_PORT:-8000}:8000"]
    # The image tag is env-configurable for prebuilt (--image) deploys; the
    # default matches compose's own <project>-<service> naming.
    assert gateway["image"] == "${GATEWAY_IMAGE:-llm-gateway-gateway}"
    # What tier-9's test_ops already pinned must survive this tier's edit.
    assert "readyz" in "".join(gateway["healthcheck"]["test"])
    assert gateway["stop_grace_period"] == "35s"
    assert gateway["restart"] == "unless-stopped"
    assert any(str(v).startswith("gateway-data:") for v in gateway["volumes"])


def test_tls_overlay_caddy_shape():
    doc = yaml.safe_load(read_repo("deploy/docker-compose.tls.yml"))
    caddy = doc["services"]["caddy"]
    assert caddy["image"].startswith("caddy:2")
    assert caddy["restart"] == "unless-stopped"
    # :80 = ACME challenges + the redirect; :443 = HTTPS, tcp + udp (HTTP/3).
    assert sorted(caddy["ports"]) == ["443:443", "443:443/udp", "80:80"]
    vols = [str(v) for v in caddy["volumes"]]
    # Relative to the overlay's own directory (project dir = the first -f
    # file's folder), i.e. the file deploy_vm.sh renders next to it.
    assert "./Caddyfile:/etc/caddy/Caddyfile:ro" in vols
    # Certificates + the ACME account must survive redeploys.
    assert "caddy-data:/data" in vols and "caddy-config:/config" in vols
    assert set(doc["volumes"]) == {"caddy-data", "caddy-config"}
    # The proxy only accepts traffic once the gateway reports ready.
    assert caddy["depends_on"]["gateway"]["condition"] == "service_healthy"
    # The overlay file itself is the opt-in — no compose profile gate.
    assert "profiles" not in caddy
    # It only adds; the gateway service is defined by the base file.
    assert set(doc["services"]) == {"caddy"}


def test_caddyfile_template_placeholders_and_proxy_target():
    template = read_repo("deploy/Caddyfile.template")
    for placeholder in ("@DOMAIN@", "@EMAIL@", "@TLS@"):
        assert placeholder in template
    assert "reverse_proxy gateway:8000" in template
    # No-SNI clients (curl against an IP literal) still get the right site.
    assert "default_sni @DOMAIN@" in template


def test_systemd_unit_template_shape():
    unit = read_repo("deploy/gateway.service.template")
    for placeholder in ("@APP_DIR@", "@COMPOSE_FILES@"):
        assert placeholder in unit
    assert "Type=oneshot" in unit
    assert "RemainAfterExit=yes" in unit
    assert "After=network-online.target docker.service" in unit
    # vm.env carries the interpolation variables to every compose run.
    assert "EnvironmentFile=-@APP_DIR@/deploy/vm.env" in unit
    assert "docker compose @COMPOSE_FILES@ up -d --wait" in unit
    assert "docker compose @COMPOSE_FILES@ down" in unit
    assert "WantedBy=multi-user.target" in unit
    # Boot ordering documented in the unit's own comments holds.
    assert "TimeoutStopSec=90" in unit


def test_unit_stop_timeout_covers_the_compose_drain_window():
    # Cross-artifact contract: systemd must wait longer than compose's
    # stop_grace_period (35s), or shutdown kills the in-app drain mid-way.
    unit = read_repo("deploy/gateway.service.template")
    compose = yaml.safe_load(read_repo("deploy/docker-compose.yml"))
    for line in unit.splitlines():
        if line.startswith("TimeoutStopSec="):
            timeout = float(line.split("=", 1)[1].rstrip("s"))
            break
    else:
        raise AssertionError("TimeoutStopSec missing from the unit template")
    grace = float(
        compose["services"]["gateway"]["stop_grace_period"].rstrip("s")
    )
    assert timeout > grace


# --------------------------------------------------- the deploy workflow (CI)


def test_deploy_workflow_is_dormant_until_enabled():
    doc = yaml.safe_load(read_repo(".github/workflows/deploy.yml"))
    trigger = doc.get(True) or doc.get("on")  # YAML 1.1 parses `on` as True
    assert trigger["push"]["branches"] == ["main"]
    assert "workflow_dispatch" in trigger  # manual deploys without a push
    job = doc["jobs"]["deploy"]
    # Dormant by design: inert until DEPLOY_ENABLED=true is set as a repo
    # variable — the same opt-in story as ci.yml being push-gated.
    assert "vars.DEPLOY_ENABLED == 'true'" in job["if"]
    assert job["timeout-minutes"] == 30
    # Deployments serialize; a half-finished deploy is never cancelled.
    assert doc["concurrency"] == {"group": "deploy-vm", "cancel-in-progress": False}
    assert doc["permissions"] == {"contents": "read"}
    steps = job["steps"]
    run_steps = [s for s in steps if "run" in s]
    assert any("secrets.DEPLOY_SSH_KEY" in s["env"]["DEPLOY_SSH_KEY"]
               for s in run_steps)
    deploy_step = steps[-1]["run"]
    assert "scripts/deploy_vm.sh" in deploy_step
    # Every deploy-time choice is a variable; the key is the one secret.
    for flag in ("--host", "--ssh-key", "--tls", "--domain", "--email", "--image"):
        assert flag in deploy_step


# --------------------------------------------- the container's TLS contract


def test_dockerfile_stays_plain_http_behind_the_proxy():
    """TLS terminates at Caddy (or an LB), once, in front — the container
    serves plain HTTP forever, so the image needs no certificates."""
    dockerfile = read_repo("Dockerfile")
    assert '--port", "8000' in dockerfile
    assert "ssl-certfile" not in dockerfile and "ssl-keyfile" not in dockerfile
    assert "EXPOSE 8000" in dockerfile
    assert "HEALTHCHECK" in dockerfile and "readyz" in dockerfile
    assert "USER gateway" in dockerfile  # still non-root


def test_script_wires_every_artifact_by_name():
    """Coherence check: the script must actually use the artifacts the
    other tests pin — same compose overlay, same templates, same service
    name, same readiness endpoint."""
    script = read_repo("scripts/deploy_vm.sh")
    for needle in (
        "deploy/docker-compose.tls.yml",
        "deploy/Caddyfile.template",
        "deploy/gateway.service.template",
        "llm-gateway.service",
        "vm.env",
        "/readyz",
        "download.docker.com",
        "accept-new",  # SSH: accept the host key on first connect only
        "--wait",      # systemd boot blocks until the gateway is healthy
    ):
        assert needle in script, needle
