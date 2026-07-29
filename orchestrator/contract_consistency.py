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
                if fnmatch.fnmatch(path.relative_to(project_dir).as_posix(), pattern)
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
