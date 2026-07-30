from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .runtime_paths import ProjectRuntime


REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR = REPO_ROOT / ".vendor"
if VENDOR.is_dir() and str(VENDOR) not in sys.path: sys.path.insert(0, str(VENDOR))


def _project_arg(parser: argparse.ArgumentParser, positional: bool = False) -> None:
    if positional: parser.add_argument("project")
    else: parser.add_argument("--project", required=True)


def _runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--revision", type=int)
    parser.add_argument("--port")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--preflight-baseline")
    parser.add_argument("--restart", action="store_true", help="begin a new checkpoint thread after an evidenced terminal blocker")
    parser.add_argument("--thread-id", help="resume a specific existing checkpoint thread for this project/revision")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ESP-IDF AutoDev design and execution harness")
    sub = parser.add_subparsers(dest="command", required=True)
    design = sub.add_parser("design", help="compile any matching requirements/connections pair into rev-NNNN")
    _project_arg(design); design.add_argument("--revision", type=int)
    design_worker = sub.add_parser("_design-worker", help=argparse.SUPPRESS)
    _project_arg(design_worker); design_worker.add_argument("--revision", type=int); design_worker.add_argument("--job-id", required=True)
    execution_worker = sub.add_parser("_execution-worker", help=argparse.SUPPRESS)
    _project_arg(execution_worker); execution_worker.add_argument("--job-id", required=True)
    validate = sub.add_parser("validate"); _project_arg(validate); validate.add_argument("--revision", type=int)
    start = sub.add_parser("start"); _project_arg(start); _runtime_args(start)
    resume = sub.add_parser("resume"); _project_arg(resume); _runtime_args(resume)
    resume.add_argument("--approve", action="store_true")
    resume.add_argument("--tier-c-json")
    rotate = sub.add_parser("rotate-credentials"); _project_arg(rotate); rotate.add_argument("--revision", type=int); rotate.add_argument("--approve", action="store_true")
    pause = sub.add_parser("pause", help="persist an explicit user pause at the next safe graph node")
    _project_arg(pause); pause.add_argument("--revision", type=int); pause.add_argument("--reason", default="explicit user pause")
    status = sub.add_parser("status"); _project_arg(status); status.add_argument("--revision", type=int)
    archive = sub.add_parser("archive-runs", help="compress historical failed-run raw artifacts")
    _project_arg(archive)
    migrate = sub.add_parser("migrate-datasheets", help="deduplicate project component datasheets")
    _project_arg(migrate); migrate.add_argument("--revision", type=int)
    reconcile = sub.add_parser("reconcile-state", help="rebuild a terminal projection and checkpoint pointer")
    _project_arg(reconcile); reconcile.add_argument("--revision", type=int); reconcile.add_argument("--thread-id")
    correct = sub.add_parser("invalidate-evidence", help="append an immutable evidence correction")
    _project_arg(correct); correct.add_argument("--evidence-id", action="append", required=True); correct.add_argument("--reason", required=True)
    snapshot = sub.add_parser("snapshot-project", help="export one project's source/facts/control state")
    _project_arg(snapshot); snapshot.add_argument("--output", type=Path, required=True)
    reset = sub.add_parser("reset-project", help="recoverably detach a verified project snapshot")
    _project_arg(reset); reset.add_argument("--snapshot", type=Path, required=True)
    reset.add_argument("--preserve-datasheet-incoming", action="store_true")
    restore = sub.add_parser("restore-project", help="restore a project snapshot without overwriting")
    _project_arg(restore); restore.add_argument("--snapshot", type=Path, required=True)
    revise = sub.add_parser("prepare-revision"); _project_arg(revise)
    revise.add_argument("--from-revision", type=int, required=True); revise.add_argument("--to-revision", type=int, required=True)
    # Backward-compatible aliases used by the first MVP.
    old_run = sub.add_parser("run"); _project_arg(old_run, positional=True); old_run.add_argument("--intent", choices=("new", "resume", "revision"), default="new"); _runtime_args(old_run)
    old_validate = sub.add_parser("validate-design"); _project_arg(old_validate, positional=True); old_validate.add_argument("--revision", type=int, default=1)
    return parser


