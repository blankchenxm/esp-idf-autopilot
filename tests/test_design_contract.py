from __future__ import annotations

import json
import shutil
from pathlib import Path

from orchestrator.design_inventory import (
    build_design_inventory,
    build_grounding_plan,
)
from orchestrator.storage import atomic_write_json, file_ref
from orchestrator.validators import design_digest, validate_contract, validate_design_package


def valid_contract(project: str = "fixture") -> dict:
    return {
        "schema_version": "1.1", "project": project,
        "requirements": [{"id": "R1", "description": "Emit ready marker", "owner": "probe", "origin": "USER"}],
        "subsystems": [{"id": "probe", "dependencies": [], "classification": "mcu_native", "execution_role": "component", "registry_search_required": False}],
        "component_selections": [], "datasheets": [],
        "verification": [{"requirement_id": "R1", "test_id": "PROBE-READY", "owner": "probe", "tier": "B", "user_involvement": False, "timeout_s": 10, "expected": {"marker": "READY", "count_min": 1}, "evidence_required": ["build_receipt", "flash_receipt", "serial_log"], "evidence_contract": {"observation": "serial", "required_kinds": ["build_receipt", "flash_receipt", "serial_log", "firmware_hash", "hardware_identity"]}}],
        "architecture": {"tasks": [{"name": "main", "priority": 1, "stack_bytes": 4096}], "queues": [], "resource_ownership": [], "failure_recovery": []},
        "workflow": {"stages": ["preflight", "design", "approval", "bind", "subsystems", "tier_c", "integration", "closure", "release"]},
        "tier_c": [], "limitations": [],
        "integration": {"tests": [{"id": "INT-READY", "requirement_ids": ["R1"], "expected": {"marker": "READY", "count_min": 1}, "timeout_s": 10}]},
        "release": {"runtime_marker": "READY", "selftest_disabled": True, "selftest_config": "CONFIG_APP_SELFTEST", "timeout_s": 10}
    }


def package(tmp_path: Path) -> Path:
    root = tmp_path; project = "fixture"; design = root / "projects" / project / "design-package" / "rev-0001"; design.mkdir(parents=True)
    for area in ("requirements", "connections"):
        path = root / area / f"{project}.md"; path.parent.mkdir(); path.write_text(f"# {area}\nfixture\n", encoding="utf-8")
    contract = valid_contract(project); (design / "spec.md").write_text("# Fixture\n\nReview-ready fixture specification.\n", encoding="utf-8"); atomic_write_json(design / "execution-contract.json", contract)
    manifest = {"schema_version": "1.0", "revision": 1, "inputs": [file_ref(root / area / f"{project}.md", root, "text/markdown").model_dump() for area in ("requirements", "connections")], "providers": [{"name": "test", "kind": "design-generator"}], "files": [file_ref(design / name, design, "text/markdown" if name.endswith(".md") else "application/json").model_dump() for name in ("spec.md", "execution-contract.json")]}
    manifest["design_digest"] = design_digest(contract, manifest); atomic_write_json(design / "manifest.json", manifest)
    atomic_write_json(design / "design-validation.json", {"schema_version": "1.0", "valid": True, "errors": [], "warnings": [], "design_digest": manifest["design_digest"]})
    atomic_write_json(design / "approval.json", {"schema_version": "1.0", "status": "APPROVED", "spec_revision": 1, "design_digest": manifest["design_digest"], "approved_by": "test", "approved_at": "2026-01-01T00:00:00Z", "unresolved_policy_items": [], "pending_tier_c_items": []})
    return design


def test_five_file_approved_package_is_valid(tmp_path: Path):
    _, errors = validate_design_package(package(tmp_path), require_approval=True); assert errors == []


def test_input_hash_change_invalidates_package(tmp_path: Path):
    design = package(tmp_path); (tmp_path / "requirements" / "fixture.md").write_text("changed", encoding="utf-8")
    assert any("hash/size mismatch" in error for error in validate_design_package(design)[1])


def test_dependency_cycle_is_rejected():
    value = valid_contract(); value["subsystems"].append({"id": "consumer", "dependencies": ["probe"], "classification": "reusable_software", "registry_search_required": True}); value["subsystems"][0]["dependencies"] = ["consumer"]
    assert any("cycle" in error for error in validate_contract(value))


def test_tier_ab_cannot_delegate_to_user():
    value = valid_contract(); value["verification"][0]["user_involvement"] = True
    assert any("illegally requires user" in error for error in validate_contract(value))


