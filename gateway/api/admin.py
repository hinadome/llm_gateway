"""Admin API: virtual key management, request logs, stats, usage export, routing/guardrail inspection."""

from __future__ import annotations

import csv
import io
import json
import secrets
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from sqlalchemy import func, select, update

from ..core.alerts import BudgetAlertManager
from ..core.auth import (  # noqa: F401 — _is_expired re-exported for the playground's key-resolution mirror
    _is_expired,
    auth_context_from_key,
    get_key_by_name,
    load_tenant_for_key,
)
from ..core.context import AuthContext
from ..config import GatewayConfig
from ..core.prompt_library import PromptLibrary
from ..core.replay import replay_entries
from ..core.shadow_diff import VERDICTS, aggregate as shadow_aggregate, pairs as shadow_pairs
from ..core import billing as billing_core
from ..core import finops as finops_core
from ..core import migration as migration_core
from ..core import promotion as promotion_core
from ..core import slos as slos_core
from ..core import slo_alerts as slo_alerts_core
from ..core import trials as trials_core
from ..core.vault import VaultService
from ..db import get_sessionmaker
from ..errors import GatewayError
from ..models import Feedback, RequestLog, Tenant, Trial, VirtualKey
from ..observability.metrics import PII_FINDINGS
from ..schemas import (
    ChatCompletionRequest,
    KeyCreateRequest,
    KeyUpdateRequest,
    PlaygroundChatRequest,
    PromotionRequest,
    PromptCreateRequest,
    PromptPatchRequest,
    ProviderKeyUpsertRequest,
    ReplayRequest,
    RollbackRequest,
    TenantCreateRequest,
    TenantUpdateRequest,
    TrialCreateRequest,
)
from .deps import ENDPOINT_SCOPES, hash_key, require_admin, require_platform_admin

router = APIRouter()

# Shadow-diff verdict vocabulary, single-sourced from the scorer —
# the /admin/shadow-diff/pairs ?verdict= filter accepts exactly these.
_VERDICT_PATTERN = "^(" + "|".join(VERDICTS) + ")$"

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
            allowed_tools=body.allowed_tools,
            allowed_mcp_servers=body.allowed_mcp_servers,
            rpm_limit=body.rpm_limit,
            tpm_limit=body.tpm_limit,
            max_concurrency=body.max_concurrency,
            priority=body.priority,
            max_budget_usd=body.max_budget_usd,
            daily_budget_usd=body.daily_budget_usd,
            monthly_budget_usd=body.monthly_budget_usd,
            expires_at=_parse_expires(body.expires_at),
            metadata_json=body.metadata,
            disabled_guardrails=body.disabled_guardrails,
            required_residencies=body.required_residencies,
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
            "allowed_tools",
            "allowed_mcp_servers",
            "rpm_limit",
            "tpm_limit",
            "max_concurrency",
            "max_budget_usd",
            "daily_budget_usd",
            "monthly_budget_usd",
            "is_active",
            "disabled_guardrails",
            "required_residencies",
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
        if "priority" in updates:
            # Non-null column: an explicit null means "back to default 0".
            vkey.priority = updates["priority"] or 0
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
            allowed_tools=body.allowed_tools,
            allowed_mcp_servers=body.allowed_mcp_servers,
            rpm_limit=body.rpm_limit,
            tpm_limit=body.tpm_limit,
            pricing_markup_pct=body.pricing_markup_pct,
            required_residencies=body.required_residencies,
            max_concurrency=body.max_concurrency,
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
            "allowed_tools",
            "allowed_mcp_servers",
            "rpm_limit",
            "tpm_limit",
            "pricing_markup_pct",
            "required_residencies",
            "max_concurrency",
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
# BYOK vault: tenant-owned provider credentials (tier 31)
# ----------------------------------------------------------------------


async def _vault_tenant_guard(
    session, tenant_id: str, admin: AuthContext
) -> Tenant:
    """Shared guard for the vault endpoints: the tenant must exist, and
    a *tenant admin* may only touch their own tenant (cross-tenant ids
    404 — same anti-leak semantics as key management; platform admins
    are not restricted)."""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise GatewayError(
            404, f"Tenant '{tenant_id}' not found.", "invalid_request_error"
        )
    if admin.tenant_id is not None and admin.tenant_id != tenant_id:
        raise GatewayError(
            404, f"Tenant '{tenant_id}' not found.", "invalid_request_error"
        )
    return tenant