def _project_dir(project: str) -> Path:
    path = (REPO_ROOT / "projects" / project).resolve()
    if path.parent != (REPO_ROOT / "projects").resolve(): raise SystemExit("invalid project name")
    return path


def _latest_revision(project_dir: Path) -> int | None:
    values = [int(path.name[4:]) for path in (project_dir / "design-package").glob("rev-[0-9][0-9][0-9][0-9]") if path.is_dir()]
    return max(values) if values else None


def _design_dir(project_dir: Path, revision: int | None) -> tuple[Path, int]:
    chosen = revision or _latest_revision(project_dir)
    if chosen is None: raise FileNotFoundError("no design revision exists; run the design command first")
    return project_dir / "design-package" / f"rev-{chosen:04d}", chosen


def _resume_input(snapshot, initial: dict, resume_value, command_type, project_dir: Path | None = None):
    """Select a graph input without ever replacing an existing checkpoint."""
    pending_interrupt = bool(snapshot.tasks and any(getattr(task, "interrupts", ()) for task in snapshot.tasks))
    existing = bool(snapshot.values)
    if existing and snapshot.values.get("mode") == "PAUSED":
        next_node = snapshot.values.get("pause_next_node")
        if not next_node:
            raise RuntimeError("paused checkpoint has no recorded resume node")
        return command_type(update={"mode": "CONTINUOUS", "pause_reason": None, "cursor": f"RESUMING:{next_node}", "next_action": f"execute {next_node}", "progress_seq": snapshot.values.get("progress_seq", 0) + 1}, goto=next_node)
    if pending_interrupt:
        return None if resume_value is None else command_type(resume=resume_value)
    if existing and snapshot.values.get("mode") in {"BLOCKED", "FAULTED"}:
        # A terminal stall is final only for its exact material.  A verified
        # source, environment, or Harness repair may safely re-enter the same
        # persisted failure boundary; unchanged material must remain blocked.
        from .policies import material_fingerprint
        if project_dir is None:
            return None
        current = material_fingerprint(project_dir, dict(snapshot.values))
        previous = snapshot.values.get("material_fingerprint")
        target = snapshot.values.get("recovery_target") or snapshot.values.get("failed_node")
        if current != previous and target:
            return command_type(
                update={"mode": "CONTINUOUS", "failure": None,
                        "diagnostic": None,
                        "blocker": None,
                        "cursor": f"RETRY:{target}:material-changed",
                        "next_action": f"retry {target} after material change",
                        "material_fingerprint": current,
                        "progress_seq": snapshot.values.get("progress_seq", 0) + 1},
                goto=target,
            )
    if existing:
        return None
    return initial


def _pid_is_alive(pid: int) -> bool:
    """Use tasklist rather than os.kill(pid, 0), which is unreliable on Windows."""
    if pid <= 0:
        return False
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=False,
    )
    return result.returncode == 0 and f'"{pid}"' in result.stdout


@contextmanager
def _runner_lock(project_dir: Path, revision: int):
    """Allow one graph invoker per project, including across interrupted CLIs.

    A LangGraph checkpoint is resumable but is not a multi-writer queue.  Two
    concurrent CLI processes can otherwise flash and open the same serial port
    while updating one thread.  A stale lock is reclaimed only after its PID is
    no longer alive; an active lock is a deterministic, retryable refusal.
    """
    path = ProjectRuntime.for_project_dir(project_dir).ensure().runner_lock
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    payload = {"pid": os.getpid(), "revision": revision, "token": token}
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            break
        except FileExistsError:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                pid = int(existing.get("pid", 0))
                if _pid_is_alive(pid):
                    raise RuntimeError(f"another Harness runner is active for {project_dir.name} (pid {pid})")
                path.unlink(missing_ok=True)
            except OSError as exc:
                # Windows reports a stale/reused-invalid PID as WinError 87
                # (rather than ProcessLookupError).  It is safe to reclaim
                # only those documented no-process forms; access-denied still
                # protects a potentially live runner.
                if getattr(exc, "winerror", None) in {3, 87}:
                    path.unlink(missing_ok=True)
                else:
                    raise
            except ValueError:
                raise RuntimeError("runner lock is malformed; preserve it for diagnosis")
    try:
        yield
    finally:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("token") == token:
                path.unlink(missing_ok=True)
        except (FileNotFoundError, json.JSONDecodeError):
            pass


