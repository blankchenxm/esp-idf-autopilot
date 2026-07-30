from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .runtime_paths import ProjectRuntime


def _usage(value: dict[str, Any]) -> dict[str, int]:
    usage = value.get("model_usage") or value.get("usage") or {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cached_input_tokens": int(usage.get("cached_input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "reasoning_tokens": int(
            usage.get("reasoning_tokens")
            or usage.get("reasoning_output_tokens")
            or 0
        ),
        "tool_calls": int(usage.get("tool_calls") or 0),
    }


def build_run_report(
    repo_root: Path, project: str, run_id: str | None = None,
) -> dict[str, Any]:
    project_dir = repo_root / "projects" / project
    model_calls: list[dict[str, Any]] = []
    runner_tool_calls = 0
    for path in (project_dir / "execution" / "receipts").rglob("*.json"):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if run_id and receipt.get("run_id") != run_id:
            continue
        outputs = receipt.get("outputs") or {}
        usage = _usage(outputs)
        context_digest = outputs.get("model_context_digest")
        if context_digest or any(usage.values()):
            model_calls.append({
                "receipt_id": receipt.get("receipt_id"),
                "node": receipt.get("operation"),
                "reason": (receipt.get("inputs") or {}).get("reason"),
                "context_digest": context_digest,
                "context_bytes": outputs.get("model_context_bytes"),
                **usage,
            })
        else:
            runner_tool_calls += 1 if receipt.get("command") else 0
    totals = {
        key: sum(int(item.get(key) or 0) for item in model_calls)
        for key in (
            "input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_tokens", "tool_calls",
        )
    }
    runtime = ProjectRuntime(repo_root, project).ensure()
    sequence = {}
    if runtime.control_event_sequence.is_file():
        sequence = json.loads(
            runtime.control_event_sequence.read_text(encoding="utf-8")
        )
    progress = 0
    model_actions = 0
    for path in runtime.control_events.glob("*.json"):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if event.get("kind") == "MODEL_ACTION_REQUIRED":
            model_actions += 1
        else:
            progress += 1
    return {
        "schema_version": "1.0",
        "project": project,
        "run_id": run_id,
        "model_calls": model_calls,
        "model_totals": totals,
        "control_plane": {
            "model_action_events": model_actions,
            "non_model_progress_events": progress,
            "suppressed_progress_events": int(
                sequence.get("suppressed_progress_events") or 0
            ),
        },
        "tool_calls": {
            "model_initiated": totals["tool_calls"],
            "deterministic_runner": runner_tool_calls,
        },
    }


def token_reduction_percent(baseline_input: int, measured_input: int) -> float:
    if baseline_input <= 0 or measured_input < 0:
        raise ValueError("token inputs must be non-negative and baseline positive")
    return 100.0 * (baseline_input - measured_input) / baseline_input
