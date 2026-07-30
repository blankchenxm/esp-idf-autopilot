from __future__ import annotations

from pathlib import Path

from orchestrator.control_events import (
    await_control_event,
    publish_control_event,
    read_control_events,
)


def test_progress_ticks_never_become_model_wakes(tmp_path: Path) -> None:
    for index in range(1000):
        publish_control_event(
            tmp_path,
            "probe",
            source="fake_build",
            mode="CONTINUOUS",
            reason="progress",
            payload={"tick": index},
            model_action_required=False,
        )

    assert len(read_control_events(
        tmp_path, "probe", model_action_only=False
    )) == 1
    assert read_control_events(
        tmp_path, "probe", model_action_only=True
    ) == []
    assert await_control_event(
        tmp_path,
        "probe",
        timeout_s=0.01,
        model_action_only=True,
        poll_interval_s=0.001,
    ) is None


def test_await_event_returns_first_meaningful_transition(tmp_path: Path) -> None:
    progress = publish_control_event(
        tmp_path,
        "probe",
        source="execution_job",
        mode="CONTINUOUS",
        reason="phase",
        model_action_required=False,
    )
    terminal = publish_control_event(
        tmp_path,
        "probe",
        source="execution_job",
        mode="FAULTED",
        reason="typed_failure",
        payload={"failure_code": "BUILD_FAILED"},
    )

    event = await_control_event(
        tmp_path, "probe", after_seq=progress["event_seq"], timeout_s=0.1
    )
    assert event == terminal
    assert event["model_action_required"] is True
