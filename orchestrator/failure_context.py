from __future__ import annotations

import json
from pathlib import Path

from .storage import ProjectStore


def select_owner_failure_logs(store: ProjectStore, run_id: str, owner: str, limit: int = 4) -> tuple[Path, ...]:
    """Return only current-run immutable failure artifacts owned by this agent."""
    matches: list[tuple[str, Path]] = []
    for receipt_path in (store.receipts / "failure").glob("*.json"):
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        failure = receipt.get("failure") or {}
        if receipt.get("run_id") != run_id or failure.get("owner") != owner:
            continue
        for artifact in receipt.get("artifacts", []):
            path = store.project_dir / str(artifact.get("path", ""))
            if path.is_file():
                matches.append((str(receipt.get("finished_at", "")), path))
    matches.sort(reverse=True)
    return tuple(path for _, path in matches[:limit])
