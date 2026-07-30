"""Deterministic reuse gate for already-materialized project components."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

from .contract_consistency import validate_source_facts


@dataclass(frozen=True)
class ImplementationReuseDecision:
    owner: str
    reusable: bool
    source_digest: str | None
    reasons: list[str]


def _source_files(project_dir: Path, owner: str) -> tuple[Path | None, list[Path], list[str]]:
    errors: list[str] = []
    if not (project_dir / "CMakeLists.txt").is_file():
        errors.append("project CMakeLists.txt is missing")
    if owner == "integration":
        source_root = project_dir / "main"
        files = sorted(path for path in source_root.rglob("*") if path.is_file()) if source_root.is_dir() else []
        if not any(path.suffix == ".c" for path in files):
            errors.append("integration requires materialized main/ C source")
        return source_root, files, errors
    source_root = project_dir / "components" / owner
    files = sorted(path for path in source_root.rglob("*") if path.is_file()) if source_root.is_dir() else []
    if not (source_root / "CMakeLists.txt").is_file():
        errors.append(f"component {owner!r} CMakeLists.txt is missing")
    if not any(path.suffix == ".c" for path in files):
        errors.append(f"component {owner!r} has no C source")
    include_root = source_root / "include"
    if not include_root.is_dir() or not any(include_root.rglob("*.h")):
        errors.append(f"component {owner!r} has no public semantic header")
    return source_root, files, errors


def assess_existing_implementation(
    project_dir: Path,
    owner: str,
    contract: dict[str, Any],
    implementation_facts: list[dict[str, Any]],
) -> ImplementationReuseDecision:
    """Decide whether existing source may proceed directly to verification.

    This gate is deliberately deterministic: a present component is reused only
    when its structure and every receipt-bound source assertion are already
    satisfied.  It never invokes an implementation agent.  The caller may ask
    for a minimal repair only when this decision is negative.
    """
    _, files, errors = _source_files(project_dir, owner)
    errors.extend(validate_source_facts(
        project_dir,
        contract,
        {owner},
        additional_facts=implementation_facts,
    ))
    # A component selftest is not verifiable merely because its function was
    # compiled.  Its frozen test setup boots app_main(), so the product
    # composition must make the retained selftest reachable from main/.  Keep
    # this in the deterministic reuse gate: otherwise an agent can create a
    # correct component that is silently absent from every hardware run.
    selftest_symbol = f"{owner}_selftest("
    component_defines_selftest = any(
        path.suffix == ".c" and selftest_symbol in path.read_text(
            encoding="utf-8", errors="ignore"
        )
        for path in files
    )
    main_files = sorted(
        path for path in (project_dir / "main").rglob("*.c")
    ) if (project_dir / "main").is_dir() else []
    if component_defines_selftest and not any(
        selftest_symbol in path.read_text(encoding="utf-8", errors="ignore")
        for path in main_files
    ):
        errors.append(
            f"component {owner!r} selftest is not composed by main/"
        )
    if errors:
        return ImplementationReuseDecision(owner, False, None, errors)
    hasher = hashlib.sha256()
    # main/ composition changes alter what the selftest image actually runs;
    # include it in the reuse digest so existing evidence is never bound to a
    # stale composition.
    for path in files + main_files:
        hasher.update(path.relative_to(project_dir).as_posix().encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(path.read_bytes()).digest())
    return ImplementationReuseDecision(owner, True, hasher.hexdigest(), [])