def test_v15_selftest_verification_requires_an_executable_isolated_setup():
    value = valid_contract()
    value["schema_version"] = "1.5"
    value["input_authority_ref"] = {
        "path": "authority.json", "sha256": "0" * 64, "size": 1,
        "media_type": "application/json",
    }
    value["implementation_facts"] = []
    value["product_decisions"] = []
    row = value["verification"][0]
    row["tier"] = "A"
    row["test_setup"] = {
        "kind": "firmware_selftest",
        "kconfig_overrides": {"CONFIG_APP_SELFTEST": "y"},
        "isolated_build": True,
    }
    row["stimulus"] = {"kind": "firmware_simulation", "sequence": ["press", "release"]}
    assert validate_contract(value) == []

    row["test_setup"].pop("isolated_build")
    assert any("isolated_build" in error for error in validate_contract(value))


def test_v15_rejects_mixed_setups_in_one_verification_batch():
    value = valid_contract()
    value["schema_version"] = "1.5"
    value["input_authority_ref"] = {
        "path": "authority.json", "sha256": "0" * 64, "size": 1,
        "media_type": "application/json",
    }
    value["implementation_facts"] = []
    value["product_decisions"] = []
    value["verification"][0].update({
        "test_setup": {"kind": "normal_boot"},
        "stimulus": {"kind": "none"},
    })
    value["requirements"].append({"id": "R2", "description": "Second check", "owner": "probe"})
    value["verification"].append({
        "requirement_id": "R2", "test_id": "PROBE-SELFTEST", "owner": "probe",
        "tier": "A", "expected": {"marker": "SELFTEST"},
        "evidence_required": ["serial_log"],
        "test_setup": {"kind": "firmware_selftest", "kconfig_overrides": {"CONFIG_APP_SELFTEST": "y"}, "isolated_build": True},
        "stimulus": {"kind": "firmware_simulation"},
    })
    assert any("mixes test_setup" in error for error in validate_contract(value))


def test_registry_gate_requires_exact_and_capability_search():
    value = valid_contract(); value["subsystems"][0]["registry_search_required"] = True
    assert any("Registry exact/capability" in error for error in validate_contract(value))


def test_registry_routing_requires_external_or_explicit_reusable_candidates():
    native = valid_contract(); native["subsystems"][0]["registry_search_required"] = True
    assert any("local IDF grounding" in error for error in validate_contract(native))
    custom = valid_contract(); custom["subsystems"][0].update({"classification": "project_custom", "registry_search_required": False})
    assert validate_contract(custom) == []
    external = valid_contract(); external["subsystems"][0].update({"classification": "external_part", "registry_search_required": False})
    assert any("requires Registry candidate grounding" in error for error in validate_contract(external))


def test_custom_external_driver_can_be_approved_at_l1():
    value = valid_contract(); value["subsystems"][0].update({"classification": "external_part", "registry_search_required": True}); value["component_selections"] = [{"subsystem_id": "probe", "status": "ok", "exact_search": {"query": "x"}, "capability_search": {"query": "sensor"}, "decision": "custom", "decision_reason": "no compatible library"}]; value["datasheets"] = [{"subsystem_id": "probe", "level": "L1", "technical_content_valid": True}]
    assert not any("Datasheet L2" in error for error in validate_contract(value))


def test_unadopted_registry_candidates_require_per_candidate_reasons():
    value = valid_contract()
    value["subsystems"][0].update(
        {"classification": "external_part", "registry_search_required": True}
    )
    selection = {
        "subsystem_id": "probe",
        "status": "ok",
        "exact_search": "SENSOR1",
        "capability_search": "ESP-IDF sensor",
        "decision": "custom",
        "decision_reason": "custom fallback",
        "provider_receipt_id": "receipt",
        "registry_candidates": ["vendor/sensor"],
        "registry_candidate_details": [
            {"component": "vendor/sensor", "version": "1.0.0"}
        ],
    }
    value["component_selections"] = [selection]
    value["datasheets"] = [
        {
            "subsystem_id": "probe",
            "level": "L2",
            "technical_content_valid": True,
        }
    ]
    assert any(
        "lack explicit rejection reasons" in error
        for error in validate_contract(value)
    )
    selection["rejected_candidates"] = [
        {"component": "vendor/sensor", "reason": "missing required bus mode"}
    ]
    assert not any(
        "lack explicit rejection reasons" in error
        for error in validate_contract(value)
    )