_TERMINAL_THREAD_MODES = {"COMPLETE", "BLOCKED", "FAULTED", "PAUSED"}


def _thread_registry_path(project_dir: Path) -> Path:
    return ProjectRuntime.for_project_dir(project_dir).ensure().thread_registry


def _read_thread_registry(project_dir: Path) -> list[dict]:
    return ProjectRuntime.for_project_dir(project_dir).read_thread_registry()


def _record_thread(project_dir: Path, project: str, revision: int, thread_id: str, mode: str) -> None:
    """Atomically maintain the small thread index; checkpoints remain authority."""
    from .storage import atomic_write_json
    records = _read_thread_registry(project_dir)
    now = datetime.now(timezone.utc).isoformat()
    current = {
        "project": project,
        "revision": revision,
        "thread_id": thread_id,
        "mode": mode,
        "updated_at": now,
    }
    records = [item for item in records if item.get("thread_id") != thread_id]
    records.append(current)
    atomic_write_json(
        _thread_registry_path(project_dir),
        {"schema_version": "1.0", "threads": records},
    )
    atomic_write_json(
        ProjectRuntime.for_project_dir(project_dir).ensure().active_thread,
        {"schema_version": "1.1", **current},
    )


def _select_thread_id(
    project_dir: Path,
    project: str,
    revision: int,
    override: str | None,
    restart: bool,
) -> str:
    expected_prefix = f"{project}:rev-{revision:04d}"
    if override:
        if restart or (override != expected_prefix and not override.startswith(expected_prefix + ":restart-")):
            raise ValueError("--thread-id must name this project's selected revision and cannot be combined with --restart")
        return override
    if restart:
        return f"{expected_prefix}:restart-{uuid.uuid4().hex[:12]}"
    matching = [
        item for item in _read_thread_registry(project_dir)
        if item.get("project") == project and item.get("revision") == revision
    ]
    active = [item for item in matching if item.get("mode") not in _TERMINAL_THREAD_MODES]
    if len(active) > 1:
        ids = sorted(str(item.get("thread_id")) for item in active)
        raise RuntimeError(f"multiple nonterminal checkpoint threads exist; select one with --thread-id: {ids}")
    if len(active) == 1:
        return str(active[0]["thread_id"])
    if matching:
        newest = max(matching, key=lambda item: str(item.get("updated_at", "")))
        return str(newest["thread_id"])
    # Compatibility with repositories created before the registry existed.
    active_thread = ProjectRuntime.for_project_dir(project_dir).ensure().active_thread
    if active_thread.is_file():
        recorded = json.loads(active_thread.read_text(encoding="utf-8"))
        if recorded.get("project") == project and recorded.get("revision") == revision:
            return str(recorded.get("thread_id") or expected_prefix)
    return expected_prefix


