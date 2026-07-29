from __future__ import annotations

import json
from pathlib import Path

from .storage import ProjectStore


def select_owner_failure_logs(store: ProjectStore, run_id: str, owner: str, limit: int = 4) -> tuple[Path, ...]:
    """Return current-run diagnostics the responsible implementation can act on.

    Boundary failure receipts contain a short policy summary. The actionable
    compiler/flash transcript is commonly attached to the diagnostic evidence
    copied from the source receipt, so include both without crossing owner or
    run boundaries.
    """
    matches: list[tuple[str, Path]] = []
    for receipt_path in (store.receipts / "failure").glob("*.json"):
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        failure = receipt.get("failure") or {}
        if receipt.get("run_id") != run_id or failure.get("owner") != owner:
            continue
        diagnostic = (receipt.get("outputs") or {}).get("diagnostic") or {}
        artifacts = list(receipt.get("artifacts", []))
        if isinstance(diagnostic, dict):
            artifacts.extend(diagnostic.get("evidence", []))
        for artifact in artifacts:
            path = store.project_dir / str(artifact.get("path", ""))
            if path.is_file():
                matches.append((str(receipt.get("finished_at", "")), path))
    matches.sort(reverse=True)
    selected: list[Path] = []
    seen: set[Path] = set()
    for _, path in matches:
        if path not in seen:
            selected.append(path); seen.add(path)
        if len(selected) == limit:
            break
    return tuple(selected)
