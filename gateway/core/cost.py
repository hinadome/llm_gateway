"""Cost calculation from per-1M-token pricing.

A small default pricing table is built-in; ``config.yaml`` `pricing:`
entries override/extend it. Lookup uses longest-prefix match so e.g.
``claude-sonnet-4-5-20251014`` matches ``claude-sonnet-4-5``.
"""

from __future__ import annotations

from typing import Any

# USD per 1M tokens (input, output). Approximate public list prices.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1-nano": (0.1, 0.4),
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "o3": (2.0, 8.0),
    "o4-mini": (1.1, 4.4),
    # Anthropic
    "claude-opus-4-1": (15.0, 75.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-5-haiku": (0.8, 4.0),
    # Google
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.3, 2.5),
    "gemini-2.0-flash": (0.1, 0.4),
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


class CostCalculator:
    def __init__(self, overrides: dict[str, dict[str, float]] | None = None) -> None:
        self.pricing: dict[str, tuple[float, float]] = dict(DEFAULT_PRICING)
        self.unit_pricing: dict[str, dict[str, float]] = {
            model: dict(units) for model, units in DEFAULT_UNIT_PRICING.items()
        }
        for model, prices in (overrides or {}).items():
            self.pricing[model] = (float(prices.get("input", 0.0)), float(prices.get("output", 0.0)))
            units = {k: float(v) for k, v in prices.items() if k in _UNIT_KEYS}
            if units:
                self.unit_pricing[model] = units

    def lookup(self, model: str) -> tuple[float, float] | None:
        """Longest-prefix match over the pricing table."""
        if not model:
            return None
        if model in self.pricing:
            return self.pricing[model]
        best: tuple[float, float] | None = None
        best_len = -1
        for prefix, prices in self.pricing.items():
            if model.startswith(prefix) and len(prefix) > best_len:
                best, best_len = prices, len(prefix)
        return best

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
        input_cost = usage.get("prompt_tokens", 0) / 1_000_000 * prices[0]
        output_cost = usage.get("completion_tokens", 0) / 1_000_000 * prices[1]
        return round(input_cost + output_cost, 8)

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
