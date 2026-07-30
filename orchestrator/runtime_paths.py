from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .storage import atomic_write_json


def repo_root_for_project(project_dir: Path) -> Path:
    """Resolve the repository root for a direct ``projects/<id>`` child.

    Tests and embedding callers may use ``<tmp>/<id>`` directly; in that case
    the parent is treated as the repository root.
    """
    resolved = project_dir.resolve()
    return resolved.parent.parent if resolved.parent.name == "projects" else resolved.parent


@dataclass(frozen=True)
class ProjectRuntime:
    repo_root: Path
    project_id: str

    @classmethod
    def for_project_dir(cls, project_dir: Path) -> "ProjectRuntime":
        resolved = project_dir.resolve()
        return cls(repo_root_for_project(resolved), resolved.name)

    @property
    def root(self) -> Path:
        return self.repo_root.resolve() / "runtime" / "projects" / self.project_id

    @property
    def checkpoints(self) -> Path:
        return self.root / "checkpoints.sqlite"

    @property
    def agent_work(self) -> Path:
        return self.root / "agent-work"

    @property
    def design_provider(self) -> Path:
        return self.root / "design-provider"

    @property
    def design_jobs(self) -> Path:
        return self.root / "design-jobs"

    @property
    def active_design_job(self) -> Path:
        return self.root / "active-design-job.json"

    @property
    def execution_jobs(self) -> Path:
        return self.root / "execution-jobs"

    @property
    def control_events(self) -> Path:
        return self.root / "control-events"

    @property
    def control_event_sequence(self) -> Path:
        return self.root / "control-event-sequence.json"

    @property
    def worker_reconciliations(self) -> Path:
        return self.root / "worker-reconciliations"

    @property
    def active_execution_job(self) -> Path:
        return self.root / "active-execution-job.json"

    @property
    def locks(self) -> Path:
        return self.root / "locks"

    @property
    def runner_lock(self) -> Path:
        return self.locks / "runner.lock"

    @property
    def thread_registry(self) -> Path:
        return self.root / "checkpoint-threads.json"

    @property
    def active_thread(self) -> Path:
        return self.root / "active-checkpoint-thread.json"

    def ensure(self) -> "ProjectRuntime":
        for path in (
            self.root,
            self.agent_work,
            self.design_provider,
            self.design_jobs,
            self.execution_jobs,
            self.control_events,
            self.worker_reconciliations,
            self.locks,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_control()
        return self

    def _migrate_legacy_control(self) -> None:
        """Copy legacy project control into the project namespace once.

        The global SQLite database is copied only when this project has a
        legacy thread pointer. A genuinely new project can therefore never
        inherit another project's checkpoint merely because its revision and
        default thread name happen to match.
        """
        project_dir = self.repo_root / "projects" / self.project_id
        legacy_execution = project_dir / "execution"
        copied: list[str] = []
        for name, target in (
            ("checkpoint-threads.json", self.thread_registry),
            ("active-checkpoint-thread.json", self.active_thread),
        ):
            source = legacy_execution / name
            if source.is_file() and not target.exists():
                shutil.copy2(source, target)
                copied.append(name)
        legacy_database = self.repo_root / "runtime" / "checkpoints.sqlite"
        if copied and legacy_database.is_file() and not self.checkpoints.exists():
            with sqlite3.connect(legacy_database) as source, sqlite3.connect(self.checkpoints) as target:
                source.backup(target)
            copied.append("checkpoints.sqlite")
        if copied:
            marker = self.root / "legacy-migration.json"
            if not marker.exists():
                atomic_write_json(marker, {
                    "schema_version": "1.0",
                    "project_id": self.project_id,
                    "copied": copied,
                    "source": "legacy global/project control paths",
                })

    def read_thread_registry(self) -> list[dict]:
        self.ensure()
        if not self.thread_registry.is_file():
            return []
        value = json.loads(self.thread_registry.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("threads"), list):
            raise RuntimeError("checkpoint thread registry is malformed")
        return [item for item in value["threads"] if isinstance(item, dict)]
