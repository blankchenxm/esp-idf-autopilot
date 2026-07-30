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
from typing import Any

from .codex_runner import background_creationflags, hidden_startupinfo
from .runtime_paths import ProjectRuntime
from .storage import atomic_write_json, digest


TERMINAL_MODES = {
    "WAITING_SPEC",
    "WAITING_TIER_C",
    "BLOCKED",
    "FAULTED",
    "COMPLETE",
    "PAUSED",
    "INTERRUPTED",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"execution job record is malformed: {path}")
    return value


def _write(runtime: ProjectRuntime, record: dict[str, Any]) -> None:
    path = runtime.execution_jobs / f"{record['job_id']}.json"
    previous = _read(path)
    atomic_write_json(path, record)
    active = _read(runtime.active_execution_job)
    if not active or active.get("job_id") == record["job_id"]:
        atomic_write_json(runtime.active_execution_job, record)
    observed = ("mode", "cursor", "phase", "next_action", "summary")
    if any(previous.get(key) != record.get(key) for key in observed):
        from .control_events import publish_control_event

        publish_control_event(
            runtime.repo_root,
            runtime.project_id,
            source="execution_job",
            mode=str(record.get("mode") or "UNKNOWN"),
            reason=next((
                key for key in observed
                if previous.get(key) != record.get(key)
            ), "state_change"),
            payload={
                "job_id": record.get("job_id"),
                "run_id": record.get("run_id"),
                "cursor": record.get("cursor"),
                "phase": record.get("phase"),
                "next_action": record.get("next_action"),
            },
        )


def _pid_is_alive(pid: int) -> bool:
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


@contextmanager
def _launch_lock(runtime: ProjectRuntime):
    path = runtime.locks / "execution-launch.lock"
    token = uuid.uuid4().hex
    payload = {"pid": os.getpid(), "token": token}
    for _ in range(100):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            break
        except FileExistsError:
            existing = _read(path)
            if not _pid_is_alive(int(existing.get("pid") or 0)):
                path.unlink(missing_ok=True)
                continue
            time.sleep(0.05)
    else:
        raise RuntimeError(f"timed out acquiring execution launch lock: {path}")
    try:
        yield
    finally:
        existing = _read(path)
        if existing.get("token") == token:
            path.unlink(missing_ok=True)


def reconcile_worker_state(
    runtime: ProjectRuntime,
    record: dict[str, Any],
) -> dict[str, Any]:
    """Atomically reconcile active pointer, job record, PID and projections."""
    original_pointer = dict(record)
    reconciliation_reasons: list[str] = []
    if record.get("job_id"):
        job_record = _read(
            runtime.execution_jobs / f"{record['job_id']}.json"
        )
        if job_record and job_record != record:
            record = job_record
            atomic_write_json(runtime.active_execution_job, record)
            reconciliation_reasons.append("stale_active_job_projection")
    elif not record:
        candidates = []
        for path in sorted(runtime.execution_jobs.glob("*.json")):
            value = _read(path)
            if value.get("mode") in {"STARTING", "CONTINUOUS"}:
                candidates.append(value)
        if len(candidates) == 1:
            record = candidates[0]
            atomic_write_json(runtime.active_execution_job, record)
            reconciliation_reasons.append("missing_active_job_pointer")
    if (
        record.get("mode") == "CONTINUOUS"
        and not _pid_is_alive(int(record.get("pid") or 0))
    ):
        record = {
            **record,
            "mode": "INTERRUPTED",
            "kind": "worker_interrupted",
            "summary": (
                "execution worker exited before a terminal checkpoint; "
                "launch or resume will continue the durable thread"
            ),
            "finished_at": _now(),
        }
        _write(runtime, record)
        reconciliation_reasons.append("dead_nonterminal_worker")
    if reconciliation_reasons:
        active_thread = _read(runtime.active_thread)
        projection = _read(
            runtime.repo_root / "projects" / runtime.project_id
            / "execution" / "run-state.json"
        )
        reconciliation = {
            "schema_version": "1.0",
            "project": runtime.project_id,
            "job_id": record.get("job_id"),
            "mode": record.get("mode"),
            "reasons": reconciliation_reasons,
            "prior_active_pointer": original_pointer,
            "checkpoint_database_present": runtime.checkpoints.is_file(),
            "active_thread": active_thread,
            "projection": projection,
            "created_at": _now(),
        }
        reconciliation["reconciliation_id"] = digest({
            key: value for key, value in reconciliation.items()
            if key not in {"created_at", "reconciliation_id"}
        })
        atomic_write_json(
            runtime.worker_reconciliations
            / f"{reconciliation['reconciliation_id']}.json",
            reconciliation,
        )
    return record


