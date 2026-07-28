from __future__ import annotations

from orchestrator.contract_views import owner_contract_view
from orchestrator.design_inventory import materialize_grounding_requirements
from orchestrator.subgraphs.subsystem import verification_batches
from orchestrator.validators import validate_contract
from test_design_contract import valid_contract


def v12_contract() -> dict:
    value = valid_contract()
    value["schema_version"] = "1.2"
    value["workflow"]["stages"] = [
        "preflight", "design", "approval", "bind", "subsystems",
        "integration", "tier_c", "closure", "release",
    ]
    value["subsystems"][0].update({
        "verification_batch": "basic",
        "isolation_required": False,
        "isolation_reason": None,
        "hardware_resources": ["uart0"],
    })
    return value


def test_owner_view_excludes_unrelated_contract_sections():
    value = v12_contract()
    value["subsystems"].extend([
        {"id": "consumer", "dependencies": ["probe"], "classification": "reusable_software", "execution_role": "component", "registry_search_required": False, "verification_batch": "basic", "isolation_required": False, "isolation_reason": None, "hardware_resources": []},
        {"id": "unrelated", "dependencies": [], "classification": "mcu_native", "execution_role": "component", "registry_search_required": False, "verification_batch": "other", "isolation_required": False, "isolation_reason": None, "hardware_resources": []},
    ])
    view = owner_contract_view(value, "probe")
    assert {item["id"] for item in view["boundary_subsystems"]} == {"probe", "consumer"}
    assert "execution_contract_sha256" in view["authority"]


def test_v12_shared_batch_is_frozen_and_contiguous():
    value = v12_contract()
    value["subsystems"].append({"id": "consumer", "dependencies": ["probe"], "classification": "project_custom", "execution_role": "component", "registry_search_required": False, "verification_batch": "basic", "isolation_required": False, "isolation_reason": None, "hardware_resources": []})
    assert validate_contract(value) == []
    assert verification_batches(value) == [["probe", "consumer"]]


def test_v12_isolated_subsystem_cannot_share_batch():
    value = v12_contract()
    value["subsystems"][0].update({"isolation_required": True, "isolation_reason": "DMA timing"})
    value["subsystems"].append({"id": "consumer", "dependencies": ["probe"], "classification": "reusable_software", "execution_role": "component", "registry_search_required": False, "verification_batch": "basic", "isolation_required": False, "isolation_reason": None, "hardware_resources": []})
    assert any("mixes isolated" in error for error in validate_contract(value))


def test_design_materialization_splits_noncontiguous_batch_reuse():
    value = v12_contract()
    value["subsystems"].extend([
        {"id": "middle", "dependencies": ["probe"], "classification": "project_custom", "execution_role": "component", "registry_search_required": False, "verification_batch": "middle", "isolation_required": False, "isolation_reason": None, "hardware_resources": []},
        {"id": "tail", "dependencies": ["middle"], "classification": "project_custom", "execution_role": "component", "registry_search_required": False, "verification_batch": "basic", "isolation_required": False, "isolation_reason": None, "hardware_resources": []},
    ])
    normalized, _, _, errors = materialize_grounding_requirements(value)
    assert errors == []
    names = {item["id"]: item["verification_batch"] for item in normalized["subsystems"]}
    assert names == {"probe": "basic", "middle": "middle", "tail": "basic-2"}
    assert not any("verification batch" in error for error in validate_contract(normalized))


def test_design_materialization_gives_an_isolated_owner_its_own_batch():
    value = v12_contract()
    value["subsystems"][0].update({
        "isolation_required": True,
        "isolation_reason": "DMA timing",
    })
    value["subsystems"].append(
        {"id": "consumer", "dependencies": ["probe"], "classification": "project_custom", "execution_role": "component", "registry_search_required": False, "verification_batch": "basic", "isolation_required": False, "isolation_reason": None, "hardware_resources": []}
    )
    normalized, _, _, _ = materialize_grounding_requirements(value)
    names = {item["id"]: item["verification_batch"] for item in normalized["subsystems"]}
    assert names == {"probe": "basic", "consumer": "basic-2"}
    assert not any("verification batch" in error for error in validate_contract(normalized))


def test_v13_groups_only_explicitly_compatible_neighbors():
    value = v12_contract()
    value["schema_version"] = "1.3"
    value["subsystems"][0].update({
        "responsibility_layer": "board_resource", "batch_compatible": True,
    })
    value["subsystems"].extend([
        {"id": "consumer", "dependencies": ["probe"], "classification": "project_custom", "execution_role": "component", "registry_search_required": False, "verification_batch": "ignored", "batch_compatible": True, "isolation_required": False, "isolation_reason": None, "hardware_resources": [], "responsibility_layer": "product_policy"},
        {"id": "driver", "dependencies": ["consumer"], "classification": "project_custom", "execution_role": "component", "registry_search_required": False, "verification_batch": "ignored", "batch_compatible": False, "isolation_required": True, "isolation_reason": "register mutation", "hardware_resources": ["bus"], "responsibility_layer": "device_driver"},
    ])
    normalized, _, _, errors = materialize_grounding_requirements(value)
    assert errors == []
    assert verification_batches(normalized) == [["probe", "consumer"], ["driver"]]


def test_materialization_removes_selection_for_project_custom_owner_with_audit():
    value = v12_contract()
    value["subsystems"].append({
        "id": "policy", "dependencies": [], "classification": "project_custom",
        "execution_role": "component", "registry_search_required": False,
        "verification_batch": "policy", "isolation_required": False,
        "isolation_reason": None, "hardware_resources": [],
    })
    value["component_selections"] = [{
        "subsystem_id": "policy", "status": "pending", "exact_search": "policy",
        "capability_search": "ESP-IDF policy", "decision": "reject",
        "decision_reason": "model-generated", "provider_receipt_id": "",
    }]
    normalized, _, _, errors = materialize_grounding_requirements(value)
    assert errors == []
    assert {
        item["subsystem_id"] for item in normalized["component_selections"]
    } == {"probe"}
    assert normalized["grounding_normalizations"][0]["subsystem_id"] == "policy"


def test_materialization_rejects_project_custom_that_claims_registry_authority():
    value = v12_contract()
    value["subsystems"].append({
        "id": "contradictory", "dependencies": [], "classification": "project_custom",
        "execution_role": "component", "registry_search_required": True,
        "verification_batch": "custom", "isolation_required": False,
        "isolation_reason": None, "hardware_resources": [],
    })
    value["component_selections"] = [{"subsystem_id": "contradictory"}]
    _, _, _, errors = materialize_grounding_requirements(value)
    assert any("incompatible external-acquisition fields" in error for error in errors)
