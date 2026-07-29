from __future__ import annotations

from orchestrator.component_selection import finalize_component_selection
from orchestrator.implementation_readiness import assess_implementation_readiness


def _registry_detail(
    component: str,
    text: str,
    *,
    version: str = "1.3.0",
) -> dict:
    return {
        "component": component,
        "version": version,
        "content": [{"type": "text", "text": text}],
    }


def test_registry_results_replace_provider_custom_draft_with_compatible_component():
    selection = {
        "subsystem_id": "nand_driver",
        "decision": "custom",
        "decision_reason": "draft preference before Registry grounding",
        "registry_candidates": ["espressif/spi_nand_flash"],
        "registry_candidate_details": [
            _registry_detail(
                "espressif/spi_nand_flash",
                """
                Component espressif/spi_nand_flash v1.3.0.
                Supports ESP-IDF >=5.0 and all ESP targets.
                Supported chips include Winbond W25N01GV.
                Public APIs initialize, read page, write page, erase block,
                report ECC status, and handle bad blocks.
                """,
            )
        ],
    }

    result = finalize_component_selection(
        selection,
        part_number="W25N01GV",
        idf_version="6.0",
        target="esp32",
        required_operations=[
            "initialize",
            "read",
            "program",
            "erase",
            "check_ecc",
            "scan_bad_blocks",
        ],
    )

    assert result["decision"] == "registry"
    assert result["selected_component"] == "espressif/spi_nand_flash"
    assert result["selected_version"] == "1.3.0"
    assert result["uncovered_operations"] == []
    assert "draft preference" not in result["decision_reason"]


def test_zero_registry_candidates_keeps_custom_without_circular_rejection():
    result = finalize_component_selection(
        {
            "subsystem_id": "rare_chip",
            "decision": "registry",
            "decision_reason": "provider draft",
            "registry_candidates": [],
            "registry_candidate_details": [],
        },
        part_number="RARE123",
        idf_version="6.0",
        target="esp32",
        required_operations=["initialize", "read"],
    )

    assert result["decision"] == "custom"
    assert result["selected_component"] is None
    assert result["covered_operations"] == []
    assert result["uncovered_operations"] == ["initialize", "read"]
    assert "no Registry candidates" in result["decision_reason"]


def test_readiness_adopted_component_with_full_coverage_needs_no_reader():
    result = assess_implementation_readiness(
        owner="nand_driver",
        required_operations=["initialize", "read", "program"],
        selection={
            "decision": "registry",
            "selected_component": "espressif/spi_nand_flash",
            "covered_operations": ["initialize", "read", "program"],
        },
        existing_facts=[],
    )

    assert result.ready is True
    assert result.missing_facts == []
    assert result.reader_requests == []


def test_readiness_local_idf_selection_covers_mcu_native_lifecycle():
    result = assess_implementation_readiness(
        owner="board_resources",
        required_operations=["initialize", "claim_resources", "reset_recovery"],
        selection={
            "decision": "local_idf",
            "provider_receipt_id": "design-provider-local-idf",
            "covered_operations": [],
        },
        existing_facts=[],
    )

    assert result.ready is True
    assert result.component_covered_operations == [
        "initialize", "claim_resources", "reset_recovery"
    ]


def test_readiness_adopted_component_reads_only_capability_gap():
    result = assess_implementation_readiness(
        owner="nand_driver",
        required_operations=["initialize", "read", "reset_recovery"],
        selection={
            "decision": "registry",
            "selected_component": "espressif/spi_nand_flash",
            "covered_operations": ["initialize", "read"],
        },
        existing_facts=[],
    )

    assert result.ready is False
    assert result.missing_facts == ["reset_recovery"]
    assert [item["operation"] for item in result.reader_requests] == [
        "reset_recovery"
    ]


def test_readiness_custom_driver_defaults_host_lifecycle_after_hardware_grounding():
    result = assess_implementation_readiness(
        owner="rare_chip",
        required_operations=["identify", "read", "reset_recovery"],
        selection={"decision": "custom", "covered_operations": []},
        existing_facts=[
            {
                "parameter": "identify",
                "source_kind": "datasheet",
                "provider_receipt_id": "receipt-identify",
            },
            {
                "parameter": "read",
                "source_kind": "datasheet",
                "provider_receipt_id": "receipt-read",
            },
        ],
    )

    assert result.ready is True
    assert result.missing_facts == []


def test_high_risk_operation_requires_authoritative_source_even_if_fact_exists():
    result = assess_implementation_readiness(
        owner="charger",
        required_operations=["charger_config"],
        selection={"decision": "custom", "covered_operations": []},
        existing_facts=[
            {
                "parameter": "charger_config",
                "value": "set current limit",
                "source_kind": "model_knowledge",
            }
        ],
    )

    assert result.ready is False
    assert result.missing_facts == ["charger_config"]
    assert result.reader_requests[0]["authoritative_source_required"] is True