def read_execution_job(
    repo_root: Path, project: str
) -> dict[str, Any]:
    runtime = ProjectRuntime(repo_root, project).ensure()
    return reconcile_worker_state(
        runtime, _read(runtime.active_execution_job)
    )


def _spawn(
    repo_root: Path, runtime: ProjectRuntime, record: dict[str, Any]
) -> subprocess.Popen[str]:
    log_path = Path(record["log"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a", encoding="utf-8")
    command = [
        sys.executable,
        "-u",
        "-m",
        "orchestrator.cli",
        "_execution-worker",
        "--project",
        str(record["project"]),
        "--job-id",
        str(record["job_id"]),
    ]
    kwargs: dict[str, Any] = {
        "cwd": repo_root,
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "text": True,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            background_creationflags()
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )
        kwargs["startupinfo"] = hidden_startupinfo()
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(command, **kwargs)
    finally:
        log.close()


def launch_execution_job(
    repo_root: Path,
    project: str,
    *,
    revision: int,
    intent: str,
    port: str | None,
    baud: int,
    baseline: str | None,
    resume_value: Any = None,
    restart: bool = False,
    thread_id: str | None = None,
) -> dict[str, Any]:
    runtime = ProjectRuntime(repo_root, project).ensure()
    with _launch_lock(runtime):
        current = read_execution_job(repo_root, project)
        if current.get("mode") == "CONTINUOUS":
            return current
        job_id = uuid.uuid4().hex
        record = {
            "schema_version": "1.0",
            "job_id": job_id,
            "project": project,
            "revision": revision,
            "intent": intent,
            "port": port,
            "baud": baud,
            "baseline": baseline,
            "resume_value": resume_value,
            "restart": restart,
            "thread_id": thread_id,
            "mode": "STARTING",
            "pid": None,
            "started_at": _now(),
            "finished_at": None,
            "log": str(runtime.execution_jobs / f"{job_id}.log"),
        }
        _write(runtime, record)
        try:
            process = _spawn(repo_root, runtime, record)
        except Exception as exc:
            failed = {
                **record,
                "mode": "FAULTED",
                "kind": "worker_launch",
                "summary": f"{type(exc).__name__}: {exc}",
                "finished_at": _now(),
            }
            _write(runtime, failed)
            return failed
        running = {**record, "mode": "CONTINUOUS", "pid": process.pid}
        _write(runtime, running)
        return running


def execution_worker_request(
    repo_root: Path, project: str, job_id: str
) -> dict[str, Any]:
    runtime = ProjectRuntime(repo_root, project).ensure()
    path = runtime.execution_jobs / f"{job_id}.json"
    for _ in range(100):
        record = _read(path)
        if not record:
            raise FileNotFoundError(f"execution job is missing: {job_id}")
        if (
            record.get("mode") == "CONTINUOUS"
            and int(record.get("pid") or 0) == os.getpid()
        ):
            running = {**record, "worker_started_at": _now()}
            _write(runtime, running)
            return running
        time.sleep(0.05)
    raise RuntimeError(
        f"execution worker launch handshake did not complete: {job_id}"
    )


def finish_execution_job(
    repo_root: Path,
    project: str,
    job_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    runtime = ProjectRuntime(repo_root, project).ensure()
    current = _read(runtime.execution_jobs / f"{job_id}.json")
    finished = {
        **current,
        "mode": str(result.get("mode") or "FAULTED"),
        "pid": os.getpid(),
        "cursor": result.get("cursor"),
        "next_action": result.get("next_action"),
        "run_id": result.get("run_id"),
        "summary": (
            (result.get("blocker") or {}).get("summary")
            if isinstance(result.get("blocker"), dict)
            else None
        ),
        "finished_at": _now(),
    }
    _write(runtime, finished)
    return finished


def fail_execution_job(
    repo_root: Path,
    project: str,
    job_id: str,
    exc: Exception,
) -> dict[str, Any]:
    runtime = ProjectRuntime(repo_root, project).ensure()
    current = _read(runtime.execution_jobs / f"{job_id}.json")
    failed = {
        **current,
        "mode": "FAULTED",
        "pid": os.getpid(),
        "kind": "internal_fault",
        "summary": f"{type(exc).__name__}: {exc}",
        "finished_at": _now(),
    }
    _write(runtime, failed)
    return failed
