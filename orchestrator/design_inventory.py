from __future__ import annotations

import copy
import re
from typing import Any


_PART_TOKEN = re.compile(r"\b[A-Z][A-Z0-9-]*\d[A-Z0-9-]*\b", re.IGNORECASE)
_PLACEHOLDERS = {"", "UNKNOWN", "PENDING", "TODO", "TBD", "N/A", "NONE"}


def _query(value: Any) -> str:
    if isinstance(value, str):
        for prefix in (
            "ESP Component Registry exact query: ",
            "ESP Component Registry query: ",
        ):
            if value.startswith(prefix):
                return value.removeprefix(prefix).strip()
        return value.strip()
    if isinstance(value, dict):
        return str(
            value.get("query")
            or value.get("exact_query")
            or value.get("capability_query")
            or ""
        ).strip()
    return ""


def _part_number(
    subsystem: dict[str, Any],
    selection: dict[str, Any] | None,
    datasheet: dict[str, Any] | None = None,
) -> str:
    """Resolve a hardware identity without relying on a project-specific table.

    New contracts must declare ``part_number`` on every external part.  The
    exact Registry query is retained as a legacy bridge because older design
    providers already used the exact silicon identifier there.  Hardware
    resource labels are a final conservative fallback and accept only a
    component-like token containing at least one digit.
    """
    declared = str(subsystem.get("part_number") or "").strip()
    if declared.upper() not in _PLACEHOLDERS:
        return declared
    sheet_part = str((datasheet or {}).get("part_number") or "").strip()
    if sheet_part.upper() not in _PLACEHOLDERS:
        return sheet_part
    exact = _query((selection or {}).get("exact_search"))
    if exact and " " not in exact and _PART_TOKEN.fullmatch(exact):
        return exact
    for resource in subsystem.get("hardware_resources", []):
        for token in _PART_TOKEN.findall(str(resource)):
            if any(char.isdigit() for char in token):
                return token
    return ""


