from __future__ import annotations

import copy
import re
from typing import Any


_OPERATION_TERMS: dict[str, tuple[str, ...]] = {
    "initialize": ("initialize", "initialization", "init_device", "init_with_layers"),
    "identify": ("identify", "identity", "device id", "jedec id", "read id"),
    "read": ("read", "read page", "read_page"),
    "program": ("program", "write", "write page", "write_page"),
    "erase": ("erase", "erase block", "trim"),
    "check_ecc": ("ecc", "error correction"),
    "scan_bad_blocks": ("bad block", "bad blocks", "bad-block"),
    "reset_recovery": ("reset", "recover", "recovery"),
    "power_config": ("power", "voltage", "supply"),
    "charger_config": ("charger", "charge current", "input current"),
    "firmware_update": ("firmware update", "ota"),
    "otp": ("otp", "one-time programmable"),
    "efuse": ("efuse", "e-fuse"),
}
_VERSION_PATTERNS = (
    re.compile(
        r"latest\s+version\s*:\s*v?(?P<version>\d+\.\d+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"component\b[^\n]*?[-–]\s*v?(?P<version>\d+\.\d+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
)


def _detail_text(detail: dict[str, Any]) -> str:
    fragments: list[str] = []
    for item in detail.get("content", []):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            fragments.append(item["text"])
        elif isinstance(item, str):
            fragments.append(item)
    for key in ("description", "documentation", "readme"):
        if isinstance(detail.get(key), str):
            fragments.append(detail[key])
    return "\n".join(fragments)


def _operation_covered(operation: str, text: str) -> bool:
    normalized = operation.casefold()
    alias = next(
        (
            key for token, key in (
                ("bad_block", "scan_bad_blocks"),
                ("bad blocks", "scan_bad_blocks"),
                ("ecc", "check_ecc"),
                ("erase", "erase"),
                ("program", "program"),
                ("write", "program"),
                ("reset", "reset_recovery"),
                ("recover", "reset_recovery"),
                ("ident", "identify"),
                ("read", "read"),
                ("init", "initialize"),
            )
            if token in normalized
        ),
        operation,
    )
    terms = _OPERATION_TERMS.get(alias, (operation.replace("_", " "),))
    folded = text.casefold()
    return any(
        re.search(
            r"(?<![a-z0-9_])"
            + re.escape(term.casefold())
            + r"(?![a-z0-9_])",
            folded,
        )
        is not None
        for term in terms
    )


def _version(detail: dict[str, Any], text: str) -> str | None:
    declared = detail.get("version") or detail.get("latest_version")
    if declared:
        return str(declared)
    for pattern in _VERSION_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group("version")
    return None


def _compatible(
    detail: dict[str, Any],
    *,
    part_number: str,
    idf_version: str,
    target: str,
) -> tuple[bool, list[str]]:
    text = _detail_text(detail)
    folded = text.casefold()
    evidence: list[str] = []
    if part_number and part_number.casefold() not in folded:
        return False, [f"documentation does not name exact part {part_number}"]
    if part_number:
        evidence.append(f"documents exact part {part_number}")
    if (
        "supports all targets" in folded
        or "all esp targets" in folded
        or target.casefold() in folded
    ):
        evidence.append(f"supports target {target}")
    elif target:
        return False, [f"documentation does not establish target {target} support"]
    major = idf_version.split(".", 1)[0]
    if "esp-idf" in folded and (
        idf_version.casefold() in folded
        or f"esp-idf {major}" in folded
        or "esp-idf >=5.0" in folded
        or "esp-idf 5.0" in folded
    ):
        evidence.append(f"documents ESP-IDF compatibility for {idf_version}")
    else:
        return False, [f"documentation does not establish ESP-IDF {idf_version} compatibility"]
    return True, evidence


def finalize_component_selection(
    selection: dict[str, Any],
    *,
    part_number: str,
    idf_version: str,
    target: str,
    required_operations: list[str],
) -> dict[str, Any]:
    """Make the final selection only after receipt-owned Registry details exist.

    Provider draft choices are deliberately ignored. The deterministic policy
    adopts a compatible Registry candidate with the greatest required-operation
    coverage, preferring Espressif's namespace on a tie. A custom driver is the
    fallback, not the initial authority.
    """
    result = copy.deepcopy(selection)
    candidates = [str(item) for item in result.get("registry_candidates", [])]
    details = {
        str(item.get("component")): item
        for item in result.get("registry_candidate_details", [])
        if isinstance(item, dict) and item.get("component")
    }
    result.pop("selected_component", None)
    result.pop("selected_version", None)
    result["covered_operations"] = []
    result["uncovered_operations"] = list(dict.fromkeys(required_operations))
    rejections: list[dict[str, str]] = []
    scored: list[tuple[int, int, str, dict[str, Any], list[str], list[str]]] = []
    for component in candidates:
        detail = details.get(component)
        if not detail:
            rejections.append({
                "component": component,
                "reason": "Registry candidate detail receipt is missing",
            })
            continue
        compatible, evidence = _compatible(
            detail,
            part_number=part_number,
            idf_version=idf_version,
            target=target,
        )
        if not compatible:
            rejections.append({
                "component": component,
                "reason": "; ".join(evidence),
            })
            continue
        text = _detail_text(detail)
        covered = [
            operation
            for operation in dict.fromkeys(required_operations)
            if _operation_covered(operation, text)
        ]
        official = 1 if component.startswith("espressif/") else 0
        scored.append(
            (len(covered), official, component, detail, evidence, covered)
        )
    if not scored:
        result["decision"] = "custom"
        result["decision_reason"] = (
            "Registry grounding returned no compatible candidate"
            if candidates
            else "Registry grounding returned no Registry candidates"
        )
        result["rejected_candidates"] = rejections
        result["selection_evidence"] = {
            "part_number": part_number,
            "idf_version": idf_version,
            "target": target,
            "policy": "post-registry-evidence-v1",
        }
        result["selected_component"] = None
        return result
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    _, _, component, detail, evidence, covered = scored[0]
    for _, _, rejected_component, _, _, rejected_covered in scored[1:]:
        rejections.append({
            "component": rejected_component,
            "reason": (
                f"compatible but lower-priority than {component}; "
                f"covers {len(rejected_covered)}/{len(required_operations)} required operations"
            ),
        })
    unique_required = list(dict.fromkeys(required_operations))
    result.update({
        "decision": "registry",
        "selected_component": component,
        "selected_version": _version(detail, _detail_text(detail)),
        "covered_operations": covered,
        "uncovered_operations": [
            operation for operation in unique_required if operation not in covered
        ],
        "decision_reason": (
            f"Post-Registry evidence confirms exact-part, target, and ESP-IDF "
            f"compatibility; covers {len(covered)}/{len(unique_required)} "
            "required operations"
        ),
        "rejected_candidates": rejections,
        "selection_evidence": {
            "part_number": part_number,
            "idf_version": idf_version,
            "target": target,
            "compatibility": evidence,
            "policy": "post-registry-evidence-v1",
        },
    })
    return result


def required_operations_for_subsystem(
    contract: dict[str, Any], subsystem: dict[str, Any]
) -> list[str]:
    """Return explicit operations, with a conservative generic legacy bridge."""
    explicit = subsystem.get("required_operations")
    if isinstance(explicit, list) and explicit:
        return list(dict.fromkeys(str(item) for item in explicit if str(item)))
    owner = str(subsystem.get("id") or "")
    fragments = [
        str(subsystem.get("isolation_reason") or ""),
        " ".join(str(item) for item in subsystem.get("hardware_resources", [])),
    ]
    for requirement in contract.get("requirements", []):
        if isinstance(requirement, dict) and requirement.get("owner") == owner:
            fragments.append(str(requirement.get("description") or ""))
    for row in contract.get("verification", []):
        if isinstance(row, dict) and row.get("owner") == owner:
            fragments.extend([
                str(row.get("setup") or ""),
                str(row.get("stimulus") or ""),
                str(row.get("expected") or ""),
            ])
    text = "\n".join(fragments)
    discovered = [
        operation
        for operation in _OPERATION_TERMS
        if _operation_covered(operation, text)
    ]
    classification = str(subsystem.get("classification") or "")
    if classification == "external_part":
        discovered = ["initialize", "identify", "read", *discovered]
    return list(dict.fromkeys(discovered))
