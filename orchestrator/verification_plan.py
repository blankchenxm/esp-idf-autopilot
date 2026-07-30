from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .storage import digest


@dataclass(frozen=True)
class VerificationImage:
    image_id: str
    owners: tuple[str, ...]
    test_ids: tuple[str, ...]
    setup: dict[str, Any]
    stimulus_adapter: str
    hardware_resources: tuple[str, ...]
    isolation_required: bool


def source_digest(project_dir: Path | None) -> str:
    if project_dir is None or not project_dir.exists():
        return "unmaterialized"
    files: list[tuple[str, str]] = []
    for root_name in ("main", "components"):
        root = project_dir / root_name
        if not root.is_dir():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            files.append((
                path.relative_to(project_dir).as_posix(),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            ))
    return digest(files)


def normalize_verification_images(
    contract: dict[str, Any], project_dir: Path | None = None,
) -> list[VerificationImage]:
    """Group exact compatible rows independently of component count/schema."""
    definitions = {
        str(item.get("id")): item
        for item in contract.get("subsystems", [])
        if isinstance(item, dict) and item.get("execution_role", "component") == "component"
    }
    source = source_digest(project_dir)
    groups: list[tuple[dict[str, Any], list[dict[str, Any]], set[str]]] = []
    for row in contract.get("verification", []):
        if row.get("tier") not in {"A", "B"}:
            continue
        owner = str(row.get("owner") or "")
        definition = definitions.get(owner, {})
        setup = row.get("test_setup") or {"kind": "normal_boot"}
        stimulus = row.get("stimulus") or {"kind": "none"}
        isolated = bool(definition.get("isolation_required"))
        base = {
            "schema": "verification-image-v1",
            "source_digest": source,
            "setup": setup,
            "stimulus_adapter": stimulus.get("adapter") or stimulus.get("kind", "none"),
            "isolation_required": isolated,
            "isolation_owner": owner if isolated else None,
        }
        resources = set(map(str, definition.get("hardware_resources", [])))
        selected = None
        if not isolated:
            for candidate in groups:
                candidate_base, candidate_rows, candidate_resources = candidate
                same_owner = all(
                    str(item.get("owner")) == owner for item in candidate_rows
                )
                if (
                    candidate_base == base
                    and (
                        same_owner
                        or (
                            bool(definition.get("batch_compatible", False))
                            and not (candidate_resources & resources)
                        )
                    )
                ):
                    selected = candidate
                    break
        if selected is None:
            selected = (base, [], set())
            groups.append(selected)
        selected[1].append(row)
        selected[2].update(resources)
    result: list[VerificationImage] = []
    for base, rows, resources in groups:
        identity = {**base, "hardware_resources": sorted(resources)}
        image_id = digest(identity)
        result.append(VerificationImage(
            image_id=image_id,
            owners=tuple(dict.fromkeys(str(row["owner"]) for row in rows)),
            test_ids=tuple(str(row["test_id"]) for row in rows),
            setup=dict(identity["setup"]),
            stimulus_adapter=str(identity["stimulus_adapter"]),
            hardware_resources=tuple(sorted(resources)),
            isolation_required=bool(identity["isolation_required"]),
        ))
    return result


def report_counts(contract: dict[str, Any], images: list[VerificationImage]) -> dict[str, int]:
    return {
        "components": sum(
            1 for item in contract.get("subsystems", [])
            if item.get("execution_role", "component") == "component"
        ),
        "verification_rows": sum(
            1 for row in contract.get("verification", []) if row.get("tier") in {"A", "B"}
        ),
        "image_identities": len(images),
        "planned_builds": len(images),
        "planned_flashes": len(images),
    }
