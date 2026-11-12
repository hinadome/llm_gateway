"""GET /v1/models and GET /v1/models/{model} — OpenAI-compatible model
listing and retrieval, filtered per key (and per-key model mappings
included, so mapped clients see the names they are allowed to send)."""

from __future__ import annotations

import fnmatch
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ..core.context import AuthContext
from ..errors import ModelNotAllowedError, ModelNotFoundError
from .deps import require_api_key

router = APIRouter()


def _permitted(name: str, auth: AuthContext) -> bool:
    """Does this key's allowlist admit this (public) model name?"""
    if auth.is_admin or not auth.allowed_models:
        return True
    return any(fnmatch.fnmatch(name, p) for p in auth.allowed_models)


def _mapping_names(config, auth: AuthContext) -> list[str]:
    """Names this key may send because its model_mappings resolve them.

    A mapping qualifies when its target is a configured public model the
    key is permitted to use — mirroring how request-time permission checks
    match the *resolved* name.
    """
    if not auth.model_mappings:
        return []
    public = set(config.public_models())
    return [
        name
        for name, target in auth.model_mappings.items()
        if target in public and _permitted(target, auth)
    ]


@router.get("/v1/models")
async def list_models(
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
):
    config = raw_request.app.state.config
    models = config.public_models()
    if not auth.is_admin and auth.allowed_models:
        models = [m for m in models if _permitted(m, auth)]
    # per-key mapping names are valid *for this key* — list them too
    for name in _mapping_names(config, auth):
        if name not in models:
            models.append(name)
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": now, "owned_by": "llm-gateway"}
            for m in models
        ],
    }


@router.get("/v1/models/{model}")
async def retrieve_model(
    model: str,
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
):
    """Model retrieve — frameworks validate models at startup against this
    (a bare 404 here is what breaks them, so answer in the OpenAI shape)."""
    config = raw_request.app.state.config
    # A name this key maps elsewhere resolves (echoed as asked, like aliases)
    is_mapped = bool(auth.model_mappings and model in auth.model_mappings)
    if model not in config.public_models() and not is_mapped:
        raise ModelNotFoundError(model)
    if not _permitted(model if not is_mapped else auth.model_mappings[model], auth):
        raise ModelNotAllowedError(model)
    return {
        "id": model,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "llm-gateway",
    }
