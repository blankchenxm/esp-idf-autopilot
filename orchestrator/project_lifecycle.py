from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .datasheet_library import project_alias_path, rebuild_datasheet_index
from .runtime_paths import ProjectRuntime
from .storage import atomic_write_json


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _file_records(root: Path, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    excluded = exclude or set()
    records: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        data = path.read_bytes()
        records.append({
            "path": relative,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        })
    return records


def _verify_snapshot(snapshot: Path, project_id: str | None = None) -> dict[str, Any]:
    manifest_path = snapshot / "snapshot-manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"snapshot manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "1.0":
        raise ValueError("unsupported project snapshot schema")
    if project_id and manifest.get("project_id") != project_id:
        raise ValueError("snapshot project_id does not match requested project")
    for record in manifest.get("files", []):
        path = (snapshot / record["path"]).resolve()
        try:
            path.relative_to(snapshot.resolve())
        except ValueError as exc:
            raise ValueError("snapshot file path escapes snapshot root") from exc
        if not path.is_file():
            raise FileNotFoundError(f"snapshot file is missing: {record['path']}")
        data = path.read_bytes()
        if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError(f"snapshot integrity check failed: {record['path']}")
    return manifest


def _input_records(repo_root: Path, project_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for area in ("requirements", "connections"):
        path = repo_root / area / f"{project_id}.md"
        if not path.is_file():
            raise FileNotFoundError(f"project input is missing: {path}")
        data = path.read_bytes()
        records.append({
            "path": path.relative_to(repo_root).as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        })
    return records


def _current_state_records(repo_root: Path, project_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    roots = [
        ("project", repo_root / "projects" / project_id),
        ("runtime", ProjectRuntime(repo_root, project_id).root),
        (
            "datasheet-incoming",
            repo_root / "hardware" / "datasheets" / "incoming" / project_id,
        ),
    ]
    for prefix, root in roots:
        if not root.is_dir():
            continue
        for record in _file_records(root):
            records.append({**record, "path": f"{prefix}/{record['path']}"})
    alias = project_alias_path(repo_root, project_id)
    if alias.is_file():
        data = alias.read_bytes()
        records.append({
            "path": "datasheet-alias.json",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        })
    return sorted(records, key=lambda item: item["path"])


def _pid_is_alive(pid: int) -> bool:
    """Check a Windows PID without treating a stale runner lock as live."""
    if pid <= 0:
        return False
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0 and f'"{pid}"' in result.stdout


def _require_inactive(repo_root: Path, project_id: str) -> None:
    lock = ProjectRuntime(repo_root, project_id).runner_lock
    if not lock.exists():
        return
    try:
        owner = json.loads(lock.read_text(encoding="utf-8"))
        pid = int(owner.get("pid", 0))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"project lifecycle operation refused: runner lock is malformed: {lock}"
        ) from exc
    if _pid_is_alive(pid):
        raise RuntimeError(
            f"project lifecycle operation refused while a runner is active: {lock} (pid {pid})"
        )
    # Match the runner's own stale-lock reclaim policy.  A lifecycle command
    # cannot safely proceed behind a live graph writer, but an interrupted
    # process must not permanently prevent snapshot/reset recovery.
    lock.unlink(missing_ok=True)


def snapshot_project(repo_root: Path, project_id: str, output: Path) -> dict[str, Any]:
    """Create a portable, integrity-bound project/control snapshot.

    User-owned requirement/connection files are hash-bound but not copied,
    because they may contain explicitly authorized local credentials.
    Shared Datasheet objects are referenced by the copied project manifest and
    remain in the repository-wide content store.
    """
    repo_root = repo_root.resolve()
    output = output.resolve()
    if _inside(output, repo_root):
        raise ValueError("project snapshots must be stored outside the repository")
    if output.exists():
        raise FileExistsError(f"snapshot output already exists: {output}")
    project_dir = repo_root / "projects" / project_id
    if not project_dir.is_dir():
        raise FileNotFoundError(f"project directory is missing: {project_dir}")
    _require_inactive(repo_root, project_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex[:12]}"
    staging.mkdir()
    try:
        shutil.copytree(project_dir, staging / "project")
        runtime = ProjectRuntime(repo_root, project_id)
        if runtime.root.is_dir():
            shutil.copytree(runtime.root, staging / "runtime")
        incoming = (
            repo_root / "hardware" / "datasheets" / "incoming" / project_id
        )
        if incoming.is_dir():
            shutil.copytree(incoming, staging / "datasheet-incoming")
        alias = project_alias_path(repo_root, project_id)
        if alias.is_file():
            shutil.copy2(alias, staging / "datasheet-alias.json")
        manifest = {
            "schema_version": "1.0",
            "project_id": project_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "inputs": _input_records(repo_root, project_id),
            "shared_datasheets_copied": False,
            # Reset compares this list with `_current_state_records`, which
            # is path-sorted across project/runtime roots.  `Path.rglob()`
            # order is filesystem-dependent on Windows, so normalize the
            # snapshot manifest to the same stable path order.
            "files": sorted(_file_records(staging), key=lambda item: item["path"]),
        }
        atomic_write_json(staging / "snapshot-manifest.json", manifest)
        os.replace(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "project_id": project_id,
        "snapshot": str(output),
        "files": len(manifest["files"]),
        "warning": "snapshot may contain ignored local private artifacts; keep it private",
    }


def reset_project(
    repo_root: Path,
    project_id: str,
    snapshot: Path,
    *,
    preserve_datasheet_incoming: bool = False,
) -> dict[str, Any]:
    """Recoverably detach project/runtime state after verifying a snapshot.

    ``preserve_datasheet_incoming`` retains user-supplied PDFs for a fresh
    Design run while still snapshotting them for recovery.
    """
    repo_root = repo_root.resolve()
    snapshot = snapshot.resolve()
    manifest = _verify_snapshot(snapshot, project_id)
    _require_inactive(repo_root, project_id)
    current_inputs = _input_records(repo_root, project_id)
    if current_inputs != manifest.get("inputs"):
        raise ValueError("user-owned inputs changed after snapshot; refusing project reset")
    if _current_state_records(repo_root, project_id) != manifest.get("files"):
        raise ValueError("project/runtime state changed after snapshot; create a fresh snapshot")
    project_dir = repo_root / "projects" / project_id
    runtime = ProjectRuntime(repo_root, project_id)
    alias = project_alias_path(repo_root, project_id)
    incoming = repo_root / "hardware" / "datasheets" / "incoming" / project_id
    detached = snapshot / "detached-original"
    if detached.exists():
        raise FileExistsError(f"snapshot already contains detached originals: {detached}")
    detached.mkdir()
    moved: list[str] = []
    try:
        if project_dir.exists():
            shutil.move(str(project_dir), str(detached / "project"))
            moved.append("project")
        if runtime.root.exists():
            shutil.move(str(runtime.root), str(detached / "runtime"))
            moved.append("runtime")
        if incoming.exists() and not preserve_datasheet_incoming:
            shutil.move(str(incoming), str(detached / "datasheet-incoming"))
            moved.append("datasheet-incoming")
        if alias.exists():
            shutil.move(str(alias), str(detached / "datasheet-alias.json"))
            moved.append("datasheet-alias.json")
        atomic_write_json(detached / "reset-receipt.json", {
            "schema_version": "1.0",
            "project_id": project_id,
            "reset_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_manifest_sha256": hashlib.sha256(
                (snapshot / "snapshot-manifest.json").read_bytes()
            ).hexdigest(),
            "moved": moved,
        })
    except Exception:
        # Restore only paths moved by this invocation.
        if (detached / "datasheet-alias.json").exists() and not alias.exists():
            alias.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(detached / "datasheet-alias.json"), str(alias))
        if (detached / "runtime").exists() and not runtime.root.exists():
            runtime.root.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(detached / "runtime"), str(runtime.root))
        if (detached / "datasheet-incoming").exists() and not incoming.exists():
            incoming.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(detached / "datasheet-incoming"), str(incoming))
        if (detached / "project").exists() and not project_dir.exists():
            project_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(detached / "project"), str(project_dir))
        raise
    rebuild_datasheet_index(repo_root)
    return {
        "project_id": project_id,
        "snapshot": str(snapshot),
        "detached": moved,
        "datasheet_incoming_preserved": preserve_datasheet_incoming and incoming.exists(),
        "inputs_preserved": [item["path"] for item in current_inputs],
    }


