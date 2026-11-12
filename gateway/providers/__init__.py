"""Provider adapter registry."""

from __future__ import annotations

import httpx

from ..config import GatewayConfig
from .anthropic_adapter import AnthropicProviderAdapter
from .azure_adapter import AzureProviderAdapter
from .base import ProviderAdapter
from .gemini_adapter import GeminiProviderAdapter
from .openai_adapter import OpenAIProviderAdapter

BUILTIN_ADAPTERS: dict[str, type[ProviderAdapter]] = {
    OpenAIProviderAdapter.name: OpenAIProviderAdapter,
    AnthropicProviderAdapter.name: AnthropicProviderAdapter,
    GeminiProviderAdapter.name: GeminiProviderAdapter,
    AzureProviderAdapter.name: AzureProviderAdapter,
}


def build_providers(
    config: GatewayConfig, client: httpx.AsyncClient
) -> dict[str, ProviderAdapter]:
    adapters: dict[str, ProviderAdapter] = {}
    for provider_name in config.providers:
        adapter_cls = BUILTIN_ADAPTERS.get(provider_name)
        if adapter_cls is None:
            raise ValueError(
                f"Unknown provider '{provider_name}'. "
                f"Built-ins: {sorted(BUILTIN_ADAPTERS)}"
            )
        adapters[provider_name] = adapter_cls(client)
    return adapters


__all__ = [
    "BUILTIN_ADAPTERS",
    "build_providers",
    "ProviderAdapter",
]
