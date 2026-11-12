"""POST /v1/images/generations — OpenAI-compatible image generation."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ..core.context import AuthContext
from ..schemas import ImageGenerationRequest
from .deps import require_api_key

router = APIRouter()


@router.post("/v1/images/generations")
async def create_image(
    request_body: ImageGenerationRequest,
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
):
    if request_body.model_dump().get("stream"):
        # gpt-image-1 streaming partial images: not supported by the
        # gateway (it would bypass accounting). Fail loudly, not silently.
        from ..errors import GatewayError

        raise GatewayError(
            400,
            "Streaming image generation (stream: true) is not supported.",
            "invalid_request_error",
            param="stream",
        )
    request_id = getattr(raw_request.state, "request_id", None)
    pipeline = raw_request.app.state.pipeline
    return await pipeline.handle_image_generation(request_body, auth, request_id=request_id)
