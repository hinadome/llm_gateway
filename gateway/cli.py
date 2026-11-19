"""The ``gateway`` CLI — a terminal client of the JSON admin API.

The dashboard is the interactive surface; the CLI is the scripted one:
keys and tenants from a terminal, a capture/replay check from cron,
the fleet state (concurrency, runaway jail, canaries, MCP, breakers)
from an SSH session. It is a *thin HTTP client* — it imports nothing
from the gateway package, holds no DB handle, enforces no policy: it
speaks to a running gateway over the same admin API as the dashboard,
with the same credential (``--key`` or ``GATEWAY_ADMIN_KEY``) and the
same scoping (every 403/404 the API would return, the CLI prints).

Conventions:

- Lists print as aligned tables of the scannable columns; ``--json``
  prints the exact response payload for everything else (the full
  truth — tables deliberately abbreviate).
- Single objects print as key–value dumps.
- The raw credential from ``keys create`` / ``keys rotate`` is
  printed exactly once, prominently — the API never returns it
  again (only its hash is stored).
- Errors print as ``gateway: <message>`` on stderr, exit code 1 — the
  API's own error envelope message, verbatim.
- Export endpoints (``logs export``, ``invoices --csv``,
  ``usage-export``) write to stdout or ``--out FILE`` — pipe-friendly.
- Flags after the subcommand; absent flags are omitted from request
  bodies so a patch touches only what the operator named. The one
  trap is boolean tri-state fields (per-key cache policy): those use
  dedicated flags — ``--cache-bypass`` sends an explicit false,
  ``--cache-inherit`` an explicit null (reset to the global policy)
  — because a plain ``--flag`` can only mean "present".

Deliberately absent: ``playground`` (an interactive chat is not an
operator surface — the dashboard's Playground tab is) and the
promotion applies (``plan`` / ``promote`` / ``rollback`` — they are
confirmation dialogs by design, and the dashboard plus the backups
it renders is the safer story; reads of all their evidence are here).
There is also no ``keys delete``: the API's DELETE is a revoke (the
row and its history stay for audit), so the CLI exposes exactly one
honest word for it — ``keys revoke``.

Zero dependencies beyond the gateway's own: argparse (stdlib) +
httpx (already required by the app). The core is async so tests can
drive it against the in-process app; the console entry point wraps
it in ``asyncio.run``.

The ``gateway`` console script is this admin client only — the
*server* entry point remains ``python -m gateway`` (see
``scripts/gateway.sh``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

DEFAULT_URL = "http://127.0.0.1:8000"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _emit(payload) -> None:
    """--json: the exact payload, pretty-printed."""
    print(json.dumps(payload, indent=2, default=str))


def _maybe_json(args, payload) -> bool:
    """True (and the payload printed) when --json was requested."""
    if args.json:
        _emit(payload)
        return True
    return False


def _render_table(cols: list[tuple[str, str]], rows: list[dict]) -> None:
    """Aligned plain-text table. ``cols`` maps header -> key; missing
    values render as an em dash, never an invented zero."""

    def cell(row: dict, key: str) -> str:
        v = row.get(key)
        if v is None:
            return "—"
        if isinstance(v, bool):
            return "yes" if v else "no"
        if isinstance(v, float):
            return f"{v:g}"
        return str(v)

    body = [[cell(r, k) for _, k in cols] for r in rows]
    widths = []
    for idx, (header, _) in enumerate(cols):
        width = len(header)
        for row in body:
            width = max(width, len(row[idx]))
        widths.append(width)
    print("  ".join(h.ljust(w) for (h, _), w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for row in body:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)))


def _render_kv(obj: dict, indent: str = "") -> None:
    """Key–value dump for single objects; nested structures inline as
    compact JSON so nothing is silently dropped."""
    for key, value in obj.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, default=str)
        elif value is None:
            value = "—"
        elif isinstance(value, bool):
            value = "yes" if value else "no"
        print(f"{indent}{key}: {value}")


def _fmt_usd(v) -> str:
    return "—" if v is None else f"${v:.6f}".rstrip("0").rstrip(".")


def _print_secret_block(row: dict) -> None:
    """The create/rotate response: the raw key shown exactly once."""
    print()
    print("  The raw key below is shown ONCE — store it now; the")
    print("  gateway keeps only its hash and can never return it again.")
    print()
    print(f"    {row.get('key')}")
    print()


def _write_out(text: str, out: str | None) -> None:
    """Export bodies (JSONL/CSV) to stdout, or to a file with --out."""
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        sys.stdout.write(text)


# ---------------------------------------------------------------------------
# HTTP core
# ---------------------------------------------------------------------------


class CliError(Exception):
    """A rendered failure: the API's message, verbatim."""


class Api:
    """The admin API over the injected httpx client — one method per
    verb, the envelope's error.message raised on any non-2xx."""

    def __init__(self, client: httpx.AsyncClient, key: str):
        self._client = client
        self._key = key

    async def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        headers = {}
        if self._key:
            headers["Authorization"] = "Bearer " + self._key
        resp = await self._client.request(
            method, path, params=params, json=json_body, headers=headers
        )
        try:
            payload = resp.json()
        except ValueError:  # non-JSON error page (proxy, crash)
            payload = None
        if resp.status_code >= 400:
            message = (
                (payload or {}).get("error", {}).get("message")
                if isinstance(payload, dict)
                else None
            ) or f"{resp.status_code} {resp.reason_phrase}"
            if resp.status_code == 401 and not self._key:
                message += " (no credential: pass --key or set GATEWAY_ADMIN_KEY)"
            raise CliError(message)
        return payload if isinstance(payload, dict) else {}

    async def get_raw(self, path: str) -> tuple[int, dict]:
        """(status, payload) without raising — for endpoints whose
        non-200 is a payload, not an error envelope (/readyz's 503
        while starting or draining reports state, not failure)."""
        headers = {}
        if self._key:
            headers["Authorization"] = "Bearer " + self._key
        resp = await self._client.get(path, headers=headers)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        return resp.status_code, payload if isinstance(payload, dict) else {}

    async def get(self, path: str, params: dict | None = None) -> dict:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json_body: dict | None = None, params: dict | None = None) -> dict:
        return await self.request("POST", path, params=params, json_body=json_body)

    async def patch(self, path: str, json_body: dict | None = None) -> dict:
        return await self.request("PATCH", path, json_body=json_body)

    async def put(self, path: str, json_body: dict | None = None) -> dict:
        return await self.request("PUT", path, json_body=json_body)

    async def delete(self, path: str) -> dict:
        return await self.request("DELETE", path)

    async def get_text(self, path: str, params: dict | None = None) -> str:
        """Non-JSON bodies: the JSONL capture and the CSV exports."""
        headers = {}
        if self._key:
            headers["Authorization"] = "Bearer " + self._key
        resp = await self._client.request(
            "GET", path, params=params, headers=headers
        )
        if resp.status_code >= 400:
            try:
                message = resp.json()["error"]["message"]
            except Exception:  # noqa: BLE001 — any parse failure falls back
                message = f"{resp.status_code} {resp.reason_phrase}"
            raise CliError(message)
        return resp.text


# ---------------------------------------------------------------------------
# Flag marshaling
# ---------------------------------------------------------------------------


