"""Cost-aware routing + cached-token billing.

Two halves of one concern — what traffic costs:

* **Cached-token billing.** Providers discount prompt-cache reads
  (OpenAI ``prompt_tokens_details.cached_tokens``, Anthropic
  ``cache_read_input_tokens``, Gemini ``cachedContentTokenCount``).
  The gateway normalizes them into the OpenAI wire shape internally
  (cached tokens are a subset of ``prompt_tokens``), bills the cached
  portion at the pricing table's ``cached_input`` rate (default 50% of
  input — OpenAI's discount; set it explicitly when the provider's
  differs), and round-trips the split back out on every surface:
  OpenAI usage details, native Anthropic ``cache_read_input_tokens``,
  native Gemini ``cachedContentTokenCount``. ``gateway_tokens_total``
  gains a ``cached_input`` type so cache-heavy traffic is visible.
* **Cost-aware routing.** ``routing.cost_aware: true`` ranks
  same-priority deployments by their upstream model's INPUT price
  (the pricing table, the same lookup billing uses): cheapest first,
  the price ladder doubling as the failover ladder, weight breaking
  price ties, unpriced sorting last, priority still ruling across
  tiers.
"""

from __future__ import annotations

import json

import pytest

from gateway.config import DeploymentConfig, ModelConfig, RoutingConfig
from gateway.core.cost import CostCalculator, cached_tokens_from_usage
from gateway.observability.metrics import TOKENS
from gateway.providers.anthropic_adapter import (
    AnthropicStreamTranslator,
    translate_response as anthropic_translate_response,
)
from gateway.providers.gemini_adapter import (
    GeminiStreamTranslator,
    translate_response as gemini_translate_response,
)

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config


# -- unit: extraction + billing math ------------------------------------------


def test_cached_tokens_extraction():
    # The OpenAI wire shape (what every adapter normalizes to).
    assert cached_tokens_from_usage(
        {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 6}}
    ) == 6
    # Flat fallback for OpenAI-compatible servers that report it that way.
    assert cached_tokens_from_usage({"prompt_tokens": 10, "cached_tokens": 4}) == 4
    # A lying upstream cannot bill negative input.
    assert cached_tokens_from_usage(
        {"prompt_tokens": 3, "prompt_tokens_details": {"cached_tokens": 9}}
    ) == 3
    # Absent / malformed details.
    assert cached_tokens_from_usage({"prompt_tokens": 10}) == 0
    assert cached_tokens_from_usage(None) == 0
    assert cached_tokens_from_usage(
        {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": "x"}}
    ) == 0


def test_calculate_bills_the_cached_split():
    calc = CostCalculator({"m": {"input": 2.0, "output": 4.0}})
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 6},
    }
    # cached_input defaults to 50% of input: (10-6)*2 + 6*1 + 7*4 = 42/1M.
    assert calc.calculate(usage, "m") == pytest.approx(42e-6)
    # Explicit cached_input (e.g. Anthropic's 10%-of-input discount).
    cheap_cache = CostCalculator(
        {"m": {"input": 2.0, "output": 4.0, "cached_input": 0.2}}
    )
    # (10-6)*2 + 6*0.2 + 7*4 = 37.2/1M
    assert cheap_cache.calculate(usage, "m") == pytest.approx(37.2e-6)
    # No cached detail: unchanged math.
    assert calc.calculate({"prompt_tokens": 10, "completion_tokens": 7}, "m") == (
        pytest.approx(48e-6)
    )
    # Unpriced model still costs nothing.
    assert calc.calculate(usage, "not-a-model") == 0.0


