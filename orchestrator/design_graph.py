from __future__ import annotations

import json
import os
import shutil
import uuid
import hashlib
from pathlib import Path
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from .adapters.design_grounding import DesignGroundingAdapter
from .design_inventory import (
    materialize_grounding_requirements,
    reconcile_grounding_limitations,
    reconcile_grounding_unknowns,
)
from .design_package import (
    CodexDesignProvider,
    DesignDraft,
    DesignProvider,
    _bind_contract_project,
    _ground_contract,
    _input_refs,
    _preserve_blocking_unknowns,
    _preserve_complete_contract_rows,
    _preserve_grounding_facts,
    _require_object_entries,
    normalize_design_execution_contract,
    approve_delegated_grounding_revision,
    next_revision,
)
from .design_diagnostics import (
    design_route,
    diagnostic_retry_key,
    diagnostic_summaries,
    planning_diagnostics,
    structural_diagnostics,
    user_decision_diagnostics,
)
from .storage import ProjectStore, atomic_write_json, file_ref
from .input_authority import write_input_authority
from .validators import (
    design_digest,
    validate_contract,
    validate_design_package,
    validate_spec_review_surface,
)
from .model_context import (
    build_design_provider_context_plan,
    validate_design_provider_context_plan,
)
from .secrets import assert_no_secret_values, secret_values


class DesignState(TypedDict, total=False):
    project: str
    job_id: str
    revision: int | None
    project_dir: str
    session: str
    staging_root: str
    attempt: int
    max_attempts: int
    draft_path: str
    planned_contract_path: str
    inventory_path: str
    grounding_plan_path: str
    grounded_contract_path: str
    grounding_providers_path: str
    blocking_unknowns_path: str
    input_authority_path: str
    provider_context_plan_path: str
    errors_path: str
    diagnostics_path: str
    mode: str
    phase: str
    route: str
    design_dir: str
    spec: str
    summary: str
    input_fingerprint: str
    repair_counts: dict[str, int]


ProviderFactory = Callable[[], DesignProvider]
GroundingFactory = Callable[[ProjectStore, str], DesignGroundingAdapter]


def _draft_to_json(draft: DesignDraft) -> dict[str, Any]:
    return {
        "spec_markdown": draft.spec_markdown,
        "execution_contract": draft.execution_contract,
        "providers": draft.providers,
        "blocking_unknowns": draft.blocking_unknowns,
    }


def _draft_from_json(path: Path) -> DesignDraft:
    value = json.loads(path.read_text(encoding="utf-8"))
    return DesignDraft(
        spec_markdown=str(value["spec_markdown"]),
        execution_contract=dict(value["execution_contract"]),
        providers=list(value.get("providers", [])),
        blocking_unknowns=list(value.get("blocking_unknowns", [])),
    )


def _render_review_spec(
    source: str,
    *,
    resolved_unknowns: list[dict[str, Any]],
    resolved_limitations: list[dict[str, Any]],
) -> str:
    """Remove stale provisional wording before validating the sole review surface.

    The provider's prose is not authority after deterministic reconciliation.
    Leaving phrases such as ``[GROUNDING_PENDING]`` in ``spec.md`` makes a
    clean contract look unsafe to a reviewer and used to force a full provider
    rewrite.  This is deliberately narrow: only lines containing the known
    provisional approval phrases are removed, and every removal is replaced by
    an explicit, typed reconciliation below.
    """
    provisional = (
        "[grounding_pending]",
        "before approval",
        "remain grounding-dependent",
        "remains grounding-dependent",
    )
    resolved_statements = {
        str(item.get("statement") or "").strip().casefold()
        for item in resolved_unknowns
    }
    lines = [
        line
        for line in source.rstrip().splitlines()
        if not any(token in line.casefold() for token in provisional)
        and line.strip().casefold().lstrip("- ") not in resolved_statements
        and not (
            "hardware values absent from immutable implementation facts" in line.casefold()
            and "blocking unknown" in line.casefold()
        )
    ]
    spec = "\n".join(lines).rstrip()
    if resolved_unknowns or resolved_limitations:
        spec += (
            "\n\n## Harness grounding reconciliation and readiness\n\n"
            "The following provider statements are not open product decisions. "
            "Their authoritative handling is recorded here:\n"
        )
        for item in resolved_unknowns:
            spec += f"\n- {item['resolution']}\n"
        resolved_unknown_statements = {
            item["statement"] for item in resolved_unknowns
        }
        for item in resolved_limitations:
            if item["statement"] not in resolved_unknown_statements:
                spec += (
                    "\n- Superseded by completed deterministic grounding and "
                    "its validated provider receipts.\n"
                )
    return spec


