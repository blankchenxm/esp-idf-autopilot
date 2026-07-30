from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .runtime_paths import ProjectRuntime
from .storage import atomic_write_json


MODEL_ACTION_MODES = frozenset({
    "WAITING_DESIGN_INPUT",
    "WAITING_SPEC",
    "WAITING_TIER_C",
    "BLOCKED",
    "FAULTED",
    "COMPLETE",
    "PAUSED",
    "INTERRUPTED",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


@contextmanager
def _sequence_lock(runtime: ProjectRuntime) -> Iterator[None]:
    path = runtime.locks / "control-events.lock"
    token = uuid.uuid4().hex
    payload = {"pid": os.getpid(), "token": token}
    for _ in range(200):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            break
        except FileExistsError:
            existing = _read(path)
            if not _pid_is_alive(int(existing.get("pid") or 0)):
                path.unlink(missing_ok=True)
                continue
            time.sleep(0.01)
    else:
        raise RuntimeError(f"timed out acquiring control-event lock: {path}")
    try:
        yield
    finally:
        existing = _read(path)
        if existing.get("token") == token:
            path.unlink(missing_ok=True)


def publish_control_event(
    repo_root: Path,
    project: str,
    *,
    source: str,
    mode: str,
    reason: str,
    payload: dict[str, Any] | None = None,
    model_action_required: bool | None = None,
) -> dict[str, Any]:
    """Persist one monotonic state-change event.

    Progress is observable data for the client/runner.  It is deliberately
    distinct from a model wake instruction.
    """
    runtime = ProjectRuntime(repo_root, project).ensure()
    with _sequence_lock(runtime):
        sequence_state = _read(runtime.control_event_sequence)
        action_required = (
            mode in MODEL_ACTION_MODES
            if model_action_required is None
            else bool(model_action_required)
        )
        progress_signature = f"{source}|{mode}|{reason}"
        if (
            not action_required
            and sequence_state.get("last_progress_signature")
            == progress_signature
        ):
            suppressed = int(
                sequence_state.get("suppressed_progress_events") or 0
            ) + 1
            runtime.control_event_sequence.write_text(
                json.dumps({
                    **sequence_state,
                    "suppressed_progress_events": suppressed,
                }, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return {
                "schema_version": "1.0",
                "event_seq": int(sequence_state.get("event_seq") or 0),
                "project": project,
                "source": source,
                "mode": mode,
                "reason": reason,
                "model_action_required": False,
                "suppressed": True,
                "suppressed_progress_events": suppressed,
            }
        sequence = int(sequence_state.get("event_seq") or 0) + 1
        event = {
            "schema_version": "1.0",
            "event_seq": sequence,
            "created_at": _now(),
            "project": project,
            "source": source,
            "mode": mode,
            "reason": reason,
            "model_action_required": action_required,
            "suppressed_progress_events": int(
                sequence_state.get("suppressed_progress_events") or 0
            ),
            "payload": payload or {},
        }
        atomic_write_json(
            runtime.control_events / f"{sequence:020d}.json", event
        )
        atomic_write_json(runtime.control_event_sequence, {
            "schema_version": "1.0",
            "event_seq": sequence,
            "last_progress_signature": (
                progress_signature if not action_required else None
            ),
            "suppressed_progress_events": 0,
        })
        return event


def read_control_events(
    repo_root: Path,
    project: str,
    *,
    after_seq: int = 0,
    model_action_only: bool = False,
) -> list[dict[str, Any]]:
    runtime = ProjectRuntime(repo_root, project).ensure()
    events: list[dict[str, Any]] = []
    for path in sorted(runtime.control_events.glob("*.json")):
        try:
            value = _read(path)
        except (OSError, json.JSONDecodeError):
            continue
        if int(value.get("event_seq") or 0) <= after_seq:
            continue
        if model_action_only and value.get("model_action_required") is not True:
            continue
        events.append(value)
    return events


def latest_control_event_seq(repo_root: Path, project: str) -> int:
    runtime = ProjectRuntime(repo_root, project).ensure()
    return int(_read(runtime.control_event_sequence).get("event_seq") or 0)


def validate_control_event_delivery(
    events: list[dict[str, Any]],
    sequence_state: dict[str, Any] | None = None,
) -> list[str]:
    """Validate that progress delivery cannot become a model wake boundary."""
    errors: list[str] = []
    previous = 0
    for event in sorted(events, key=lambda item: int(item.get("event_seq") or 0)):
        sequence = int(event.get("event_seq") or 0)
        if sequence <= previous:
            errors.append("control event_seq is not strictly monotonic")
        previous = sequence
        kind = str(event.get("kind") or "")
        requires_model = bool(event.get("model_action_required"))
        if kind == "PROGRESS" and requires_model:
            errors.append("PROGRESS event requests a model action")
        if kind == "MODEL_ACTION_REQUIRED" and not requires_model:
            errors.append("MODEL_ACTION_REQUIRED event lacks its wake flag")
    if sequence_state:
        persisted = int(sequence_state.get("event_seq") or 0)
        if persisted < previous:
            errors.append("control sequence projection trails persisted events")
    return errors


def await_control_event(
    repo_root: Path,
    project: str,
    *,
    after_seq: int = 0,
    timeout_s: float | None = None,
    model_action_only: bool = True,
    poll_interval_s: float = 0.1,
) -> dict[str, Any] | None:
    """Block in deterministic code until a meaningful transition exists."""
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    while True:
        events = read_control_events(
            repo_root,
            project,
            after_seq=after_seq,
            model_action_only=model_action_only,
        )
        if events:
            return events[0]
        if deadline is not None and time.monotonic() >= deadline:
            return None
        time.sleep(poll_interval_s)
