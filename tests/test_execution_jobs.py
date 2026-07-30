from __future__ import annotations

from pathlib import Path

from orchestrator.execution_jobs import (
    finish_execution_job,
    launch_execution_job,
    read_execution_job,
)
from orchestrator.runtime_paths import ProjectRuntime
from orchestrator.storage import atomic_write_json


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


def test_status_atomically_repairs_stale_active_projection(
    tmp_path: Path, monkeypatch,
):
    runtime = ProjectRuntime(tmp_path, "demo").ensure()
    job = {
        "schema_version": "1.0",
        "job_id": "job-1",
        "project": "demo",
        "mode": "CONTINUOUS",
        "pid": 4242,
        "cursor": "build",
    }
    atomic_write_json(runtime.execution_jobs / "job-1.json", job)
    atomic_write_json(
        runtime.active_execution_job,
        {**job, "cursor": "stale"},
    )
    monkeypatch.setattr(
        "orchestrator.execution_jobs._pid_is_alive", lambda _pid: True
    )

    reconciled = read_execution_job(tmp_path, "demo")
    assert reconciled["cursor"] == "build"
    persisted = __import__("json").loads(
        runtime.active_execution_job.read_text(encoding="utf-8")
    )
    assert persisted["cursor"] == "build"
    records = list(runtime.worker_reconciliations.glob("*.json"))
    assert len(records) == 1
