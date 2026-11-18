"""Prompt compression: middle-out folding to fit a token budget.

The remediation counterpart of context_window: where that guardrail
blocks (or redirects) an over-window request, ``prompt_compression``
deterministically compresses it to fit — no LLM call, no added latency.
Middle-out folding: leading system messages are always kept, the first
``keep_first`` and last ``keep_last`` non-system messages are kept, the
middle is dropped with a marker note prepended to the first kept
message (content-only — roles and content shapes preserved, so every
adapter handles it). If still over, kept messages are truncated
oldest-first toward ``min_message_chars`` — never the system block,
never the last message. When even that cannot reach the target (a
gigantic last message), the guardrail steps aside: it never blocks;
context_window (ordered after it) or the provider gives the honest
error.

Target: ``max_input_tokens`` when set, else the model's declared
window minus ``reserve_output_tokens`` (the same vocabulary
context_window uses, so compress-then-check composes exactly).
"""

from __future__ import annotations

import pytest

from prometheus_client import REGISTRY

from gateway.config import (
    CacheConfig,
    DeploymentConfig,
    GuardrailConfig,
    GuardrailScope,
    ModelConfig,
)
from gateway.core.context import AuthContext, RequestContext
from gateway.guardrails.builtins.prompt_compression import (
    PromptCompressionGuardrail,
)
from gateway.schemas import ChatCompletionRequest

from .conftest import ADMIN_KEY, _make_env, auth, create_key, make_config


def _models():
    return [
        # Window-declared model: window-fit mode targets 4000-1024=2976
        # input tokens (the same reserve context_window assumes).
        ModelConfig(
            name="windowed",
            context_window=4000,
            deployments=[
                DeploymentConfig(name="openai/w", provider="openai", model="model-a")
            ],
        ),
        # Big window: only an explicit max_input_tokens manages it.
        ModelConfig(
            name="big-model",
            context_window=1_000_000,
            deployments=[
                DeploymentConfig(name="openai/big", provider="openai", model="model-b")
            ],
        ),
        # No declared window and no explicit budget: unmanaged.
        ModelConfig(
            name="unmanaged",
            deployments=[
                DeploymentConfig(name="openai/u", provider="openai", model="model-c")
            ],
        ),
    ]


def _config(params=None, guardrails=None, cache=False, **guardrail_extra):
    effective = list(guardrails) if guardrails is not None else [
        GuardrailConfig(
            name="prompt_compression", enabled=True, params=params or {}, **guardrail_extra
        ),
    ]
    return make_config(
        models=_models(),
        cache=CacheConfig(enabled=cache),
        guardrails=effective,
    )


def _filler(text: str, tokens: int) -> dict:
    """A message of ~`tokens` estimated tokens (~4 chars/token)."""
    return {"role": "user", "content": f"{text}: " + "x" * (tokens * 4)}


def _conversation(n_fillers: int, filler_tokens: int = 400) -> list[dict]:
    """system + task + N fillers + final — the middle-out shape."""
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Summarize the following documents."},
        *(_filler(f"doc {i}", filler_tokens) for i in range(1, n_fillers + 1)),
        {"role": "user", "content": "Now give me the summary."},
    ]


async def _chat(env, key: str, model: str, messages: list[dict], **extra):
    return await env.client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": messages, **extra},
        headers=auth(key),
    )


def _upstream_messages(env, index: int = -1) -> list[dict]:
    bodies = [b for b in env.mock.bodies() if isinstance(b, dict) and "messages" in b]
    return bodies[index]["messages"]


def _actions(log_row: dict, guardrail: str) -> list[dict]:
    return [
        a for a in (log_row["guardrail_actions"] or [])
        if a["guardrail"] == guardrail
    ]


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


async def _last_log(client) -> dict:
    logs = (await client.get("/admin/logs", headers=auth(ADMIN_KEY))).json()["data"]
    return logs[0]


# ----------------------------------------------------------------------
# Middle-out folding: the core contract
# ----------------------------------------------------------------------