def test_lookup_carries_cached_prices_and_prefix_match():
    calc = CostCalculator()  # built-in table
    # Longest-prefix: the dated snapshot matches claude-sonnet-4-5,
    # whose cache reads bill at 10% of base input (0.3 vs 3.0).
    assert calc.lookup("claude-sonnet-4-5-20251014") == (3.0, 15.0, 0.3)
    # Entries without an explicit cached price default to 50% of input.
    assert CostCalculator({"m": {"input": 4.0}}).lookup("m") == (4.0, 0.0, 2.0)
    # The router's ranking key.
    assert CostCalculator().input_price("gpt-4o") == 2.5
    assert CostCalculator().input_price("mystery") is None


# -- unit: adapter translation --------------------------------------------------


def test_anthropic_adapter_folds_cache_reads_into_prompt():
    native = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {
            # Anthropic reports cache traffic OUTSIDE input_tokens.
            "input_tokens": 8,
            "output_tokens": 3,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 2,
        },
    }
    out = anthropic_translate_response(native, "public")
    # prompt_tokens is the FULL input (8 + 40 + 2); cached reads are
    # the subset the details field carries.
    assert out["usage"]["prompt_tokens"] == 50
    assert out["usage"]["completion_tokens"] == 3
    assert out["usage"]["prompt_tokens_details"] == {"cached_tokens": 40}

    # Streams: message_start carries the same fold; message_stop emits
    # the trailing OpenAI-style usage chunk.
    tr = AnthropicStreamTranslator("public")
    tr.handle_event(
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 8, "cache_read_input_tokens": 40}},
        }
    )
    tr.handle_event(
        {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 3}}
    )
    final = [
        c for c in tr.handle_event({"type": "message_stop"}) if c.get("usage")
    ]
    assert final[-1]["usage"]["prompt_tokens"] == 48
    assert final[-1]["usage"]["prompt_tokens_details"] == {"cached_tokens": 40}


def test_gemini_adapter_surfaces_the_cached_split():
    native = {
        "candidates": [
            {"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {
            # Gemini's promptTokenCount already INCLUDES the cached share.
            "promptTokenCount": 20,
            "candidatesTokenCount": 5,
            "totalTokenCount": 25,
            "cachedContentTokenCount": 12,
        },
    }
    out = gemini_translate_response(native, "public")
    assert out["usage"]["prompt_tokens"] == 20
    assert out["usage"]["prompt_tokens_details"] == {"cached_tokens": 12}

    # Streams: the usage chunk is emitted alongside the chunk that
    # carries usageMetadata.
    tr = GeminiStreamTranslator("public")
    chunks = tr.handle_chunk(native)
    usage_chunks = [c for c in chunks if c.get("usage")]
    assert usage_chunks[-1]["usage"]["prompt_tokens_details"] == {"cached_tokens": 12}


# -- e2e: billing + surface round trips ----------------------------------------


async def test_cached_tokens_billed_logged_and_surfaced(tmp_path):
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "cached-caller"))["key"]
        # The upstream reports 6 of its 10 input tokens as cache reads.
        env.mock.usage_extra = {"prompt_tokens_details": {"cached_tokens": 6}}
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hi"), headers=auth(key)
        )
        assert resp.status_code == 200
        # The client sees the OpenAI wire shape, details included.
        assert resp.json()["usage"]["prompt_tokens_details"]["cached_tokens"] == 6

        # Billing: pricing is model-a {input: 2.0, output: 4.0};
        # cached_input defaults to 1.0. (10-6)*2 + 6*1 + 7*4 = 42/1M.
        row = (
            await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        ).json()["data"][0]
        assert row["cached_tokens"] == 6
        assert row["cost_usd"] == pytest.approx(42e-6)

        # The capture export (the replay harness's artifact) carries it.
        export = await env.client.get("/admin/logs/export", headers=auth(ADMIN_KEY))
        entry = json.loads(export.text.splitlines()[0])
        assert entry["cached_tokens"] == 6

        # The token counter sees the cache-read series.
        assert TOKENS.labels("cached_input")._value.get() >= 6