def _csv(value: str | None) -> list[str] | None:
    """Comma-separated CLI flag -> the API's list (None means the
    flag was absent; an empty item list is the server's error)."""
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _json_obj(value: str) -> dict:
    """A JSON-object CLI flag (--prompt-vars, --model-mappings,
    --metadata) parsed to the dict the API expects."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise CliError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CliError("expected a JSON object, e.g. '{\"user\": \"Sam\"}'")
    return parsed


_NULL_WORDS = ("null", "none", "clear")


def _nullable_str(value: str) -> str | None:
    """'null'/'none'/'clear' -> JSON null (clear the field); any other
    string passes through — for the nullable patch fields whose
    explicit null means 'unset' (expires_at, grace_until)."""
    if value.strip().lower() in _NULL_WORDS:
        return None
    return value


def _set(args, body: dict, flag: str, field: str | None = None,
         transform=None) -> None:
    """Copy a provided flag into the request body; absent flags are
    omitted so PATCHes touch only what the operator named."""
    value = getattr(args, flag, None)
    if value is not None:
        if transform is not None:
            value = transform(value)
        body[field or flag] = value


def _params_from(args, *names: str) -> dict:
    """Query params from provided flags only (an absent flag takes the
    server's default; 0 is a value, not an absence)."""
    params = {}
    for name in names:
        value = getattr(args, name, None)
        if value is None or value == "":
            continue
        params[name] = value
    return params


# ---------------------------------------------------------------------------
# Commands: identity + fleet state
# ---------------------------------------------------------------------------


async def cmd_status(api: Api, args) -> None:
    me = await api.get("/admin/whoami")
    health = await api.get("/healthz")
    # /readyz's non-200 is a state report, not an error envelope —
    # read it raw so the NOT-ready line says WHY (draining, db).
    code, ready = await api.get_raw("/readyz")
    if code == 200 and ready.get("ready") is True:
        ready_line = "ready"
    elif "ready" in ready:
        draining = "yes" if ready.get("draining") else "no"
        db = "yes" if ready.get("db") else "no"
        ready_line = f"NOT ready (draining: {draining}, db: {db})"
    else:
        message = (ready.get("error") or {}).get("message") or f"HTTP {code}"
        ready_line = f"NOT ready ({message})"
    if _maybe_json(args, {"whoami": me, "health": health, "readyz": ready}):
        return
    _render_kv(me)
    print(
        f"gateway: {health.get('version')} · "
        f"up {health.get('uptime_seconds')}s · {ready_line}"
    )


async def cmd_stats(api: Api, args) -> None:
    payload = await api.get("/admin/stats", {"hours": args.hours})
    if _maybe_json(args, payload):
        return
    print(f"Requests ({payload.get('window_hours')}h): {payload.get('total_requests')}")
    print(f"Error rate: {payload.get('error_rate')}")
    print(f"Spend: {_fmt_usd(payload.get('cost_usd_total'))}")
    lat = payload.get("latency_ms") or {}
    print(
        f"Latency p50/p95/p99: {lat.get('p50')} / {lat.get('p95')} / {lat.get('p99')} ms"
    )
    ttft = payload.get("ttft_ms") or {}
    if ttft.get("p50") is not None:
        print(f"TTFT p50/p95/p99: {ttft.get('p50')} / {ttft.get('p95')} / {ttft.get('p99')} ms")
    tokens = payload.get("tokens") or {}
    print(f"Tokens prompt/completion: {tokens.get('prompt')} / {tokens.get('completion')}")
    by_model = payload.get("by_model") or []
    if by_model:
        _render_table(
            [("Model", "model"), ("Requests", "requests"),
             ("Tokens", "tokens"), ("Cost", "cost_usd")],
            by_model,
        )


# ---------------------------------------------------------------------------
# Commands: keys
# ---------------------------------------------------------------------------


async def cmd_keys_list(api: Api, args) -> None:
    params = _params_from(args, "tenant_id", "idle_days")
    payload = await api.get("/admin/keys", params)
    if _maybe_json(args, payload):
        return
    rows = payload.get("data") or []
    _render_table(
        [("Name", "name"), ("Tenant", "tenant_id"), ("Active", "is_active"),
         ("Admin", "is_admin"), ("RPM", "rpm_limit"), ("TPM", "tpm_limit"),
         ("Conc", "max_concurrency"), ("Spend", "spend_usd"),
         ("Budget", "max_budget_usd"), ("Expires", "expires_at"),
         ("Last used", "last_used_at")],
        rows,
    )
    print(f"({len(rows)} key(s))")


async def cmd_keys_show(api: Api, args) -> None:
    payload = await api.get(f"/admin/keys/{args.key_id}")
    if _maybe_json(args, payload):
        return
    _render_kv(payload)


async def cmd_keys_create(api: Api, args) -> None:
    body: dict = {"name": args.name}
    _set(args, body, "is_admin")  # default=None: absent flag = absent field
    _set(args, body, "allowed_models", "allowed_models", _csv)
    _set(args, body, "allowed_tools", "allowed_tools", _csv)
    _set(args, body, "allowed_mcp_servers", "allowed_mcp_servers", _csv)
    _set(args, body, "rpm_limit")
    _set(args, body, "tpm_limit")
    _set(args, body, "max_concurrency")
    _set(args, body, "priority")
    _set(args, body, "max_budget_usd")
    _set(args, body, "daily_budget_usd")
    _set(args, body, "monthly_budget_usd")
    _set(args, body, "expires_at")
    _set(args, body, "metadata", transform=_json_obj)
    _set(args, body, "disabled_guardrails", "disabled_guardrails", _csv)
    if args.cache_bypass:
        body["cache_enabled"] = False
    _set(args, body, "cache_ttl_seconds")
    _set(args, body, "required_residencies", "required_residencies", _csv)
    _set(args, body, "model_mappings", transform=_json_obj)
    _set(args, body, "default_model")
    _set(args, body, "system_prompt")
    _set(args, body, "system_prompt_ref")
    _set(args, body, "prompt_variables", transform=_json_obj)
    _set(args, body, "allowed_endpoints", "allowed_endpoints", _csv)
    _set(args, body, "allowed_ips", "allowed_ips", _csv)
    _set(args, body, "tenant_id")
    payload = await api.post("/admin/keys", body)
    if _maybe_json(args, payload):
        return
    print(f"Created key '{payload.get('name')}' (id {payload.get('id')}).")
    _print_secret_block(payload)


async def cmd_keys_edit(api: Api, args) -> None:
    body: dict = {}
    for flag in (
        "allowed_models", "allowed_tools", "allowed_mcp_servers", "rpm_limit",
        "tpm_limit", "max_concurrency", "priority", "max_budget_usd",
        "daily_budget_usd", "monthly_budget_usd", "disabled_guardrails",
        "cache_ttl_seconds", "required_residencies", "allowed_endpoints",
        "allowed_ips", "tenant_id", "system_prompt", "system_prompt_ref",
    ):
        transform = _csv if flag in {
            "allowed_models", "allowed_tools", "allowed_mcp_servers",
            "disabled_guardrails", "required_residencies", "allowed_endpoints",
            "allowed_ips",
        } else None
        _set(args, body, flag, transform=transform)
    for flag in ("model_mappings", "prompt_variables"):
        _set(args, body, flag, transform=_json_obj)
    _set(args, body, "default_model")
    if args.expires_at is not None:
        body["expires_at"] = _nullable_str(args.expires_at)
    if args.grace_until is not None:
        body["grace_until"] = _nullable_str(args.grace_until)
    if args.cache_bypass:
        body["cache_enabled"] = False
    if args.cache_inherit:
        body["cache_enabled"] = None  # explicit null: back to the global policy
    if args.inactive:
        body["is_active"] = False
    if args.active:
        body["is_active"] = True
    if not body:
        raise CliError("nothing to change: pass at least one field flag")
    payload = await api.patch(f"/admin/keys/{args.key_id}", body)
    if _maybe_json(args, payload):
        return
    print(f"Updated key '{payload.get('name')}'.")


async def cmd_keys_rotate(api: Api, args) -> None:
    payload = await api.post(
        f"/admin/keys/{args.key_id}/rotate", {"grace_seconds": args.grace_seconds}
    )
    if _maybe_json(args, payload):
        return
    grace = (
        f" (previous credential stays live for {args.grace_seconds}s)"
        if args.grace_seconds else ""
    )
    print(f"Rotated key '{payload.get('name')}'{grace}.")
    _print_secret_block(payload)


async def cmd_keys_revoke(api: Api, args) -> None:
    payload = await api.patch(f"/admin/keys/{args.key_id}", {"is_active": False})
    if _maybe_json(args, payload):
        return
    print(f"Revoked key '{payload.get('name')}' (is_active: no).")


# ---------------------------------------------------------------------------
# Commands: tenants
# ---------------------------------------------------------------------------


async def cmd_tenants_list(api: Api, args) -> None:
    payload = await api.get("/admin/tenants")
    if _maybe_json(args, payload):
        return
    rows = payload.get("data") or []
    _render_table(
        [("Name", "name"), ("Display", "display_name"), ("Active", "is_active"),
         ("Monthly spend", "monthly_spend_usd"), ("Monthly budget", "monthly_budget_usd"),
         ("Markup %", "pricing_markup_pct"), ("RPM", "rpm_limit"),
         ("TPM", "tpm_limit"), ("Conc", "max_concurrency")],
        rows,
    )
    print(f"({len(rows)} tenant(s))")


async def cmd_tenants_show(api: Api, args) -> None:
    payload = await api.get(f"/admin/tenants/{args.tenant_id}")
    if _maybe_json(args, payload):
        return
    _render_kv(payload)


async def cmd_tenants_create(api: Api, args) -> None:
    body: dict = {"name": args.name}
    _set(args, body, "display_name")
    _set(args, body, "allowed_models", "allowed_models", _csv)
    _set(args, body, "allowed_tools", "allowed_tools", _csv)
    _set(args, body, "allowed_mcp_servers", "allowed_mcp_servers", _csv)
    _set(args, body, "rpm_limit")
    _set(args, body, "tpm_limit")
    _set(args, body, "pricing_markup_pct")
    _set(args, body, "max_concurrency")
    _set(args, body, "max_budget_usd")
    _set(args, body, "daily_budget_usd")
    _set(args, body, "monthly_budget_usd")
    _set(args, body, "required_residencies", "required_residencies", _csv)
    if args.inactive:
        body["is_active"] = False
    payload = await api.post("/admin/tenants", body)
    if _maybe_json(args, payload):
        return
    print(f"Created tenant '{payload.get('name')}' (id {payload.get('id')}).")


async def cmd_tenants_edit(api: Api, args) -> None:
    body: dict = {}
    for flag in (
        "display_name", "allowed_models", "allowed_tools",
        "allowed_mcp_servers", "rpm_limit", "tpm_limit",
        "pricing_markup_pct", "max_concurrency", "max_budget_usd",
        "daily_budget_usd", "monthly_budget_usd", "required_residencies",
    ):
        transform = _csv if flag in {
            "allowed_models", "allowed_tools", "allowed_mcp_servers",
            "required_residencies",
        } else None
        _set(args, body, flag, transform=transform)
    if args.inactive:
        body["is_active"] = False
    if args.active:
        body["is_active"] = True
    if not body:
        raise CliError("nothing to change: pass at least one field flag")
    payload = await api.patch(f"/admin/tenants/{args.tenant_id}", body)
    if _maybe_json(args, payload):
        return
    print(f"Updated tenant '{payload.get('name')}'.")


async def cmd_tenants_delete(api: Api, args) -> None:
    payload = await api.delete(f"/admin/tenants/{args.tenant_id}")
    if _maybe_json(args, payload):
        return
    print(f"Deleted tenant '{args.tenant_id}'.")


# ---------------------------------------------------------------------------
# Commands: BYOK vault
# ---------------------------------------------------------------------------


async def cmd_vault_status(api: Api, args) -> None:
    payload = await api.get("/admin/vault")
    if _maybe_json(args, payload):
        return
    _render_kv(payload)


async def cmd_vault_list(api: Api, args) -> None:
    payload = await api.get(f"/admin/tenants/{args.tenant_id}/provider-keys")
    if _maybe_json(args, payload):
        return
    _render_table(
        [("Provider", "provider"), ("Label", "label"), ("Key hint", "key_hint"),
         ("Updated", "updated_at")],
        payload.get("data") or [],
    )


async def cmd_vault_put(api: Api, args) -> None:
    value = args.value
    if value is None:
        if sys.stdin.isatty():
            raise CliError(
                "refusing to read a secret from an interactive terminal "
                "(shell history): pass --value via env/secret expansion, "
                "or pipe the credential on stdin"
            )
        value = sys.stdin.read().strip()
    if not value:
        raise CliError("empty provider key: pipe the value on stdin or pass --value")
    body: dict = {"key": value}
    _set(args, body, "label")
    payload = await api.put(
        f"/admin/tenants/{args.tenant_id}/provider-keys/{args.provider}", body
    )
    if _maybe_json(args, payload):
        return
    print(f"Stored {args.provider} credential for tenant {args.tenant_id}:")
    _render_kv(payload)


async def cmd_vault_delete(api: Api, args) -> None:
    payload = await api.delete(
        f"/admin/tenants/{args.tenant_id}/provider-keys/{args.provider}"
    )
    if _maybe_json(args, payload):
        return
    print(f"Deleted {args.provider} credential for tenant {args.tenant_id}.")


# ---------------------------------------------------------------------------
# Commands: prompt library
# ---------------------------------------------------------------------------


async def cmd_prompts_list(api: Api, args) -> None:
    payload = await api.get("/admin/prompts")
    if _maybe_json(args, payload):
        return
    # Summary rows carry the full active/candidate version dicts —
    # the table shows their version numbers, --json the whole truth.
    rows = [
        {
            "name": row.get("name"),
            "versions": row.get("versions"),
            "active": (row.get("active") or {}).get("version"),
            "candidate": (row.get("candidate") or {}).get("version"),
        }
        for row in payload.get("data") or []
    ]
    _render_table(
        [("Name", "name"), ("Versions", "versions"),
         ("Active", "active"), ("Candidate", "candidate")],
        rows,
    )


async def cmd_prompts_show(api: Api, args) -> None:
    payload = await api.get(f"/admin/prompts/{args.name}")
    if _maybe_json(args, payload):
        return
    _render_table(
        [("Version", "version"), ("Active", "is_active"),
         ("Candidate", "is_candidate"), ("A/B", "ab_fraction"),
         ("Notes", "notes"), ("Created", "created_at")],
        payload.get("data") or [],
    )


async def cmd_prompts_create(api: Api, args) -> None:
    body_text = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else args.body
    if not body_text or not body_text.strip():
        raise CliError("empty prompt body: pass --body or --body-file")
    body: dict = {"name": args.name, "body": body_text}
    _set(args, body, "notes")
    payload = await api.post("/admin/prompts", body)
    if _maybe_json(args, payload):
        return
    print(f"Version {payload.get('version')} of '{args.name}' created"
          + (" and activated" if payload.get("is_active") else "")
          + f" (id {payload.get('id')}).")


async def cmd_prompts_edit(api: Api, args) -> None:
    body: dict = {}
    if args.activate:
        body["activate"] = True
    if args.deactivate:
        body["activate"] = False
    if args.candidate is not None:
        body["candidate"] = args.candidate
    _set(args, body, "ab_fraction")
    _set(args, body, "notes")
    if not body:
        raise CliError(
            "nothing to change: pass --activate / --deactivate / "
            "--candidate true|false / --ab-fraction / --notes"
        )
    payload = await api.patch(f"/admin/prompts/{args.prompt_id}", body)
    if _maybe_json(args, payload):
        return
    print(f"Updated version {payload.get('version')} of '{payload.get('name')}'.")


async def cmd_prompts_delete(api: Api, args) -> None:
    payload = await api.delete(f"/admin/prompts/{args.prompt_id}")
    if _maybe_json(args, payload):
        return
    print(f"Deleted prompt version {args.prompt_id}.")


# ---------------------------------------------------------------------------
# Commands: logs, capture/replay
# ---------------------------------------------------------------------------


def _log_filters(args) -> dict:
    return _params_from(
        args, "limit", "offset", "status", "key_name", "key_id", "model",
        "request_id", "trace_id", "tenant_id",
    )


async def cmd_logs_list(api: Api, args) -> None:
    payload = await api.get("/admin/logs", _log_filters(args))
    if _maybe_json(args, payload):
        return
    _render_table(
        [("Id", "id"), ("When", "created_at"), ("Key", "key_name"),
         ("Model", "model"), ("Deployment", "deployment"),
         ("Status", "status"), ("Prompt tok", "prompt_tokens"),
         ("Completion tok", "completion_tokens"), ("Cost", "cost_usd"),
         ("Latency", "latency_ms")],
        payload.get("data") or [],
    )
    print(f"({payload.get('total')} row(s) total)")


async def cmd_logs_show(api: Api, args) -> None:
    payload = await api.get(f"/admin/logs/{args.log_id}")
    if _maybe_json(args, payload):
        return
    _render_kv(payload)


async def cmd_logs_export(api: Api, args) -> None:
    text = await api.get_text("/admin/logs/export", _log_filters(args))
    _write_out(text, args.out)


async def cmd_replay(api: Api, args) -> None:
    path = Path(args.file)
    if not path.is_file():
        raise CliError(f"capture file not found: {args.file}")
    entries = []
    with path.open(encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise CliError(f"capture line {number} is not JSON: {exc}") from exc
    if not entries:
        raise CliError(f"no capture lines in {args.file}")
    report = await api.post("/admin/replay", {"entries": entries})
    if _maybe_json(args, report):
        return
    print(
        f"replayed {report.get('replayed')} · changed {report.get('changed')}"
        f" · unchanged {report.get('unchanged')}"
        f" · not comparable {report.get('not_comparable')}"
        f" · errors {report.get('errors')}"
    )
    for line in report.get("results") or []:
        if line.get("result") in ("changed", "error"):
            fields = ", ".join(line.get("changes") or [])
            why = line.get("skip_reason") or line.get("detail") or ""
            print(f"  [{line.get('result')}] {fields or why}"
                  + (f" — {why}" if fields and why else ""))
    changed = report.get("changed") or 0
    print(
        "changed lines are what the CURRENT config would do differently"
        + (" — review before a hot reload." if changed else " — nothing would change.")
    )


# ---------------------------------------------------------------------------
# Commands: feedback
# ---------------------------------------------------------------------------


async def cmd_feedback_list(api: Api, args) -> None:
    params = _params_from(
        args, "limit", "offset", "key_name", "model", "request_id",
        "min_rating", "max_rating", "tenant_id",
    )
    payload = await api.get("/admin/feedback", params)
    if _maybe_json(args, payload):
        return
    _render_table(
        [("When", "created_at"), ("Rating", "rating"), ("Key", "key_name"),
         ("Model", "model"), ("Request", "request_id"), ("Comment", "comment")],
        payload.get("data") or [],
    )
    print(f"({payload.get('total')} row(s) total)")


async def cmd_feedback_stats(api: Api, args) -> None:
    params = _params_from(args, "key_name", "model", "tenant_id")
    payload = await api.get("/admin/feedback/stats", params)
    if _maybe_json(args, payload):
        return
    overall = payload.get("overall") or {}
    if not overall.get("count"):
        print("no feedback yet.")
        return
    print(
        f"ratings: {overall.get('count')} total · average {overall.get('avg_rating')}"
        f" · {overall.get('pct_positive')}% positive · {overall.get('pct_negative')}% negative"
    )
    for title, section in (("model", "by_model"), ("key", "by_key")):
        for name, bucket in (payload.get(section) or {}).items():
            print(
                f"  [{title}] {name}: {bucket.get('avg_rating')} avg of"
                f" {bucket.get('count')} · {bucket.get('pct_positive')}% pos /"
                f" {bucket.get('pct_negative')}% neg"
            )


# ---------------------------------------------------------------------------
# Commands: fleet state (routing, breakers, guardrails, canaries,
# runaway, concurrency, MCP, SLOs, PII)
# ---------------------------------------------------------------------------


async def cmd_routing(api: Api, args) -> None:
    payload = await api.get("/admin/routing")
    if _maybe_json(args, payload):
        return
    cb = payload.get("circuit_breaker") or {}
    print(f"cost-aware routing: {'on' if payload.get('cost_aware') else 'off'} · "
          f"circuit breakers: {'on' if cb.get('enabled') else 'off'}")
    rows = []
    for model in payload.get("models") or []:
        for dep in model.get("deployments") or []:
            rows.append({
                "model": model.get("name"),
                "name": dep.get("name"),
                "provider": dep.get("provider"),
                "dep_model": dep.get("model"),
                "priority": dep.get("priority"),
                "weight": dep.get("weight"),
                "shadow": dep.get("shadow"),
                "breaker": (dep.get("breaker") or {}).get("state"),
            })
    _render_table(
        [("Model", "model"), ("Deployment", "name"), ("Provider", "provider"),
         ("Upstream", "dep_model"), ("Prio", "priority"), ("Weight", "weight"),
         ("Shadow", "shadow"), ("Breaker", "breaker")],
        rows,
    )


async def cmd_breakers_reset(api: Api, args) -> None:
    payload = await api.post(f"/admin/routing/breakers/{args.deployment}/reset")
    if _maybe_json(args, payload):
        return
    print(f"Breaker for '{payload.get('deployment')}' force-closed "
          "(the next request probes it again):")
    _render_kv(payload.get("breaker") or {})


async def cmd_guardrails(api: Api, args) -> None:
    payload = await api.get("/admin/guardrails")
    if _maybe_json(args, payload):
        return
    _render_table(
        [("Name", "name"), ("Mode", "mode"), ("Scope", "scope"),
         ("Description", "description")],
        payload.get("guardrails") or [],
    )


async def cmd_canaries(api: Api, args) -> None:
    payload = await api.get("/admin/canaries")
    if _maybe_json(args, payload):
        return
    print(f"canary probes: {'enabled' if payload.get('enabled') else 'disabled'} · "
          f"interval {payload.get('interval_seconds')}s · "
          f"timeout {payload.get('timeout_seconds')}s")
    _render_table(
        [("Deployment", "deployment"), ("Provider", "provider"),
         ("Model", "model"), ("Outcome", "outcome"), ("Healthy", "healthy"),
         ("Latency", "latency_ms"), ("Checked", "checked_at"),
         ("Error", "error")],
        payload.get("data") or [],
    )


async def cmd_canaries_probe(api: Api, args) -> None:
    payload = await api.post("/admin/canaries/probe")
    if _maybe_json(args, payload):
        return
    print(f"probed {payload.get('probed')} deployment(s), "
          f"{payload.get('healthy')} healthy.")
    _render_table(
        [("Deployment", "deployment"), ("Outcome", "outcome"),
         ("Healthy", "healthy"), ("Latency", "latency_ms"),
         ("Error", "error")],
        payload.get("data") or [],
    )


async def cmd_runaway(api: Api, args) -> None:
    payload = await api.get("/admin/runaway")
    if _maybe_json(args, payload):
        return
    print(f"runaway protection: {'on' if payload.get('enabled') else 'off'}")
    jailed = payload.get("jailed") or []
    if not jailed:
        print("no key is jailed — nothing is looping.")
        return
    _render_table(
        [("Key", "key_name"), ("Id", "key_id"),
         ("Cooldown left (s)", "retry_after_seconds"), ("Offenses", "offenses")],
        jailed,
    )


async def cmd_runaway_release(api: Api, args) -> None:
    payload = await api.post(f"/admin/runaway/{args.key_id}/release")
    if _maybe_json(args, payload):
        return
    print(f"Released key {args.key_id} from the runaway jail.")


async def cmd_concurrency(api: Api, args) -> None:
    payload = await api.get("/admin/concurrency")
    if _maybe_json(args, payload):
        return
    print(f"admission: {'on' if payload.get('enabled') else 'off'} · "
          f"wait {payload.get('wait_seconds')}s · "
          f"queued total {payload.get('queued_total')}")
    keys = payload.get("keys") or []
    tenants = payload.get("tenants") or []
    if keys:
        print("keys holding slots")
        _render_table(
            [("Key", "key_name"), ("In-flight", "inflight"), ("Queued", "queued")],
            keys,
        )
    if tenants:
        print("tenants holding slots")
        _render_table([("Tenant", "tenant_name"), ("In-flight", "inflight")], tenants)
    if not keys and not tenants:
        print("no key or tenant is holding slots right now.")


async def cmd_mcp(api: Api, args) -> None:
    payload = await api.get("/admin/mcp")
    if _maybe_json(args, payload):
        return
    cb = payload.get("circuit_breaker") or {}
    print(f"mcp proxy: {'on' if payload.get('enabled') else 'off'} · "
          f"session ttl {payload.get('session_ttl_seconds')}s · "
          f"circuit breakers: {'on' if cb.get('enabled') else 'off'}")
    servers = payload.get("servers") or []
    if not servers:
        print("no MCP servers configured.")
        return
    for server in servers:
        headers = ", ".join(server.get("header_names") or []) or "—"
        breaker = (server.get("breaker") or {}).get("state") or "—"
        print(f"  {server.get('name')}: {server.get('url')} "
              f"(timeout {server.get('timeout_seconds')}s, "
              f"auth headers: {headers}, "
              f"open sessions: {server.get('open_sessions')}, "
              f"breaker: {breaker})")


async def cmd_slos(api: Api, args) -> None:
    payload = await api.get("/admin/slos")
    if _maybe_json(args, payload):
        return
    _render_table(
        [("Name", "name"), ("Model", "model"), ("Metric", "metric"),
         ("Target", "target"), ("Window (min)", "window_minutes"),
         ("Value", "value"), ("Verdict", "verdict")],
        payload.get("data") or [],
    )


async def cmd_pii_scan(api: Api, args) -> None:
    if args.log_id is not None:
        payload = await api.get(f"/admin/logs/{args.log_id}/pii-scan")
        if _maybe_json(args, payload):
            return
        _render_kv(payload)
        return
    params = _params_from(args, "limit", "key_name", "model", "status", "tenant_id")
    payload = await api.get("/admin/pii-scan", params)
    if _maybe_json(args, payload):
        return
    print(f"scanned {payload.get('scanned')} row(s) · "
          f"{payload.get('with_findings')} with findings · "
          f"{payload.get('bodies_not_captured')} without stored bodies")
    by_kind = payload.get("by_kind") or {}
    if by_kind:
        print("findings by kind: " + ", ".join(f"{k}×{v}" for k, v in by_kind.items()))
    else:
        print("no findings — the scanned bodies are clean.")


# ---------------------------------------------------------------------------
# Commands: billing & finops
# ---------------------------------------------------------------------------


async def cmd_invoices(api: Api, args) -> None:
    params: dict = {}
    _set(args, params, "month")
    _set(args, params, "tenant_id")
    if args.csv:
        text = await api.get_text("/admin/invoices", {**params, "format": "csv"})
        _write_out(text, args.out)
        return
    payload = await api.get("/admin/invoices", params)
    if _maybe_json(args, payload):
        return
    period = payload.get("period") or {}
    print(f"invoice {payload.get('month')} · "
          f"{period.get('since')} → {period.get('until')}")
    totals = payload.get("totals") or {}
    print(f"requests {totals.get('requests')} · errors {totals.get('errors')} · "
          f"tokens {totals.get('total_tokens')} · "
          f"cost {_fmt_usd(totals.get('cost_usd'))}")
    rows = []
    for tenant_section in payload.get("tenants") or []:
        tenant = tenant_section.get("tenant_name") or tenant_section.get("tenant_id")
        for key_section in tenant_section.get("keys") or []:
            for item in key_section.get("line_items") or []:
                rows.append({
                    "tenant": tenant,
                    "key": key_section.get("key_name"),
                    "model": item.get("model"),
                    "requests": item.get("requests"),
                    "total_tokens": item.get("total_tokens"),
                    "cost_usd": item.get("cost_usd"),
                })
    for key_section in payload.get("unassigned") or []:
        for item in key_section.get("line_items") or []:
            rows.append({
                "tenant": "—",
                "key": key_section.get("key_name"),
                "model": item.get("model"),
                "requests": item.get("requests"),
                "total_tokens": item.get("total_tokens"),
                "cost_usd": item.get("cost_usd"),
            })
    _render_table(
        [("Tenant", "tenant"), ("Key", "key"), ("Model", "model"),
         ("Requests", "requests"), ("Tokens", "total_tokens"), ("Cost", "cost_usd")],
        rows,
    )


async def cmd_anomalies(api: Api, args) -> None:
    params = _params_from(args, "tenant_id")
    payload = await api.get("/admin/anomalies", params)
    if _maybe_json(args, payload):
        return
    flags = payload.get("flags") or []
    if not flags:
        print("no flags — the good case.")
        return
    for flag in flags:
        print(f"[{flag.get('kind')}] {flag.get('message')}")
        print(f"    window {flag.get('window')} · baseline {flag.get('baseline')}"
              f" · ratio {flag.get('ratio')}")


async def cmd_forecasts(api: Api, args) -> None:
    params = _params_from(args, "month", "tenant_id")
    payload = await api.get("/admin/forecasts", params)
    if _maybe_json(args, payload):
        return

    def pace(block: dict, indent: str = "  ") -> None:
        """The two projections every section carries (run-rate and
        recent pace), plus the trend ratio where one exists."""
        for name in ("run_rate", "recent_pace"):
            sub = block.get(name) or {}
            if sub.get("daily_usd") is not None:
                print(f"{indent}{name.replace('_', ' ')}: "
                      f"{_fmt_usd(sub.get('daily_usd'))}/day → projects "
                      f"{_fmt_usd(sub.get('projected_month_usd'))} for the month")
        if block.get("trend_ratio") is not None:
            print(f"{indent}trend: {block.get('trend_ratio')}x "
                  "(recent daily vs month-to-date daily)")

    if payload.get("month_complete"):
        print(f"month {payload.get('month')} — complete: the invoice is the final bill")
    else:
        print(f"month {payload.get('month')} · "
              f"{payload.get('elapsed_days')}/{payload.get('days_in_month')} days elapsed")
    print("totals:")
    pace(payload.get("totals") or {})
    for title in ("tenants", "unassigned"):
        for section in payload.get(title) or []:
            name = section.get("tenant_name") or section.get("tenant_id") or "unassigned"
            print(f"{name}:")
            pace(section)
            if section.get("verdict"):
                pct = section.get("projected_pct_of_budget")
                print(f"  verdict: {section.get('verdict')}"
                      + (f" ({pct}% of budget)" if pct is not None else "")
                      + f" — based on {section.get('verdict_based_on')}")


async def cmd_usage_export(api: Api, args) -> None:
    params: dict = {"format": args.format}
    _set(args, params, "since")
    _set(args, params, "until")
    _set(args, params, "key_name")
    _set(args, params, "model")
    _set(args, params, "status")
    _set(args, params, "tenant_id")
    if args.include_shadow:
        params["include_shadow"] = "true"
    text = await api.get_text("/admin/usage/export", params)
    _write_out(text, args.out)


# ---------------------------------------------------------------------------
# Commands: migration evidence (reads) + trials
# ---------------------------------------------------------------------------


async def cmd_shadow_diff(api: Api, args) -> None:
    params = _params_from(args, "model", "since")
    payload = await api.get("/admin/shadow-diff", params)
    if _maybe_json(args, payload):
        return
    _render_table(
        [("Model", "model"), ("Deployment", "deployment"), ("Provider", "provider"),
         ("Pairs", "pairs"), ("Exact", "exact"), ("Near", "near"),
         ("Divergent", "divergent"), ("Match", "match_rate"),
         ("Similarity", "similarity_mean"), ("Latency Δms", "latency_delta_ms")],
        payload.get("groups") or [],
    )


def _recommendation(row: dict) -> dict:
    return row.get("recommendation") or {}


async def cmd_verdicts(api: Api, args) -> None:
    params = _params_from(args, "model", "deployment", "since")
    payload = await api.get("/admin/migration/verdicts", params)
    if _maybe_json(args, payload):
        return
    rows = payload.get("data") or []
    _render_table(
        [("Model", "model"), ("Deployment", "deployment"), ("Pairs", "pairs"),
         ("Match", "match_rate"), ("Divergent", "divergent"),
         ("Verdict", "verdict")],
        [
            {
                "model": r.get("model"),
                "deployment": r.get("deployment"),
                "pairs": r.get("pairs"),
                "match_rate": r.get("match_rate"),
                "divergent": r.get("divergent"),
                "verdict": _recommendation(r).get("verdict"),
            }
            for r in rows
        ],
    )
    for r in rows:
        for reason in _recommendation(r).get("reasons") or []:
            print(f"  {r.get('deployment')}: {reason}")


async def cmd_watch(api: Api, args) -> None:
    params = _params_from(args, "model", "deployment", "since")
    payload = await api.get("/admin/migration/watch", params)
    if _maybe_json(args, payload):
        return
    _render_table(
        [("Model", "model"), ("Deployment", "deployment"),
         ("Requests", "requests"), ("Errors", "errors"),
         ("Error rate", "error_rate"), ("Latency mean", "latency_mean_ms"),
         ("Expected share", "expected_share"), ("Actual share", "actual_share"),
         ("Verdict", "verdict")],
        [
            {
                "model": r.get("model"),
                "deployment": r.get("deployment"),
                "requests": r.get("requests"),
                "errors": r.get("errors"),
                "error_rate": r.get("error_rate"),
                "latency_mean_ms": r.get("latency_mean_ms"),
                "expected_share": r.get("expected_share"),
                "actual_share": r.get("actual_share"),
                "verdict": _recommendation(r).get("verdict"),
            }
            for r in payload.get("data") or []
        ],
    )


async def cmd_pairs(api: Api, args) -> None:
    if args.trial_id:
        params = _params_from(args, "verdict", "limit", "offset")
        payload = await api.get(f"/admin/trials/{args.trial_id}/pairs", params)
    else:
        if not args.model or not args.deployment:
            raise CliError("pairs needs --model and --deployment (or --trial-id)")
        params = _params_from(args, "model", "deployment", "verdict", "limit", "offset")
        payload = await api.get("/admin/shadow-diff/pairs", params)
    if _maybe_json(args, payload):
        return
    # Both sides' status live in nested primary/shadow dicts — the
    # table shows the scannable statuses; --json has the excerpts.
    _render_table(
        [("Request", "request_id"), ("Verdict", "verdict"),
         ("Similarity", "similarity"), ("Primary", "primary"), ("Shadow", "shadow")],
        [
            {
                "request_id": r.get("request_id"),
                "verdict": r.get("verdict"),
                "similarity": r.get("similarity"),
                "primary": (r.get("primary") or {}).get("status"),
                "shadow": (r.get("shadow") or {}).get("status"),
            }
            for r in payload.get("data") or []
        ],
    )
    print(f"({payload.get('total')} pair(s) total)")


async def cmd_trials_create(api: Api, args) -> None:
    body: dict = {"model": args.model, "deployment": args.deployment}
    _set(args, body, "since")
    _set(args, body, "until")
    _set(args, body, "limit")
    _set(args, body, "concurrency")
    _set(args, body, "max_cost_usd")
    payload = await api.post("/admin/trials", body)
    if _maybe_json(args, payload):
        return
    print(f"trial {payload.get('id')} started: replaying '{args.model}' traffic "
          f"against candidate '{args.deployment}'.")


async def cmd_trials_list(api: Api, args) -> None:
    params = _params_from(args, "limit", "offset")
    payload = await api.get("/admin/trials", params)
    if _maybe_json(args, payload):
        return
    _render_table(
        [("Id", "id"), ("Model", "model"), ("Deployment", "deployment"),
         ("Status", "status"), ("Stop reason", "stop_reason"),
         ("Selected", "selected"), ("Completed", "completed"),
         ("Skipped", "skipped"), ("Cost", "cost_usd")],
        payload.get("data") or [],
    )
    print(f"({payload.get('total')} trial(s) total)")


async def cmd_trials_show(api: Api, args) -> None:
    payload = await api.get(f"/admin/trials/{args.trial_id}")
    if _maybe_json(args, payload):
        return
    _render_kv(payload)


async def cmd_trials_cancel(api: Api, args) -> None:
    payload = await api.post(f"/admin/trials/{args.trial_id}/cancel")
    if _maybe_json(args, payload):
        return
    print(f"cancelled trial {args.trial_id} (stops within one wave).")


# ---------------------------------------------------------------------------
# Commands: operations (reload, backups)
# ---------------------------------------------------------------------------


async def cmd_reload(api: Api, args) -> None:
    payload = await api.post("/admin/reload")
    if _maybe_json(args, payload):
        return
    restart = payload.get("restart_required") or []
    print(f"reloaded from {payload.get('config_path')} — "
          f"{len(payload.get('models') or [])} model(s), "
          f"{len(payload.get('guardrails') or [])} guardrail(s).")
    if restart:
        print(f"restart required for: {', '.join(restart)}")


async def cmd_backups_list(api: Api, args) -> None:
    payload = await api.get("/admin/backups")
    if _maybe_json(args, payload):
        return
    _render_table(
        [("Backup", "name"), ("Size", "size_bytes"), ("Created", "created_at")],
        payload.get("data") or [],
    )


async def cmd_backups_create(api: Api, args) -> None:
    payload = await api.post("/admin/backup")
    if _maybe_json(args, payload):
        return
    print(f"created backup {payload.get('name')} "
          f"({payload.get('size_bytes')} bytes).")


async def cmd_backups_delete(api: Api, args) -> None:
    payload = await api.delete(f"/admin/backups/{args.name}")
    if _maybe_json(args, payload):
        return
    print(f"deleted backup {args.name}.")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true",
        help="print the exact API payload instead of the table/summary",
    )
    common.add_argument(
        "--url", default=os.environ.get("GATEWAY_URL", DEFAULT_URL),
        help=f"gateway base URL (default: {DEFAULT_URL}, env GATEWAY_URL)",
    )
    common.add_argument(
        "--key", default=os.environ.get("GATEWAY_ADMIN_KEY", ""),
        help="admin key (env GATEWAY_ADMIN_KEY)",
    )
    common.add_argument(
        "--timeout", type=float, default=30.0,
        help="request timeout in seconds (default 30)",
    )

    p = argparse.ArgumentParser(
        prog="gateway",
        description="Operate a running LLM Gateway over its JSON admin API.",
        epilog="Flags go after the subcommand; set GATEWAY_URL and "
               "GATEWAY_ADMIN_KEY to skip --url/--key entirely.",
    )
    sub = p.add_subparsers(dest="group", required=True, metavar="group")

    # status / stats -------------------------------------------------------
    sp = sub.add_parser("status", help="who am I, and is the gateway up and ready",
                        parents=[common])
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("stats", help="traffic stats for a rolling window",
                        parents=[common])
    sp.add_argument("--hours", type=int, default=24)
    sp.set_defaults(func=cmd_stats)

    # keys ------------------------------------------------------------------
    g = sub.add_parser("keys", help="virtual keys: list / show / create / edit /"
                                    " rotate / revoke")
    gsub = g.add_subparsers(dest="action", required=True, metavar="action")

    def key_policy_fields(parser: argparse.ArgumentParser) -> None:
        """Fields shared by keys create and keys edit (both schemas)."""
        parser.add_argument("--models", dest="allowed_models",
                            help="allowed models (fnmatch patterns, comma-separated)")
        parser.add_argument("--tools", dest="allowed_tools",
                            help="allowed tool names (fnmatch, comma-separated)")
        parser.add_argument("--mcp-servers", dest="allowed_mcp_servers",
                            help="allowed MCP servers (fnmatch, comma-separated)")
        parser.add_argument("--rpm", dest="rpm_limit", type=int,
                            help="requests per minute cap")
        parser.add_argument("--tpm", dest="tpm_limit", type=int,
                            help="tokens per minute cap")
        parser.add_argument("--conc", dest="max_concurrency", type=int,
                            help="max simultaneous upstream requests")
        parser.add_argument("--priority", type=int,
                            help="QoS priority under concurrency contention (-1000..1000)")
        parser.add_argument("--budget", dest="max_budget_usd", type=float,
                            help="lifetime spend cap (USD)")
        parser.add_argument("--daily", dest="daily_budget_usd", type=float,
                            help="UTC calendar-day spend cap (USD)")
        parser.add_argument("--monthly", dest="monthly_budget_usd", type=float,
                            help="UTC calendar-month spend cap (USD)")
        parser.add_argument("--disabled-guardrails",
                            help="guardrail names to skip (comma-separated)")
        parser.add_argument("--cache-ttl", dest="cache_ttl_seconds", type=int,
                            help="per-key cache window (seconds)")
        parser.add_argument("--residency", dest="required_residencies",
                            help="deployment residency tags required (comma-separated)")
        parser.add_argument("--endpoints", dest="allowed_endpoints",
                            help="data-plane scopes allowed (comma-separated: chat, embeddings, ...)")
        parser.add_argument("--ips", dest="allowed_ips",
                            help="source IP/CIDR allowlist (comma-separated)")
        parser.add_argument("--system-prompt", dest="system_prompt",
                            help="inline system prompt for requests that carry none")
        parser.add_argument("--prompt-ref", dest="system_prompt_ref",
                            help="named library prompt whose ACTIVE version serves")
        parser.add_argument("--prompt-vars", dest="prompt_variables",
                            help="{{variable}} substitutions, as a JSON object string")
        parser.add_argument("--model-mappings",
                            help="per-key model routing, as a JSON object string")
        parser.add_argument("--default-model",
                            help="this key's catch-all model for unknown names")

    sp = gsub.add_parser("list", help="list keys", parents=[common])
    sp.add_argument("--tenant", dest="tenant_id", help="only this tenant's keys")
    sp.add_argument("--idle-days", dest="idle_days", type=float,
                    help="only keys unused for N days (never-used keys included)")
    sp.set_defaults(func=cmd_keys_list)

    sp = gsub.add_parser("show", help="one key, full row", parents=[common])
    sp.add_argument("key_id")
    sp.set_defaults(func=cmd_keys_show)

    sp = gsub.add_parser("create", help="create a key (raw key printed once)",
                         parents=[common])
    sp.add_argument("name")
    # default=None: an absent flag must be an absent field, never a false one
    sp.add_argument("--admin", dest="is_admin", action="store_true", default=None,
                    help="grant admin API access (platform admin outside tenants, "
                         "tenant admin inside — not patchable later)")
    sp.add_argument("--expires", dest="expires_at",
                    help="expiry (ISO-8601)")
    sp.add_argument("--metadata",
                    help="arbitrary JSON object of your own (never interpreted)")
    mx = sp.add_mutually_exclusive_group()
    mx.add_argument("--cache-bypass", dest="cache_bypass", action="store_true",
                    help="never serve this key from either response cache "
                         "(explicit cache_enabled: false)")
    key_policy_fields(sp)
    sp.add_argument("--tenant", dest="tenant_id",
                    help="owning tenant id (platform admins only)")
    sp.set_defaults(func=cmd_keys_create)

    sp = gsub.add_parser("edit", help="patch a key (only the flags you pass)",
                         parents=[common])
    sp.add_argument("key_id")
    key_policy_fields(sp)
    sp.add_argument("--expires", dest="expires_at",
                    help="expiry (ISO-8601; 'null' clears it)")
    sp.add_argument("--grace-until",
                    help="move (or end, 'null') the rotation grace window")
    mx = sp.add_mutually_exclusive_group()
    mx.add_argument("--cache-bypass", dest="cache_bypass", action="store_true",
                    help="never serve this key from either response cache "
                         "(explicit cache_enabled: false)")
    mx.add_argument("--cache-inherit", dest="cache_inherit", action="store_true",
                    help="reset to the global cache policy (explicit null)")
    sp.add_argument("--inactive", action="store_true", help="deactivate the key")
    sp.add_argument("--active", action="store_true", help="re-activate the key")
    sp.add_argument("--tenant", dest="tenant_id",
                    help="re-home the key to this tenant (platform admins only)")
    sp.set_defaults(func=cmd_keys_edit)

    sp = gsub.add_parser("rotate", help="replace the credential (raw key printed once)",
                         parents=[common])
    sp.add_argument("key_id")
    sp.add_argument("--grace-seconds", dest="grace_seconds", type=int, default=0,
                    help="keep the previous credential working for N seconds")
    sp.set_defaults(func=cmd_keys_rotate)

    sp = gsub.add_parser("revoke", help="deactivate a key — its row and history stay",
                         parents=[common])
    sp.add_argument("key_id")
    sp.set_defaults(func=cmd_keys_revoke)

    # tenants ----------------------------------------------------------------
    g = sub.add_parser("tenants", help="tenants: list / show / create / edit / delete")
    gsub = g.add_subparsers(dest="action", required=True, metavar="action")

    def tenant_fields(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--display", dest="display_name",
                            help="free-form display name")
        parser.add_argument("--models", dest="allowed_models",
                            help="member-key model allowlist (fnmatch, comma-separated)")
        parser.add_argument("--tools", dest="allowed_tools",
                            help="member-key tool allowlist (fnmatch, comma-separated)")
        parser.add_argument("--mcp-servers", dest="allowed_mcp_servers",
                            help="allowed MCP servers (fnmatch, comma-separated)")
        parser.add_argument("--rpm", dest="rpm_limit", type=int, help="pooled RPM cap")
        parser.add_argument("--tpm", dest="tpm_limit", type=int, help="pooled TPM cap")
        parser.add_argument("--markup", dest="pricing_markup_pct", type=float,
                            help="cost-plus billing percent (20.0 means +20%%)")
        parser.add_argument("--conc", dest="max_concurrency", type=int,
                            help="pooled concurrent-request cap")
        parser.add_argument("--budget", dest="max_budget_usd", type=float,
                            help="lifetime spend cap (USD)")
        parser.add_argument("--daily", dest="daily_budget_usd", type=float,
                            help="UTC calendar-day spend cap (USD)")
        parser.add_argument("--monthly", dest="monthly_budget_usd", type=float,
                            help="UTC calendar-month spend cap (USD)")
        parser.add_argument("--residency", dest="required_residencies",
                            help="deployment residency tags required (comma-separated)")
        parser.add_argument("--inactive", action="store_true",
                            help="create inactive (or deactivate on edit)")
        parser.add_argument("--active", action="store_true",
                            help="re-activate (edit only)")

    sp = gsub.add_parser("list", help="list tenants", parents=[common])
    sp.set_defaults(func=cmd_tenants_list)

    sp = gsub.add_parser("show", help="one tenant, full row", parents=[common])
    sp.add_argument("tenant_id")
    sp.set_defaults(func=cmd_tenants_show)

    sp = gsub.add_parser("create", help="create a tenant", parents=[common])
    sp.add_argument("name", help="header-safe slug (letters, digits, - and _)")
    tenant_fields(sp)
    sp.set_defaults(func=cmd_tenants_create)

    sp = gsub.add_parser("edit", help="patch a tenant (only the flags you pass)",
                         parents=[common])
    sp.add_argument("tenant_id")
    tenant_fields(sp)
    sp.set_defaults(func=cmd_tenants_edit)

    sp = gsub.add_parser("delete", help="delete a tenant (refused while keys remain)",
                         parents=[common])
    sp.add_argument("tenant_id")
    sp.set_defaults(func=cmd_tenants_delete)

    # vault -------------------------------------------------------------------
    g = sub.add_parser("vault", help="BYOK vault: status / list / put / delete")
    gsub = g.add_subparsers(dest="action", required=True, metavar="action")

    sp = gsub.add_parser("status", help="vault config at a glance", parents=[common])
    sp.set_defaults(func=cmd_vault_status)

    sp = gsub.add_parser("list", help="a tenant's stored provider credentials",
                         parents=[common])
    sp.add_argument("tenant_id")
    sp.set_defaults(func=cmd_vault_list)

    sp = gsub.add_parser("put", help="store (or replace) a provider credential",
                         parents=[common])
    sp.add_argument("tenant_id")
    sp.add_argument("provider")
    sp.add_argument("--value", help="the credential — prefer stdin or env expansion, "
                                    "not shell history")
    sp.add_argument("--label", help="free-form label to tell stored keys apart")
    sp.set_defaults(func=cmd_vault_put)

    sp = gsub.add_parser("delete", help="delete a stored credential", parents=[common])
    sp.add_argument("tenant_id")
    sp.add_argument("provider")
    sp.set_defaults(func=cmd_vault_delete)

    # prompts ------------------------------------------------------------------
    g = sub.add_parser("prompts", help="prompt library: list / show / create / edit / delete")
    gsub = g.add_subparsers(dest="action", required=True, metavar="action")

    sp = gsub.add_parser("list", help="prompt names with their active/candidate versions",
                         parents=[common])
    sp.set_defaults(func=cmd_prompts_list)

    sp = gsub.add_parser("show", help="one prompt's versions", parents=[common])
    sp.add_argument("name")
    sp.set_defaults(func=cmd_prompts_show)

    sp = gsub.add_parser("create", help="append a version (first of a name auto-activates)",
                         parents=[common])
    sp.add_argument("name")
    body = sp.add_mutually_exclusive_group(required=True)
    body.add_argument("--body", help="the prompt text inline")
    body.add_argument("--body-file", help="file with the prompt text")
    sp.add_argument("--notes", help="change notes")
    sp.set_defaults(func=cmd_prompts_create)

    sp = gsub.add_parser("edit", help="activate / candidate / A-B / notes",
                         parents=[common])
    sp.add_argument("prompt_id")
    sp.add_argument("--activate", action="store_true", help="make this version serve")
    sp.add_argument("--deactivate", action="store_true",
                    help="deactivate this version")
    sp.add_argument("--candidate", type=lambda v: v.lower() in ("true", "1", "yes"),
                    help="mark (true) or unmark (false) as the A/B candidate")
    sp.add_argument("--ab-fraction", dest="ab_fraction", type=float,
                    help="mirror fraction for the candidate")
    sp.add_argument("--notes", help="change notes")
    sp.set_defaults(func=cmd_prompts_edit)

    sp = gsub.add_parser("delete", help="delete one prompt version (the active one is refused)",
                         parents=[common])
    sp.add_argument("prompt_id")
    sp.set_defaults(func=cmd_prompts_delete)

    # logs --------------------------------------------------------------------
    g = sub.add_parser("logs", help="request logs: list / show / export")
    gsub = g.add_subparsers(dest="action", required=True, metavar="action")

    def log_filters(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--limit", type=int, help="page size (default 50)")
        parser.add_argument("--offset", type=int, help="page start (default 0)")
        parser.add_argument("--status", help="success | error | blocked")
        parser.add_argument("--key-name", dest="key_name", help="filter by key name")
        parser.add_argument("--key-id", dest="key_id", help="filter by key id")
        parser.add_argument("--model", help="filter by model")
        parser.add_argument("--request-id", dest="request_id", help="filter by request id")
        parser.add_argument("--trace-id", dest="trace_id", help="filter by trace id")
        parser.add_argument("--tenant", dest="tenant_id",
                            help="per-tenant lens (platform admins)")

    sp = gsub.add_parser("list", help="query the request log", parents=[common])
    log_filters(sp)
    sp.set_defaults(func=cmd_logs_list)

    sp = gsub.add_parser("show", help="one row, full detail incl. bodies", parents=[common])
    sp.add_argument("log_id")
    sp.set_defaults(func=cmd_logs_show)

    sp = gsub.add_parser("export", help="the replay capture: JSONL with recorded baselines",
                         parents=[common])
    log_filters(sp)
    sp.add_argument("--out", help="write to FILE instead of stdout")
    sp.set_defaults(func=cmd_logs_export)

    sp = sub.add_parser(
        "replay", help="replay a capture file against the current config and diff it",
        parents=[common])
    sp.add_argument("file", help="capture JSONL (gateway logs export --out ...)")
    sp.set_defaults(func=cmd_replay)

    # feedback ----------------------------------------------------------------
    g = sub.add_parser("feedback", help="client ratings: list / stats")
    gsub = g.add_subparsers(dest="action", required=True, metavar="action")

    sp = gsub.add_parser("list", help="feedback rows, newest first", parents=[common])
    sp.add_argument("--limit", type=int)
    sp.add_argument("--offset", type=int)
    sp.add_argument("--key-name", dest="key_name")
    sp.add_argument("--model")
    sp.add_argument("--request-id", dest="request_id")
    sp.add_argument("--min-rating", dest="min_rating", type=int)
    sp.add_argument("--max-rating", dest="max_rating", type=int)
    sp.add_argument("--tenant", dest="tenant_id")
    sp.set_defaults(func=cmd_feedback_list)

    sp = gsub.add_parser("stats", help="quality aggregates: overall / by model / by key",
                         parents=[common])
    sp.add_argument("--key-name", dest="key_name")
    sp.add_argument("--model")
    sp.add_argument("--tenant", dest="tenant_id")
    sp.set_defaults(func=cmd_feedback_stats)

    # fleet state -----------------------------------------------------------
    sp = sub.add_parser("routing", help="models, deployments, priorities, breaker state",
                        parents=[common])
    sp.set_defaults(func=cmd_routing)

    g = sub.add_parser("breakers", help="circuit breakers")
    gsub = g.add_subparsers(dest="action", required=True, metavar="action")
    sp = gsub.add_parser("reset", help="force-close one deployment's breaker",
                         parents=[common])
    sp.add_argument("deployment")
    sp.set_defaults(func=cmd_breakers_reset)

    sp = sub.add_parser("guardrails", help="the configured guardrail inventory",
                        parents=[common])
    sp.set_defaults(func=cmd_guardrails)

    g = sub.add_parser("canaries", help="synthetic probe fleet")
    gsub = g.add_subparsers(dest="action", required=True, metavar="action")
    sp = gsub.add_parser("list", help="latest probe result per deployment",
                         parents=[common])
    sp.set_defaults(func=cmd_canaries)
    sp = gsub.add_parser("probe", help="run one probe wave now (spends provider quota)",
                         parents=[common])
    sp.set_defaults(func=cmd_canaries_probe)

    g = sub.add_parser("runaway", help="runaway-agent jail")
    gsub = g.add_subparsers(dest="action", required=True, metavar="action")
    sp = gsub.add_parser("list", help="jailed keys + config echo", parents=[common])
    sp.set_defaults(func=cmd_runaway)
    sp = gsub.add_parser("release", help="release a key from the jail", parents=[common])
    sp.add_argument("key_id")
    sp.set_defaults(func=cmd_runaway_release)

    sp = sub.add_parser("concurrency", help="in-flight slots and waiters, right now",
                        parents=[common])
    sp.set_defaults(func=cmd_concurrency)

    sp = sub.add_parser("mcp", help="MCP proxy: servers and open sessions",
                        parents=[common])
    sp.set_defaults(func=cmd_mcp)

    sp = sub.add_parser("slos", help="SLO targets, measured and verdicted",
                        parents=[common])
    sp.set_defaults(func=cmd_slos)

    sp = sub.add_parser("pii-scan", help="PII/secret scan (bulk, or one row via --id)",
                        parents=[common])
    sp.add_argument("--id", dest="log_id", type=int, help="scan one request-log row")
    sp.add_argument("--limit", type=int, help="rows to scan (bulk mode)")
    sp.add_argument("--key-name", dest="key_name")
    sp.add_argument("--model")
    sp.add_argument("--status")
    sp.add_argument("--tenant", dest="tenant_id")
    sp.set_defaults(func=cmd_pii_scan)

    # billing ----------------------------------------------------------------
    sp = sub.add_parser("invoices", help="the month statement", parents=[common])
    sp.add_argument("--month", help="YYYY-MM (default: current UTC month)")
    sp.add_argument("--tenant", dest="tenant_id", help="one tenant's statement")
    sp.add_argument("--csv", action="store_true",
                    help="CSV line items (pipe to --out FILE)")
    sp.add_argument("--out", help="write to FILE instead of stdout")
    sp.set_defaults(func=cmd_invoices)

    sp = sub.add_parser("anomalies", help="finops flags: is any of this unusual?",
                        parents=[common])
    sp.add_argument("--tenant", dest="tenant_id")
    sp.set_defaults(func=cmd_anomalies)

    sp = sub.add_parser("forecasts", help="where will this month land?",
                        parents=[common])
    sp.add_argument("--month", help="YYYY-MM (default: current UTC month)")
    sp.add_argument("--tenant", dest="tenant_id")
    sp.set_defaults(func=cmd_forecasts)

    sp = sub.add_parser("usage-export", help="usage line items (CSV or JSON)",
                        parents=[common])
    sp.add_argument("--format", choices=["csv", "json"], default="csv")
    sp.add_argument("--since", help="ISO-8601 lower bound")
    sp.add_argument("--until", help="ISO-8601 upper bound")
    sp.add_argument("--key-name", dest="key_name")
    sp.add_argument("--model")
    sp.add_argument("--status")
    sp.add_argument("--tenant", dest="tenant_id")
    sp.add_argument("--include-shadow", dest="include_shadow", action="store_true",
                    help="include shadow-mirror rows")
    sp.add_argument("--out", help="write to FILE instead of stdout")
    sp.set_defaults(func=cmd_usage_export)

    # migration evidence ------------------------------------------------------
    sp = sub.add_parser("shadow-diff", help="mirror evidence per (model, deployment)",
                        parents=[common])
    sp.add_argument("--model")
    sp.add_argument("--since", help="ISO-8601 (stale pairs excluded)")
    sp.set_defaults(func=cmd_shadow_diff)

    sp = sub.add_parser("verdicts", help="go / no-go recommendations per candidate",
                        parents=[common])
    sp.add_argument("--model")
    sp.add_argument("--deployment")
    sp.add_argument("--since")
    sp.set_defaults(func=cmd_verdicts)

    sp = sub.add_parser("watch", help="post-promotion watch over serving traffic",
                        parents=[common])
    sp.add_argument("--model")
    sp.add_argument("--deployment")
    sp.add_argument("--since")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("pairs", help="scored pairs — the migration review queue",
                        parents=[common])
    sp.add_argument("--model", help="the public model (mirror pairs)")
    sp.add_argument("--deployment", help="the shadow deployment (mirror pairs)")
    sp.add_argument("--verdict", help="exact | near | divergent | ...")
    sp.add_argument("--trial-id", dest="trial_id",
                    help="a trial run's pairs instead of mirror pairs")
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--offset", type=int, default=0)
    sp.set_defaults(func=cmd_pairs)

    g = sub.add_parser("trials", help="candidate trials: create / list / show / cancel")
    gsub = g.add_subparsers(dest="action", required=True, metavar="action")

    sp = gsub.add_parser("create", help="replay a traffic window against a candidate",
                         parents=[common])
    sp.add_argument("--model", required=True,
                    help="the public model whose window is replayed")
    sp.add_argument("--deployment", required=True, help="the candidate deployment")
    sp.add_argument("--since")
    sp.add_argument("--until")
    sp.add_argument("--limit", type=int,
                    help="max entries (server clamps to the trials: ceilings)")
    sp.add_argument("--concurrency", type=int)
    sp.add_argument("--max-cost", dest="max_cost_usd", type=float)
    sp.set_defaults(func=cmd_trials_create)

    sp = gsub.add_parser("list", help="trial runs, newest first", parents=[common])
    sp.add_argument("--limit", type=int)
    sp.add_argument("--offset", type=int)
    sp.set_defaults(func=cmd_trials_list)

    sp = gsub.add_parser("show", help="one trial + its verdict summary", parents=[common])
    sp.add_argument("trial_id")
    sp.set_defaults(func=cmd_trials_show)

    sp = gsub.add_parser("cancel", help="cancel a pending/running trial", parents=[common])
    sp.add_argument("trial_id")
    sp.set_defaults(func=cmd_trials_cancel)

    # operations -------------------------------------------------------------
    sp = sub.add_parser("reload", help="hot-reload config.yaml (same as SIGHUP)",
                        parents=[common])
    sp.set_defaults(func=cmd_reload)

    g = sub.add_parser("backups", help="SQLite backups: list / create / delete")
    gsub = g.add_subparsers(dest="action", required=True, metavar="action")
    sp = gsub.add_parser("list", help="existing backups", parents=[common])
    sp.set_defaults(func=cmd_backups_list)
    sp = gsub.add_parser("create", help="snapshot the database now (SQLite only)",
                         parents=[common])
    sp.set_defaults(func=cmd_backups_create)
    sp = gsub.add_parser("delete", help="delete one backup file", parents=[common])
    sp.add_argument("name")
    sp.set_defaults(func=cmd_backups_delete)

    return p


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def _dispatch(args, client: httpx.AsyncClient, key: str | None = None) -> int:
    """Run the parsed command; the shared error rendering for both the
    async core and the console entry point."""
    api = Api(client, key if key is not None else args.key)
    try:
        await args.func(api, args)
    except CliError as exc:
        print(f"gateway: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"gateway: unreachable — {exc}", file=sys.stderr)
        return 1
    return 0


async def run(argv: list[str] | None, client: httpx.AsyncClient,
              key: str | None = None) -> int:
    """The async core — testable against an in-process app via its
    injected AsyncClient (the console entry point builds its own).
    ``key`` overrides the parsed --key/GATEWAY_ADMIN_KEY (tests pass
    the fixture's admin key here); the injected client's timeout is
    the one that applies."""
    args = build_parser().parse_args(argv)
    return await _dispatch(args, client, key=key)


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point: ``gateway <group> [action] [flags]``."""
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        return 130


async def _main_async(args) -> int:
    """main()'s async half: its own client against --url/GATEWAY_URL."""
    async with httpx.AsyncClient(
        base_url=args.url, timeout=args.timeout
    ) as client:
        return await _dispatch(args, client)
