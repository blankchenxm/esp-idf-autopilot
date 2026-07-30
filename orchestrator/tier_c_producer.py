from __future__ import annotations

from typing import Any, Iterable


def validate_tier_c_contract(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    integration_tests = {
        str(item.get("id") or item.get("test_id"))
        for item in contract.get("integration", {}).get("tests", [])
        if isinstance(item, dict)
    }
    operation_ids = {
        str(item.get("operation_id"))
        for item in contract.get("operations", [])
        if isinstance(item, dict)
    }
    for item in contract.get("tier_c", []):
        artifact = item.get("artifact_contract") or {}
        method = artifact.get("access_method")
        if method == "physical_observation":
            if artifact.get("source") or item.get("delivery_method"):
                errors.append(f"Tier C {item.get('id')!r} physical observation must not claim a file delivery")
            continue
        required = (
            "producer_phase", "producer_test_id", "producer_operation_ids",
            "delivery_method", "correlation_key", "producer_receipt_kinds",
            "artifact_validation",
        )
        missing = [key for key in required if not item.get(key)]
        if missing:
            errors.append(f"Tier C {item.get('id')!r} lacks producer fields {missing}")
            continue
        if str(item["producer_test_id"]) not in integration_tests:
            errors.append(f"Tier C {item.get('id')!r} names an unknown producer_test_id")
        unknown = sorted(set(map(str, item["producer_operation_ids"])) - operation_ids)
        if unknown:
            errors.append(f"Tier C {item.get('id')!r} names unknown producer operations {unknown}")
        if method == "local_file" and item["delivery_method"] != "local_generation":
            errors.append(f"Tier C {item.get('id')!r} local_file requires local_generation")
        if method == "download_url" and "protocol_receipt" not in item["producer_receipt_kinds"]:
            errors.append(f"Tier C {item.get('id')!r} download_url requires protocol_receipt")
    return errors


def validate_tier_c_producer_chain(
    item: dict[str, Any],
    receipts: Iterable[dict[str, Any]],
    *,
    run_id: str,
    design_digest: str,
    firmware_sha256: str,
) -> list[str]:
    if (item.get("artifact_contract") or {}).get("access_method") == "physical_observation":
        return []
    matching = [
        receipt for receipt in receipts
        if receipt.get("run_id") == run_id
        and receipt.get("outputs", {}).get("design_digest") == design_digest
        and receipt.get("outputs", {}).get("firmware_sha256") == firmware_sha256
        and receipt.get("outputs", {}).get("producer_test_id") == item.get("producer_test_id")
        and receipt.get("outputs", {}).get("correlation_key") == item.get("correlation_key")
        and receipt.get("success") is True
    ]
    kinds = {str(receipt.get("operation")) for receipt in matching}
    missing = sorted(set(item.get("producer_receipt_kinds", [])) - kinds)
    return [] if not missing else [
        f"Tier C {item.get('id')!r} lacks final-image producer Receipts {missing}"
    ]
