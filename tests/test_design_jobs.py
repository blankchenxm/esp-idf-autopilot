from __future__ import annotations

import json
import os
from pathlib import Path

import orchestrator.cli as cli
from orchestrator.design_jobs import (
    await_design_job_started,
    fail_design_job,
    finish_design_job,
    launch_or_observe_design,
    read_design_job,
)
from orchestrator.runtime_paths import ProjectRuntime


class Process:
    def __init__(self, pid: int):
        self.pid = pid


def inputs(root: Path, project: str) -> None:
    for area in ("requirements", "connections"):
        path = root / area / f"{project}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {project} {area}\n", encoding="utf-8")


def test_design_job_is_project_scoped_and_duplicate_launch_is_reused(tmp_path: Path, monkeypatch):
    for project in ("alpha_board", "beta_display"):
        inputs(tmp_path, project)
    launched: list[str] = []

    def spawn(_root, _runtime, record):
        launched.append(record["project"])
        return Process(1000 + len(launched))

    monkeypatch.setattr("orchestrator.design_jobs._spawn_worker", spawn)
    monkeypatch.setattr("orchestrator.design_jobs._pid_is_alive", lambda pid: pid in {1001, 1002})

    alpha = launch_or_observe_design(tmp_path, "alpha_board")
    duplicate = launch_or_observe_design(tmp_path, "alpha_board")
    beta = launch_or_observe_design(tmp_path, "beta_display")

    assert alpha["mode"] == "DESIGN_RUNNING"
    assert duplicate["job_id"] == alpha["job_id"]
    assert beta["job_id"] != alpha["job_id"]
    assert launched == ["alpha_board", "beta_display"]
    assert ProjectRuntime(tmp_path, "alpha_board").active_design_job.is_file()
    assert ProjectRuntime(tmp_path, "beta_display").active_design_job.is_file()


def test_worker_completion_survives_original_caller_exit(tmp_path: Path, monkeypatch):
    inputs(tmp_path, "generic_sensor")
    monkeypatch.setattr("orchestrator.design_jobs._spawn_worker", lambda *_: Process(4242))
    monkeypatch.setattr("orchestrator.design_jobs._pid_is_alive", lambda pid: pid == 4242)
    running = launch_or_observe_design(tmp_path, "generic_sensor")

    design = tmp_path / "projects" / "generic_sensor" / "design-package" / "rev-0001"
    design.mkdir(parents=True)
    (design / "spec.md").write_text("# Review\n", encoding="utf-8")
    completed = finish_design_job(tmp_path, "generic_sensor", running["job_id"], design)

    assert completed["mode"] == "WAITING_SPEC"
    assert read_design_job(tmp_path, "generic_sensor")["job_id"] == running["job_id"]
    persisted = json.loads(ProjectRuntime(tmp_path, "generic_sensor").active_design_job.read_text(encoding="utf-8"))
    assert persisted["mode"] == "WAITING_SPEC"


def test_waiting_design_input_is_reused_until_inputs_change(tmp_path: Path, monkeypatch):
    project = "needs_policy"
    inputs(tmp_path, project)
    pids = iter((8101, 8102))
    monkeypatch.setattr(
        "orchestrator.design_jobs._spawn_worker", lambda *_: Process(next(pids))
    )
    monkeypatch.setattr(
        "orchestrator.design_jobs._pid_is_alive", lambda pid: pid in {8101, 8102}
    )
    first = launch_or_observe_design(tmp_path, project, revision=7)
    assert first["requested_revision"] == 7
    assert first["revision"] is None
    from orchestrator.design_jobs import finish_design_job

    waiting = finish_design_job(
        tmp_path,
        project,
        first["job_id"],
        graph_result={
            "mode": "WAITING_DESIGN_INPUT",
            "spec": str(tmp_path / "draft-spec.md"),
            "summary": "policy missing",
        },
    )
    same = launch_or_observe_design(tmp_path, project)
    assert same["job_id"] == waiting["job_id"]
    assert waiting["requested_revision"] == 7
    assert waiting["revision"] is None
    (tmp_path / "requirements" / f"{project}.md").write_text(
        "# changed\n", encoding="utf-8"
    )
    changed = launch_or_observe_design(tmp_path, project)
    assert changed["job_id"] != waiting["job_id"]