async def test_explicit_cached_input_price_bills_correctly(tmp_path):
    # The operator prices the provider's real discount (10% of input).
    async for env in _make_env(
        tmp_path,
        config=make_config(
            pricing={
                "model-a": {"input": 2.0, "output": 4.0, "cached_input": 0.2}
            }
        ),
    ):
        key = (await create_key(env.client, "cheap-cache"))["key"]
        env.mock.usage_extra = {"prompt_tokens_details": {"cached_tokens": 6}}
        await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hi"), headers=auth(key)
        )
        row = (
            await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        ).json()["data"][0]
        # (10-6)*2 + 6*0.2 + 7*4 = 37.2/1M
        assert row["cost_usd"] == pytest.approx(37.2e-6)


async def test_native_surfaces_round_trip_cached_tokens(tmp_path):
    async for env in _make_env(tmp_path):
        key = (await create_key(env.client, "native-caller"))["key"]
        env.mock.usage_extra = {"prompt_tokens_details": {"cached_tokens": 6}}

        # Native Anthropic: cache reads in Anthropic's own usage field.
        anthropic_resp = await env.client.post(
            "/v1/messages",
            json={
                "model": "test-model",
                "max_tokens": 50,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"x-api-key": key},
        )
        assert anthropic_resp.status_code == 200
        assert anthropic_resp.json()["usage"]["cache_read_input_tokens"] == 6
        assert anthropic_resp.json()["usage"]["input_tokens"] == 10

        # Native Gemini: cachedContentTokenCount.
        gemini_resp = await env.client.post(
            "/v1/models/test-model:generateContent",
            json={"contents": [{"parts": [{"text": "hi"}]}]},
            headers={"x-goog-api-key": key},
        )
        assert gemini_resp.status_code == 200
        meta = gemini_resp.json()["usageMetadata"]
        assert meta["cachedContentTokenCount"] == 6
        assert meta["promptTokenCount"] == 10


# -- cost-aware routing ---------------------------------------------------------


def _same_priority_models() -> list[ModelConfig]:
    """test-model served by two SAME-priority deployments (the tier
    cost-aware routing ranks); other-model for completeness."""
    return [
        ModelConfig(
            name="test-model",
            deployments=[
                DeploymentConfig(
                    name="openai/primary", provider="openai", model="model-a"
                ),
                DeploymentConfig(
                    name="openai/fallback",
                    provider="openai",
                    model="model-b",
                    priority=100,  # same tier
                ),
            ],
        ),
        ModelConfig(
            name="other-model",
            deployments=[
                DeploymentConfig(
                    name="openai/other", provider="openai", model="model-c"
                )
            ],
        ),
    ]


_PRICE_LADDER = {
    "model-a": {"input": 5.0, "output": 10.0},
    "model-b": {"input": 1.0, "output": 2.0},
}


async def _deployments_used(env, n: int) -> set[str]:
    """Send n requests; collect the deployments that served them.
    Distinct content per request — identical bodies would be served
    by the response cache (deployment None), not the router."""
    key = (await create_key(env.client, "routed"))["key"]
    used: set[str] = set()
    for i in range(n):
        resp = await env.client.post(
            "/v1/chat/completions",
            json=chat_body(content=f"hi {i}"),
            headers=auth(key),
        )
        assert resp.status_code == 200
        row = (
            await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        ).json()["data"][0]
        used.add(row["deployment"])
    return used


async def test_cost_aware_prefers_cheapest_in_tier(tmp_path):
    config = make_config(
        models=_same_priority_models(),
        pricing=_PRICE_LADDER,
        routing=RoutingConfig(cost_aware=True),
    )
    async for env in _make_env(tmp_path, config=config):
        # model-b ($1/M) beats model-a ($5/M) every time.
        assert await _deployments_used(env, 8) == {"openai/fallback"}


