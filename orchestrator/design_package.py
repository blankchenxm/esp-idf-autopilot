from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import copy
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .storage import atomic_write_json, file_ref
from .storage import ProjectStore
from .codex_runner import codex_command, codex_creationflags, hidden_powershell_command, isolated_codex_profile, is_authentication_failure, terminate_process_tree
from .validators import design_digest, validate_contract, validate_design_package
from .adapters.design_grounding import DesignGroundingAdapter
from .runtime_paths import ProjectRuntime
from .secrets import assert_no_secret_values, redact_text, secret_values
from .design_inventory import materialize_grounding_requirements
from .input_authority import exact_authorized_identifier
from .input_authority import compile_input_authority
from .model_context import (
    DESIGN_PROVIDER_DOCUMENTS,
    build_readonly_context_envelope,
    build_design_provider_context_plan,
    parse_codex_jsonl_usage,
    validate_design_provider_context_plan,
    validate_model_usage_budget,
)
from .schema_capabilities import current_schema_version, schema_has


@dataclass(frozen=True)
class DesignDraft:
    spec_markdown: str
    execution_contract: dict[str, Any]
    providers: list[dict[str, Any]]
    blocking_unknowns: list[dict[str, Any] | str]


class DesignProvider(Protocol):
    def generate(self, repo_root: Path, project: str) -> DesignDraft: ...


class DesignCompilationError(ValueError):
    def __init__(self, message: str, staging_dir: Path):
        super().__init__(message)
        self.staging_dir = staging_dir


_REQUIRED_CONTRACT_KEYS = (
    "schema_version",
    "project",
    "requirements",
    "subsystems",
    "component_selections",
    "datasheets",
    "verification",
    "architecture",
    "workflow",
    "tier_c",
    "limitations",
    "integration",
    "release",
)


