from __future__ import annotations

import json
from pathlib import Path

from orchestrator.datasheet_library import project_alias_path
from orchestrator.project_lifecycle import reset_project, restore_project, snapshot_project
from orchestrator.runtime_paths import ProjectRuntime


def test_snapshot_reset_restore_is_project_scoped_and_recoverable(tmp_path: Path):
    repo = tmp_path / "repo"
    project_id = "demo"
    project = repo / "projects" / project_id
    project.mkdir(parents=True)
    (project / "source.c").write_text("int demo;", encoding="utf-8")
    for area in ("requirements", "connections"):
        path = repo / area / f"{project_id}.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"# {area}\n", encoding="utf-8")
    runtime = ProjectRuntime(repo, project_id).ensure()
    (runtime.root / "control.txt").write_text("checkpoint", encoding="utf-8")
    alias = project_alias_path(repo, project_id)
    alias.parent.mkdir(parents=True)
    alias.write_text(json.dumps({
        "schema_version": "1.1", "project_id": project_id, "aliases": {},
    }), encoding="utf-8")
    shared = repo / "hardware" / "datasheets" / "objects" / "sha256" / "shared.pdf"
    shared.parent.mkdir(parents=True)
    shared.write_bytes(b"%PDF-shared")
    incoming = (
        repo / "hardware" / "datasheets" / "incoming" / project_id / "sensor"
    )
    incoming.mkdir(parents=True)
    (incoming / "candidate.pdf").write_bytes(b"%PDF-project-candidate")
    output = tmp_path / "snapshots" / "demo-before"

    result = snapshot_project(repo, project_id, output)
    assert result["project_id"] == project_id
    reset = reset_project(repo, project_id, output)
    assert set(reset["detached"]) == {
        "project",
        "runtime",
        "datasheet-incoming",
        "datasheet-alias.json",
    }
    assert (
        not project.exists()
        and not runtime.root.exists()
        and not incoming.parents[0].exists()
        and not alias.exists()
    )
    assert (repo / "requirements" / "demo.md").is_file()
    assert shared.is_file()

    restored = restore_project(repo, project_id, output)
    assert restored["project_id"] == project_id
    assert (project / "source.c").read_text(encoding="utf-8") == "int demo;"
    assert (runtime.root / "control.txt").read_text(encoding="utf-8") == "checkpoint"
    assert alias.is_file()
    assert (incoming / "candidate.pdf").read_bytes() == b"%PDF-project-candidate"


def test_snapshot_refuses_repository_internal_destination(tmp_path: Path):
    repo = tmp_path / "repo"
    project = repo / "projects" / "demo"
    project.mkdir(parents=True)
    for area in ("requirements", "connections"):
        path = repo / area / "demo.md"
        path.parent.mkdir(parents=True)
        path.write_text(area, encoding="utf-8")
    import pytest
    with pytest.raises(ValueError, match="outside"):
        snapshot_project(repo, "demo", repo / "snapshot")


def test_reset_can_preserve_user_supplied_datasheet_incoming(tmp_path: Path):
    repo = tmp_path / "repo"
    project_id = "demo"
    project = repo / "projects" / project_id
    project.mkdir(parents=True)
    for area in ("requirements", "connections"):
        path = repo / area / f"{project_id}.md"
        path.parent.mkdir(parents=True)
        path.write_text(area, encoding="utf-8")
    incoming = repo / "hardware" / "datasheets" / "incoming" / project_id
    incoming.mkdir(parents=True)
    pdf = incoming / "sensor.pdf"
    pdf.write_bytes(b"%PDF-user-supplied")
    snapshot = tmp_path / "snapshots" / "demo"

    snapshot_project(repo, project_id, snapshot)
    result = reset_project(
        repo,
        project_id,
        snapshot,
        preserve_datasheet_incoming=True,
    )

    assert result["datasheet_incoming_preserved"] is True
    assert "datasheet-incoming" not in result["detached"]
    assert pdf.read_bytes() == b"%PDF-user-supplied"


def test_reset_refuses_state_changed_after_snapshot(tmp_path: Path):
    import pytest

    repo = tmp_path / "repo"
    project = repo / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "source.c").write_text("before", encoding="utf-8")
    for area in ("requirements", "connections"):
        path = repo / area / "demo.md"
        path.parent.mkdir(parents=True)
        path.write_text(area, encoding="utf-8")
    output = tmp_path / "snapshots" / "demo"
    snapshot_project(repo, "demo", output)
    (project / "source.c").write_text("after", encoding="utf-8")
    with pytest.raises(ValueError, match="state changed"):
        reset_project(repo, "demo", output)
    assert project.is_dir()


def test_snapshot_manifest_uses_the_same_path_order_as_reset_comparison(tmp_path: Path):
    repo = tmp_path / "repo"
    project_id = "ordered"
    project = repo / "projects" / project_id
    project.mkdir(parents=True)
    for area in ("requirements", "connections"):
        path = repo / area / f"{project_id}.md"
        path.parent.mkdir(parents=True)
        path.write_text(area, encoding="utf-8")
    runtime = ProjectRuntime(repo, project_id).ensure()
    (project / "nested" / "z").mkdir(parents=True)
    (project / "nested" / "z" / "one.txt").write_text("one", encoding="utf-8")
    (runtime.root / "nested" / "a").mkdir(parents=True)
    (runtime.root / "nested" / "a" / "two.txt").write_text("two", encoding="utf-8")
    output = tmp_path / "snapshots" / "ordered"
    manifest = snapshot_project(repo, project_id, output)
    paths = [item["path"] for item in json.loads((output / "snapshot-manifest.json").read_text(encoding="utf-8"))["files"]]
    assert paths == sorted(paths)
    assert manifest["project_id"] == project_id


def test_snapshot_reclaims_a_stale_runner_lock(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    project = repo / "projects" / "demo"
    project.mkdir(parents=True)
    for area in ("requirements", "connections"):
        path = repo / area / "demo.md"
        path.parent.mkdir(parents=True)
        path.write_text(area, encoding="utf-8")
    runtime = ProjectRuntime(repo, "demo").ensure()
    runtime.runner_lock.write_text('{"pid": 99999, "token": "stale"}', encoding="utf-8")
    monkeypatch.setattr("orchestrator.project_lifecycle._pid_is_alive", lambda _pid: False)

    snapshot_project(repo, "demo", tmp_path / "snapshots" / "demo")

    assert not runtime.runner_lock.exists()
