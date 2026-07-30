"""Explicit, secret-free authority for rotating local firmware credentials."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .input_authority import compile_input_authority, semantic_input_bytes
from .models import Receipt
from .secrets import secret_values
from .secrets import write_crumb_credentials
from .storage import ProjectStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _semantic_ref(path: Path) -> tuple[str, int]:
    data = semantic_input_bytes("requirements", path.read_bytes())
    return hashlib.sha256(data).hexdigest(), len(data)


def _authority_projection(authority: dict[str, Any]) -> dict[str, set[tuple[str, str]]]:
    """Compare frozen public authority without unstable source offsets."""
    return {
        "identifiers": {
            (str(item.get("source")), str(item.get("value")))
            for item in authority.get("identifiers", [])
        },
        "pins": {
            (str(item.get("source")), str(item.get("name", item.get("gpio"))))
            for item in authority.get("pins", [])
        },
        "protocols": {
            (str(item.get("source")), str(item.get("endpoint")))
            for item in authority.get("protocols", [])
        },
    }


def _rotation_receipts(project_dir: Path) -> list[Path]:
    return sorted((project_dir / "execution" / "receipts" / "credential-rotation").glob("*.json"), reverse=True)


def rotation_allows_input(design_dir: Path, reference: dict[str, Any], current: bytes) -> bool:
    """True only for an explicit, still-current credential-only rotation."""
    if reference.get("path") != "requirements/" + design_dir.parent.parent.name + ".md":
        return False
    project_dir = design_dir.parent.parent
    manifest = json.loads((design_dir / "manifest.json").read_text(encoding="utf-8"))
    current_sha, current_size = _semantic_ref(project_dir.parents[1] / reference["path"])
    for path in _rotation_receipts(project_dir):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        inputs = value.get("inputs", {})
        if (
            value.get("success") is True
            and value.get("operation") == "credential_rotation"
            and inputs.get("design_digest") == manifest.get("design_digest")
            and inputs.get("input_path") == reference.get("path")
            and inputs.get("semantic_sha256") == current_sha
            and inputs.get("semantic_size") == current_size
        ):
            return True
    return False


def authorize_credential_rotation(repo_root: Path, project: str, design_dir: Path) -> dict[str, Any]:
    """Record an explicit user-authorized rotation without storing a secret."""
    requirements = repo_root / "requirements" / f"{project}.md"
    if not secret_values(requirements.read_text(encoding="utf-8")):
        raise ValueError("credential rotation requires at least one recognized local secret")
    frozen_path = design_dir / "input-authority.json"
    if not frozen_path.is_file():
        raise ValueError("credential rotation requires the frozen input-authority record")
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    current = compile_input_authority(repo_root, project)
    if _authority_projection(frozen) != _authority_projection(current):
        raise ValueError("credential rotation changed public design authority; prepare a design revision instead")
    manifest = json.loads((design_dir / "manifest.json").read_text(encoding="utf-8"))
    sha256, size = _semantic_ref(requirements)
    store = ProjectStore(design_dir.parent.parent)
    store.ensure()
    # This is deliberately outside design authority and ignored by Git.  It is
    # regenerated only after the public-authority guard and explicit approval.
    if project == "crumb":
        write_crumb_credentials(
            requirements, store.project_dir / "private" / "crumb_credentials.h"
        )
    started = _now()
    receipt = Receipt(
        receipt_id=store.new_id("credential-rotation"),
        run_id="credential-rotation",
        operation="credential_rotation",
        started_at=started,
        finished_at=_now(),
        success=True,
        command=["orchestrator", "rotate-credentials", "--approve"],
        inputs={
            "design_digest": manifest["design_digest"],
            "input_path": f"requirements/{project}.md",
            "semantic_sha256": sha256,
            "semantic_size": size,
            "public_authority_verified": True,
        },
        outputs={"credential_materialized": project == "crumb", "approval": "explicit_user_rotation"},
    )
    path = store.write_receipt(receipt, "credential-rotation")
    return {"authorized": True, "receipt": str(path), "design_revision": manifest["revision"]}