async def test_cost_aware_failover_walks_the_price_ladder(tmp_path):
    config = make_config(
        models=_same_priority_models(),
        pricing=_PRICE_LADDER,
        routing=RoutingConfig(cost_aware=True),
    )
    async for env in _make_env(tmp_path, config=config):
        key = (await create_key(env.client, "routed"))["key"]
        env.mock.fail_counts["model-b"] = 3  # exhausts the cheap one
        resp = await env.client.post(
            "/v1/chat/completions", json=chat_body(content="hi"), headers=auth(key)
        )
        assert resp.status_code == 200
        row = (
            await env.client.get("/admin/logs", headers=auth(ADMIN_KEY))
        ).json()["data"][0]
        # The price ladder doubles as the failover ladder.
        assert row["deployment"] == "openai/primary"


async def test_cost_aware_unpriced_sorts_last(tmp_path):
    # Only model-b carries a price; the unpriced model-a gets no
    # discount and sorts last.
    config = make_config(
        models=_same_priority_models(),
        pricing={"model-b": {"input": 1.0, "output": 2.0}},
        routing=RoutingConfig(cost_aware=True),
    )
    async for env in _make_env(tmp_path, config=config):
        assert await _deployments_used(env, 8) == {"openai/fallback"}


async def test_cost_aware_equal_prices_still_load_balance(tmp_path):
    # Equal prices: weight breaks the tie — both deployments see traffic.
    config = make_config(
        models=_same_priority_models(),
        pricing={"model-a": {"input": 2.0}, "model-b": {"input": 2.0}},
        routing=RoutingConfig(cost_aware=True),
    )
    async for env in _make_env(tmp_path, config=config):
        assert await _deployments_used(env, 20) == {
            "openai/primary",
            "openai/fallback",
        }


async def test_cost_aware_off_keeps_weighted_random(tmp_path):
    # Default: cost_aware off — plain weighted-random across the tier.
    config = make_config(models=_same_priority_models(), pricing=_PRICE_LADDER)
    async for env in _make_env(tmp_path, config=config):
        assert await _deployments_used(env, 20) == {
            "openai/primary",
            "openai/fallback",
        }


async def test_cost_aware_never_outranks_priority(tmp_path):
    # The cheap deployment sits in a LOWER tier (priority 200): the
    # operator's explicit tier order wins over price.
    models = [
        ModelConfig(
            name="test-model",
            deployments=[
                DeploymentConfig(
                    name="openai/primary", provider="openai", model="model-a"
                ),
                DeploymentConfig(
                    name="openai/fallback",
                    provider="openai",
                    model="model-b",
                    priority=200,
                ),
            ],
        ),
    ]
    config = make_config(
        models=models,
        pricing=_PRICE_LADDER,
        routing=RoutingConfig(cost_aware=True),
    )
    async for env in _make_env(tmp_path, config=config):
        assert await _deployments_used(env, 8) == {"openai/primary"}


async def test_admin_routing_echoes_cost_awareness_and_prices(tmp_path):
    config = make_config(
        models=_same_priority_models(),
        pricing=_PRICE_LADDER,
        routing=RoutingConfig(cost_aware=True),
    )
    async for env in _make_env(tmp_path, config=config):
        body = (
            await env.client.get("/admin/routing", headers=auth(ADMIN_KEY))
        ).json()
        assert body["cost_aware"] is True
        models = {m["name"]: m for m in body["models"]}
        tier = {d["name"]: d for d in models["test-model"]["deployments"]}
        # Prices looked up on the upstream model; the cached price is
        # the default 50% of input when the entry sets none.
        assert tier["openai/primary"]["pricing"] == {
            "input_usd_per_mtok": 5.0,
            "output_usd_per_mtok": 10.0,
            "cached_input_usd_per_mtok": 2.5,
        }
        assert tier["openai/fallback"]["pricing"] == {
            "input_usd_per_mtok": 1.0,
            "output_usd_per_mtok": 2.0,
            "cached_input_usd_per_mtok": 0.5,
        }
        # Unpriced model-c: no pricing block (sorts last when on).
        assert models["other-model"]["deployments"][0]["pricing"] is None
