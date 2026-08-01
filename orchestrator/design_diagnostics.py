from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .models import (
    Diagnostic,
    DiagnosticSeverity,
    FailureCategory,
    FailureDisposition,
)


def design_diagnostic(
    *,
    code: str,
    summary: str,
    cause: FailureCategory,
    disposition: FailureDisposition,
    responsible_party: str,
    affected_owner: str | None = None,
    user_action_required: str | None = None,
    retry_scope: str | None = None,
    severity: DiagnosticSeverity = DiagnosticSeverity.BLOCKING,
) -> dict[str, Any]:
    """Create the sole routing representation used by the Design Graph."""
    return Diagnostic(
        code=code,
        summary=summary,
        cause=cause,
        disposition=disposition,
        severity=severity,
        responsible_party=responsible_party,
        affected_owner=affected_owner,
        subsystem_id=affected_owner,
        user_action_required=user_action_required,
        retry_scope=retry_scope,
    ).model_dump(mode="json")


def structural_diagnostics(errors: Iterable[str]) -> list[dict[str, Any]]:
    """Preserve validator messages as presentation, with explicit producer policy.

    The deterministic contract validator owns these errors, so the routing
    metadata is known without parsing the English text.
    """
    return [
        design_diagnostic(
            code="CONTRACT_VALIDATION_FAILED",
            summary=str(error),
            cause=FailureCategory.DATA_PATH,
            disposition=FailureDisposition.REPAIR_INTERNAL,
            responsible_party="design_provider",
            retry_scope="contract_section",
        )
        for error in errors
    ]


def planning_diagnostics(errors: Iterable[str]) -> list[dict[str, Any]]:
    return [
        design_diagnostic(
            code="GROUNDING_PLAN_INVALID",
            summary=str(error),
            cause=FailureCategory.DATA_PATH,
            disposition=FailureDisposition.REPAIR_INTERNAL,
            responsible_party="design_provider",
            retry_scope="inventory_item",
        )
        for error in errors
    ]


def user_decision_diagnostics(
    unknowns: Iterable[dict[str, Any] | str],
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for unknown in unknowns:
        if isinstance(unknown, dict):
            code = str(unknown.get("code") or "MODEL_PROPOSAL_INVALID")
            if code in {"USER_DECISION", "INPUT_AMBIGUITY"}:
                values.append(design_diagnostic(code=code, summary=f"design decision required: {unknown.get('owner')}/{unknown.get('category')}", cause=FailureCategory.LIMITATION, disposition=FailureDisposition.WAITING_HUMAN, responsible_party="product_owner", affected_owner=str(unknown.get("owner") or ""), user_action_required=json.dumps(unknown, ensure_ascii=False)))
            elif code == "EXTERNAL_ACQUISITION":
                values.append(design_diagnostic(code=code, summary=f"external acquisition pending for {unknown.get('owner')}", cause=FailureCategory.DATA_PATH, disposition=FailureDisposition.RETRY_TRANSIENT, responsible_party="design_grounding", affected_owner=str(unknown.get("owner") or "")))
            elif code == "INTERNAL_FAULT":
                values.append(design_diagnostic(code=code, summary=f"internal design fault for {unknown.get('owner')}", cause=FailureCategory.DATA_PATH, disposition=FailureDisposition.INTERNAL_FAULT, responsible_party="harness", affected_owner=str(unknown.get("owner") or "")))
            continue
        text = str(unknown)
        if text.strip().casefold().startswith("[user_decision]"):
            values.append(design_diagnostic(
                code="PRODUCT_DECISION_REQUIRED",
                summary=f"blocking unknown: {text}",
                cause=FailureCategory.LIMITATION,
                disposition=FailureDisposition.WAITING_HUMAN,
                responsible_party="product_owner",
                user_action_required=text,
            ))
        else:
            values.append(design_diagnostic(
                code="TECHNICAL_UNKNOWN_UNRESOLVED",
                summary=(
                    "provider left an untyped technical unknown instead of "
                    f"an auditable design proposal: {text}"
                ),
                cause=FailureCategory.DATA_PATH,
                disposition=FailureDisposition.REPAIR_INTERNAL,
                responsible_party="design_provider",
                retry_scope="technical_unknown",
            ))
    return values


def diagnostic_summaries(values: Iterable[dict[str, Any]]) -> list[str]:
    return [str(value.get("summary") or value.get("code") or "design diagnostic") for value in values]


def diagnostic_set_fingerprint(values: Iterable[dict[str, Any]]) -> str:
    """Fingerprint one typed blocking result for no-progress detection."""
    material = sorted(
        (
            str(value.get("code") or ""),
            str(value.get("responsible_party") or ""),
            str(value.get("retry_scope") or ""),
            str(value.get("affected_owner") or ""),
            str(value.get("disposition") or ""),
            str(value.get("severity") or ""),
            str(value.get("summary") or ""),
        )
        for value in values
        if value.get("severity", DiagnosticSeverity.BLOCKING.value)
        == DiagnosticSeverity.BLOCKING.value
    )
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def design_route(
    diagnostics: Iterable[dict[str, Any]],
    *,
    repeated_material: bool = False,
) -> tuple[str, str, str]:
    """Route only on typed disposition/severity; summaries are never inspected."""
    blocking = [
        Diagnostic.model_validate(value)
        for value in diagnostics
        if value.get("severity", DiagnosticSeverity.BLOCKING.value)
        == DiagnosticSeverity.BLOCKING.value
    ]
    if not blocking:
        return "promote", "DESIGN_RUNNING", "promote"
    dispositions = {item.disposition for item in blocking}
    repairable = dispositions.intersection(
        {
            FailureDisposition.REPAIR_INTERNAL,
            FailureDisposition.RETRY_TRANSIENT,
        }
    )
    if repairable:
        if not repeated_material:
            return "repair", "DESIGN_RUNNING", "repair"
        # A repeated, receipt-backed source acquisition failure is an external
        # blocker. Other identical typed diagnostic sets are internal stalls.
        if any(item.code in {"DATASHEET_GROUNDING_FAILED", "REGISTRY_SEARCH_FAILED", "REGISTRY_CANDIDATE_DETAIL_FAILED"} for item in blocking):
            return "blocked", "BLOCKED", "blocked"
        return "faulted", "FAULTED", "faulted"
    if dispositions == {FailureDisposition.WAITING_HUMAN}:
        return "waiting_input", "WAITING_DESIGN_INPUT", "waiting_input"
    if FailureDisposition.HARD_EXTERNAL_BLOCKER in dispositions:
        return "blocked", "BLOCKED", "blocked"
    if FailureDisposition.INTERNAL_FAULT in dispositions:
        return "faulted", "FAULTED", "faulted"
    return "faulted", "FAULTED", "faulted"
