"""Tests for the presidio_pii guardrail (optional 'guardrails' extra).

The NLP engine loads a ~425 MB spacy model on first construction, so the
heavy guardrails are module-scoped; later constructions hit the module-level
engine cache and are cheap.
"""

from __future__ import annotations

import pytest

from gateway.config import GuardrailConfig
from gateway.core.context import AuthContext, RequestContext
from gateway.guardrails.base import Guardrail
from gateway.guardrails.builtins import BUILTIN_GUARDRAILS
from gateway.guardrails.builtins.presidio_pii import PresidioPIIGuardrail
from gateway.schemas import ChatCompletionRequest

from .conftest import ADMIN_KEY, _make_env, auth, chat_body, create_key, make_config

PII_TEXT = (
    "My SSN is 262-39-8378, email jane.doe@example.com, "
    "card 4111 1111 1111 1111."
)


def make_request(content) -> ChatCompletionRequest:
    if isinstance(content, str):
        messages = [{"role": "user", "content": content}]
    else:
        messages = content
    return ChatCompletionRequest(model="test-model", messages=messages)


def make_ctx() -> RequestContext:
    return RequestContext(request_id="req_test", auth=AuthContext())


@pytest.fixture(scope="module")
def nlp_guardrail() -> PresidioPIIGuardrail:
    return PresidioPIIGuardrail({})


@pytest.fixture(scope="module")
def pattern_guardrail() -> PresidioPIIGuardrail:
    return PresidioPIIGuardrail({"use_nlp": False})


# ----------------------------------------------------------------------
# Registration / validation
# ----------------------------------------------------------------------


def test_registered_in_builtins():
    assert BUILTIN_GUARDRAILS.get("presidio_pii") is PresidioPIIGuardrail
    # Lazy import: the module must be importable without presidio installed
    # (only constructing the guardrail requires it).
    assert issubclass(PresidioPIIGuardrail, Guardrail)


def test_unknown_entity_raises():
    with pytest.raises(ValueError, match="unknown entities"):
        PresidioPIIGuardrail({"entities": ["NOT_A_THING"]})


def test_block_entities_must_be_subset():
    with pytest.raises(ValueError, match="subset"):
        PresidioPIIGuardrail({"block_entities": ["URL"]})


def test_pattern_only_explicit_ner_entity_raises():
    # PERSON needs the NLP engine; an explicit request must fail loudly.
    with pytest.raises(ValueError, match="PERSON"):
        PresidioPIIGuardrail({"use_nlp": False, "entities": ["PERSON"]})


# ----------------------------------------------------------------------
# Request-phase redaction (NLP engine)
# ----------------------------------------------------------------------


async def test_redacts_default_entities(nlp_guardrail):
    result = await nlp_guardrail.check_request(make_request(PII_TEXT), make_ctx())
    assert result is not None
    assert result.action.value == "modify"
    redacted = result.modified_messages[0].content
    assert "262-39-8378" not in redacted
    assert "jane.doe@example.com" not in redacted
    assert "4111 1111 1111 1111" not in redacted
    assert "<US_SSN>" in redacted
    assert "<EMAIL_ADDRESS>" in redacted
    assert "<CREDIT_CARD>" in redacted
    counts = result.details["counts"]
    assert counts["US_SSN"] == 1
    assert counts["EMAIL_ADDRESS"] == 1
    assert counts["CREDIT_CARD"] == 1


async def test_clean_text_is_noop(nlp_guardrail):
    # No proper nouns: PERSON/LOCATION are in the default entity set.
    result = await nlp_guardrail.check_request(
        make_request("what is the weather like today, generally?"), make_ctx()
    )
    assert result is None


async def test_custom_placeholder(nlp_guardrail):
    guardrail = PresidioPIIGuardrail({"placeholder": "[REDACTED:{ENTITY}]"})
    result = await guardrail.check_request(
        make_request("email jane.doe@example.com"), make_ctx()
    )
    assert "[REDACTED:EMAIL_ADDRESS]" in result.modified_messages[0].content


async def test_content_parts_list_redacted(nlp_guardrail):
    request = make_request(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "my email is jane.doe@example.com"},
                    {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                ],
            }
        ]
    )
    result = await nlp_guardrail.check_request(request, make_ctx())
    parts = result.modified_messages[0].content
    assert parts[0]["text"] == "my email is <EMAIL_ADDRESS>"
    # Non-text parts pass through untouched.
    assert parts[1] == {"type": "image_url", "image_url": {"url": "http://x/y.png"}}


# ----------------------------------------------------------------------
# Block entities
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def card_blocker() -> PresidioPIIGuardrail:
    return PresidioPIIGuardrail({"block_entities": ["CREDIT_CARD"]})


