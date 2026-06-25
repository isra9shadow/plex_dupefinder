"""JSON Schemas for the model's structured output + validation.

The DIAGNOSIS schema is the shared output contract: OllamaClient.generate_json
asks the model to satisfy it, ``validate`` checks the payload (jsonschema), and
``render`` turns it into markdown. Keeping it here (one place) means the templates,
the renderer and the guard never disagree on the shape.
"""

from __future__ import annotations

from typing import Any

import jsonschema

SEVERITIES = ("info", "warning", "error", "critical")
EVIDENCE_KINDS = ("fact", "inference", "hypothesis")
RISKS = ("low", "medium", "high")

# One diagnostic report: a short summary + a list of findings. Each finding must
# cite evidence tagged fact/inference/hypothesis (anti-hallucination) and keep its
# remediation commands Unraid-compatible (further enforced by aictx.guard).
DIAGNOSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"enum": list(SEVERITIES)},
                    "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                    "root_cause": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "kind": {"enum": list(EVIDENCE_KINDS)},
                                "detail": {"type": "string"},
                            },
                            "required": ["kind", "detail"],
                        },
                    },
                    "recommended_actions": {"type": "array", "items": {"type": "string"}},
                    "unraid_commands": {"type": "array", "items": {"type": "string"}},
                    "risk": {"enum": list(RISKS)},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": [
                    "title",
                    "severity",
                    "confidence",
                    "root_cause",
                    "evidence",
                    "recommended_actions",
                    "risk",
                    "priority",
                ],
            },
        },
    },
    "required": ["summary", "findings"],
}


def validate(payload: object, schema: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation errors ([] = valid)."""
    validator = jsonschema.Draft202012Validator(schema)
    return [error.message for error in sorted(validator.iter_errors(payload), key=str)]
