from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .models import FailureCategory, FailureDisposition


SIGNALS: list[tuple[FailureCategory, re.Pattern[str]]] = [
    (
        FailureCategory.ENVIRONMENT,
        re.compile(
            r"activate\.local\.ps1|ESP-IDF environment.*(?:missing|failed|broken)|"
            r"activation.*(?:missing|failed|broken)|"
            r"IDF_PATH.*(?:not set|missing|invalid|not found)|"
            r"(?:not set|missing).*IDF_PATH",
            re.I,
        ),
    ),
    # Stage-0 capability probing touches several MCP servers.  A transport or
    # task-group failure there is retryable tooling, not evidence that a
    # Registry query returned an authoritative outage.
    (FailureCategory.TOOL, re.compile(r"mandatory MCP capability probe|TaskGroup", re.I)),
    (FailureCategory.REGISTRY, re.compile(r"registry|component.*not found|mcp", re.I)),
    (FailureCategory.LINK, re.compile(r"undefined reference|multiple definition|linker", re.I)),
    (FailureCategory.API, re.compile(r"implicit declaration|incompatible.*argument|no member named|undeclared|not declared|does not name a type", re.I)),
    (FailureCategory.FLASH, re.compile(r"failed to connect|write timeout|flash.*failed", re.I)),
    (FailureCategory.SERIAL, re.compile(r"access is denied|port.*busy|serial|marker.*missing", re.I)),
    (FailureCategory.WATCHDOG, re.compile(r"watchdog|task_wdt", re.I)),
    (FailureCategory.BACKPRESSURE, re.compile(r"queue.*full|overflow|dropped", re.I)),
    (FailureCategory.SYNCHRONIZATION, re.compile(r"deadlock|race|mutex|semaphore", re.I)),
    (FailureCategory.STORAGE, re.compile(r"nvs|filesystem|storage|no space", re.I)),
    (FailureCategory.PROTOCOL, re.compile(r"tls|http|mqtt|auth|protocol", re.I)),
    (FailureCategory.PERFORMANCE, re.compile(r"latency|jitter|throughput|deadline", re.I)),
    (FailureCategory.BUILD, re.compile(r"cmake error|ninja: build stopped|compilation terminated", re.I)),
]


def classify_failure(text: str) -> FailureCategory:
    for category, pattern in SIGNALS:
        if pattern.search(text):
            return category
    return FailureCategory.UNKNOWN


def disposition_for(
    category: FailureCategory,
    *,
    retryable: bool = True,
    node: str | None = None,
) -> FailureDisposition:
    """Map a technical cause to policy without parsing presentation text."""
    if category == FailureCategory.LIMITATION:
        return FailureDisposition.HARD_EXTERNAL_BLOCKER
    if category == FailureCategory.ENVIRONMENT and not retryable:
        return FailureDisposition.HARD_EXTERNAL_BLOCKER
    if category == FailureCategory.HARDWARE and not retryable:
        # A missing or ambiguous board/port is an external hardware condition.
        # It must not be routed to the implementation agent: preflight is not
        # a contract owner and cannot be repaired by changing project source.
        return FailureDisposition.HARD_EXTERNAL_BLOCKER
    if category in {
        FailureCategory.FLASH,
        FailureCategory.SERIAL,
        FailureCategory.HARDWARE,
        FailureCategory.TOOL,
        FailureCategory.REGISTRY,
    } and retryable:
        return FailureDisposition.RETRY_TRANSIENT
    if category in {
        FailureCategory.BUILD,
        FailureCategory.API,
        FailureCategory.LINK,
        FailureCategory.DATA_PATH,
        FailureCategory.STATE_MACHINE,
        FailureCategory.WATCHDOG,
        FailureCategory.BACKPRESSURE,
        FailureCategory.SYNCHRONIZATION,
        FailureCategory.STORAGE,
        FailureCategory.PROTOCOL,
        FailureCategory.PERFORMANCE,
        FailureCategory.INTEGRATION,
        FailureCategory.DATASHEET,
    }:
        return FailureDisposition.REPAIR_INTERNAL
    if category == FailureCategory.UNKNOWN:
        return FailureDisposition.INTERNAL_FAULT
    return FailureDisposition.REPAIR_INTERNAL


def progress_fingerprint(state: dict[str, Any]) -> str:
    material = {key: state.get(key) for key in ("run_id", "phase", "cursor", "next_action", "subsystem_index", "failure", "receipt_ids", "evidence_ids")}
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


def material_fingerprint(project_dir: Path, state: dict[str, Any]) -> str:
    """Hash project source and harness code that can materially alter a retry."""
    payload: list[tuple[str, str]] = []
    allowed_roots = (project_dir / "main", project_dir / "components")
    for root in allowed_roots:
        if root.is_dir():
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                payload.append((path.relative_to(project_dir).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    for name in ("CMakeLists.txt", "Kconfig.projbuild", "sdkconfig", "sdkconfig.defaults"):
        path = project_dir / name
        if path.is_file(): payload.append((name, hashlib.sha256(path.read_bytes()).hexdigest()))
    harness_root = project_dir.parents[1] / "orchestrator"
    for path in sorted(item for item in harness_root.rglob("*.py") if item.is_file()):
        payload.append((f"harness/{path.relative_to(harness_root).as_posix()}", hashlib.sha256(path.read_bytes()).hexdigest()))
    payload.append(("authority", json.dumps({"design_digest": state.get("design_digest"), "hardware_identity": state.get("hardware_identity"), "subsystem_index": state.get("subsystem_index")}, sort_keys=True)))
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def failure_fingerprint(node: str, category: FailureCategory, summary: str, material: str) -> str:
    normalized = re.sub(r"[0-9a-f]{8,}|COM\d+|\d+", "#", summary.lower())
    # Disposable implementation mirrors include a random owner-workspace
    # suffix.  It is not material change: retaining it makes a repeated
    # Windows path failure look novel forever and defeats bounded recovery.
    normalized = re.sub(
        r"agent-work(?:\\\\|/)[^\\\\/\s]+|crumb-[a-z0-9_-]+",
        "<agent-work>", normalized,
    )
    return hashlib.sha256(json.dumps({"node": node, "category": category.value, "summary": normalized, "material": material}, sort_keys=True).encode()).hexdigest()


def recovery_budget(category: FailureCategory) -> int:
    return {
        FailureCategory.FLASH: 3, FailureCategory.SERIAL: 3, FailureCategory.HARDWARE: 2,
        FailureCategory.TOOL: 2, FailureCategory.BUILD: 2, FailureCategory.API: 2,
        FailureCategory.LINK: 2, FailureCategory.INTEGRATION: 2, FailureCategory.UNKNOWN: 1,
    }.get(category, 1)


def retry_allowed(category: FailureCategory, attempt: int) -> bool:
    budgets = {FailureCategory.FLASH: 2, FailureCategory.SERIAL: 2, FailureCategory.REGISTRY: 2, FailureCategory.TOOL: 2}
    return attempt <= budgets.get(category, 1)


def affected_consumers(owner: str, dependencies: dict[str, list[str]]) -> set[str]:
    affected = {owner}
    changed = True
    while changed:
        changed = False
        for node, deps in dependencies.items():
            if node not in affected and affected.intersection(deps):
                affected.add(node)
                changed = True
    return affected