def restore_project(repo_root: Path, project_id: str, snapshot: Path) -> dict[str, Any]:
    """Restore a snapshot without overwriting current project/control state."""
    repo_root = repo_root.resolve()
    snapshot = snapshot.resolve()
    manifest = _verify_snapshot(snapshot, project_id)
    if _input_records(repo_root, project_id) != manifest.get("inputs"):
        raise ValueError("current user-owned inputs do not match the snapshot")
    project_dir = repo_root / "projects" / project_id
    runtime = ProjectRuntime(repo_root, project_id)
    alias = project_alias_path(repo_root, project_id)
    incoming = repo_root / "hardware" / "datasheets" / "incoming" / project_id
    for path, label in (
        (project_dir, "project"),
        (runtime.root, "runtime"),
        (incoming, "project Datasheet incoming area"),
        (alias, "datasheet alias"),
    ):
        if path.exists():
            raise FileExistsError(f"cannot restore over existing {label}: {path}")
    project_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(snapshot / "project", project_dir)
    if (snapshot / "runtime").is_dir():
        runtime.root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(snapshot / "runtime", runtime.root)
    if (snapshot / "datasheet-incoming").is_dir():
        incoming.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(snapshot / "datasheet-incoming", incoming)
    if (snapshot / "datasheet-alias.json").is_file():
        alias.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot / "datasheet-alias.json", alias)
    rebuild_datasheet_index(repo_root)
    return {
        "project_id": project_id,
        "restored_from": str(snapshot),
        "project": str(project_dir),
        "runtime": str(runtime.root) if runtime.root.exists() else None,
        "datasheet_incoming": str(incoming) if incoming.exists() else None,
    }
