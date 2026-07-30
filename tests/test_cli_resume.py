from types import SimpleNamespace

import json

import pytest

from orchestrator.cli import _parser, _record_thread, _resume_input, _runner_lock, _select_thread_id
from orchestrator.runtime_paths import ProjectRuntime


class CommandStub:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def snapshot(values, next=(), interrupts=False):
    tasks = (SimpleNamespace(interrupts=("gate",) if interrupts else ()),)
    return SimpleNamespace(values=values, next=next, tasks=tasks)


def test_existing_non_interrupt_checkpoint_resumes_without_initial_state():
    initial = {"intent": "new", "project": "wrong-if-injected"}
    assert _resume_input(snapshot({"mode": "CONTINUOUS"}, next=("subsystem",)), initial, None, CommandStub) is None


def test_pending_interrupt_requires_explicit_response():
    initial = {"intent": "new"}
    waiting = snapshot({"mode": "WAITING_TIER_C"}, next=("tier_c",), interrupts=True)
    assert _resume_input(waiting, initial, None, CommandStub) is None
    assert _resume_input(waiting, initial, {"TC1": "confirmed"}, CommandStub).kwargs == {"resume": {"TC1": "confirmed"}}


def test_paused_checkpoint_dispatches_only_the_recorded_safe_node():
    paused = snapshot({"mode": "PAUSED", "pause_next_node": "subsystem", "progress_seq": 7})
    result = _resume_input(paused, {"intent": "new"}, None, CommandStub)
    assert result.kwargs["goto"] == "subsystem"
    assert result.kwargs["update"]["mode"] == "CONTINUOUS"
    assert result.kwargs["update"]["progress_seq"] == 8


def test_pause_accepts_an_optional_design_revision():
    args = _parser().parse_args(["pause", "--project", "demo"])
    assert args.revision is None and args.reason == "explicit user pause"


def test_thread_selector_prefers_only_nonterminal_thread(tmp_path):
    project = tmp_path / "demo"
    _record_thread(project, "demo", 1, "demo:rev-0001", "COMPLETE")
    _record_thread(project, "demo", 1, "demo:rev-0001:restart-active", "CONTINUOUS")
    assert _select_thread_id(project, "demo", 1, None, False).endswith("restart-active")


def test_thread_selector_rejects_ambiguous_nonterminal_threads(tmp_path):
    project = tmp_path / "demo"
    _record_thread(project, "demo", 1, "demo:rev-0001:restart-a", "CONTINUOUS")
    _record_thread(project, "demo", 1, "demo:rev-0001:restart-b", "WAITING_TIER_C")
    with pytest.raises(RuntimeError, match="multiple nonterminal"):
        _select_thread_id(project, "demo", 1, None, False)


def test_thread_record_tracks_terminal_mode_in_active_projection(tmp_path):
    project = tmp_path / "demo"
    _record_thread(project, "demo", 2, "demo:rev-0002", "COMPLETE")
    active = json.loads(ProjectRuntime.for_project_dir(project).active_thread.read_text(encoding="utf-8"))
    assert active["mode"] == "COMPLETE" and active["revision"] == 2


def test_stale_runner_lock_is_reclaimed(tmp_path, monkeypatch):
    project = tmp_path / "demo"; lock = ProjectRuntime.for_project_dir(project).runner_lock
    lock.parent.mkdir(parents=True); lock.write_text('{"pid":999999,"token":"old"}', encoding="utf-8")
    monkeypatch.setattr("orchestrator.cli._pid_is_alive", lambda pid: False)
    with _runner_lock(project, 1):
        assert json.loads(lock.read_text(encoding="utf-8"))["token"] != "old"
    assert not lock.exists()


def test_projects_with_same_revision_have_separate_control_namespaces(tmp_path):
    first = tmp_path / "projects" / "alpha"
    second = tmp_path / "projects" / "beta"
    _record_thread(first, "alpha", 1, "alpha:rev-0001", "CONTINUOUS")
    _record_thread(second, "beta", 1, "beta:rev-0001", "COMPLETE")
    first_runtime = ProjectRuntime.for_project_dir(first)
    second_runtime = ProjectRuntime.for_project_dir(second)
    assert first_runtime.root != second_runtime.root
    assert first_runtime.checkpoints != second_runtime.checkpoints
    assert first_runtime.read_thread_registry()[0]["project"] == "alpha"
    assert second_runtime.read_thread_registry()[0]["project"] == "beta"


def test_legacy_global_checkpoint_is_copied_only_for_project_with_legacy_pointer(tmp_path):
    import sqlite3

    project = tmp_path / "projects" / "legacy"
    execution = project / "execution"
    execution.mkdir(parents=True)
    (execution / "active-checkpoint-thread.json").write_text(json.dumps({
        "schema_version": "1.0", "project": "legacy", "revision": 1,
        "thread_id": "legacy:rev-0001", "mode": "BLOCKED",
    }), encoding="utf-8")
    database = tmp_path / "runtime" / "checkpoints.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("create table marker (value text)")
        connection.execute("insert into marker values ('legacy')")
    migrated = ProjectRuntime(tmp_path, "legacy").ensure()
    fresh = ProjectRuntime(tmp_path, "fresh").ensure()
    assert migrated.checkpoints.is_file()
    with sqlite3.connect(migrated.checkpoints) as connection:
        assert connection.execute("select value from marker").fetchone()[0] == "legacy"
    assert not fresh.checkpoints.exists()


def test_blocked_checkpoint_reenters_only_after_material_change(tmp_path, monkeypatch):
    project = tmp_path / "demo"; project.mkdir()
    values = {"mode": "BLOCKED", "material_fingerprint": "old", "failed_node": "subsystem", "progress_seq": 4}
    monkeypatch.setattr("orchestrator.policies.material_fingerprint", lambda project_dir, state: "new")
    result = _resume_input(snapshot(values), {}, None, CommandStub, project)
    assert result.kwargs["goto"] == "subsystem" and result.kwargs["update"]["material_fingerprint"] == "new"


def test_resume_reentry_identifies_a_completed_fault_with_a_recovery_target():
    values = {"mode": "CONTINUOUS", "recovery_target": "subsystem"}
    state = snapshot(values)
    assert not state.next and not state.tasks