def _decode_codex_design_output(value: dict[str, Any]) -> DesignDraft:
    """Decode JSON-valued strings used to keep the Codex output schema strict.

    The Responses API requires every object in a structured-output schema to
    enumerate its properties and set ``additionalProperties`` to false.  The
    execution contract intentionally remains extensible and is validated by
    the repository's canonical schema, so it crosses the model boundary as a
    JSON string and is decoded before deterministic validation.
    """
    try:
        contract = json.loads(value["execution_contract_json"])
        providers = json.loads(value["providers_json"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Codex design provider returned invalid embedded JSON: {exc}") from exc
    if not isinstance(contract, dict):
        raise ValueError("Codex design provider execution_contract_json must decode to an object")
    if isinstance(providers, dict):
        providers = [providers]
    if not isinstance(providers, list):
        raise ValueError("Codex design provider providers_json must decode to an object or array")
    normalized_providers: list[dict[str, Any]] = []
    for item in providers:
        if isinstance(item, dict):
            normalized_providers.append(item)
        elif isinstance(item, str) and item.strip():
            normalized_providers.append({"name": item.strip()})
        else:
            raise ValueError("Codex design provider providers_json entries must be objects or non-empty strings")
    return DesignDraft(
        spec_markdown=value["spec_markdown"],
        execution_contract=contract,
        providers=normalized_providers,
        blocking_unknowns=list(value["blocking_unknowns"]),
    )


_REQUIRED_WORKFLOW_STAGES = [
    "preflight", "design", "approval", "bind", "subsystems", "integration",
    "tier_c", "closure", "release",
]
_LEGACY_WORKFLOW_STAGES = [
    "preflight", "design", "approval", "bind", "subsystems", "tier_c",
    "integration", "closure", "release",
]


def normalize_design_execution_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Normalize fixed Harness mechanics before deterministic validation.

    Artifact and protocol receipt transactions are materialized only by the
    Tier C flow.  They are not product choices, so retaining them on an A/B
    row can only create an impossible execution promise.  Workflow stages and
    Tier-C confirmation bindings are likewise Harness mechanics, not product
    decisions.  This operates on the post-grounding contract so it covers both
    an initial provider draft and a repair draft after preservation/merging.
    """
    contract = copy.deepcopy(contract)
    version = str(contract.get("schema_version") or "")
    workflow_stages = (
        _LEGACY_WORKFLOW_STAGES
        if version and not schema_has(version, "verification_batch")
        else _REQUIRED_WORKFLOW_STAGES
    )
    contract["workflow"] = {"stages": list(workflow_stages)}
    tier_c = {
        str(item.get("test_id")): item
        for item in contract.get("tier_c", [])
        if isinstance(item, dict) and item.get("test_id")
    }
    for item in tier_c.values():
        evidence = item.setdefault("evidence_contract", {})
        kinds = list(evidence.get("required_kinds") or [])
        if "user_confirmation" not in kinds:
            kinds.append("user_confirmation")
        evidence["required_kinds"] = kinds
        evidence.setdefault("observation", "human")
    for row in contract.get("verification", []):
        if not isinstance(row, dict):
            continue
        evidence = row.get("evidence_contract")
        if not isinstance(evidence, dict):
            continue
        kinds = evidence.get("required_kinds")
        if row.get("tier") in {"A", "B"} and isinstance(kinds, list):
            evidence["required_kinds"] = [
                kind for kind in kinds if kind not in {"artifact", "protocol_receipt"}
            ]
            row["evidence_required"] = [
                kind for kind in list(row.get("evidence_required") or [])
                if kind not in {"artifact", "protocol_receipt"}
            ]
            if not row["evidence_required"]:
                row["evidence_required"] = ["serial_log"]
            if not evidence["required_kinds"]:
                evidence["required_kinds"] = ["serial_log"]
            if evidence.get("observation") in {"artifact", "protocol"}:
                evidence["observation"] = "serial"
        elif row.get("tier") == "C":
            merged = list(kinds or [])
            if "user_confirmation" not in merged:
                merged.append("user_confirmation")
            evidence["required_kinds"] = merged
            evidence["observation"] = "human"
    return contract


def _normalize_automatic_evidence_kinds(draft: DesignDraft) -> DesignDraft:
    """Backward-compatible draft wrapper for provider-output tests."""
    return DesignDraft(
        spec_markdown=draft.spec_markdown,
        execution_contract=normalize_design_execution_contract(draft.execution_contract),
        providers=draft.providers,
        blocking_unknowns=draft.blocking_unknowns,
    )


def _repair_contract_slice(contract: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    """Return only contract rows named by deterministic repair diagnostics."""
    messages = "\n".join(errors)
    verification = [
        row for row in contract.get("verification", [])
        if isinstance(row, dict) and str(row.get("test_id") or "") in messages
    ]
    test_ids = {str(row.get("test_id")) for row in verification}
    owners = {str(row.get("owner")) for row in verification}
    requirement_ids = {str(row.get("requirement_id")) for row in verification}
    operations = [
        row for row in contract.get("operations", [])
        if isinstance(row, dict)
        and (
            str(row.get("operation_id") or "") in messages
            or str(row.get("owner") or "") in owners
            or str((row.get("runtime_probe") or {}).get("test_id") or "")
            in test_ids
        )
    ]
    return {
        "project": contract.get("project"),
        "requirements": [
            row for row in contract.get("requirements", [])
            if isinstance(row, dict) and str(row.get("id")) in requirement_ids
        ],
        "subsystems": [
            row for row in contract.get("subsystems", [])
            if isinstance(row, dict) and str(row.get("id")) in owners
        ],
        "verification": verification,
        "operations": operations,
        "architecture": contract.get("architecture"),
        "integration": contract.get("integration"),
        "release": contract.get("release"),
        "tier_c": [
            row for row in contract.get("tier_c", [])
            if isinstance(row, dict) and str(row.get("test_id") or "") in test_ids
        ],
    }


def _bind_contract_project(draft: DesignDraft, project: str) -> DesignDraft:
    """Bind an omitted project to CLI authority; reject a conflicting project."""
    returned = str(draft.execution_contract.get("project") or "").strip()
    if not returned or returned.casefold() == project.casefold():
        draft.execution_contract["project"] = project
        return draft
    raise ValueError(f"design provider returned a contract for the wrong project: {returned!r}")


def _preserve_blocking_unknowns(
    draft: DesignDraft, prior_unknowns: list[dict[str, Any] | str]
) -> DesignDraft:
    """Prevent a non-authoritative repair from erasing owner decisions."""
    # Only explicitly typed product-owner decisions survive provider repair.
    # Grounding and untyped technical unknowns belong to the producer and must
    # be resolvable by receipt reconciliation or a repaired design proposal.
    merged = [item for item in prior_unknowns if isinstance(item, dict) and item.get("code") == "USER_DECISION"]
    # Legacy revisions can still be repaired/read, but newly generated drafts
    # use typed objects and never route by prose.
    merged.extend(item for item in prior_unknowns if isinstance(item, str) and item.strip().casefold().startswith("[user_decision]"))
    for item in draft.blocking_unknowns:
        equivalent = next(
            (
                index
            for index, existing in enumerate(merged)
            if _equivalent_unknown(existing, item)
            ),
            None,
        )
        if equivalent is None:
            merged.append(item)
        elif isinstance(item, str) and isinstance(merged[equivalent], str) and len(item) > len(merged[equivalent]):
            merged[equivalent] = item
    return DesignDraft(
        spec_markdown=draft.spec_markdown,
        execution_contract=draft.execution_contract,
        providers=draft.providers,
        blocking_unknowns=merged,
    )


_UNKNOWN_STOPWORDS = {
    "a", "an", "and", "are", "be", "before", "complete", "define",
    "for", "how", "in", "is", "must", "of", "or", "provide", "required",
    "select", "supply", "the", "to", "what", "which", "with",
}


def _unknown_terms(value: str) -> tuple[str, set[str]]:
    normalized = value.casefold()
    category = (
        "grounding"
        if "[grounding_pending" in normalized
        else "user"
        if "[user_decision" in normalized
        else "untyped"
    )
    terms = {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_#/-]*", normalized)
        if token not in _UNKNOWN_STOPWORDS
        and token not in {"grounding_pending", "user_decision"}
    }
    return category, terms


def _equivalent_unknown(left: dict[str, Any] | str, right: dict[str, Any] | str) -> bool:
    """Conservatively collapse repair rephrasings of one open decision."""
    if isinstance(left, dict) and isinstance(right, dict):
        return (left.get("code"), left.get("owner"), left.get("category")) == (right.get("code"), right.get("owner"), right.get("category"))
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    left_category, left_terms = _unknown_terms(left)
    right_category, right_terms = _unknown_terms(right)
    if left_category != right_category or not left_terms or not right_terms:
        return left.strip() == right.strip()
    overlap = len(left_terms & right_terms) / min(
        len(left_terms), len(right_terms)
    )
    return overlap >= 0.6


def _preserve_grounding_facts(
    draft: DesignDraft, prior_contract: dict[str, Any]
) -> DesignDraft:
    """Carry Harness-owned Registry/Datasheet facts through model repair."""
    contract = copy.deepcopy(draft.execution_contract)
    subsystems = {
        str(item.get("id")): item
        for item in contract.get("subsystems", [])
        if isinstance(item, dict)
    }
    selections = contract.setdefault("component_selections", [])
    current_selections = {
        str(item.get("subsystem_id")): item
        for item in selections
        if isinstance(item, dict)
    }
    for prior in prior_contract.get("component_selections", []):
        if not isinstance(prior, dict):
            continue
        owner = str(prior.get("subsystem_id") or "")
        subsystem = subsystems.get(owner)
        if not subsystem or subsystem.get("classification") == "project_custom":
            continue
        current = current_selections.get(owner)
        if current is None:
            current = copy.deepcopy(prior)
            selections.append(current)
            current_selections[owner] = current
        else:
            for field in (
                "status",
                "provider_receipt_id",
                "registry_candidates",
                "registry_candidate_details",
                "registry_candidate_detail_errors",
            ):
                if field in prior:
                    current[field] = copy.deepcopy(prior[field])

    sheets = contract.setdefault("datasheets", [])
    current_sheets = {
        str(item.get("subsystem_id")): item
        for item in sheets
        if isinstance(item, dict)
    }
    for prior in prior_contract.get("datasheets", []):
        if not isinstance(prior, dict):
            continue
        owner = str(prior.get("subsystem_id") or "")
        subsystem = subsystems.get(owner)
        if not subsystem or subsystem.get("classification") != "external_part":
            continue
        current = current_sheets.get(owner)
        if current is None:
            sheets.append(copy.deepcopy(prior))
        else:
            for field in (
                "document_id",
                "revision",
                "source",
                "content_hash",
                "coverage",
                "level",
                "technical_content_valid",
                "identity_verified",
                "provider_receipt_id",
            ):
                if field in prior:
                    current[field] = copy.deepcopy(prior[field])

    # A repair provider may copy a receipt ID from its evidence context, but
    # the current grounding transaction is the only authority for the next
    # revision. Rebind each implementation fact to the current owner/source
    # receipt so stale IDs cannot survive a resumed or retried design.
    current_receipts: dict[tuple[str, str], str] = {}
    for item in current_selections.values():
        receipt_id = str(item.get("provider_receipt_id") or "")
        if receipt_id:
            current_receipts[(str(item.get("subsystem_id") or ""), "registry")] = receipt_id
            current_receipts[(str(item.get("subsystem_id") or ""), "local_idf")] = receipt_id
    for item in current_sheets.values():
        receipt_id = str(item.get("provider_receipt_id") or "")
        if receipt_id:
            current_receipts[(str(item.get("subsystem_id") or ""), "datasheet")] = receipt_id
    for fact in contract.get("implementation_facts", []):
        if not isinstance(fact, dict):
            continue
        key = (str(fact.get("subsystem_id") or ""), str(fact.get("source_kind") or ""))
        receipt_id = current_receipts.get(key)
        if receipt_id:
            fact["provider_receipt_id"] = receipt_id
    return DesignDraft(
        spec_markdown=draft.spec_markdown,
        execution_contract=contract,
        providers=draft.providers,
        blocking_unknowns=draft.blocking_unknowns,
    )


def _preserve_complete_contract_rows(
    draft: DesignDraft, prior_contract: dict[str, Any]
) -> DesignDraft:
    """Keep complete prior rows when a bounded repair returns a partial row.

    A repair is asked to correct a named defect, not to silently delete the
    acceptance/evidence contract that already passed validation.  Merge by
    stable row identity and recursively fill only missing fields, leaving an
    explicit repair value intact.  This is deliberately independent of prose
    similarity and prevents one bad provider response from erasing all tests.
    """
    value = copy.deepcopy(draft.execution_contract)

    def fill_missing(current: Any, prior: Any) -> Any:
        if isinstance(current, dict) and isinstance(prior, dict):
            merged = copy.deepcopy(current)
            for key, prior_value in prior.items():
                if key not in merged:
                    merged[key] = copy.deepcopy(prior_value)
                else:
                    merged[key] = fill_missing(merged[key], prior_value)
            return merged
        return current

    collections = (
        ("requirements", "id"),
        ("subsystems", "id"),
        ("tier_c", "id"),
        ("operations", "operation_id"),
    )
    for name, identity in collections:
        current = value.get(name)
        prior = prior_contract.get(name)
        if not isinstance(prior, list):
            continue
        # The structured-output schema represents an omitted collection as an
        # empty list.  In a bounded repair that is a partial-patch sentinel,
        # not authority to erase an already synthesized product contract.
        # An explicit row-level change remains possible because non-empty
        # collections still merge by their stable identity below.
        if not isinstance(current, list) or (not current and prior):
            value[name] = copy.deepcopy(prior)
            continue
        previous = {
            str(item.get(identity)): item
            for item in prior if isinstance(item, dict) and item.get(identity)
        }
        value[name] = [
            fill_missing(item, previous.get(str(item.get(identity)), {}))
            if isinstance(item, dict) else item
            for item in current
        ]
    # Verification rows have a stable product identity beyond a provider's
    # presentation-only test label.  A repair may reword `T-R1-HOLD-RECORD`
    # as `T-R1`; match the requirement/tier/owner tuple and retain its stable
    # product identity.  Acceptance and evidence fields are *not* frozen:
    # this helper runs before validation, so they may be precisely the fields
    # a bounded repair was asked to correct.
    prior_rows = prior_contract.get("verification")
    current_rows = value.get("verification")
    if isinstance(prior_rows, list):
        if not isinstance(current_rows, list) or (not current_rows and prior_rows):
            value["verification"] = copy.deepcopy(prior_rows)
        else:
            prior_by_test = {
                str(row.get("test_id")): row
                for row in prior_rows
                if isinstance(row, dict) and row.get("test_id")
            }
            prior_by_role = {
                (str(row.get("requirement_id")), str(row.get("tier")), str(row.get("owner"))): row
                for row in prior_rows if isinstance(row, dict)
            }
            prior_by_requirement_owner = {
                (str(row.get("requirement_id")), str(row.get("owner"))): row
                for row in prior_rows if isinstance(row, dict)
            }
            merged_rows = []
            for row in current_rows:
                if not isinstance(row, dict):
                    merged_rows.append(row)
                    continue
                prior = prior_by_test.get(str(row.get("test_id")))
                if prior is None:
                    prior = prior_by_role.get((str(row.get("requirement_id")), str(row.get("tier")), str(row.get("owner"))))
                if prior is None:
                    prior = prior_by_requirement_owner.get((str(row.get("requirement_id")), str(row.get("owner"))))
                merged = fill_missing(row, prior or {})
                if prior and prior.get("test_id"):
                    merged["test_id"] = prior["test_id"]
                    for frozen in ("requirement_id", "owner", "tier"):
                        if frozen in prior:
                            merged[frozen] = copy.deepcopy(prior[frozen])
                merged_rows.append(merged)
            value["verification"] = merged_rows
    # This function operates on an unapproved staging draft.  Batch and
    # isolation topology must therefore retain only missing fields above, not
    # overwrite an explicit repair.  Freezing it here would make a reported
    # topology validation error impossible to repair and cause a repeated
    # failure fingerprint.
    prior_tests = {
        str(item.get("id")): item for item in prior_contract.get("integration", {}).get("tests", [])
        if isinstance(item, dict) and item.get("id")
    }
    if isinstance(value.get("integration"), dict) and isinstance(value["integration"].get("tests"), list):
        value["integration"]["tests"] = [
            fill_missing(item, prior_tests.get(str(item.get("id")), {})) if isinstance(item, dict) else item
            for item in value["integration"]["tests"]
        ]
    for name in ("workflow", "architecture", "integration", "release"):
        if name not in value and name in prior_contract:
            value[name] = copy.deepcopy(prior_contract[name])
        elif isinstance(value.get(name), dict) and isinstance(prior_contract.get(name), dict):
            value[name] = fill_missing(value[name], prior_contract[name])
    return DesignDraft(
        spec_markdown=draft.spec_markdown,
        execution_contract=value,
        providers=draft.providers,
        blocking_unknowns=draft.blocking_unknowns,
    )


def _codex_design_command(work: Path, schema: Path, output: Path) -> list[str]:
    """Build a provider command without outer-session plugin/MCP initialization."""
    command = codex_command() + [
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        # The provider context is intentionally a disposable directory under
        # runtime/, rather than a checkout.  Codex must therefore be told
        # that this is an authorized non-Git read-only workspace.
        "--skip-git-repo-check",
    ]
    for feature in ("apps", "plugins", "remote_plugin", "workspace_dependencies"):
        command.extend(["--disable", feature])
    command.extend([
        "-c", "mcp_servers={}",
        "--sandbox", "read-only",
        "-C", str(work),
        "--output-schema", str(schema),
        "--output-last-message", str(output),
        "-",
    ])
    return command


def _write_design_authority_bundle(
    repo_root: Path, work: Path, project: str, raw_requirements: str,
) -> Path:
    """Materialize one redacted, hash-annotated authority file for synthesis."""
    sections: list[str] = [
        "# Design provider authority bundle\n",
        "This file is the complete bounded authority surface for this Design call.\n",
    ]
    source_paths = [
        repo_root / "requirements" / f"{project}.md",
        repo_root / "connections" / f"{project}.md",
        *(repo_root / item for item in DESIGN_PROVIDER_DOCUMENTS),
    ]
    for path in source_paths:
        if not path.is_file():
            continue
        content = (
            redact_text(raw_requirements)
            if path == repo_root / "requirements" / f"{project}.md"
            else path.read_text(encoding="utf-8")
        )
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        relative = path.relative_to(repo_root).as_posix()
        sections.extend([
            f"\n## SOURCE {relative}\n",
            f"sha256: {source_hash}\n\n",
            content.rstrip(),
            "\n",
        ])
    bundle = work / "design-authority.md"
    bundle.write_text("".join(sections), encoding="utf-8")
    return bundle


class CodexDesignProvider:
    """Generate a project-agnostic draft; deterministic validators remain authoritative."""

    def __init__(self, timeout: int = 600, repair_draft: DesignDraft | None = None, repair_errors: list[str] | None = None):
        self.timeout = timeout
        self.repair_draft = repair_draft
        self.repair_errors = repair_errors or []

    def repair(self, repo_root: Path, project: str, draft: DesignDraft, errors: list[str]) -> DesignDraft:
        return CodexDesignProvider(self.timeout, draft, errors).generate(repo_root, project)

    def generate(self, repo_root: Path, project: str) -> DesignDraft:
        schema = repo_root / "schemas" / "design-draft-output.schema.json"
        runtime = ProjectRuntime(repo_root, project).ensure().design_provider
        work = Path(tempfile.mkdtemp(prefix=f"{project}-context-", dir=runtime))
        (work / "requirements").mkdir(); (work / "connections").mkdir()
        raw_requirements = (repo_root / "requirements" / f"{project}.md").read_text(encoding="utf-8")
        secrets = secret_values(raw_requirements)
        (work / "requirements" / f"{project}.md").write_text(redact_text(raw_requirements), encoding="utf-8")
        shutil.copy2(repo_root / "connections" / f"{project}.md", work / "connections" / f"{project}.md")
        # Do not copy the repository's root AGENTS.md into the nested Codex
        # workspace.  It directs an interactive top-level agent to invoke the
        # esp-idf-firmware skill, but that skill (and its tool surface) is not
        # present in this deliberately minimal provider context.  Copying it
        # therefore makes the provider satisfy a workflow instruction instead
        # of producing the structured design draft requested below.  The
        # harness prompt and copied design documents are the provider's
        # complete, read-only authority.
        (work / "AGENTS.md").write_text(
            "# Isolated Design Provider Context\n\n"
            "Read the files named in the caller's prompt with read-only tools, "
            "then produce only the structured design draft requested by the caller. "
            "Do not invoke skills, edit files, or perform external side effects.\n",
            encoding="utf-8",
        )
        atomic_write_json(work / "input-authority.json", compile_input_authority(repo_root, project))
        authority_bundle = _write_design_authority_bundle(
            repo_root, work, project, raw_requirements
        )
        context_plan = build_design_provider_context_plan(repo_root, project)
        context_errors = validate_design_provider_context_plan(context_plan)
        if context_errors:
            raise RuntimeError(
                "Design provider context gate failed: " + "; ".join(context_errors)
            )
        # Existing projects need a real amendment baseline.  The newest
        # approved package is immutable/read-only context: a provider performs
        # impact analysis instead of regressing grounded facts merely because
        # the newly edited input does not repeat every earlier decision.
        approved = []
        for candidate in (repo_root / "projects" / project / "design-package").glob("rev-[0-9][0-9][0-9][0-9]"):
            approval = candidate / "approval.json"
            if approval.is_file() and json.loads(approval.read_text(encoding="utf-8")).get("status") == "APPROVED":
                approved.append(candidate)
        if approved:
            prior = work / "prior-approved-design"; prior.mkdir()
            for name in ("spec.md", "execution-contract.json", "manifest.json", "approval.json"):
                shutil.copy2(max(approved) / name, prior / name)
        with tempfile.NamedTemporaryFile(prefix=f"{project}-", suffix=".json", dir=runtime, delete=False) as handle:
            output = Path(handle.name)
        prompt = f"""You are the Design Subgraph provider for ESP-IDF project {project!r}.
Read design-authority.md, input-authority.json, and AGENTS.md. The authority bundle contains
the redacted user inputs and the selected Design/architecture/verification/schema sources with
their source hashes. Do not search for or read any other files. Do not edit files. Return only
the requested JSON object.

If prior-approved-design/ exists, it is an immutable approved baseline. Perform an impact analysis
against current inputs. Preserve still-applicable grounded facts, component selections, protocol
decisions, acceptance rows, and provider source details; alter only behavior affected by a current
input change. Do not turn resolved facts into UNKNOWN solely because they are not repeated in the
new request. Copy every still-applicable baseline datasheet record verbatim, including its
document_id, revision, source, content_hash, coverage, level, and its accepted risk state. Do not
create a new blocking_unknown solely to restate a baseline grounding limitation; introduce one
only when the current input changes that subsystem or exposes a new safety/implementation fact.
Keep genuinely new uncertainty explicit.


Compile a review-first spec and a project-specific execution contract. It must be derived only
from the named project's inputs, never from a sample project. Inventory every MCU-native, external-part, and reusable-software
subsystem; create a dependency DAG; assign every R/DR owner and verification row; predeclare
expected marker/value/range/count/duration/tolerance and evidence. Freeze FreeRTOS ownership.
Every subsystem must declare execution_role: component for an independently brought-up component,
or integration for the one final cross-component integration node. Never infer a role from a name.
For the current schema, architecture.runtime_flow is mandatory. It is the production (selftest-off)
execution graph, not a test description: declare one system_orchestration entrypoint symbol and
ordered steps. Each step names its owner, one owner required_operation, product symbol,
requirement_ids, optional preceding steps, and source_assertions. Every R/DR must appear in at
least one step. Every integration test must declare runtime_step_ids that cover all of its
requirement_ids. A component selftest or a simulation-only helper is never a runtime-flow step.
Every external_part subsystem must declare its exact manufacturer part_number. This field is
mandatory even when no Registry component or Datasheet source is known; the Harness derives its
grounding plan from this inventory and performs acquisition itself.
Before the spec can be approval-ready, exact external parts require Datasheet L0/L1 identity,
interface, electrical, board-safety, and acceptance facts. Every external_part and
reusable_software subsystem must declare the small list of operations its implementation needs in
required_operations (for example initialize/read/program/reset_recovery), without prescribing
registers or APIs. Implementation facts are optional during Design: keep any already-grounded
facts, but do not deep-read registers, bitfields, DMA settings, or complete driver procedures merely
to approve the architecture. After approval, the deterministic Execution readiness gate compares
required_operations with the finally selected component and acquires only missing facts before
coding or hardware access. Emit product_decisions separately for behavior, safety,
resource-budget, and external protocol choices. Only a genuine product choice may become a
user-owned unresolved item.
Emit the current typed operations table. Every operation_id binds one owner, kind, risk,
named capabilities, exact authority_sources with capability_ids, implementation assertions,
a runtime probe, and consumers. Never use owner-wide component coverage, prose inference, or
a policy default for hardware/register/transport/destructive/safety operations. Policy defaults
are allowed only for versioned host algorithms, product policy, or system orchestration.
Every verification row must also provide evidence_contract with observation and required_kinds.
Use build_receipt/flash_receipt/serial_log/firmware_hash/hardware_identity/artifact/
protocol_receipt/user_confirmation as applicable; only Tier C may require user_confirmation.
For Tier A/B rows, required_kinds is restricted to build_receipt, flash_receipt,
serial_log, firmware_hash, and hardware_identity. Do not require artifact or
protocol_receipt for Tier A/B: those runtime transactions are unavailable outside
the declared Tier C artifact flow. A WAV selftest is therefore a serial/readback
observation, not an artifact-evidence request.
Set schema_version to "{current_schema_version()}". Every Tier A/B verification row must declare an executable
test_setup and stimulus. Use normal_boot + none for normal boot evidence. If markers require a
firmware selftest, declare firmware_selftest with isolated_build=true, explicit
kconfig_overrides, and firmware_simulation; the runner builds, flashes, and captures that image
separately from release. Real physical input may use automated_fixture only when the fixture and
its stimulus are declared; otherwise it is Tier C. The input-authority record is the only authority for exact external
part numbers, pins, and endpoint identity: copy an identifier exactly or emit a typed INPUT_AMBIGUITY
unknown; never add package suffixes or infer a variant. Every component subsystem must declare responsibility_layer,
verification_batch, batch_compatible, isolation_required, isolation_reason, and hardware_resources.
Use stable responsibility boundaries: board_resource for shared board buses/pins/power/clock,
device_driver for a precise external part, data_service for persistence/encoding/connectivity,
product_policy for user-visible state behavior, system_orchestration for lifecycle composition, and
integration only for the final cross-component node. Do not make one ESP-IDF facility a public
component merely because it exists; GPIO/I2C/SPI/I2S/NVS/Wi-Fi/HTTP/SNTP are internal capabilities
unless they own a stable shared boundary. Propose batches during design:
Represent peripheral buses through protocol-neutral resource_requirements and resource_capabilities.
Adapters normalize datasheet limits and local ESP-IDF capabilities into constraints/candidates; the
Harness selects one compatible unclaimed candidate and records resource_allocations with provenance.
Do not create per-protocol decision flows. Ask for a user decision only when no candidate satisfies
the evidence-bound constraints or multiple policy-distinct choices remain.
external-chip/register/DMA/audio/storage/destructive or ambiguous tests must be isolated; verified
non-destructive compatible owners must set batch_compatible=true and share one contiguous
dependency-ordered batch. Runtime never changes this decision. Every Tier C item must independently declare test_id, owner, expected,
and evidence_contract; it must not borrow those fields from an A/B verification row.
Every non-physical Tier C item must additionally declare its integration/production producer
test, producer operation IDs, delivery method, correlation key, required producer Receipt
kinds, and deterministic artifact validation. A local path without an executable producer is
invalid and must never become a request for the user to create a missing file.
Each verification_batch has exactly one test_setup value. If any row for an owner
in a batch needs a different setup (for example normal_boot instead of
firmware_selftest, or different selftest Kconfig overrides), assign that owner a
different batch; never mix setup objects within one batch.
For a product with normal runtime orchestration, set release.early_smoke=true so the Harness
performs one isolated selftest-off build/flash/boot immediately after component bring-up and before
expensive integration. The final release transaction is still mandatory and fresh.
Declare architecture.component_api_manifest with semantic APIs, resources, exact dependencies,
allowed dependency layers, and test_only status. Extend runtime_flow with production config,
typed edges/ports/transitions, operation_ids, and observation points. Integration must declare
production_scenarios that enter through the same normal entrypoint. Release must select at least
one core production scenario and declare forbidden runtime patterns.
Its artifact_contract must be executable: local_file source is a project-relative file path
(optionally templated only with {{run_id}}/{{item_id}}), download_url source is an absolute
HTTP(S) URL, and physical_observation is used only when no file or URL can exist. Never put prose,
an anticipated artifact description, or a serial-only object in artifact_contract.source.
Classify every subsystem before proposing a library. Only external_part (an external chip with a
driver candidate) and reusable_software (an explicitly considered third-party dependency) set
registry_search_required=true and contain Registry exact/capability queries plus an adopt/reject
proposal. MCU-native ESP-IDF facilities (GPIO, I2C, SPI, I2S, Wi-Fi, NVS, SNTP, HTTP client,
FreeRTOS) are mcu_native with registry_search_required=false and use local IDF grounding. Product
code owned by this project (recording policy, storage queue, upload policy, orchestration, and
integration) is project_custom with registry_search_required=false; do not create a Registry
selection for it. Use decision registry/local_idf/custom/reject and, for registry,
selected_component as an exact namespace/name only as a non-authoritative proposal. The Harness
executes MCP, fetches every candidate detail, and replaces the proposal with the final evidence-based
decision; do not invent results or provider_receipt_id values.
Simple board discretes such as a push button or indicator LED that are used only through an
MCU-native GPIO are not external_part driver candidates. Represent the GPIO facility as
mcu_native and debounce/product behavior as project_custom. Do not require a mechanical MPN,
Registry search, or Datasheet unless a user requirement actually depends on that component's
electrical, timing, safety, or mechanical rating.
The provider cannot reject candidates it has not seen. After Registry grounding the Harness makes
the final selection and records concrete compatibility/coverage reasons for every unselected
candidate. Never use the draft choice itself as a rejection reason.
Datasheet facts require exact part/variant/document/revision/coverage. Unknown facts remain
blocking_unknowns or UNKNOWN/DEFERRED. Return blocking_unknowns as typed objects with code, owner,
category, phase, authority, severity, required_operations, and resolution. Only USER_DECISION or
INPUT_AMBIGUITY may have null resolution at design time. Do not encode routing state in prose.
Prefix acquisition-only statements that the Harness can
resolve from its Registry/Datasheet work with [GROUNDING_PENDING]. Prefix choices that require
product-owner input with [USER_DECISION]. Never combine those two categories in one statement.
An unprefixed technical unknown is a provider error. For sample rate, width, bounded duration,
queue sizing, loss thresholds, and similar engineering choices, propose explicit conservative
ASSUMPTION/POLICY values with acceptance bounds for the reviewer; do not create an extra human
gate merely because the user did not preselect an engineering default.
Custom drivers do not automatically require a complete L2 study. Execution requests only the
facts missing for required operations; destructive and safety-critical constants must be backed by
an authoritative source. L3 remains failure-directed.
Do not expose credential values; use references. Include mandatory workflow stages in order:
preflight, design, approval, bind, subsystems, integration, tier_c, closure, release.
The contract must satisfy the repository JSON schema and deterministic validator.
Before returning, self-check these non-negotiable rules: every requirement including DR rows has
one verification row; every Tier C verification has one tier_c object with id, requirement_id and
instructions; every integration test has requirement_ids and an executable expected rule; every
integration-owned verification row that could match more than one integration test declares
integration_test_id; workflow
must be exactly this JSON shape and order: {{"stages":["preflight","design","approval","bind",
"subsystems","integration","tier_c","closure","release"]}}. Do not omit or reorder
preflight even when no special preflight behavior is requested. Registry exact/capability queries
must be real searchable phrases,
never placeholders such as PENDING, TODO, UNKNOWN, or DESIGN_HARNESS. Datasheet records must name
the exact part and may use a source URL as a lead, but must never use a placeholder hash or source.
Return the complete execution contract as serialized JSON text in execution_contract_json.
Return the provider metadata array as serialized JSON text in providers_json. These two fields
must be valid JSON strings, not nested objects, because the Harness decodes and validates them.
"""
        if self.repair_draft is not None:
            repair_context = work / "repair-context"; repair_context.mkdir()
            (repair_context / "spec.md").write_text(self.repair_draft.spec_markdown, encoding="utf-8")
            repair_slice = _repair_contract_slice(
                self.repair_draft.execution_contract, self.repair_errors
            )
            atomic_write_json(repair_context / "affected-contract.json", repair_slice)
            grounding_context = _stage_grounding_context(
                repo_root, project, work, repair_slice
            )
            prompt += f"""

This is an unnumbered staging repair. Return a minimal execution-contract patch containing only
the rows in repair-context/affected-contract.json that must change to fix every deterministic
error below. Do not regenerate unrelated requirements, subsystems, verification rows, topology,
or workflow. The Harness preserves every omitted row from the immutable prior staging contract.
Read repair-context/spec.md, repair-context/affected-contract.json, and only the receipt-bound
files named by grounding-context/manifest.json when a repaired row needs those facts. These files
are authoritative evidence for the current repair. Datasheet receipt raw output contains
`extracted_text`: use its concrete register, command, timing, geometry, electrical, and format
statements (and the receipt-bound ESP-IDF headers where applicable) to replace every
GROUNDING_PENDING implementation_facts.value with a structured, non-placeholder value. If that
evidence genuinely lacks a required value, preserve the unknown and let deterministic validation
block; never invent a value.

ERRORS (the exact fields to repair):
{json.dumps(self.repair_errors, ensure_ascii=False)}
"""
        def invoke(instruction: str) -> DesignDraft:
            # Windows imposes a hard command-line length limit.  A repair
            # prompt can legitimately contain a complete JSON contract, so
            # pass all provider instructions over stdin (Codex's documented
            # ``-`` prompt form) rather than truncating design scope.  Use a
            # regular disposable file as stdin: on Windows,
            # ``communicate(input=..., timeout=...)`` can block synchronously
            # while filling an anonymous pipe before its timeout wait begins.
            # A file-backed stdin keeps the complete prompt while making the
            # provider timeout authoritative.
            # The provider sees only a disposable, redacted context and the
            # outer Harness owns every persisted artifact.  The provider never
            # needs to write source or runtime state, so enforce a read-only
            # sandbox even though its disposable context is already redacted.
            # This prevents a provider mistake from deleting user project
            # state outside that context.
            command = _codex_design_command(work, schema, output)
            authority_files = [
                authority_bundle,
                work / "input-authority.json",
                work / "AGENTS.md",
            ]
            if self.repair_draft is not None:
                authority_files.extend(
                    path for path in (work / "repair-context").rglob("*")
                    if path.is_file()
                )
            envelope = build_readonly_context_envelope(
                project=project,
                reason=(
                    "design_structural_repair"
                    if self.repair_draft is not None
                    else "design_synthesis"
                ),
                instruction=instruction,
                workspace=work,
                authority_files=authority_files,
                secret_values=secrets,
                # Design synthesis is bounded by one transaction, a single
                # authority bundle, tool-call limits, and provider timeout.
                # Cumulative cached input accounting is intentionally not a
                # terminal gate.  Output and reasoning usage are likewise
                # metrics rather than correctness gates: a schema-valid
                # Design must not be discarded only because model accounting
                # crossed an arbitrary token threshold.
                max_input_tokens=None,
                max_output_tokens=None,
                max_reasoning_tokens=None,
            )
            context_path = work / "model-context.json"
            atomic_write_json(context_path, envelope)
            prompt_path = work / "provider-prompt.txt"
            prompt_path.write_text(
                "Read model-context.json and every authority file it names. "
                "Perform exactly that read-only Design task and return only "
                "the schema-conforming result.\n",
                encoding="utf-8",
            )
            for auth_attempt in range(2):
                with prompt_path.open("r", encoding="utf-8") as prompt_handle:
                    process = subprocess.Popen(
                        command,
                        cwd=work,
                        env=child_env,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        stdin=prompt_handle,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        creationflags=codex_creationflags(),
                    )
                    try:
                        provider_output, _ = process.communicate(
                            timeout=self.timeout
                        )
                    except subprocess.TimeoutExpired as exc:
                        partial = exc.output or ""
                        if isinstance(partial, bytes):
                            partial = partial.decode("utf-8", errors="replace")
                        terminate_process_tree(process)
                        raise RuntimeError(
                            f"Codex design provider timed out after {self.timeout} seconds: {partial[-2000:]}"
                        ) from exc
                if process.returncode == 0:
                    decoded = _decode_codex_design_output(
                        json.loads(output.read_text(encoding="utf-8"))
                    )
                    usage = parse_codex_jsonl_usage(provider_output)
                    budget_errors = validate_model_usage_budget(
                        usage, envelope["budgets"]
                    )
                    if budget_errors:
                        raise RuntimeError(
                            "Design provider model budget exceeded: "
                            + "; ".join(budget_errors)
                        )
                    return DesignDraft(
                        spec_markdown=decoded.spec_markdown,
                        execution_contract=decoded.execution_contract,
                        providers=decoded.providers + [{
                            "kind": "design_model_transaction",
                            "model_context_digest": envelope["context_digest"],
                            "model_context_bytes": context_path.stat().st_size,
                            "model_usage": usage,
                            "budgets": envelope["budgets"],
                        }],
                        blocking_unknowns=decoded.blocking_unknowns,
                    )
                if auth_attempt == 0 and is_authentication_failure(provider_output):
                    # Cockpit updates ~/.codex/auth.json for the next account.
                    # Give that atomic switch a moment, then launch a fresh
                    # process which reloads the active credentials.
                    time.sleep(2)
                    profile.refresh_auth()
                    continue
                raise RuntimeError(f"Codex design provider exited {process.returncode}: {provider_output[-2000:]}")
        codex_temp = runtime / "codex-tmp"
        try:
            with isolated_codex_profile(codex_temp) as profile:
                child_env = profile.environment.copy(); child_env.pop("PYTHONPATH", None)
                draft = invoke(prompt)
                draft = _normalize_automatic_evidence_kinds(draft)
                # The CLI project argument and isolated input paths are the
                # authority for an omitted structured-output field. A
                # conflicting non-empty project remains a hard rejection.
                draft = _bind_contract_project(draft, project)
                if self.repair_draft is not None:
                    draft = _preserve_grounding_facts(
                        draft, self.repair_draft.execution_contract
                    )
                    draft = _preserve_complete_contract_rows(
                        draft, self.repair_draft.execution_contract
                    )
                protected_unknowns = list(
                    self.repair_draft.blocking_unknowns
                    if self.repair_draft is not None
                    else []
                )
                draft = _preserve_blocking_unknowns(
                    draft, protected_unknowns
                )
                protected_unknowns = list(draft.blocking_unknowns)
                structural_errors: list[str] = []
                for repair_attempt in range(2):
                    try:
                        _require_object_entries(draft.execution_contract)
                        planned_contract, _, _, planning_errors = (
                            materialize_grounding_requirements(
                                draft.execution_contract
                            )
                        )
                        draft = DesignDraft(
                            spec_markdown=draft.spec_markdown,
                            execution_contract=planned_contract,
                            providers=draft.providers,
                            blocking_unknowns=draft.blocking_unknowns,
                        )
                        structural_errors = [
                            item
                            for item in (
                                validate_contract(draft.execution_contract)
                                + planning_errors
                            )
                            if not any(token in item for token in (
                                "Registry search is an outage",
                                "lacks validated Datasheet",
                                "custom driver requires Datasheet",
                                "grounding receipt",
                                "provider receipt",
                                "remains unresolved [GROUNDING_PENDING]",
                                # Receipt-bound facts cannot be completed
                                # until the graph has run Datasheet/IDF
                                # grounding.  Do not exhaust this provider's
                                # pre-grounding format-repair budget merely
                                # because it omitted a fact or left its value
                                # pending; the graph repair receives the
                                # receipt-owned extraction and is the sole
                                # stage allowed to resolve it.
                                "implementation fact ",
                                "lacks receipt-bound implementation facts",
                            ))
                        ]
                    except ValueError as exc:
                        structural_errors = [str(exc)]
                    if not structural_errors:
                        break
                    if repair_attempt == 1:
                        break
                    atomic_write_json(work / "structural-repair-contract.json", draft.execution_contract)
                    repair = f"""The first Design draft for project {project!r} failed deterministic
structural validation. Read structural-repair-contract.json, requirements/{project}.md,
connections/{project}.md, and the named design/schema documents. Return one complete replacement
using the required output schema; do not return a JSON patch.

Do not change product scope, requirements, pins, facts, or introduce guessed hardware values.
Fix every error below while retaining all mandatory contract sections and genuine unknowns in
blocking_unknowns.

ERRORS:
{json.dumps(structural_errors, ensure_ascii=False)}
"""
                    protected_contract = draft.execution_contract
                    draft = invoke(repair)
                    draft = _bind_contract_project(draft, project)
                    draft = _preserve_grounding_facts(
                        draft, protected_contract
                    )
                    draft = _preserve_complete_contract_rows(
                        draft, protected_contract
                    )
                    draft = _preserve_blocking_unknowns(
                        draft, protected_unknowns
                    )
                    protected_unknowns = list(draft.blocking_unknowns)
                # This provider-side pass is only an early formatting aid.
                # If it exhausts its bounded attempts, return the draft to
                # the Design graph instead of turning a repairable contract
                # defect into a worker-level internal fault.  The graph owns
                # the durable diagnostics, retry accounting, and its
                # grounding-aware repair context.
                assert_no_secret_values(draft.spec_markdown + json.dumps(draft.execution_contract, ensure_ascii=False), secrets, "generated design")
                return draft
        finally:
            output.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)


def _input_refs(repo_root: Path, project: str) -> list[dict[str, Any]]:
    from .input_authority import semantic_input_ref

    refs = []
    for area in ("requirements", "connections"):
        path = repo_root / area / f"{project}.md"
        if not path.is_file():
            raise FileNotFoundError(f"required user input is missing: {path}")
        refs.append(semantic_input_ref(path, repo_root, area))
    return refs


def next_revision(project_dir: Path) -> int:
    revisions = [int(path.name.removeprefix("rev-")) for path in (project_dir / "design-package").glob("rev-[0-9][0-9][0-9][0-9]") if path.is_dir()]
    return max(revisions, default=0) + 1


def _query(value: Any) -> str:
    if isinstance(value, str):
        for prefix in ("ESP Component Registry exact query: ", "ESP Component Registry query: "):
            if value.startswith(prefix): return value.removeprefix(prefix)
        return value
    if isinstance(value, dict): return str(value.get("query") or value.get("exact_query") or value.get("capability_query") or "")
    return ""


def _stage_grounding_context(repo_root: Path, project: str, work: Path, contract: dict[str, Any]) -> Path:
    """Expose receipt-bound implementation sources to the bounded repair provider.

    The initial provider is intentionally isolated from runtime facts.  Once
    deterministic grounding has completed, however, a structural repair must
    be able to inspect the exact datasheet and local SDK sources that justify
    implementation facts.  Previously the repair prompt saw only a pending
    placeholder and could not replace it with a receipt-bound fact.
    """
    context = work / "grounding-context"
    context.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    project_dir = repo_root / "projects" / project
    for sheet in contract.get("datasheets", []):
        if not isinstance(sheet, dict):
            continue
        owner = str(sheet.get("subsystem_id") or "owner")
        source = repo_root / str(sheet.get("source") or "")
        if source.is_file():
            target = context / f"{owner}.pdf"
            shutil.copy2(source, target)
            manifest.append({"kind": "datasheet", "subsystem_id": owner, "path": target.name, "provider_receipt_id": sheet.get("provider_receipt_id")})
        receipt_id = str(sheet.get("provider_receipt_id") or "")
        receipt = project_dir / "execution" / "receipts" / "design" / f"{receipt_id}.json"
        if receipt_id and receipt.is_file():
            target = context / f"{owner}-datasheet-receipt.json"
            shutil.copy2(receipt, target)
            manifest.append({"kind": "datasheet_receipt", "subsystem_id": owner, "path": target.name, "provider_receipt_id": receipt_id})

    for selection in contract.get("component_selections", []):
        if not isinstance(selection, dict):
            continue
        owner = str(selection.get("subsystem_id") or "owner")
        receipt_id = str(selection.get("provider_receipt_id") or "")
        receipt = project_dir / "execution" / "receipts" / "design" / f"{receipt_id}.json"
        if receipt_id and receipt.is_file():
            target = context / f"{owner}-selection-receipt.json"
            shutil.copy2(receipt, target)
            manifest.append({"kind": "selection_receipt", "subsystem_id": owner, "path": target.name, "provider_receipt_id": receipt_id})

    # The design worker is launched from a fresh shell, so IDF_PATH may not be
    # present.  Resolve it only through the repository's explicit activation
    # script; never guess an ESP-IDF installation path.
    idf_path = os.environ.get("IDF_PATH", "")
    activation = repo_root / "activate.local.ps1"
    if not idf_path and activation.is_file():
        result = subprocess.run(
            hidden_powershell_command("-ExecutionPolicy", "Bypass", "-Command", f". '{activation}'; Write-Output $env:IDF_PATH"),
            cwd=repo_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        candidates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        for candidate in reversed(candidates):
            if Path(candidate).is_dir():
                idf_path = candidate
                break
    idf_files = (
        "components/esp_driver_i2s/include/driver/i2s_pdm.h",
        "components/esp_driver_i2s/include/driver/i2s_common.h",
        "components/esp_driver_spi/include/driver/spi_master.h",
        "components/esp_driver_i2c/include/driver/i2c_master.h",
    )
    if idf_path:
        for relative in idf_files:
            source = Path(idf_path) / relative
            if source.is_file():
                target = context / ("idf-" + relative.replace("/", "-").replace("\\", "-"))
                shutil.copy2(source, target)
                manifest.append({"kind": "local_idf", "path": target.name, "source": relative})
    atomic_write_json(context / "manifest.json", {"project": project, "sources": manifest})
    return context


def _ground_contract(contract: dict[str, Any], grounding: DesignGroundingAdapter, repo_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Replace model assertions with actual Registry/datasheet provider Receipts."""
    from .design_diagnostics import design_diagnostic
    from .models import FailureCategory, FailureDisposition

    value = copy.deepcopy(contract)
    authority = value.pop("_input_authority", {})
    providers: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def internal(
        code: str,
        summary: str,
        *,
        owner: str | None = None,
        cause: FailureCategory = FailureCategory.DATA_PATH,
        party: str = "design_grounding",
        scope: str = "grounding_item",
    ) -> None:
        errors.append(
            design_diagnostic(
                code=code,
                summary=summary,
                cause=cause,
                disposition=FailureDisposition.REPAIR_INTERNAL,
                responsible_party=party,
                affected_owner=owner,
                retry_scope=scope,
            )
        )
    from .component_selection import (
        finalize_component_selection,
        required_operations_for_subsystem,
    )
    subsystem_definitions = {
        str(item.get("id")): item
        for item in value.get("subsystems", [])
        if isinstance(item, dict)
    }
    for subsystem in subsystem_definitions.values():
        if schema_has(str(value.get("schema_version")), "input_authority") and subsystem.get("classification") == "external_part":
            part = str(subsystem.get("part_number") or "")
            if authority and not exact_authorized_identifier(authority, part):
                internal("INPUT_AUTHORITY_VIOLATION", f"{subsystem.get('id')} part_number {part!r} is not an exact user input identifier", owner=str(subsystem.get("id") or ""), party="design_provider")
    registry_required = {
        owner for owner, item in subsystem_definitions.items()
        if item.get("registry_search_required")
    }
    for selection in value.get("component_selections", []):
        if not isinstance(selection, dict):
            internal(
                "COMPONENT_SELECTION_INVALID",
                "component_selections entries must be objects",
                party="design_provider",
            )
            continue
        subsystem_id = str(selection.get("subsystem_id", "")); exact = _query(selection.get("exact_search")); capability = _query(selection.get("capability_search"))
        if subsystem_id not in registry_required:
            selection["status"] = "local_idf"
            receipt = grounding.local_idf_selection(subsystem_id, exact, capability)
            selection["provider_receipt_id"] = receipt.receipt_id
            receipt_path = grounding.store.receipts / "design" / f"{receipt.receipt_id}.json"
            providers.append({"kind": "local_idf", "subsystem_id": subsystem_id, "receipt_id": receipt.receipt_id, "receipt_ref": file_ref(receipt_path, repo_root, "application/json").model_dump()})
            continue
        if not exact or not capability:
            internal(
                "REGISTRY_QUERY_MISSING",
                f"{subsystem_id} Registry queries must be concrete non-empty strings",
                owner=subsystem_id,
                party="design_provider",
            )
        # Registry grounding precedes final selection. A provider draft choice
        # is proposal context only and must not limit which candidate details
        # the Harness fetches.
        receipt = grounding.registry_search(subsystem_id, exact, capability, None)
        selection["provider_receipt_id"] = receipt.receipt_id
        selection["status"] = "ok" if receipt.success else "outage"
        selection["registry_candidates"] = list(receipt.outputs.get("candidate_ids", []))
        selection["registry_candidate_details"] = list(
            receipt.outputs.get("candidate_details", [])
        )
        selection["registry_candidate_detail_errors"] = list(
            receipt.outputs.get("candidate_detail_errors", [])
        )
        receipt_path = grounding.store.receipts / "design" / f"{receipt.receipt_id}.json"
        providers.append({"kind": "registry", "subsystem_id": subsystem_id, "receipt_id": receipt.receipt_id, "receipt_ref": file_ref(receipt_path, repo_root, "application/json").model_dump()})
        if not receipt.success:
            internal(
                "REGISTRY_SEARCH_FAILED",
                receipt.failure.summary if receipt.failure else f"Registry grounding failed for {subsystem_id}",
                owner=subsystem_id,
                cause=FailureCategory.REGISTRY,
            )
        for detail_error in selection["registry_candidate_detail_errors"]:
            internal(
                "REGISTRY_CANDIDATE_DETAIL_FAILED",
                f"{subsystem_id} Registry candidate detail lookup failed for "
                f"{detail_error.get('component')}: {detail_error.get('summary')}",
                owner=subsystem_id,
                cause=FailureCategory.REGISTRY,
            )
        if receipt.success and not selection["registry_candidate_detail_errors"]:
            subsystem = subsystem_definitions.get(subsystem_id, {})
            required_operations = required_operations_for_subsystem(
                value, subsystem
            )
            subsystem["required_operations"] = required_operations
            finalized = finalize_component_selection(
                selection,
                part_number=str(subsystem.get("part_number") or exact),
                idf_version=str(
                    value.get("platform", {}).get("idf_version") or "5.0+"
                ),
                target=str(
                    value.get("platform", {}).get("target") or "esp32"
                ),
                required_operations=required_operations,
            )
            selection.clear()
            selection.update(finalized)
    for sheet in value.get("datasheets", []):
        if not isinstance(sheet, dict):
            internal(
                "DATASHEET_ENTRY_INVALID",
                "datasheets entries must be objects",
                party="design_provider",
            )
            continue
        subsystem_id = str(sheet.get("subsystem_id", "")); receipt = grounding.datasheet_inspect(subsystem_id, sheet)
        sheet["provider_receipt_id"] = receipt.receipt_id
        if receipt.success:
            from .datasheet_library import resolve_datasheet_alias
            grounded_source = str(
                receipt.outputs.get("source")
                or receipt.outputs.get("resolved_source")
                or sheet.get("source", "")
            )
            sheet["source"] = resolve_datasheet_alias(
                repo_root, grounded_source, grounding.store.project_dir.name
            )
            sheet["content_hash"] = receipt.outputs["sha256"]
            sheet["technical_content_valid"] = receipt.outputs["technical_content_valid"]
            sheet["document_id"] = receipt.outputs.get("document_id") or sheet.get("document_id")
            sheet["revision"] = receipt.outputs.get("revision") or sheet.get("revision")
            sheet["coverage"] = receipt.outputs.get("coverage") or sheet.get("coverage", [])
            sheet["identity_verified"] = receipt.outputs.get("identity_verified", False)
            extracted_facts = receipt.outputs.get("implementation_facts", [])
            if extracted_facts:
                facts = [
                    item for item in value.get("implementation_facts", [])
                    if not (
                        isinstance(item, dict)
                        and item.get("subsystem_id") == subsystem_id
                        and item.get("source_kind") == "datasheet"
                    )
                ]
                for fact in extracted_facts:
                    if isinstance(fact, dict):
                        facts.append({**fact, "provider_receipt_id": receipt.receipt_id})
                value["implementation_facts"] = facts
            for diagnostic in receipt.outputs.get("grounding_diagnostics", []):
                errors.append(dict(diagnostic))
        else: sheet["technical_content_valid"] = False
        receipt_path = grounding.store.receipts / "design" / f"{receipt.receipt_id}.json"
        providers.append({"kind": "datasheet", "subsystem_id": subsystem_id, "receipt_id": receipt.receipt_id, "receipt_ref": file_ref(receipt_path, repo_root, "application/json").model_dump()})
        if not receipt.success:
            internal(
                "DATASHEET_GROUNDING_FAILED",
                receipt.failure.summary if receipt.failure else f"Datasheet grounding failed for {subsystem_id}",
                owner=subsystem_id,
                cause=FailureCategory.DATASHEET,
            )

    # Implementation facts are drafted before the Harness has executed the
    # grounding transactions, so their provider_receipt_id may still be a
    # placeholder.  Rebind facts to the receipts from this exact transaction;
    # otherwise successful datasheet/Registry grounding is incorrectly
    # rejected as an unbound fact and the design can never reach approval.
    receipts_by_owner_and_kind: dict[tuple[str, str], str] = {}
    for selection in value.get("component_selections", []):
        if not isinstance(selection, dict):
            continue
        receipt_id = str(selection.get("provider_receipt_id") or "")
        owner = str(selection.get("subsystem_id") or "")
        if receipt_id and owner:
            receipts_by_owner_and_kind[(owner, "registry")] = receipt_id
            receipts_by_owner_and_kind[(owner, "local_idf")] = receipt_id
    for sheet in value.get("datasheets", []):
        if not isinstance(sheet, dict):
            continue
        receipt_id = str(sheet.get("provider_receipt_id") or "")
        owner = str(sheet.get("subsystem_id") or "")
        if receipt_id and owner:
            receipts_by_owner_and_kind[(owner, "datasheet")] = receipt_id
    for fact in value.get("implementation_facts", []):
        if not isinstance(fact, dict):
            continue
        key = (
            str(fact.get("subsystem_id") or ""),
            str(fact.get("source_kind") or ""),
        )
        receipt_id = receipts_by_owner_and_kind.get(key)
        if receipt_id:
            fact["provider_receipt_id"] = receipt_id
    return value, providers, errors


def _require_object_entries(contract: dict[str, Any]) -> None:
    """Fail with a precise model-contract path before any `.get()` use."""
    missing = [key for key in _REQUIRED_CONTRACT_KEYS if key not in contract]
    if missing:
        raise ValueError(f"design contract is missing required keys: {missing}")
    for key in ("requirements", "subsystems", "component_selections", "datasheets", "verification", "tier_c", "limitations"):
        value = contract.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"design contract {key} must be an array")
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValueError(f"design contract {key}[{index}] must be an object, got {type(item).__name__}")
    integration = contract.get("integration", {})
    if not isinstance(integration, dict):
        raise ValueError("design contract integration must be an object")
    for index, item in enumerate(integration.get("tests", [])):
        if not isinstance(item, dict):
            raise ValueError(f"design contract integration.tests[{index}] must be an object, got {type(item).__name__}")


def compile_initial_design(
    repo_root: Path,
    project: str,
    provider: DesignProvider | None = None,
    revision: int | None = None,
    grounding: DesignGroundingAdapter | None = None,
    max_attempts: int = 2,
) -> Path:
    """Run the same Design LangGraph used by the CLI.

    Direct callers retain injectable provider/grounding seams for tests and
    offline tooling, but no longer bypass the normalized node topology.
    Only a clean graph result allocates a product revision.
    """
    if max_attempts not in {1, 2}:
        raise ValueError("Design Graph supports one bounded structural repair")
    from .design_graph import build_design_graph

    selected_provider = provider or CodexDesignProvider()

    def provider_factory() -> DesignProvider:
        return selected_provider

    def grounding_factory(_store: ProjectStore, _run_id: str) -> DesignGroundingAdapter:
        if grounding is not None:
            return grounding
        return DesignGroundingAdapter(
            repo_root.resolve(),
            _store,
            _run_id,
        )

    result = build_design_graph(
        repo_root,
        provider_factory=provider_factory,
        grounding_factory=grounding_factory,
    ).invoke(
        {
            "project": project,
            "job_id": f"direct-{uuid.uuid4().hex[:16]}",
            "revision": revision,
            "max_attempts": max_attempts,
        },
        {"recursion_limit": 32},
    )
    if result.get("mode") == "WAITING_SPEC":
        return Path(result["design_dir"])
    staging = Path(
        result.get("staging_root")
        or repo_root / "projects" / project / "design-package" / ".staging"
    )
    raise DesignCompilationError(
        str(result.get("summary") or "design did not reach a clean review package"),
        staging,
    )


def approve_revision(design_dir: Path, approved_by: str = "user") -> dict[str, Any]:
    contract, errors = validate_design_package(design_dir, require_approval=False)
    validation = json.loads((design_dir / "design-validation.json").read_text(encoding="utf-8"))
    if errors or validation.get("errors"):
        raise ValueError("cannot approve invalid or unresolved design: " + "; ".join(errors + validation.get("errors", [])))
    manifest = json.loads((design_dir / "manifest.json").read_text(encoding="utf-8"))
    approval = {"schema_version": "1.0", "project_id": contract["project"], "status": "APPROVED", "spec_revision": manifest["revision"], "design_digest": manifest["design_digest"], "approved_by": approved_by, "approved_at": datetime.now(timezone.utc).isoformat(), "unresolved_policy_items": [], "pending_tier_c_items": [item.get("id") for item in contract.get("tier_c", [])], "approval_basis": {"kind": "user"}}
    atomic_write_json(design_dir / "approval.json", approval)
    return approval


def approve_delegated_grounding_revision(
    repo_root: Path, project_dir: Path, design_dir: Path
) -> dict[str, Any] | None:
    """Auto-approve a fact-only amendment when a project policy delegates it.

    This deliberately does *not* infer broad user consent.  The new contract
    must preserve every product-facing section of a previously approved
    revision; only receipt-bound grounding records and their resolved
    limitations may differ.
    """
    policy_path = project_dir / "design-autonomy.json"
    if not policy_path.is_file():
        return None
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid delegated-grounding policy: {exc}") from exc
    if policy.get("schema_version") != "1.0" or policy.get("delegated_grounding_auto_approval") is not True:
        return None

    candidate_contract = json.loads(
        (design_dir / "execution-contract.json").read_text(encoding="utf-8")
    )
    candidate_manifest = json.loads((design_dir / "manifest.json").read_text(encoding="utf-8"))
    candidate_revision = int(candidate_manifest["revision"])

    def product_projection(contract: dict[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(contract)
        for key in (
            "datasheets",
            "component_selections",
            "limitations",
            "design_inventory",
            "grounding_plan",
            "implementation_facts",
        ):
            value.pop(key, None)
        return value

    candidates = []
    for directory in project_dir.joinpath("design-package").glob("rev-[0-9][0-9][0-9][0-9]"):
        manifest_path = directory / "manifest.json"
        approval_path = directory / "approval.json"
        if not manifest_path.is_file() or not approval_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        if int(manifest.get("revision", 0)) < candidate_revision and approval.get("status") == "APPROVED":
            candidates.append((int(manifest["revision"]), directory, approval))
    if not candidates:
        return None
    source_revision, source_dir, source_approval = max(candidates)
    source_contract = json.loads((source_dir / "execution-contract.json").read_text(encoding="utf-8"))
    same_product_contract = (
        product_projection(source_contract) == product_projection(candidate_contract)
    )
    # Delegation never repairs a changed product contract.  Historical
    # packages that once used a permissive recovery escape hatch remain
    # immutable history; every new amendment must pass this equality check.
    if not same_product_contract:
        return None

    policy_hash = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    approval = {
        "schema_version": "1.0",
        "project_id": candidate_contract["project"],
        "status": "APPROVED",
        "spec_revision": candidate_revision,
        "design_digest": candidate_manifest["design_digest"],
        "approved_by": "delegated-grounding-policy",
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "unresolved_policy_items": [],
        "pending_tier_c_items": [item.get("id") for item in candidate_contract.get("tier_c", [])],
        "approval_basis": {
            "kind": "delegated_grounding",
            "policy_path": str(policy_path.relative_to(repo_root)).replace("\\", "/"),
            "policy_sha256": policy_hash,
            "source_revision": source_revision,
            "source_design_digest": str(source_approval.get("design_digest", "")),
        },
    }
    atomic_write_json(design_dir / "approval.json", approval)
    return approval


def create_revision(repo_root: Path, project: str, source_revision: int, target_revision: int) -> Path:
    """Create an immutable amendment draft; never auto-approve a copied design."""
    project_dir = (repo_root / "projects" / project).resolve()
    source = project_dir / "design-package" / f"rev-{source_revision:04d}"
    target = project_dir / "design-package" / f"rev-{target_revision:04d}"
    if target.exists(): raise FileExistsError(f"design revision already exists: {target}")
    target.mkdir(parents=True)
    for name in ("spec.md", "execution-contract.json"):
        shutil.copy2(source / name, target / name)
    contract = json.loads((target / "execution-contract.json").read_text(encoding="utf-8"))
    from .schema_capabilities import current_schema_version, require_executable_schema
    migration_errors = require_executable_schema(contract)
    atomic_write_json(target / "migration-report.json", {
        "schema_version": "1.0",
        "project_id": project,
        "source_revision": source_revision,
        "target_revision": target_revision,
        "source_contract_schema": contract.get("schema_version"),
        "target_contract_schema": current_schema_version(),
        "derived_fields": [],
        "unresolved_fields": [
            "operations",
            "architecture.component_api_manifest",
            "architecture.runtime_flow typed edges/ports/transitions",
            "integration.production_scenarios",
            "Tier C producer chains",
            "release core scenarios and forbidden patterns",
        ] if migration_errors else [],
        "input_files_unchanged": [
            f"requirements/{project}.md",
            f"connections/{project}.md",
        ],
        "status": "DESIGN_RECOMPILATION_REQUIRED" if migration_errors else "IMPACT_ANALYSIS_REQUIRED",
    })
    manifest = {"schema_version": "1.0", "project_id": project, "revision": target_revision, "inputs": _input_refs(repo_root, project), "providers": [{"name": "revision-copy", "kind": "design-generator", "source_revision": source_revision, "status": "requires-impact-analysis"}], "files": [file_ref(target / name, target, "text/markdown" if name.endswith(".md") else "application/json").model_dump() for name in ("spec.md", "execution-contract.json")]}
    calculated = design_digest(contract, manifest); manifest["design_digest"] = calculated
    atomic_write_json(target / "manifest.json", manifest)
    atomic_write_json(target / "design-validation.json", {"schema_version": "1.0", "project_id": project, "valid": False, "errors": ["revision impact analysis and revalidation required"], "warnings": [], "design_digest": calculated})
    atomic_write_json(target / "approval.json", {"schema_version": "1.0", "project_id": project, "status": "PENDING", "spec_revision": target_revision, "design_digest": calculated, "approved_by": None, "approved_at": None, "unresolved_policy_items": ["revision impact analysis"], "pending_tier_c_items": []})
    return target