def build_design_inventory(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the complete project-agnostic subsystem inventory.

    This is intentionally derived by the Harness after the provider draft.
    Grounding coverage therefore cannot disappear merely because the provider
    omitted a ``datasheets`` entry.
    """
    selections = {
        str(item.get("subsystem_id")): item
        for item in contract.get("component_selections", [])
        if isinstance(item, dict)
    }
    datasheets = {
        str(item.get("subsystem_id")): item
        for item in contract.get("datasheets", [])
        if isinstance(item, dict)
    }
    inventory: list[dict[str, Any]] = []
    for subsystem in contract.get("subsystems", []):
        if not isinstance(subsystem, dict):
            continue
        subsystem_id = str(subsystem.get("id") or "")
        classification = str(subsystem.get("classification") or "")
        selection = selections.get(subsystem_id)
        item = {
            "subsystem_id": subsystem_id,
            "classification": classification,
            "execution_role": subsystem.get("execution_role", "component"),
            "dependencies": list(subsystem.get("dependencies", [])),
            "registry_search_required": bool(
                subsystem.get("registry_search_required")
            ),
            "part_number": (
                _part_number(subsystem, selection, datasheets.get(subsystem_id))
                if classification == "external_part"
                else None
            ),
        }
        inventory.append(item)
    return inventory


def build_grounding_plan(
    contract: dict[str, Any], inventory: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Produce mandatory grounding work from inventory, not model omissions."""
    inventory = inventory or build_design_inventory(contract)
    selections = {
        str(item.get("subsystem_id")): item
        for item in contract.get("component_selections", [])
        if isinstance(item, dict)
    }
    plan: list[dict[str, Any]] = []
    for item in inventory:
        subsystem_id = item["subsystem_id"]
        selection = selections.get(subsystem_id)
        if item["registry_search_required"]:
            plan.append(
                {
                    "kind": "registry",
                    "subsystem_id": subsystem_id,
                    "exact_query": _query((selection or {}).get("exact_search")),
                    "capability_query": _query(
                        (selection or {}).get("capability_search")
                    ),
                    "required": True,
                }
            )
        elif selection is not None:
            plan.append(
                {
                    "kind": "local_idf",
                    "subsystem_id": subsystem_id,
                    "exact_query": _query(selection.get("exact_search")),
                    "capability_query": _query(selection.get("capability_search")),
                    "required": True,
                }
            )
        if item["classification"] == "external_part":
            plan.append(
                {
                    "kind": "datasheet",
                    "subsystem_id": subsystem_id,
                    "part_number": item["part_number"],
                    # Design approval needs identity and board-level interface,
                    # electrical and safety facts. Operation-specific
                    # implementation facts are acquired later by the Execution
                    # readiness gate, after final component selection.
                    "required_level": "L1",
                    "required": True,
                }
            )
    return plan


def materialize_grounding_requirements(
    contract: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Add missing deterministic grounding records to a provider draft.

    The returned contract remains a draft.  Empty source/document metadata are
    deliberate acquisition inputs, never claims of validated facts.
    """
    value = copy.deepcopy(contract)
    errors: list[str] = []
    definitions = {
        str(item.get("id") or ""): item
        for item in value.get("subsystems", [])
        if isinstance(item, dict)
    }
    normalizations: list[dict[str, Any]] = []
    selections: list[dict[str, Any]] = []
    for selection in value.setdefault("component_selections", []):
        if not isinstance(selection, dict):
            selections.append(selection)
            continue
        owner = str(selection.get("subsystem_id") or "")
        subsystem = definitions.get(owner, {})
        if str(subsystem.get("classification") or "") != "project_custom":
            selections.append(selection)
            continue
        # A selection is an external acquisition decision.  project_custom
        # owners are implemented in this project and cannot acquire one.
        # Remove the model's redundant entry, but fail rather than silently
        # normalizing a self-contradictory classification.
        contradictory = []
        if subsystem.get("registry_search_required"):
            contradictory.append("registry_search_required")
        if str(subsystem.get("part_number") or "").strip():
            contradictory.append("part_number")
        normalizations.append({
            "kind": "removed_project_custom_selection",
            "subsystem_id": owner,
            "reason": "project_custom owners do not participate in component selection or Registry grounding",
        })
        if contradictory:
            errors.append(
                f"{owner} is project_custom but declares incompatible "
                f"external-acquisition fields {contradictory}"
            )
    value["component_selections"] = selections
    if normalizations:
        value["grounding_normalizations"] = normalizations
    by_selection = {
        str(item.get("subsystem_id")): item
        for item in selections
        if isinstance(item, dict)
    }
    by_datasheet = {
        str(item.get("subsystem_id")): item
        for item in value.get("datasheets", [])
        if isinstance(item, dict)
    }
    for subsystem in value.get("subsystems", []):
        if not isinstance(subsystem, dict):
            continue
        owner = str(subsystem.get("id") or "")
        classification = str(subsystem.get("classification") or "")
        existing = by_selection.get(owner)
        if classification == "external_part" and existing is not None:
            # The exact Registry search is a Harness-owned identity lookup.
            # Providers may suggest useful prose, but capability wording in
            # this field can hide a real exact-part candidate (for example,
            # ``W25N01GV ESP-IDF driver`` versus ``W25N01GV``).
            part = _part_number(
                subsystem, existing, by_datasheet.get(owner)
            )
            provider_query = _query(existing.get("exact_search"))
            if part and provider_query != part:
                existing["provider_exact_search"] = provider_query
                existing["exact_search"] = part
        if classification == "project_custom" or existing is not None:
            continue
        part = str(subsystem.get("part_number") or "").strip()
        if classification == "mcu_native":
            selection = {
                "subsystem_id": owner,
                "status": "pending",
                "exact_search": f"ESP-IDF {owner}",
                "capability_search": f"ESP-IDF {owner} support",
                "decision": "local_idf",
                "decision_reason": "Harness-derived MCU-native ESP-IDF grounding",
                "provider_receipt_id": "",
                "origin": "DERIVED_GROUNDING_PLAN",
            }
        elif classification == "external_part":
            exact = part or owner
            selection = {
                "subsystem_id": owner,
                "status": "pending",
                "exact_search": exact,
                "capability_search": f"ESP-IDF {exact} driver",
                "decision": "custom",
                "decision_reason": (
                    "Harness-derived fallback pending Registry candidate evaluation"
                ),
                "provider_receipt_id": "",
                "origin": "DERIVED_GROUNDING_PLAN",
            }
        elif classification == "reusable_software":
            selection = {
                "subsystem_id": owner,
                "status": "pending",
                "exact_search": owner,
                "capability_search": f"ESP-IDF {owner}",
                "decision": "reject",
                "decision_reason": (
                    "Harness-derived fallback pending Registry candidate evaluation"
                ),
                "provider_receipt_id": "",
                "origin": "DERIVED_GROUNDING_PLAN",
            }
        else:
            continue
        selections.append(selection)
        by_selection[owner] = selection
    _normalize_verification_batches(value)
    inventory = build_design_inventory(value)
    plan = build_grounding_plan(value, inventory)
    sheets = value.setdefault("datasheets", [])
    by_owner = {
        str(item.get("subsystem_id")): item
        for item in sheets
        if isinstance(item, dict)
    }
    for work in plan:
        if work["kind"] != "datasheet":
            continue
        owner = work["subsystem_id"]
        part = str(work.get("part_number") or "").strip()
        if not part:
            errors.append(
                f"{owner} external part lacks a deterministic part_number"
            )
            continue
        if owner in by_owner:
            sheet = by_owner[owner]
            sheet.setdefault("part_number", part)
            sheet.setdefault("variant", part)
            sheet.setdefault("level", work["required_level"])
            continue
        sheet = {
            "subsystem_id": owner,
            "part_number": part,
            "variant": part,
            "document_id": "",
            "revision": "",
            "source": "",
            "content_hash": "",
            "coverage": [],
            "level": work["required_level"],
            "technical_content_valid": False,
            "provider_receipt_id": "",
            "origin": "DERIVED_GROUNDING_PLAN",
        }
        sheets.append(sheet)
        by_owner[owner] = sheet
    value["design_inventory"] = inventory
    value["grounding_plan"] = plan
    return value, inventory, plan, errors


def _normalize_verification_batches(contract: dict[str, Any]) -> None:
    """Make model-proposed v1.2 batches executable without changing owners.

    A batch is only a frozen execution grouping.  When a provider reuses its
    label after an intervening dependency, retaining that label is impossible:
    the runtime would execute two distinct batches under one identity.  Split
    the later run deterministically and preserve the original label for the
    first run.  Isolated owners are likewise always given their own batch.
    """
    from .schema_capabilities import schema_has
    version = str(contract.get("schema_version") or "")
    if not schema_has(version, "verification_batch"):
        return
    try:
        from .validators import topological_subsystems
        order = topological_subsystems(contract.get("subsystems", []))
    except (KeyError, TypeError, ValueError):
        return
    definitions = {
        str(item.get("id")): item
        for item in contract.get("subsystems", [])
        if isinstance(item, dict)
    }
    components = [
        owner for owner in order
        if definitions.get(owner, {}).get("execution_role", "component") == "component"
    ]
    if schema_has(version, "implementation_facts"):
        # A provider explicitly opts a non-destructive owner into sharing via
        # ``batch_compatible``.  Group only adjacent opted-in owners; this
        # keeps dependency order and never infers that a register/DMA/storage
        # test is safe to share merely from its name.
        shared_index = 0
        previous_shared = False
        for owner in components:
            item = definitions[owner]
            isolated = bool(item.get("isolation_required"))
            compatible = item.get("batch_compatible") is True and not isolated
            if compatible:
                if not previous_shared:
                    shared_index += 1
                item["verification_batch"] = f"shared-{shared_index}"
            else:
                item["verification_batch"] = f"isolated-{owner}"
            previous_shared = compatible
        return
    used_names: set[str] = set()
    run_counts: dict[str, int] = {}
    previous_name: str | None = None
    previous_isolated = False
    for owner in components:
        item = definitions[owner]
        name = item.get("verification_batch")
        if not isinstance(name, str) or not name:
            previous_name = None
            continue
        isolated = bool(item.get("isolation_required"))
        repeats_later = name in used_names and name != previous_name
        must_split = isolated or previous_isolated or repeats_later
        if must_split:
            index = run_counts.get(name, 1)
            candidate = name
            while candidate in used_names:
                index += 1
                candidate = f"{name}-{index}"
            item["verification_batch"] = candidate
            name = candidate
            run_counts[item.get("verification_batch", candidate).rsplit("-", 1)[0]] = index
        else:
            run_counts.setdefault(name, 1)
        used_names.add(name)
        previous_name = name
        previous_isolated = isolated


def validate_grounding_plan_coverage(contract: dict[str, Any]) -> list[str]:
    """Prove every inventory item has its mandatory grounding transaction."""
    inventory = contract.get("design_inventory")
    plan = contract.get("grounding_plan")
    if not isinstance(inventory, list) or not isinstance(plan, list):
        return ["design inventory and grounding plan are required"]
    errors: list[str] = []
    work = {
        (str(item.get("kind")), str(item.get("subsystem_id")))
        for item in plan
        if isinstance(item, dict)
    }
    sheets = {
        str(item.get("subsystem_id")): item
        for item in contract.get("datasheets", [])
        if isinstance(item, dict)
    }
    selections = {
        str(item.get("subsystem_id")): item
        for item in contract.get("component_selections", [])
        if isinstance(item, dict)
    }
    for item in inventory:
        if not isinstance(item, dict):
            errors.append("design inventory entries must be objects")
            continue
        owner = str(item.get("subsystem_id") or "")
        if item.get("registry_search_required") and ("registry", owner) not in work:
            errors.append(f"{owner} lacks a Registry grounding work item")
        if item.get("classification") == "mcu_native":
            if ("local_idf", owner) not in work:
                errors.append(f"{owner} lacks a local ESP-IDF grounding work item")
            selection = selections.get(owner)
            if not selection or not selection.get("provider_receipt_id"):
                errors.append(f"{owner} lacks a local ESP-IDF grounding receipt")
        if item.get("classification") == "external_part":
            if ("datasheet", owner) not in work:
                errors.append(f"{owner} lacks a datasheet grounding work item")
            if not item.get("part_number"):
                errors.append(f"{owner} external part lacks part_number")
            if owner not in sheets:
                errors.append(f"{owner} lacks a datasheet contract record")
            elif not sheets[owner].get("provider_receipt_id"):
                errors.append(f"{owner} lacks a datasheet grounding receipt")
    return errors


def reconcile_grounding_unknowns(
    contract: dict[str, Any],
    blocking_unknowns: list[dict[str, Any] | str],
    validation_errors: list[str],
    input_authority: dict[str, Any] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Route provisional technical statements to their authoritative gate.

    Product-policy unknowns remain user gates.  A provider may explicitly mark
    a provisional statement with ``[GROUNDING_PENDING]``.  Legacy provider
    output is also recognized when it describes unfinished acquisition work
    (Datasheet, Registry, local ESP-IDF, receipts, or implementation facts)
    rather than a product choice.  Credential handling is a repository-wide
    Harness policy, while driver/API/register/DMA detail belongs to the
    per-owner Execution-readiness gate.  Neither is a product-owner question.
    ``[USER_DECISION]`` always wins.
    """
    typed = [item for item in blocking_unknowns if isinstance(item, dict)]
    if typed:
        pins_are_authoritative = bool((input_authority or {}).get("pins"))
        def is_resolved_input_pin_record(item: dict[str, Any]) -> bool:
            return (
                item.get("code") == "INPUT_AMBIGUITY"
                and "pin_authority" in str(item.get("category") or "")
                and pins_are_authoritative
            )
        active = [
            item for item in typed
            if item.get("code") in {"USER_DECISION", "INPUT_AMBIGUITY"}
            and item.get("resolution") is None
            and not is_resolved_input_pin_record(item)
        ]
        resolved = [
            {"unknown": item, "statement": item.get("code"), "resolution": item.get("resolution") or ("Resolved from hash-bound user pin records." if is_resolved_input_pin_record(item) else f"Deferred to {item.get('phase')} owner {item.get('owner')}"), "resolution_kind": item.get("code")}
            for item in typed if item not in active
        ]
        return active, resolved
    grounding_error_tokens = (
        "registry",
        "datasheet",
        "grounding receipt",
        "grounding work item",
        "external part lacks part_number",
        "implementation fact",
    )
    if any(
        any(token in error.casefold() for token in grounding_error_tokens)
        for error in validation_errors
    ):
        return list(blocking_unknowns), []

    receipts = sorted(
        {
            str(item.get("provider_receipt_id"))
            for collection in (
                contract.get("component_selections", []),
                contract.get("datasheets", []),
            )
            for item in collection
            if isinstance(item, dict) and item.get("provider_receipt_id")
        }
    )
    active: list[str] = []
    resolved: list[dict[str, Any]] = []
    for statement in blocking_unknowns:
        normalized = str(statement).strip()
        lower = normalized.casefold()
        marked = lower.startswith("[grounding_pending]")
        user_decision = lower.startswith("[user_decision]")
        acquisition_subject = any(
            token in lower
            for token in (
                "datasheet",
                "registry",
                "local esp-idf",
                "local idf",
                "provider receipt",
                "receipt-bound",
                "grounding",
                "implementation fact",
            )
        )
        unfinished = any(
            token in lower
            for token in (
                "defer",
                "pending",
                "remain",
                "unavailable",
                "not available yet",
                "not present",
                "available yet",
                "cannot yet",
                "cannot be fixed until",
                "require receipt-bound",
                "requires receipt-bound",
            )
        )
        legacy = (
            not user_decision
            and acquisition_subject
            and unfinished
        )
        credential_policy_subject = (
            not user_decision
            and any(
                token in lower
                for token in (
                    "credential", "password", "secret", "redact",
                    "private build artifact",
                )
            )
        )
        readiness_subject = (
            not user_decision
            and any(
                token in lower
                for token in (
                    "implementation fact", "register", "bitfield", "dma",
                    "decimation", "clock mode", "driver", "esp-idf receive",
                    "safe stop", "recovery behavior", "overrun",
                )
            )
        )
        # These two categories must win over legacy "remain pending" prose.
        # The facts may be absent at Design by policy; L1 proves identity and
        # board constraints, while execution readiness binds the small,
        # operation-specific implementation addendum before source changes.
        if credential_policy_subject:
            resolved.append(
                {
                    "statement": normalized,
                    "resolution": (
                        "Closed by the Harness credential policy: authorized "
                        "values are consumed only through ignored local private "
                        "build artifacts and are redacted from generated authority "
                        "and evidence."
                    ),
                    "provider_receipt_ids": [],
                    "resolution_kind": "harness_policy",
                }
            )
        elif readiness_subject:
            resolved.append(
                {
                    "statement": normalized,
                    "resolution": (
                        "Deferred to the owner-specific Execution-readiness gate; "
                        "only missing required-operation facts will be acquired, "
                        "bound in an immutable implementation addendum, and checked "
                        "against project source before implementation."
                    ),
                    "provider_receipt_ids": receipts,
                    "resolution_kind": "execution_readiness",
                }
            )
        elif marked or legacy:
            resolved.append(
                {
                    "statement": normalized,
                    "resolution": (
                        "Superseded by the completed deterministic grounding "
                        "plan and its validated provider receipts."
                    ),
                    "provider_receipt_ids": receipts,
                    "resolution_kind": "design_grounding",
                }
            )
        else:
            active.append(normalized)
    return active, resolved


def reconcile_grounding_limitations(
    contract: dict[str, Any],
    *,
    grounding_complete: bool,
) -> list[dict[str, Any]]:
    """Make receipt-resolved acquisition limitations non-blocking and auditable."""
    if not grounding_complete:
        return []
    receipts = sorted(
        {
            str(item.get("provider_receipt_id"))
            for collection in (
                contract.get("component_selections", []),
                contract.get("datasheets", []),
            )
            for item in collection
            if isinstance(item, dict) and item.get("provider_receipt_id")
        }
    )
    resolved: list[dict[str, Any]] = []
    for limitation in contract.get("limitations", []):
        if not isinstance(limitation, dict):
            continue
        statement = str(limitation.get("statement") or "").strip()
        if not statement.casefold().startswith("[grounding_pending]"):
            continue
        prior_status = limitation.get("status")
        limitation["status"] = "resolved_grounding"
        limitation["resolution"] = (
            "Superseded by the completed deterministic grounding plan and "
            "its validated provider receipts."
        )
        limitation["provider_receipt_ids"] = receipts
        resolved.append(
            {
                "id": limitation.get("id"),
                "statement": statement,
                "prior_status": prior_status,
                "status": "resolved_grounding",
                "provider_receipt_ids": receipts,
            }
        )
    return resolved
