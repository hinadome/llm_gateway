"""OpenAI-compatible Files + Batch API.

* ``POST /v1/files``                   (multipart: file + purpose)
* ``GET  /v1/files``                   (owned files, cursor-paginated)
* ``GET  /v1/files/{id}``              (metadata)
* ``GET  /v1/files/{id}/content``      (the raw bytes)
* ``POST /v1/batches``                 (create — input validated up front)
* ``GET  /v1/batches``                 (owned batches, cursor-paginated)
* ``GET  /v1/batches/{id}``            (retrieve)
* ``POST /v1/batches/{id}/cancel``     (cancel)

Everything requires a virtual key (the platform admin key has no key
row to own files or carry spend accounting). Batches execute in the
background — see ``core/batch_worker.py``; each line runs through the
full chat pipeline exactly like an interactive request.

There is no "validating" status: the input file's JSONL is validated
synchronously at ``POST /v1/batches``, so a created batch is always
runnable.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import and_, or_, select, update

from ..core.context import AuthContext
from ..db import get_sessionmaker
from ..errors import GatewayError
from ..models import Batch, StoredFile
from ..schemas import ChatCompletionRequest
from .deps import require_api_key

router = APIRouter()

# The only endpoint/window combination this gateway executes. Kept as
# literals (not config) — widening either means teaching the worker to
# drive other pipelines, which is a design change, not a setting.
_SUPPORTED_ENDPOINT = "/v1/chat/completions"
_SUPPORTED_WINDOW = "24h"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _unix(dt: datetime | None) -> int | None:
    """OpenAI-style integer epoch seconds (None passes through).

    SQLite hands naive datetimes back on read — they were written
    UTC, so treat them as such rather than local time.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _file_payload(f: StoredFile) -> dict:
    return {
        "id": f.id,
        "object": "file",
        "bytes": f.bytes,
        "created_at": _unix(f.created_at),
        "filename": f.filename,
        "purpose": f.purpose,
        "status": "processed",
        "status_details": None,
    }


def _batch_payload(b: Batch) -> dict:
    errors = None
    if b.status == "failed" and b.error_message:
        errors = {
            "object": "list",
            "data": [{"code": "batch_failed", "message": b.error_message}],
        }
    return {
        "id": b.id,
        "object": "batch",
        "endpoint": b.endpoint,
        "input_file_id": b.input_file_id,
        "completion_window": b.completion_window,
        "status": b.status,
        "output_file_id": b.output_file_id,
        "error_file_id": b.error_file_id,
        "errors": errors,
        "created_at": _unix(b.created_at),
        "in_progress_at": _unix(b.in_progress_at),
        "finalizing_at": _unix(b.finalizing_at),
        "completed_at": _unix(b.completed_at),
        "failed_at": _unix(b.failed_at),
        "expired_at": _unix(b.expired_at),
        "cancelled_at": _unix(b.cancelled_at),
        "request_counts": {
            "total": b.total_requests,
            "completed": b.completed_lines,
            "failed": b.failed_lines,
        },
        "metadata": b.meta or {},
    }


async def _batch_auth(
    request: Request, auth: Annotated[AuthContext, Depends(require_api_key)]
) -> AuthContext:
    """The files/batches surface: authenticate (chain), then require the
    feature flag and a virtual key. Order is fail-closed — a bad key is
    401 even when the API is disabled."""
    if not request.app.state.config.batch.enabled:
        raise GatewayError(
            404,
            "The files/batches API is disabled on this gateway "
            "(config: batch.enabled: false).",
            "invalid_request_error",
            code="batch_disabled",
        )
    if auth.key_id is None:
        raise GatewayError(
            403,
            "Files and batches require a virtual API key — the platform "
            "admin key has no key row to own them or carry spend.",
            "invalid_request_error",
            code="virtual_key_required",
        )
    return auth


def _line_error(number: int, problem: str) -> GatewayError:
    return GatewayError(
        400,
        f"Input line {number}: {problem}",
        "invalid_request_error",
        code="invalid_batch_line",
        param="input_file_id",
    )


