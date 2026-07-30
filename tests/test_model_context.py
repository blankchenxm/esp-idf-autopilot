from __future__ import annotations

import json
from pathlib import Path

from orchestrator.model_context import (
    MAX_LOG_BYTES,
    build_owner_context_envelope,
    parse_codex_jsonl_usage,
    validate_model_usage_budget,
)


def _contract() -> dict:
    return {
        "project": "probe",
        "subsystems": [
            {
                "id": "sensor",
                "dependencies": [],
                "required_operations": ["initialize", "sample"],
            },
            {
                "id": "unrelated",
                "dependencies": [],
                "required_operations": ["erase"],
            },
        ],
        "requirements": [
            {"id": "R1", "owner": "sensor"},
            {"id": "R2", "owner": "unrelated"},
        ],
        "verification": [
            {"test_id": "T1", "owner": "sensor", "requirement_id": "R1"},
            {"test_id": "T2", "owner": "unrelated", "requirement_id": "R2"},
        ],
        "component_selections": [],
        "datasheets": [],
        "architecture": {},
        "integration": {"tests": []},
        "release": {},
        "limitations": [],
    }


def test_owner_context_is_deterministic_bounded_and_scoped(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = project / "components" / "sensor" / "sensor.c"
    source.parent.mkdir(parents=True)
    source.write_text("int sensor_sample(void) { return 1; }\n")
    unrelated = project / "components" / "unrelated" / "erase.c"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("void erase(void) {}\n")
    log = tmp_path / "failure.log"
    secret = "never-copy-this-secret"
    log.write_text(("x" * (10 * 1024 * 1024)) + secret)

    arguments = {
        "project": "probe",
        "run_id": "run-1",
        "design_digest": "a" * 64,
        "owner": "sensor",
        "reason": "implementation_or_repair",
        "instruction": f"repair sampling without {secret}",
        "contract": _contract(),
        "project_dir": project,
        "implementation_addendum": None,
        "failure_logs": (log,),
        "secret_values": [secret],
    }
    first = build_owner_context_envelope(**arguments)
    second = build_owner_context_envelope(**arguments)

    assert first == second
    serialized = json.dumps(first, ensure_ascii=False)
    assert secret not in serialized
    assert "unrelated/erase.c" not in serialized
    assert '"owner": "unrelated"' not in serialized
    assert len(first["diagnostic_excerpts"][0]["excerpt"].encode()) <= MAX_LOG_BYTES
    assert first["context_digest"] == second["context_digest"]


def test_codex_jsonl_usage_is_accounted() -> None:
    output = "\n".join([
        json.dumps({"type": "item.completed", "item": {}}),
        json.dumps({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 12,
                "reasoning_output_tokens": 3,
            },
        }),
    ])
    usage = parse_codex_jsonl_usage(output)
    assert usage["input_tokens"] == 100
    assert usage["cached_input_tokens"] == 80
    assert usage["total_tokens"] == 115
    assert usage["source"] == "codex_jsonl"


def test_model_usage_budget_never_expands_context_automatically() -> None:
    usage = {
        "source": "codex_jsonl",
        "input_tokens": 40_001,
        "output_tokens": 1,
        "reasoning_output_tokens": 1,
        "tool_calls": 1,
    }
    errors = validate_model_usage_budget(usage, {
        "max_input_tokens": 40_000,
        "max_output_tokens": 8_000,
        "max_reasoning_tokens": 8_000,
        "max_tool_calls": 32,
    })
    assert errors == ["input_tokens exceeded max_input_tokens"]