def test_registry_adoption_requires_details_and_rejects_other_candidates():
    value = valid_contract()
    value["subsystems"][0].update(
        {"classification": "reusable_software", "registry_search_required": True}
    )
    selection = {
        "subsystem_id": "probe",
        "status": "ok",
        "exact_search": "vendor/sensor",
        "capability_search": "ESP-IDF sensor",
        "decision": "registry",
        "decision_reason": "official implementation selected",
        "selected_component": "vendor/sensor",
        "provider_receipt_id": "receipt",
        "registry_candidates": ["vendor/sensor", "other/sensor"],
        "registry_candidate_details": [
            {"component": "vendor/sensor"},
            {"component": "other/sensor"},
        ],
    }
    value["component_selections"] = [selection]
    errors = validate_contract(value)
    assert any("lack explicit rejection reasons" in item for item in errors)

    selection["rejected_candidates"] = [
        {"component": "other/sensor", "reason": "unsupported target"}
    ]
    errors = validate_contract(value)
    assert not any("lack explicit rejection reasons" in item for item in errors)
    assert not any("lack implementation details" in item for item in errors)


def test_mandatory_workflow_cannot_bypass_closure():
    value = valid_contract(); value["workflow"]["stages"].remove("closure")
    assert any("closure" in error for error in validate_contract(value))


def test_integration_verification_must_map_to_one_capture():
    value = valid_contract()
    value["subsystems"].append({
        "id": "integration",
        "dependencies": ["probe"],
        "classification": "project_custom",
        "execution_role": "integration",
        "responsibility_layer": "integration",
        "registry_search_required": False,
    })
    value["requirements"].append({
        "id": "R2",
        "owner": "integration",
        "statement": "system ready",
    })
    value["verification"].append({
        "requirement_id": "R2",
        "test_id": "DR_INT",
        "owner": "integration",
        "tier": "B",
        "expected": {"marker": "READY"},
    })
    value["integration"]["tests"] = [
        {
            "id": "INT-A",
            "requirement_ids": ["R2"],
            "expected": {"marker": "READY"},
            "timeout_s": 10,
        },
        {
            "id": "INT-B",
            "requirement_ids": ["R2"],
            "expected": {"marker": "READY"},
            "timeout_s": 10,
        },
    ]
    assert any(
        "must map to exactly one integration test" in error
        for error in validate_contract(value)
    )
    value["verification"][-1]["integration_test_id"] = "INT-A"
    assert not any(
        "must map to exactly one integration test" in error
        for error in validate_contract(value)
    )


def test_blocking_grounding_limitation_cannot_be_approval_ready():
    value = valid_contract()
    value["limitations"] = [
        {
            "id": "L_AUDIO",
            "status": "blocking_grounding",
            "statement": "[GROUNDING_PENDING] Do not guess the capture format.",
        }
    ]
    assert any(
        "L_AUDIO" in error and "prevents approval" in error
        for error in validate_contract(value)
    )


def test_v13_grounding_pending_limitation_cannot_hide_behind_generic_status():
    value = valid_contract()
    value["schema_version"] = "1.3"
    value["limitations"] = [
        {
            "id": "L_AUDIO",
            "status": "BLOCKING",
            "statement": "[GROUNDING_PENDING] Do not guess the capture format.",
        }
    ]
    assert any(
        "L_AUDIO" in error and "leaves grounding unresolved" in error
        for error in validate_contract(value)
    )


def test_exact_external_part_requires_only_l1_during_design():
    value = valid_contract()
    value["subsystems"][0].update(
        {
            "classification": "external_part",
            "part_number": "PART1",
            "registry_search_required": True,
        }
    )
    for decision in ("reject", "registry", "custom"):
        value["component_selections"] = [
            {
                "subsystem_id": "probe",
                "decision": decision,
                "exact_search": "PART1",
                "capability_search": "PART1 driver",
            }
        ]
        plan = build_grounding_plan(value, build_design_inventory(value))
        datasheet = next(item for item in plan if item["kind"] == "datasheet")
        assert datasheet["required_level"] == "L1"


def test_v13_external_part_can_defer_implementation_facts_to_execution_readiness():
    value = valid_contract(); value["schema_version"] = "1.3"
    value["subsystems"][0].update({
        "classification": "external_part", "part_number": "PART1",
        "registry_search_required": True, "responsibility_layer": "device_driver",
        "verification_batch": "driver", "batch_compatible": False,
        "isolation_required": True, "isolation_reason": "register access",
        "hardware_resources": ["i2c"],
    })
    value["product_decisions"] = []
    value["implementation_facts"] = []
    value["component_selections"] = [{
        "subsystem_id": "probe", "status": "ok", "exact_search": "PART1",
        "capability_search": "driver", "decision": "custom",
        "decision_reason": "custom", "provider_receipt_id": "receipt-1",
    }]
    value["datasheets"] = [{
        "subsystem_id": "probe", "part_number": "PART1", "variant": "PART1",
        "document_id": "d", "revision": "r", "source": "s", "content_hash": "h",
        "coverage": ["identity", "interface", "electrical", "timing", "registers_commands"],
        "level": "L2", "technical_content_valid": True, "provider_receipt_id": "receipt-1",
    }]
    assert not any(
        "lacks receipt-bound implementation facts" in error
        for error in validate_contract(value)
    )


