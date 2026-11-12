"""Audit-log webhooks: security-relevant admin actions, delivered to an
external audit trail.

An audit record is emitted whenever an administrator changes the security
posture of the gateway — creating, updating or revoking a virtual key, or
force-resetting a deployment's circuit breaker. Each record identifies
*who* did it (the admin key name and the auth chain step that
authenticated them — ``admin_key``, ``jwt``, a custom step) and *what*
changed, and is POSTed as JSON to the configured webhook
(``audit.webhook_url`` / ``AUDIT_WEBHOOK_URL``).

Delivery mirrors budget alerts: best-effort with one retry, never
blocking the admin operation itself (records are delivered by a background
task), failures logged and counted on
``gateway_audit_events_total{event,outcome}``. With no webhook configured,
events are only counted as ``outcome="skipped"``. The raw key material of
a newly created virtual key is never part of any record — it is shown to
the administrator exactly once and then nowhere.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from ..config import AuditConfig
from ..observability import metrics

if TYPE_CHECKING:  # pragma: no cover
    from .context import AuthContext

logger = logging.getLogger("gateway.audit")

# Human summaries per event, for webhook targets that render messages
# (Slack-style). Free-form details ride along in the payload.
_MESSAGES = {
    "key.created": "Virtual key '{details[key_name]}' was created by {actor}.",
    "key.updated": "Virtual key '{details[key_name]}' was updated by {actor} (fields: {details[fields]}).",
    "key.revoked": "Virtual key '{details[key_name]}' was revoked by {actor}.",
    "breaker.reset": "Circuit breaker '{details[deployment]}' was reset by {actor}.",
}


class AuditLogger:
    """Emits audit records for admin actions and delivers them best-effort."""

    def __init__(
        self,
        config: AuditConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._owns_client = client is None
        # Dedicated client (not the instrumented app one): audit POSTs
        # should not appear as spans in request traces.
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- recording ----------------------------------------------------------

    def record(
        self,
        event: str,
        actor: AuthContext | None = None,
        **details: Any,
    ) -> None:
        """Queue an audit record for background delivery.

        Never blocks (and never raises into) the admin operation it
        describes. ``actor`` is the authenticated admin context; its
        key name and auth method identify who performed the action.
        """
        payload = self._payload(event, actor, details)
        # Fire-and-forget, like shadow mirroring and spend accounting:
        # the admin response must not wait on the webhook (and must not
        # fail if it is down). All failures are contained in _deliver.
        task = asyncio.create_task(self._deliver(payload))
        task.add_done_callback(_log_task_failure)

    def _payload(
        self,
        event: str,
        actor: AuthContext | None,
        details: dict[str, Any],
    ) -> dict:
        actor_name = (actor.key_name if actor else None) or "unknown"
        auth_method = (actor.auth_method if actor else None) or "unknown"
        template = _MESSAGES.get(event)
        message = (
            template.format(details=details, actor=actor_name)
            if template
            else f"{event} by {actor_name}."
        )
        return {
            "type": "audit",
            "event": event,
            "actor": {"name": actor_name, "auth_method": auth_method},
            "details": details,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # -- delivery -----------------------------------------------------------

    async def _deliver(self, payload: dict) -> bool:
        """POST the payload; retry once; metric + log on failure."""
        event = payload.get("event", "unknown")
        url = self.config.resolve_url()
        if not self.config.enabled or not url:
            # No webhook configured: still count the event that *would*
            # have been emitted, so ops can spot silent auditing in
            # /metrics.
            metrics.AUDIT.labels(event=event, outcome="skipped").inc()
            return False
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.post(
                    url,
                    json=payload,
                    timeout=httpx.Timeout(self.config.timeout_seconds),
                )
                if response.status_code < 400:
                    metrics.AUDIT.labels(event=event, outcome="sent").inc()
                    return True
                logger.warning(
                    "audit webhook returned %s for %s (attempt %s)",
                    response.status_code,
                    event,
                    attempt,
                )
            except Exception as exc:  # noqa: BLE001 - best-effort delivery
                logger.warning(
                    "audit webhook failed for %s (attempt %s): %s",
                    event,
                    attempt,
                    exc,
                )
            if attempt >= max(1, self.config.max_attempts):
                metrics.AUDIT.labels(event=event, outcome="failed").inc()
                return False
            await asyncio.sleep(0.5)


def _log_task_failure(task: asyncio.Task) -> None:
    # _deliver contains its own error handling; this catches the
    # unexpected (e.g. payload serialization) without an unretrieved-
    # exception warning.
    if not task.cancelled() and task.exception() is not None:
        logger.error("audit delivery task failed: %s", task.exception())
