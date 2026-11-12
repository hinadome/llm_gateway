"""OpenAI-compatible image endpoints.

* ``POST /v1/images/generations``  (JSON: prompt + params)
* ``POST /v1/images/edits``        (multipart: image(s) + prompt, optional mask)
* ``POST /v1/images/variations``   (multipart: image, no prompt)

Multipart is parsed once at the edge (python-multipart), validated against
``uploads.max_upload_mb`` (per file and in aggregate), and re-serialized
upstream — the same routing/retry/breaker/accounting machinery as the rest
of the gateway, with unknown form fields passed through for provider
passthrough. An ``image`` form value that is a plain string (not a file)
is accepted as base64 image data, with an optional ``data:`` URL prefix —
so base64-native clients work alongside OpenAI-SDK-style file uploads.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError

from ..core.context import AuthContext
from ..errors import GatewayError
from ..schemas import ImageEditRequest, ImageGenerationRequest, ImageVariationRequest
from .deps import require_api_key

router = APIRouter()

# Known scalar form fields per surface (everything else rides along in
# ``extra`` for provider passthrough).
_EDIT_FIELDS = ("model", "prompt", "n", "size", "quality", "response_format", "user")
_VARIATION_FIELDS = ("model", "n", "size", "response_format", "user")


def _decode_b64_image(value: str) -> tuple[bytes, str, str]:
    """A plain-string form value for ``image``: base64 data, optionally
    as a data URL (``data:image/png;base64,....``)."""
    content_type = "image/png"
    data = value
    if data.startswith("data:"):
        try:
            header, data = data.split(",", 1)
            media = header[5:].split(";")[0]
            if media:
                content_type = media
        except ValueError as exc:
            raise RequestValidationError(
                [{"loc": ("body", "image"), "msg": "Invalid data URL", "type": "value_error"}]
            ) from exc
    try:
        return base64.b64decode(data, validate=True), "image", content_type
    except (binascii.Error, ValueError) as exc:
        raise RequestValidationError(
            [{"loc": ("body", "image"), "msg": "Invalid base64 image data", "type": "value_error"}]
        ) from exc


async def _image_files_from_form(
    form_values: list[tuple[str, object]],
) -> list[tuple[bytes, str, str | None]]:
    """Collect the uploaded images: ``image``/``image[]`` file parts, or
    base64 string values under the same names."""
    images: list[tuple[bytes, str, str | None]] = []
    for key, value in form_values:
        if key not in ("image", "image[]"):
            continue
        if hasattr(value, "read"):  # starlette UploadFile
            content = await value.read()
            images.append(
                (
                    content,
                    getattr(value, "filename", None) or "image",
                    getattr(value, "content_type", None),
                )
            )
        elif isinstance(value, str) and value:
            images.append(_decode_b64_image(value))
    return images


def _check_upload_size(raw_request: Request, images: list[tuple[bytes, str, str | None]],
                        mask: tuple[bytes, str, str | None] | None) -> None:
    total = sum(len(content) for content, _, _ in images)
    if mask is not None:
        total += len(mask[0])
    max_bytes = raw_request.app.state.config.uploads.max_upload_mb * 1024 * 1024
    if total > max_bytes:
        raise GatewayError(
            413,
            f"Uploaded images too large: {total} bytes "
            f"(limit {max_bytes} bytes = uploads.max_upload_mb).",
            "request_too_large",
            code="upload_too_large",
        )


async def _edit_from_form(raw_request: Request) -> ImageEditRequest:
    form = await raw_request.form()
    items = list(form.multi_items())
    images = await _image_files_from_form(items)
    if not images:
        raise RequestValidationError(
            [{"loc": ("body", "image"), "msg": "Field required", "type": "missing"}]
        )
    fields: dict[str, str] = {}
    extra: dict[str, list[str]] = {}
    mask: tuple[bytes, str, str | None] | None = None
    for key, value in items:
        if hasattr(value, "read"):
            if key == "mask" and mask is None:
                content = await value.read()
                mask = (
                    content,
                    getattr(value, "filename", None) or "mask",
                    getattr(value, "content_type", None),
                )
            continue
        if key in _EDIT_FIELDS and key not in fields:
            fields[key] = str(value)
        elif key not in ("image", "image[]", "mask"):
            extra.setdefault(key, []).append(str(value))

    if not fields.get("model"):
        raise RequestValidationError(
            [{"loc": ("body", "model"), "msg": "Field required", "type": "missing"}]
        )
    prompt = fields.get("prompt")
    if not prompt:
        raise RequestValidationError(
            [{"loc": ("body", "prompt"), "msg": "Field required", "type": "missing"}]
        )
    _check_upload_size(raw_request, images, mask)
    return ImageEditRequest(
        model=fields["model"],
        prompt=prompt,
        images=images,
        mask=mask,
        n=int(fields["n"]) if fields.get("n") else 1,
        size=fields.get("size"),
        quality=fields.get("quality"),
        response_format=fields.get("response_format"),
        user=fields.get("user"),
        extra=extra,
    )


async def _variation_from_form(raw_request: Request) -> ImageVariationRequest:
    form = await raw_request.form()
    items = list(form.multi_items())
    images = await _image_files_from_form(items)
    if not images:
        raise RequestValidationError(
            [{"loc": ("body", "image"), "msg": "Field required", "type": "missing"}]
        )
    fields: dict[str, str] = {}
    extra: dict[str, list[str]] = {}
    for key, value in items:
        if hasattr(value, "read"):
            continue
        if key in _VARIATION_FIELDS and key not in fields:
            fields[key] = str(value)
        elif key not in ("image", "image[]"):
            extra.setdefault(key, []).append(str(value))

    if not fields.get("model"):
        raise RequestValidationError(
            [{"loc": ("body", "model"), "msg": "Field required", "type": "missing"}]
        )
    _check_upload_size(raw_request, images, None)
    return ImageVariationRequest(
        model=fields["model"],
        images=images,
        n=int(fields["n"]) if fields.get("n") else 1,
        size=fields.get("size"),
        response_format=fields.get("response_format"),
        user=fields.get("user"),
        extra=extra,
    )


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


@router.post("/v1/images/edits")
async def edit_image(
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
):
    request = await _edit_from_form(raw_request)
    request_id = getattr(raw_request.state, "request_id", None)
    pipeline = raw_request.app.state.pipeline
    return await pipeline.handle_image_edit(request, auth, request_id=request_id)


@router.post("/v1/images/variations")
async def vary_image(
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
):
    request = await _variation_from_form(raw_request)
    request_id = getattr(raw_request.state, "request_id", None)
    pipeline = raw_request.app.state.pipeline
    return await pipeline.handle_image_variation(request, auth, request_id=request_id)