async def test_block_entities_blocks_request(card_blocker):
    result = await card_blocker.check_request(make_request(PII_TEXT), make_ctx())
    assert result.action.value == "block"
    assert result.details["entities"] == ["CREDIT_CARD"]


async def test_block_entities_absent_pii_flows(card_blocker):
    result = await card_blocker.check_request(
        make_request("just a normal question about weather"), make_ctx()
    )
    assert result is None


# ----------------------------------------------------------------------
# Response masking
# ----------------------------------------------------------------------


async def test_mask_in_response(nlp_guardrail):
    guardrail = PresidioPIIGuardrail({"mask_in_response": True})
    response = {
        "choices": [
            {"message": {"role": "assistant", "content": "Sure, my email is bot@example.com."}}
        ]
    }
    result = await guardrail.check_response(make_request("q"), response, make_ctx())
    assert result is not None
    assert result.action.value == "modify"
    content = result.modified_response["choices"][0]["message"]["content"]
    assert "bot@example.com" not in content
    assert "<EMAIL_ADDRESS>" in content


async def test_response_phase_off_by_default(nlp_guardrail):
    response = {
        "choices": [{"message": {"role": "assistant", "content": "bot@example.com"}}]
    }
    result = await nlp_guardrail.check_response(make_request("q"), response, make_ctx())
    assert result is None


# ----------------------------------------------------------------------
# Pattern-only mode
# ----------------------------------------------------------------------


async def test_pattern_only_redacts(pattern_guardrail):
    result = await pattern_guardrail.check_request(make_request(PII_TEXT), make_ctx())
    assert result is not None
    assert result.action.value == "modify"
    redacted = result.modified_messages[0].content
    assert "jane.doe@example.com" not in redacted
    assert "4111 1111 1111 1111" not in redacted
    assert "<EMAIL_ADDRESS>" in redacted
    assert "<CREDIT_CARD>" in redacted


def test_pattern_only_drops_ner_defaults():
    guardrail = PresidioPIIGuardrail({"use_nlp": False})
    # PERSON/LOCATION are silently dropped from the *default* set (an
    # explicit request for them raises, tested above).
    assert "PERSON" not in guardrail.entities
    assert "LOCATION" not in guardrail.entities
    assert "EMAIL_ADDRESS" in guardrail.entities


async def test_score_threshold_filters_weak_matches():
    # PhoneRecognizer pattern scores 0.4 without NLP context; raising the
    # threshold above it must drop the phone but keep the email (1.0).
    guardrail = PresidioPIIGuardrail({"use_nlp": False, "score_threshold": 0.5})
    result = await guardrail.check_request(
        make_request("call 415-555-2671 or email jane.doe@example.com"), make_ctx()
    )
    redacted = result.modified_messages[0].content
    assert "415-555-2671" in redacted  # below threshold: untouched
    assert "jane.doe@example.com" not in redacted
    assert "<EMAIL_ADDRESS>" in redacted


# ----------------------------------------------------------------------
# Through the full app
# ----------------------------------------------------------------------


@pytest.fixture
async def presidio_env(tmp_path):
    config = make_config(
        guardrails=[
            GuardrailConfig(name="presidio_pii", params={"mask_in_response": True})
        ]
    )
    async for ns in _make_env(tmp_path, config=config):
        yield ns


@pytest.fixture
async def presidio_block_env(tmp_path):
    config = make_config(
        guardrails=[
            GuardrailConfig(
                name="presidio_pii", params={"block_entities": ["CREDIT_CARD"]}
            )
        ]
    )
    async for ns in _make_env(tmp_path, config=config):
        yield ns


async def test_e2e_redaction_reaches_provider(presidio_env):
    key = (await create_key(presidio_env.client, "presidio"))["key"]
    resp = await presidio_env.client.post(
        "/v1/chat/completions",
        json=chat_body(content=f"my email is jane.doe@example.com PIIPLEASE"),
        headers=auth(key),
    )
    assert resp.status_code == 200
    # The provider only ever saw the placeholder.
    sent = presidio_env.mock.bodies()[-1]
    assert "jane.doe@example.com" not in str(sent)
    assert "<EMAIL_ADDRESS>" in sent["messages"][0]["content"]
    # The response (mock echoes PII on PIIPLEASE) is masked on the way out.
    content = resp.json()["choices"][0]["message"]["content"]
    assert "bot@example.com" not in content
    assert "<EMAIL_ADDRESS>" in content


async def test_e2e_block_returns_400(presidio_block_env):
    key = (await create_key(presidio_block_env.client, "presidio-block"))["key"]
    resp = await presidio_block_env.client.post(
        "/v1/chat/completions",
        json=chat_body(content=f"card 4111 1111 1111 1111 and a question"),
        headers=auth(key),
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "content_policy_violation"
    assert "presidio_pii" in error["message"]
    assert len(presidio_block_env.mock.requests) == 0