def _validate_input(content: str, max_requests: int) -> int:
    """Validate every JSONL line; returns the request count.

    Rules: every line must be a JSON object (no blank lines — a single
    trailing newline is fine, ``splitlines`` already drops it) with a
    string ``custom_id`` and an object ``body``; ``method`` when present
    must be POST and ``url`` must be ``/v1/chat/completions`` (the only
    surface the batch worker drives). ``body.stream`` is stripped at
    execution — batches never stream.
    """
    if not content.strip():
        raise GatewayError(
            400,
            "The input file contains no requests.",
            "invalid_request_error",
            code="empty_batch_input",
            param="input_file_id",
        )
    count = 0
    for number, raw in enumerate(content.splitlines(), start=1):
        try:
            line = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _line_error(number, f"not valid JSON ({exc.msg})") from exc
        if not isinstance(line, dict):
            raise _line_error(number, "must be a JSON object")
        custom_id = line.get("custom_id")
        if not isinstance(custom_id, str) or not custom_id.strip():
            raise _line_error(number, "'custom_id' (string) is required")
        body = line.get("body")
        if not isinstance(body, dict):
            raise _line_error(number, "'body' (object) is required")
        try:
            # The same validation an interactive request gets — so a
            # created batch is always runnable (bodies that would 400
            # are refused up front).
            ChatCompletionRequest.model_validate(body)
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(part) for part in first.get("loc", ()))
            detail = f"{loc}: {first.get('msg')}" if loc else str(first.get("msg"))
            raise _line_error(number, f"invalid body — {detail}") from exc
        method = line.get("method", "POST")
        if str(method).upper() != "POST":
            raise _line_error(number, f"method {method!r} is not supported (only POST)")
        url = line.get("url", _SUPPORTED_ENDPOINT)
        if url != _SUPPORTED_ENDPOINT:
            raise _line_error(
                number, f"url {url!r} is not supported (only {_SUPPORTED_ENDPOINT})"
            )
        count += 1
    if count > max_requests:
        raise GatewayError(
            400,
            f"The input file contains {count} requests (limit {max_requests} = "
            "batch.max_requests).",
            "invalid_request_error",
            code="too_many_batch_requests",
            param="input_file_id",
        )
    return count


async def _owned_file(
    session, file_id: str, auth: AuthContext, *, for_batch: bool = False
) -> StoredFile:
    """The caller's file, or 404 — a file that exists but belongs to
    another key is indistinguishable from one that never did."""
    f = await session.get(StoredFile, file_id)
    if f is None or f.key_id != auth.key_id:
        raise GatewayError(
            404,
            f"The file '{file_id}' does not exist.",
            "invalid_request_error",
            code="file_not_found",
        )
    if for_batch and f.purpose != "batch":
        raise GatewayError(
            400,
            f"The file '{file_id}' was not uploaded with purpose='batch' "
            f"(purpose: {f.purpose!r}).",
            "invalid_request_error",
            code="invalid_purpose",
            param="input_file_id",
        )
    return f


async def _owned_batch(session, batch_id: str, auth: AuthContext) -> Batch:
    b = await session.get(Batch, batch_id)
    if b is None or b.key_id != auth.key_id:
        raise GatewayError(
            404,
            f"The batch '{batch_id}' does not exist.",
            "invalid_request_error",
            code="batch_not_found",
        )
    return b


# ----------------------------------------------------------------------
# /v1/files
# ----------------------------------------------------------------------


