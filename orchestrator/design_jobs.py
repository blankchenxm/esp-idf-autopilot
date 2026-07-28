from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .runtime_paths import ProjectRuntime
from .storage import atomic_write_json


TERMINAL_MODES = {"WAITING_DESIGN_INPUT", "WAITING_SPEC", "BLOCKED", "FAULTED"}


def _input_fingerprint(repo_root: Path, project: str) -> str:
    import hashlib

    value = hashlib.sha256()
    for area in ("requirements", "connections"):
        path = repo_root / area / f"{project}.md"
        value.update(area.encode("utf-8"))
        value.update(path.read_bytes())
    return value.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
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
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _read(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"design job record is malformed: {path}")
    return value


def _write(runtime: ProjectRuntime, record: dict) -> None:
    job_id = str(record["job_id"])
    atomic_write_json(runtime.design_jobs / f"{job_id}.json", record)
    active = _read(runtime.active_design_job)
    if not active or active.get("job_id") == job_id:
        atomic_write_json(runtime.active_design_job, record)


def update_design_job_progress(
    repo_root: Path, project: str, job_id: str, phase: str
) -> None:
    """Persist a small, observable design-job phase timeline.

    This is job metadata, never a graph checkpoint or design fact.  It makes
    long provider/deep-reader calls diagnosable while the durable graph keeps
    ownership of control state.
    """
    runtime = ProjectRuntime(repo_root, project).ensure()
    path = runtime.design_jobs / f"{job_id}.json"
    # Direct graph/unit-test invocations do not necessarily originate from
    # the durable job launcher.  They still exercise graph semantics, but
    # have no job record to project progress into.
    if not path.is_file():
        return
    current = _read(path)
    now = _now()
    history = list(current.get("phase_history") or [])
    if history and history[-1].get("phase") == phase:
        return
    if history and not history[-1].get("finished_at"):
        history[-1] = {**history[-1], "finished_at": now}
    history.append({"phase": phase, "started_at": now})
    _write(runtime, {**current, "phase": phase, "phase_history": history})


@contextmanager
def _launch_lock(runtime: ProjectRuntime) -> Iterator[None]:
    """Serialize the short inspect/create transaction, never the worker."""
    path = runtime.locks / "design-launch.lock"
    payload = {"pid": os.getpid(), "token": uuid.uuid4().hex}
    for _ in range(100):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            break
        except FileExistsError:
            existing = _read(path)
            if not _pid_is_alive(int(existing.get("pid", 0))):
                path.unlink(missing_ok=True)
                continue
            time.sleep(0.05)
    else:
        raise RuntimeError(f"timed out acquiring design launch lock: {path}")
    try:
        yield
    finally:
        try:
            existing = _read(path)
            if existing.get("token") == payload["token"]:
                path.unlink(missing_ok=True)
        except (FileNotFoundError, json.JSONDecodeError):
            pass


def read_design_job(repo_root: Path, project: str) -> dict:
    runtime = ProjectRuntime(repo_root, project).ensure()
    record = _read(runtime.active_design_job)
    if not record:
        return {}
    if record.get("mode") == "DESIGN_RUNNING" and not _pid_is_alive(int(record.get("pid", 0))):
        booted = bool(record.get("worker_started_at"))
        record = {
            **record,
            "mode": "BLOCKED",
            "kind": "interrupted" if booted else "worker_boot",
            "summary": (
                "design worker exited after completing its launch handshake"
                if booted
                else "design worker exited before completing its launch handshake"
            ),
            "interruption_count": int(record.get("interruption_count") or 0) + 1,
            "evidence": record.get("log"),
            "finished_at": _now(),
        }
        _write(runtime, record)
    return record


def _worker_command(project: str, revision: int | None, job_id: str) -> list[str]:
    command = [
        sys.executable,
        "-u",
        "-m",
        "orchestrator.cli",
        "_design-worker",
        "--project",
        project,
        "--job-id",
        job_id,
    ]
    if revision is not None:
        command.extend(["--revision", str(revision)])
    return command


