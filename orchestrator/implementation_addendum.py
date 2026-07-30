from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .storage import digest
from .implementation_readiness import (
    AUTHORITATIVE_SOURCE_KINDS,
    DEFAULT_CUSTOM_SOFTWARE_OPERATIONS,
    DEFAULT_CUSTOM_SOFTWARE_POLICY,
    HIGH_RISK_OPERATIONS,
)


def _payload(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item for key, item in value.items()
        if key != "addendum_digest"
    }


def build_implementation_addendum(
    *,
    design_digest: str,
    owner: str,
    selection: dict[str, Any],
    required_operations: list[str],
    implementation_facts: list[dict[str, Any]],
    source_receipts: list[dict[str, str]],
    operation_authorities: list[dict[str, str]],
) -> dict[str, Any]:
    value = {
        "schema_version": "1.1",
        "design_digest": design_digest,
        "owner": owner,
        "selection": {
            key: selection.get(key)
            for key in (
                "decision",
                "selected_component",
                "selected_version",
                "covered_operations",
            )
            if key in selection
        },
        "required_operations": list(dict.fromkeys(required_operations)),
        "implementation_facts": implementation_facts,
        "operation_authorities": operation_authorities,
        "source_receipts": source_receipts,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    value["addendum_digest"] = digest(value)
    return value


def validate_implementation_addendum(
    value: dict[str, Any],
    *,
    design_digest: str,
    owner: str,
) -> list[str]:
    errors: list[str] = []
    schema_version = value.get("schema_version")
    if schema_version not in {"1.0", "1.1"}:
        errors.append("implementation addendum schema_version must be 1.0 or 1.1")
    if value.get("design_digest") != design_digest:
        errors.append("implementation addendum design digest mismatch")
    if value.get("owner") != owner:
        errors.append("implementation addendum owner mismatch")
    if value.get("addendum_digest") != digest(_payload(value)):
        errors.append("implementation addendum digest mismatch")
    receipts = {
        str(item.get("receipt_id")): str(item.get("sha256") or "")
        for item in value.get("source_receipts", [])
        if isinstance(item, dict)
    }
    for receipt_id, receipt_hash in receipts.items():
        if not receipt_id or len(receipt_hash) != 64:
            errors.append("implementation addendum has malformed source receipt binding")
    required = set(value.get("required_operations", []))
    covered = set(value.get("selection", {}).get("covered_operations", []))
    for fact in value.get("implementation_facts", []):
        if not isinstance(fact, dict):
            errors.append("implementation addendum facts must be objects")
            continue
        operation = str(fact.get("parameter") or "")
        receipt_id = str(fact.get("provider_receipt_id") or "")
        if operation:
            covered.add(operation)
        if receipt_id not in receipts:
            errors.append(
                f"implementation fact {fact.get('id')!r} lacks a bound source receipt"
            )
    if schema_version == "1.1":
        authority_by_operation: dict[str, dict[str, Any]] = {}
        for authority in value.get("operation_authorities", []):
            if not isinstance(authority, dict):
                errors.append("implementation operation authority must be an object")
                continue
            operation = str(authority.get("operation") or "")
            kind = str(authority.get("kind") or "")
            if operation not in required:
                errors.append(f"implementation authority has unknown operation: {operation!r}")
                continue
            if operation in authority_by_operation:
                errors.append(f"implementation authority is duplicated: {operation}")
                continue
            if kind == "component_selection":
                if operation not in value.get("selection", {}).get("covered_operations", []):
                    errors.append(f"component authority lacks selection coverage: {operation}")
            elif kind == "hardware_fact":
                receipt_id = str(authority.get("provider_receipt_id") or "")
                source_kind = str(authority.get("source_kind") or "")
                if receipt_id not in receipts:
                    errors.append(f"hardware-fact authority lacks receipt binding: {operation}")
                if operation in HIGH_RISK_OPERATIONS and source_kind not in AUTHORITATIVE_SOURCE_KINDS:
                    errors.append(f"high-risk authority is not authoritative: {operation}")
                if not any(
                    str(fact.get("parameter") or "") == operation
                    and str(fact.get("provider_receipt_id") or "") == receipt_id
                    and str(fact.get("source_kind") or "") == source_kind
                    for fact in value.get("implementation_facts", [])
                    if isinstance(fact, dict)
                ):
                    errors.append(f"hardware-fact authority lacks matching fact: {operation}")
            elif kind == "local_idf_default":
                receipt_id = str(authority.get("provider_receipt_id") or "")
                if operation not in DEFAULT_CUSTOM_SOFTWARE_OPERATIONS:
                    errors.append(f"default authority is not permitted for operation: {operation}")
                if operation in HIGH_RISK_OPERATIONS:
                    errors.append(f"default authority cannot cover high-risk operation: {operation}")
                if authority.get("policy_id") != DEFAULT_CUSTOM_SOFTWARE_POLICY["policy_id"]:
                    errors.append(f"default authority has unknown policy: {operation}")
                if authority.get("policy_version") != DEFAULT_CUSTOM_SOFTWARE_POLICY["policy_version"]:
                    errors.append(f"default authority has unknown policy version: {operation}")
                if authority.get("provider_operation") != "idf_version":
                    errors.append(f"default authority lacks idf_version provenance: {operation}")
                if receipt_id not in receipts:
                    errors.append(f"default authority lacks local-IDF receipt binding: {operation}")
            else:
                errors.append(f"implementation authority has unknown kind: {kind!r}")
            authority_by_operation[operation] = authority
        for operation in required:
            if operation not in authority_by_operation:
                errors.append(f"implementation authority is missing: {operation}")
            else:
                covered.add(operation)
    missing = sorted(required - covered)
    if missing:
        errors.append(
            f"implementation addendum lacks required operation coverage: {missing}"
        )
    return errors


def validate_addendum_source_bindings(
    project_dir: Path, value: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    receipt_operations: dict[str, str] = {}
    for binding in value.get("source_receipts", []):
        if not isinstance(binding, dict):
            errors.append("implementation addendum source receipt must be an object")
            continue
        logical = str(binding.get("path") or "")
        path = project_dir / logical
        receipt_id = str(binding.get("receipt_id") or "")
        expected_hash = str(binding.get("sha256") or "")
        if not path.is_file():
            errors.append(f"implementation addendum source receipt is missing: {logical}")
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_hash:
            errors.append(
                f"implementation addendum source receipt hash mismatch: {receipt_id}"
            )
            continue
        try:
            receipt = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            errors.append(
                f"implementation addendum source receipt is invalid JSON: {receipt_id}"
            )
            continue
        if receipt.get("receipt_id") != receipt_id or receipt.get("success") is not True:
            errors.append(
                f"implementation addendum source receipt identity/success mismatch: {receipt_id}"
            )
            continue
        receipt_operations[receipt_id] = str(receipt.get("operation") or "")
    for authority in value.get("operation_authorities", []):
        if not isinstance(authority, dict) or authority.get("kind") != "local_idf_default":
            continue
        receipt_id = str(authority.get("provider_receipt_id") or "")
        if receipt_operations.get(receipt_id) != "idf_version":
            errors.append(
                "default authority source receipt is not an idf_version receipt: "
                f"{authority.get('operation')!r}"
            )
    return errors