def _run(project: str, revision: int, intent: str, port: str | None, baud: int, baseline: str | None, resume_value=None, restart: bool = False, thread_id_override: str | None = None) -> dict:
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command
    from .graph import build_graph
    project_dir = _project_dir(project); design_dir = project_dir / "design-package" / f"rev-{revision:04d}"
    thread_id = _select_thread_id(project_dir, project, revision, thread_id_override, restart)
    _record_thread(project_dir, project, revision, thread_id, "NEW")
    runtime = ProjectRuntime(REPO_ROOT, project).ensure()
    initial = {"project": project, "project_dir": str(project_dir), "intent": intent, "design_revision": revision, "design_dir": str(design_dir), "port": port, "baud": baud, "target": "esp32", "preflight_baseline": baseline}
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    with _runner_lock(project_dir, revision):
        with SqliteSaver.from_conn_string(str(runtime.checkpoints)) as saver:
            graph = build_graph(REPO_ROOT, saver)
            snapshot = graph.get_state(config)
            value = _resume_input(snapshot, initial, resume_value, Command, project_dir)
            reenter_recovery = (
                bool(snapshot.values)
                and not snapshot.next
                and not snapshot.tasks
                and bool(snapshot.values.get("recovery_target") or snapshot.values.get("failed_node"))
            )
            if value is not None and snapshot.values and snapshot.values.get("mode") in {"BLOCKED", "FAULTED"}:
                # A completed LangGraph run has no pending task.  ``Command``
                # cannot revive it by itself, so use the graph state API to
                # re-enter the existing recovery boundary; that boundary's
                # deterministic route schedules the original failed node.
                graph.update_state(config, value.update, as_node="recover")
                result = graph.invoke(None, config)
            elif reenter_recovery:
                from .policies import material_fingerprint
                target = snapshot.values.get("recovery_target") or snapshot.values.get("failed_node")
                current = material_fingerprint(project_dir, dict(snapshot.values))
                updates = {
                    "mode": "CONTINUOUS", "failure": None,
                    "diagnostic": None, "blocker": None,
                    "cursor": f"RETRY:{target}:resume-repair",
                    "next_action": f"retry {target} after material change",
                    "material_fingerprint": current,
                    "progress_seq": snapshot.values.get("progress_seq", 0) + 1,
                }
                graph.update_state(config, updates, as_node="recover")
                result = graph.invoke(None, config)
            elif value is None and (bool(snapshot.tasks and any(getattr(task, "interrupts", ()) for task in snapshot.tasks)) or (snapshot.values and not snapshot.next)):
                result = dict(snapshot.values)
            else:
                result = graph.invoke(value, config)
            committed = graph.get_state(config)
            if any(
                getattr(task, "interrupts", ())
                for task in committed.tasks
            ):
                projection_path = project_dir / "execution" / "run-state.json"
                if projection_path.is_file():
                    projection = json.loads(
                        projection_path.read_text(encoding="utf-8")
                    )
                    result = {
                        **result,
                        "mode": projection.get("mode", result.get("mode")),
                        "cursor": projection.get(
                            "cursor", result.get("cursor")
                        ),
                        "next_action": projection.get(
                            "next_action", result.get("next_action")
                        ),
                    }
            _record_thread(project_dir, project, revision, thread_id, str(result.get("mode", "CONTINUOUS")))
            return result


def _pause(project: str, revision: int, reason: str) -> dict:
    from langgraph.checkpoint.sqlite import SqliteSaver
    from .graph import HarnessNodes, build_graph
    project_dir = _project_dir(project)
    runtime = ProjectRuntime(REPO_ROOT, project).ensure()
    thread_ref = runtime.active_thread
    if not thread_ref.is_file():
        raise RuntimeError("no active checkpoint exists to pause")
    recorded = json.loads(thread_ref.read_text(encoding="utf-8"))
    thread_id = str(recorded.get("thread_id") or f"{project}:rev-{revision:04d}")
    config = {"configurable": {"thread_id": thread_id}}
    # Pause must serialize with graph execution.  Updating a checkpoint while
    # a live worker holds the runner lock can project PAUSED while that worker
    # continues a stale node and prevents the later resume from acquiring the
    # same lock.
    with _runner_lock(project_dir, revision):
        with SqliteSaver.from_conn_string(str(runtime.checkpoints)) as saver:
            graph = build_graph(REPO_ROOT, saver); snapshot = graph.get_state(config)
            if not snapshot.values:
                raise RuntimeError("no checkpoint state exists to pause")
            if snapshot.values.get("mode") in {"COMPLETE", "BLOCKED", "FAULTED", "PAUSED"}:
                return dict(snapshot.values)
            if any(getattr(task, "interrupts", ()) for task in snapshot.tasks):
                raise RuntimeError("checkpoint is waiting for a declared human gate; submit that response instead of pausing")
            if len(snapshot.next) != 1:
                raise RuntimeError(f"pause requires exactly one safe next node, got {list(snapshot.next)!r}")
            next_node = snapshot.next[0]
            nodes = HarnessNodes(REPO_ROOT)
            updates = nodes.pause_updates(dict(snapshot.values), next_node, reason)
            # This is LangGraph's state API, not a checkpoint-file edit.  The
            # control node has no outgoing edge, so it clears the scheduled work.
            graph.update_state(config, updates, as_node="pause_control")
            nodes.record_pause(dict(snapshot.values), updates)
            return dict(graph.get_state(config).values)


