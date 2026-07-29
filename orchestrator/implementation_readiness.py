from __future__ import annotations

from dataclasses import dataclass
from typing import Any


HIGH_RISK_OPERATIONS = frozenset({
    "erase",
    "program",
    "otp",
    "efuse",
    "power_config",
    "charger_config",
    "firmware_update",
})
AUTHORITATIVE_SOURCE_KINDS = frozenset({"datasheet", "registry", "local_idf"})
DEFAULT_CUSTOM_SOFTWARE_OPERATIONS = frozenset({
    "initialize", "detect_sample_loss", "reset_recovery",
})


@dataclass(frozen=True)
class ImplementationReadiness:
    owner: str
    ready: bool
    required_operations: list[str]
    component_covered_operations: list[str]
    fact_covered_operations: list[str]
    missing_facts: list[str]
    reader_requests: list[dict[str, Any]]


def assess_implementation_readiness(
    *,
    owner: str,
    required_operations: list[str],
    selection: dict[str, Any] | None,
    existing_facts: list[dict[str, Any]],
) -> ImplementationReadiness:
    """Compute the one execution-time gate between selection and coding."""
    required = list(dict.fromkeys(str(item) for item in required_operations if item))
    selection = selection or {}
    declared_coverage = set(selection.get("covered_operations", []))
    # MCU-native owners use the installed ESP-IDF API surface, which is
    # grounded by a local_idf selection receipt rather than a component
    # Registry record or an external-part datasheet.  Treat that receipt as
    # covering the frozen lifecycle operations; otherwise a generic board
    # resource owner is incorrectly routed to the external datasheet reader.
    if (
        selection.get("decision") == "local_idf"
        and selection.get("provider_receipt_id")
    ):
        declared_coverage.update(required)
    component_covered = [
        operation for operation in required if operation in declared_coverage
    ]
    fact_covered: list[str] = []
    for operation in required:
        if operation in component_covered:
            continue
        facts = [
            item
            for item in existing_facts
            if str(item.get("subsystem_id") or owner) == owner
            and str(item.get("parameter") or "") == operation
        ]
        if not facts:
            continue
        if operation in HIGH_RISK_OPERATIONS and not any(
            str(item.get("source_kind") or "") in AUTHORITATIVE_SOURCE_KINDS
            and bool(item.get("provider_receipt_id"))
            for item in facts
        ):
            continue
        fact_covered.append(operation)
    # Default policy for a custom external-part driver: once an authoritative
    # hardware/interface fact exists, these are host-side ESP-IDF lifecycle
    # behaviours.  They do not require a fictitious datasheet parameter with
    # the same software-operation name.  High-risk operations remain subject
    # to explicit authoritative operation facts above.
    if selection.get("decision") == "custom" and any(
        str(item.get("subsystem_id") or owner) == owner
        and str(item.get("source_kind") or "") in AUTHORITATIVE_SOURCE_KINDS
        for item in existing_facts
    ):
        for operation in required:
            if operation in DEFAULT_CUSTOM_SOFTWARE_OPERATIONS and operation not in fact_covered:
                fact_covered.append(operation)
    missing = [
        operation
        for operation in required
        if operation not in component_covered and operation not in fact_covered
    ]
    requests = [
        {
            "owner": owner,
            "operation": operation,
            "authoritative_source_required": operation in HIGH_RISK_OPERATIONS,
        }
        for operation in missing
    ]
    return ImplementationReadiness(
        owner=owner,
        ready=not missing,
        required_operations=required,
        component_covered_operations=component_covered,
        fact_covered_operations=fact_covered,
        missing_facts=missing,
        reader_requests=requests,
    )
