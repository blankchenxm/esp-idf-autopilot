from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from orchestrator.control_events import publish_control_event
from orchestrator.models import Receipt
from orchestrator.run_metrics import build_run_report
from orchestrator.run_metrics import token_reduction_percent
from orchestrator.storage import ProjectStore


def _receipt(
    store: ProjectStore,
    operation: str,
    *,
    inputs: dict,
    outputs: dict,
    command: list[str],
) -> Receipt:
    now = datetime.now(timezone.utc).isoformat()
    receipt = Receipt(
        receipt_id=store.new_id(operation),
        run_id="run-1",
        operation=operation,
        started_at=now,
        finished_at=now,
        success=True,
        inputs=inputs,
        outputs=outputs,
        command=command,
    )
    store.write_receipt(receipt, "metrics")
    return receipt


def test_run_report_separates_model_runner_and_non_model_progress(
    tmp_path: Path,
) -> None:
    project = tmp_path / "projects" / "probe"
    store = ProjectStore(project)
    store.ensure()
    _receipt(
        store,
        "implement_or_repair",
        inputs={"reason": "typed_repair"},
        outputs={
            "model_context_digest": "c" * 64,
            "model_context_bytes": 4096,
            "model_usage": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 10,
                "reasoning_output_tokens": 5,
                "tool_calls": 2,
            },
        },
        command=["codex", "exec"],
    )
    _receipt(
        store,
        "verification_build",
        inputs={},
        outputs={},
        command=["idf-wrapper", "build"],
    )
    for tick in range(1000):
        publish_control_event(
            tmp_path,
            "probe",
            source="synthetic_build",
            mode="CONTINUOUS",
            reason="progress",
            payload={"tick": tick},
            model_action_required=False,
        )

    report = build_run_report(tmp_path, "probe", "run-1")
    assert report["model_totals"] == {
        "input_tokens": 100,
        "cached_input_tokens": 80,
        "output_tokens": 10,
        "reasoning_tokens": 5,
        "tool_calls": 2,
    }
    assert report["tool_calls"] == {
        "model_initiated": 2,
        "deterministic_runner": 1,
    }
    assert report["control_plane"]["model_action_events"] == 0
    assert report["control_plane"]["non_model_progress_events"] == 1
    assert report["control_plane"]["suppressed_progress_events"] == 999


def test_budget_bound_replay_clears_the_55_percent_gate() -> None:
    root = Path(__file__).resolve().parents[1]
    benchmark = json.loads(
        (root / "benchmarks" / "harness-token-replay.json").read_text(
            encoding="utf-8"
        )
    )
    baseline = benchmark["baseline"]["input_tokens"]
    measured_ceiling = benchmark["bounded_replay"]["input_token_ceiling"]
    reduction = token_reduction_percent(baseline, measured_ceiling)
    assert reduction >= benchmark["acceptance"][
        "required_reduction_percent"
    ]
    assert reduction >= benchmark["bounded_replay"][
        "reduction_percent_floor"
    ]
    assert benchmark["bounded_replay"]["model_calls_from_progress"] == 0
