from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Blocker, RunMode, RunStateProjection
from .policies import progress_fingerprint
from .storage import ProjectStore, atomic_write_json
from .runtime_paths import ProjectRuntime


def invalidated_evidence_ids(project_dir: Path) -> set[str]:
    result: set[str] = set()
    for path in (project_dir / "execution" / "corrections").glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            result.update(str(item) for item in value.get("invalidated_evidence_ids", []))
        except (OSError, json.JSONDecodeError):
            continue
    return result


def record_evidence_correction(project_dir: Path, evidence_ids: list[str], reason: str, corrected_by: str = "user") -> dict[str, Any]:
    """Append a superseding fact; immutable evidence is never edited/deleted."""
    if not evidence_ids or not reason.strip():
        raise ValueError("evidence correction requires IDs and a reason")
    store = ProjectStore(project_dir); store.ensure()
    known = {path.stem: path for path in store.evidence.rglob("*.json")}
    missing = sorted(set(evidence_ids) - known.keys())
    if missing:
        raise FileNotFoundError(f"evidence correction names unknown IDs: {missing}")
    correction_id = store.new_id("correction")
    value = {
        "schema_version": "1.0",
        "correction_id": correction_id,
        "invalidated_evidence_ids": sorted(set(evidence_ids)),
        "reason": reason.strip(),
        "corrected_by": corrected_by,
        "corrected_at": datetime.now(timezone.utc).isoformat(),
    }
    path = store.execution / "corrections" / f"{correction_id}.json"
    atomic_write_json(path, value)
    project_correction_state(project_dir)
    return value


def project_correction_state(project_dir: Path) -> bool:
    """Demote a terminal projection when a correction invalidates its evidence."""
    state_path = project_dir / "execution" / "run-state.json"
    if not state_path.is_file():
        return False
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("mode") != RunMode.COMPLETE.value:
        if state.get("mode") == RunMode.BLOCKED.value and (state.get("blocker") or {}).get("kind") == "evidence_correction":
            projection = RunStateProjection.model_validate(state).model_copy(update={"progress_fingerprint": progress_fingerprint(state)})
            ProjectStore(project_dir).project_state(projection)
            return True
        return False
    current_run = state.get("run_id")
    invalidated = invalidated_evidence_ids(project_dir)
    affects_current = False
    for path in (project_dir / "execution" / "evidence").rglob("*.json"):
        try:
            evidence = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if evidence.get("evidence_id") in invalidated and evidence.get("run_id") == current_run:
            affects_current = True
            break
    if not affects_current:
        return False
    blocker = Blocker(
        kind="evidence_correction",
        summary="Previously accepted evidence was superseded by an immutable correction.",
        evidence="execution/corrections",
        needed="reverify the corrected acceptance row and downstream integration before a fresh release",
    )
    projection = RunStateProjection.model_validate(state).model_copy(update={
        "mode": RunMode.BLOCKED,
        "cursor": "BLOCKED:closure:evidence-correction",
        "next_action": blocker.needed,
        "release_verified": False,
        "blocker": blocker,
        "progress_fingerprint": progress_fingerprint({**state, "cursor": "BLOCKED:closure:evidence-correction", "next_action": blocker.needed}),
    })
    ProjectStore(project_dir).project_state(projection)
    active_path = ProjectRuntime.for_project_dir(project_dir).ensure().active_thread
    if active_path.is_file():
        active = json.loads(active_path.read_text(encoding="utf-8"))
        active["mode"] = RunMode.BLOCKED.value
        active["run_id"] = current_run
        atomic_write_json(active_path, active)
    return True
