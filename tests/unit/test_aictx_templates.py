"""Unit tests for the declarative per-task PromptTemplates in aictx.templates."""

from __future__ import annotations

import pytest
from aictx.provider import PromptTemplate
from aictx.schema import DIAGNOSIS_SCHEMA
from aictx.templates import analyst, dupefinder, logwatch, organizer

TEMPLATES = {
    "analyst": analyst,
    "logwatch": logwatch,
    "organizer": organizer,
    "dupefinder": dupefinder,
}


@pytest.mark.parametrize("task", sorted(TEMPLATES))
def test_module_exposes_prompt_template(task: str) -> None:
    template = TEMPLATES[task].TEMPLATE
    assert isinstance(template, PromptTemplate)
    assert template.task == task


@pytest.mark.parametrize("task", sorted(TEMPLATES))
def test_role_and_instructions_non_empty(task: str) -> None:
    template = TEMPLATES[task].TEMPLATE
    assert template.system_role.strip()
    assert template.instructions.strip()


@pytest.mark.parametrize("task", sorted(TEMPLATES))
def test_schema_is_diagnosis_schema(task: str) -> None:
    assert TEMPLATES[task].TEMPLATE.schema is DIAGNOSIS_SCHEMA


@pytest.mark.parametrize("task", sorted(TEMPLATES))
def test_instructions_enforce_unraid_and_json(task: str) -> None:
    instructions = TEMPLATES[task].TEMPLATE.instructions.lower()
    assert "unraid" in instructions
    assert "systemctl" in instructions
    assert "json" in instructions


@pytest.mark.parametrize("task", sorted(TEMPLATES))
def test_provider_order_present(task: str) -> None:
    assert TEMPLATES[task].TEMPLATE.provider_order