def _main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv); project = args.project; project_dir = _project_dir(project)
    if args.command == "design":
        from .design_jobs import launch_or_observe_design
        result = launch_or_observe_design(REPO_ROOT, project, args.revision)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("mode") == "BLOCKED" else 0
    if args.command == "_design-worker":
        from .design_jobs import await_design_job_started, fail_design_job, finish_design_job
        from .design_graph import run_design_graph
        try:
            await_design_job_started(REPO_ROOT, project, args.job_id)
            print(
                json.dumps(
                    {
                        "event": "design_worker_started",
                        "project": project,
                        "job_id": args.job_id,
                        "pid": os.getpid(),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            with _runner_lock(project_dir, args.revision or 0):
                graph_result = run_design_graph(
                    REPO_ROOT, project, args.job_id, revision=args.revision
                )
            result = finish_design_job(
                REPO_ROOT, project, args.job_id, graph_result=graph_result
            )
            print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
        except Exception as exc:
            result = fail_design_job(REPO_ROOT, project, args.job_id, exc)
            print(json.dumps(result, ensure_ascii=False, indent=2)); return 1
    if args.command == "_execution-worker":
        from .execution_jobs import (
            execution_worker_request,
            fail_execution_job,
            finish_execution_job,
        )
        try:
            request = execution_worker_request(
                REPO_ROOT, project, args.job_id
            )
            result = _run(
                project,
                int(request["revision"]),
                str(request["intent"]),
                request.get("port"),
                int(request.get("baud") or 115200),
                request.get("baseline"),
                request.get("resume_value"),
                bool(request.get("restart")),
                request.get("thread_id"),
            )
            finished = finish_execution_job(
                REPO_ROOT, project, args.job_id, result
            )
            print(json.dumps(finished, ensure_ascii=False, indent=2))
            return 0 if finished.get("mode") != "FAULTED" else 1
        except Exception as exc:
            failed = fail_execution_job(
                REPO_ROOT, project, args.job_id, exc
            )
            print(json.dumps(failed, ensure_ascii=False, indent=2))
            return 1
    if args.command in {"validate", "validate-design"}:
        from .validators import validate_design_package
        design_dir, _ = _design_dir(project_dir, args.revision)
        _, errors = validate_design_package(design_dir, require_approval=True)
        print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2)); return 1 if errors else 0
    if args.command == "prepare-revision":
        from .design_package import create_revision
        print(create_revision(REPO_ROOT, project, args.from_revision, args.to_revision)); return 0
    if args.command == "rotate-credentials":
        if not args.approve:
            raise ValueError("credential rotation requires explicit --approve")
        from .credential_rotation import authorize_credential_rotation
        design_dir, _ = _design_dir(project_dir, args.revision)
        print(json.dumps(authorize_credential_rotation(REPO_ROOT, project, design_dir), ensure_ascii=False, indent=2))
        return 0
    if args.command == "status":
        from .execution_jobs import read_execution_job

        execution_job = read_execution_job(REPO_ROOT, project)
        if execution_job.get("mode") == "CONTINUOUS":
            print(json.dumps(execution_job, ensure_ascii=False, indent=2))
            return 0
        path = project_dir / "execution" / "run-state.json"
        if path.exists():
            print(path.read_text(encoding="utf-8")); return 0
        from .design_jobs import read_design_job
        design_job = read_design_job(REPO_ROOT, project)
        print(json.dumps(design_job or {"status": "NOT_STARTED"}, ensure_ascii=False, indent=2)); return 0
    if args.command == "archive-runs":
        from .archive import archive_historical_failures
        print(json.dumps(archive_historical_failures(project_dir), ensure_ascii=False, indent=2)); return 0
    if args.command == "migrate-datasheets":
        from .datasheet_library import migrate_project_datasheets
        design_dir, _ = _design_dir(project_dir, args.revision)
        contract = json.loads((design_dir / "execution-contract.json").read_text(encoding="utf-8"))
        print(json.dumps(migrate_project_datasheets(REPO_ROOT, project_dir, contract), ensure_ascii=False, indent=2)); return 0
    if args.command == "invalidate-evidence":
        from .corrections import record_evidence_correction
        print(json.dumps(record_evidence_correction(project_dir, args.evidence_id, args.reason), ensure_ascii=False, indent=2)); return 0
    if args.command in {"snapshot-project", "reset-project", "restore-project"}:
        from .project_lifecycle import reset_project, restore_project, snapshot_project
        if args.command == "snapshot-project":
            result = snapshot_project(REPO_ROOT, project, args.output)
        elif args.command == "reset-project":
            result = reset_project(
                REPO_ROOT,
                project,
                args.snapshot,
                preserve_datasheet_incoming=args.preserve_datasheet_incoming,
            )
        else:
            result = restore_project(REPO_ROOT, project, args.snapshot)
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    if args.command == "reconcile-state":
        from .reconcile import reconcile_terminal_state
        design_dir, selected_revision = _design_dir(project_dir, args.revision)
        print(json.dumps(reconcile_terminal_state(REPO_ROOT, project, project_dir, selected_revision, args.thread_id), ensure_ascii=False, indent=2)); return 0
    revision = args.revision or _latest_revision(project_dir)
    if revision is None:
        print(json.dumps({"mode": "NEEDS_DESIGN", "next_command": f"python -m orchestrator.cli design --project {project}"}, ensure_ascii=False)); return 4
    if args.command == "pause":
        result = _pause(project, revision, args.reason)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str)); return 0
    design_dir = project_dir / "design-package" / f"rev-{revision:04d}"
    resume_value = None
    if args.command == "resume" and args.approve:
        from .design_package import approve_revision
        approve_revision(design_dir); resume_value = "APPROVE"
    elif args.command == "resume" and args.tier_c_json:
        resume_value = json.loads(args.tier_c_json)
    intent = args.intent if args.command == "run" else ("resume" if args.command == "resume" else "new")
    if args.command in {"start", "resume"}:
        from .execution_jobs import launch_execution_job

        result = launch_execution_job(
            REPO_ROOT,
            project,
            revision=revision,
            intent=intent,
            port=args.port,
            baud=args.baud,
            baseline=args.preflight_baseline,
            resume_value=resume_value,
            restart=args.restart,
            thread_id=args.thread_id,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 1 if result.get("mode") == "FAULTED" else 0
    result = _run(project, revision, intent, args.port, args.baud, args.preflight_baseline, resume_value, args.restart, args.thread_id)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str)); return 0 if result.get("mode") == "COMPLETE" else 2


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        arguments = argv if argv is not None else sys.argv[1:]
        project = None
        for index, value in enumerate(arguments):
            if value == "--project" and index + 1 < len(arguments): project = arguments[index + 1]
        payload = {"mode": "BLOCKED", "kind": "cli", "summary": f"{type(exc).__name__}: {exc}", "attempts": 1, "evidence": None, "needed": "resolve the reported input, environment, or persisted-state error and resume", "project": project}
        print(json.dumps(payload, ensure_ascii=False, indent=2)); return 2


if __name__ == "__main__": raise SystemExit(main())
