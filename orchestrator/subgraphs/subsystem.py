from __future__ import annotations


def subsystem_order(contract: dict) -> list[str]:
    from ..validators import topological_subsystems
    roles = {item["id"]: item.get("execution_role", "component") for item in contract["subsystems"]}
    # New contracts declare this distinction explicitly.  The default keeps
    # pre-role immutable revisions executable; it never interprets an ID.
    return [owner for owner in topological_subsystems(contract["subsystems"])
            if roles[owner] == "component"]


def expected_for_owner(contract: dict, owner: str) -> list[dict]:
    return [row for row in contract["verification"] if row.get("owner") == owner]


def verification_batches(contract: dict) -> list[list[str]]:
    """Return frozen contiguous batches; legacy contracts remain singleton."""
    order = subsystem_order(contract)
    definitions = {item["id"]: item for item in contract["subsystems"]}
    if contract.get("schema_version") not in {"1.2", "1.3"}:
        return [[owner] for owner in order]
    result: list[list[str]] = []
    for owner in order:
        name = definitions[owner]["verification_batch"]
        if result and definitions[result[-1][0]]["verification_batch"] == name:
            result[-1].append(owner)
        else:
            result.append([owner])
    return result
