"""Azure OpenAI adapter.

Azure OpenAI speaks the OpenAI wire format but differs in three ways,
all handled by overriding the URL/header hooks of the OpenAI adapter:

* **Deployments, not models** — requests address an Azure *deployment*:
  ``{api_base}/openai/deployments/{deployment}/chat/completions``. The
  gateway's ``DeploymentConfig.model`` is the Azure deployment name
  (e.g. ``gpt-4o-2024-08-06-prod``); clients keep seeing the gateway's
  public model name.
* **``api-key`` header** — Azure key auth uses the ``api-key`` header
  (the gateway also *accepts* that header from clients on the way in,
  for Azure SDKs pointed at the gateway).
* **``api-version`` query param** — required on every call. Defaults to
  a recent stable version; override per provider with
  ``providers.azure.api_version``.

Azure supports the same non-chat surfaces with deployment-addressed URLs —
images/generations, audio/transcriptions, audio/translations and
audio/speech (Whisper / gpt-4o-transcribe deployments for the audio ones) —
so they pass through this subclass unchanged too.

Streaming, tools, and embeddings pass through unchanged — Azure's
responses are OpenAI-shaped, so the parent adapter's translation,
usage accounting and error mapping apply as-is.

There is no Entra/Active Directory token auth in this version: key auth
(``api-key``) only, like the other providers.
"""

from __future__ import annotations

from urllib.parse import quote

from ..config import DeploymentConfig, ProviderConfig
from .openai_adapter import OpenAIProviderAdapter

# A recent, broadly compatible api-version (chat completions, tools,
# embeddings, stream_options.include_usage). Override with
# providers.azure.api_version when you need a specific one.
DEFAULT_API_VERSION = "2024-10-21"


class AzureProviderAdapter(OpenAIProviderAdapter):
    name = "azure"

    @staticmethod
    def _deployment_path(
        deployment: DeploymentConfig, provider_cfg: ProviderConfig, path: str
    ) -> str:
        version = provider_cfg.api_version or DEFAULT_API_VERSION
        return (
            f"{provider_cfg.api_base.rstrip('/')}/openai/deployments"
            f"/{quote(deployment.model, safe='')}/{path}?api-version={version}"
        )

    def _chat_url(self, deployment: DeploymentConfig, provider_cfg: ProviderConfig) -> str:
        return self._deployment_path(deployment, provider_cfg, "chat/completions")

    def _responses_url(self, deployment: DeploymentConfig, provider_cfg: ProviderConfig) -> str:
        # Native Responses southbound (deployment.native_responses), the
        # Azure deployment-addressed form of /responses.
        return self._deployment_path(deployment, provider_cfg, "responses")

    def _embed_url(self, deployment: DeploymentConfig, provider_cfg: ProviderConfig) -> str:
        return self._deployment_path(deployment, provider_cfg, "embeddings")

    def _images_url(self, deployment: DeploymentConfig, provider_cfg: ProviderConfig) -> str:
        return self._deployment_path(deployment, provider_cfg, "images/generations")

    def _audio_url(
        self, deployment: DeploymentConfig, provider_cfg: ProviderConfig, kind: str
    ) -> str:
        # kind: "transcriptions" | "translations"
        return self._deployment_path(deployment, provider_cfg, f"audio/{kind}")

    def _speech_url(self, deployment: DeploymentConfig, provider_cfg: ProviderConfig) -> str:
        return self._deployment_path(deployment, provider_cfg, "audio/speech")

    def _headers(self, provider_cfg: ProviderConfig) -> dict[str, str]:
        return {
            "api-key": provider_cfg.resolve_key() or "",
            "Content-Type": "application/json",
        }

    def _auth_headers(self, provider_cfg: ProviderConfig) -> dict[str, str]:
        # Multipart uploads: httpx owns the Content-Type (boundary).
        return {"api-key": provider_cfg.resolve_key() or ""}
