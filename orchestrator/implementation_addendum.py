from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .storage import digest


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
) -> dict[str, Any]:
    value = {
        "schema_version": "1.0",
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
    if value.get("schema_version") != "1.0":
        errors.append("implementation addendum schema_version must be 1.0")
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
    return errors
