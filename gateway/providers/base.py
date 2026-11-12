"""Provider adapter abstraction.

Adapters speak OpenAI on the gateway side (in and out) and provider-native
on the upstream side. Three methods:

* ``complete`` — non-streaming; returns an OpenAI ``chat.completion`` dict.
* ``stream``  — yields OpenAI ``chat.completion.chunk`` dicts; usage (when
  available) is attached to the final chunk's ``usage`` field.
* ``embed``   — embeddings; returns an OpenAI ``list[embedding]`` dict.
* ``generate_image`` — image generation; returns an OpenAI ``images.response``
  dict (``{created, data: [...]}``).
* ``edit_image`` — image editing (uploaded image(s) + prompt); returns an
  OpenAI ``images.response`` dict.
* ``vary_image`` — image variations (uploaded image, no prompt); returns an
  OpenAI ``images.response`` dict.
* ``transcribe`` — audio transcription/translation; returns
  ``(content, media_type)`` — content is a dict for JSON response formats,
  plain text otherwise (srt/vtt/text).
* ``speak``    — text-to-speech; returns ``(byte_iterator, media_type)``
  streaming as the provider sends it.

The base implementations of the non-chat methods raise a clear error that
is not retryable on the same deployment but does fall through to the next
one, so chat-only providers (e.g. Anthropic) need no special handling —
the router falls through to a deployment that supports the modality.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import AsyncIterator, ClassVar

import httpx

from ..config import DeploymentConfig, ProviderConfig
from ..errors import ProviderError
from ..schemas import (
    ChatCompletionRequest,
    EmbeddingRequest,
    ImageEditRequest,
    ImageGenerationRequest,
    ImageVariationRequest,
    SpeechRequest,
    TranscriptionRequest,
)
from .sse import sse_data_lines


def extract_error_message(data: object) -> str | None:
    """Best-effort error message extraction across provider JSON bodies."""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("message") or err.get("status") or json.dumps(err)
        if isinstance(err, str):
            return err
        if "message" in data:
            return str(data["message"])
    return None


def raise_for_provider_status(response: httpx.Response, provider: str) -> None:
    """Map an upstream HTTP error to a ProviderError with retry/fallback hints."""
    if response.status_code < 400:
        return
    try:
        data = response.json()
    except Exception:
        data = None
    message = extract_error_message(data) or (response.text or "")[:500] or "provider error"
    status = response.status_code
    if status == 400:
        raise ProviderError(400, message, "invalid_request_error", provider=provider)
    if status in (401, 403):
        # Broken credentials — retrying the same deployment is pointless,
        # but failing over to another deployment makes sense.
        raise ProviderError(
            status,
            f"Provider authentication failed: {message}",
            "authentication_error" if status == 401 else "permission_error",
            provider=provider,
            fallback=True,
        )
    if status == 404:
        raise ProviderError(
            404, message, "invalid_request_error", code="model_not_found",
            param="model", provider=provider, fallback=True,
        )
    if status == 413:
        raise ProviderError(413, message, "request_too_large", provider=provider)
    if status == 422:
        raise ProviderError(422, message, "invalid_request_error", provider=provider)
    if status == 429:
        raise ProviderError(
            429, f"Provider rate limit: {message}", "rate_limit_error",
            code="rate_limit_exceeded", provider=provider, retryable=True, fallback=True,
        )
    raise ProviderError(
        status or 502, f"Provider error ({status}): {message}", "api_error",
        code="provider_error", provider=provider, retryable=True, fallback=True,
    )


def network_error(provider: str, exc: Exception) -> ProviderError:
    return ProviderError(
        503,
        f"Could not reach provider '{provider}': {exc}",
        "api_error",
        code="provider_unreachable",
        provider=provider,
        retryable=True,
        fallback=True,
    )


class ProviderAdapter(ABC):
    name: ClassVar[str]

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    @abstractmethod
    async def complete(
        self,
        deployment: DeploymentConfig,
        request: ChatCompletionRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        """Execute a non-streaming chat completion; return OpenAI-format dict."""

    @abstractmethod
    def stream(
        self,
        deployment: DeploymentConfig,
        request: ChatCompletionRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> AsyncIterator[dict]:
        """Yield OpenAI-format chunk dicts for a streaming completion."""

    async def embed(
        self,
        deployment: DeploymentConfig,
        request: EmbeddingRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        """Execute an embeddings request; return OpenAI-format dict.

        Chat-only providers keep this default: the error is not retryable
        on the same deployment but does fall through to the next one, so a
        mixed routing group (e.g. Anthropic primary, OpenAI fallback)
        still works.
        """
        raise ProviderError(
            501,
            f"Provider '{self.name}' does not support embeddings "
            f"(deployment '{deployment.name}').",
            "api_error",
            code="embeddings_not_supported",
            fallback=True,
        )

    async def generate_image(
        self,
        deployment: DeploymentConfig,
        request: ImageGenerationRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        """Generate images; return an OpenAI images-format dict."""
        raise ProviderError(
            501,
            f"Provider '{self.name}' does not support image generation "
            f"(deployment '{deployment.name}').",
            "api_error",
            code="images_not_supported",
            fallback=True,
        )

    async def edit_image(
        self,
        deployment: DeploymentConfig,
        request: ImageEditRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        """Edit uploaded image(s) per a prompt; return an OpenAI
        images-format dict."""
        raise ProviderError(
            501,
            f"Provider '{self.name}' does not support image editing "
            f"(deployment '{deployment.name}').",
            "api_error",
            code="images_not_supported",
            fallback=True,
        )

    async def vary_image(
        self,
        deployment: DeploymentConfig,
        request: ImageVariationRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        """Generate variations of an uploaded image; return an OpenAI
        images-format dict."""
        raise ProviderError(
            501,
            f"Provider '{self.name}' does not support image variations "
            f"(deployment '{deployment.name}').",
            "api_error",
            code="images_not_supported",
            fallback=True,
        )

    async def transcribe(
        self,
        deployment: DeploymentConfig,
        request: TranscriptionRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> tuple[dict | str, str]:
        """Transcribe/translate audio; return ``(content, media_type)``.

        ``content`` is a dict for the JSON response formats (``json``,
        ``verbose_json``) and a string for the text ones (``text``, ``srt``,
        ``vtt``); ``media_type`` is the provider's response content type.
        """
        raise ProviderError(
            501,
            f"Provider '{self.name}' does not support audio transcription "
            f"(deployment '{deployment.name}').",
            "api_error",
            code="audio_not_supported",
            fallback=True,
        )

    async def speak(
        self,
        deployment: DeploymentConfig,
        request: SpeechRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> tuple[AsyncIterator[bytes], str]:
        """Synthesize speech; return ``(byte_iterator, media_type)`` — the
        audio streams to the client as the provider sends it."""
        raise ProviderError(
            501,
            f"Provider '{self.name}' does not support text-to-speech "
            f"(deployment '{deployment.name}').",
            "api_error",
            code="audio_not_supported",
            fallback=True,
        )

    async def _read_json(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict,
        timeout: httpx.Timeout,
        provider: str,
    ) -> dict:
        try:
            response = await self.client.request(
                method, url, headers=headers, json=body, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise network_error(provider, exc) from exc
        raise_for_provider_status(response, provider)
        try:
            return response.json()
        except Exception as exc:  # invalid JSON from provider
            raise ProviderError(
                502, f"Invalid JSON from provider '{provider}'", "api_error",
                retryable=True, fallback=True, provider=provider,
            ) from exc

    @staticmethod
    def _sse_body_chunks(
        line: str, provider: str
    ) -> dict | None:
        """Parse one SSE data line to JSON; [DONE] and empty lines yield None."""
        if not line or line == "[DONE]":
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None