class DesignGraphNodes:
    """Project-agnostic, artifact-backed Design Subgraph nodes."""

    def __init__(
        self,
        repo_root: Path,
        provider_factory: ProviderFactory | None = None,
        grounding_factory: GroundingFactory | None = None,
    ):
        self.repo_root = repo_root.resolve()
        self.provider_factory = provider_factory or CodexDesignProvider
        self.grounding_factory = grounding_factory

    def _project(self, state: DesignState) -> tuple[Path, ProjectStore]:
        project_dir = Path(state["project_dir"]).resolve()
        if project_dir.parent != (self.repo_root / "projects").resolve():
            raise ValueError("project directory must be a direct child of projects/")
        store = ProjectStore(project_dir)
        store.ensure()
        return project_dir, store

    def _progress(self, state: DesignState, phase: str) -> None:
        if not state.get("job_id"):
            return
        from .design_jobs import update_design_job_progress
        update_design_job_progress(
            self.repo_root, str(state["project"]), str(state["job_id"]), phase
        )

    @staticmethod
    def _attempt_dir(state: DesignState) -> Path:
        return Path(state["staging_root"]) / f"attempt-{int(state['attempt']):03d}"

    def initialize(self, state: DesignState) -> dict[str, Any]:
        self._progress(state, "initialize")
        project = str(state["project"])
        project_dir = (self.repo_root / "projects" / project).resolve()
        if project_dir.parent != (self.repo_root / "projects").resolve():
            raise ValueError("project must be a direct child name")
        _input_refs(self.repo_root, project)
        input_fingerprint = self._input_fingerprint(project)
        ProjectStore(project_dir).ensure()
        session = state.get("session") or uuid.uuid4().hex[:16]
        staging = project_dir / "design-package" / ".staging" / session
        staging.mkdir(parents=True, exist_ok=True)
        attempt = int(state.get("attempt") or 1)
        max_attempts = int(state.get("max_attempts") or 2)
        if max_attempts not in {1, 2}:
            raise ValueError("Design Graph supports at most one structural repair")
        attempt_dir = staging / f"attempt-{attempt:03d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        authority_path = staging / "input-authority.json"
        write_input_authority(authority_path, self.repo_root, project)
        return {
            "project_dir": str(project_dir),
            "session": session,
            "staging_root": str(staging),
            "attempt": attempt,
            "max_attempts": max_attempts,
            "mode": "DESIGN_RUNNING",
            "phase": "provider_context_gate",
            "route": "provider_context_gate",
            "input_fingerprint": input_fingerprint,
            "repair_counts": dict(state.get("repair_counts") or {}),
            "input_authority_path": str(authority_path),
        }

    def provider_context_gate(self, state: DesignState) -> dict[str, Any]:
        """Checkpoint the bounded Design model context before synthesis."""
        self._progress(state, "provider_context_gate")
        plan = build_design_provider_context_plan(
            self.repo_root, str(state["project"])
        )
        errors = validate_design_provider_context_plan(plan)
        attempt_dir = self._attempt_dir(state)
        plan_path = attempt_dir / "provider-context-plan.json"
        atomic_write_json(
            plan_path,
            {**plan, "valid": not errors, "errors": errors},
        )
        if errors:
            raise ValueError("Design provider context gate failed: " + "; ".join(errors))
        return {
            "provider_context_plan_path": str(plan_path),
            "phase": "synthesize",
            "route": "synthesize",
        }

    def _input_fingerprint(self, project: str) -> str:
        value = hashlib.sha256()
        for area in ("requirements", "connections"):
            path = self.repo_root / area / f"{project}.md"
            value.update(area.encode("utf-8"))
            value.update(path.read_bytes())
        return value.hexdigest()

    def synthesize(self, state: DesignState) -> dict[str, Any]:
        self._progress(state, "synthesize")
        attempt_dir = self._attempt_dir(state)
        path = attempt_dir / "draft.json"
        if not path.is_file():
            draft = self.provider_factory().generate(
                self.repo_root, str(state["project"])
            )
            draft = _bind_contract_project(draft, str(state["project"]))
            _require_object_entries(draft.execution_contract)
            raw_requirements = (
                self.repo_root
                / "requirements"
                / f"{state['project']}.md"
            ).read_text(encoding="utf-8")
            assert_no_secret_values(
                draft.spec_markdown
                + json.dumps(draft.execution_contract, ensure_ascii=False),
                secret_values(raw_requirements),
                "generated design",
            )
            atomic_write_json(path, _draft_to_json(draft))
        return {
            "draft_path": str(path),
            "phase": "inventory",
            "route": "inventory",
        }

    def inventory(self, state: DesignState) -> dict[str, Any]:
        self._progress(state, "inventory")
        attempt_dir = self._attempt_dir(state)
        # Regenerate from immutable user inputs on every repair attempt. This
        # preserves the input hash binding while allowing a Harness parser
        # improvement to repair a staged authority projection.
        write_input_authority(Path(state["input_authority_path"]), self.repo_root, str(state["project"]))
        draft = _draft_from_json(Path(state["draft_path"]))
        authority = json.loads(Path(state["input_authority_path"]).read_text(encoding="utf-8"))
        draft.execution_contract.setdefault("_input_authority", authority)
        draft.execution_contract["input_authority_ref"] = file_ref(Path(state["input_authority_path"]), self.repo_root, "application/json").model_dump()
        contract, inventory, plan, planning_errors = (
            materialize_grounding_requirements(draft.execution_contract)
        )
        contract_path = attempt_dir / "planned-contract.json"
        inventory_path = attempt_dir / "design-inventory.json"
        plan_path = attempt_dir / "grounding-plan.json"
        errors_path = attempt_dir / "planning-errors.json"
        atomic_write_json(contract_path, contract)
        atomic_write_json(
            inventory_path,
            {"schema_version": "1.0", "project_id": state["project"], "items": inventory},
        )
        atomic_write_json(
            plan_path,
            {"schema_version": "1.0", "project_id": state["project"], "items": plan},
        )
        atomic_write_json(
            errors_path,
            {
                "errors": planning_errors,
                "diagnostics": planning_diagnostics(planning_errors),
            },
        )
        return {
            "planned_contract_path": str(contract_path),
            "inventory_path": str(inventory_path),
            "grounding_plan_path": str(plan_path),
            "errors_path": str(errors_path),
            "phase": "ground",
            "route": "ground",
        }

    def ground(self, state: DesignState) -> dict[str, Any]:
        self._progress(state, "ground")
        project_dir, store = self._project(state)
        attempt_dir = self._attempt_dir(state)
        contract = json.loads(
            Path(state["planned_contract_path"]).read_text(encoding="utf-8")
        )
        run_id = (
            f"design-{state['project']}-staging-{state['session']}"
            f"-attempt-{int(state['attempt']):03d}"
        )
        grounding = (
            self.grounding_factory(store, run_id)
            if self.grounding_factory
            else DesignGroundingAdapter(self.repo_root, store, run_id)
        )
        grounded, providers, grounding_errors = _ground_contract(
            contract, grounding, self.repo_root
        )
        # Apply fixed evidence/workflow mechanics only after every provider
        # and preservation path has produced the final staging contract.
        grounded = normalize_design_execution_contract(grounded)
        contract_path = attempt_dir / "grounded-contract.json"
        providers_path = attempt_dir / "grounding-providers.json"
        errors_path = attempt_dir / "grounding-errors.json"
        atomic_write_json(contract_path, grounded)
        atomic_write_json(providers_path, providers)
        atomic_write_json(
            errors_path,
            {
                "errors": diagnostic_summaries(grounding_errors),
                "diagnostics": grounding_errors,
            },
        )
        return {
            "grounded_contract_path": str(contract_path),
            "grounding_providers_path": str(providers_path),
            "errors_path": str(errors_path),
            "phase": "validate",
            "route": "validate",
        }

    def validate(self, state: DesignState) -> dict[str, Any]:
        self._progress(state, "validate")
        attempt_dir = self._attempt_dir(state)
        draft = _draft_from_json(Path(state["draft_path"]))
        contract = json.loads(
            Path(state["grounded_contract_path"]).read_text(encoding="utf-8")
        )
        planning_result = json.loads(
            (attempt_dir / "planning-errors.json").read_text(encoding="utf-8")
        )
        grounding_result = json.loads(
            (attempt_dir / "grounding-errors.json").read_text(encoding="utf-8")
        )
        planning_errors = list(planning_result.get("errors", []))
        grounding_errors = list(grounding_result.get("errors", []))
        grounding_diagnostics = list(
            grounding_result.get("diagnostics", [])
        )
        grounding_is_incomplete = any(
            item.get("severity", "BLOCKING") == "BLOCKING"
            and item.get("responsible_party") == "design_grounding"
            for item in grounding_diagnostics
        )
        resolved_limitations = reconcile_grounding_limitations(
            contract,
            grounding_complete=not grounding_is_incomplete,
        )
        blocking_grounding_errors = [
            str(item.get("summary") or item.get("code"))
            for item in grounding_diagnostics
            if item.get("severity", "BLOCKING") == "BLOCKING"
        ]
        input_authority = json.loads(Path(state["input_authority_path"]).read_text(encoding="utf-8"))
        from .resource_resolution import resolve_resource_requirements
        allocations, allocation_errors = resolve_resource_requirements(contract)
        if contract.get("resource_requirements"):
            contract["resource_allocations"] = allocations
        contract_validator_errors = validate_contract(contract, input_authority)
        contract_validator_errors.extend(allocation_errors)
        active_unknowns, resolved_unknowns = reconcile_grounding_unknowns(
            contract,
            draft.blocking_unknowns,
            contract_validator_errors + planning_errors + blocking_grounding_errors,
            input_authority,
        )
        spec = _render_review_spec(
            draft.spec_markdown,
            resolved_unknowns=resolved_unknowns,
            resolved_limitations=resolved_limitations,
        )
        validator_errors = contract_validator_errors + validate_spec_review_surface(spec)
        contract_errors = validator_errors + planning_errors + grounding_errors
        diagnostics = (
            (
                []
                if grounding_is_incomplete
                else structural_diagnostics(validator_errors)
            )
            + list(planning_result.get("diagnostics", []))
            + grounding_diagnostics
            + user_decision_diagnostics(active_unknowns)
        )
        errors = diagnostic_summaries(diagnostics)
        if active_unknowns:
            spec += (
                "\n\n## Harness active design decisions\n\n"
                "These decisions require product-owner input before a formal "
                "design revision can be created. Any related concrete value "
                "elsewhere in this staged draft is a non-authoritative "
                "proposal and must not be implemented or approved:\n"
            )
            for item in active_unknowns:
                spec += f"\n- {item}\n"
        (attempt_dir / "spec.md").write_text(
            spec + "\n", encoding="utf-8"
        )
        atomic_write_json(attempt_dir / "execution-contract.json", contract)
        unknowns_path = attempt_dir / "blocking-unknowns.json"
        atomic_write_json(
            unknowns_path,
            {
                "active": active_unknowns,
                "resolved_by_grounding": resolved_unknowns,
                "resolved_limitations": resolved_limitations,
            },
        )
        atomic_write_json(
            attempt_dir / "errors.json",
            {
                "valid": not any(
                    item.get("severity", "BLOCKING") == "BLOCKING"
                    for item in diagnostics
                ),
                "errors": errors,
                "diagnostics": diagnostics,
            },
        )
        route, mode, phase = design_route(
            diagnostics,
            attempt=int(state["attempt"]),
            max_attempts=int(state.get("max_attempts") or 2),
            repair_counts=dict(state.get("repair_counts") or {}),
        )
        return {
            "errors_path": str(attempt_dir / "errors.json"),
            "diagnostics_path": str(attempt_dir / "errors.json"),
            "blocking_unknowns_path": str(unknowns_path),
            "route": route,
            "mode": mode,
            "phase": phase,
            "spec": str(attempt_dir / "spec.md"),
            "summary": "; ".join(errors),
        }

    @staticmethod
    def validation_route(state: DesignState) -> str:
        return str(state.get("route") or "blocked")

    def repair(self, state: DesignState) -> dict[str, Any]:
        self._progress(state, "repair")
        draft_path = Path(state.get("draft_path", ""))
        if not draft_path.is_file():
            # A repair is only valid against the immutable staging draft that
            # produced the validation errors.  If that artifact disappeared
            # (for example after an interrupted worker or project cleanup),
            # never let a raw FileNotFoundError escape or synthesize a repair
            # from incomplete state.  The caller can start a fresh, safe
            # design transaction against the unchanged user inputs.
            return {
                "mode": "FAULTED",
                "phase": "faulted",
                "route": "faulted",
                "summary": (
                    "design staging artifact missing before structural repair: "
                    f"{draft_path}; start a new design transaction"
                ),
            }
        base_draft = _draft_from_json(draft_path)
        prior = DesignDraft(
            spec_markdown=base_draft.spec_markdown,
            execution_contract=json.loads(
                Path(state["grounded_contract_path"]).read_text(encoding="utf-8")
            ),
            providers=base_draft.providers
            + json.loads(
                Path(state["grounding_providers_path"]).read_text(encoding="utf-8")
            ),
            blocking_unknowns=base_draft.blocking_unknowns,
        )
        error_record = json.loads(
            Path(state["errors_path"]).read_text(encoding="utf-8")
        )
        errors = list(error_record.get("errors", []))
        blocking = [
            item
            for item in error_record.get("diagnostics", [])
            if item.get("severity", "BLOCKING") == "BLOCKING"
        ]
        local_grounding_retry = bool(blocking) and all(
            item.get("responsible_party") == "design_grounding"
            for item in blocking
        )
        repair_counts = dict(state.get("repair_counts") or {})
        for item in blocking:
            key = diagnostic_retry_key(item)
            repair_counts[key] = repair_counts.get(key, 0) + 1
        if local_grounding_retry:
            # Keep the provider draft and receipt-bound deterministic facts
            # intact. The next generation changes only the probabilistic
            # deep-reader cache key; acquire/extract successes are reused.
            draft = DesignDraft(
                spec_markdown=base_draft.spec_markdown,
                execution_contract=prior.execution_contract,
                providers=base_draft.providers,
                blocking_unknowns=base_draft.blocking_unknowns,
            )
        else:
            provider = self.provider_factory()
            repair = getattr(provider, "repair", None)
            if repair is None:
                return {
                    "mode": "FAULTED",
                    "phase": "faulted",
                    "route": "faulted",
                    "summary": "design provider does not support structural repair",
                }
            draft = repair(
                self.repo_root, str(state["project"]), prior, errors
            )
            draft = _bind_contract_project(draft, str(state["project"]))
            draft = _preserve_grounding_facts(
                draft, prior.execution_contract
            )
            draft = _preserve_complete_contract_rows(
                draft, prior.execution_contract
            )
            _require_object_entries(draft.execution_contract)
            # A structural repair is not an authority event. It may add newly
            # discovered unknowns, but cannot erase a product-owner decision.
            draft = _preserve_blocking_unknowns(
                draft, prior.blocking_unknowns
            )
        attempt = int(state["attempt"]) + 1
        attempt_dir = Path(state["staging_root"]) / f"attempt-{attempt:03d}"
        attempt_dir.mkdir(parents=True, exist_ok=False)
        path = attempt_dir / "draft.json"
        atomic_write_json(path, _draft_to_json(draft))
        return {
            "attempt": attempt,
            "draft_path": str(path),
            "repair_counts": repair_counts,
            "phase": "inventory",
            "route": "inventory",
        }

    @staticmethod
    def repair_route(state: DesignState) -> str:
        if state.get("mode") == "BLOCKED":
            return "blocked"
        if state.get("mode") == "FAULTED":
            return "faulted"
        return "inventory"

    def promote(self, state: DesignState) -> dict[str, Any]:
        self._progress(state, "promote")
        project_dir, _ = self._project(state)
        if self._input_fingerprint(str(state["project"])) != state["input_fingerprint"]:
            raise ValueError(
                "design inputs changed during generation; start a new design transaction"
            )
        attempt_dir = self._attempt_dir(state)
        draft = _draft_from_json(Path(state["draft_path"]))
        contract = json.loads(
            (attempt_dir / "execution-contract.json").read_text(encoding="utf-8")
        )
        diagnostic_record = json.loads(
            (attempt_dir / "errors.json").read_text(encoding="utf-8")
        )
        review_warnings = [
            str(item.get("summary") or item.get("code"))
            for item in diagnostic_record.get("diagnostics", [])
            if item.get("severity") in {"REVIEW", "ADVISORY"}
        ]
        grounding_providers = json.loads(
            Path(state["grounding_providers_path"]).read_text(encoding="utf-8")
        )
        revision = int(state.get("revision") or next_revision(project_dir))
        target = project_dir / "design-package" / f"rev-{revision:04d}"
        prepared_target: Path | None = None
        if target.exists():
            # ``prepare-revision`` intentionally creates a pending, invalid
            # amendment shell before Design recomputes the package. Treat
            # only that exact shell as replaceable; valid or approved
            # revisions remain immutable and are never overwritten.
            try:
                approval = json.loads((target / "approval.json").read_text(encoding="utf-8"))
                validation = json.loads((target / "design-validation.json").read_text(encoding="utf-8"))
                manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise FileExistsError(f"design revision already exists: {target}")
            prepared_shell = (
                approval.get("status") == "PENDING"
                and validation.get("valid") is False
                and "revision impact analysis and revalidation required" in validation.get("errors", [])
                and any(
                    item.get("name") == "revision-copy"
                    and item.get("status") == "requires-impact-analysis"
                    for item in manifest.get("providers", [])
                    if isinstance(item, dict)
                )
            )
            if not prepared_shell:
                raise FileExistsError(f"design revision already exists: {target}")
            prepared_target = target.with_name(f".prepared-{state['session']}")
            os.replace(target, prepared_target)
        promotion = project_dir / "design-package" / f".promotion-{state['session']}"
        promotion.mkdir(parents=True, exist_ok=False)
        try:
            shutil.copy2(attempt_dir / "spec.md", promotion / "spec.md")
            shutil.copy2(Path(state["input_authority_path"]), promotion / "input-authority.json")
            contract["input_authority_ref"] = file_ref(
                promotion / "input-authority.json", promotion, "application/json"
            ).model_dump()
            atomic_write_json(attempt_dir / "execution-contract.json", contract)
            shutil.copy2(
                attempt_dir / "execution-contract.json",
                promotion / "execution-contract.json",
            )
            generators = [
                {**item, "kind": item.get("kind", "design-generator")}
                for item in draft.providers
            ]
            manifest = {
                "schema_version": "1.0",
                "project_id": state["project"],
                "revision": revision,
                "inputs": _input_refs(self.repo_root, str(state["project"])),
                "providers": generators + grounding_providers,
                "files": [],
            }
            manifest["files"] = [
                file_ref(
                    promotion / name,
                    promotion,
                    "text/markdown"
                    if name.endswith(".md")
                    else "application/json",
                ).model_dump()
                for name in ("spec.md", "execution-contract.json", "input-authority.json")
            ]
            calculated = design_digest(contract, manifest)
            manifest["design_digest"] = calculated
            atomic_write_json(promotion / "manifest.json", manifest)
            atomic_write_json(
                promotion / "design-validation.json",
                {
                    "schema_version": "1.0",
                    "project_id": state["project"],
                    "valid": True,
                    "errors": [],
                    "warnings": review_warnings,
                    "design_digest": calculated,
                },
            )
            atomic_write_json(
                promotion / "approval.json",
                {
                    "schema_version": "1.0",
                    "project_id": state["project"],
                    "status": "PENDING",
                    "spec_revision": revision,
                    "design_digest": calculated,
                    "approved_by": None,
                    "approved_at": None,
                    "unresolved_policy_items": [],
                    "pending_tier_c_items": [
                        item.get("id")
                        for item in contract.get("tier_c", [])
                        if isinstance(item, dict)
                    ],
                },
            )
            _, package_errors = validate_design_package(
                promotion, require_approval=False
            )
            if package_errors:
                raise ValueError(
                    "generated design package is invalid: "
                    + "; ".join(package_errors)
                )
            auto_approval = approve_delegated_grounding_revision(
                self.repo_root, project_dir, promotion
            )
            from .datasheet_library import sync_datasheet_references

            sync_datasheet_references(self.repo_root, project_dir, contract)
            os.replace(promotion, target)
        finally:
            if promotion.exists():
                shutil.rmtree(promotion)
            if prepared_target is not None and prepared_target.exists():
                if not target.exists():
                    os.replace(prepared_target, target)
                else:
                    shutil.rmtree(prepared_target)
        return {
            "mode": "APPROVED_SPEC" if auto_approval else "WAITING_SPEC",
            "phase": "bind" if auto_approval else "approval",
            "route": "end",
            "design_dir": str(target),
            "spec": str(target / "spec.md"),
            "summary": "delegated grounding revision auto-approved" if auto_approval else "",
        }

    @staticmethod
    def waiting_input(state: DesignState) -> dict[str, Any]:
        return {
            "mode": "WAITING_DESIGN_INPUT",
            "phase": "waiting_input",
            "route": "end",
        }

    @staticmethod
    def blocked(state: DesignState) -> dict[str, Any]:
        return {"mode": "BLOCKED", "phase": "blocked", "route": "end"}

    @staticmethod
    def faulted(state: DesignState) -> dict[str, Any]:
        return {"mode": "FAULTED", "phase": "faulted", "route": "end"}


