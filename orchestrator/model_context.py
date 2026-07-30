from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contract_views import owner_contract_view
from .secrets import redact_text
from .storage import digest


CONTEXT_SCHEMA_VERSION = "1.0"
MAX_CONTEXT_BYTES = 128 * 1024
MAX_LOG_BYTES = 16 * 1024
MAX_DESIGN_CONTEXT_BYTES = 512 * 1024


def _sha256_file(path: Path) -> str:
    import hashlib

    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _source_manifest(project_dir: Path, owner: str) -> list[dict[str, Any]]:
    roots = [
        project_dir / "components" / owner,
        project_dir / "main",
    ]
    root_files = [
        project_dir / "CMakeLists.txt",
        project_dir / "Kconfig.projbuild",
        project_dir / "sdkconfig.defaults",
    ]
    files = [path for path in root_files if path.is_file()]
    for root in roots:
        if root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return [
        {
            "path": path.relative_to(project_dir).as_posix(),
            "sha256": _sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in sorted(set(files))
    ]


def _bounded_failure_excerpts(
    paths: tuple[Path, ...],
    secret_values: list[str],
) -> list[dict[str, Any]]:
    remaining = MAX_LOG_BYTES
    excerpts: list[dict[str, Any]] = []
    for path in paths:
        if remaining <= 0 or not path.is_file():
            break
        raw = path.read_bytes()
        selected = raw[-remaining:]
        text = selected.decode("utf-8", errors="replace")
        for value in secret_values:
            if value:
                text = text.replace(value, "[REDACTED]")
        encoded = text.encode("utf-8")
        remaining -= len(encoded)
        excerpts.append({
            "artifact_path": str(path),
            "artifact_sha256": _sha256_file(path),
            "artifact_size": len(raw),
            "excerpt": text,
            "excerpt_truncated": len(selected) < len(raw),
        })
    return excerpts


def _redact_known_values(text: str, values: list[str]) -> str:
    redacted = redact_text(text)
    for value in values:
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def build_owner_context_envelope(
    *,
    project: str,
    run_id: str,
    design_digest: str,
    owner: str,
    reason: str,
    instruction: str,
    contract: dict[str, Any],
    project_dir: Path,
    implementation_addendum: dict[str, Any] | None,
    failure_logs: tuple[Path, ...],
    secret_values: list[str],
) -> dict[str, Any]:
    contract_slice = owner_contract_view(contract, owner)
    operation_ids = [
        str(item)
        for item in contract_slice.get("subsystem", {}).get(
            "required_operations", []
        )
    ]
    test_ids = [
        str(item.get("test_id"))
        for item in contract_slice.get("verification", [])
        if item.get("test_id")
    ]
    authority_refs: list[dict[str, Any]] = []
    if implementation_addendum is not None:
        authority_refs.append({
            "kind": "implementation_addendum",
            "digest": implementation_addendum.get("addendum_digest")
            or digest(implementation_addendum),
            "operation_authorities": implementation_addendum.get(
                "operation_authorities", []
            ),
            "source_receipts": implementation_addendum.get(
                "source_receipts", []
            ),
        })
    base = {
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "reason": reason,
        "project": project,
        "run_id": run_id,
        "design_digest": design_digest,
        "node": "implementation_agent",
        "owner": owner,
        "operation_ids": operation_ids,
        "test_ids": test_ids,
        "instruction": _redact_known_values(instruction, secret_values),
        "contract_slice": contract_slice,
        "authority_refs": authority_refs,
        "source_manifest": _source_manifest(project_dir, owner),
        "diagnostic_excerpts": _bounded_failure_excerpts(
            failure_logs, secret_values
        ),
        "modification_allowlist": [
            "CMakeLists.txt",
            "Kconfig.projbuild",
            "sdkconfig",
            "sdkconfig.*",
            "main/**",
            f"components/{owner}/**",
        ],
        "budgets": {
            "max_context_bytes": MAX_CONTEXT_BYTES,
            "max_log_bytes": MAX_LOG_BYTES,
            "max_model_transactions": 1,
            "max_followups": 1,
        },
        "redactions": {
            "plaintext_secret_count": len(
                [value for value in secret_values if value]
            ),
            "plaintext_values_included": False,
        },
    }
    envelope = {**base, "context_digest": digest(base)}
    serialized = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(serialized) > MAX_CONTEXT_BYTES:
        raise ValueError(
            f"model context envelope exceeds {MAX_CONTEXT_BYTES} bytes: "
            f"{len(serialized)}"
        )
    return envelope


def build_readonly_context_envelope(
    *,
    project: str,
    reason: str,
    instruction: str,
    workspace: Path,
    authority_files: list[Path],
    secret_values: list[str] | None = None,
    max_context_bytes: int = MAX_DESIGN_CONTEXT_BYTES,
) -> dict[str, Any]:
    """Build a fresh-context packet for Design and targeted readers."""
    secrets = secret_values or []
    files = []
    for path in sorted(set(authority_files)):
        resolved = path.resolve()
        if not resolved.is_file():
            continue
        try:
            relative = resolved.relative_to(workspace.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"model authority file escapes workspace: {resolved}"
            ) from exc
        files.append({
            "path": relative,
            "sha256": _sha256_file(resolved),
            "size": resolved.stat().st_size,
        })
    base = {
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "reason": reason,
        "project": project,
        "instruction": _redact_known_values(instruction, secrets),
        "authority_files": files,
        "modification_allowlist": [],
        "budgets": {
            "max_context_bytes": max_context_bytes,
            "max_model_transactions": 1,
            "max_followups": 0,
        },
        "redactions": {
            "plaintext_secret_count": len([value for value in secrets if value]),
            "plaintext_values_included": False,
        },
    }
    envelope = {**base, "context_digest": digest(base)}
    serialized = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(serialized) > max_context_bytes:
        raise ValueError(
            f"model context envelope exceeds {max_context_bytes} bytes: "
            f"{len(serialized)}"
        )
    return envelope


def parse_codex_jsonl_usage(output: str) -> dict[str, Any]:
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    events = 0
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        events += 1
        usage = value.get("usage")
        if not isinstance(usage, dict):
            continue
        for key in totals:
            totals[key] += int(usage.get(key) or 0)
    totals["total_tokens"] = (
        totals["input_tokens"]
        + totals["output_tokens"]
        + totals["reasoning_output_tokens"]
    )
    totals["event_count"] = events
    totals["source"] = "codex_jsonl" if events else "unavailable"
    return totals
