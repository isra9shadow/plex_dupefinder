"""Tests for the aictx frozen contracts (provider + schema)."""

from __future__ import annotations

from aictx import schema
from aictx.provider import (
    BuiltPrompt,
    ContextBlock,
    ContextProvider,
    PromptTemplate,
    Tier,
    estimate_tokens,
)


def test_estimate_tokens_is_roughly_chars_over_four() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100


def test_context_block_tokens_and_render() -> None:
    block = ContextBlock(name="x", title="Title", body="body text", tier=Tier.HIGH)
    assert block.tokens == estimate_tokens("Title") + estimate_tokens("body text")
    assert block.render() == "## Title\nbody text"
    assert ContextBlock("x", "T", "b", Tier.LOW, token_hint=42).tokens == 42


def test_tier_ordering_critical_first() -> None:
    assert sorted([Tier.LOW, Tier.CRITICAL, Tier.HIGH]) == [Tier.CRITICAL, Tier.HIGH, Tier.LOW]


def test_provider_protocol_is_runtime_checkable() -> None:
    class _P:
        name = "p"

        def block(self) -> ContextBlock | None:
            return None

    assert isinstance(_P(), ContextProvider)


def test_dataclasses_construct() -> None:
    tmpl = PromptTemplate(task="t", system_role="r", instructions="i", schema={})
    assert tmpl.provider_order == ()
    bp = BuiltPrompt(system="s", prompt="p", schema={}, tokens=3)
    assert bp.tokens == 3


def test_diagnosis_schema_validates_good_and_bad() -> None:
    good = {
        "summary": "ok",
        "findings": [
            {
                "title": "Traefik ACME perms",
                "severity": "warning",
                "confidence": 80,
                "root_cause": "acme.json world-readable",
                "evidence": [{"kind": "fact", "detail": "permission denied acme.json"}],
                "recommended_actions": ["chmod 600 acme.json"],
                "unraid_commands": [],
                "risk": "low",
                "priority": 3,
            }
        ],
    }
    assert schema.validate(good, schema.DIAGNOSIS_SCHEMA) == []

    bad = {"summary": "x", "findings": [{"title": "no required fields"}]}
    assert schema.validate(bad, schema.DIAGNOSIS_SCHEMA)  # non-empty error list