@router.get("/admin/vault")
async def vault_status(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """The BYOK vault at a glance (tier 34): enabled or the honest
    reason it is not, the strict stance, the cache TTL and the
    invalidation backend. No secrets anywhere in the payload — the
    unavailable reason names the missing SETTING, never a value.

    ``admin_context`` tells the dashboard which tenant a *tenant
    admin* manages (their own — the only one the provider-key
    endpoints let them touch), so the Vault tab works for both admin
    kinds; platform admins get null and pick a tenant from the list.
    """
    vault: VaultService = raw_request.app.state.vault
    config = raw_request.app.state.config
    return {
        "enabled": vault.enabled,
        "unavailable_reason": (
            None if vault.enabled else vault.unavailable_reason
        ),
        "strict": config.vault.strict,
        "ttl_seconds": config.vault.ttl_seconds,
        "invalidation_backend": config.vault.backend,
        "admin_context": (
            None
            if admin.tenant_id is None
            else {
                "tenant_id": admin.tenant_id,
                "tenant_name": admin.tenant_name,
            }
        ),
    }


@router.get("/admin/tenants/{tenant_id}/provider-keys")
async def list_provider_keys(
    tenant_id: str,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Metadata for every provider credential a tenant stored: provider,
    label, key hint (last characters — never the key), timestamps. The
    vault being disabled is not an error for reads: an empty list is an
    empty list."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant = await _vault_tenant_guard(session, tenant_id, admin)
        vault: VaultService = raw_request.app.state.vault
        data = await vault.list_for_tenant(tenant.id)
    return {"object": "list", "tenant_id": tenant.id, "data": data}


@router.put("/admin/tenants/{tenant_id}/provider-keys/{provider}")
async def upsert_provider_key(
    tenant_id: str,
    provider: str,
    body: ProviderKeyUpsertRequest,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Store (or replace) a tenant's credential for one provider. From
    the next request on (within the cache TTL), every request from that
    tenant through that provider rides the tenant's account instead of
    the platform's configured key — chat, embeddings, images and audio
    included. The key itself is encrypted at rest and never returned,
    not even to the caller that just sent it; the response and the
    audit record carry only the hint."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant = await _vault_tenant_guard(session, tenant_id, admin)
        vault: VaultService = raw_request.app.state.vault
        result = await vault.put(tenant.id, provider, body.key, label=body.label)
    raw_request.app.state.audit.record(
        "vault.provider_key.upsert",
        actor=admin,
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        provider=provider,
        key_hint=result["key_hint"],
    )
    return result


@router.delete("/admin/tenants/{tenant_id}/provider-keys/{provider}")
async def delete_provider_key(
    tenant_id: str,
    provider: str,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Remove a tenant's stored credential. The next request (within
    the cache TTL) rides the platform's configured key again. Deleting
    a nonexistent entry is a 404, not a silent success — an operator
    deserves to know the stored key they thought was there is not."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant = await _vault_tenant_guard(session, tenant_id, admin)
        vault: VaultService = raw_request.app.state.vault
        deleted = await vault.delete(tenant.id, provider)
        if not deleted:
            raise GatewayError(
                404,
                f"No stored provider key for tenant '{tenant.name}' / "
                f"provider '{provider}'.",
                "invalid_request_error",
            )
    raw_request.app.state.audit.record(
        "vault.provider_key.delete",
        actor=admin,
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        provider=provider,
    )
    return {"tenant_id": tenant.id, "provider": provider, "deleted": True}


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
        "shadow_verdict": row.shadow_verdict,
        "shadow_similarity": row.shadow_similarity,
        # Candidate trial (tier 36): the run this shadow row replays for
        # (None on primaries, mirrors and prompt A/B rows).
        "trial_id": row.trial_id,
        # BYOK vault: the provider whose upstream call used the TENANT'S
        # stored credential (None = platform credential). Observability
        # only — the credential itself is never stored anywhere.
        "byok_provider": row.byok_provider,
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
        "shadow_verdict": row.shadow_verdict,
        "shadow_similarity": row.shadow_similarity,
        "trial_id": row.trial_id,
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


@router.get("/admin/invoices")
async def invoice(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
    format: str = Query("json", pattern="^(json|csv)$"),
    month: str | None = None,
    tenant_id: str | None = None,
) -> Response:
    """Usage invoices (tier 45): the billing epic's first surface — a
    month statement of what each key and tenant consumed.

    The usage export answers "which requests did this key make?" one
    row at a time; this answers what the finance side asks: "what
    did each tenant consume in October, per key, per model?" The
    same request-log rows rolled up into line items — requests,
    errors, the token splits (cached included — a cached answer is a
    real served request, already billed at the cached rate), and
    cost — with the key's and tenant's ``monthly_budget_usd`` beside
    the spend, so a statement doubles as a spend-vs-plan check.
    ``month=YYYY-MM`` selects the period (calendar months in UTC;
    default: the current month; a future month is a 400 — an
    invoice for a period that has not happened must not pretend to
    be empty).

    The billing lens is CLIENT-CONSUMED usage: shadow mirrors (they
    never cost the client anything) and candidate-trial replays
    (deliberately re-run traffic, not the tenant's doing) are
    excluded. Keys with no traffic in the period still appear — a
    zero-spend section with no line items, because an unused key is
    a finops finding, not an absence — and keys whose row was
    deleted keep their line items under the tenant they lived in.

    Tenant admins invoice only their own tenant (the scope is
    forced); platform admins may filter by ``?tenant_id=`` or read
    the whole platform. Everything sorts by spend — the big numbers
    are the top of the statement. ``format=csv`` returns one row per
    line item in a stable column order (download attachment).
    """
    # Tenant scoping: forced for tenant admins, opt-in for platform.
    effective_tenant = admin.tenant_id if admin.tenant_id is not None else tenant_id
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        data = await billing_core.statement(session, month, tenant_id=effective_tenant)

    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(billing_core.INVOICE_CSV_COLUMNS)
        columns = billing_core.INVOICE_CSV_COLUMNS
        for tenant_section in [*data["tenants"]]:
            tenant_name = tenant_section["tenant_name"]
            for key_section in tenant_section["keys"]:
                for line in key_section["line_items"]:
                    writer.writerow(
                        [
                            data["month"],
                            tenant_section["tenant_id"],
                            tenant_name or "",
                            key_section["key_id"] or "",
                            key_section["key_name"] or "",
                            line["model"],
                            *[line[col] for col in columns[6:]],
                        ]
                    )
        for key_section in data["unassigned"]:
            for line in key_section["line_items"]:
                writer.writerow(
                    [
                        data["month"],
                        "",
                        "",
                        key_section["key_id"] or "",
                        key_section["key_name"] or "",
                        line["model"],
                        *[line[col] for col in columns[6:]],
                    ]
                )
        filename = f"invoice_{data['month']}.csv"
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return JSONResponse(
        {
            "object": "invoice",
            **data,
        }
    )


@router.get("/admin/anomalies")
async def anomaly_flags(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_admin)],
    tenant_id: str | None = None,
) -> dict:
    """Cost anomaly flags (tier 46): unusual money and traffic
    patterns on the request logs, surfaced as flags — never pager
    alerts (error-rate health is the SLO epic's job; this is the
    money lens). Four shapes, each a billing surprise in its own
    way: ``cost_spike`` (a key spending several times its trailing
    daily rate), ``traffic_spike`` (the request-rate version),
    ``traffic_stop`` (a key with real baseline traffic that went
    silent — the churn/broken-integration signal a spend dashboard
    structurally cannot show), and ``first_seen_model`` (something
    new on the bill).

    The comparison: the recent ``window_hours`` (default 24) against
    the trailing ``baseline_days`` (default 14) immediately before
    it, over the client-consumed lens (shadow mirrors and trial
    replays excluded, cache hits included — the tier-45 rules). The
    honesty rule is the SLO tiers': **a thin baseline is not a
    verdict** — fewer than ``min_baseline_requests`` baseline rows
    (default 10) means no spike and no silence, never a scary ratio
    on thin evidence; a brand-new key cannot spike at all. A
    baseline that cost nothing with window spend fires with
    ``ratio: null`` and a message saying so — never a fabricated
    infinity.

    Every flag carries its own numbers (window vs baseline requests
    and cost, the ratio where one exists, the bounds in the
    envelope) so a flag is auditable without calling back.
    Read-at-request-time — no loop, no state, hot-reload applies
    instantly. ``finops.enabled: false`` serves an honest empty list
    while the invoice keeps serving. Tenant admins see their own
    tenant only (the scope is forced); platform admins may filter
    ``?tenant_id=`` or read the whole platform.
    """
    # Tenant scoping: forced for tenant admins, opt-in for platform
    # (the invoice's rule — the anomalies are read beside it).
    effective_tenant = admin.tenant_id if admin.tenant_id is not None else tenant_id
    config = raw_request.app.state.config
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        data = await finops_core.anomalies(session, config.finops, tenant_id=effective_tenant)
    return {
        "object": "anomalies",
        "config": config.finops.model_dump(),
        **data,
    }


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
                        "residency": d.residency,
                        "pricing": _pricing(d.model),
                        "breaker": snapshots.get(d.name),
                    }
                    for d in model.deployments
                ],
            }
            for model in config.models
        ],
    }


