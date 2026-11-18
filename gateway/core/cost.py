"""Cost calculation from per-1M-token pricing.

A small default pricing table is built-in; ``config.yaml`` `pricing:`
entries override/extend it. Lookup uses longest-prefix match so e.g.
``claude-sonnet-4-5-20251014`` matches ``claude-sonnet-4-5``.

Each entry prices three token streams — input, output and *cached*
input (provider-side prompt caching: OpenAI's
``prompt_tokens_details.cached_tokens``, Anthropic's
``cache_read_input_tokens``, Gemini's ``cachedContentTokenCount``).
Cached tokens are a subset of ``prompt_tokens`` and bill at their own
discounted rate. An entry without an explicit cached price defaults to
50% of the input price (OpenAI's convention); providers with deeper
discounts (Anthropic's cache reads are 10% of base input) should set
``cached_input`` explicitly — the built-in table does.
"""

from __future__ import annotations

from typing import Any

# USD per 1M tokens (input, output, cached_input). Approximate public
# list prices; the cached column is the provider's own prompt-cache read
# price (OpenAI ~25-50% of input depending on model, Anthropic 10%,
# Gemini ~25%) — None falls back to 50% of input.
DEFAULT_PRICING: dict[str, tuple[float, float, float | None]] = {
    # OpenAI
    "gpt-4o": (2.5, 10.0, 1.25),
    "gpt-4o-mini": (0.15, 0.6, 0.075),
    "gpt-4.1": (2.0, 8.0, 0.5),
    "gpt-4.1-mini": (0.4, 1.6, 0.1),
    "gpt-4.1-nano": (0.1, 0.4, 0.025),
    "gpt-5": (1.25, 10.0, 0.125),
    "gpt-5-mini": (0.25, 2.0, 0.025),
    "o3": (2.0, 8.0, 0.5),
    "o4-mini": (1.1, 4.4, 0.275),
    # Anthropic
    "claude-opus-4-1": (15.0, 75.0, 1.5),
    "claude-sonnet-4-5": (3.0, 15.0, 0.3),
    "claude-haiku-4-5": (1.0, 5.0, 0.1),
    "claude-3-5-sonnet": (3.0, 15.0, 0.3),
    "claude-3-5-haiku": (0.8, 4.0, 0.08),
    # Google
    "gemini-2.5-pro": (1.25, 10.0, 0.31),
    "gemini-2.5-flash": (0.3, 2.5, 0.075),
    "gemini-2.0-flash": (0.1, 0.4, 0.025),
}

# USD unit prices for non-token modalities (approximate public list prices;
# config.yaml `pricing:` entries override/extend with the same keys):
#   image_usd               — per generated image
#   audio_usd_per_minute    — per minute of transcribed audio input
#   speech_usd_per_1m_chars — per 1M input characters for text-to-speech
DEFAULT_UNIT_PRICING: dict[str, dict[str, float]] = {
    "gpt-image-1": {"image_usd": 0.04},
    "dall-e-3": {"image_usd": 0.04},
    "dall-e-2": {"image_usd": 0.016},
    "whisper-1": {"audio_usd_per_minute": 0.006},
    "gpt-4o-transcribe": {"audio_usd_per_minute": 0.006},
    "gpt-4o-mini-transcribe": {"audio_usd_per_minute": 0.003},
    "tts-1": {"speech_usd_per_1m_chars": 15.0},
    "tts-1-hd": {"speech_usd_per_1m_chars": 30.0},
    "gpt-4o-mini-tts": {"speech_usd_per_1m_chars": 0.15},
}

_UNIT_KEYS = ("image_usd", "audio_usd_per_minute", "speech_usd_per_1m_chars")

# The cached-input default when an entry (built-in or configured) gives
# no explicit cached price: 50% of the input price — OpenAI's discount.
# Set `cached_input` explicitly when the provider's differs.
_DEFAULT_CACHED_FRACTION = 0.5


def cached_tokens_from_usage(usage: dict[str, Any] | None) -> int:
    """Cached *input* tokens reported by the upstream, as the gateway's
    one internal contract: an int, a subset of ``prompt_tokens``.

    Reads the OpenAI wire shape (``usage.prompt_tokens_details.
    cached_tokens`` — the shape every adapter normalizes to), with a
    flat ``cached_tokens`` fallback for OpenAI-compatible servers
    that report it that way, clamped to ``prompt_tokens`` so a lying
    upstream cannot bill negative input.
    """
    if not isinstance(usage, dict):
        return 0
    details = usage.get("prompt_tokens_details")
    cached = (
        details.get("cached_tokens", 0) if isinstance(details, dict) else 0
    ) or usage.get("cached_tokens") or 0
    try:
        cached = int(cached)
    except (TypeError, ValueError):
        return 0
    prompt = usage.get("prompt_tokens") or 0
    if cached > prompt:
        return max(0, int(prompt))
    return max(0, cached)


