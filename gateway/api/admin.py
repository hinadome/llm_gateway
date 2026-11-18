"""Admin API: virtual key management, request logs, stats, usage export, routing/guardrail inspection."""

from __future__ import annotations

import csv
import io
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from sqlalchemy import func, select

from ..core.alerts import BudgetAlertManager
from ..core.context import AuthContext
from ..core.prompt_library import PromptLibrary
from ..core.replay import replay_entries
from ..db import get_sessionmaker
from ..errors import GatewayError
from ..models import Feedback, RequestLog, Tenant, VirtualKey
from ..observability.metrics import PII_FINDINGS
from ..schemas import (
    KeyCreateRequest,
    KeyUpdateRequest,
    PromptCreateRequest,
    PromptPatchRequest,
    ReplayRequest,
    TenantCreateRequest,
    TenantUpdateRequest,
)
from .deps import ENDPOINT_SCOPES, hash_key, require_admin, require_platform_admin

router = APIRouter()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@router.get("/admin")
async def admin_root() -> RedirectResponse:
    """The admin dashboard lives at /admin/ui."""
    return RedirectResponse(url="/admin/ui", status_code=307)


@router.get("/admin/ui")
async def admin_ui() -> FileResponse:
    """Serve the self-contained admin dashboard.

    One static HTML file (inline CSS/JS, no build step, no external
    resources — works offline). It fetches the JSON admin API above with
    the admin key the operator enters; the shell itself contains no data
    and therefore needs no auth (all data endpoints still do).
    """
    return FileResponse(_STATIC_DIR / "admin.html", media_type="text/html")


def _generate_key(prefix: str) -> str:
    return f"{prefix}-{secrets.token_urlsafe(24)}"


