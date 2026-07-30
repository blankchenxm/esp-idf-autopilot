from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


MAX_VERIFICATION_ROOTS = 12
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class HygieneReport:
    free_bytes: int
    verification_roots: int
    verification_bytes: int
    pruned_roots: tuple[str, ...]


def _directory_bytes(root: Path) -> int:
    return sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file()
    ) if root.is_dir() else 0


def validate_generated_file_hygiene(
    repo_root: Path, project_dir: Path, *, prune: bool = False,
) -> tuple[HygieneReport, list[str]]:
    errors: list[str] = []
    ignore = (repo_root / ".gitignore").read_text(
        encoding="utf-8", errors="ignore"
    )
    if "projects/*/.v/" not in ignore:
        errors.append("verification root projects/*/.v/ is not ignored")
    verification = project_dir / ".v"
    roots = sorted(
        (item for item in verification.iterdir() if item.is_dir()),
        key=lambda item: item.stat().st_mtime,
    ) if verification.is_dir() else []
    pruned: list[str] = []
    if prune and len(roots) > MAX_VERIFICATION_ROOTS:
        for root in roots[:-MAX_VERIFICATION_ROOTS]:
            resolved = root.resolve()
            resolved.relative_to(verification.resolve())
            shutil.rmtree(resolved)
            pruned.append(root.name)
        roots = roots[-MAX_VERIFICATION_ROOTS:]
    free = shutil.disk_usage(project_dir).free
    if free < MIN_FREE_BYTES:
        errors.append(
            f"local disk free space {free} is below required {MIN_FREE_BYTES}"
        )
    return HygieneReport(
        free_bytes=free,
        verification_roots=len(roots),
        verification_bytes=_directory_bytes(verification),
        pruned_roots=tuple(pruned),
    ), errors
