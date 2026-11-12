"""Unit tests for cost calculation and the rate limiter."""

from __future__ import annotations

from gateway.core.cost import CostCalculator
from gateway.core.ratelimit import RateLimiter


def test_cost_known_model_exact():
    calc = CostCalculator()
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert calc.calculate(usage, "gpt-4o") == 12.5  # 2.5 + 10.0


def test_cost_prefix_matching():
    calc = CostCalculator()
    usage = {"prompt_tokens": 500_000, "completion_tokens": 0}
    assert calc.calculate(usage, "claude-sonnet-4-5-20250929") == 1.5


def test_cost_unknown_model_is_zero():
    calc = CostCalculator()
    assert calc.calculate({"prompt_tokens": 100}, "mystery-model") == 0.0


def test_cost_config_override():
    calc = CostCalculator({"model-a": {"input": 2.0, "output": 4.0}})
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}
    assert round(calc.calculate(usage, "model-a"), 6) == 4.0


def test_cost_no_usage():
    assert CostCalculator().calculate(None, "gpt-4o") == 0.0


# ----------------------------------------------------------------------
# Rate limiter
# ----------------------------------------------------------------------


async def test_rpm_sliding_window():
    limiter = RateLimiter()
    assert (await limiter.check("k", rpm_limit=2, tpm_limit=None))[0] is True
    await limiter.record_request("k")
    await limiter.record_request("k")
    allowed, retry_after = await limiter.check("k", rpm_limit=2, tpm_limit=None)
    assert allowed is False
    assert 0 < retry_after <= 60


async def test_tpm_sliding_window():
    limiter = RateLimiter()
    await limiter.record_usage("k", 500)
    allowed, _ = await limiter.check("k", rpm_limit=None, tpm_limit=1000, estimated_tokens=600)
    assert allowed is False
    allowed, _ = await limiter.check("k", rpm_limit=None, tpm_limit=1000, estimated_tokens=400)
    assert allowed is True


async def test_no_limits_always_allows():
    limiter = RateLimiter()
    for _ in range(100):
        await limiter.record_request("k")
        await limiter.record_usage("k", 10_000)
    assert (await limiter.check("k", rpm_limit=None, tpm_limit=None))[0] is True


async def test_windows_are_per_key():
    limiter = RateLimiter()
    await limiter.record_request("k1")
    await limiter.record_request("k1")
    assert (await limiter.check("k2", rpm_limit=1, tpm_limit=None))[0] is True
