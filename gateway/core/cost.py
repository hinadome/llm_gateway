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


class CostCalculator:
    def __init__(self, overrides: dict[str, dict[str, float]] | None = None) -> None:
        self.pricing: dict[str, tuple[float, float]] = dict(DEFAULT_PRICING)
        for model, prices in (overrides or {}).items():
            self.pricing[model] = (float(prices.get("input", 0.0)), float(prices.get("output", 0.0)))

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

    def calculate(self, usage: dict[str, Any] | None, model: str) -> float:
        if not usage:
            return 0.0
        prices = self.lookup(model)
        if prices is None:
            return 0.0
        input_cost = usage.get("prompt_tokens", 0) / 1_000_000 * prices[0]
        output_cost = usage.get("completion_tokens", 0) / 1_000_000 * prices[1]
        return round(input_cost + output_cost, 8)