def _parse_expires(expires_at: str | None) -> datetime | None:
    if not expires_at:
        return None
    try:
        dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GatewayError(
            400, f"Invalid expires_at: {exc}", "invalid_request_error", param="expires_at"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ----------------------------------------------------------------------
# Virtual keys
# ----------------------------------------------------------------------


def _validate_scoping(allowed_endpoints: list[str] | None) -> None:
    """Scope names are a closed set — catch typos at set time, not as a
    silently-never-matching restriction later."""
    if allowed_endpoints is None:
        return
    unknown = [s for s in allowed_endpoints if s not in ENDPOINT_SCOPES]
    if unknown:
        raise GatewayError(
            400,
            f"Unknown endpoint scope(s): {', '.join(unknown)}. "
            f"Valid scopes: {', '.join(sorted(ENDPOINT_SCOPES))}.",
            "invalid_request_error",
        )


def _require_key_access(admin: AuthContext, vkey: VirtualKey) -> None:
    """Tenant admins may only touch their own tenant's keys.

    A 404 (not 403): confirming that another tenant's key id exists
    would leak key ids to callers outside the tenant — the response is
    byte-identical to the unknown-key case. Platform admins see it all.
    """
    if admin.tenant_id is not None and vkey.tenant_id != admin.tenant_id:
        raise GatewayError(404, f"Key '{vkey.id}' not found.", "invalid_request_error")


def _require_log_access(admin: AuthContext, log: RequestLog) -> None:
    """Same rule for single log rows (they carry the tenant too)."""
    if admin.tenant_id is not None and log.tenant_id != admin.tenant_id:
        raise GatewayError(
            404, f"Log entry {log.id} not found.", "invalid_request_error"
        )


@router.post("/admin/keys", status_code=201)
async def create_key(
    body: KeyCreateRequest,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    raw_key = _generate_key("sk-gw")
    _validate_scoping(body.allowed_endpoints)
    # Tenancy: platform admins may place a key in any existing tenant
    # (or none); tenant admins are confined to their own — a mismatching
    # explicit tenant_id is refused, not silently overridden.
    tenant_id = body.tenant_id
    if admin.tenant_id is not None:
        if tenant_id is not None and tenant_id != admin.tenant_id:
            raise GatewayError(
                400,
                "Tenant administrators can only create keys in their own "
                f"tenant ('{admin.tenant_name}').",
                "invalid_request_error",
            )
        tenant_id = admin.tenant_id
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if tenant_id is not None:
            tenant = await session.get(Tenant, tenant_id)
            if tenant is None:
                raise GatewayError(
                    400,
                    f"Tenant '{tenant_id}' does not exist.",
                    "invalid_request_error",
                )
        existing = await session.execute(
            select(VirtualKey).where(VirtualKey.name == body.name)
        )
        if existing.scalar_one_or_none() is not None:
            raise GatewayError(
                409, f"A key named '{body.name}' already exists.", "invalid_request_error"
            )
        vkey = VirtualKey(
            name=body.name,
            key_hash=hash_key(raw_key),
            key_prefix=raw_key[:12],
            tenant_id=tenant_id,
            is_admin=body.is_admin,
            allowed_models=body.allowed_models,
            rpm_limit=body.rpm_limit,
            tpm_limit=body.tpm_limit,
            max_budget_usd=body.max_budget_usd,
            daily_budget_usd=body.daily_budget_usd,
            monthly_budget_usd=body.monthly_budget_usd,
            expires_at=_parse_expires(body.expires_at),
            metadata_json=body.metadata,
            disabled_guardrails=body.disabled_guardrails,
            model_mappings=body.model_mappings,
            default_model=body.default_model,
            system_prompt=body.system_prompt,
            system_prompt_ref=body.system_prompt_ref,
            prompt_variables=body.prompt_variables,
            allowed_endpoints=body.allowed_endpoints,
            allowed_ips=body.allowed_ips,
        )
        session.add(vkey)
        await session.commit()
        await session.refresh(vkey)
        result = vkey.to_public_dict()
    # Audit AFTER a successful commit, and never the raw key itself —
    # it was shown to the administrator once and is nowhere else.
    raw_request.app.state.audit.record(
        "key.created",
        actor=admin,
        key_id=result["id"],
        key_name=body.name,
        tenant_id=result["tenant_id"],
    )
    # The raw key is returned exactly once, at creation time.
    result["key"] = raw_key
    return result


@router.get("/admin/keys")
async def list_keys(admin: Annotated[AuthContext, Depends(require_admin)]) -> dict:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(VirtualKey).order_by(VirtualKey.created_at)
        if admin.tenant_id is not None:  # tenant admin: own tenant only
            stmt = stmt.where(VirtualKey.tenant_id == admin.tenant_id)
        result = await session.execute(stmt)
        keys = [v.to_public_dict() for v in result.scalars()]
    return {"object": "list", "data": keys}


@router.get("/admin/keys/{key_id}")
async def get_key(key_id: str, admin: Annotated[AuthContext, Depends(require_admin)]) -> dict:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        vkey = await session.get(VirtualKey, key_id)
        if vkey is None:
            raise GatewayError(404, f"Key '{key_id}' not found.", "invalid_request_error")
        _require_key_access(admin, vkey)
        return vkey.to_public_dict()


@router.patch("/admin/keys/{key_id}")
async def update_key(
    key_id: str,
    body: KeyUpdateRequest,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        vkey = await session.get(VirtualKey, key_id)
        if vkey is None:
            raise GatewayError(404, f"Key '{key_id}' not found.", "invalid_request_error")
        _require_key_access(admin, vkey)
        updates = body.model_dump(exclude_unset=True)
        if "expires_at" in updates:
            vkey.expires_at = _parse_expires(updates.pop("expires_at"))
        if "allowed_endpoints" in updates:
            _validate_scoping(updates["allowed_endpoints"])
        # Re-homing a key is a platform-admin operation — a tenant admin
        # moving keys out of (or between) tenants would let keys escape
        # their scope. The 403 is honest here: the key is theirs.
        if "tenant_id" in updates:
            if admin.tenant_id is not None:
                raise GatewayError(
                    403,
                    "Tenant administrators cannot move keys between tenants.",
                    "invalid_request_error",
                )
            new_tenant_id = updates.pop("tenant_id")
            if new_tenant_id is not None:
                target = await session.get(Tenant, new_tenant_id)
                if target is None:
                    raise GatewayError(
                        400,
                        f"Tenant '{new_tenant_id}' does not exist.",
                        "invalid_request_error",
                    )
            vkey.tenant_id = new_tenant_id
        budget_changed = "max_budget_usd" in updates
        for field in (
            "allowed_models",
            "rpm_limit",
            "tpm_limit",
            "max_budget_usd",
            "daily_budget_usd",
            "monthly_budget_usd",
            "is_active",
            "disabled_guardrails",
            "model_mappings",
            "default_model",
            "system_prompt",
            "system_prompt_ref",
            "prompt_variables",
            "allowed_endpoints",
            "allowed_ips",
        ):
            if field in updates:
                setattr(vkey, field, updates[field])
        if budget_changed:
            # Raising (or lowering) the budget re-arms alerts: levels the
            # current spend has already crossed count as fired, so only
            # future crossings fire a webhook.
            alerter: BudgetAlertManager = raw_request.app.state.alerts
            vkey.budget_alerts_fired = alerter.rearm_levels(
                vkey.spend_usd, vkey.max_budget_usd
            )
        await session.commit()
        await session.refresh(vkey)
        result = vkey.to_public_dict()
    # Audit AFTER a successful commit: which fields the admin touched.
    raw_request.app.state.audit.record(
        "key.updated",
        actor=admin,
        key_id=key_id,
        key_name=result["name"],
        fields=sorted(updates.keys()),
    )
    return result


@router.delete("/admin/keys/{key_id}")
async def revoke_key(
    key_id: str,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        vkey = await session.get(VirtualKey, key_id)
        if vkey is None:
            raise GatewayError(404, f"Key '{key_id}' not found.", "invalid_request_error")
        _require_key_access(admin, vkey)
        vkey.is_active = False
        await session.commit()
        raw_request.app.state.audit.record(
            "key.revoked", actor=admin, key_id=key_id, key_name=vkey.name
        )
        return {"id": key_id, "revoked": True}


# ----------------------------------------------------------------------
# Tenants (platform admins only)
# ----------------------------------------------------------------------


@router.post("/admin/tenants", status_code=201)
async def create_tenant(
    body: TenantCreateRequest,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        existing = await session.execute(select(Tenant).where(Tenant.name == body.name))
        if existing.scalar_one_or_none() is not None:
            raise GatewayError(
                409, f"A tenant named '{body.name}' already exists.", "invalid_request_error"
            )
        tenant = Tenant(
            name=body.name,
            display_name=body.display_name,
            is_active=body.is_active,
            allowed_models=body.allowed_models,
            rpm_limit=body.rpm_limit,
            tpm_limit=body.tpm_limit,
            pricing_markup_pct=body.pricing_markup_pct,
            max_budget_usd=body.max_budget_usd,
            daily_budget_usd=body.daily_budget_usd,
            monthly_budget_usd=body.monthly_budget_usd,
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        result = tenant.to_public_dict()
    raw_request.app.state.audit.record(
        "tenant.created",
        actor=admin,
        tenant_id=result["id"],
        tenant_name=body.name,
    )
    return result


@router.get("/admin/tenants")
async def list_tenants(
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(Tenant).order_by(Tenant.created_at))
        tenants = [t.to_public_dict() for t in result.scalars()]
    return {"object": "list", "data": tenants}


@router.get("/admin/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: str, admin: Annotated[AuthContext, Depends(require_platform_admin)]
) -> dict:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise GatewayError(
                404, f"Tenant '{tenant_id}' not found.", "invalid_request_error"
            )
        return tenant.to_public_dict()


@router.patch("/admin/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: str,
    body: TenantUpdateRequest,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Update a tenant's policy ceiling. The slug is immutable (it is
    what APIs, log filters and the X-Gateway-Tenant header speak);
    display_name and every limit are patchable at runtime — the next
    request snapshots the new values at auth time."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise GatewayError(
                404, f"Tenant '{tenant_id}' not found.", "invalid_request_error"
            )
        updates = body.model_dump(exclude_unset=True)
        budget_changed = "max_budget_usd" in updates
        for field in (
            "display_name",
            "is_active",
            "allowed_models",
            "rpm_limit",
            "tpm_limit",
            "pricing_markup_pct",
            "max_budget_usd",
            "daily_budget_usd",
            "monthly_budget_usd",
        ):
            if field in updates:
                setattr(tenant, field, updates[field])
        if budget_changed:
            # Raising/lowering the budget re-arms alerts, same as keys.
            alerter: BudgetAlertManager = raw_request.app.state.alerts
            tenant.budget_alerts_fired = alerter.rearm_levels(
                tenant.spend_usd, tenant.max_budget_usd
            )
        await session.commit()
        await session.refresh(tenant)
        result = tenant.to_public_dict()
    raw_request.app.state.audit.record(
        "tenant.updated",
        actor=admin,
        tenant_id=tenant_id,
        tenant_name=result["name"],
        fields=sorted(updates.keys()),
    )
    return result


@router.delete("/admin/tenants/{tenant_id}")
async def delete_tenant(
    tenant_id: str,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Delete a tenant. Refused while any key still references it:

    a dangling tenant_id fails closed at auth time (the key cannot be
    served as an unrestricted platform key), so orphaning keys would
    silently revoke them. Re-home or revoke the keys first.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise GatewayError(
                404, f"Tenant '{tenant_id}' not found.", "invalid_request_error"
            )
        key_count = await session.scalar(
            select(func.count()).where(VirtualKey.tenant_id == tenant_id)
        )
        if key_count:
            raise GatewayError(
                409,
                f"Tenant '{tenant.name}' still has {key_count} key(s) — "
                "re-home or revoke them first (a key without its tenant "
                "fails closed at auth time).",
                "invalid_request_error",
            )
        name = tenant.name
        await session.delete(tenant)
        await session.commit()
    raw_request.app.state.audit.record(
        "tenant.deleted", actor=admin, tenant_id=tenant_id, tenant_name=name
    )
    return {"id": tenant_id, "deleted": True}


# ----------------------------------------------------------------------
# Prompt library (prompt ops)
# ----------------------------------------------------------------------


@router.post("/admin/prompts", status_code=201)
async def create_prompt_version(
    body: PromptCreateRequest,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Append a version to a named prompt. The first version of a name
    auto-activates (an inactive-only prompt could never serve); promote
    later versions via PATCH {activate: true}."""
    library: PromptLibrary = raw_request.app.state.prompt_library
    result = await library.create_version(body.name, body.body, body.notes)
    raw_request.app.state.audit.record(
        "prompt.created",
        actor=admin,
        prompt_name=body.name,
        version=result["version"],
        auto_activated=result["is_active"],
    )
    return result


@router.get("/admin/prompts")
async def list_prompts(
    raw_request: Request, admin: Annotated[AuthContext, Depends(require_platform_admin)]
) -> dict:
    library: PromptLibrary = raw_request.app.state.prompt_library
    return {"object": "list", "data": await library.list_names()}


@router.get("/admin/prompts/{name}")
async def get_prompt_versions(
    name: str,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    library: PromptLibrary = raw_request.app.state.prompt_library
    versions = await library.versions(name)
    if versions is None:
        raise GatewayError(
            404, f"Prompt '{name}' not found.", "invalid_request_error"
        )
    return {"object": "list", "data": versions}


@router.patch("/admin/prompts/{prompt_id}")
async def patch_prompt_version(
    prompt_id: int,
    body: PromptPatchRequest,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Activate (promote/rollback), mark as the A/B candidate, retarget
    the mirror fraction, or edit notes."""
    library: PromptLibrary = raw_request.app.state.prompt_library
    result = await library.patch_version(
        prompt_id,
        activate=body.activate,
        candidate=body.candidate,
        ab_fraction=body.ab_fraction,
        notes=body.notes,
    )
    if result is None:
        raise GatewayError(
            404, f"Prompt version {prompt_id} not found.", "invalid_request_error"
        )
    raw_request.app.state.audit.record(
        "prompt.updated",
        actor=admin,
        prompt_name=result["name"],
        version=result["version"],
        fields=sorted(
            f for f in ("activate", "candidate", "ab_fraction", "notes")
            if getattr(body, f) is not None
        ),
    )
    return result


@router.delete("/admin/prompts/{prompt_id}")
async def delete_prompt_version(
    prompt_id: int,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Delete one version. The active version of a name is refused —
    keys reference the name, so serving must be re-pointed explicitly
    (activate another version) before the active row can go away."""
    library: PromptLibrary = raw_request.app.state.prompt_library
    result = await library.delete_version(prompt_id)
    if result is None:
        raise GatewayError(
            404, f"Prompt version {prompt_id} not found.", "invalid_request_error"
        )
    if isinstance(result, tuple):  # ("active", row) — refusal
        row = result[1]
        raise GatewayError(
            409,
            f"Version {row['version']} is the active version of "
            f"'{row['name']}' — activate another version before deleting it.",
            "invalid_request_error",
        )
    raw_request.app.state.audit.record(
        "prompt.deleted",
        actor=admin,
        prompt_name=result["name"],
        version=result["version"],
    )
    return result


# ----------------------------------------------------------------------
# Request logs
# ----------------------------------------------------------------------


def _decode_rate(row: RequestLog) -> float | None:
    """Completion tokens per second AFTER the first token (the decode
    phase — excludes the wait for the model to start)."""
    if not row.ttft_ms or not row.completion_tokens or (row.latency_ms or 0) <= row.ttft_ms:
        return None
    return round(row.completion_tokens / ((row.latency_ms - row.ttft_ms) / 1000.0), 1)


def _log_row(row: RequestLog) -> dict:
    """Log fields safe for list views (bodies excluded — they can be large)."""
    return {
        "id": row.id,
        "request_id": row.request_id,
        "key_name": row.key_name,
        "key_id": row.key_id,
        "route": row.route,
        "tenant_id": row.tenant_id,
        "model": row.model,
        "provider": row.provider,
        "deployment": row.deployment,
        "status": row.status,
        "error_message": row.error_message,
        "guardrail_actions": row.guardrail_actions,
        "stream": row.stream,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "cached_tokens": row.cached_tokens,
        "cost_usd": row.cost_usd,
        "latency_ms": row.latency_ms,
        # Streaming detail: time-to-first-token (None on non-streaming
        # rows) and the derived generation-phase decode rate
        # (completion tokens per second after the first token).
        "ttft_ms": row.ttft_ms,
        "decode_tokens_per_sec": _decode_rate(row),
        "cache_hit": row.cache_hit,
        "shadow": bool(row.shadow),
        "prompt_variant": row.prompt_variant,
        "trace_id": row.trace_id,
        "span_id": row.span_id,
        "auth_method": row.auth_method,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/admin/logs")
async def list_logs(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = None,
    key_name: str | None = None,
    key_id: str | None = None,
    model: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    """Request log rows. Tenant admins see only their own tenant's rows
    (the filter is forced, not defaulted); platform admins may filter
    by any tenant_id."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(RequestLog).order_by(RequestLog.id.desc())
        # Tenant scoping: forced for tenant admins, opt-in for platform.
        effective_tenant = admin.tenant_id if admin.tenant_id is not None else tenant_id
        if effective_tenant:
            stmt = stmt.where(RequestLog.tenant_id == effective_tenant)
        if status:
            stmt = stmt.where(RequestLog.status == status)
        if key_name:
            stmt = stmt.where(RequestLog.key_name == key_name)
        if key_id:
            stmt = stmt.where(RequestLog.key_id == key_id)
        if model:
            stmt = stmt.where(RequestLog.model == model)
        if request_id:
            stmt = stmt.where(RequestLog.request_id == request_id)
        if trace_id:
            stmt = stmt.where(RequestLog.trace_id == trace_id)
        total = await session.scalar(
            select(func.count()).select_from(stmt.subquery())
        )
        result = await session.execute(stmt.limit(limit).offset(offset))
        rows = result.scalars().all()
        data = [_log_row(row) for row in rows]
    return {"object": "list", "total": total, "limit": limit, "offset": offset, "data": data}


@router.get("/admin/logs/export")
async def export_logs(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
    limit: int = Query(1000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    status: str | None = None,
    key_name: str | None = None,
    key_id: str | None = None,
    model: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    tenant_id: str | None = None,
) -> StreamingResponse:
    """Capture for the replay harness (see POST /admin/replay and
    core/replay.py): the replayable request log as JSONL — rows with a
    stored request body, shadow mirrors excluded — one line per
    request with the recorded baseline (caller identity, route, model,
    status, deployment, guardrail verdicts) the replay diffs against.

    Same scoping as /admin/logs: tenant admins export only their own
    tenant's rows. ``comparable`` marks the rows a replay can diff
    (recorded success/blocked); rows recorded as ``error`` carry a
    skip_reason — their outcome was upstream/counter state, which a
    dry run cannot re-run.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # NOTE: bodies are filtered in Python, not SQL. The JSON column
        # stores Python None as the literal string 'null' on SQLite
        # (IS NOT NULL would diverge between the SQLite and JSONB
        # variants), so the page limit bounds *scanned* rows.
        stmt = (
            select(RequestLog)
            .order_by(RequestLog.id.desc())
            .where(RequestLog.shadow.isnot(True))
        )
        effective_tenant = admin.tenant_id if admin.tenant_id is not None else tenant_id
        if effective_tenant:
            stmt = stmt.where(RequestLog.tenant_id == effective_tenant)
        if status:
            stmt = stmt.where(RequestLog.status == status)
        if key_name:
            stmt = stmt.where(RequestLog.key_name == key_name)
        if key_id:
            stmt = stmt.where(RequestLog.key_id == key_id)
        if model:
            stmt = stmt.where(RequestLog.model == model)
        if request_id:
            stmt = stmt.where(RequestLog.request_id == request_id)
        if trace_id:
            stmt = stmt.where(RequestLog.trace_id == trace_id)
        rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()

    def lines():
        for row in rows:
            # Python-side body filter (see the NOTE above the query):
            # NULL bodies are not replayable and don't belong in a
            # capture line.
            if row.request_body is None:
                continue
            line = _log_row(row)
            line["request_body"] = row.request_body
            comparable = row.status in ("success", "blocked")
            line["comparable"] = comparable
            line["skip_reason"] = (
                None
                if comparable
                else "recorded error outcome — upstream/counter state a replay cannot re-run"
            )
            yield json.dumps(line, default=str) + "\n"

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": "attachment; filename=gateway-capture.jsonl"
        },
    )


@router.post("/admin/replay")
async def replay_capture(
    body: ReplayRequest,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Replay a capture against the *current* config and diff it.

    Each entry re-executes through the request-policy phases only —
    resolution, prompt policy, permissions, guardrails, routing
    selection — as the recorded caller's *current* key (re-loaded
    fresh: edited or revoked keys replay under what they are now). No
    upstream calls, counters, cache, spend, log rows or lifecycle
    events; guardrail counters are suppressed (ctx.dry_run) so the
    harness never trips block alerts. The report's ``changed`` lines
    are what this config would change about that traffic — the safety
    net for a hot reload.
    """
    pipeline = raw_request.app.state.pipeline
    cfg = pipeline.config.replay
    if not cfg.enabled:
        raise GatewayError(
            404,
            "The replay harness is disabled (replay.enabled: false).",
            "invalid_request_error",
            code="replay_disabled",
        )
    if len(body.entries) > cfg.max_entries:
        raise GatewayError(
            400,
            f"Too many entries: {len(body.entries)} (replay.max_entries is "
            f"{cfg.max_entries}). Replay a sample, not all of history.",
            "invalid_request_error",
            code="too_many_replay_entries",
        )
    return await replay_entries(pipeline, body.entries, admin)


@router.get("/admin/logs/{log_id}")
async def get_log(
    log_id: int,
    admin: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """One log row in full, including the stored request/response bodies."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        log = await session.get(RequestLog, log_id)
        if log is None:
            raise GatewayError(404, f"Log entry {log_id} not found.", "invalid_request_error")
        _require_log_access(admin, log)
        row = _log_row(log)
    row["request_body"] = log.request_body
    row["response_body"] = log.response_body
    # Wire capture (see observability/wire.py): allowlisted client request
    # headers, the request as actually sent to the provider, and the
    # allowlisted provider response headers. NULL on rows written before
    # the capture existed, on cache hits, and when the allowlists matched
    # nothing.
    row["client_headers"] = log.client_headers
    row["upstream_request"] = log.upstream_request
    row["upstream_headers"] = log.upstream_headers
    return row


# ----------------------------------------------------------------------
# Feedback
# ----------------------------------------------------------------------


def _feedback_row(row: Feedback) -> dict:
    return {
        "id": row.id,
        "object": "feedback",
        "request_id": row.request_id,
        "key_name": row.key_name,
        "tenant_id": row.tenant_id,
        "model": row.model,
        "rating": row.rating,
        "comment": row.comment,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/admin/feedback")
async def list_feedback(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    key_name: str | None = None,
    model: str | None = None,
    request_id: str | None = None,
    min_rating: int | None = Query(None, ge=1, le=5),
    max_rating: int | None = Query(None, ge=1, le=5),
    tenant_id: str | None = None,
) -> dict:
    """Client-reported feedback rows, newest first. Tenant admins see
    only their own tenant's rows (the filter is forced, not defaulted —
    same rule as /admin/logs)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(Feedback).order_by(Feedback.created_at.desc(), Feedback.id.desc())
        effective_tenant = admin.tenant_id if admin.tenant_id is not None else tenant_id
        if effective_tenant:
            stmt = stmt.where(Feedback.tenant_id == effective_tenant)
        if key_name:
            stmt = stmt.where(Feedback.key_name == key_name)
        if model:
            stmt = stmt.where(Feedback.model == model)
        if request_id:
            stmt = stmt.where(Feedback.request_id == request_id)
        if min_rating is not None:
            stmt = stmt.where(Feedback.rating >= min_rating)
        if max_rating is not None:
            stmt = stmt.where(Feedback.rating <= max_rating)
        total = await session.scalar(
            select(func.count()).select_from(stmt.subquery())
        )
        result = await session.execute(stmt.limit(limit).offset(offset))
        rows = result.scalars().all()
        data = [_feedback_row(row) for row in rows]
    return {"object": "list", "total": total, "limit": limit, "offset": offset, "data": data}


def _feedback_bucket(rows: list) -> dict:
    """count / average / %negative / %positive for one group.

    Feedback is human-rate (one row per explicit client action, not
    per request), so bucketing in Python keeps one consistent
    definition across dimensions — including the 4-5/1-2 sentiment
    split — without three divergent SQL expressions.
    """
    count = len(rows)
    if count == 0:
        return {
            "count": 0,
            "avg_rating": None,
            "pct_negative": None,
            "pct_positive": None,
        }
    ratings = [row.rating for row in rows]
    negative = sum(1 for r in ratings if r <= 2)
    positive = sum(1 for r in ratings if r >= 4)
    return {
        "count": count,
        "avg_rating": round(sum(ratings) / count, 3),
        "pct_negative": round(100.0 * negative / count, 1),
        "pct_positive": round(100.0 * positive / count, 1),
    }


@router.get("/admin/feedback/stats")
async def feedback_stats(
    admin: Annotated[AuthContext, Depends(require_admin)],
    model: str | None = None,
    key_name: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    """Quality aggregates from client feedback: overall, by model and by
    key (positive = rating 4-5, negative = 1-2). Tenant admins are
    scoped to their own tenant."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(Feedback)
        effective_tenant = admin.tenant_id if admin.tenant_id is not None else tenant_id
        if effective_tenant:
            stmt = stmt.where(Feedback.tenant_id == effective_tenant)
        if model:
            stmt = stmt.where(Feedback.model == model)
        if key_name:
            stmt = stmt.where(Feedback.key_name == key_name)
        all_rows = (await session.execute(stmt)).scalars().all()

    by_model: dict[str, list] = {}
    by_key: dict[str, list] = {}
    for row in all_rows:
        by_model.setdefault(row.model or "unknown", []).append(row)
        by_key.setdefault(row.key_name or "unknown", []).append(row)
    return {
        "object": "feedback_stats",
        "overall": _feedback_bucket(all_rows),
        "by_model": {
            name: _feedback_bucket(rows) for name, rows in sorted(by_model.items())
        },
        "by_key": {name: _feedback_bucket(rows) for name, rows in sorted(by_key.items())},
    }


# ----------------------------------------------------------------------
# Canaries
# ----------------------------------------------------------------------


@router.get("/admin/canaries")
async def canary_status(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Last synthetic-probe result per deployment, newest fleet view.
    Deployment health is platform-wide configuration (not tenant
    data) — the same rule as /admin/routing, so platform admins only.
    ``outcome`` is ok / timeout / error, or "unknown" before the first
    probe (a wave has not run yet — canary is disabled by default;
    POST /admin/canaries/probe runs one now)."""
    prober = raw_request.app.state.canary
    config = raw_request.app.state.config.canary
    return {
        "object": "canary_status",
        "enabled": config.enabled,
        "interval_seconds": config.interval_seconds,
        "timeout_seconds": config.timeout_seconds,
        "deployments_filter": config.deployments,
        "data": prober.snapshot(),
    }


@router.post("/admin/canaries/probe")
async def canary_probe_now(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Run one probe wave now (regardless of canary.enabled) and
    return the fresh results — "probe now" for ops, the same wave the
    background loop runs on its interval. Each probe is a direct
    provider call (no pipeline: no log rows, no spend, no breaker
    accounting) bounded by the canary timeout, so a wave costs
    max_tokens of provider quota per deployment."""
    prober = raw_request.app.state.canary
    # Hot-reload parity: a manual wave uses the *current* config, even
    # if the background loop has not woken up to pick it up yet.
    prober.config = raw_request.app.state.config.canary
    results = await prober.probe_once()
    return {
        "object": "canary_probe",
        "probed": len(results),
        "healthy": sum(1 for r in results if r["healthy"]),
        "data": results,
    }


# ----------------------------------------------------------------------
# PII / secret scan
# ----------------------------------------------------------------------


async def _scan_and_record(raw_request: Request, rows: "list[RequestLog]") -> list[dict]:
    """Scan rows once with the current scanner; count + emit per
    flagged row. Returns every row's result (clean rows included —
    the bulk view reports them too).

    Metrics count *rows* containing a kind (not raw occurrences —
    one pasted key in a row is one exposure); ``pii.found`` events
    carry the same per-row kinds so a subscriber can alert/ticket."""
    scanner = raw_request.app.state.pii_scanner
    events = getattr(raw_request.app.state, "events", None)
    results = []
    for row in rows:
        result = scanner.scan_log(row)
        results.append(result)
        if result["clean"]:
            continue
        request_findings = result["findings"]["request"]
        response_findings = result["findings"]["response"]
        for kind in set(request_findings) | set(response_findings):
            PII_FINDINGS.labels(kind=kind).inc()
        if events is not None:
            events.emit_background(
                "pii.found",
                None,
                log_id=result["log_id"],
                request_id=result["request_id"],
                key_name=result["key_name"],
                model=result["model"],
                kinds={
                    kind: request_findings.get(kind, 0) + response_findings.get(kind, 0)
                    for kind in sorted(set(request_findings) | set(response_findings))
                },
            )
    return results


@router.get("/admin/pii-scan")
async def pii_scan_bulk(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
    limit: int = Query(100, ge=1, le=5000),
    key_name: str | None = None,
    model: str | None = None,
    status: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    """Scan recent request-log bodies for PII/secrets, on demand (a
    pure read — idempotent, nothing persisted). Returns per-row
    findings for the rows that have any, plus aggregates. Tenant
    admins are scoped to their own tenant (forced, like /admin/logs).
    Rows whose bodies were never stored report ``captured: false`` —
    counted separately from clean rows, never blurred."""
    config = raw_request.app.state.config.pii_scan
    if not config.enabled:
        raise GatewayError(404, "PII scan is disabled (pii_scan.enabled).",
                            "invalid_request_error")
    limit = min(limit, config.max_rows)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(RequestLog).order_by(RequestLog.id.desc())
        effective_tenant = admin.tenant_id if admin.tenant_id is not None else tenant_id
        if effective_tenant:
            stmt = stmt.where(RequestLog.tenant_id == effective_tenant)
        if key_name:
            stmt = stmt.where(RequestLog.key_name == key_name)
        if model:
            stmt = stmt.where(RequestLog.model == model)
        if status:
            stmt = stmt.where(RequestLog.status == status)
        rows = (await session.execute(stmt.limit(limit))).scalars().all()

    results = await _scan_and_record(raw_request, rows)
    flagged = [r for r in results if not r["clean"]]
    by_kind: dict[str, int] = {}
    for result in flagged:
        for direction in ("request", "response"):
            for kind, count in result["findings"][direction].items():
                by_kind[kind] = by_kind.get(kind, 0) + count
    return {
        "object": "pii_scan",
        "scanned": len(results),
        "with_findings": len(flagged),
        "bodies_not_captured": sum(1 for r in results if not r["captured"]),
        "kinds": sorted(raw_request.app.state.pii_scanner.detectors),
        "by_kind": dict(sorted(by_kind.items())),
        "rows": flagged,
    }


@router.get("/admin/logs/{log_id}/pii-scan")
async def pii_scan_one(
    log_id: int,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """PII/secret findings for one request-log row (kind + count
    only — the matched text is never returned). Same access rule as
    GET /admin/logs/{id}: tenant admins may scan their own tenant's
    rows only."""
    config = raw_request.app.state.config.pii_scan
    if not config.enabled:
        raise GatewayError(404, "PII scan is disabled (pii_scan.enabled).",
                            "invalid_request_error")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        log = await session.get(RequestLog, log_id)
        if log is None:
            raise GatewayError(404, f"Log entry {log_id} not found.",
                               "invalid_request_error")
        _require_log_access(admin, log)
    result = (await _scan_and_record(raw_request, [log]))[0]
    return {"object": "pii_scan_result", **result}


# ----------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------


def _percentile(sorted_values: list[int], pct: float) -> int | None:
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, max(0, round(pct * (len(sorted_values) - 1))))
    return sorted_values[idx]


@router.get("/admin/stats")
async def stats(
    admin: Annotated[AuthContext, Depends(require_admin)],
    hours: int = Query(24, ge=1, le=24 * 30),
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    # Client traffic only: shadow (mirrored) rows are excluded so they do
    # not pollute error rates, latency percentiles or cost totals.
    not_shadow = RequestLog.shadow.isnot(True)
    # Tenant admins see their own tenant's traffic only; platform admins
    # see everything, broken down by tenant.
    conds = [RequestLog.created_at >= since, not_shadow]
    if admin.tenant_id is not None:
        conds.append(RequestLog.tenant_id == admin.tenant_id)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        base = select(RequestLog).where(*conds)

        total = await session.scalar(select(func.count()).select_from(base.subquery()))

        by_status_rows = await session.execute(
            select(RequestLog.status, func.count()).where(*conds).group_by(RequestLog.status)
        )
        by_status = {status: count for status, count in by_status_rows}

        by_model_rows = await session.execute(
            select(
                RequestLog.model,
                func.count(),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                func.coalesce(func.sum(RequestLog.total_tokens), 0),
            )
            .where(*conds)
            .group_by(RequestLog.model)
        )
        by_model = [
            {"model": model, "requests": count, "cost_usd": round(cost, 6), "tokens": tokens}
            for model, count, cost, tokens in by_model_rows
        ]

        by_key_rows = await session.execute(
            select(
                RequestLog.key_name,
                func.count(),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
            )
            .where(*conds)
            .group_by(RequestLog.key_name)
        )
        by_key = [
            {"key_name": key, "requests": count, "cost_usd": round(cost, 6)}
            for key, count, cost in by_key_rows
        ]

        by_tenant: list[dict] | None = None
        if admin.tenant_id is None:  # platform admin: the whole picture
            by_tenant_rows = await session.execute(
                select(
                    RequestLog.tenant_id,
                    func.count(),
                    func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                )
                .where(*conds)
                .group_by(RequestLog.tenant_id)
            )
            by_tenant = [
                {"tenant_id": tenant, "requests": count, "cost_usd": round(cost, 6)}
                for tenant, count, cost in by_tenant_rows
            ]

        latency_rows = await session.execute(
            select(RequestLog.latency_ms)
            .where(
                *conds,
                RequestLog.latency_ms.isnot(None),
            )
            .order_by(RequestLog.latency_ms)
            .limit(5000)
        )
        latencies = [row[0] for row in latency_rows if row[0] is not None]

        # TTFT percentiles over streamed requests only — non-streaming
        # rows have nothing to measure (the whole response arrives at
        # once, so "first token" would just be the latency again).
        ttft_rows = await session.execute(
            select(RequestLog.ttft_ms)
            .where(
                *conds,
                RequestLog.ttft_ms.isnot(None),
            )
            .order_by(RequestLog.ttft_ms)
            .limit(5000)
        )
        ttfts = [row[0] for row in ttft_rows if row[0] is not None]

        tokens_row = await session.execute(
            select(
                func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
                func.coalesce(func.sum(RequestLog.completion_tokens), 0),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
            ).where(*conds)
        )
        prompt_tokens, completion_tokens, total_cost = tokens_row.one()

    errors = by_status.get("error", 0) + by_status.get("blocked", 0)
    return {
        "window_hours": hours,
        "total_requests": total,
        "by_status": by_status,
        "error_rate": (errors / total) if total else 0.0,
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
        },
        "cost_usd_total": round(total_cost, 6),
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
        },
        "ttft_ms": {
            "p50": _percentile(ttfts, 0.50),
            "p95": _percentile(ttfts, 0.95),
            "p99": _percentile(ttfts, 0.99),
        },
        "by_model": by_model,
        "by_key": by_key,
        **({"by_tenant": by_tenant} if by_tenant is not None else {}),
    }


# ----------------------------------------------------------------------
# Usage export (billing / chargeback)
# ----------------------------------------------------------------------

_USAGE_CSV_COLUMNS = (
    "request_id",
    "created_at",
    "key_name",
    "tenant_id",
    "model",
    "provider",
    "deployment",
    "status",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "total_tokens",
    "cost_usd",
    "latency_ms",
    "cache_hit",
    "stream",
    "shadow",
)


def _usage_row(row: RequestLog) -> dict:
    """Billing-relevant log fields (stable set + column order for CSV)."""
    return {
        "request_id": row.request_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "key_name": row.key_name,
        "tenant_id": row.tenant_id,
        "model": row.model,
        "provider": row.provider,
        "deployment": row.deployment,
        "status": row.status,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "cached_tokens": row.cached_tokens,
        "total_tokens": row.total_tokens,
        "cost_usd": round(row.cost_usd, 6) if row.cost_usd is not None else None,
        "latency_ms": row.latency_ms,
        "cache_hit": bool(row.cache_hit),
        "stream": bool(row.stream),
        "shadow": bool(row.shadow),
        "prompt_variant": row.prompt_variant,
    }


def _parse_range_ts(value: str, field: str) -> datetime:
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GatewayError(
            400, f"Invalid {field}: {exc}", "invalid_request_error", param=field
        ) from exc
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


@router.get("/admin/usage/export")
async def export_usage(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
    format: str = Query("json", pattern="^(json|csv)$"),
    since: str | None = None,
    until: str | None = None,
    key_name: str | None = None,
    model: str | None = None,
    status: str | None = None,
    tenant_id: str | None = None,
    include_shadow: bool = False,
    limit: int = Query(100_000, ge=1, le=250_000),
) -> Response:
    """Usage export for billing / chargeback: request rows plus aggregates.

    Rows are request-log entries in ``[since, until]`` (ISO-8601
    timestamps; naive values are read as UTC; defaults: the last 30 days
    up to now), filterable by key, model, tenant and status. Tenant
    admins export only their own tenant's usage (the filter is forced);
    platform admins may filter by any tenant or export everything.
    Mirrored (shadow) calls are excluded by default — they never cost
    the client anything — ``include_shadow=true`` includes them
    (flagged). ``format=json`` returns ``{summary, requests}`` with
    totals and per-key / per-model / per-status breakdowns;
    ``format=csv`` returns one row per request in a stable column order
    (download attachment).
    """
    until_ts = (
        _parse_range_ts(until, "until") if until else datetime.now(timezone.utc)
    )
    since_ts = (
        _parse_range_ts(since, "since") if since else until_ts - timedelta(days=30)
    )
    if since_ts > until_ts:
        raise GatewayError(
            400, "'since' must not be after 'until'.", "invalid_request_error"
        )

    conditions = [
        RequestLog.created_at >= since_ts,
        RequestLog.created_at <= until_ts,
    ]
    if not include_shadow:
        conditions.append(RequestLog.shadow.isnot(True))
    # Tenant scoping: forced for tenant admins, opt-in for platform.
    effective_tenant = admin.tenant_id if admin.tenant_id is not None else tenant_id
    if effective_tenant:
        conditions.append(RequestLog.tenant_id == effective_tenant)
    if key_name:
        conditions.append(RequestLog.key_name == key_name)
    if model:
        conditions.append(RequestLog.model == model)
    if status:
        conditions.append(RequestLog.status == status)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(RequestLog)
                    .where(*conditions)
                    .order_by(RequestLog.created_at, RequestLog.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

        async def _grouped(*group_cols):
            stmt = (
                select(
                    *group_cols,
                    func.count(),
                    func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
                    func.coalesce(func.sum(RequestLog.completion_tokens), 0),
                    func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                )
                .where(*conditions)
                .group_by(*group_cols)
                .order_by(func.sum(RequestLog.cost_usd).desc())
            )
            return (await session.execute(stmt)).all()

        totals = (
            await session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
                    func.coalesce(func.sum(RequestLog.completion_tokens), 0),
                    func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                ).where(*conditions)
            )
        ).one()
        by_status_rows = await _grouped(RequestLog.status)
        by_key_rows = await _grouped(RequestLog.key_name)
        by_model_rows = await _grouped(RequestLog.model)
        # Per-tenant chargeback split, platform admins only (a tenant
        # admin's export is their own tenant by construction).
        by_tenant_rows = (
            await _grouped(RequestLog.tenant_id) if admin.tenant_id is None else None
        )

    total_count, prompt_tokens, completion_tokens, total_cost = totals

    def _entries(rows_, label):
        return [
            {
                label: value,
                "requests": count,
                "prompt_tokens": p,
                "completion_tokens": c,
                "cost_usd": round(cost, 6),
            }
            for value, count, p, c, cost in rows_
        ]

    summary = {
        "range": {"since": since_ts.isoformat(), "until": until_ts.isoformat()},
        "filters": {
            "key_name": key_name,
            "model": model,
            "status": status,
            "tenant_id": effective_tenant,
            "include_shadow": include_shadow,
        },
        "requests": total_count,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost_usd": round(total_cost, 6),
        "by_status": {
            value: {"requests": count, "cost_usd": round(cost, 6)}
            for value, count, _, _, cost in by_status_rows
        },
        "by_key": _entries(by_key_rows, "key_name"),
        "by_model": _entries(by_model_rows, "model"),
        **(
            {"by_tenant": _entries(by_tenant_rows, "tenant_id")}
            if by_tenant_rows is not None
            else {}
        ),
        "row_count": len(rows),
        # the row list is capped by `limit`; aggregates never are
        "truncated": len(rows) < total_count,
    }

    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_USAGE_CSV_COLUMNS)
        for row in (_usage_row(r) for r in rows):
            writer.writerow(
                [
                    (
                        str(row[col]).lower()
                        if isinstance(row[col], bool)
                        else ("" if row[col] is None else row[col])
                    )
                    for col in _USAGE_CSV_COLUMNS
                ]
            )
        filename = f"usage_{since_ts:%Y%m%d}_{until_ts:%Y%m%d}.csv"
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return JSONResponse(
        {
            "object": "usage_export",
            "summary": summary,
            "requests": [_usage_row(r) for r in rows],
        }
    )


# ----------------------------------------------------------------------
# Introspection: routing table + guardrails
# ----------------------------------------------------------------------


@router.get("/admin/routing")
async def routing_table(raw_request: Request, admin: Annotated[AuthContext, Depends(require_platform_admin)]) -> dict:
    config = raw_request.app.state.config
    breakers = raw_request.app.state.pipeline.router.breakers
    cost = raw_request.app.state.pipeline.router._cost

    def _pricing(deployment_model: str) -> dict | None:
        """Per-1M prices the router ranks by (looked up on the upstream
        model, the same key billing uses). None = unpriced (sorts last
        when cost_aware is on; calculate() bills nothing)."""
        prices = cost.lookup(deployment_model)
        if prices is None:
            return None
        return {
            "input_usd_per_mtok": prices[0],
            "output_usd_per_mtok": prices[1],
            "cached_input_usd_per_mtok": prices[2],
        }

    snapshots = await breakers.snapshots()
    return {
        "circuit_breaker": {
            "enabled": breakers.enabled,
            "deployments": snapshots,
        },
        # Cost-aware routing: cheapest-priced deployment first within a
        # priority tier (see routing.cost_aware).
        "cost_aware": config.routing.cost_aware,
        "models": [
            {
                "name": model.name,
                "context_window": model.context_window,
                "deployments": [
                    {
                        "name": d.name,
                        "provider": d.provider,
                        "model": d.model,
                        "priority": d.priority,
                        "weight": d.weight,
                        "shadow": d.shadow,
                        "shadow_fraction": d.shadow_fraction,
                        "pricing": _pricing(d.model),
                        "breaker": snapshots.get(d.name),
                    }
                    for d in model.deployments
                ],
            }
            for model in config.models
        ],
    }


@router.post("/admin/routing/breakers/{deployment_name:path}/reset")
async def reset_circuit(
    deployment_name: str,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Force-close a deployment's circuit (next request probes it again)."""
    breakers = raw_request.app.state.pipeline.router.breakers
    try:
        await breakers.reset(deployment_name)
    except KeyError:
        raise GatewayError(
            404,
            f"Deployment '{deployment_name}' does not exist.",
            "invalid_request_error",
            code="deployment_not_found",
        )
    raw_request.app.state.audit.record(
        "breaker.reset", actor=admin, deployment=deployment_name
    )
    return {
        "deployment": deployment_name,
        "breaker": (await breakers.snapshots())[deployment_name],
    }


@router.get("/admin/guardrails")
async def guardrails(raw_request: Request, admin: Annotated[AuthContext, Depends(require_platform_admin)]) -> dict:
    manager = raw_request.app.state.pipeline.guardrails
    return {"guardrails": manager.describe()}


# ----------------------------------------------------------------------
# Runaway-agent protection: jail view + release
# ----------------------------------------------------------------------


@router.get("/admin/runaway")
async def runaway_view(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Runaway-agent protection: currently jailed keys (with remaining
    cooldown and offense count), the live config echo, and the lifetime
    trip count per key. Read-only — POST .../release is the escape
    hatch. In-process state, so a multi-replica fleet reports its own
    jail here."""
    pipeline = raw_request.app.state.pipeline
    protector = pipeline.runaway
    cfg = pipeline.config.runaway
    jailed = await protector.snapshot() if protector is not None else []
    # Join the key rows for display names — the jail stores key ids.
    names: dict[str, str] = {}
    ids = [entry["key_id"] for entry in jailed]
    if ids:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            rows = await session.execute(
                select(VirtualKey).where(VirtualKey.id.in_(ids))
            )
            names = {vk.id: vk.name for vk in rows.scalars()}
    return {
        "enabled": cfg.enabled,
        "config": cfg.model_dump(),
        "jailed": [
            {**entry, "key_name": names.get(entry["key_id"])}
            for entry in jailed
        ],
    }


@router.post("/admin/runaway/{key_id}/release")
async def runaway_release(
    key_id: str,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Release a key from the runaway cooldown jail: clears the jail,
    the offense history (so a re-trip starts back at the first-offense
    cooldown, not escalated) and the loop windows. The key serves again
    immediately; a genuinely still-looping agent re-trips on fresh hits."""
    protector = raw_request.app.state.pipeline.runaway
    if protector is None:  # standalone pipeline construction — never via create_app
        raise GatewayError(
            404, "Runaway-agent protection is not active.", "invalid_request_error"
        )
    released = await protector.release(key_id)
    if not released:
        raise GatewayError(
            404,
            f"Key '{key_id}' is not in a runaway cooldown jail.",
            "invalid_request_error",
            code="key_not_jailed",
        )
    raw_request.app.state.audit.record(
        "runaway.released", actor=admin, key_id=key_id
    )
    return {"key_id": key_id, "released": True}


# ----------------------------------------------------------------------
# Operations: hot-reload + SQLite backup/restore
# ----------------------------------------------------------------------


@router.post("/admin/reload")
async def reload_config(
    raw_request: Request, admin: Annotated[AuthContext, Depends(require_platform_admin)]
) -> dict:
    """Re-read config.yaml and swap the policy components without
    restarting (same path SIGHUP takes).

    Reloads guardrails, models/deployments, routing policy, pricing and
    prompt-policy knobs. Infra-bound settings (provider credentials, the
    auth chain, cache/rate-limit backends, telemetry) are not hot-swapped;
    a config that changed any of them reports it in ``restart_required``.
    An invalid config is a 400 and leaves the running one live."""
    from ..main import reload_gateway

    try:
        summary = await reload_gateway(raw_request.app, source="admin")
    except Exception as exc:  # noqa: BLE001 — any load/validate failure = bad config
        raise GatewayError(
            400,
            f"Configuration reload failed (running config unchanged): {exc}",
            "invalid_request_error",
            code="config_invalid",
        ) from exc
    raw_request.app.state.audit.record(
        "config.reloaded",
        actor=admin,
        config_path=summary["config_path"],
        source=summary["source"],
    )
    return summary


_BACKUP_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


@router.post("/admin/backup", status_code=201)
async def create_backup(
    raw_request: Request, admin: Annotated[AuthContext, Depends(require_platform_admin)]
) -> dict:
    """Snapshot the SQLite database to ``settings.backup_dir``.

    Uses SQLite's online ``VACUUM INTO``: a consistent, compact copy of
    the live database without blocking traffic (safe while requests are
    being served and logged). Restore is deliberately offline — stop the
    gateway, replace the database file, start it again; restoring under
    a live engine is a footgun the API refuses to be.

    SQLite-only by construction: any other backend (PostgreSQL) is
    refused with a 400 pointing at the platform's own tooling
    (pg_dump) — a half-copy through a different mechanism would be a
    false sense of safety.
    """
    from ..db import get_engine

    engine = get_engine()
    if engine.dialect.name != "sqlite":
        raise GatewayError(
            400,
            "Online backups are SQLite-only (VACUUM INTO). For a "
            f"{engine.dialect.name} database, use its own tooling "
            "(pg_dump for PostgreSQL).",
            "invalid_request_error",
            code="backup_not_supported",
        )
    backup_dir = Path(raw_request.app.state.settings.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"gateway-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.db"
    async with engine.connect() as conn:
        await conn.exec_driver_sql("VACUUM INTO ?", (str(path),))
    size = path.stat().st_size
    raw_request.app.state.audit.record(
        "backup.created", actor=admin, name=path.name, size_bytes=size
    )
    return {"name": path.name, "path": str(path), "size_bytes": size}


@router.get("/admin/backups")
async def list_backups(
    raw_request: Request, admin: Annotated[AuthContext, Depends(require_platform_admin)]
) -> dict:
    backup_dir = Path(raw_request.app.state.settings.backup_dir)
    backups = []
    if backup_dir.is_dir():
        # Newest first: mtime desc (name = timestamp + random suffix, so
        # same-second backups tie-break deterministically by name).
        entries = [
            (entry.stat().st_mtime, entry.name, entry)
            for entry in backup_dir.iterdir()
            if entry.is_file() and entry.name.endswith(".db")
        ]
        for _, name, entry in sorted(entries, reverse=True):
            backups.append(
                {
                    "name": name,
                    "size_bytes": entry.stat().st_size,
                    "created_at": datetime.fromtimestamp(
                        entry.stat().st_mtime, tz=timezone.utc
                    ).isoformat(),
                }
            )
    return {"object": "list", "data": backups}


@router.delete("/admin/backups/{name}")
async def delete_backup(
    name: str,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)]
) -> dict:
    """Delete one backup by file name (flat names only — no path
    separators accepted, so the endpoint can never reach outside the
    backup directory)."""
    if not name or set(name) - _BACKUP_NAME_CHARS:
        raise GatewayError(
            400,
            "Invalid backup name.",
            "invalid_request_error",
            code="invalid_backup_name",
        )
    backup_dir = Path(raw_request.app.state.settings.backup_dir)
    target = backup_dir / name
    if not target.is_file():
        raise GatewayError(
            404,
            f"Backup '{name}' does not exist.",
            "invalid_request_error",
            code="backup_not_found",
        )
    target.unlink()
    raw_request.app.state.audit.record("backup.deleted", actor=admin, name=name)
    return {"deleted": name}
