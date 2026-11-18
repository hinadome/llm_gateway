"""Capture-replay harness: diff real traffic against the current config.

The request log already *captures* traffic (bodies, guardrail verdicts,
routing, per-caller identity — ``logging.store_bodies``). This module is
the other two legs:

* **replay** — re-execute a captured request through the request-policy
  phases *only* (model resolution, prompt policy, permissions,
  guardrails, routing selection; see ``GatewayPipeline.replay_request``),
  as the *current* state of the key that made it. No upstream call, no
  counters, no cache, no spend, no log rows, no lifecycle events — the
  run is free, deterministic and safe on a live gateway.
* **diff** — compare the predicted outcome against what the capture
  recorded. The differences are exactly what the current config would
  change about that traffic — the safety net for a hot reload: capture,
  apply the candidate config, replay, *read the diff*, then keep or roll
  back before clients notice.

Comparability is decided per line, honestly: rows recorded as ``error``
cannot be re-run (upstream failures and counter rejections are state,
not policy), truncated or non-chat bodies cannot be parsed, and entries
without a recorded baseline produce predictions but no diff.

The harness replays as *keys*, impersonating their current policy (a
tenant admin may only replay their tenant's keys — the same boundary
as managing them; platform admins replay anything). Entries with no
key identity — bare requests, admin-key captures — replay as the
calling admin's context.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..db import get_sessionmaker
from ..errors import AuthError, GatewayError
from ..models import VirtualKey
from ..observability.metrics import REPLAY
from ..schemas import ChatCompletionRequest
from .auth import (
    _is_expired,
    auth_context_from_key,
    get_key_by_name,
    load_tenant_for_key,
)

# Recorded statuses the replay can diff against. "error" rows are out:
# their cause may be upstream flake or a counter rejection — state the
# dry run deliberately does not re-evaluate — so a diff would be noise.
_COMPARABLE_STATUSES = ("success", "blocked")


def _request_verdicts(actions: list | None) -> list[tuple]:
    """The comparable part of a verdict list: request-phase
    (guardrail, action) pairs, sorted.

    Response/stream-phase verdicts are excluded — the replay stops at
    the request phase, so those cannot be predicted, and live rows mix
    phases in one list.
    """
    return sorted(
        (a.get("guardrail"), a.get("action"))
        for a in (actions or [])
        if isinstance(a, dict) and a.get("phase") == "request"
    )


async def _resolve_caller(
    entry: dict[str, Any], admin: Any
) -> tuple[Any, dict | None, str | None]:
    """The captured caller's *current* key policy, or a prediction that
    live traffic would be rejected at auth, or a skip reason when the
    entry cannot be replayed as a key at all.

    Resolution order: ``key_id`` (exact; the export carries it), then
    the unique ``key_name`` (older captures). A deleted, disabled or
    expired key is not a harness error — it is a finding: requests
    that used to serve would now 401.

    Entries with no virtual-key identity — a bare request, or a
    capture of the platform admin key — replay as the *calling*
    admin's context: there is no key policy to be faithful to, and
    the caller explicitly handed the request to the harness (the
    admin context bypasses permission checks, so such lines can only
    diff on model/guardrail/routing changes).
    """
    key_id = entry.get("key_id")
    key_name = entry.get("key_name")
    if not key_id and (not key_name or key_name == "admin"):
        return admin, None, None
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        vkey: VirtualKey | None = (
            await session.get(VirtualKey, key_id) if key_id else None
        )
        if vkey is None and not key_id:
            vkey = await get_key_by_name(key_name)
    identity = f"key '{key_name or key_id}'"
    if vkey is None:
        return None, _auth_rejection(f"{identity} no longer exists."), None
    if not vkey.is_active:
        return None, _auth_rejection(f"{identity} has been revoked (is_active=false)."), None
    if _is_expired(vkey.expires_at):
        return None, _auth_rejection(f"{identity} has expired."), None
    if admin.tenant_id is not None and vkey.tenant_id != admin.tenant_id:
        # Tenant admins replay their own tenant's traffic — the same
        # boundary as every other key operation they may perform.
        return None, None, "entry belongs to another tenant"
    # The tenant row loads with live fail-closed semantics: a dangling
    # or disabled tenant rejects here exactly as live auth would.
    try:
        tenant = await load_tenant_for_key(vkey)
    except AuthError as exc:
        return None, _auth_rejection(exc.message), None
    auth = auth_context_from_key(vkey, tenant, auth_method="replay")
    return auth, None, None


def _auth_rejection(message: str) -> dict:
    return {
        "model": None,
        "prompt_policy_applied": False,
        "guardrail_actions": [],
        "status": "error",
        "error": {"code": "replay_caller_rejected", "message": message},
        "deployments": None,
    }


def _diff(recorded: dict[str, Any], predicted: dict[str, Any]) -> list[str]:
    """Field names where the prediction and the capture disagree."""
    changes: list[str] = []
    if predicted.get("status") != recorded.get("status"):
        changes.append("status")
    # model is None only on auth-rejection predictions (the request
    # never reached resolution) — that is a status finding, not a
    # model finding, so it is not double-flagged here.
    if predicted.get("model") is not None and predicted.get("model") != recorded.get(
        "model"
    ):
        changes.append("model")
    if _request_verdicts(predicted.get("guardrail_actions")) != _request_verdicts(
        recorded.get("guardrail_actions")
    ):
        changes.append("guardrail_actions")
    recorded_deployment = recorded.get("deployment")
    ladder = predicted.get("deployments")
    if recorded_deployment and ladder and recorded_deployment not in ladder:
        changes.append("deployment")
    return changes


async def _replay_one(
    pipeline: Any, entry: dict[str, Any], admin: Any
) -> dict[str, Any]:
    """Replay one capture line; returns the per-line result record."""
    body = entry.get("request_body")
    if not isinstance(body, dict):
        return {
            "result": "error",
            "changes": None,
            "skip_reason": None,
            "detail": "entry has no request_body — not a capture line",
            "predicted": None,
            "recorded": None,
        }
    try:
        request = ChatCompletionRequest.model_validate(body)
    except ValidationError as exc:
        # An embeddings-shaped row, or a body truncated by
        # logging.max_body_chars — honest: the harness cannot re-run it.
        return {
            "result": "not_comparable",
            "changes": None,
            "skip_reason": f"unparseable request body ({exc.error_count()} errors)",
            "predicted": None,
            "recorded": None,
        }

    auth, rejection, skip = await _resolve_caller(entry, admin)
    if skip is not None:
        return {
            "result": "not_comparable",
            "changes": None,
            "skip_reason": skip,
            "predicted": None,
            "recorded": None,
        }
    if rejection is not None:
        predicted = rejection
    else:
        predicted = await pipeline.replay_request(
            request, auth, route=entry.get("route", "chat")
        )

    # A recorded baseline enables the diff; rows recorded as "error"
    # carry state the dry run cannot re-run (upstream/counter outcomes).
    recorded_status = entry.get("status")
    has_baseline = recorded_status in _COMPARABLE_STATUSES and isinstance(
        entry.get("request_body"), dict
    )
    recorded = (
        {
            "model": entry.get("model"),
            "status": recorded_status,
            "deployment": entry.get("deployment"),
            "guardrail_actions": entry.get("guardrail_actions"),
        }
        if has_baseline
        else None
    )
    if not has_baseline:
        return {
            "result": "not_comparable",
            "changes": None,
            "skip_reason": (
                "recorded status 'error' — upstream/counter outcomes are "
                "state, not policy; predicted outcome only"
                if recorded_status == "error"
                else "no comparable recorded baseline"
            ),
            "predicted": predicted,
            "recorded": None,
        }
    changes = _diff(recorded, predicted)
    return {
        "result": "changed" if changes else "unchanged",
        "changes": changes or None,
        "skip_reason": None,
        "predicted": predicted,
        "recorded": recorded,
    }


async def replay_entries(pipeline: Any, entries: list, admin: Any) -> dict[str, Any]:
    """Replay a capture through the current config and diff it.

    Returns the harness report: per-line results plus the summary the
    metrics also carry (``gateway_replay_total{result}``).
    """
    results = []
    counts = {"changed": 0, "unchanged": 0, "not_comparable": 0, "error": 0}
    for entry in entries:
        if not isinstance(entry, dict):
            results.append(
                {
                    "result": "error",
                    "changes": None,
                    "skip_reason": None,
                    "detail": "entry is not an object",
                    "predicted": None,
                    "recorded": None,
                }
            )
            counts["error"] += 1
            REPLAY.labels("error").inc()
            continue
        try:
            line = await _replay_one(pipeline, entry, admin)
        except GatewayError as exc:
            line = {
                "result": "error",
                "changes": None,
                "skip_reason": None,
                "detail": f"harness error: {exc.message}",
                "predicted": None,
                "recorded": None,
            }
        results.append(line)
        counts[line["result"]] += 1
        REPLAY.labels(line["result"]).inc()
    return {
        "object": "replay",
        "replayed": len(results),
        "changed": counts["changed"],
        "unchanged": counts["unchanged"],
        "not_comparable": counts["not_comparable"],
        "errors": counts["error"],
        "results": results,
    }