def build_design_graph(
    repo_root: Path,
    checkpointer=None,
    provider_factory: ProviderFactory | None = None,
    grounding_factory: GroundingFactory | None = None,
):
    nodes = DesignGraphNodes(repo_root, provider_factory, grounding_factory)
    graph = StateGraph(DesignState)
    graph.add_node("initialize", nodes.initialize)
    graph.add_node("provider_context_gate", nodes.provider_context_gate)
    graph.add_node("synthesize", nodes.synthesize)
    graph.add_node("inventory", nodes.inventory)
    graph.add_node("ground", nodes.ground)
    graph.add_node("validate", nodes.validate)
    graph.add_node("repair", nodes.repair)
    graph.add_node("promote", nodes.promote)
    graph.add_node("waiting_input", nodes.waiting_input)
    graph.add_node("blocked", nodes.blocked)
    graph.add_node("faulted", nodes.faulted)
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "provider_context_gate")
    graph.add_edge("provider_context_gate", "synthesize")
    graph.add_edge("synthesize", "inventory")
    graph.add_edge("inventory", "ground")
    graph.add_edge("ground", "validate")
    graph.add_conditional_edges(
        "validate",
        nodes.validation_route,
        {
            "promote": "promote",
            "repair": "repair",
            "waiting_input": "waiting_input",
            "blocked": "blocked",
            "faulted": "faulted",
        },
    )
    graph.add_conditional_edges(
        "repair",
        nodes.repair_route,
        {"inventory": "inventory", "blocked": "blocked", "faulted": "faulted"},
    )
    graph.add_edge("promote", END)
    graph.add_edge("waiting_input", END)
    graph.add_edge("blocked", END)
    graph.add_edge("faulted", END)
    return graph.compile(checkpointer=checkpointer)


