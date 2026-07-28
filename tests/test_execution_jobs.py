from __future__ import annotations

from pathlib import Path

from orchestrator.execution_jobs import (
    finish_execution_job,
    launch_execution_job,
    read_execution_job,
)


class Process:
    pid = 4242


def test_execution_worker_is_detached_and_observable(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        "orchestrator.execution_jobs._spawn", lambda *_: Process()
    )
    monkeypatch.setattr(
        "orchestrator.execution_jobs._pid_is_alive", lambda pid: pid == 4242
    )

    running = launch_execution_job(
        tmp_path,
        "demo",
        revision=1,
        intent="new",
        port="COM4",
        baud=115200,
        baseline=None,
    )

    assert running["mode"] == "CONTINUOUS"
    assert running["pid"] == 4242
    assert read_execution_job(tmp_path, "demo")["job_id"] == running["job_id"]


def test_execution_worker_terminal_result_is_durable(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        "orchestrator.execution_jobs._spawn", lambda *_: Process()
    )
    monkeypatch.setattr(
        "orchestrator.execution_jobs._pid_is_alive", lambda _pid: True
    )
    running = launch_execution_job(
        tmp_path,
        "demo",
        revision=1,
        intent="resume",
        port=None,
        baud=115200,
        baseline=None,
        resume_value={"TC1": {"status": "confirmed"}},
    )
    finished = finish_execution_job(
        tmp_path,
        "demo",
        running["job_id"],
        {
            "mode": "WAITING_TIER_C",
            "cursor": "tier-c",
            "next_action": "review",
            "run_id": "run-1",
        },
    )
    assert finished["mode"] == "WAITING_TIER_C"
    assert read_execution_job(tmp_path, "demo")["run_id"] == "run-1"