async def test_fold_drops_middle_keeps_ends(tmp_path):
    # 10 fillers with defaults keep_first=2/keep_last=4: the middle 6
    # drop, a note lands on the first kept message, upstream serves.
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "writer"))["key"]
        messages = _conversation(10, filler_tokens=400)

        resp = await _chat(env, key, "windowed", messages)
        assert resp.status_code == 200, resp.text

        sent = _upstream_messages(env)
        # system + task + doc1 (keep_first=2) + docs 8..10 + final
        # (keep_last=4) = 7 messages; docs 2..7 are gone.
        assert len(sent) == 7
        assert sent[0]["role"] == "system"
        assert sent[0]["content"] == "You are a helpful assistant."
        assert "Summarize the following documents." in sent[1]["content"]
        assert "gateway: 6 earlier messages" in sent[1]["content"]
        assert not any("doc 2:" in str(m["content"]) for m in sent)
        assert not any("doc 7:" in str(m["content"]) for m in sent)
        assert "doc 8" in sent[3]["content"]
        assert sent[-1]["content"] == "Now give me the summary."

        row = await _last_log(env.client)
        (verdict,) = _actions(row, "prompt_compression")
        assert verdict["action"] == "modify"
        assert verdict["details"]["messages_dropped"] == 6
        assert verdict["details"]["messages_truncated"] == 0
        assert verdict["details"]["tokens_saved"] == (
            verdict["details"]["tokens_before"] - verdict["details"]["tokens_after"]
        )
        assert verdict["details"]["tokens_after"] <= verdict["details"]["target"]


async def test_fitting_request_completely_untouched(tmp_path):
    # Under the target: no compression verdict at all, messages
    # byte-identical upstream (compression is not a no-op tax).
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "small"))["key"]
        messages = _conversation(2, filler_tokens=100)

        resp = await _chat(env, key, "windowed", messages)
        assert resp.status_code == 200
        assert _actions((await _last_log(env.client)), "prompt_compression") == []
        assert _upstream_messages(env) == messages


async def test_explicit_budget_manages_big_window_model(tmp_path):
    # max_input_tokens is an explicit budget — cost control even on a
    # model whose window would never block.
    async for env in _make_env(
        tmp_path, config=_config(params={"max_input_tokens": 1000})
    ):
        key = (await create_key(env.client, "saver"))["key"]
        resp = await _chat(env, key, "big-model", _conversation(10))
        assert resp.status_code == 200

        (verdict,) = _actions((await _last_log(env.client)), "prompt_compression")
        assert verdict["details"]["target"] == 1000
        assert verdict["details"]["tokens_after"] <= 1000
        assert len(_upstream_messages(env)) == 7  # folded


async def test_unmanaged_model_passes_through(tmp_path):
    # No max_input_tokens, model declares no window: unmanaged.
    async for env in _make_env(tmp_path, config=_config()):
        key = (await create_key(env.client, "free"))["key"]
        messages = _conversation(20, filler_tokens=1000)
        resp = await _chat(env, key, "unmanaged", messages)
        assert resp.status_code == 200
        assert _actions((await _last_log(env.client)), "prompt_compression") == []
        assert len(_upstream_messages(env)) == len(messages)  # untouched


# ----------------------------------------------------------------------
# Truncation and the step-aside
# ----------------------------------------------------------------------


async def test_oldest_first_truncation_when_fold_cannot(tmp_path):
    # Three messages, no foldable middle; the giant OLDEST message is
    # truncated toward min_message_chars, system and the LAST message
    # stay byte-identical, and the request serves.
    async for env in _make_env(
        tmp_path, config=_config(params={"max_input_tokens": 500})
    ):
        key = (await create_key(env.client, "trunc"))["key"]
        messages = [
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "A" * 20000},  # ~5000 tokens
            {"role": "user", "content": "B" * 800},  # ~200 tokens
            {"role": "user", "content": "the latest turn, never touched"},
        ]
        resp = await _chat(env, key, "big-model", messages)
        assert resp.status_code == 200, resp.text

        (verdict,) = _actions((await _last_log(env.client)), "prompt_compression")
        assert verdict["details"]["messages_dropped"] == 0
        assert verdict["details"]["messages_truncated"] == 1
        assert verdict["details"]["tokens_after"] <= 500

        sent = _upstream_messages(env)
        assert sent[0]["content"] == "policy"  # system: never trimmed
        assert sent[1]["content"].startswith("A" * 512)  # trimmed to the floor
        assert "truncated" in sent[1]["content"]
        assert sent[2]["content"] == "B" * 800
        assert sent[3]["content"] == "the latest turn, never touched"