@router.post("/v1/files")
async def upload_file(
    request: Request, auth: Annotated[AuthContext, Depends(_batch_auth)]
):
    form = await request.form()
    upload = None
    for key, value in form.multi_items():
        if key == "file" and hasattr(value, "read"):
            upload = value
            break
    if upload is None:
        raise GatewayError(
            400,
            "Multipart field 'file' is required.",
            "invalid_request_error",
            code="file_required",
        )
    purpose = None
    for key, value in form.multi_items():
        if key == "purpose" and not hasattr(value, "read"):
            purpose = str(value)
            break
    if purpose != "batch":
        raise GatewayError(
            400,
            "Multipart field 'purpose' is required and must be 'batch' "
            f"(got {purpose!r}).",
            "invalid_request_error",
            code="invalid_purpose",
        )

    content = await upload.read()
    cfg = request.app.state.config.batch
    if len(content) > cfg.max_file_bytes:
        raise GatewayError(
            413,
            f"File too large: {len(content)} bytes "
            f"(limit {cfg.max_file_bytes} = batch.max_file_bytes).",
            "request_too_large",
            code="file_too_large",
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GatewayError(
            400,
            f"File must be UTF-8 text (JSONL): {exc}",
            "invalid_request_error",
            code="invalid_file_encoding",
        ) from exc

    stored = StoredFile(
        key_id=auth.key_id,
        tenant_id=auth.tenant_id,
        filename=getattr(upload, "filename", None) or "input.jsonl",
        purpose="batch",
        bytes=len(content),
        content=text,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(stored)
        await session.commit()
    return _file_payload(stored)


@router.get("/v1/files")
async def list_files(
    auth: Annotated[AuthContext, Depends(_batch_auth)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    after: str | None = None,
):
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        conds = [StoredFile.key_id == auth.key_id]
        if after is not None:
            anchor = await _owned_file(session, after, auth)
            # Newest-first listing: the next page is everything strictly
            # after the anchor in that order.
            conds.append(
                or_(
                    StoredFile.created_at < anchor.created_at,
                    and_(
                        StoredFile.created_at == anchor.created_at,
                        StoredFile.id < anchor.id,
                    ),
                )
            )
        rows = (
            (
                await session.execute(
                    select(StoredFile)
                    .where(*conds)
                    .order_by(StoredFile.created_at.desc(), StoredFile.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return {"object": "list", "data": [_file_payload(r) for r in rows]}


@router.get("/v1/files/{file_id}")
async def retrieve_file(
    file_id: str, auth: Annotated[AuthContext, Depends(_batch_auth)]
):
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        f = await _owned_file(session, file_id, auth)
    return _file_payload(f)


@router.get("/v1/files/{file_id}/content")
async def retrieve_file_content(
    file_id: str, auth: Annotated[AuthContext, Depends(_batch_auth)]
):
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        f = await _owned_file(session, file_id, auth)
    # Raw passthrough (JSONL is not one JSON document, so no re-encoding).
    return Response(content=f.content, media_type="application/json")


# ----------------------------------------------------------------------
# /v1/batches
# ----------------------------------------------------------------------


class CreateBatchRequest(BaseModel):
    input_file_id: str
    endpoint: str = _SUPPORTED_ENDPOINT
    completion_window: str = _SUPPORTED_WINDOW
    metadata: dict[str, str] = Field(default_factory=dict)


@router.post("/v1/batches")
async def create_batch(
    payload: CreateBatchRequest,
    request: Request,
    auth: Annotated[AuthContext, Depends(_batch_auth)],
):
    if payload.endpoint != _SUPPORTED_ENDPOINT:
        raise GatewayError(
            400,
            f"endpoint {payload.endpoint!r} is not supported "
            f"(only {_SUPPORTED_ENDPOINT}).",
            "invalid_request_error",
            code="unsupported_batch_endpoint",
            param="endpoint",
        )
    if payload.completion_window != _SUPPORTED_WINDOW:
        raise GatewayError(
            400,
            f"completion_window {payload.completion_window!r} is not supported "
            f"(only {_SUPPORTED_WINDOW}).",
            "invalid_request_error",
            code="unsupported_completion_window",
            param="completion_window",
        )

    now = _now()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        f = await _owned_file(
            session, payload.input_file_id, auth, for_batch=True
        )
        count = _validate_input(f.content, request.app.state.config.batch.max_requests)
        batch = Batch(
            key_id=auth.key_id,
            tenant_id=auth.tenant_id,
            input_file_id=f.id,
            endpoint=payload.endpoint,
            completion_window=payload.completion_window,
            meta=payload.metadata,
            status="in_progress",
            total_requests=count,
            created_at=now,
            in_progress_at=now,
            expires_at=now + timedelta(hours=24),
        )
        session.add(batch)
        await session.commit()
    request.app.state.events.emit_background(
        "batch.created",
        None,
        batch_id=batch.id,
        key_id=batch.key_id,
        input_file_id=batch.input_file_id,
        total_requests=batch.total_requests,
    )
    return _batch_payload(batch)


@router.get("/v1/batches")
async def list_batches(
    auth: Annotated[AuthContext, Depends(_batch_auth)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    after: str | None = None,
):
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        conds = [Batch.key_id == auth.key_id]
        if after is not None:
            anchor = await _owned_batch(session, after, auth)
            conds.append(
                or_(
                    Batch.created_at < anchor.created_at,
                    and_(
                        Batch.created_at == anchor.created_at,
                        Batch.id < anchor.id,
                    ),
                )
            )
        rows = (
            (
                await session.execute(
                    select(Batch)
                    .where(*conds)
                    .order_by(Batch.created_at.desc(), Batch.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return {"object": "list", "data": [_batch_payload(r) for r in rows]}


@router.get("/v1/batches/{batch_id}")
async def retrieve_batch(
    batch_id: str, auth: Annotated[AuthContext, Depends(_batch_auth)]
):
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        b = await _owned_batch(session, batch_id, auth)
    return _batch_payload(b)


@router.post("/v1/batches/{batch_id}/cancel")
async def cancel_batch(
    batch_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(_batch_auth)],
):
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        b = await _owned_batch(session, batch_id, auth)
        # Guarded update: a cancel that races the worker's final wave
        # never overwrites a terminal state (last write must not win).
        result = await session.execute(
            update(Batch)
            .where(
                Batch.id == b.id,
                Batch.status.in_(("in_progress", "finalizing")),
            )
            .values(status="cancelled", cancelled_at=_now())
        )
        if (result.rowcount or 0) == 0:
            await session.refresh(b)
            raise GatewayError(
                400,
                f"This batch is {b.status} and can no longer be cancelled.",
                "invalid_request_error",
                code="cant_cancel_finished_batch",
            )
        await session.commit()
        await session.refresh(b)
    request.app.state.events.emit_background(
        "batch.cancelled",
        None,
        batch_id=b.id,
        key_id=b.key_id,
        completed=b.completed_lines,
        failed=b.failed_lines,
        total=b.total_requests,
    )
    return _batch_payload(b)
