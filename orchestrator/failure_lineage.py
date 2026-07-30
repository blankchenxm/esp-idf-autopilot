from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .storage import digest


def failure_lineage_id(
    *,
    invariant_id: str,
    node: str,
    owner: str | None = None,
    operation_id: str | None = None,
    test_id: str | None = None,
    image_id: str | None = None,
) -> str:
    return digest({
        "schema": "failure-lineage-v1",
        "invariant_id": invariant_id,
        "node": node,
        "owner": owner,
        "operation_id": operation_id,
        "test_id": test_id,
        "image_id": image_id,
    })


def relevant_material_revision(material: dict[str, Any]) -> str:
    """Hash only fields declared relevant by the owning transaction."""
    return digest({"schema": "relevant-material-v1", "material": material})


@dataclass(frozen=True)
class RecoveryAdmission:
    admitted: bool
    reason: str
    attempt: int


def validate_recovery_admission(
    ledger: dict[str, dict[str, Any]],
    *,
    lineage_id: str,
    material_revision: str,
    disposition: str,
    typed: bool,
    new_diagnostic_id: str | None = None,
) -> RecoveryAdmission:
    if not typed:
        return RecoveryAdmission(False, "untyped exceptions are INTERNAL_FAULT", 0)
    entry = ledger.setdefault(lineage_id, {
        "transient_attempts": 0,
        "model_repairs": {},
        "diagnostic_ids": [],
    })
    if disposition == "RETRY_TRANSIENT":
        if entry["transient_attempts"] >= 2:
            return RecoveryAdmission(False, "transient retry budget exhausted", entry["transient_attempts"])
        entry["transient_attempts"] += 1
        return RecoveryAdmission(True, "typed transient retry", entry["transient_attempts"])
    if disposition != "REPAIR_INTERNAL":
        return RecoveryAdmission(False, f"{disposition} does not admit model repair", 0)
    repairs = entry["model_repairs"]
    if repairs.get(material_revision, 0) >= 1:
        return RecoveryAdmission(False, "lineage/material already used its model repair", repairs[material_revision])
    if new_diagnostic_id and new_diagnostic_id in entry["diagnostic_ids"]:
        return RecoveryAdmission(False, "diagnostic identity is not new", 0)
    repairs[material_revision] = 1
    if new_diagnostic_id:
        entry["diagnostic_ids"].append(new_diagnostic_id)
    return RecoveryAdmission(True, "one typed model repair admitted", 1)
