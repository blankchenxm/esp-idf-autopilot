from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


OPERATION_KINDS = frozenset({
    "hardware_register", "hardware_transport", "idf_api", "host_algorithm",
    "product_policy", "system_orchestration",
})
RISK_LEVELS = frozenset({"safe", "reversible", "destructive", "safety_critical"})
POLICY_KINDS = frozenset({"host_algorithm", "product_policy", "system_orchestration"})
AUTHORITATIVE_SOURCE_TYPES = frozenset({"registry", "datasheet", "local_idf", "input", "policy"})


@dataclass(frozen=True)
class OperationAuthorityResult:
    passed: bool
    decisions: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]


def compile_operation_authority(
    contract: dict[str, Any],
    *,
    available_receipt_ids: set[str] | None = None,
) -> OperationAuthorityResult:
    """Resolve each typed operation capability to one exact authority source.

    Owner-level component coverage and prose inference are deliberately absent.
    An authority source must name the exact capability it proves.
    """
    available = available_receipt_ids
    errors: list[str] = []
    decisions: list[dict[str, Any]] = []
    operation_ids: set[str] = set()
    owners = {str(item.get("id")) for item in contract.get("subsystems", [])}
    for operation in contract.get("operations", []):
        if not isinstance(operation, dict):
            errors.append("operations contains a non-object")
            continue
        operation_id = str(operation.get("operation_id") or "")
        owner = str(operation.get("owner") or "")
        kind = str(operation.get("kind") or "")
        risk = str(operation.get("risk") or "")
        if not operation_id or operation_id in operation_ids:
            errors.append(f"invalid or duplicate operation_id {operation_id!r}")
            continue
        operation_ids.add(operation_id)
        if owner not in owners:
            errors.append(f"operation {operation_id!r} has unknown owner {owner!r}")
        if kind not in OPERATION_KINDS:
            errors.append(f"operation {operation_id!r} has invalid kind {kind!r}")
        if risk not in RISK_LEVELS:
            errors.append(f"operation {operation_id!r} has invalid risk {risk!r}")
        capabilities = tuple(dict.fromkeys(
            str(item) for item in operation.get("required_capabilities", []) if str(item)
        ))
        if not capabilities:
            errors.append(f"operation {operation_id!r} has no required_capabilities")
            continue
        assertions = operation.get("implementation_assertions")
        if not isinstance(assertions, list) or not assertions:
            errors.append(f"operation {operation_id!r} lacks implementation_assertions")
        probe = operation.get("runtime_probe")
        if not isinstance(probe, dict) or not probe.get("test_id"):
            errors.append(f"operation {operation_id!r} lacks a typed runtime_probe")
        default = operation.get("default_policy")
        if default is not None and (
            kind not in POLICY_KINDS
            or risk in {"destructive", "safety_critical"}
            or not isinstance(default, dict)
            or not default.get("policy_id")
            or not default.get("version")
        ):
            errors.append(f"operation {operation_id!r} uses an inadmissible default_policy")
        sources = operation.get("authority_sources")
        if not isinstance(sources, list) or not sources:
            errors.append(f"operation {operation_id!r} has no authority_sources")
            continue
        for capability in capabilities:
            candidates: list[dict[str, Any]] = []
            for source in sources:
                if not isinstance(source, dict):
                    continue
                source_type = str(source.get("source_type") or "")
                proves = set(str(item) for item in source.get("capability_ids", []))
                receipt_id = str(source.get("provider_receipt_id") or "")
                if source_type not in AUTHORITATIVE_SOURCE_TYPES or capability not in proves:
                    continue
                if source_type != "input" and not receipt_id:
                    continue
                if available is not None and receipt_id and receipt_id not in available:
                    continue
                if source_type == "policy" and default is None:
                    continue
                if kind.startswith("hardware_") and source_type == "policy":
                    continue
                candidates.append(source)
            if len(candidates) != 1:
                errors.append(
                    f"operation {operation_id!r} capability {capability!r} "
                    f"requires exactly one admissible authority, found {len(candidates)}"
                )
                continue
            source = candidates[0]
            decisions.append({
                "operation_id": operation_id,
                "owner": owner,
                "capability_id": capability,
                "source_type": source["source_type"],
                "authority_ref": source.get("authority_ref"),
                "provider_receipt_id": source.get("provider_receipt_id"),
            })
    referenced = {
        str(item)
        for operation in contract.get("operations", [])
        if isinstance(operation, dict)
        for item in operation.get("consumers", [])
    }
    flow_steps = {
        str(item.get("id"))
        for item in contract.get("architecture", {}).get("runtime_flow", {}).get("steps", [])
        if isinstance(item, dict)
    }
    unknown_consumers = sorted(item for item in referenced if item.startswith("step:") and item[5:] not in flow_steps)
    if unknown_consumers:
        errors.append(f"operations reference unknown runtime consumers {unknown_consumers}")
    return OperationAuthorityResult(not errors, tuple(decisions), tuple(errors))


def _source_texts(project_dir: Path, glob: str) -> list[tuple[Path, str]]:
    return [
        (path, path.read_text(encoding="utf-8", errors="ignore"))
        for path in sorted(project_dir.glob(glob))
        if path.is_file() and path.suffix.lower() in {".c", ".cc", ".cpp", ".h", ".hpp"}
    ]


def validate_implementation_completeness(
    project_dir: Path, contract: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    for operation in contract.get("operations", []):
        operation_id = str(operation.get("operation_id") or "")
        matched = False
        for assertion in operation.get("implementation_assertions", []):
            path_glob = str(assertion.get("path_glob") or "")
            symbol = str(assertion.get("symbol") or "")
            required_tokens = [str(item) for item in assertion.get("required_tokens", [])]
            for path, text in _source_texts(project_dir, path_glob):
                if symbol and not re.search(rf"\b{re.escape(symbol)}\s*\(", text):
                    continue
                if not all(token in text for token in required_tokens):
                    continue
                body_match = re.search(
                    rf"\b{re.escape(symbol)}\s*\([^)]*\)\s*\{{(?P<body>.*?)\n\}}",
                    text, re.S,
                ) if symbol else None
                body = body_match.group("body") if body_match else text
                if "ESP_ERR_NOT_SUPPORTED" in body:
                    errors.append(
                        f"operation {operation_id!r} implementation {path.as_posix()} "
                        "returns ESP_ERR_NOT_SUPPORTED"
                    )
                    continue
                matched = True
        if not matched:
            errors.append(f"operation {operation_id!r} has no complete source/link assertion")
    return errors


def validate_linked_operations(
    build_dir: Path, contract: dict[str, Any],
) -> list[str]:
    maps = sorted(build_dir.glob("*.map"))
    if not maps:
        return ["build produced no linker map for operation completeness"]
    link_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore") for path in maps
    )
    errors: list[str] = []
    for operation in contract.get("operations", []):
        operation_id = str(operation.get("operation_id") or "")
        symbols = {
            str(assertion.get("link_symbol") or assertion.get("symbol") or "")
            for assertion in operation.get("implementation_assertions", [])
        }
        symbols.discard("")
        if not symbols or not any(
            re.search(rf"\b{re.escape(symbol)}\b", link_text)
            for symbol in symbols
        ):
            errors.append(
                f"operation {operation_id!r} has no linked implementation symbol"
            )
    return errors