class CostCalculator:
    def __init__(self, overrides: dict[str, dict[str, float]] | None = None) -> None:
        self.pricing: dict[str, tuple[float, float, float | None]] = dict(DEFAULT_PRICING)
        self.unit_pricing: dict[str, dict[str, float]] = {
            model: dict(units) for model, units in DEFAULT_UNIT_PRICING.items()
        }
        for model, prices in (overrides or {}).items():
            input_price = float(prices.get("input", 0.0))
            self.pricing[model] = (
                input_price,
                float(prices.get("output", 0.0)),
                (
                    float(prices["cached_input"])
                    if "cached_input" in prices
                    else None
                ),
            )
            units = {k: float(v) for k, v in prices.items() if k in _UNIT_KEYS}
            if units:
                self.unit_pricing[model] = units

    def lookup(self, model: str) -> tuple[float, float, float] | None:
        """Longest-prefix match over the pricing table. Returns
        (input, output, cached_input) USD per 1M tokens; the cached
        price defaults to 50% of input when the entry sets none."""
        if not model:
            return None
        if model in self.pricing:
            entry = self.pricing[model]
            return (entry[0], entry[1], entry[2] if entry[2] is not None else entry[0] * _DEFAULT_CACHED_FRACTION)
        best: tuple[float, float, float] | None = None
        best_len = -1
        for prefix, entry in self.pricing.items():
            if model.startswith(prefix) and len(prefix) > best_len:
                best = (
                    entry[0],
                    entry[1],
                    entry[2] if entry[2] is not None else entry[0] * _DEFAULT_CACHED_FRACTION,
                )
                best_len = len(prefix)
        return best

    def input_price(self, model: str) -> float | None:
        """Input USD/1M for a model, or None when unpriced — the
        cost-aware router's ranking key (input price is the dominant
        per-request cost; cached discounts are upstream state the
        router cannot see pre-dispatch)."""
        prices = self.lookup(model)
        return prices[0] if prices is not None else None

    def lookup_units(self, model: str, key: str) -> float | None:
        """Longest-prefix match over unit prices for one unit key."""
        if not model:
            return None
        best: float | None = None
        best_len = -1
        for prefix, units in self.unit_pricing.items():
            if model.startswith(prefix) and len(prefix) > best_len and key in units:
                best, best_len = units[key], len(prefix)
        return best

    def calculate(self, usage: dict[str, Any] | None, model: str) -> float:
        if not usage:
            return 0.0
        prices = self.lookup(model)
        if prices is None:
            return 0.0
        prompt = usage.get("prompt_tokens", 0) or 0
        cached = cached_tokens_from_usage(usage)
        # Cached tokens are a subset of prompt_tokens (the internal
        # contract): the uncached remainder bills at full input price,
        # the cached portion at the provider's cache-read price.
        input_cost = (prompt - cached) / 1_000_000 * prices[0]
        cached_cost = cached / 1_000_000 * prices[2]
        output_cost = usage.get("completion_tokens", 0) / 1_000_000 * prices[1]
        return round(input_cost + cached_cost + output_cost, 8)

    # -- non-token modalities -------------------------------------------------

    def calculate_images(self, model: str, images: int) -> float:
        """Image generation: flat per-image price (size/quality-aware
        pricing can be configured per exact model name)."""
        per_image = self.lookup_units(model, "image_usd")
        if per_image is None:
            return 0.0
        return round(max(0, images) * per_image, 8)

    def calculate_audio(self, model: str, minutes: float) -> float:
        """Transcription/translation: per-minute price over the audio
        duration (from the provider's response when it reports one)."""
        per_minute = self.lookup_units(model, "audio_usd_per_minute")
        if per_minute is None:
            return 0.0
        return round(max(0.0, minutes) * per_minute, 8)

    def calculate_speech(self, model: str, characters: int) -> float:
        """Text-to-speech: per-1M-input-characters price."""
        per_1m = self.lookup_units(model, "speech_usd_per_1m_chars")
        if per_1m is None:
            return 0.0
        return round(max(0, characters) / 1_000_000 * per_1m, 8)