@router.get("/admin/shadow-diff")
async def shadow_diff_summary(
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
    model: str | None = None,
    since: str | None = None,
) -> dict:
    """Shadow diffing — the migration-evidence aggregate (tier 35).

    Shadow mirroring writes paired rows (same request_id); this scores
    them into go/no-go numbers per (model, shadow deployment): pair
    counts, the verdict breakdown (exact / near / divergent /
    shadow_error / not_comparable), match rate, mean similarity, and
    the latency / token / cost deltas against the paired primaries —
    the answers an operator needs before promoting a candidate to
    serving. Deployment mirrors only (prompt A/B rows are excluded —
    their divergence is by design); rows scored before tier 35 carry
    no verdict and are not re-guessed. ``?since=`` (ISO-8601) scopes
    the window, ``?model=`` to one public model.

    Platform-admin-only like /admin/routing: the pairs carry request
    BODIES, which cross tenants — a tenant admin cannot read them.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return await shadow_aggregate(
            session,
            model=model,
            since=_parse_range_ts(since, "since") if since else None,
        )


@router.get("/admin/shadow-diff/pairs")
async def shadow_diff_pair_list(
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
    model: str = Query(...),
    deployment: str = Query(...),
    verdict: str | None = Query(None, pattern=_VERDICT_PATTERN),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    """The shadow-diff drilldown: scored pairs for one shadow
    deployment, newest first — the divergent ones (``?verdict=
    divergent``) are the migration review queue. Each pair carries both
    sides' status / latency / tokens / cost and the stored bodies' text
    excerpts; texts are null on rows whose bodies were not persisted
    (``logging.store_bodies`` off) — the verdict still exists, because
    it was computed in memory at mirror time."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return await shadow_pairs(
            session,
            model=model,
            deployment=deployment,
            verdict=verdict,
            limit=limit,
            offset=offset,
        )


# -- migration verdicts (tier 37) -----------------------------------------
# The go/no-go recommendation engine over the migration evidence:
# core/migration.evaluate turns a shadow-diff mirror group (and, via
# the folded summaries, a candidate-trial run) into go / hold / no_go
# / insufficient_evidence with the per-check numbers that drove it.
# Platform-admin-only like the evidence it reads.


@router.get("/admin/migration/verdicts")
async def migration_verdicts(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
    model: str | None = None,
    deployment: str | None = None,
    since: str | None = None,
) -> dict:
    """Go/no-go recommendations for the mirrored (standing) migration
    evidence — one row per scored shadow-deployment group, carrying
    the full evidence group plus its recommendation: verdict (go /
    hold / no_go / insufficient_evidence), per-check measurements and
    the reasons for every failed check. Thresholds come from the live
    ``migration:`` config and are echoed in the response, so a verdict
    is always auditable against the bars that produced it.
    ``?model=`` / ``?deployment=`` narrow to one candidate,
    ``?since=`` (ISO-8601) bounds the evidence window (stale pairs —
    from before a candidate's last update — can be excluded). Trial
    evidence is NOT included here: the trials endpoints carry their own
    per-run recommendations."""
    config = raw_request.app.state.config
    since_ts = _parse_range_ts(since, "since") if since else None
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        agg = await shadow_aggregate(session, model=model, since=since_ts)
    data = []
    for group in agg["groups"]:
        if deployment is not None and group["deployment"] != deployment:
            continue
        data.append(
            {
                **group,
                "recommendation": migration_core.evaluate(group, config.migration),
            }
        )
    return {"data": data, "thresholds": config.migration.model_dump()}


# -- migration promotion (tier 38) -----------------------------------------
# The deliberate act: a go verdict turned into serving traffic through
# config.yaml itself — plan renders the exact edits, promote applies
# them (backup -> write -> the existing hot-reload path -> audit), so
# the file stays the single routing truth. Platform-admin-only: this
# REWRITES the routing configuration.


