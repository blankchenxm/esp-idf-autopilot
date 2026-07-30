from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_MATRIX_PATH = Path(__file__).resolve().parents[1] / "schemas" / "schema-capabilities.json"


@dataclass(frozen=True)
class SchemaPolicy:
    current: str
    capabilities: dict[str, frozenset[str]]
    release_required: frozenset[str]


def schema_policy() -> SchemaPolicy:
    value = json.loads(_MATRIX_PATH.read_text(encoding="utf-8"))
    capabilities = {
        str(version): frozenset(str(item) for item in items)
        for version, items in value["capabilities"].items()
    }
    current = str(value["current_executable_schema"])
    if current not in capabilities:
        raise ValueError("current executable schema is absent from capability matrix")
    return SchemaPolicy(
        current=current,
        capabilities=capabilities,
        release_required=frozenset(str(item) for item in value["release_required"]),
    )


def current_schema_version() -> str:
    return schema_policy().current


def schema_has(version: str | None, capability: str) -> bool:
    return capability in schema_policy().capabilities.get(str(version), frozenset())


def require_executable_schema(contract: dict[str, Any]) -> list[str]:
    """Return missing modern execution capabilities without mutating legacy input."""
    policy = schema_policy()
    version = str(contract.get("schema_version") or "")
    available = policy.capabilities.get(version)
    if available is None:
        return [f"unknown execution-contract schema version {version!r}"]
    missing = sorted(policy.release_required - available)
    if missing:
        return [
            "DESIGN_REVISION_REQUIRED: approved schema "
            f"{version} lacks release-authoritative capabilities {missing}; "
            f"create and approve an unmodified-input migration to schema {policy.current}"
        ]
    return []


def validate_capability_matrix(rule_capabilities: dict[str, str]) -> list[str]:
    """Prove every registered rule has an explicit value for every schema."""
    policy = schema_policy()
    errors: list[str] = []
    unknown = sorted(set(rule_capabilities) - set(policy.capabilities))
    if unknown:
        errors.append(f"registry names unknown schema versions {unknown}")
    missing = sorted(set(policy.capabilities) - set(rule_capabilities))
    if missing:
        errors.append(f"registry omits schema versions {missing}")
    return errors
