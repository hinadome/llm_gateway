"""Audio endpoints — OpenAI-compatible transcription, translation and TTS.

* ``POST /v1/audio/transcriptions``  (multipart: file + params)
* ``POST /v1/audio/translations``    (same shape; translates to English)
* ``POST /v1/audio/speech``          (JSON in, binary audio out)

Multipart is parsed once at the edge (python-multipart), validated against
``audio.max_upload_mb``, and re-serialized upstream — so the same
routing/retry/breaker/accounting machinery as the rest of the gateway
applies, and providers that need extra form fields get them passed through.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError

from ..core.context import AuthContext
from ..errors import GatewayError
from ..schemas import SpeechRequest, TranscriptionRequest
from .deps import require_api_key

router = APIRouter()

# Known form fields -> TranscriptionRequest fields. Everything else rides
# along in ``extra`` (provider passthrough, e.g. timestamp_granularities[]).
_FORM_FIELDS = ("model", "language", "prompt", "response_format", "temperature")


async def _transcription_from_form(
    raw_request: Request, endpoint: str
) -> TranscriptionRequest:
    """Build the internal request from the parsed multipart form."""
    form = await raw_request.form()
    file = None
    fields: dict[str, str] = {}
    extra: dict[str, list[str]] = {}
    for key, value in form.multi_items():
        if hasattr(value, "read"):  # starlette UploadFile
            if key == "file" and file is None:
                file = value
            continue
        if key in _FORM_FIELDS and key not in fields:
            fields[key] = str(value)
        elif key != "file":
            extra.setdefault(key, []).append(str(value))

    if file is None:
        raise RequestValidationError(
            [{"loc": ("body", "file"), "msg": "Field required", "type": "missing"}]
        )
    model = fields.get("model")
    if not model:
        raise RequestValidationError(
            [{"loc": ("body", "model"), "msg": "Field required", "type": "missing"}]
        )

    content = await file.read()
    max_bytes = raw_request.app.state.config.audio.max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise GatewayError(
            413,
            f"Audio file too large: {len(content)} bytes "
            f"(limit {max_bytes} bytes = audio.max_upload_mb).",
            "request_too_large",
            code="audio_file_too_large",
        )
    return TranscriptionRequest(
        model=model,
        endpoint=endpoint,
        file=content,
        file_name=getattr(file, "filename", None) or "audio",
        file_content_type=getattr(file, "content_type", None),
        language=fields.get("language"),
        prompt=fields.get("prompt"),
        response_format=fields.get("response_format"),
        temperature=(
            float(fields["temperature"]) if fields.get("temperature") else None
        ),
        extra=extra,
    )


async def _run_transcription(
    raw_request: Request, auth: AuthContext, endpoint: str
):
    request = await _transcription_from_form(raw_request, endpoint)
    request_id = getattr(raw_request.state, "request_id", None)
    pipeline = raw_request.app.state.pipeline
    return await pipeline.handle_transcription(request, auth, request_id=request_id)


@router.post("/v1/audio/transcriptions")
async def transcribe_audio(
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
):
    return await _run_transcription(raw_request, auth, "transcriptions")


@router.post("/v1/audio/translations")
async def translate_audio(
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
):
    return await _run_transcription(raw_request, auth, "translations")


@router.post("/v1/audio/speech")
async def create_speech(
    request_body: SpeechRequest,
    raw_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
):
    request_id = getattr(raw_request.state, "request_id", None)
    pipeline = raw_request.app.state.pipeline
    return await pipeline.handle_speech(request_body, auth, request_id=request_id)
