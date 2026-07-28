"""Stop hook that keeps an active ESP-IDF firmware pipeline running."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ALLOWED_WAIT_MODES = {"WAITING_SPEC", "WAITING_TIER_C", "PAUSED"}
KNOWN_MODES = ALLOWED_WAIT_MODES | {"CONTINUOUS", "BLOCKED", "COMPLETE", "FAULTED"}


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _evidence_exists(repo_root: Path, project_dir: Path, value: Any) -> bool:
    if not _nonempty(value):
        return False
    raw = Path(value)
    candidates = [raw] if raw.is_absolute() else [project_dir / raw, repo_root / raw]
    for candidate in candidates:
        if _inside(candidate, project_dir) and candidate.is_file() and candidate.stat().st_size > 0:
            return True
    return False


def load_states(repo_root: Path) -> list[tuple[Path, dict[str, Any] | None, str | None]]:
    states: list[tuple[Path, dict[str, Any] | None, str | None]] = []
    projects = repo_root / "projects"
    if not projects.is_dir():
        return states
    for path in sorted(projects.glob("*/run-state.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top level must be an object")
            states.append((path, data, None))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            states.append((path, None, str(exc)))
    return states


def complete_errors(repo_root: Path, state_path: Path, state: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    closure = state.get("closure")
    release = state.get("release_evidence")
    if state.get("cursor") != "STAGE 3.6:release:pass":
        errors.append("cursor is not STAGE 3.6:release:pass")
    if state.get("release_verified") is not True:
        errors.append("release_verified is not true")
    if not isinstance(closure, dict):
        errors.append("closure object is missing")
    else:
        total = closure.get("required_total")
        passed = closure.get("pass")
        if not isinstance(total, int) or total < 0 or passed != total:
            errors.append("closure pass count does not equal required_total")
        for field in ("partial", "fail", "blocked"):
            if closure.get(field) != 0:
                errors.append(f"closure.{field} is not zero")
    if not isinstance(release, dict):
        errors.append("release_evidence object is missing")
    else:
        if release.get("closure_pass") is not True:
            errors.append("closure_pass is not true")
        if release.get("selftest_disabled") is not True:
            errors.append("selftest_disabled is not true")
        project_dir = state_path.parent
        for field in ("build_log", "flash_log", "serial_log"):
            if not _evidence_exists(repo_root, project_dir, release.get(field)):
                errors.append(f"{field} is missing, empty, or outside the project")
        if not _nonempty(release.get("runtime_marker")):
            errors.append("runtime_marker is empty")
    return errors


def blocked_errors(state: dict[str, Any]) -> list[str]:
    blocker = state.get("blocker")
    if not isinstance(blocker, dict):
        return ["blocker object is missing"]
    return [
        f"blocker.{field} is empty"
        for field in ("kind", "summary", "evidence", "needed")
        if not _nonempty(blocker.get(field))
    ]


def continuation(reason: str) -> dict[str, str]:
    return {"decision": "block", "reason": reason}


def evaluate(repo_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    states = load_states(repo_root)
    if not states:
        return {}

    malformed = [(path, error) for path, state, error in states if state is None]
    if malformed:
        details = "; ".join(f"{path}: {error}" for path, error in malformed)
        return continuation(
            "Do not stop: an ESP-IDF run-state file is malformed. "
            f"Repair it using the run-state protocol, then resume the recorded next action. {details}"
        )

    valid_states = [(path, state) for path, state, _ in states if state is not None]
    unknown = [
        (path, state.get("mode"))
        for path, state in valid_states
        if state.get("mode") not in KNOWN_MODES
    ]
    if unknown:
        details = "; ".join(f"{path}: mode={mode!r}" for path, mode in unknown)
        return continuation(
            "Do not stop: an ESP-IDF run-state has an unknown mode. "
            f"Repair the state and continue. {details}"
        )

    continuous = [(path, state) for path, state in valid_states if state.get("mode") == "CONTINUOUS"]
    if len(continuous) > 1:
        paths = ", ".join(str(path) for path, _ in continuous)
        return continuation(
            "Do not stop: more than one ESP-IDF project is marked CONTINUOUS. "
            f"Reconcile the active run states, keep exactly one active, and resume it. Files: {paths}"
        )
    if continuous:
        path, state = continuous[0]
        cursor = state.get("cursor")
        next_action = state.get("next_action")
        if not _nonempty(cursor) or not _nonempty(next_action):
            return continuation(
                f"Do not stop: {path} is CONTINUOUS but cursor or next_action is empty. "
                "Repair the state from DEVLOG/evidence and execute the next concrete action."
            )
        repeated = bool(hook_input.get("stop_hook_active"))
        prefix = "The prior continuation still did not reach an allowed terminal. " if repeated else ""
        return continuation(
            f"{prefix}Do not stop or ask whether to continue. Read {path}, keep mode CONTINUOUS, "
            f"and execute its next action now. Cursor: {cursor}. Next action: {next_action}. "
            "Internal failures require diagnose-patch-reverify; advance through remaining dependencies "
            "until WAITING_TIER_C, a fully evidenced hard BLOCKED state, or verified Stage 3.6 release."
        )

    for path, state in valid_states:
        mode = state.get("mode")
        if mode == "COMPLETE":
            errors = complete_errors(repo_root, path, state)
            if errors:
                return continuation(
                    f"Do not stop: {path} claims COMPLETE without valid release evidence. "
                    f"Set CONTINUOUS, make the missing verification the next action, and resume. Missing: {'; '.join(errors)}"
                )
        elif mode == "BLOCKED":
            errors = blocked_errors(state)
            if errors:
                return continuation(
                    f"Do not stop: {path} claims BLOCKED without a fully evidenced hard blocker. "
                    f"Repair the state and continue internal diagnosis. Missing: {'; '.join(errors)}"
                )
    return {}


def main() -> int:
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
        if not isinstance(hook_input, dict):
            hook_input = {}
        repo_root = Path(__file__).resolve().parents[2]
        result = evaluate(repo_root, hook_input)
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # Fail safe: a broken guard must not silently permit a premature stop.
        sys.stdout.write(
            json.dumps(
                continuation(
                    "Do not stop: the ESP-IDF Stop hook failed. Diagnose and repair the hook before "
                    f"continuing the firmware pipeline. Error: {type(exc).__name__}: {exc}"
                ),
                ensure_ascii=False,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
