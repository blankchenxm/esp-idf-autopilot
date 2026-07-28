from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import RunStateProjection
from .storage import ProjectStore
from .runtime_paths import ProjectRuntime
from .validators import validate_terminal
from .policies import progress_fingerprint


def _threads(database: Path, prefix: str) -> list[str]:
    if not database.is_file():
        return []
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE ?", (prefix + "%",)).fetchall()
    return [str(row[0]) for row in rows]


def reconcile_terminal_state(repo_root: Path, project: str, project_dir: Path, revision: int, thread_id: str | None = None) -> dict[str, Any]:
    """Rebuild projection/pointer through model and graph APIs, never hand edits."""
    from langgraph.checkpoint.sqlite import SqliteSaver
    from .cli import _record_thread
    from .graph import build_graph
    database = ProjectRuntime(repo_root, project).ensure().checkpoints
    candidates = [thread_id] if thread_id else _threads(database, f"{project}:rev-{revision:04d}")
    selected = None; values = None
    with SqliteSaver.from_conn_string(str(database)) as saver:
        graph = build_graph(repo_root, saver)
        for candidate in candidates:
            snapshot = graph.get_state({"configurable": {"thread_id": candidate}})
            if snapshot.values and snapshot.values.get("mode") == "COMPLETE":
                if selected is not None:
                    raise RuntimeError("multiple COMPLETE checkpoint threads exist; pass --thread-id")
                selected, values = candidate, dict(snapshot.values)
        if selected is None or values is None:
            raise RuntimeError("no COMPLETE checkpoint thread was found")
        values.update({"blocker": None, "failure": None, "failed_node": None, "pause_reason": None, "pause_next_node": None})
        projection = RunStateProjection(
            run_id=values["run_id"], mode=values["mode"], cursor=values["cursor"],
            next_action=values.get("next_action", "none"), progress_seq=values.get("progress_seq", 0),
            progress_fingerprint=progress_fingerprint(values),
            release_verified=values.get("release_verified", False), closure=values.get("closure"),
            release_evidence=values.get("release_evidence"), blocker=None,
        )
        errors = validate_terminal(projection, project_dir)
        if errors:
            raise ValueError("cannot reconcile an invalid terminal state: " + "; ".join(errors))
        graph.update_state({"configurable": {"thread_id": selected}}, {"blocker": None, "failure": None, "failed_node": None, "pause_reason": None, "pause_next_node": None}, as_node="release")
    ProjectStore(project_dir).project_state(projection)
    _record_thread(project_dir, project, revision, selected, "COMPLETE")
    return {"mode": "COMPLETE", "thread_id": selected, "run_id": projection.run_id, "errors": []}