def test_dead_worker_is_blocked_and_next_design_starts_new_job(tmp_path: Path, monkeypatch):
    inputs(tmp_path, "recoverable")
    pids = iter((501, 502))
    monkeypatch.setattr("orchestrator.design_jobs._spawn_worker", lambda *_: Process(next(pids)))
    alive = {501}
    monkeypatch.setattr("orchestrator.design_jobs._pid_is_alive", lambda pid: pid in alive)
    first = launch_or_observe_design(tmp_path, "recoverable")
    alive.clear()
    assert read_design_job(tmp_path, "recoverable")["mode"] == "BLOCKED"

    alive.add(502)
    second = launch_or_observe_design(tmp_path, "recoverable")
    assert second["mode"] == "DESIGN_RUNNING"
    assert second["job_id"] == first["job_id"]
    assert second.get("resumed_at")


def test_worker_launch_handshake_persists_boot_evidence(
    tmp_path: Path, monkeypatch
):
    project = "boot_evidence"
    inputs(tmp_path, project)
    monkeypatch.setattr(
        "orchestrator.design_jobs._spawn_worker", lambda *_: Process(os.getpid())
    )
    monkeypatch.setattr(
        "orchestrator.design_jobs._pid_is_alive", lambda pid: pid == os.getpid()
    )
    running = launch_or_observe_design(tmp_path, project)

    booted = await_design_job_started(
        tmp_path, project, running["job_id"]
    )

    assert booted["worker_started_at"]
    assert booted["phase"] == "initialize"
    assert booted["kind"] is None
    assert booted["summary"] is None


def test_repeated_worker_boot_exit_has_bounded_recovery(
    tmp_path: Path, monkeypatch
):
    project = "bounded_boot"
    inputs(tmp_path, project)
    pids = iter((7101, 7102, 7103, 7104))
    launched: list[int] = []

    def spawn(*_):
        process = Process(next(pids))
        launched.append(process.pid)
        return process

    alive: set[int] = set()
    monkeypatch.setattr("orchestrator.design_jobs._spawn_worker", spawn)
    monkeypatch.setattr(
        "orchestrator.design_jobs._pid_is_alive", lambda pid: pid in alive
    )

    current = launch_or_observe_design(tmp_path, project)
    for expected in (1, 2, 3):
        current = read_design_job(tmp_path, project)
        assert current["interruption_count"] == expected
        assert current["kind"] == "worker_boot"
        if expected < 3:
            current = launch_or_observe_design(tmp_path, project)
            assert current["mode"] == "DESIGN_RUNNING"

    blocked = launch_or_observe_design(tmp_path, project)
    assert blocked["mode"] == "FAULTED"
    assert blocked["kind"] == "worker_stall"
    assert blocked["interruption_count"] == 3
    assert launched == [7101, 7102, 7103]


def test_worker_failure_binds_checkpoint_evidence(tmp_path: Path, monkeypatch):
    project = "failure_evidence"
    inputs(tmp_path, project)
    monkeypatch.setattr(
        "orchestrator.design_jobs._spawn_worker", lambda *_: Process(901)
    )
    monkeypatch.setattr(
        "orchestrator.design_jobs._pid_is_alive", lambda pid: pid == 901
    )
    running = launch_or_observe_design(tmp_path, project)
    evidence = tmp_path / "projects" / project / "design-package" / ".staging"
    evidence.mkdir(parents=True)
    monkeypatch.setattr(
        "orchestrator.design_graph.inspect_design_graph_state",
        lambda *_: {"phase": "ground", "staging_root": str(evidence)},
    )

    failed = fail_design_job(
        tmp_path, project, running["job_id"], RuntimeError("grounding failed")
    )

    assert failed["mode"] == "FAULTED"
    assert failed["phase"] == "ground"
    assert failed["evidence"] == str(evidence)


def test_cli_returns_running_then_status_observes_same_generic_job(tmp_path: Path, monkeypatch, capsys):
    inputs(tmp_path, "camera_display")
    (tmp_path / "projects" / "camera_display").mkdir(parents=True)
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr("orchestrator.design_jobs._spawn_worker", lambda *_: Process(777))
    monkeypatch.setattr("orchestrator.design_jobs._pid_is_alive", lambda pid: pid == 777)

    assert cli.main(["design", "--project", "camera_display"]) == 0
    launched = json.loads(capsys.readouterr().out)
    assert launched["mode"] == "DESIGN_RUNNING"

    assert cli.main(["status", "--project", "camera_display"]) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed["job_id"] == launched["job_id"]
    assert observed["project"] == "camera_display"
