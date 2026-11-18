"""Feedback capture: client-reported quality signals for logged requests.

``POST /v1/feedback`` — rate a request the calling key made (1..5, an
optional comment), by the ``X-Request-Id`` the gateway returned. The
missing half of the quality story: shadow mirroring and prompt A/B
compare *outputs*, feedback says which one was actually good.

Ownership follows the files/batches rule: the request-log row must
belong to the calling key (the platform admin may rate anything); a
foreign or unknown ``request_id`` 404s rather than leaking existence.
Ratings are counted on ``gateway_feedback_total{rating}`` and surface
as ``feedback.recorded`` lifecycle events; ``/admin/feedback`` and
``/admin/feedback/stats`` (see ``api/admin.py``) turn them into
per-model / per-key quality views.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from ..core.context import AuthContext
from ..db import get_sessionmaker
from ..errors import GatewayError
from ..models import Feedback, RequestLog
from ..observability.metrics import FEEDBACK
from ..schemas import FeedbackRequest
from ..utils import truncate
from .deps import require_api_key

router = APIRouter()


@router.post("/v1/feedback")
async def submit_feedback(
    request: Request,
    body: FeedbackRequest,
    auth: Annotated[AuthContext, Depends(require_api_key)],
) -> dict:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        log = (
            (
                await session.execute(
                    select(RequestLog)
                    .where(RequestLog.request_id == body.request_id)
                    .order_by(RequestLog.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        # Ownership: the caller must be the key that made the request
        # (platform admins exempt). Unknown and foreign ids get the
        # same 404 — never confirm another key's request exists.
        if log is None or (not auth.is_admin and log.key_name != auth.key_name):
            raise GatewayError(
                404,
                f"No request '{body.request_id}' for this key.",
                "invalid_request_error",
                code="feedback_target_not_found",
            )
        feedback = Feedback(
            request_id=body.request_id,
            key_id=auth.key_id,
            key_name=log.key_name,
            tenant_id=log.tenant_id,
            # Model as logged — stats stay stable if the model is later
            # deleted or the alias remapped.
            model=log.model,
            rating=body.rating,
            comment=truncate(body.comment, 2000) if body.comment else None,
        )
        session.add(feedback)
        await session.commit()
    FEEDBACK.labels(rating=str(body.rating)).inc()
    # Observer-only lifecycle event (fire-and-forget, like the batch
    # API's created/cancelled) — quality dashboards can subscribe.
    events = getattr(request.app.state, "events", None)
    if events is not None:
        events.emit_background(
            "feedback.recorded",
            None,
            request_id=body.request_id,
            model=feedback.model,
            rating=body.rating,
            key_name=feedback.key_name,
        )
    return {
        "id": feedback.id,
        "object": "feedback",
        "request_id": feedback.request_id,
        "rating": feedback.rating,
        "created_at": feedback.created_at.isoformat(),
    }