def test_delegated_grounding_policy_auto_approves_only_fact_only_revision(tmp_path: Path):
    from orchestrator.design_package import approve_delegated_grounding_revision

    source = package(tmp_path)
    target = source.parent / "rev-0002"
    shutil.copytree(source, target)
    contract = json.loads((target / "execution-contract.json").read_text(encoding="utf-8"))
    contract["limitations"] = [{"id": "L1", "status": "resolved_grounding"}]
    atomic_write_json(target / "execution-contract.json", contract)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    manifest["revision"] = 2
    manifest["files"] = [
        file_ref(target / name, target, "text/markdown" if name.endswith(".md") else "application/json").model_dump()
        for name in ("spec.md", "execution-contract.json")
    ]
    manifest["design_digest"] = design_digest(contract, manifest)
    atomic_write_json(target / "manifest.json", manifest)
    atomic_write_json(target / "design-validation.json", {"schema_version": "1.0", "valid": True, "errors": [], "warnings": [], "design_digest": manifest["design_digest"]})
    atomic_write_json(target / "approval.json", {"schema_version": "1.0", "status": "PENDING", "spec_revision": 2, "design_digest": manifest["design_digest"], "approved_by": None, "approved_at": None, "unresolved_policy_items": [], "pending_tier_c_items": []})
    policy = tmp_path / "projects" / "fixture" / "design-autonomy.json"
    policy.write_text('{"schema_version":"1.0","delegated_grounding_auto_approval":true}', encoding="utf-8")

    approval = approve_delegated_grounding_revision(tmp_path, policy.parent, target)

    assert approval is not None
    assert approval["approval_basis"]["kind"] == "delegated_grounding"
    assert validate_design_package(target, require_approval=True)[1] == []

    contract["requirements"][0]["description"] = "Changed product behavior"
    atomic_write_json(target / "execution-contract.json", contract)
    assert approve_delegated_grounding_revision(tmp_path, policy.parent, target) is None


def test_component_loop_role_is_not_inferred_from_a_reserved_name():
    value = valid_contract()
    value["subsystems"].append({"id": "system_test", "dependencies": ["probe"], "classification": "mcu_native", "execution_role": "integration", "registry_search_required": False})
    from orchestrator.subgraphs.subsystem import subsystem_order
    assert subsystem_order(value) == ["probe"]


def test_tier_c_supplement_has_its_own_evidence_contract():
    value = valid_contract()
    value["tier_c"] = [{"id": "TC1", "requirement_id": "R1", "owner": "probe", "instructions": "Observe the physical result.", "test_id": "TC-PHYSICAL", "expected": {"confirmation": "confirmed"}, "evidence_contract": {"observation": "human", "required_kinds": ["user_confirmation"]}}]
    assert validate_contract(value) == []


def test_tier_c_cannot_omit_user_confirmation_when_declared():
    value = valid_contract()
    value["tier_c"] = [{"id": "TC1", "requirement_id": "R1", "instructions": "Observe.", "evidence_contract": {"observation": "human", "required_kinds": ["serial_log"]}}]
    assert any("user_confirmation" in error for error in validate_contract(value))


def test_schema_keeps_legacy_v10_tier_c_contract_readable():
    from orchestrator.validators import _schema_errors

    value = valid_contract()
    value["schema_version"] = "1.0"
    value["tier_c"] = [{
        "id": "TC1",
        "requirement_id": "R1",
        "instructions": "Observe the physical result.",
    }]
    assert _schema_errors(value, "execution-contract.schema.json") == []


def test_runtime_rejects_declared_evidence_kinds_not_produced_by_a_node(tmp_path: Path):
    from orchestrator.graph import HarnessNodes
    row = {"test_id": "PROBE-READY", "evidence_contract": {"observation": "protocol", "required_kinds": ["protocol_receipt"]}}
    try:
        HarnessNodes(tmp_path)._evidence_kinds(row, {"build_receipt", "serial_log"})
    except ValueError as exc:
        assert "protocol_receipt" in str(exc)
    else:
        raise AssertionError("missing declared evidence kind was silently accepted")
