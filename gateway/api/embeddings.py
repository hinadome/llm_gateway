"""POST /v1/embeddings — OpenAI-compatible embeddings endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ..core.context import AuthContext
from ..schemas import EmbeddingRequest
from .deps import require_api_key

router = APIRouter()


@router.post("/v1/embeddings")
async def create_embedding(
    request_body: EmbeddingRequest,
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
):
    request_id = getattr(raw_request.state, "request_id", None)
    pipeline = raw_request.app.state.pipeline
    return await pipeline.handle_embedding(request_body, auth, request_id=request_id)
