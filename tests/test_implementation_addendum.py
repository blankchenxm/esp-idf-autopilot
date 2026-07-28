from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.implementation_addendum import (
    build_implementation_addendum,
    validate_implementation_addendum,
)
from orchestrator.storage import ProjectStore


def _fact(operation: str) -> dict:
    return {
        "id": f"IF_{operation}",
        "subsystem_id": "nand_driver",
        "parameter": operation,
        "value": "receipt-owned value",
        "source_kind": "datasheet",
        "provider_receipt_id": "reader-1",
        "source_assertions": [{
            "path_glob": "components/nand_driver/**/*",
            "required_tokens": [operation],
        }],
    }


def test_addendum_is_immutable_and_bound_to_design_owner_and_receipts(
    tmp_path: Path,
):
    project = tmp_path / "projects" / "demo"
    store = ProjectStore(project)
    store.ensure()
    addendum = build_implementation_addendum(
        design_digest="a" * 64,
        owner="nand_driver",
        selection={
            "decision": "registry",
            "selected_component": "espressif/spi_nand_flash",
            "selected_version": "1.3.0",
            "covered_operations": ["read"],
        },
        required_operations=["read", "reset_recovery"],
        implementation_facts=[_fact("reset_recovery")],
        source_receipts=[{
            "receipt_id": "reader-1",
            "sha256": "b" * 64,
        }],
    )

    path = store.write_implementation_addendum(addendum)
    loaded = json.loads(path.read_text(encoding="utf-8"))

    assert validate_implementation_addendum(
        loaded,
        design_digest="a" * 64,
        owner="nand_driver",
    ) == []
    with pytest.raises(FileExistsError):
        store.write_implementation_addendum(addendum)


def test_addendum_digest_detects_tampering(tmp_path: Path):
    addendum = build_implementation_addendum(
        design_digest="a" * 64,
        owner="charger",
        selection={"decision": "custom", "covered_operations": []},
        required_operations=["charger_config"],
        implementation_facts=[{
            **_fact("charger_config"),
            "subsystem_id": "charger",
        }],
        source_receipts=[{
            "receipt_id": "reader-2",
            "sha256": "c" * 64,
        }],
    )
    addendum["implementation_facts"][0]["value"] = "changed"

    assert "implementation addendum digest mismatch" in (
        validate_implementation_addendum(
            addendum,
            design_digest="a" * 64,
            owner="charger",
        )
    )