def run_design_graph(
    repo_root: Path,
    project: str,
    job_id: str,
    revision: int | None = None,
) -> dict[str, Any]:
    from langgraph.checkpoint.sqlite import SqliteSaver
    from .runtime_paths import ProjectRuntime

    runtime = ProjectRuntime(repo_root, project).ensure()
    config = {
        "configurable": {"thread_id": f"{project}:design:{job_id}"},
        "recursion_limit": 32,
    }
    initial: DesignState = {
        "project": project,
        "job_id": job_id,
        "revision": revision,
        "max_attempts": 2,
    }
    with SqliteSaver.from_conn_string(str(runtime.checkpoints)) as saver:
        graph = build_design_graph(repo_root, saver)
        snapshot = graph.get_state(config)
        if snapshot.values and not snapshot.next:
            return dict(snapshot.values)
        return dict(graph.invoke(None if snapshot.values else initial, config))


def inspect_design_graph_state(
    repo_root: Path, project: str, job_id: str
) -> dict[str, Any]:
    """Read the persisted failure cursor without mutating graph state."""
    from langgraph.checkpoint.sqlite import SqliteSaver
    from .runtime_paths import ProjectRuntime

    runtime = ProjectRuntime(repo_root, project).ensure()
    config = {"configurable": {"thread_id": f"{project}:design:{job_id}"}}
    with SqliteSaver.from_conn_string(str(runtime.checkpoints)) as saver:
        snapshot = build_design_graph(repo_root, saver).get_state(config)
        return dict(snapshot.values or {})
