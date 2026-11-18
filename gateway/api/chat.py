"""POST /v1/chat/completions — the main proxy endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ..core.context import AuthContext
from ..schemas import ChatCompletionRequest
from .deps import require_api_key

router = APIRouter()


@router.post("/v1/chat/completions")
async def create_chat_completion(
    request_body: ChatCompletionRequest,
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
):
    request_id = getattr(raw_request.state, "request_id", None)
    pipeline = raw_request.app.state.pipeline
    return await pipeline.handle_chat(
        request_body,
        auth,
        request_id=request_id,
        route="chat",
        idempotency_key=raw_request.headers.get("Idempotency-Key"),
    )