async def test_gigantic_last_message_steps_aside(tmp_path):
    # A single gigantic LAST message cannot be saved (the latest turn is
    # sacred): compression does nothing — never blocks — and the
    # request flows on untouched.
    async for env in _make_env(
        tmp_path, config=_config(params={"max_input_tokens": 500})
    ):
        key = (await create_key(env.client, "hopeless"))["key"]
        giant = [{"role": "user", "content": "Y" * 40000}]
        resp = await _chat(env, key, "big-model", giant)
        assert resp.status_code == 200
        assert _actions((await _last_log(env.client)), "prompt_compression") == []
        assert _upstream_messages(env) == giant


# ----------------------------------------------------------------------
# The pairing: compression saves what context_window would block
# ----------------------------------------------------------------------


async def test_compression_saves_request_window_would_block(tmp_path):
    # The tier story: same conversation, context_window ordered after
    # prompt_compression. With compression enabled the fold brings the
    # request under the window and it SERVES; disabled, the identical
    # conversation 400s.
    both = [
        GuardrailConfig(name="prompt_compression", enabled=True),
        GuardrailConfig(name="context_window", enabled=True),
    ]
    messages = _conversation(8, filler_tokens=400)  # ~3203 tokens > 2976 target

    async for env in _make_env(tmp_path, config=_config(guardrails=both)):
        key = (await create_key(env.client, "saved"))["key"]
        resp = await _chat(env, key, "windowed", messages)
        assert resp.status_code == 200, resp.text
        # The fold note names the gap so the model knows history is
        # missing.
        assert "gateway: " in _upstream_messages(env)[1]["content"]

    disabled = [
        GuardrailConfig(name="prompt_compression", enabled=False),
        GuardrailConfig(name="context_window", enabled=True),
    ]
    async for env in _make_env(tmp_path, config=_config(guardrails=disabled)):
        key = (await create_key(env.client, "blocked"))["key"]
        resp = await _chat(env, key, "windowed", messages)
        assert resp.status_code == 400
        assert "maximum context length" in resp.json()["error"]["message"]


# ----------------------------------------------------------------------
# Framework composition: observe mode, scoping, cache identity, metrics
# ----------------------------------------------------------------------


async def test_observe_mode_logs_without_touching(tmp_path):
    async for env in _make_env(
        tmp_path,
        config=_config(
            params={"max_input_tokens": 1000},
            mode="observe",
        ),
    ):
        key = (await create_key(env.client, "watcher"))["key"]
        messages = _conversation(10)
        observed_before = _sample(
            "gateway_guardrail_observed_total",
            {"guardrail": "prompt_compression", "phase": "request", "action": "modify"},
        )
        saved_before = _sample(
            "gateway_prompt_compression_saved_tokens_total", {"model": "windowed"}
        )

        resp = await _chat(env, key, "windowed", messages)
        assert resp.status_code == 200
        # The request went upstream UNTOUCHED — observe never enforces.
        assert _upstream_messages(env) == messages

        (verdict,) = _actions((await _last_log(env.client)), "prompt_compression")
        assert verdict["mode"] == "observe"
        assert verdict["action"] == "modify"
        assert verdict["details"]["tokens_saved"] > 0
        assert _sample(
            "gateway_guardrail_observed_total",
            {"guardrail": "prompt_compression", "phase": "request", "action": "modify"},
        ) == observed_before + 1
        # Observe-mode savings never count: they were never enforced.
        assert _sample(
            "gateway_prompt_compression_saved_tokens_total", {"model": "windowed"}
        ) == saved_before


async def test_enforced_savings_counted_on_the_metric(tmp_path):
    async for env in _make_env(
        tmp_path, config=_config(params={"max_input_tokens": 1000})
    ):
        key = (await create_key(env.client, "counter"))["key"]
        before = _sample(
            "gateway_prompt_compression_saved_tokens_total", {"model": "windowed"}
        )
        resp = await _chat(env, key, "windowed", _conversation(10))
        assert resp.status_code == 200
        (verdict,) = _actions((await _last_log(env.client)), "prompt_compression")
        assert _sample(
            "gateway_prompt_compression_saved_tokens_total", {"model": "windowed"}
        ) == before + verdict["details"]["tokens_saved"]


