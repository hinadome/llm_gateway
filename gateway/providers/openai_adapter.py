"""OpenAI adapter (also works for any OpenAI-compatible API).

Because the gateway itself speaks the OpenAI wire format, this adapter is
close to a passthrough: it forwards the request as-is (with the native model
id) and relays the response. Streaming chunks pass through unchanged, except
that ``stream_options.include_usage`` is forced on so the gateway can account
usage even when the client did not ask for it.

One southbound branch departs from pure chat shape: deployments with
``native_responses: true`` that receive a request which arrived via
``POST /v1/responses`` forward the *original* Responses body to the
provider's ``/responses`` endpoint (see ``providers/responses_native.py``)
and translate the response back into the chat shape the pipeline expects —
so Responses-specific fields survive the round trip while policy, cache,
accounting and logging stay on the internal representation.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from ..config import DeploymentConfig, ProviderConfig
from ..schemas import (
    ChatCompletionRequest,
    EmbeddingRequest,
    ImageEditRequest,
    ImageGenerationRequest,
    ImageVariationRequest,
    SpeechRequest,
    TranscriptionRequest,
)
from .base import ProviderAdapter, network_error, raise_for_provider_status
from .responses_native import completion_to_chat, native_body, stream_events_to_chunks
from .sse import sse_data_lines


class OpenAIProviderAdapter(ProviderAdapter):
    name = "openai"

    # URL/header construction is hook-based so Azure-style OpenAI services
    # (deployment-addressed, api-key header, versioned query param) can
    # subclass this adapter and change only addressing/auth.
    def _chat_url(self, deployment: DeploymentConfig, provider_cfg: ProviderConfig) -> str:
        return f"{provider_cfg.api_base.rstrip('/')}/chat/completions"

    def _responses_url(self, deployment: DeploymentConfig, provider_cfg: ProviderConfig) -> str:
        # Native Responses southbound (deployment.native_responses)
        return f"{provider_cfg.api_base.rstrip('/')}/responses"

    def _embed_url(self, deployment: DeploymentConfig, provider_cfg: ProviderConfig) -> str:
        return f"{provider_cfg.api_base.rstrip('/')}/embeddings"

    def _images_url(self, deployment: DeploymentConfig, provider_cfg: ProviderConfig) -> str:
        return f"{provider_cfg.api_base.rstrip('/')}/images/generations"

    def _images_edits_url(self, deployment: DeploymentConfig, provider_cfg: ProviderConfig) -> str:
        return f"{provider_cfg.api_base.rstrip('/')}/images/edits"

    def _images_variations_url(self, deployment: DeploymentConfig, provider_cfg: ProviderConfig) -> str:
        return f"{provider_cfg.api_base.rstrip('/')}/images/variations"

    def _audio_url(
        self, deployment: DeploymentConfig, provider_cfg: ProviderConfig, kind: str
    ) -> str:
        # kind: "transcriptions" | "translations"
        return f"{provider_cfg.api_base.rstrip('/')}/audio/{kind}"

    def _speech_url(self, deployment: DeploymentConfig, provider_cfg: ProviderConfig) -> str:
        return f"{provider_cfg.api_base.rstrip('/')}/audio/speech"

    def _headers(self, provider_cfg: ProviderConfig) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {provider_cfg.resolve_key() or ''}",
            "Content-Type": "application/json",
        }

    def _auth_headers(self, provider_cfg: ProviderConfig) -> dict[str, str]:
        """Auth only — for requests where httpx owns the Content-Type
        (multipart uploads, which need their boundary in it)."""
        return {"Authorization": f"Bearer {provider_cfg.resolve_key() or ''}"}

    @staticmethod
    def _build_body(request: ChatCompletionRequest, native_model: str) -> dict:
        body: dict = request.model_dump(exclude_none=True, exclude={"stream_options"})
        body["model"] = native_model
        if request.stream:
            body["stream_options"] = {"include_usage": True}
        return body

    async def complete(
        self,
        deployment: DeploymentConfig,
        request: ChatCompletionRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        # Native Responses southbound: the request arrived via the
        # Responses bridge and the messages are still the pristine parse
        # (no guardrail/prompt-policy modification — those fall back to
        # the chat path). Forward the original body, translate back.
        native = native_body(deployment, request, stream=request.stream)
        if native is not None:
            data = await self._read_json(
                "POST",
                self._responses_url(deployment, provider_cfg),
                self._headers(provider_cfg),
                native,
                timeout,
                self.name,
            )
            return completion_to_chat(data, request.model)
        body = self._build_body(request, deployment.model)
        data = await self._read_json(
            "POST",
            self._chat_url(deployment, provider_cfg),
            self._headers(provider_cfg),
            body,
            timeout,
            self.name,
        )
        data["model"] = request.model  # present the public model name to clients
        return data

    async def embed(
        self,
        deployment: DeploymentConfig,
        request: EmbeddingRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        body: dict = {"model": deployment.model, "input": request.input}
        # Canonical float form internally — base64 is re-encoded on the way
        # out by the pipeline, so cache entries stay provider-neutral.
        body["encoding_format"] = "float"
        if request.dimensions is not None:
            body["dimensions"] = request.dimensions
        data = await self._read_json(
            "POST",
            self._embed_url(deployment, provider_cfg),
            self._headers(provider_cfg),
            body,
            timeout,
            self.name,
        )
        data["model"] = request.model
        return data

    async def generate_image(
        self,
        deployment: DeploymentConfig,
        request: ImageGenerationRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        body = request.model_dump(exclude_none=True)
        body["model"] = deployment.model
        return await self._read_json(
            "POST",
            self._images_url(deployment, provider_cfg),
            self._headers(provider_cfg),
            body,
            timeout,
            self.name,
        )

    def _image_files(
        self, images: list[tuple[bytes, str, str | None]]
    ) -> list[tuple[str, tuple[str, bytes, str]]]:
        """Upload list for multipart image endpoints. A single image goes
        under ``image`` (the DALL·E form); several use OpenAI's repeated
        ``image[]`` fields (the gpt-image-1 multi-reference form)."""
        if len(images) <= 1:
            name = "image"
        else:
            name = "image[]"
        return [
            (name, (filename or "image", content, content_type or "image/png"))
            for content, filename, content_type in images or []
        ]

    async def _post_images_multipart(
        self,
        url: str,
        data: dict[str, str | list[str]],
        images: list[tuple[bytes, str, str | None]],
        mask: tuple[bytes, str, str | None] | None,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        files = self._image_files(images)
        if mask is not None:
            files.append(
                ("mask", (mask[1] or "mask", mask[0], mask[2] or "image/png"))
            )
        try:
            response = await self.client.post(
                url,
                headers=self._auth_headers(provider_cfg),
                data=data,
                files=files,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise network_error(self.name, exc) from exc
        raise_for_provider_status(response, self.name)
        return response.json()

    async def edit_image(
        self,
        deployment: DeploymentConfig,
        request: ImageEditRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        """Multipart forward to OpenAI's /images/edits (image(s) + prompt,
        optional mask; unknown form fields ride along in ``extra``)."""
        data: dict[str, str | list[str]] = {
            "model": deployment.model,
            "prompt": request.prompt,
        }
        for field in ("n", "size", "quality", "response_format", "user"):
            value = getattr(request, field)
            if value is not None:
                data[field] = str(value)
        data.update(request.extra)
        return await self._post_images_multipart(
            self._images_edits_url(deployment, provider_cfg),
            data,
            request.images,
            request.mask,
            provider_cfg,
            timeout,
        )

    async def vary_image(
        self,
        deployment: DeploymentConfig,
        request: ImageVariationRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> dict:
        """Multipart forward to OpenAI's /images/variations (DALL·E 2)."""
        data: dict[str, str | list[str]] = {"model": deployment.model}
        for field in ("n", "size", "response_format", "user"):
            value = getattr(request, field)
            if value is not None:
                data[field] = str(value)
        data.update(request.extra)
        return await self._post_images_multipart(
            self._images_variations_url(deployment, provider_cfg),
            data,
            request.images,
            None,
            provider_cfg,
            timeout,
        )

    async def transcribe(
        self,
        deployment: DeploymentConfig,
        request: TranscriptionRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> tuple[dict | str, str]:
        """Multipart forward to OpenAI's /audio/{transcriptions,translations}.

        Unknown form fields ride along (``extra``), so provider-specific
        parameters (e.g. ``timestamp_granularities[]``) keep working without
        gateway changes. The response is returned with the provider's
        content type — JSON formats parse to a dict, text formats (srt/vtt)
        pass through as strings.
        """
        data: dict[str, str | list[str]] = {"model": deployment.model}
        for field in ("language", "prompt", "response_format", "temperature"):
            value = getattr(request, field)
            if value is not None:
                data[field] = str(value)
        data.update(request.extra)
        files = {
            "file": (
                request.file_name,
                request.file,
                request.file_content_type or "application/octet-stream",
            )
        }
        try:
            response = await self.client.post(
                self._audio_url(deployment, provider_cfg, request.endpoint),
                headers=self._auth_headers(provider_cfg),
                data=data,
                files=files,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise network_error(self.name, exc) from exc
        raise_for_provider_status(response, self.name)
        media_type = response.headers.get("content-type", "application/json")
        if "json" in media_type:
            try:
                return response.json(), media_type
            except Exception as exc:  # invalid JSON from provider
                raise ProviderError(
                    502, f"Invalid JSON from provider '{self.name}'", "api_error",
                    retryable=True, fallback=True, provider=self.name,
                ) from exc
        return response.text, media_type

    async def speak(
        self,
        deployment: DeploymentConfig,
        request: SpeechRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> tuple[AsyncIterator[bytes], str]:
        """Stream the provider's audio as it arrives: the upstream response
        body is forwarded chunk-by-chunk (never buffered whole)."""
        body = request.model_dump(exclude_none=True)
        body["model"] = deployment.model
        upstream = self.client.build_request(
            "POST",
            self._speech_url(deployment, provider_cfg),
            headers=self._headers(provider_cfg),
            json=body,
            timeout=timeout,
        )
        try:
            response = await self.client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            raise network_error(self.name, exc) from exc
        if response.status_code >= 400:
            await response.aread()  # so error mapping can see the body
            await response.aclose()
            raise_for_provider_status(response, self.name)
        media_type = response.headers.get("content-type", "audio/mpeg")

        async def _audio() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            except httpx.HTTPError as exc:
                raise network_error(self.name, exc) from exc
            finally:
                await response.aclose()

        return _audio(), media_type

    async def stream(
        self,
        deployment: DeploymentConfig,
        request: ChatCompletionRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> AsyncIterator[dict]:
        # Native Responses southbound: stream the provider's /responses
        # event stream and translate it into the same chat-chunk
        # sequence the chat path produces (role chunk, deltas, finish,
        # usage-only terminal chunk).
        native = native_body(deployment, request, stream=request.stream)
        if native is not None:
            async for chunk in self._stream_native(
                deployment, native, request, provider_cfg, timeout
            ):
                yield chunk
            return
        body = self._build_body(request, deployment.model)
        try:
            async with self.client.stream(
                "POST",
                self._chat_url(deployment, provider_cfg),
                headers=self._headers(provider_cfg),
                json=body,
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise_for_provider_status(response, self.name)
                async for line in sse_data_lines(response.aiter_bytes()):
                    if not line or line == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk["model"] = request.model
                    yield chunk
        except httpx.HTTPError as exc:
            raise network_error(self.name, exc) from exc

    async def _stream_native(
        self,
        deployment: DeploymentConfig,
        body: dict,
        request: ChatCompletionRequest,
        provider_cfg: ProviderConfig,
        timeout: httpx.Timeout,
    ) -> AsyncIterator[dict]:
        """Provider-native Responses event stream -> chat chunks."""

        async def events() -> AsyncIterator[dict]:
            async with self.client.stream(
                "POST",
                self._responses_url(deployment, provider_cfg),
                headers=self._headers(provider_cfg),
                json=body,
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise_for_provider_status(response, self.name)
                async for line in sse_data_lines(response.aiter_bytes()):
                    if not line or line == "[DONE]":
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

        try:
            async for chunk in stream_events_to_chunks(events(), request.model):
                yield chunk
        except httpx.HTTPError as exc:
            raise network_error(self.name, exc) from exc
