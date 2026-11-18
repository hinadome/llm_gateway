"""PII-scan sweep (tier 53): the scan gets teeth — and legs.

Until now the PII scan was a mirror an admin had to look into: an
on-demand GET, findings evaporating into a metric and an event at
scan time, and the flagged bodies staying fully readable forever —
through the full-body read, through the replay export, until general
retention deleted the row wholesale. Exposure you have to remember
to check for is exposure you find at breach time.

The sweep closes that with the tier-52 loop discipline (always
started, config re-read each pass; tests drive ``evaluate_once``
directly, pinning the clock, because the lifespan loop never runs
under pytest):

* **Findings land once per row.** Each pass walks rows not yet
  scanned (``pii_scanned_at IS NULL`` — a native datetime column, so
  the filter is SQL-safe; the JSON findings column's NULL does *not*
  compare safely on SQLite), oldest first, bounded by
  ``sweep_max_rows``. Bodiless rows are marked scanned-clean —
  "nothing visible" recorded once, so the walk converges instead of
  re-visiting them forever. The persisted write is guarded on
  ``pii_scanned_at IS NULL``: the on-demand endpoints race this pass,
  and the guard means a row scanned by two paths still fires
  ``pii.found`` exactly once. State persists before delivery (the
  budget rule: duplicate silence over duplicate noise).
* **New findings announce themselves.** A newly flagged row fires
  the same ``pii.found`` observer event and ``gateway_pii_scan_
  findings_total{kind}`` increments as the on-demand scan — a
  subscriber's alerting works identically whether an admin asked or
  the sweep found it. Quiet traffic is covered too: no spend, no
  click, still scanned.
* **The purge leg.** With ``pii_scan.purge_days`` set, flagged rows
  whose findings landed longer ago than that get their stored bodies
  *deleted* — ``request_body``/``response_body`` nulled, metadata and
  findings kept for the audit trail, marked on ``pii_purged_at``.
  The walk selects candidates with SQL-safe columns only (flagged
  Boolean + purged-at datetime + scanned-at cutoff) and each purged
  row leaves the candidate set, so the leg cannot starve behind a
  backlog. Counted on ``gateway_pii_scan_purged_rows_total``;
  ``purge_days: null`` (the default) keeps bodies until general log
  retention — the quarantine masking still applies.

Quarantine itself (masking flagged rows on the full read, withholding
them from the replay export) lives in ``api/admin.py`` — the sweep
only records facts; the read paths act on them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from ..config import PIIScanConfig
from ..db import get_sessionmaker
from ..models import RequestLog
from ..observability import metrics
from .pii_scan import PIIScanner

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .events import EventBus

logger = logging.getLogger("gateway.pii_sweep")


class PIISweepManager:
    """Records PII/secret findings on log rows (once) and purges
    flagged bodies past their retention. Purely administrative: no
    request path ever waits on it."""

    def __init__(self, events: EventBus | None = None) -> None:
        # Lifecycle-event bus (optional — tests may construct without
        # one): newly flagged rows surface as pii.found events, same
        # payload the on-demand scan emits.
        self.events = events

    async def evaluate_once(
        self,
        scanner: PIIScanner,
        config: PIIScanConfig,
        now: datetime | None = None,
    ) -> int:
        """One pass: scan up to ``sweep_max_rows`` unscanned rows,
        then (if ``purge_days`` is set) purge flagged bodies past
        their window. Returns the number of NEWLY FLAGGED rows (the
        purged count is visible on the metric). ``scanner`` and
        ``config`` are passed by the caller — the lifespan loop hands
        it the current ``app.state.pii_scanner`` and config each pass,
        so a hot reload of detectors or knobs applies from the next
        pass; tests pin ``now``. Disabled (``enabled: false``) is a
        no-op."""
        if not config.enabled:
            return 0
        now = now or datetime.now(timezone.utc)
        newly_flagged = await self._scan_pass(scanner, config, now)
        if config.purge_days is not None:
            await self._purge_pass(config, now)
        return len(newly_flagged)

    # -- the scan leg -------------------------------------------------------

    async def _scan_pass(
        self, scanner: PIIScanner, config: PIIScanConfig, now: datetime
    ) -> list[tuple[int, str | None, str | None, str | None, dict]]:
        """Scan + persist the unscanned rows; return the payload
        tuples for the newly flagged ones (captured before the commit
        — the ORM rows expire with the session)."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(RequestLog)
                        .where(RequestLog.pii_scanned_at.is_(None))
                        .order_by(RequestLog.id.asc())
                        .limit(config.sweep_max_rows)
                    )
                )
                .scalars()
                .all()
            )
            flagged: list[tuple[int, str | None, str | None, str | None, dict]] = []
            for row in rows:
                result = scanner.scan_log(row)
                # Guarded write — only the FIRST scan of a row lands
                # (the on-demand endpoints race this pass): identical
                # findings, but pii.found fires exactly once.
                res = await session.execute(
                    update(RequestLog)
                    .where(RequestLog.id == row.id, RequestLog.pii_scanned_at.is_(None))
                    .values(
                        pii_findings=result["findings"],
                        pii_scanned_at=now,
                        pii_flagged=not result["clean"],
                    )
                )
                if res.rowcount and not result["clean"]:
                    flagged.append(
                        (row.id, row.request_id, row.key_name, row.model,
                         result["findings"])
                    )
            # Persist before delivery (the budget rule).
            await session.commit()
        for log_id, request_id, key_name, model, findings in flagged:
            request = findings["request"]
            response = findings["response"]
            for kind in set(request) | set(response):
                metrics.PII_FINDINGS.labels(kind=kind).inc()
            if self.events is not None:
                self.events.emit_background(
                    "pii.found",
                    None,
                    log_id=log_id,
                    request_id=request_id,
                    key_name=key_name,
                    model=model,
                    kinds={
                        kind: request.get(kind, 0) + response.get(kind, 0)
                        for kind in sorted(set(request) | set(response))
                    },
                )
        return flagged

    # -- the purge leg --------------------------------------------------------

    async def _purge_pass(self, config: PIIScanConfig, now: datetime) -> int:
        """Delete the stored bodies of flagged rows past ``purge_days``
        (SQL-safe candidate selection: the Boolean flag and the
        datetimes; purged rows leave the candidate set, so the leg
        cannot starve behind a backlog). Metadata and findings stay —
        the audit trail outlives the exposure."""
        cutoff = now - timedelta(days=config.purge_days or 0.0)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(RequestLog)
                        .where(
                            RequestLog.pii_flagged.is_(True),
                            RequestLog.pii_purged_at.is_(None),
                            RequestLog.pii_scanned_at <= cutoff,
                        )
                        .order_by(RequestLog.id.asc())
                        .limit(config.sweep_max_rows)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return 0
            removed = 0
            for row in rows:
                if row.request_body is not None or row.response_body is not None:
                    removed += 1
                await session.execute(
                    update(RequestLog)
                    .where(RequestLog.id == row.id)
                    .values(request_body=None, response_body=None, pii_purged_at=now)
                )
            await session.commit()
        metrics.PII_PURGED.inc(removed)
        logger.info(
            "pii purge: removed stored bodies from %d flagged row(s)", removed
        )
        return removed
