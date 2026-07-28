from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

from .models import ArtifactRef, ExecutionEvent, Receipt, Evidence, RunStateProjection


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_ref(path: Path, base: Path, media_type: str = "application/octet-stream") -> ArtifactRef:
    data = path.read_bytes()
    return ArtifactRef(path=path.resolve().relative_to(base.resolve()).as_posix(), sha256=hashlib.sha256(data).hexdigest(), size=len(data), media_type=media_type)


def resolve_artifact_bytes(project_dir: Path, logical_path: str) -> bytes:
    """Read a direct artifact or its integrity-bound historical archive member."""
    direct = project_dir / logical_path
    if direct.is_file():
        return direct.read_bytes()
    index_path = project_dir / "execution" / "archive" / "index.json"
    if not index_path.is_file():
        raise FileNotFoundError(f"artifact is missing: {logical_path}")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    record = index.get("artifacts", {}).get(Path(logical_path).as_posix())
    if not record:
        raise FileNotFoundError(f"artifact is missing and not archived: {logical_path}")
    with zipfile.ZipFile(project_dir / record["archive"]) as bundle:
        data = bundle.read(record["member"])
    if hashlib.sha256(data).hexdigest() != record["sha256"] or len(data) != record["size"]:
        raise ValueError(f"archived artifact integrity check failed: {logical_path}")
    return data


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.model_dump(mode="json", by_alias=True) if hasattr(value, "model_dump") else value
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class ProjectStore:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir.resolve()
        self.execution = self.project_dir / "execution"
        self.receipts = self.execution / "receipts"
        self.evidence = self.execution / "evidence"
        self.logs = self.project_dir / "logs"

    def ensure(self) -> None:
        for path in (self.execution, self.receipts, self.evidence, self.logs):
            path.mkdir(parents=True, exist_ok=True)

    def write_receipt(self, receipt: Receipt, category: str) -> Path:
        if receipt.project_id is None:
            receipt = receipt.model_copy(update={"project_id": self.project_dir.name})
        path = self.receipts / category / f"{receipt.receipt_id}.json"
        if path.exists():
            raise FileExistsError(f"immutable receipt already exists: {path}")
        atomic_write_json(path, receipt)
        return path

    def find_successful_receipt(
        self, operation: str, idempotency_key: str
    ) -> Receipt | None:
        """Return an integrity-valid completed transaction for exact replay.

        A receipt is reusable only when the producer explicitly supplied an
        idempotency key and every bound artifact still has its recorded bytes.
        Failed or legacy receipts are never inferred to be reusable.
        """
        if not idempotency_key:
            return None
        for path in sorted(self.receipts.rglob("*.json"), reverse=True):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if (
                    value.get("success") is not True
                    or value.get("operation") != operation
                    or value.get("inputs", {}).get("idempotency_key")
                    != idempotency_key
                ):
                    continue
                receipt = Receipt.model_validate(value)
                for artifact in receipt.artifacts:
                    data = resolve_artifact_bytes(self.project_dir, artifact.path)
                    if (
                        hashlib.sha256(data).hexdigest() != artifact.sha256
                        or len(data) != artifact.size
                    ):
                        raise ValueError("receipt artifact integrity mismatch")
                return receipt
            except (
                FileNotFoundError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ):
                continue
        return None

    def write_evidence(self, evidence: Evidence, category: str) -> Path:
        if evidence.project_id is None:
            evidence = evidence.model_copy(update={"project_id": self.project_dir.name})
        path = self.evidence / category / f"{evidence.evidence_id}.json"
        if path.exists():
            raise FileExistsError(f"immutable evidence already exists: {path}")
        atomic_write_json(path, evidence)
        return path

    def write_implementation_addendum(self, addendum: dict[str, Any]) -> Path:
        """Persist one immutable execution-time implementation authority."""
        owner = str(addendum.get("owner") or "")
        addendum_digest = str(addendum.get("addendum_digest") or "")
        if not owner or not addendum_digest:
            raise ValueError("implementation addendum requires owner and digest")
        path = (
            self.execution
            / "implementation-addenda"
            / owner
            / f"{addendum_digest}.json"
        )
        if path.exists():
            raise FileExistsError(f"immutable addendum already exists: {path}")
        atomic_write_json(path, addendum)
        return path

    def append_event(self, event: ExecutionEvent) -> None:
        if event.project_id is None:
            event = event.model_copy(update={"project_id": self.project_dir.name})
        self.execution.mkdir(parents=True, exist_ok=True)
        with (self.execution / "events.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n")

    def project_state(self, projection: RunStateProjection) -> Path:
        if projection.project_id is None:
            projection = projection.model_copy(update={"project_id": self.project_dir.name})
        path = self.execution / "run-state.json"
        atomic_write_json(path, projection)
        # Compatibility projection for the current read-only Stop hook. Both files
        # are derived from the same checkpoint update and are never control inputs.
        atomic_write_json(self.project_dir / "run-state.json", projection)
        return path

    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:16]}"
