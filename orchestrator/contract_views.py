from __future__ import annotations

from typing import Any

from .storage import digest


def _mentions_owner(value: Any, owner: str) -> bool:
    if isinstance(value, str):
        return value == owner
    if isinstance(value, dict):
        return any(_mentions_owner(item, owner) for item in value.values())
    if isinstance(value, list):
        return any(_mentions_owner(item, owner) for item in value)
    return False


def owner_contract_view(contract: dict[str, Any], owner: str) -> dict[str, Any]:
    """Derive a minimal implementation view without creating new authority."""
    subsystems = {item["id"]: item for item in contract.get("subsystems", [])}
    if owner not in subsystems:
        raise KeyError(f"unknown contract owner: {owner}")
    selected = subsystems[owner]
    direct_dependencies = set(selected.get("dependencies", []))
    direct_consumers = {
        item["id"] for item in contract.get("subsystems", [])
        if owner in item.get("dependencies", [])
    }
    visible_owners = {owner, *direct_dependencies, *direct_consumers}
    requirement_ids = {
        item["id"] for item in contract.get("requirements", [])
        if item.get("owner") == owner
    }
    return {
        "schema_version": "1.0",
        "view_kind": "owner-contract",
        "project": contract.get("project"),
        "owner": owner,
        "authority": {
            "execution_contract_sha256": digest(contract),
            "note": "Derived read-only view; execution-contract.json remains authority.",
        },
        "subsystem": selected,
        "boundary_subsystems": [
            item for item in contract.get("subsystems", [])
            if item.get("id") in visible_owners
        ],
        "requirements": [
            item for item in contract.get("requirements", [])
            if item.get("id") in requirement_ids
        ],
        "verification": [
            item for item in contract.get("verification", [])
            if item.get("owner") == owner or item.get("requirement_id") in requirement_ids
        ],
        "operations": [
            item for item in contract.get("operations", [])
            if item.get("owner") == owner
        ],
        "implementation_facts": [
            item for item in contract.get("implementation_facts", [])
            if item.get("subsystem_id") == owner
        ],
        "component_selections": [
            item for item in contract.get("component_selections", [])
            if item.get("subsystem_id") in visible_owners
        ],
        "datasheets": [
            item for item in contract.get("datasheets", [])
            if item.get("subsystem_id") in visible_owners
        ],
        "architecture": {
            key: [
                item for item in value
                if _mentions_owner(item, owner)
            ] if isinstance(value, list) else value
            for key, value in contract.get("architecture", {}).items()
        },
        "integration_impact": [
            item for item in contract.get("integration", {}).get("tests", [])
            if requirement_ids.intersection(item.get("requirement_ids", []))
        ],
        "release_constraints": contract.get("release", {}),
        "limitations": [
            item for item in contract.get("limitations", [])
            if _mentions_owner(item, owner)
        ],
    }