async def test_per_key_runtime_disable(tmp_path):
    # disabled_guardrails on the key row: off without a restart,
    # exactly like every other guardrail.
    async for env in _make_env(
        tmp_path, config=_config(params={"max_input_tokens": 1000})
    ):
        plain = (await create_key(env.client, "plain"))["key"]
        opted_out = (
            await create_key(
                env.client, "opted-out", disabled_guardrails=["prompt_compression"]
            )
        )["key"]
        messages = _conversation(10)

        assert (await _chat(env, plain, "windowed", messages)).status_code == 200
        assert len(_actions((await _last_log(env.client)), "prompt_compression")) == 1

        assert (await _chat(env, opted_out, "windowed", messages)).status_code == 200
        assert _actions((await _last_log(env.client)), "prompt_compression") == []
        # The out-key request went upstream in full.
        assert _upstream_messages(env) == messages


async def test_scoped_guardrail_cache_identity(tmp_path):
    # Guardrail modifications run BEFORE the cache lookup, so the
    # compressed and uncompressed forms of one logical prompt hold
    # DIFFERENT entries — different wires, different entries.
    scoped = GuardrailConfig(
        name="prompt_compression",
        enabled=True,
        params={"max_input_tokens": 1000},
        scope=GuardrailScope(keys=["in-scope"]),
    )
    async for env in _make_env(tmp_path, config=_config(guardrails=[scoped], cache=True)):
        in_scope = (await create_key(env.client, "in-scope"))["key"]
        out_of_scope = (await create_key(env.client, "out-of-scope"))["key"]
        messages = _conversation(10)

        first = await _chat(env, in_scope, "windowed", messages)
        assert first.headers["x-gateway-cache"] == "MISS"
        second = await _chat(env, out_of_scope, "windowed", messages)
        # NOT the compressed entry — the uncompressed form is its own
        # cache identity.
        assert second.headers["x-gateway-cache"] == "MISS"
        # Each form replays from its OWN entry.
        assert (
            await _chat(env, in_scope, "windowed", messages)
        ).headers["x-gateway-cache"] == "HIT"
        assert (
            await _chat(env, out_of_scope, "windowed", messages)
        ).headers["x-gateway-cache"] == "HIT"


async def test_content_parts_shapes_preserved(tmp_path):
    # The fold note becomes a leading TEXT part; the image part rides
    # along untouched — no structural games, every adapter can serve it.
    async for env in _make_env(
        tmp_path, config=_config(params={"max_input_tokens": 1000})
    ):
        key = (await create_key(env.client, "multimodal"))["key"]
        messages = [
            {"role": "system", "content": "policy"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                ],
            },
            *(_filler(f"doc {i}", 400) for i in range(1, 9)),
            {"role": "user", "content": "final"},
        ]
        resp = await _chat(env, key, "big-model", messages)
        assert resp.status_code == 200, resp.text

        sent = _upstream_messages(env)
        parts = sent[1]["content"]
        assert isinstance(parts, list) and len(parts) == 3
        assert parts[0]["type"] == "text" and "gateway: " in parts[0]["text"]
        assert parts[1] == {"type": "text", "text": "describe this"}
        assert parts[2]["type"] == "image_url"


# ----------------------------------------------------------------------
# Unit-level: dry-run counting, parameter validation
# ----------------------------------------------------------------------


async def test_dry_run_replay_never_counts(tmp_path):
    # The replay harness sets ctx.dry_run: verdicts are recorded but
    # enforcement counters stay untouched (the GUARDRAIL_BLOCKS
    # discipline, applied to savings).
    guardrail = PromptCompressionGuardrail({"max_input_tokens": 800})
    ctx = RequestContext(
        request_id="r", auth=AuthContext(), model="windowed", stream=False
    )
    ctx.dry_run = True
    before = _sample(
        "gateway_prompt_compression_saved_tokens_total", {"model": "windowed"}
    )
    request = ChatCompletionRequest(
        model="windowed", messages=[m for m in _conversation(10)]
    )
    result = await guardrail.check_request(request, ctx)
    assert result is not None and result.action.value == "modify"
    assert _sample(
        "gateway_prompt_compression_saved_tokens_total", {"model": "windowed"}
    ) == before


@pytest.mark.parametrize(
    "params",
    [
        {"max_input_tokens": 0},
        {"keep_last": 0},
        {"keep_first": -1},
        {"min_message_chars": 0},
        {"reserve_output_tokens": -1},
    ],
)
def test_invalid_params_fail_loudly(params):
    # Config-load failure, same contract as context_window's on_exceed
    # validation: a nonsensical policy never silently runs.
    with pytest.raises(ValueError):
        PromptCompressionGuardrail(params)