def _spawn_worker(repo_root: Path, runtime: ProjectRuntime, record: dict) -> subprocess.Popen[str]:
    log_path = Path(record["log"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a", encoding="utf-8")
    kwargs: dict = {
        "cwd": repo_root,
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "text": True,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        requested_revision = record.get(
            "requested_revision", record.get("revision")
        )
        return subprocess.Popen(
            _worker_command(
                str(record["project"]),
                requested_revision,
                str(record["job_id"]),
            ),
            **kwargs,
        )
    finally:
        log.close()


def launch_or_observe_design(repo_root: Path, project: str, revision: int | None = None) -> dict:
    for area in ("requirements", "connections"):
        source = repo_root / area / f"{project}.md"
        if not source.is_file():
            raise FileNotFoundError(f"required user input is missing: {source}")
    runtime = ProjectRuntime(repo_root, project).ensure()
    input_fingerprint = _input_fingerprint(repo_root, project)
    with _launch_lock(runtime):
        current = read_design_job(repo_root, project)
        if current.get("mode") == "DESIGN_RUNNING":
            return current
        if current.get("mode") == "WAITING_SPEC":
            design_dir = Path(str(current.get("design_dir", "")))
            same_revision = revision is None or current.get("revision") == revision
            same_inputs = current.get("input_fingerprint") == input_fingerprint
            if design_dir.is_dir() and same_revision and same_inputs:
                return current
        if current.get("mode") == "WAITING_DESIGN_INPUT":
            if current.get("input_fingerprint") == input_fingerprint:
                return current
        if (
            current.get("mode") == "BLOCKED"
            and current.get("kind") in {"interrupted", "worker_boot"}
            and current.get("input_fingerprint") == input_fingerprint
            and int(current.get("interruption_count") or 0) >= 3
        ):
            stalled = {
                **current,
                "mode": "FAULTED",
                "kind": "worker_stall",
                "summary": (
                    "design worker exited three times without a material input "
                    "change; automatic recovery is exhausted"
                ),
            }
            _write(runtime, stalled)
            return stalled
        if (
            current.get("mode") == "BLOCKED"
            and current.get("kind") in {"interrupted", "worker_boot"}
            and current.get("input_fingerprint") == input_fingerprint
            and int(current.get("interruption_count") or 0) < 3
        ):
            restarting = {
                **current,
                "mode": "DESIGN_STARTING",
                "pid": None,
                "finished_at": None,
                "resumed_at": _now(),
                "worker_started_at": None,
                "phase": "worker_boot",
                "kind": None,
                "summary": None,
                "evidence": None,
            }
            _write(runtime, restarting)
            try:
                process = _spawn_worker(repo_root, runtime, restarting)
            except Exception as exc:
                failed = {
                    **restarting,
                    "mode": "FAULTED",
                    "kind": "launch",
                    "summary": f"{type(exc).__name__}: {exc}",
                    "finished_at": _now(),
                }
                _write(runtime, failed)
                return failed
            running = {
                **restarting,
                "mode": "DESIGN_RUNNING",
                "pid": process.pid,
            }
            _write(runtime, running)
            return running
        job_id = uuid.uuid4().hex
        record = {
            "schema_version": "1.0",
            "mode": "DESIGN_STARTING",
            "project": project,
            "requested_revision": revision,
            "revision": None,
            "job_id": job_id,
            "pid": None,
            "started_at": _now(),
            "finished_at": None,
            "input_fingerprint": input_fingerprint,
            "log": str(runtime.design_jobs / f"{job_id}.log"),
        }
        atomic_write_json(runtime.design_jobs / f"{job_id}.json", record)
        atomic_write_json(runtime.active_design_job, record)
        try:
            process = _spawn_worker(repo_root, runtime, record)
        except Exception as exc:
            failed = {
                **record,
                "mode": "FAULTED",
                "kind": "launch",
                "summary": f"{type(exc).__name__}: {exc}",
                "finished_at": _now(),
            }
            _write(runtime, failed)
            return failed
        running = {**record, "mode": "DESIGN_RUNNING", "pid": process.pid}
        _write(runtime, running)
        return running


def await_design_job_started(repo_root: Path, project: str, job_id: str) -> dict:
    """Prevent a fast worker from racing the launcher's PID publication."""
    runtime = ProjectRuntime(repo_root, project).ensure()
    path = runtime.design_jobs / f"{job_id}.json"
    for _ in range(100):
        record = _read(path)
        # Windows virtual-environment launchers may create a short-lived
        # shim process and then replace it with the actual Python worker.  In
        # that case Popen.pid is not the worker PID, although the job record
        # already proves this worker owns the same launch.  Bind the durable
        # record to the real worker instead of faulting before Design starts.
        if record.get("mode") == "DESIGN_RUNNING" and record.get("job_id") == job_id:
            record = {
                **record,
                "pid": os.getpid(),
                "worker_started_at": _now(),
                "phase": "initialize",
                "kind": None,
                "summary": None,
                "evidence": None,
            }
            _write(runtime, record)
            return record
        time.sleep(0.05)
    raise RuntimeError(f"design worker launch handshake did not complete: {job_id}")


def finish_design_job(
    repo_root: Path,
    project: str,
    job_id: str,
    design_dir: Path | None = None,
    graph_result: dict | None = None,
) -> dict:
    runtime = ProjectRuntime(repo_root, project).ensure()
    current = _read(runtime.design_jobs / f"{job_id}.json")
    terminal = graph_result or {}
    mode = str(terminal.get("mode") or "WAITING_SPEC")
    resolved_design_dir = design_dir or (
        Path(str(terminal["design_dir"])) if terminal.get("design_dir") else None
    )
    requested_revision = current.get(
        "requested_revision", current.get("revision")
    )
    resolved_revision = None
    if resolved_design_dir and resolved_design_dir.name.startswith("rev-"):
        try:
            resolved_revision = int(resolved_design_dir.name.removeprefix("rev-"))
        except ValueError:
            pass
    result = {
        **current,
        "mode": mode,
        "pid": os.getpid(),
        "requested_revision": requested_revision,
        "revision": resolved_revision,
        "design_dir": str(resolved_design_dir or "") or None,
        "spec": str(
            (design_dir / "spec.md")
            if design_dir is not None
            else terminal.get("spec") or ""
        )
        or None,
        "kind": (
            "design_input"
            if mode == "WAITING_DESIGN_INPUT"
            else ("internal_fault" if mode == "FAULTED" else None)
        ),
        "summary": terminal.get("summary") or None,
        "finished_at": _now(),
    }
    _write(runtime, result)
    return result


def fail_design_job(repo_root: Path, project: str, job_id: str, exc: Exception) -> dict:
    runtime = ProjectRuntime(repo_root, project).ensure()
    current = _read(runtime.design_jobs / f"{job_id}.json")
    evidence = str(getattr(exc, "staging_dir", "") or "")
    graph_state: dict = {}
    if not evidence:
        try:
            from .design_graph import inspect_design_graph_state

            graph_state = inspect_design_graph_state(repo_root, project, job_id)
            for key in (
                "errors_path",
                "grounding_providers_path",
                "grounded_contract_path",
                "draft_path",
                "staging_root",
            ):
                candidate = str(graph_state.get(key) or "")
                if candidate and Path(candidate).exists():
                    evidence = candidate
                    break
        except Exception:
            # Preserve the original worker failure even if checkpoint
            # inspection itself is unavailable or corrupt.
            graph_state = {}
    result = {
        **current,
        "mode": "FAULTED",
        "pid": os.getpid(),
        "kind": "internal_fault",
        "summary": f"{type(exc).__name__}: {exc}",
        "phase": graph_state.get("phase") or current.get("phase"),
        "evidence": evidence or None,
        "finished_at": _now(),
    }
    _write(runtime, result)
    return result
