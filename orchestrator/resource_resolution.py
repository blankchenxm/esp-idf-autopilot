"""Protocol-neutral allocation of source-bound hardware resources.

Protocol adapters translate datasheet and ESP-IDF facts into the small generic
requirement/candidate vocabulary used here.  This module intentionally does
not contain I2C, SPI, I2S, UART, or project names.
"""
from __future__ import annotations

from typing import Any


def resolve_resource_requirements(contract: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve declared requirements against declared target capabilities.

    A requirement selects one compatible, unclaimed candidate.  Attribute
    constraints use ``equals``, ``minimum``, ``maximum`` and ``one_of`` so
    protocol-specific facts can be normalized without protocol-specific
    control flow in the Harness.
    """
    requirements = contract.get("resource_requirements", [])
    candidates = contract.get("resource_capabilities", [])
    if not isinstance(requirements, list) or not isinstance(candidates, list):
        return [], ["resource requirements and capabilities must be arrays"]
    allocations: list[dict[str, Any]] = []
    claimed: set[str] = set()
    errors: list[str] = []
    for requirement in requirements:
        if not isinstance(requirement, dict):
            errors.append("resource requirement must be an object")
            continue
        owner = str(requirement.get("owner") or "")
        kind = str(requirement.get("kind") or "")
        constraints = requirement.get("constraints", {})
        if not owner or not kind or not isinstance(constraints, dict):
            errors.append("resource requirement needs owner, kind, and constraints")
            continue
        matches: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get("kind") != kind:
                continue
            candidate_id = str(candidate.get("id") or "")
            if not candidate_id or candidate_id in claimed:
                continue
            attributes = candidate.get("attributes", {})
            if not isinstance(attributes, dict):
                continue
            compatible = True
            for name, constraint in constraints.items():
                value = attributes.get(name)
                if not isinstance(constraint, dict):
                    compatible = False; break
                if "equals" in constraint and value != constraint["equals"]:
                    compatible = False; break
                if "one_of" in constraint and value not in constraint["one_of"]:
                    compatible = False; break
                if "minimum" in constraint and (not isinstance(value, (int, float)) or value < constraint["minimum"]):
                    compatible = False; break
                if "maximum" in constraint and (not isinstance(value, (int, float)) or value > constraint["maximum"]):
                    compatible = False; break
            if compatible:
                matches.append(candidate)
        if not matches:
            errors.append(f"{owner} has no compatible {kind} resource candidate")
            continue
        selected = sorted(matches, key=lambda item: (int(item.get("priority", 0)), str(item.get("id"))))[0]
        claimed.add(str(selected["id"]))
        allocations.append({
            "owner": owner,
            "requirement_id": requirement.get("id"),
            "candidate_id": selected["id"],
            "attributes": selected.get("attributes", {}),
            "provenance": "DERIVED_RESOURCE_RESOLUTION",
        })
    return allocations, errors
