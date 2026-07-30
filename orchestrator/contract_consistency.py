"""Deterministic source-to-contract checks run before hardware side effects."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any


_SOURCE_ROOTS = ("main", "components")
_EVIDENCE_SHA256_TOKEN = re.compile(r"^sha256:[0-9a-f]{64}$", re.IGNORECASE)


def _is_evidence_locator_token(token: str) -> bool:
    """Return whether a token locates evidence rather than implementation.

    Source assertions bind values to project source.  Provider workspaces may
    also leak their extracted-datasheet filename or a receipt hash into that
    list; neither can (or should) be represented by firmware source.
    """
    return token == "datasheet-extracted.txt" or bool(_EVIDENCE_SHA256_TOKEN.fullmatch(token))


def _matches_project_glob(path: str, pattern: str) -> bool:
    """Match a project assertion glob, where ``**`` also permits zero dirs."""
    return fnmatch.fnmatch(path, pattern) or (
        "/**/" in pattern
        and fnmatch.fnmatch(path, pattern.replace("/**/", "/"))
    )


def validate_source_facts(
    project_dir: Path,
    contract: dict[str, Any],
    owners: set[str] | None = None,
    additional_facts: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Verify declared implementation facts are represented in project source.

    The contract deliberately uses source tokens rather than a language parser:
    ESP-IDF components contain C, CMake and Kconfig, and a token assertion is
    portable while still preventing a grounded value from silently disappearing
    before a build/flash transaction.  Assertions are scoped to project-owned
    source and may not escape the project root.
    """
    errors: list[str] = []
    files = [
        path for root in _SOURCE_ROOTS
        for path in (project_dir / root).rglob("*")
        if path.is_file()
    ]
    facts = list(contract.get("implementation_facts", []))
    facts.extend(additional_facts or [])
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        owner = str(fact.get("subsystem_id") or "")
        if owners is not None and owner not in owners:
            continue
        fact_id = str(fact.get("id") or "<unnamed>")
        for assertion in fact.get("source_assertions", []):
            if not isinstance(assertion, dict):
                errors.append(f"implementation fact {fact_id!r} has a malformed source assertion")
                continue
            pattern = str(assertion.get("path_glob") or "").replace("\\", "/")
            if not pattern or pattern.startswith("/") or ".." in Path(pattern).parts:
                errors.append(f"implementation fact {fact_id!r} has unsafe source path_glob")
                continue
            matched = [
                path for path in files
                if _matches_project_glob(
                    path.relative_to(project_dir).as_posix(), pattern
                )
            ]
            if not matched:
                errors.append(f"implementation fact {fact_id!r} source assertion matches no project source: {pattern}")
                continue
            text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in matched)
            required_tokens = [
                str(token) for token in assertion.get("required_tokens", [])
                if not _is_evidence_locator_token(str(token))
            ]
            missing = [token for token in required_tokens if token not in text]
            if missing:
                errors.append(f"implementation fact {fact_id!r} source assertion is missing tokens {missing} in {pattern}")
    return errors


def validate_runtime_flow_source(project_dir: Path, contract: dict[str, Any]) -> list[str]:
    """Bind a schema-1.6 production flow to non-selftest project source.

    This is intentionally separate from implementation facts: a source token
    can prove a driver parameter while still leaving the driver disconnected
    from normal runtime.  Runtime-flow assertions are design-owned wiring
    promises and are checked before integration and release side effects.
    """
    flow = contract.get("architecture", {}).get("runtime_flow")
    if not isinstance(flow, dict):
        return []
    files = [
        path for root in _SOURCE_ROOTS
        for path in (project_dir / root).rglob("*")
        if path.is_file() and "_selftest." not in path.name
    ]
    errors: list[str] = []
    main_files = list((project_dir / "main").glob("*.c"))
    main_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in main_files)
    entrypoint = flow.get("entrypoint") or {}
    entry_symbol = str(entrypoint.get("symbol") or "")
    if entry_symbol and not re.search(r"\b" + re.escape(entry_symbol) + r"\s*\(", main_text):
        errors.append(f"runtime-flow entrypoint symbol {entry_symbol!r} is not called from main/")
    for step in flow.get("steps") or []:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or "<unnamed>")
        symbol = str(step.get("symbol") or "")
        text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in files)
        if symbol and not re.search(r"\b" + re.escape(symbol) + r"\s*\(", text):
            errors.append(f"runtime-flow step {step_id!r} symbol {symbol!r} is absent from production source")
        for assertion in step.get("source_assertions") or []:
            pattern = str(assertion.get("path_glob") or "").replace("\\", "/")
            if not pattern or pattern.startswith("/") or ".." in Path(pattern).parts:
                errors.append(f"runtime-flow step {step_id!r} has unsafe source path_glob")
                continue
            matched = [
                path for path in files
                if _matches_project_glob(path.relative_to(project_dir).as_posix(), pattern)
            ]
            if not matched:
                errors.append(f"runtime-flow step {step_id!r} source assertion matches no production source: {pattern}")
                continue
            source = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in matched)
            missing = [str(token) for token in assertion.get("required_tokens", []) if str(token) not in source]
            if missing:
                errors.append(f"runtime-flow step {step_id!r} is missing source tokens {missing} in {pattern}")
    return errors