async def _promotion_evidence(
    app: Any, model: str, deployment: str
) -> tuple[dict, dict, str]:
    """The evidence a promotion is judged on, at apply time: the
    standing mirror group for (model, candidate) — live traffic, the
    loudest evidence when it exists — else the latest completed trial
    for the pair (deliberate replay), else an empty group (which
    verdicts insufficient_evidence). Returns ``(group, recommendation,
    source)`` with ``source`` saying which leg the verdict came from."""
    config = app.state.config
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        agg = await shadow_aggregate(session, model=model)
        group = next(
            (g for g in agg["groups"] if g["deployment"] == deployment), None
        )
        source = "mirror"
        if group is None:
            row = (
                await session.execute(
                    select(Trial)
                    .where(
                        Trial.model == model,
                        Trial.deployment == deployment,
                        Trial.status == "completed",
                    )
                    .order_by(Trial.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                group, source = {}, "none"
            else:
                group = await trials_core.run_summary(session, row.id)
                source = f"trial:{row.id}"
        recommendation = migration_core.evaluate(group, config.migration)
    return group, recommendation, source


def _promotion_plan(app: Any, body: PromotionRequest) -> dict:
    """Build the plan against the config file as it is RIGHT NOW (the
    running config is the ground truth for what the candidate is; the
    file is what gets edited). PromotionError maps straight onto the
    OpenAI-style error envelope — the message is the whole story."""
    path = Path(app.state.settings.config_path)
    if not path.exists():
        raise GatewayError(
            409,
            f"Config file not found at {path} — promotion edits the file "
            "the gateway loads, so the file must exist.",
            "invalid_request_error",
            code="config_file_missing",
        )
    text = promotion_core.read_config_text(path)
    try:
        return promotion_core.build_plan(
            text, app.state.config, body.model, body.deployment, body.weight
        )
    except promotion_core.PromotionError as exc:
        raise GatewayError(
            exc.status_code, exc.message, "invalid_request_error", code=exc.code
        ) from exc


@router.post("/admin/migration/plan")
async def migration_promotion_plan(
    raw_request: Request,
    body: PromotionRequest,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Render the exact config.yaml edits a promotion would apply —
    and the verdict justifying them — with NO side effects: the file
    is read, never written, nothing is audited, no evidence moves.

    The response carries the promotion mode (``shadow_flip`` — a shadow
    twin flips to serving; ``rebalance`` — an already-serving
    deployment's weight changes; ``cross_model_insert`` — a candidate
    living on another model is copied into the target model's
    deployments), the structured ``changes`` list, a unified ``diff`` of
    the file, the candidate's resulting ``canary_share`` of its
    priority tier, the evidence ``recommendation`` (go / hold / no_go /
    insufficient_evidence with per-check numbers) and which leg it
    came from (``evidence_source``: mirror / trial:<id> / none).
    ``promote`` re-builds the plan at apply time — this preview is
    never applied stale."""
    plan = _promotion_plan(raw_request.app, body)
    _group, recommendation, source = await _promotion_evidence(
        raw_request.app, body.model, body.deployment
    )
    plan.pop("new_text")  # the diff is the preview; the text is apply's
    return {**plan, "recommendation": recommendation, "evidence_source": source}


@router.post("/admin/migration/promote")
async def migration_promote(
    raw_request: Request,
    body: PromotionRequest,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Apply a promotion: flip the candidate to serving (or rebalance
    its weight, or insert it into the target model) at ``weight`` in
    the incumbent's priority tier — through config.yaml itself, so the
    file stays the single routing truth.

    The gate: the evidence at apply time must not verdict ``no_go``
    (answers diverge, or the candidate fails the traffic it is given)
    unless ``force=true`` — a forced promotion is audited as forced.
    A ``hold`` or ``insufficient_evidence`` verdict does not block; the
    concern rides along as a warning on the promotion. The incumbent
    is never demoted or removed — promotion only adds serving
    capacity, and nothing is ever auto-removed: repeat promote at a
    higher weight to climb the canary ladder.

    The apply: timestamped backup -> write -> the same hot-reload path
    POST /admin/reload takes (an invalid edit leaves the running
    config live and the file restored from the backup) -> audit
    ``promotion.applied``. The response carries the plan (mode,
    changes, diff, canary_share, warnings), the verdict it was judged
    on, the backup name and the reload summary."""
    plan = _promotion_plan(raw_request.app, body)
    _group, recommendation, source = await _promotion_evidence(
        raw_request.app, body.model, body.deployment
    )
    if (
        recommendation["verdict"] == migration_core.RECOMMENDATION_NO_GO
        and not body.force
    ):
        reasons = "; ".join(recommendation["reasons"]) or "no reasons recorded"
        raise GatewayError(
            409,
            f"Promotion blocked: the evidence ({source}) for "
            f"'{body.model}' / '{body.deployment}' verdicts no_go — "
            f"{reasons}. Address the findings, collect fresh evidence, "
            "or promote anyway with force=true (audited).",
            "invalid_request_error",
            code="promotion_blocked",
        )
    if recommendation["verdict"] != migration_core.RECOMMENDATION_GO:
        plan["warnings"].append(
            f"evidence verdict is '{recommendation['verdict']}' ({source}): "
            + ("; ".join(recommendation["reasons"]) or "no reasons recorded")
        )

    path = Path(raw_request.app.state.settings.config_path)
    backup = promotion_core.write_with_backup(path, plan["new_text"])

    from ..main import reload_gateway  # same import stance as /admin/reload

    try:
        reload_summary = await reload_gateway(raw_request.app, source="promotion")
    except Exception as exc:  # noqa: BLE001 — any load/validate failure
        promotion_core.restore_backup(backup, path)
        raise GatewayError(
            400,
            f"Promotion failed: the edited config did not reload (running "
            f"config unchanged, file restored from {backup.name}): {exc}",
            "invalid_request_error",
            code="config_invalid",
        ) from exc

    raw_request.app.state.audit.record(
        "promotion.applied",
        actor=admin,
        model=body.model,
        deployment=body.deployment,
        weight=body.weight,
        mode=plan["mode"],
        verdict=recommendation["verdict"],
        evidence_source=source,
        force=body.force,
        backup=backup.name,
        changes=plan["changes"],
    )
    plan.pop("new_text")
    return {
        **plan,
        "recommendation": recommendation,
        "evidence_source": source,
        "forced": body.force,
        "backup": backup.name,
        "reload": reload_summary,
    }


# -- promotion rollback (tier 39) -------------------------------------------
# Promotion's undo, with the same discipline: every apply (promote,
# rollback) leaves a timestamped backup; the rollback surface lists
# those backups with an honest explanation of what restoring each
# would do, and applying one runs the apply discipline in reverse —
# the live file is backed up FIRST (a rollback is itself one rollback
# away from undone), the reload refusal path restores, the act is
# audited. Never an automatic act: no evidence gate, no force flag —
# rollback IS the escape hatch.


def _resolve_config_backup(app: Any, name: str | None) -> tuple[Path, Path]:
    """The config file and the chosen backup file, validated: flat
    names only (the charset bars path separators, so the resolution
    can never leave the config file's directory), the
    ``config.yaml.bak-`` prefix, and existence. ``name=None`` picks
    the LATEST backup — the common "undo the last promotion" case."""
    config_path = Path(app.state.settings.config_path)
    if not config_path.exists():
        raise GatewayError(
            409,
            f"Config file not found at {config_path} — rollback restores "
            "the file the gateway loads, so both must exist.",
            "invalid_request_error",
            code="config_file_missing",
        )
    if name is None:
        backups = promotion_core.list_config_backups(config_path)
        if not backups:
            raise GatewayError(
                404,
                "No promotion backups exist yet — a backup is written by "
                "POST /admin/migration/promote (and by rollback itself).",
                "invalid_request_error",
                code="backup_not_found",
            )
        target = backups[0]
    else:
        if not name or set(name) - promotion_core.BACKUP_NAME_CHARS:
            raise GatewayError(
                400,
                "Invalid backup name — expected a flat "
                f"'{config_path.name}.bak-<stamp>' file name.",
                "invalid_request_error",
                code="invalid_backup_name",
            )
        if not name.startswith(f"{config_path.name}.bak-"):
            raise GatewayError(
                400,
                f"'{name}' is not a promotion backup of {config_path.name} "
                f"— expected a '{config_path.name}.bak-<stamp>' file name.",
                "invalid_request_error",
                code="invalid_backup_name",
            )
        target = config_path.with_name(name)
    if not target.is_file():
        raise GatewayError(
            404,
            f"Backup '{target.name}' does not exist.",
            "invalid_request_error",
            code="backup_not_found",
        )
    return config_path, target


def _backup_row(
    config_path: Path, target: Path, live_text: str, live_config: GatewayConfig
) -> dict:
    """One backup's listing row: identity (name, stamp, size), honesty
    (does it still parse), and the explanation of what restoring it
    would do (``changes`` from the parsed comparison)."""
    try:
        backup_config = GatewayConfig.load(target)
        parses = True
        changes = promotion_core.compare_configs(live_config, backup_config)
    except Exception:
        parses = False
        changes = None
    stamp = promotion_core.parse_backup_stamp(target.name)
    return {
        "name": target.name,
        "created_at": stamp.isoformat() if stamp is not None else None,
        "size_bytes": target.stat().st_size,
        "parses": parses,
        "is_current": promotion_core.read_config_text(target) == live_text,
        "changes": changes,
    }


@router.get("/admin/migration/backups")
async def migration_backup_list(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """The promotion backups carried next to config.yaml
    (``config.yaml.bak-<utc stamp>`` — written by promote, and by
    rollback itself), newest first. Pure read: the file is never
    written, nothing is audited.

    Each row explains the backup: ``created_at`` (the stamp in the
    name), ``size_bytes``, whether it still ``parses`` (an old backup
    can predate schema changes — honestly labeled, refused at apply
    time), ``is_current`` (byte-identical to the live file — a
    rollback to it is the no-op the rollback endpoint refuses), and
    the ``changes`` restoring it would apply (parsed configs
    compared: ``shadow``/``unshadow`` flags re-set, ``set_weight``/
    ``set_priority`` knob edits, ``add_deployment``/
    ``remove_deployment`` — the cross-model insert, undone). The
    line-level diff lives on the per-backup detail endpoint."""
    config_path = Path(raw_request.app.state.settings.config_path)
    if not config_path.exists():
        raise GatewayError(
            409,
            f"Config file not found at {config_path} — rollback reads the "
            "backups the gateway's own applies wrote next to it.",
            "invalid_request_error",
            code="config_file_missing",
        )
    live_text = promotion_core.read_config_text(config_path)
    live_config = raw_request.app.state.config
    rows = [
        _backup_row(config_path, target, live_text, live_config)
        for target in promotion_core.list_config_backups(config_path)
    ]
    return {"object": "list", "data": rows}


@router.get("/admin/migration/backups/{name}")
async def migration_backup_detail(
    name: str,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """One backup in full: the listing row plus the line-level
    ``diff`` of the live file → the backup's text — the human preview
    of what rolling back to it applies. Pure read, like the list."""
    config_path, target = _resolve_config_backup(raw_request.app, name)
    live_text = promotion_core.read_config_text(config_path)
    row = _backup_row(
        config_path, target, live_text, raw_request.app.state.config
    )
    backup_text = promotion_core.read_config_text(target)
    row["diff"] = (
        ""
        if row["is_current"]
        else promotion_core.unified_diff(
            live_text, backup_text, f"config.yaml (backup {target.name})"
        )
    )
    return row


@router.post("/admin/migration/rollback")
async def migration_rollback(
    raw_request: Request,
    body: RollbackRequest,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Restore config.yaml from a promotion backup — the deliberate
    undo of a promotion (or a ladder of them), applied with the same
    discipline promote applies, in reverse:

    the chosen backup (``backup`` names it; None = the latest —
    "undo the last promotion") must exist AND still parse (never
    start an edit that cannot land); the live file is backed up FIRST
    (the rollback's own undo point — a rollback is one rollback away
    from undone); the backup's text is written; the same hot-reload
    path POST /admin/reload takes applies (source="rollback"); on
    reload refusal the undo point is restored and the running config
    never moved. The act is audited as ``promotion.rolled_back`` with
    the structured ``changes`` the restore applied. A backup
    byte-identical to the live file is refused with 409
    ``rollback_noop`` — rolling back nothing is not an act worth
    auditing."""
    config_path, target = _resolve_config_backup(raw_request.app, body.backup)
    backup_text = promotion_core.read_config_text(target)
    try:
        backup_config = GatewayConfig.load(target)
    except Exception as exc:
        raise GatewayError(
            409,
            f"Backup '{target.name}' does not parse as a gateway config "
            f"({exc}) — it predates or diverges from the current schema. "
            "Restoring it could not land; edit it by hand if you truly "
            "need its contents.",
            "invalid_request_error",
            code="backup_invalid",
        ) from exc
    live_text = promotion_core.read_config_text(config_path)
    if backup_text == live_text:
        raise GatewayError(
            409,
            f"Nothing to roll back — '{target.name}' is byte-identical to "
            f"the live {config_path.name}. Pick another backup.",
            "invalid_request_error",
            code="rollback_noop",
        )
    changes = promotion_core.compare_configs(raw_request.app.state.config, backup_config)
    undo_point = promotion_core.write_with_backup(config_path, backup_text)

    from ..main import reload_gateway  # same import stance as promote

    try:
        reload_summary = await reload_gateway(raw_request.app, source="rollback")
    except Exception as exc:  # noqa: BLE001 — any load/validate failure
        promotion_core.restore_backup(undo_point, config_path)
        raise GatewayError(
            400,
            f"Rollback failed: the restored config did not reload (running "
            f"config unchanged, file restored from {undo_point.name}): {exc}",
            "invalid_request_error",
            code="config_invalid",
        ) from exc

    raw_request.app.state.audit.record(
        "promotion.rolled_back",
        actor=admin,
        backup=target.name,
        undo_point=undo_point.name,
        changes=changes,
    )
    return {
        "backup": target.name,
        "undo_point": undo_point.name,
        "changes": changes,
        "diff": promotion_core.unified_diff(
            live_text, backup_text, f"config.yaml (backup {target.name})"
        ),
        "reload": reload_summary,
    }


# -- post-promotion watch (tier 40) -----------------------------------------
# Once a candidate is promoted the mirrors stop, so the pair-shaped
# evidence ends exactly when watching matters. The watch re-verdicts
# what the logs still say: the serving rows each deployment actually
# carried (mirrors, trial replays and cache replays excluded), against
# its same-priority-tier peers over the same window — its own error
# rate and its mean latency as a percent of the peers' mean, judged by
# the same migration: thresholds. A RECOMMENDATION to keep serving or
# to roll back, never an automatic one.


@router.get("/admin/migration/watch")
async def migration_watch(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
    model: str | None = None,
    deployment: str | None = None,
    since: str | None = None,
) -> dict:
    """Post-promotion watch: go/no-go over the SERVING traffic — the
    migration story continued after the promotion flips the candidate
    live and the mirrored evidence ends.

    One row per serving (non-shadow) deployment, each re-verdicted on
    the rows attributed to it — the client-facing log rows written
    once a dispatch COMPLETED on the deployment (mirrors — shadow
    twins and prompt A/B — candidate-trial replays and cache replays
    excluded: evidence, experiments and cache hits are not serving
    traffic). The attribution is the honest part: a request that
    fails before any dispatch completes (the router exhausted its
    ladder — a failing sole deployment, or a candidate rescued by a
    tier peer) logs with no deployment, so a failing candidate shows
    up as traffic that ran away, never as fabricated attributed
    errors. Each row carries its ``requests`` / ``errors`` /
    ``error_rate`` / ``latency_mean_ms`` / ``cost_mean_usd`` over
    the watch window, the tier peers' aggregate it is compared
    against (``baseline``), the routing share split (``expected_share``
    = its weight's share of its priority tier, ``actual_share`` = its
    fraction of the tier's attributed requests in the window — a
    rescued candidate thins toward 0 while its expected share stays
    put, the loudest "traffic ran away from it" signal the watch can
    show), and the ``recommendation`` (go / hold / no_go /
    insufficient_evidence — same vocabulary, same thresholds, same
    per-check numbers as the pre-promotion verdicts; a ``no_go`` or
    ``hold`` on a freshly promoted candidate says where the Backups
    button is, never pushes it).

    The window: each row watches from when the deployment started
    serving the model (``since`` — its first serving row, i.e. the
    promotion moment measured on the traffic itself; no side state to
    drift), and the baseline aggregates the same-priority peers over
    exactly that window — the same traffic regime, the same clock.
    ``?since=`` (ISO-8601) overrides the window for every row instead
    (use it to re-verdict a slice); ``?model=`` / ``?deployment=``
    narrow; an unknown model or deployment is empty data, not an
    error. Thresholds come from the live ``migration:`` config and
    are echoed, so every verdict is auditable against its bars."""
    config = raw_request.app.state.config
    since_ts = _parse_range_ts(since, "since") if since else None
    sessionmaker = get_sessionmaker()

    data: list[dict] = []
    async with sessionmaker() as session:
        for cfg_model in sorted(config.models, key=lambda m: m.name):
            if model is not None and cfg_model.name != model:
                continue
            serving = [d for d in cfg_model.deployments if not d.shadow]
            if not serving:
                continue
            if deployment is not None and not any(
                d.name == deployment for d in serving
            ):
                continue  # nothing here can match — no query needed
            # Tier-weight totals per priority — the expected-share
            # denominators, a routing-config statement over the FULL
            # tier (not narrowed by ?deployment=): the peers exist
            # whether or not the operator filtered the response.
            tier_weight: dict[int, int] = {}
            for d in serving:
                tier_weight[d.priority] = tier_weight.get(d.priority, 0) + d.weight
            overview = await migration_core.serving_overview(
                session, cfg_model.name, since=since_ts
            )
            for dep in sorted(serving, key=lambda d: d.name):
                if deployment is not None and dep.name != deployment:
                    continue
                stats = overview.get(dep.name) or {
                    "requests": 0,
                    "errors": 0,
                    "error_rate": None,
                    "latency_mean_ms": None,
                    "cost_mean_usd": None,
                    "first_served_at": None,
                }
                peers = [
                    d.name
                    for d in serving
                    if d.name != dep.name and d.priority == dep.priority
                ]
                # The baseline — the peers' aggregate over the
                # candidate's own window (one light query; only a
                # deployment that carried traffic opens one).
                if stats["requests"] > 0:
                    window_start = since_ts or stats["first_served_at"]
                    baseline_raw = await migration_core.serving_baseline(
                        session, cfg_model.name, peers, window_start
                    )
                else:
                    baseline_raw = {
                        "requests": 0,
                        "errors": 0,
                        "error_rate": None,
                        "latency_mean_ms": None,
                    }
                baseline = {**baseline_raw, "deployments": peers}
                recommendation = migration_core.evaluate_serving(
                    {
                        **stats,
                        "baseline_requests": baseline_raw["requests"],
                        "baseline_errors": baseline_raw["errors"],
                        "baseline_latency_mean_ms": baseline_raw["latency_mean_ms"],
                    },
                    config.migration,
                )
                tier_requests = stats["requests"] + baseline_raw["requests"]
                window_since = since_ts or stats["first_served_at"]
                data.append(
                    {
                        "model": cfg_model.name,
                        "deployment": dep.name,
                        "priority": dep.priority,
                        "weight": dep.weight,
                        "since": (
                            window_since.isoformat()
                            if window_since is not None
                            else None
                        ),
                        "requests": stats["requests"],
                        "errors": stats["errors"],
                        "error_rate": stats["error_rate"],
                        "latency_mean_ms": stats["latency_mean_ms"],
                        "cost_mean_usd": stats["cost_mean_usd"],
                        "expected_share": round(
                            dep.weight / tier_weight[dep.priority], 4
                        ),
                        "actual_share": round(stats["requests"] / tier_requests, 4)
                        if tier_requests
                        else 0.0,
                        "baseline": baseline,
                        "recommendation": recommendation,
                    }
                )
    data.sort(key=lambda row: (row["model"], row["deployment"]))
    return {"data": data, "thresholds": config.migration.model_dump()}


# -- admin playground (tier 41) ---------------------------------------------
# The dashboard's own chat client: a request typed into the UI, run
# through the REAL pipeline — the same auth-context snapshot, the same
# guardrails, budgets, residency, rate limits, routing, cache and
# billing a live request from that key would get — so "what does this
# key actually experience?" has an answer that IS the thing, never a
# simulation of it. Platform-admin-only: the caller may impersonate any
# key (its budgets bind, its spend accrues), which is exactly why the
# attempt is audited as clearly as it is granted.


@router.post("/admin/playground/chat")
async def playground_chat(
    body: PlaygroundChatRequest,
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """One chat completion through the real pipeline, from the admin
    dashboard — the completion a client would receive (verbatim, the
    response body itself) beside the diagnostics of what happened
    while producing it.

    ``key`` names a virtual key (the human label from /admin/keys) to
    run the request as: resolved through the same chain the JWT auth
    step uses — name -> active/expiry checks -> tenant -> the
    ``auth_context_from_key`` snapshot — so the request gets the
    impersonated key's full policy reality: its model/tool allowlists,
    its budgets and residency, its guardrail scopes and rate limits,
    and its BILLING (spend, log rows and everything downstream accrue
    to the key, indistinguishable from its live traffic except that
    each row's ``auth_method`` says ``playground``). Omitted, the
    calling platform admin's own context runs the chat — the
    unrestricted operator view. In both cases the context is relabeled
    ``auth_method="playground"``: log rows and audit events say how the
    request was issued, whatever key carried it.

    The request runs the exact pipeline a ``/v1/chat/completions``
    call rides — same ``route="chat"`` (so guardrail route-scopes
    behave identically), same guardrails, same router, same response
    cache (a repeat request can hit it and say so in its
    diagnostics). Errors pass through verbatim: a guardrail block, an
    exhausted budget, a rate-limit refusal or an upstream failure
    surfaces as the standard error envelope the real client would
    see. Non-streaming only (``stream: true`` is an explicit 400 —
    one request, one answer, one diagnostics panel).

    The response wraps the client-facing completion in
    ``{"response": ..., "diagnostics": ...}`` — the diagnostics are
    the request-log row the pipeline just wrote (the same one
    /admin/logs shows and usage export bills): request id, key,
    model/deployment/provider, status, tokens, cost, latency,
    cache-hit and the guardrail verdicts (``guardrail_actions``) the
    run produced. Every playground chat is audited as
    ``playground.chat`` with the calling admin as the actor and the
    impersonated key, model, request id and outcome in the details —
    an admin impersonating tenant keys is a sensitive probe, and the
    trail is the price of the grant.
    """
    if body.stream:
        raise GatewayError(
            400,
            "The playground is non-streaming only — one request, one "
            "answer, one diagnostics panel (omit stream or set it false).",
            "invalid_request_error",
            code="stream_not_supported",
        )

    # The calling context: the impersonated key's snapshot (built by
    # the same chain the JWT step uses) or the platform admin's own,
    # relabeled so the row says where the request was issued.
    if body.key is None:
        effective_auth = replace(admin, auth_method="playground")
    else:
        vkey = await get_key_by_name(body.key)
        if vkey is None:
            raise GatewayError(
                404, f"Key '{body.key}' not found.", "invalid_request_error"
            )
        if not vkey.is_active:
            raise GatewayError(
                400,
                f"Key '{body.key}' is inactive — the playground runs as a "
                "key a live request could actually use.",
                "invalid_request_error",
                code="key_inactive",
            )
        if _is_expired(vkey.expires_at):
            raise GatewayError(
                400, f"Key '{body.key}' has expired.", "invalid_request_error"
            )
        tenant = await load_tenant_for_key(vkey)
        effective_auth = auth_context_from_key(
            vkey, tenant=tenant, auth_method="playground"
        )

    request_id = (
        getattr(raw_request.state, "request_id", None) or f"req_{uuid.uuid4().hex}"
    )
    chat_request = ChatCompletionRequest(
        model=body.model,
        messages=body.messages,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        stream=False,
    )
    pipeline = raw_request.app.state.pipeline
    try:
        response = await pipeline.handle_chat(
            chat_request, effective_auth, request_id=request_id, route="chat"
        )
    except GatewayError as exc:
        # The error itself passes through verbatim (the real client's
        # envelope, unchanged); the ATTEMPT is still audited — an
        # impersonated probe that failed is as much a fact as one that
        # succeeded.
        raw_request.app.state.audit.record(
            "playground.chat",
            actor=admin,
            key=body.key,
            model=body.model,
            request_id=request_id,
            outcome="error",
            error=exc.message,
        )
        raise

    # The diagnostics: the row the pipeline just wrote — one honest
    # source (the same one the logs show and the export bills). A
    # missing row (logging off, or an exotic path) degrades to the ids
    # we know rather than a fabricated panel.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        log = (
            await session.execute(
                select(RequestLog).where(RequestLog.request_id == request_id)
            )
        ).scalar_one_or_none()
    if log is None:
        diagnostics: dict[str, Any] = {
            "request_id": request_id,
            "key": effective_auth.key_name,
            "model": body.model,
            "status": "success",
            "guardrail_actions": [],
            "logged": False,
        }
    else:
        diagnostics = {
            "request_id": log.request_id,
            "key": log.key_name,
            "key_id": log.key_id,
            "auth_method": log.auth_method,
            "model": log.model,
            "deployment": log.deployment,
            "provider": log.provider,
            "route": log.route,
            "status": log.status,
            "error_message": log.error_message,
            "prompt_tokens": log.prompt_tokens,
            "completion_tokens": log.completion_tokens,
            "cached_tokens": log.cached_tokens,
            "cost_usd": log.cost_usd,
            "latency_ms": log.latency_ms,
            "ttft_ms": log.ttft_ms,
            "cache_hit": bool(log.cache_hit),
            "guardrail_actions": log.guardrail_actions or [],
            "logged": True,
        }

    raw_request.app.state.audit.record(
        "playground.chat",
        actor=admin,
        key=body.key,
        model=body.model,
        request_id=request_id,
        outcome=diagnostics.get("status", "success"),
        cost_usd=diagnostics.get("cost_usd"),
        cache_hit=diagnostics.get("cache_hit", False),
    )
    return {
        "response": json.loads(response.body),
        "diagnostics": diagnostics,
    }


# -- service level objectives (tier 42) --------------------------------------
# Named, deliberate promises about the client experience — "p95
# latency under 800ms", "error rate under 1%" — measured over the
# request logs and verdicted honestly. Pure config read at request
# time: no loop, no persistence, no notifications in this tier, so a
# tightened bar re-verdicts on the next read (hot-reload applies
# instantly). Platform-admin-only: SLO definitions are platform
# health configuration, like canaries and routing.


@router.get("/admin/slos")
async def slo_status(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """One row per configured SLO target (``slos:`` in config.yaml,
    hot-reloadable), measured and verdicted over its rolling window.

    Each row echoes the target's definition (name, model, optional
    deployment scope, metric, the ceiling, the window, the sufficiency
    minimum) beside the measurement — the window's requests/errors/
    error-rate, the latency percentiles and TTFT p95, all from one
    pass over the same rows — the target metric's ``value``, and the
    ``verdict``: ``met`` (the ceiling holds), ``breached`` (it does
    not — the human ``reason`` says by how much) or
    ``insufficient_evidence`` (fewer than ``min_requests`` requests in
    the window, or nothing streamed for a TTFT target — a quiet
    window is not a verdict). What counts is CLIENT EXPERIENCE, not
    deployment health: non-shadow, non-trial rows for the scope, cache
    replays included (a cached answer is a fast answer the client
    really received) and unattributed error rows included at model
    scope; a deployment-scoped target measures only its attributed
    rows, exactly like the post-promotion watch.

    The numbers always ship beside the verdict, so the call is
    auditable and re-bucketable, never taken on faith. Tier 43 adds
    the error-budget lens to every row: ``budget`` (the bar's
    bad-event allowance — the bar itself for ``error_rate``, the
    implied 1−p tail for the percentile metrics — beside the bad
    count, the burn rate = observed/allowed, remaining, and the
    on_track / at_risk / exhausted / insufficient_evidence state)
    and ``burn`` (each ``slos.burn`` rule's multiwindow pace
    verdict: the long window establishes the burn is real, the
    short one confirms it is still burning — a burst that already
    ended never fires; zero-tolerance bars have no rate to compute
    and say so). Tier 44 attaches each target's persisted ``alert``
    state (firing, condition, since — what on-call was already paged
    for, from the same settings row the evaluator wrote; ``null``
    when the target has never fired or has recovered). This surface is
    the measurement and the honest answer — the *delivery* is the
    background evaluator's job (webhook + ``slo.alert`` /
    ``slo.recovered`` events, fire-once per transition, configured
    under ``slos.alerts``)."""
    config = raw_request.app.state.config
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        data = await slos_core.status(session, config)
        state = await slo_alerts_core.alert_state(session)
    for row in data:
        row["alert"] = state.get(row["name"])
    return {"data": data, "config": config.slos.model_dump()}


# -- candidate trials (tier 36) -------------------------------------------
# The deliberate-replay leg: a captured window of one public model's
# traffic replayed against a named candidate deployment with real
# upstream calls, throttled + spend-capped, scored like shadow mirrors.
# Platform-admin-only for the same reason as shadow-diff: the pairs
# carry request bodies, which cross tenants.


def _trial_dict(row: Trial) -> dict:
    return {
        "id": row.id,
        "model": row.model,
        "deployment": row.deployment,
        "provider": row.provider,
        "params": row.params or {},
        "status": row.status,
        "stop_reason": row.stop_reason,
        "error_message": row.error_message,
        "selected": row.selected,
        "completed": row.completed,
        "skipped": row.skipped,
        "cost_usd": row.cost_usd,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


@router.post("/admin/trials")
async def create_trial(
    raw_request: Request,
    body: TrialCreateRequest,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Start a candidate trial: replay a captured traffic window of
    ``model`` (its successful, body-storing chat rows inside
    ``since``/``until``, newest first, up to ``limit``) against the
    ``deployment`` candidate — real upstream calls in waves of
    ``concurrency``, cumulative candidate spend capped at
    ``max_cost_usd`` (all three clamped to the ``trials:`` ceilings).
    Each replayed entry writes a scored shadow log row (the tier-35
    vocabulary); the run executes in the background and its row
    reports progress. The candidate may be any configured deployment —
    it does not need to belong to the replayed model."""
    config = raw_request.app.state.config
    if not config.trials.enabled:
        raise GatewayError(
            404,
            "Candidate trials are disabled on this gateway "
            "(config: trials.enabled: false).",
            "invalid_request_error",
            code="trials_disabled",
        )
    target = trials_core.validate_run_request(config, body.model, body.deployment)
    params = trials_core.clamp_params(
        config.trials,
        limit=body.limit,
        concurrency=body.concurrency,
        max_cost_usd=body.max_cost_usd,
    )
    if body.since:
        params["since"] = _parse_range_ts(body.since, "since").isoformat()
    if body.until:
        params["until"] = _parse_range_ts(body.until, "until").isoformat()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = Trial(
            model=body.model,
            deployment=target.name,
            provider=target.provider,
            params=params,
        )
        session.add(row)
        await session.commit()
    # Tracked background work: shutdown drains the run like mirrors.
    pipeline = raw_request.app.state.pipeline
    pipeline._spawn(trials_core.run_trial(pipeline, row.id))
    raw_request.app.state.audit.record(
        "trial.created",
        actor=admin,
        trial_id=row.id,
        model=row.model,
        deployment=row.deployment,
    )
    return _trial_dict(row)


@router.get("/admin/trials")
async def list_trials(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    """Trial runs, newest first — status, progress counters, cumulative
    candidate spend and the stop reason (done / no_entries / cost_cap /
    cancelled / orphaned_by_restart / error). Each row carries its
    verdict summary (one grouped query for the page) so the table can
    show the verdict breakdown without per-run detail fetches, plus
    the run's migration recommendation (tier 37: the summary IS an
    evidence group — core/migration.evaluate verdicts it against the
    live ``migration:`` thresholds)."""
    config = raw_request.app.state.config
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        total = (
            await session.execute(select(func.count()).select_from(Trial))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(Trial)
                    .order_by(Trial.created_at.desc(), Trial.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        summaries = await trials_core.run_summaries(session, [row.id for row in rows])
        data = []
        for row in rows:
            out = _trial_dict(row)
            # run_summaries seeds an entry for every requested id — a
            # run with no rows yet evaluates (honestly) as thin evidence.
            out["summary"] = summaries[row.id]
            out["recommendation"] = migration_core.evaluate(
                out["summary"], config.migration
            )
            data.append(out)
        return {
            "data": data,
            "total": total,
            "limit": limit,
            "offset": offset,
        }


@router.get("/admin/trials/{trial_id}")
async def get_trial(
    raw_request: Request,
    trial_id: str,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """One trial run: the row plus its verdict summary (exact / near /
    divergent / shadow_error / not_comparable counts and the mean
    similarity over comparable pairs), computed from the run's log
    rows — and the run's migration recommendation (tier 37): the
    go/no-go call against the live ``migration:`` thresholds, with the
    per-check measurements."""
    config = raw_request.app.state.config
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(Trial, trial_id)
        if row is None:
            raise GatewayError(
                404, f"No trial '{trial_id}'.", "invalid_request_error"
            )
        out = _trial_dict(row)
        out["summary"] = await trials_core.run_summary(session, trial_id)
        out["recommendation"] = migration_core.evaluate(
            out["summary"], config.migration
        )
        return out


@router.get("/admin/trials/{trial_id}/pairs")
async def trial_pair_list(
    trial_id: str,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
    verdict: str | None = Query(None, pattern=_VERDICT_PATTERN),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    """The trial drilldown — the same pair shape as /admin/shadow-diff/
    pairs (both sides' status/latency/tokens/cost + text excerpts),
    scoped to the run's rows. ``?verdict=divergent`` is the migration
    review queue for the trial."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(Trial, trial_id)
        if row is None:
            raise GatewayError(
                404, f"No trial '{trial_id}'.", "invalid_request_error"
            )
        return await shadow_pairs(
            session,
            model=row.model,
            deployment=row.deployment,
            verdict=verdict,
            limit=limit,
            offset=offset,
            trial_id=row.id,
        )


@router.post("/admin/trials/{trial_id}/cancel")
async def cancel_trial(
    raw_request: Request,
    trial_id: str,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Cancel a pending/running trial. Guarded on the current status:
    a run that already finished answers 409; a live one stops within
    one wave (the runner re-checks this row between waves — DB status
    is the only cross-replica truth)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(Trial, trial_id)
        if row is None:
            raise GatewayError(
                404, f"No trial '{trial_id}'.", "invalid_request_error"
            )
        result = await session.execute(
            update(Trial)
            .where(Trial.id == trial_id, Trial.status.in_(["pending", "running"]))
            .values(
                status="cancelled",
                stop_reason=trials_core.STOP_CANCELLED,
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        if (result.rowcount or 0) == 0:
            raise GatewayError(
                409,
                f"Trial '{trial_id}' already finished ({row.status}).",
                "invalid_request_error",
            )
        raw_request.app.state.audit.record(
            "trial.cancelled", actor=admin, trial_id=trial_id
        )
        # expire_on_commit is off, so re-read the row the guarded
        # UPDATE just rewrote — the response must show the new state.
        await session.refresh(row)
        return _trial_dict(row)


@router.get("/admin/mcp")
async def mcp_status(
    raw_request: Request, admin: Annotated[AuthContext, Depends(require_platform_admin)]
) -> dict:
    """The MCP proxy at a glance (the same introspection role as
    /admin/routing): the config surface (enabled, TTL) and each
    configured server — url, timeout, the NAMES of its auth headers
    (never the values — they are secrets, possibly env-resolved) and
    how many gateway sessions are currently open against it."""
    config = raw_request.app.state.config.mcp
    registry = raw_request.app.state.mcp_registry
    counts = registry.live_counts()
    return {
        "object": "mcp_status",
        "enabled": config.enabled,
        "session_ttl_seconds": config.session_ttl_seconds,
        "servers": [
            {
                "name": server.name,
                "url": server.url,
                "timeout_seconds": server.timeout_seconds,
                "header_names": sorted({**server.headers, **server.headers_env}),
                "open_sessions": counts.get(server.name, 0),
            }
            for server in config.servers
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


@router.get("/admin/concurrency")
async def concurrency_view(
    raw_request: Request,
    admin: Annotated[AuthContext, Depends(require_platform_admin)],
) -> dict:
    """Concurrency admission control: keys/tenants currently holding
    in-flight slots or queueing for one, plus the live config echo and
    the per-key gauges' ground truth. In-process state — a
    multi-replica fleet reports its own share here. Read-only: slots
    free themselves as responses complete; the caps live on the
    key/tenant rows."""
    pipeline = raw_request.app.state.pipeline
    limiter = pipeline.concurrency
    cfg = pipeline.config.concurrency
    snapshot = limiter.snapshot() if limiter is not None else {"keys": [], "tenants": [], "queued_total": 0}
    # Join display names — the limiter tracks ids only.
    names: dict[str, str] = {}
    tenant_names: dict[str, str] = {}
    ids = [entry["key_id"] for entry in snapshot["keys"]]
    tids = [entry["tenant_id"] for entry in snapshot["tenants"]]
    if ids or tids:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            if ids:
                rows = await session.execute(
                    select(VirtualKey).where(VirtualKey.id.in_(ids))
                )
                names = {vk.id: vk.name for vk in rows.scalars()}
            if tids:
                rows = await session.execute(
                    select(Tenant).where(Tenant.id.in_(tids))
                )
                tenant_names = {t.id: t.name for t in rows.scalars()}
    return {
        "enabled": cfg.enabled,
        "wait_seconds": cfg.wait_seconds,
        "keys": [
            {**entry, "key_name": names.get(entry["key_id"])}
            for entry in snapshot["keys"]
        ],
        "tenants": [
            {**entry, "tenant_name": tenant_names.get(entry["tenant_id"])}
            for entry in snapshot["tenants"]
        ],
        "queued_total": snapshot["queued_total"],
    }


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
