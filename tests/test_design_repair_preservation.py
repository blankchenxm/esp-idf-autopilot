from __future__ import annotations

from orchestrator.design_package import DesignDraft, _preserve_complete_contract_rows
from orchestrator.validators import validate_contract
from tests.test_design_contract import valid_contract


def test_schema_14_uses_integration_before_tier_c_workflow_order() -> None:
    contract = valid_contract()
    contract["schema_version"] = "1.4"
    contract["input_authority_ref"] = {
        "path": "input-authority.json", "sha256": "0" * 64,
        "size": 1, "media_type": "application/json",
    }
    contract["workflow"] = {"stages": [
        "preflight", "design", "approval", "bind", "subsystems",
        "integration", "tier_c", "closure", "release",
    ]}
    assert not any("workflow stage" in error for error in validate_contract(contract))


def test_repair_preserves_missing_verification_evidence_fields() -> None:
    prior = valid_contract()
    prior["verification"][0]["evidence_required"] = ["serial_log"]
    repaired = valid_contract()
    repaired["verification"][0].pop("evidence_required")
    repaired["verification"][0]["test_id"] = "rewritten-label"
    result = _preserve_complete_contract_rows(
        DesignDraft("# Spec", repaired, [], []), prior
    )
    assert result.execution_contract["verification"][0]["evidence_required"] == ["serial_log"]
    assert result.execution_contract["verification"][0]["test_id"] == prior["verification"][0]["test_id"]


def test_repair_allows_explicit_verification_evidence_and_batch_fixes() -> None:
    prior = valid_contract()
    prior["verification"][0]["evidence_contract"] = {
        "observation": "serial",
        "required_kinds": ["artifact"],
    }
    prior["subsystems"][0]["verification_batch"] = "out_of_order"
    repaired = valid_contract()
    repaired["verification"][0]["evidence_contract"] = {
        "observation": "serial",
        "required_kinds": ["serial_log"],
    }
    repaired["subsystems"][0]["verification_batch"] = "contiguous"

    result = _preserve_complete_contract_rows(
        DesignDraft("# Spec", repaired, [], []), prior
    )

    assert result.execution_contract["verification"][0]["evidence_contract"] == {
        "observation": "serial",
        "required_kinds": ["serial_log"],
    }
    assert result.execution_contract["subsystems"][0]["verification_batch"] == "contiguous"


def test_repair_preserves_required_collections_when_provider_returns_empty_patch() -> None:
    prior = valid_contract()
    repaired = valid_contract()
    repaired["requirements"] = []
    repaired["subsystems"] = []
    repaired["verification"] = []

    result = _preserve_complete_contract_rows(
        DesignDraft("# Spec", repaired, [], []), prior
    )

    assert result.execution_contract["requirements"] == prior["requirements"]
    assert result.execution_contract["subsystems"] == prior["subsystems"]
    assert result.execution_contract["verification"] == prior["verification"]
