from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

from .storage import atomic_write_json


def _failure_run_ids(project_dir: Path) -> set[str]:
    result: set[str] = set()
    for path in (project_dir / "execution" / "receipts" / "failure").glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("run_id"):
                result.add(str(value["run_id"]))
        except (OSError, json.JSONDecodeError):
            continue
    return result


def archive_historical_failures(project_dir: Path) -> dict[str, Any]:
    """Compress bulky failed-run artifacts while preserving fact JSON files."""
    state_path = project_dir / "execution" / "run-state.json"
    current_run = json.loads(state_path.read_text(encoding="utf-8")).get("run_id") if state_path.is_file() else None
    archive_root = project_dir / "execution" / "archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    index_path = archive_root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {"schema_version": "1.0", "artifacts": {}}
    archived: list[str] = []
    for run_id in sorted(_failure_run_ids(project_dir) - {str(current_run)}):
        sources = [project_dir / "logs" / run_id, project_dir / "execution" / "release" / run_id]
        files = [path for source in sources if source.is_dir() for path in source.rglob("*") if path.is_file()]
        if not files:
            continue
        zip_path = archive_root / f"{run_id}.zip"
        temporary = zip_path.with_suffix(".zip.tmp")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for path in files:
                logical = path.relative_to(project_dir).as_posix()
                bundle.write(path, logical)
                index["artifacts"][logical] = {
                    "archive": zip_path.relative_to(project_dir).as_posix(),
                    "member": logical,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size": path.stat().st_size,
                }
        temporary.replace(zip_path)
        for source in sources:
            if source.is_dir():
                shutil.rmtree(source)
        archived.append(run_id)
    atomic_write_json(index_path, index)
    return {"archived_runs": archived, "index": index_path.relative_to(project_dir).as_posix()}
