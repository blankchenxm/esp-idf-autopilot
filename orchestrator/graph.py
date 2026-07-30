from __future__ import annotations

import json
import hashlib
import re
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from langgraph.errors import GraphInterrupt

from .adapters.hardware import HardwareAdapter
from .adapters.idf import IdfAdapter
from .adapters.serial import SerialAdapter
from .adapters.capabilities import CapabilityAdapter
from .adapters.agent import AgentAction, AgentAdapter
from .models import ArtifactRef, Blocker, Closure, Diagnostic, DiagnosticSeverity, Evidence, ExecutionEvent, Failure, FailureCategory, FailureDisposition, Receipt, ReleaseEvidence, RunMode, RunRecord, RunStateProjection, Tier, Verdict
from .errors import DiagnosticFailure, ReceiptFailure
from .policies import affected_consumers, disposition_for, failure_fingerprint, material_fingerprint, progress_fingerprint, recovery_budget
from .evaluation import evaluate_text
from .contract_consistency import validate_runtime_flow_source, validate_source_facts
from .implementation_reuse import assess_existing_implementation
from .state import HarnessState
from .storage import ProjectStore, atomic_write_json, digest, file_ref
from .subgraphs.release import release_marker
from .subgraphs.subsystem import expected_for_owner, subsystem_order, verification_batches
from .validators import (
    validate_design_package,
    validate_release_runtime_text,
    validate_release_transaction,
    validate_terminal,
)
from .implementation_readiness import assess_implementation_readiness
from .implementation_addendum import (
    build_implementation_addendum,
    validate_addendum_source_bindings,
    validate_implementation_addendum,
)
from .component_selection import required_operations_for_subsystem
from .schema_capabilities import require_executable_schema, schema_has
from .invariants import (
    load_invariant_registry,
    required_rule_ids_before,
    validate_graph_conformance,
    validate_invariant_registry,
)
from .operation_authority import (
    compile_operation_authority,
    validate_implementation_completeness,
    validate_linked_operations,
)
from .verification_plan import normalize_verification_images, report_counts
from .transactions import authority_bound_key, idempotency_authority
from .component_architecture import validate_component_architecture
from .production_composition import (
    parse_runtime_observations,
    validate_production_composition,
)
from .tier_c_producer import validate_tier_c_producer_chain
from .failure_lineage import (
    failure_lineage_id,
    relevant_material_revision,
    validate_recovery_admission,
)
from .project_hygiene import validate_generated_file_hygiene
from .control_events import (
    read_control_events,
    validate_control_event_delivery,
)
from .runtime_paths import ProjectRuntime


class HarnessNodes:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    def _context(self, state: HarnessState) -> tuple[Path, ProjectStore]:
        project_dir = Path(state["project_dir"]).resolve()
        if project_dir.parent != (self.repo_root / "projects").resolve():
            raise ValueError("project directory must be a direct child of projects/")
        store = ProjectStore(project_dir)
        store.ensure()
        return project_dir, store

    def _contract(self, state: HarnessState) -> dict:
        return json.loads((Path(state["design_dir"]) / "execution-contract.json").read_text(encoding="utf-8"))

    @staticmethod
    def _missing_project_kconfig_overrides(
        project_dir: Path, overrides: dict[str, Any]
    ) -> list[str]:
        """Reject project selftest flags that ESP-IDF would silently ignore."""
        prefix = "CONFIG_" + project_dir.name.upper() + "_"
        requested = {
            symbol for symbol in overrides
            if isinstance(symbol, str) and symbol.startswith(prefix)
        }
        declared: set[str] = set()
        for path in (project_dir / "components").glob("*/Kconfig"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            declared.update(
                "CONFIG_" + match.group(1)
                for match in re.finditer(r"(?m)^\s*config\s+([A-Z0-9_]+)\b", text)
            )
        return sorted(requested - declared)

    @staticmethod
    def _release_selftest_symbol(contract: dict[str, Any]) -> str:
        """Normalize legacy string and structured selftest-off release config."""
        setting = contract.get("release", {}).get("selftest_config")
        if isinstance(setting, dict):
            if len(setting) != 1:
                raise ValueError("release.selftest_config must name one disabled Kconfig symbol")
            symbol, requested = next(iter(setting.items()))
            if not isinstance(symbol, str) or str(requested) not in {"", "n"}:
                raise ValueError("release.selftest_config must request a disabled Kconfig symbol")
            return symbol
        symbol, separator, requested = str(setting or "").partition("=")
        if not symbol or (separator and requested not in {"", "n"}):
            raise ValueError("release.selftest_config must request a disabled Kconfig symbol")
        return symbol

    def _integration_owner(self, state: HarnessState) -> str:
        owners = [
            str(item["id"])
            for item in self._contract(state).get("subsystems", [])
            if item.get("execution_role") == "integration"
        ]
        return owners[0] if len(owners) == 1 else "integration"

    def _transaction_key(
        self, state: HarnessState, operation: str, **scope: Any
    ) -> str:
        """Bind a replayable side effect to exact authority and material."""
        project_dir, _ = self._context(state)
        return authority_bound_key(
            run_id=state["run_id"],
            design_digest=state.get("design_digest"),
            hardware_identity=state.get("hardware_identity"),
            material_fingerprint=material_fingerprint(project_dir, state),
            operation=operation,
            scope=scope,
        )

    def _event(self, state: HarnessState, node: str, payload: dict[str, Any]) -> None:
        _, store = self._context(state)
        store.append_event(ExecutionEvent(event_id=store.new_id("event"), run_id=state["run_id"], event_type="node", node=node, payload=payload))

    def _pass_receipt(
        self,
        state: HarnessState,
        category: str,
        operation: str,
        *,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        artifacts: list[ArtifactRef] | None = None,
        idempotency_key: str | None = None,
    ) -> Receipt:
        _, store = self._context(state)
        if idempotency_key:
            cached = store.find_successful_receipt(
                operation, idempotency_key
            )
            if cached is not None:
                return cached
        now = datetime.now(timezone.utc).isoformat()
        receipt = Receipt(
            receipt_id=store.new_id(operation),
            run_id=state["run_id"],
            operation=operation,
            started_at=now,
            finished_at=now,
            success=True,
            inputs={
                **inputs,
                "idempotency_key": idempotency_key,
                "idempotency_authority": idempotency_authority(
                    idempotency_key
                ),
            },
            outputs=outputs,
            artifacts=artifacts or [],
            failure=None,
        )
        store.write_receipt(receipt, category)
        return receipt

    @staticmethod
    def _load_receipt_by_id(project_dir: Path, receipt_id: str) -> Receipt:
        matches = [
            path
            for path in (project_dir / "execution" / "receipts").rglob("*.json")
            if path.stem == receipt_id
        ]
        if len(matches) != 1:
            raise ValueError(
                f"expected one immutable Receipt {receipt_id!r}, found {len(matches)}"
            )
        return Receipt.model_validate_json(
            matches[0].read_text(encoding="utf-8")
        )

    @staticmethod
    def _equivalent_evidence_id(
        store: ProjectStore,
        *,
        run_id: str,
        design_digest: str,
        test_id: str,
        firmware_sha256: str,
        receipt_ids: list[str],
    ) -> str | None:
        expected_receipts = set(receipt_ids)
        for path in store.evidence.rglob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                value.get("run_id") == run_id
                and value.get("design_digest") == design_digest
                and value.get("test_id") == test_id
                and value.get("firmware_sha256") == firmware_sha256
                and set(value.get("receipt_ids", [])) == expected_receipts
                and value.get("verdict") == "PASS"
            ):
                return str(value["evidence_id"])
        return None

    @staticmethod
    def _owner_material_revision(
        project_dir: Path, owner: str | None, state: HarnessState,
    ) -> str:
        files: list[tuple[str, str]] = []
        roots = (
            [project_dir / "components" / owner]
            if owner and (project_dir / "components" / owner).is_dir()
            else [project_dir / "main"]
        )
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                files.append((
                    path.relative_to(project_dir).as_posix(),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                ))
        return relevant_material_revision({
            "design_digest": state.get("design_digest"),
            "owner": owner,
            "image_id": state.get("transaction", {}).get("image", {}).get("image_id"),
            "files": files,
        })

    @staticmethod
    def _product_default_instruction(owner: str) -> str:
        """Versioned Harness defaults used only when the spec leaves a product value open."""
        if owner == "audio_pipeline":
            return (
                " Apply Harness product-default policy audio-pcm-v1: use "
                "48_000 Hz, signed 16-bit PCM, stereo, and a 240-second "
                "maximum recording duration. These are product defaults, not "
                "claimed external-datasheet facts; keep that distinction in source."
            )
        return ""

    @staticmethod
    def _require_owned_source(project_dir: Path, owner: str, rows: list[dict] | None = None) -> None:
        """Ensure a graph stage only consumes materialized, project-owned source.

        Source authoring belongs to the parent Codex session, where it can inspect the
        approved contract and make an auditable patch.  Launching a second, disposable
        Codex process from a graph node was neither a reliable transaction nor a source
        of hardware evidence.  The execution graph therefore validates the boundary and
        performs only deterministic build/flash/serial work.
        """
        if not (project_dir / "CMakeLists.txt").is_file():
            raise FileNotFoundError("project CMakeLists.txt is missing")
        if owner == "integration":
            main_dir = project_dir / "main"
            if not main_dir.is_dir() or not any(main_dir.rglob("*.c")):
                raise FileNotFoundError("integration requires materialized main/ C source")
            # Integration behavior is orchestrated from ``main/`` but its
            # semantic implementation belongs to project components.  Looking
            # at main alone turns the intended architecture rule into a false
            # source-gate failure whenever a component owns the test logic.
            source_root = project_dir / "components"
        else:
            component_dir = project_dir / "components" / owner
            if not (component_dir / "CMakeLists.txt").is_file(): raise FileNotFoundError(f"component {owner!r} CMakeLists.txt is missing")
            if not any(component_dir.rglob("*.c")): raise FileNotFoundError(f"component {owner!r} has no C source")
            if not any((component_dir / "include").rglob("*.h")): raise FileNotFoundError(f"component {owner!r} has no public semantic header")
            source_root = component_dir
        # Runtime markers are observable behavior, not source literals. They
        # may be assembled through macros, format strings, or generated lookup
        # tables, so source substring matching is neither sound nor complete.
        # The fresh serial evaluator below remains their deterministic gate.

    def _materialize_owner_source(
        self, state: HarnessState, project_dir: Path, store: ProjectStore,
        owner: str, rows: list[dict],
    ) -> list[str]:
        """Create/repair the owner source before its first deterministic build.

        A new approved project intentionally has no generated source after a
        reset.  Treat that absence as an implementation transaction, not as a
        build failure.  The AgentAdapter writes an immutable receipt and only
        imports whitelisted project-owned files from its disposable mirror.
        """
        addendum_path, readiness_receipts = self._ensure_implementation_readiness(
            state, project_dir, store, owner
        )
        addendum_facts: list[dict[str, Any]] = []
        if addendum_path is not None:
            value = json.loads(addendum_path.read_text(encoding="utf-8"))
            addendum_facts = [
                item for item in value.get("implementation_facts", [])
                if isinstance(item, dict)
            ]
        reuse = assess_existing_implementation(
            project_dir, owner, self._contract(state), addendum_facts
        )
        if reuse.reusable:
            self._event(state, "implementation_reuse", {
                "owner": owner,
                "decision": "reuse",
                "source_digest": reuse.source_digest,
                "addendum": addendum_path.relative_to(project_dir).as_posix()
                if addendum_path is not None else None,
            })
            return readiness_receipts
        action = AgentAction(
            project=str(state["project"]),
            owner=owner,
            contract_path=Path(state["design_dir"]) / "execution-contract.json",
            requirement_path=self.repo_root / "requirements" / f"{state['project']}.md",
            connection_path=self.repo_root / "connections" / f"{state['project']}.md",
            instruction=(
                "Repair only the named reuse-gate failures for this existing "
                f"owner; do not rewrite compliant source. Failures: {reuse.reasons}"
            ),
            implementation_addendum_path=addendum_path,
        )
        result = AgentAdapter(
            self.repo_root, store, str(state["run_id"]), timeout=1200
        ).execute(action)
        if not result.success:
            raise ReceiptFailure(result)
        repaired = assess_existing_implementation(
            project_dir, owner, self._contract(state), addendum_facts
        )
        if not repaired.reusable:
            # A successful agent process is not proof that it closed every
            # deterministic reuse assertion.  Keep this an owner-directed
            # repair (rather than degrading it to an unknown Harness fault),
            # so a later material change retries the same checkpoint.
            raise DiagnosticFailure(Diagnostic(
                code="IMPLEMENTATION_REUSE_REPAIR_INCOMPLETE",
                cause=FailureCategory.API,
                disposition=FailureDisposition.REPAIR_INTERNAL,
                responsible_party="implementation_agent",
                affected_owner=owner,
                subsystem_id=owner,
                summary=(
                    f"owner {owner!r} remains ineligible for reuse: "
                    f"{repaired.reasons}"
                ),
                retry_scope="owner_and_consumers",
            ))
        self._event(state, "implementation_reuse", {
            "owner": owner,
            "decision": "repaired",
            "source_digest": repaired.source_digest,
            "reasons": reuse.reasons,
        })
        return readiness_receipts + [result.receipt_id]

    def _ensure_implementation_readiness(
        self,
        state: HarnessState,
        project_dir: Path,
        store: ProjectStore,
        owner: str,
    ) -> tuple[Path | None, list[str]]:
        """Bind selection coverage and only missing facts before coding."""
        contract = self._contract(state)
        if schema_has(str(contract.get("schema_version")), "typed_operations"):
            # Current contracts are resolved once by the dedicated,
            # receipt-producing operation_authority node. Re-entering the
            # legacy owner-level readiness helper would reintroduce blanket
            # local-IDF/project-custom coverage.
            return None, []
        subsystem = next(
            (
                item for item in contract.get("subsystems", [])
                if item.get("id") == owner
            ),
            None,
        )
        if not isinstance(subsystem, dict):
            # Legacy/private materialization tests and pre-contract recovery
            # fixtures may exercise the source boundary with an empty contract.
            # Readiness has no authority to invent an owner in that case.
            if not contract.get("subsystems"):
                return None, []
            raise ValueError(f"unknown implementation owner {owner!r}")
        selection = next(
            (
                item for item in contract.get("component_selections", [])
                if item.get("subsystem_id") == owner
            ),
            None,
        )
        required_operations = required_operations_for_subsystem(
            contract, subsystem
        )
        # Project-owned policy/orchestration has no external implementation
        # source to target-read. Its frozen operations are the approved design
        # authority; routing it to an external datasheet reader is invalid.
        if (
            selection is None
            and subsystem.get("classification") == "project_custom"
        ):
            selection = {
                "decision": "project_owned",
                "covered_operations": list(required_operations),
            }
        if not required_operations and selection is None:
            return None, []
        addendum_root = store.execution / "implementation-addenda" / owner
        if addendum_root.is_dir():
            for path in sorted(addendum_root.glob("*.json"), reverse=True):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not validate_implementation_addendum(
                    value,
                    design_digest=str(state["design_digest"]),
                    owner=owner,
                ):
                    return path, []
        design_facts = [
            dict(item)
            for item in contract.get("implementation_facts", [])
            if isinstance(item, dict) and item.get("subsystem_id") == owner
        ]
        readiness = assess_implementation_readiness(
            owner=owner,
            required_operations=required_operations,
            selection=selection,
            existing_facts=design_facts,
        )
        receipt_ids: list[str] = []
        # Targeted datasheet reads are probabilistic. Preserve facts anchored
        # by earlier successful reads in this run so a later response cannot
        # erase already-grounded operation coverage.
        prior_targeted: list[dict[str, Any]] = []
        readiness_receipts = store.receipts / "readiness"
        if readiness_receipts.is_dir():
            for path in readiness_receipts.glob("*.json"):
                try:
                    prior = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if (
                    prior.get("success") is not True
                    or prior.get("operation") != "datasheet_artifact_inspect"
                ):
                    continue
                for item in prior.get("outputs", {}).get("implementation_facts", []):
                    if isinstance(item, dict) and item.get("subsystem_id") == owner:
                        prior_targeted.append({
                            **item,
                            "provider_receipt_id": prior.get("receipt_id"),
                        })
        facts = design_facts + prior_targeted
        if not readiness.ready:
            sheet = next(
                (
                    item for item in contract.get("datasheets", [])
                    if item.get("subsystem_id") == owner
                ),
                None,
            )
            if not isinstance(sheet, dict):
                raise DiagnosticFailure(Diagnostic(
                    code="READINESS_DATASHEET_MISSING",
                    cause=FailureCategory.DATASHEET,
                    disposition=FailureDisposition.INTERNAL_FAULT,
                    responsible_party="implementation_readiness",
                    affected_owner=owner,
                    subsystem_id=owner,
                    summary=(
                        f"{owner} requires targeted implementation facts "
                        "but has no approved L1 datasheet record"
                    ),
                    retry_scope=f"{owner}:targeted_reader",
                ))
            from .adapters.design_grounding import DesignGroundingAdapter
            reader = DesignGroundingAdapter(
                self.repo_root,
                store,
                str(state["run_id"]),
                receipt_category="readiness",
            )
            receipt = reader.datasheet_inspect(
                owner,
                dict(sheet),
                requested_facts=readiness.reader_requests,
            )
            receipt_ids.append(receipt.receipt_id)
            if not receipt.success:
                raise ReceiptFailure(receipt)
            targeted = [
                {
                    **item,
                    "provider_receipt_id": receipt.receipt_id,
                }
                for item in receipt.outputs.get("implementation_facts", [])
                if isinstance(item, dict)
            ]
            facts = design_facts + prior_targeted + targeted
            readiness = assess_implementation_readiness(
                owner=owner,
                required_operations=required_operations,
                selection=selection,
                existing_facts=facts,
            )
            if not readiness.ready:
                raise DiagnosticFailure(Diagnostic(
                    code="READINESS_FACT_GAPS_REMAIN",
                    cause=FailureCategory.DATASHEET,
                    disposition=FailureDisposition.REPAIR_INTERNAL,
                    responsible_party="implementation_readiness",
                    affected_owner=owner,
                    subsystem_id=owner,
                    summary=(
                        f"targeted reader left required operation gaps for "
                        f"{owner}: {readiness.missing_facts}"
                    ),
                    retry_scope=f"{owner}:targeted_reader",
                ))
        source_receipt_ids = {
            str(item.get("provider_receipt_id"))
            for item in facts
            if item.get("provider_receipt_id")
        }
        if selection and selection.get("provider_receipt_id"):
            source_receipt_ids.add(str(selection["provider_receipt_id"]))
        operation_authorities = [dict(item) for item in readiness.operation_authorities]
        default_authorities = [
            item for item in operation_authorities
            if item.get("kind") == "local_idf_default"
        ]
        if default_authorities:
            idf_receipt_id = next((
                receipt_id
                for receipt_id in state.get("receipt_ids", [])
                if any(
                    json.loads(path.read_text(encoding="utf-8")).get("operation") == "idf_version"
                    for path in store.receipts.rglob(f"{receipt_id}.json")
                )
            ), None)
            if not idf_receipt_id:
                raise ValueError("local-IDF default policy requires an idf_version receipt")
            source_receipt_ids.add(str(idf_receipt_id))
            for authority in default_authorities:
                authority["provider_receipt_id"] = str(idf_receipt_id)
                authority["provider_operation"] = "idf_version"
        source_receipts: list[dict[str, str]] = []
        for receipt_id in sorted(source_receipt_ids):
            paths = list(store.receipts.rglob(f"{receipt_id}.json"))
            if len(paths) != 1:
                raise ValueError(
                    f"implementation readiness receipt binding is ambiguous "
                    f"for {receipt_id!r}"
                )
            reference = file_ref(paths[0], project_dir, "application/json")
            source_receipts.append({
                "receipt_id": receipt_id,
                "sha256": reference.sha256,
                "path": reference.path,
            })
        addendum_selection = dict(
            selection or {"decision": "project_owned", "covered_operations": []}
        )
        # Persist the readiness-derived coverage. A receipt-bound local_idf
        # selection covers MCU-native lifecycle operations without an external
        # datasheet, so its addendum must retain that determination.
        addendum_selection["covered_operations"] = list(
            readiness.component_covered_operations
        )
        addendum = build_implementation_addendum(
            design_digest=str(state["design_digest"]),
            owner=owner,
            selection=addendum_selection,
            required_operations=required_operations,
            implementation_facts=facts,
            source_receipts=source_receipts,
            operation_authorities=operation_authorities,
        )
        errors = validate_implementation_addendum(
            addendum,
            design_digest=str(state["design_digest"]),
            owner=owner,
        )
        if errors:
            raise ValueError("; ".join(errors))
        return store.write_implementation_addendum(addendum), receipt_ids

    def _validated_implementation_addenda(
        self,
        state: HarnessState,
        owners: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        project_dir, store = self._context(state)
        contract = self._contract(state)
        values: list[dict[str, Any]] = []
        errors: list[str] = []
        for subsystem in contract.get("subsystems", []):
            if not isinstance(subsystem, dict):
                continue
            owner = str(subsystem.get("id") or "")
            if owners is not None and owner not in owners:
                continue
            # Product integration/orchestration has no external component
            # selection to bind. Its source remains checked directly against
            # the contract, but it must not be forced to invent an addendum.
            if subsystem.get("execution_role") == "integration":
                continue
            required = required_operations_for_subsystem(contract, subsystem)
            if not required:
                continue
            paths = sorted(
                (
                    store.execution
                    / "implementation-addenda"
                    / owner
                ).glob("*.json"),
                reverse=True,
            )
            valid: dict[str, Any] | None = None
            for path in paths:
                try:
                    candidate = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                candidate_errors = validate_implementation_addendum(
                    candidate,
                    design_digest=str(state["design_digest"]),
                    owner=owner,
                )
                candidate_errors.extend(
                    validate_addendum_source_bindings(project_dir, candidate)
                )
                if not candidate_errors:
                    valid = candidate
                    break
            if valid is None:
                errors.append(
                    f"{owner} lacks a valid design/source-bound implementation addendum"
                )
            else:
                values.append(valid)
        return values, errors

    def _implementation_addendum_path(
        self, state: HarnessState, owner: str
    ) -> Path | None:
        _, store = self._context(state)
        root = store.execution / "implementation-addenda" / owner
        for path in sorted(root.glob("*.json"), reverse=True):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not validate_implementation_addendum(
                value,
                design_digest=str(state["design_digest"]),
                owner=owner,
            ):
                return path
        return None

    @staticmethod
    def _verification_workspace(
        project_dir: Path, state: HarnessState, batch: str
    ) -> Path:
        """Return a short project-local root for isolated ESP-IDF builds.

        ESP-IDF creates deeply nested generated object paths.  Keeping an
        isolated build under ``execution/verification/run-...`` exceeds the
        practical Windows toolchain path budget before the compiler can write
        dependency files.  This directory remains project-local and run/batch
        unique while leaving enough path budget for generated sources.
        """
        run_token = str(state["run_id"]).removeprefix("run-")[:8]
        batch_token = f"b{int(state.get('batch_index') or 0)}-{batch[:24]}"
        return project_dir / ".v" / run_token / batch_token

    @staticmethod
    def _serial_completion_marker(rows: list[dict]) -> str | None:
        """Choose a capture stop marker that cannot truncate ordered evidence.

        A component may own both a policy row with an early standalone marker
        and an operational row whose ordered sequence completes later.  Waiting
        for the first standalone marker closes the monitor before the operation
        is observable.  The final marker of an ordered sequence is therefore
        the authoritative stop condition whenever one is declared.
        """
        ordered = [expected["ordered_markers"][-1] for row in rows
                   if (expected := row.get("expected", {})).get("ordered_markers")]
        if ordered:
            return ordered[-1]
        return next((row.get("expected", {}).get("marker") for row in rows
                     if row.get("expected", {}).get("marker")), None)

    @staticmethod
    def _verification_setup(rows: list[dict]) -> dict[str, Any]:
        """Return the single validated setup for a frozen verification batch.

        Legacy contracts remain normal-boot.  Schema 1.5 rejects mixed setup
        values in a batch, so one image/hash always maps to one setup.
        """
        setup = rows[0].get("test_setup") or {"kind": "normal_boot"}
        if any((row.get("test_setup") or {"kind": "normal_boot"}) != setup for row in rows):
            raise ValueError("verification batch mixes test_setup values")
        return setup

    @staticmethod
    def _serial_timeout(rows: list[dict], setup: dict[str, Any]) -> int:
        """Apply the versioned Harness capture default for firmware selftests.

        An explicit, frozen ``timeout_s`` remains authoritative.  Older
        contracts often omit it; their former generic 15-second capture is too
        short for legitimate reset/recovery selftests (for example a complete
        flash scan).  ``selftest-capture-v1`` gives those isolated images a
        bounded 60-second default without changing normal-boot behavior.
        """
        declared = max((int(row.get("timeout_s", 0)) for row in rows), default=0)
        if declared > 0:
            return declared
        return 60 if setup.get("kind") == "firmware_selftest" else 15

    @staticmethod
    def _evidence_kinds(row: dict, available: set[str]) -> list[str]:
        """Reject a declared evidence contract that this node did not produce."""
        declared = row.get("evidence_contract")
        if not declared:
            return sorted(available)
        required = set(declared.get("required_kinds", []))
        missing = sorted(required - available)
        if missing:
            raise ValueError(f"{row.get('test_id', row.get('id'))} evidence contract missing runtime kinds: {missing}")
        return sorted(required)

    @staticmethod
    def _integration_rows_for_test(
        test: dict[str, Any], rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        test_id = str(test.get("test_id") or test.get("id") or "")
        requirements = set(test.get("requirement_ids") or [])
        return [
            row
            for row in rows
            if (
                row.get("integration_test_id") == test_id
                or (
                    not row.get("integration_test_id")
                    and row.get("requirement_id") in requirements
                )
            )
        ]

    def _projection(self, state: HarnessState, updates: dict[str, Any]) -> None:
        _, store = self._context(state)
        combined = dict(state)
        combined.update(updates)
        # Terminal success is a new authoritative state, not a presentation
        # layered over a historical blocker.  Keep the immutable failure
        # receipts/events, but never project mutually contradictory control
        # fields such as COMPLETE + blocker.
        terminal = combined.get("mode") == RunMode.COMPLETE.value
        projection = RunStateProjection(
            run_id=combined["run_id"], mode=combined.get("mode", RunMode.CONTINUOUS.value), cursor=combined.get("cursor", "STAGE 0:init"),
            next_action=combined.get("next_action", "resume orchestrator"), progress_seq=combined.get("progress_seq", 0),
            progress_fingerprint=progress_fingerprint(combined), release_verified=combined.get("release_verified", False),
            closure=combined.get("closure"), release_evidence=combined.get("release_evidence"),
            blocker=None if terminal else combined.get("blocker"),
        )
        store.project_state(projection)

    def safe(self, node: str):
        operation = getattr(self, node)
        def wrapped(state: HarnessState) -> dict:
            try:
                updates = operation(state)
                return {**updates, "failure": None, "failed_node": None}
            except GraphInterrupt:
                raise
            except Exception as exc:
                project_dir, store = self._context(state)
                source_receipt = exc.receipt if isinstance(exc, ReceiptFailure) else None
                typed_diagnostic = exc.diagnostic if isinstance(exc, DiagnosticFailure) else None
                if source_receipt is not None:
                    typed_failure = source_receipt.failure
                    assert typed_failure is not None
                    summary = typed_failure.summary
                    category = typed_failure.category
                    retryable = typed_failure.retryable
                    evidence = typed_failure.evidence or source_receipt.artifacts
                elif typed_diagnostic is not None:
                    summary = typed_diagnostic.summary
                    category = typed_diagnostic.cause
                    retryable = typed_diagnostic.disposition in {
                        FailureDisposition.RETRY_TRANSIENT,
                        FailureDisposition.REPAIR_INTERNAL,
                    }
                    evidence = typed_diagnostic.evidence
                else:
                    summary = f"{type(exc).__name__}: {exc}"
                    # Untyped exceptions are Harness defects. Presentation
                    # text never grants retry or firmware-repair authority.
                    category = FailureCategory.UNKNOWN
                    retryable = False
                    evidence = []
                material = material_fingerprint(project_dir, state); signature = failure_fingerprint(node, category, summary, material)
                started = datetime.now(timezone.utc).isoformat(); receipt_id = store.new_id("failure")
                log_path = store.logs / state["run_id"] / f"{receipt_id}.log"; log_path.parent.mkdir(parents=True, exist_ok=True)
                # Unknown Harness exceptions must retain their traceback.  A
                # one-line Windows PermissionError has no actionable path and
                # turns a deterministic repair into guesswork.
                detail = (
                    traceback.format_exc()
                    if source_receipt is None and typed_diagnostic is None
                    else summary + "\n"
                )
                log_path.write_text(detail, encoding="utf-8")
                transaction_owners = list(
                    state.get("transaction", {}).get("image", {}).get(
                        "owners", []
                    )
                )
                attributed_owner = (
                    (typed_diagnostic.affected_owner if typed_diagnostic else None)
                    or (
                        source_receipt.failure.owner
                        if source_receipt and source_receipt.failure else None
                    )
                    or (
                        transaction_owners[0]
                        if len(transaction_owners) == 1 else None
                    )
                )
                owner = (
                    attributed_owner
                    or ((state.get("subsystems") or [None])[state.get("subsystem_index", 0)]
                        if node == "subsystem" and state.get("subsystem_index", 0) < len(state.get("subsystems", []))
                        else (
                            self._integration_owner(state)
                            if node in {
                                "integration",
                                "release_smoke",
                                "release",
                            }
                            else node
                        ))
                )
                disposition = (
                    typed_diagnostic.disposition
                    if typed_diagnostic is not None
                    else (
                        disposition_for(category, retryable=retryable, node=node)
                        if source_receipt is not None
                        else FailureDisposition.INTERNAL_FAULT
                    )
                )
                if (
                    typed_diagnostic is None
                    and disposition == FailureDisposition.REPAIR_INTERNAL
                    and (
                        node in {
                            "configure", "integration_configure",
                            "release_configure", "release_fullclean",
                        }
                        or attributed_owner is None
                    )
                ):
                    disposition = FailureDisposition.INTERNAL_FAULT
                boundary_artifact = file_ref(log_path, project_dir, "text/plain")
                failure = Failure(
                    category=category,
                    summary=summary,
                    owner=owner,
                    retryable=retryable,
                    attempt=(state.get("failure_attempts", {}).get(signature, 0) + 1),
                    evidence=evidence or [boundary_artifact],
                )
                diagnostic = typed_diagnostic or Diagnostic(
                    code=f"{node}_{category.value}_failed".upper().replace("-", "_"),
                    cause=category,
                    disposition=disposition,
                    severity=DiagnosticSeverity.BLOCKING,
                    responsible_party=("implementation_agent" if disposition == FailureDisposition.REPAIR_INTERNAL else "orchestrator"),
                    affected_owner=owner,
                    subsystem_id=owner if node == "subsystem" else None,
                    invariant_id=(
                        typed_diagnostic.code
                        if typed_diagnostic is not None else f"UNTYPED:{node}"
                    ),
                    operation_id=(
                        str(state.get("transaction", {}).get("operation_id"))
                        if state.get("transaction", {}).get("operation_id")
                        else None
                    ),
                    image_id=(
                        str(state.get("transaction", {}).get("image", {}).get("image_id"))
                        if state.get("transaction", {}).get("image")
                        else None
                    ),
                    summary=summary,
                    evidence=evidence or [boundary_artifact],
                    retry_scope=("owner_and_consumers" if disposition == FailureDisposition.REPAIR_INTERNAL else node),
                    material_fingerprint=material,
                    failure_fingerprint=signature,
                    invalidated_descendants=[
                        right for left, right in (
                            ("configure", "build"),
                            ("build", "flash"),
                            ("flash", "observe"),
                            ("observe", "evaluate"),
                            ("evaluate", "evidence_commit"),
                        ) if left == node
                    ],
                    model_call_admitted=(
                        disposition == FailureDisposition.REPAIR_INTERNAL
                    ),
                )
                lineage = failure_lineage_id(
                    invariant_id=(
                        typed_diagnostic.code
                        if typed_diagnostic is not None
                        else f"UNTYPED:{node}"
                    ),
                    node=node,
                    owner=owner,
                    operation_id=(
                        str(state.get("transaction", {}).get("operation_id"))
                        if state.get("transaction", {}).get("operation_id")
                        else None
                    ),
                    test_id=(
                        typed_diagnostic.test_id
                        if typed_diagnostic is not None else None
                    ),
                    image_id=(
                        str(state.get("transaction", {}).get("image", {}).get("image_id"))
                        if state.get("transaction", {}).get("image")
                        else None
                    ),
                )
                material_revision = self._owner_material_revision(
                    project_dir, owner, state
                )
                receipt = Receipt(
                    receipt_id=receipt_id,
                    run_id=state["run_id"],
                    operation=f"{node}_failure",
                    started_at=started,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    success=False,
                    inputs={
                        "node": node,
                        "material_fingerprint": material,
                        "source_receipt_id": source_receipt.receipt_id if source_receipt else None,
                    },
                    outputs={
                        "failure_fingerprint": signature,
                        "failure_lineage_id": lineage,
                        "material_revision": material_revision,
                        "diagnostic": diagnostic.model_dump(mode="json"),
                    },
                    artifacts=[boundary_artifact],
                    failure=failure,
                )
                store.write_receipt(receipt, "failure")
                source_ids = [source_receipt.receipt_id] if source_receipt and source_receipt.receipt_id not in state.get("receipt_ids", []) else []
                updates = {
                    "failure": {**failure.model_dump(mode="json"), "fingerprint": signature},
                    "diagnostic": diagnostic.model_dump(mode="json"),
                    "failed_node": node,
                    "material_fingerprint": material,
                    "last_failure_fingerprint": signature,
                    "failure_lineage_id": lineage,
                    "failure_material_revision": material_revision,
                    "receipt_ids": state.get("receipt_ids", []) + source_ids + [receipt.receipt_id],
                    "phase": "recovery",
                    "cursor": f"RECOVERY:{node}",
                    "next_action": f"apply {disposition.value} policy for {category.value}",
                    "progress_seq": state.get("progress_seq", 0) + 1,
                }
                self._projection(state, updates); self._event({**state, **updates}, node, {"result": "failure", "category": category.value, "fingerprint": signature})
                return updates
        return wrapped

    @staticmethod
    def route_after(next_node: str):
        return lambda state: "recover" if state.get("failure") else next_node

    def recover(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state); value = state.get("failure") or {}
        category = FailureCategory(value.get("category", FailureCategory.UNKNOWN.value)); signature = str(value.get("fingerprint", "")); target = str(state.get("failed_node") or "")
        diagnostic_value = state.get("diagnostic")
        diagnostic = Diagnostic.model_validate(diagnostic_value) if diagnostic_value else Diagnostic(
            code=f"{target}_{category.value}_failed".upper().replace("-", "_"),
            cause=category,
            disposition=disposition_for(category, retryable=bool(value.get("retryable", True)), node=target),
            responsible_party="orchestrator",
            affected_owner=value.get("owner"),
            summary=str(value.get("summary", "unrecoverable failure")),
            evidence=[
                item for item in value.get("evidence", [])
                if isinstance(item, dict) and item.get("sha256") and item.get("size") is not None
            ],
            material_fingerprint=state.get("material_fingerprint"),
            failure_fingerprint=signature or None,
        )
        attempts = dict(state.get("failure_attempts", {})); attempts[signature] = attempts.get(signature, 0) + 1
        budget = recovery_budget(category)
        evidence_path = (value.get("evidence") or [{}])[0].get("path", "execution/receipts/failure")
        if diagnostic.disposition in {
            FailureDisposition.WAITING_HUMAN,
            FailureDisposition.HARD_EXTERNAL_BLOCKER,
        }:
            blocker = Blocker(
                kind=category.value,
                summary=diagnostic.summary,
                evidence=evidence_path,
                needed=diagnostic.user_action_required or "resolve the evidenced external or product constraint",
            )
            updates = {"failure_attempts": attempts, "mode": RunMode.BLOCKED.value, "blocker": blocker.model_dump(), "cursor": f"BLOCKED:{target}", "next_action": "await the single required external change", "progress_seq": state["progress_seq"] + 1}
            self._projection(state, updates); self._event({**state, **updates}, "recover", {"result": "blocked", "fingerprint": signature, "attempts": attempts[signature]})
            return updates
        if diagnostic.disposition == FailureDisposition.INTERNAL_FAULT:
            blocker = Blocker(
                kind="internal_fault",
                summary=diagnostic.summary,
                evidence=evidence_path,
                needed="repair the Harness implementation or its typed adapter contract",
            )
            updates = {
                "failure_attempts": attempts,
                "mode": RunMode.FAULTED.value,
                "blocker": blocker.model_dump(),
                "cursor": f"FAULTED:{target}",
                "next_action": "repair the internal Harness fault",
                "progress_seq": state["progress_seq"] + 1,
            }
            self._projection(state, updates); self._event({**state, **updates}, "recover", {"result": "internal_fault", "fingerprint": signature})
            return updates
        ledger = json.loads(json.dumps(state.get("recovery_ledger", {})))
        lineage = str(state.get("failure_lineage_id") or failure_lineage_id(
            invariant_id=diagnostic.code,
            node=target,
            owner=diagnostic.affected_owner,
            test_id=diagnostic.test_id,
        ))
        material_revision = str(
            state.get("failure_material_revision")
            or self._owner_material_revision(
                project_dir, diagnostic.affected_owner, state
            )
        )
        admission = validate_recovery_admission(
            ledger,
            lineage_id=lineage,
            material_revision=material_revision,
            disposition=diagnostic.disposition.value,
            typed=diagnostic_value is not None,
            new_diagnostic_id=diagnostic.code,
        )
        if diagnostic.disposition in {
            FailureDisposition.RETRY_TRANSIENT,
            FailureDisposition.REPAIR_INTERNAL,
        } and not admission.admitted:
            blocker = Blocker(
                kind="internal_stall",
                summary=admission.reason,
                evidence=evidence_path,
                needed="produce a new typed diagnostic or relevant owner-material revision",
            )
            updates = {
                "failure_attempts": attempts,
                "recovery_ledger": ledger,
                "mode": RunMode.FAULTED.value,
                "blocker": blocker.model_dump(),
                "cursor": f"FAULTED:{target}:lineage-stalled",
                "next_action": "repair the typed failure without repeating model work",
                "progress_seq": state["progress_seq"] + 1,
            }
            self._projection(state, updates)
            self._event(
                {**state, **updates},
                "recover",
                {"result": "lineage_stall", "failure_lineage_id": lineage},
            )
            return updates
        if attempts[signature] > budget:
            blocker = Blocker(
                kind="internal_stall",
                summary=diagnostic.summary,
                evidence=evidence_path,
                needed="change the owning source, Harness adapter, or other material before resuming",
            )
            updates = {
                "failure_attempts": attempts,
                "mode": RunMode.FAULTED.value,
                "blocker": blocker.model_dump(),
                "cursor": f"FAULTED:{target}:stalled",
                "next_action": "repair the unchanged internal failure",
                "progress_seq": state["progress_seq"] + 1,
            }
            self._projection(state, updates); self._event({**state, **updates}, "recover", {"result": "internal_stall", "fingerprint": signature, "attempts": attempts[signature]})
            return updates
        receipt_ids: list[str] = []
        invalidated_image_index: int | None = None
        # A changed Harness fingerprint is itself material retry input.  Do
        # not send a stale adapter/workspace failure to an owner agent and
        # then reject that agent for leaving firmware source unchanged.
        # Node-created diagnostics predate the boundary normalizer in some
        # revisions and therefore may not carry their own material fingerprint.
        # The checkpoint still records the material observed at that boundary;
        # use it as the authoritative fallback so a Harness-only correction
        # retries the affected node instead of needlessly asking a firmware
        # owner to change already-correct source.
        observed_material = (
            diagnostic.material_fingerprint
            or state.get("material_fingerprint")
        )
        harness_material_changed = bool(
            observed_material
            and observed_material != material_fingerprint(project_dir, state)
        )
        if (
            diagnostic.disposition == FailureDisposition.REPAIR_INTERNAL
            and not harness_material_changed
        ):
            owner_value = diagnostic.affected_owner
            if not owner_value and target in {
                "integration",
                "release_smoke",
                "release",
                "closure",
            }:
                owner_value = self._integration_owner(state)
            repair_owners = [
                owner for owner in str(owner_value or "").split("+") if owner
            ]
            if len(repair_owners) != 1:
                blocker = Blocker(
                    kind="repair_owner_ambiguous",
                    summary=diagnostic.summary,
                    evidence=evidence_path,
                    needed="fix the Harness to identify exactly one owning repair transaction",
                )
                updates = {
                    "failure_attempts": attempts,
                    "recovery_ledger": ledger,
                    "mode": RunMode.FAULTED.value,
                    "blocker": blocker.model_dump(),
                    "cursor": f"FAULTED:{target}:owner",
                    "next_action": "repair typed owner attribution",
                    "progress_seq": state["progress_seq"] + 1,
                }
                self._projection(state, updates)
                return updates
            before = material_fingerprint(project_dir, state)
            last_result: Receipt | None = None
            for owner in repair_owners:
                result = AgentAdapter(
                    self.repo_root,
                    store,
                    state["run_id"],
                    timeout=1200,
                ).execute(AgentAction(
                    project=state["project"],
                    owner=owner,
                    contract_path=Path(state["design_dir"]) / "execution-contract.json",
                    requirement_path=self.repo_root / "requirements" / f"{state['project']}.md",
                    connection_path=self.repo_root / "connections" / f"{state['project']}.md",
                    instruction=(
                        f"Repair typed diagnostic {diagnostic.code}. "
                        f"Cause: {diagnostic.cause.value}. "
                        f"Observed failure: {diagnostic.summary}. "
                        "Make the smallest owning change and preserve the "
                        "approved contract. If a component selftest is not "
                        "reachable from main/, update only the necessary "
                        "product composition call as part of this owner repair."
                        + self._product_default_instruction(owner)
                    ),
                    implementation_addendum_path=(
                        self._implementation_addendum_path(state, owner)
                    ),
                ))
                last_result = result
                receipt_ids.append(result.receipt_id)
                if result.success:
                    continue
                blocker = Blocker(
                    kind="repair_agent_fault",
                    summary=(
                        result.failure.summary
                        if result.failure
                        else f"repair agent failed for {owner}"
                    ),
                    evidence=(
                        result.artifacts[0].path
                        if result.artifacts
                        else evidence_path
                    ),
                    needed=(
                        "repair the internal implementation-agent transaction"
                    ),
                )
                updates = {
                    "failure_attempts": attempts,
                    "recovery_ledger": ledger,
                    "receipt_ids": state.get("receipt_ids", []) + receipt_ids,
                    "mode": RunMode.FAULTED.value,
                    "blocker": blocker.model_dump(),
                    "cursor": f"FAULTED:{target}:repair-agent",
                    "next_action": "repair the internal implementation-agent transaction",
                    "progress_seq": state["progress_seq"] + 1,
                }
                self._projection(state, updates)
                return updates
            after = material_fingerprint(project_dir, state)
            if after == before:
                blocker = Blocker(
                    kind="repair_no_material_change",
                    summary=(
                        "repair agent completed for "
                        f"{'+'.join(repair_owners)} without changing retry material"
                    ),
                    evidence=(
                        last_result.artifacts[0].path
                        if last_result and last_result.artifacts
                        else evidence_path
                    ),
                    needed="produce an owning source or Harness change before retrying",
                )
                updates = {
                    "failure_attempts": attempts,
                    "recovery_ledger": ledger,
                    "receipt_ids": state.get("receipt_ids", []) + receipt_ids,
                    "mode": RunMode.FAULTED.value,
                    "blocker": blocker.model_dump(),
                    "cursor": f"FAULTED:{target}:no-change",
                    "next_action": "repair the unchanged internal failure",
                    "progress_seq": state["progress_seq"] + 1,
                }
                self._projection(state, updates)
                return updates
            contract = self._contract(state)
            dependencies = {
                str(item["id"]): list(map(str, item.get("dependencies", [])))
                for item in contract.get("subsystems", [])
                if item.get("id")
            }
            impacted: set[str] = set()
            for owner in repair_owners:
                impacted.update(affected_consumers(owner, dependencies))
            evidence_to_invalidate: list[str] = []
            for path in store.evidence.rglob("*.json"):
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if (
                    existing.get("run_id") == state["run_id"]
                    and existing.get("owner") in impacted
                    and existing.get("verdict") == "PASS"
                ):
                    evidence_to_invalidate.append(str(existing["evidence_id"]))
            if evidence_to_invalidate:
                from .corrections import record_evidence_correction

                correction = record_evidence_correction(
                    project_dir,
                    evidence_to_invalidate,
                    (
                        f"typed repair {diagnostic.code} changed relevant "
                        f"material for {sorted(impacted)}"
                    ),
                    corrected_by="orchestrator",
                )
                correction_receipt = self._pass_receipt(
                    state,
                    "recovery",
                    "impact_invalidation",
                    inputs={
                        "failure_lineage_id": lineage,
                        "material_revision": material_revision,
                    },
                    outputs=correction,
                    idempotency_key=digest({
                        "lineage": lineage,
                        "material": material_revision,
                        "evidence_ids": sorted(evidence_to_invalidate),
                    }),
                )
                receipt_ids.append(correction_receipt.receipt_id)
            affected_indexes = [
                index
                for index, image in enumerate(
                    state.get("verification_images", [])
                )
                if impacted.intersection(image.get("owners", []))
            ]
            if affected_indexes:
                invalidated_image_index = min(affected_indexes)
        elif diagnostic.disposition == FailureDisposition.RETRY_TRANSIENT:
            SerialAdapter(self.repo_root, store, state["run_id"]).stop()
            if state.get("hardware_identity"):
                from .models import HardwareIdentity
                session, receipt = HardwareAdapter(self.repo_root, store, state["run_id"]).refresh_session(HardwareIdentity.model_validate(state["hardware_identity"]), state.get("baud", 115200))
                receipt_ids.append(receipt.receipt_id)
                if session: state = {**state, "port": session.port}
        retry_target = (
            "integration"
            if (
                diagnostic.disposition == FailureDisposition.REPAIR_INTERNAL
                and target == "release"
            )
            else target
        )
        if invalidated_image_index is not None:
            retry_target = "implementation_materialize"
        updates = {"failure_attempts": attempts, "recovery_ledger": ledger, "failure": None, "diagnostic": None, "recovery_target": retry_target, "receipt_ids": state.get("receipt_ids", []) + receipt_ids, "phase": f"retry:{retry_target}", "cursor": f"RECOVERY:{retry_target}:attempt-{attempts[signature]}", "next_action": f"retry {retry_target} from checkpoint boundary", "progress_seq": state["progress_seq"] + 1}
        if invalidated_image_index is not None:
            updates["verification_image_index"] = invalidated_image_index
            updates["transaction"] = {}
        if state.get("port") != updates.get("port") and state.get("port"): updates["port"] = state["port"]
        self._projection(state, updates); self._event({**state, **updates}, "recover", {"result": "retry", "target": retry_target, "fingerprint": signature, "attempt": attempts[signature]})
        return updates

    def recovery_route(self, state: HarnessState) -> str:
        return "end" if state.get("mode") in {RunMode.BLOCKED.value, RunMode.FAULTED.value} else str(state.get("recovery_target") or state.get("failed_node") or "end")

    def initialize(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state)
        run_id = state.get("run_id") or f"run-{uuid.uuid4().hex[:16]}"
        design_dir = Path(state.get("design_dir") or project_dir / "design-package" / "rev-0001").resolve()
        updates = {"run_id": run_id, "design_dir": str(design_dir), "mode": RunMode.CONTINUOUS.value, "phase": "preflight", "cursor": "STAGE 0:initialize", "next_action": "run environment and hardware preflight", "progress_seq": 1, "receipt_ids": [], "evidence_ids": [], "subsystem_index": 0, "batch_index": 0, "verification_image_index": 0, "release_attempt": 0, "failure_attempts": {}, "recovery_ledger": {}, "invariant_passes": [], "transaction": {}, "all_implementations_materialized": False}
        report, hygiene_errors = validate_generated_file_hygiene(
            self.repo_root, project_dir, prune=True
        )
        if hygiene_errors:
            raise DiagnosticFailure(Diagnostic(
                code="PROJECT_HYGIENE_INVALID",
                cause=FailureCategory.STORAGE,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="harness",
                summary="; ".join(hygiene_errors),
            ))
        receipt = self._pass_receipt(
            {**state, **updates},
            "hygiene",
            "project_hygiene",
            inputs={"project": state["project"]},
            outputs={
                "free_bytes": report.free_bytes,
                "verification_roots": report.verification_roots,
                "verification_bytes": report.verification_bytes,
                "pruned_roots": list(report.pruned_roots),
            },
            idempotency_key=self._transaction_key(
                {**state, **updates}, "project_hygiene"
            ),
        )
        updates["receipt_ids"] = [receipt.receipt_id]
        updates["invariant_passes"] = ["HR-014-PROJECT-HYGIENE"]
        self._projection({**state, **updates}, {})
        self._event({**state, **updates}, "initialize", {"intent": state.get("intent", "new")})
        return updates

    def pause_control(self, state: HarnessState) -> dict:
        """A checkpoint-owned control node used only by the CLI pause transaction.

        It deliberately has no outgoing edge.  ``pause`` records the next safe
        graph node before scheduling this node, so no build/flash/serial side
        effect can be launched after an explicit user pause.
        """
        return {}

    def pause_updates(self, state: HarnessState, next_node: str, reason: str) -> dict:
        if not next_node:
            raise ValueError("pause requires a safe next graph node")
        updates = {
            "mode": RunMode.PAUSED.value,
            "pause_next_node": next_node,
            "pause_reason": reason,
            "cursor": f"PAUSED:before:{next_node}",
            "next_action": f"resume from {next_node}",
            "progress_seq": state.get("progress_seq", 0) + 1,
        }
        return updates

    def record_pause(self, state: HarnessState, updates: dict[str, Any]) -> None:
        """Project a pause only after LangGraph has committed its checkpoint."""
        self._projection(state, updates)
        self._event({**state, **updates}, "pause", {"next_node": updates["pause_next_node"], "reason": updates["pause_reason"]})

    def preflight(self, state: HarnessState) -> dict:
        _, store = self._context(state)
        if not (self.repo_root / "tools" / "idf.ps1").is_file():
            raise DiagnosticFailure(Diagnostic(
                code="IDF_WRAPPER_MISSING",
                cause=FailureCategory.ENVIRONMENT,
                disposition=FailureDisposition.HARD_EXTERNAL_BLOCKER,
                responsible_party="workspace_owner",
                summary=(
                    "tools/idf.ps1 is missing; ESP-IDF paths must not be guessed"
                ),
                user_action_required="restore the repository tools/idf.ps1 wrapper",
            ))
        idf = IdfAdapter(self.repo_root, store, state["run_id"])
        version = idf.version()
        if not version.success:
            raise ReceiptFailure(version)
        capabilities = CapabilityAdapter(self.repo_root, store, state["run_id"]).probe()
        if not capabilities.success:
            raise ReceiptFailure(capabilities)
        baseline = Path(state["preflight_baseline"]) if state.get("preflight_baseline") else None
        session, hardware = HardwareAdapter(self.repo_root, store, state["run_id"]).preflight(state.get("port"), state.get("baud", 115200), baseline)
        if not hardware.success or not session:
            if not hardware.success:
                raise ReceiptFailure(hardware)
            raise DiagnosticFailure(Diagnostic(
                code="HARDWARE_PREFLIGHT_SESSION_MISSING",
                cause=FailureCategory.HARDWARE,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="hardware_adapter",
                summary="successful hardware preflight returned no hardware session",
            ))
        updates = {"port": session.port, "baud": session.baud, "hardware_identity": session.identity.model_dump(), "receipt_ids": state.get("receipt_ids", []) + [version.receipt_id, capabilities.receipt_id, hardware.receipt_id], "phase": "design", "cursor": "STAGE 0:preflight:pass", "next_action": "validate design package", "progress_seq": state["progress_seq"] + 1}
        self._projection(state, updates); self._event({**state, **updates}, "preflight", {"port": session.port, "identity": session.identity.stable_key()})
        return updates

    def design(self, state: HarnessState) -> dict:
        contract, errors = validate_design_package(Path(state["design_dir"]), require_approval=False)
        if errors:
            raise DiagnosticFailure(Diagnostic(
                code="APPROVED_DESIGN_PACKAGE_INVALID",
                cause=FailureCategory.DATA_PATH,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="design_harness",
                summary="design package invalid: " + "; ".join(errors),
                retry_scope="design_revision",
            ))
        updates = {"subsystems": subsystem_order(contract), "verification_batches": verification_batches(contract), "design_digest": json.loads((Path(state["design_dir"]) / "manifest.json").read_text(encoding="utf-8"))["design_digest"], "phase": "approval", "cursor": "STAGE 1.3:design:validated", "next_action": "bind approval to design digest", "progress_seq": state["progress_seq"] + 1}
        self._projection(state, updates); return updates

    def approval(self, state: HarnessState) -> dict:
        approval_path = Path(state["design_dir"]) / "approval.json"
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        if approval.get("status") != "APPROVED" or approval.get("design_digest") != state["design_digest"]:
            waiting = {
                "mode": RunMode.WAITING_SPEC.value,
                "phase": "approval",
                "cursor": "STAGE 1.4:approval:waiting",
                "next_action": "review and approve the digest-bound specification",
                "progress_seq": state.get("progress_seq", 0) + 1,
            }
            self._projection(state, waiting)
            self._event(
                {**state, **waiting},
                "approval",
                {
                    "result": "waiting",
                    "design_digest": state["design_digest"],
                },
            )
            response = interrupt({"kind": "SPEC_APPROVAL", "spec": str(Path(state["design_dir"]) / "spec.md"), "design_digest": state["design_digest"], "required_response": "APPROVE or request changes"})
            if str(response).strip().upper() != "APPROVE":
                raise DiagnosticFailure(Diagnostic(
                    code="SPEC_APPROVAL_NOT_GRANTED",
                    cause=FailureCategory.LIMITATION,
                    disposition=FailureDisposition.HARD_EXTERNAL_BLOCKER,
                    responsible_party="product_owner",
                    summary="design approval was not granted",
                    user_action_required=(
                        "revise the user inputs/design and approve a new digest"
                    ),
                ))
            from .design_package import approve_revision
            approve_revision(Path(state["design_dir"]))
        _, errors = validate_design_package(Path(state["design_dir"]), require_approval=True)
        if errors:
            raise ValueError("approved package failed binding: " + "; ".join(errors))
        updates = {"phase": "bind", "cursor": "STAGE 1.5:approval:pass", "next_action": "bind approved design and refresh hardware session", "progress_seq": state["progress_seq"] + 1}
        self._projection(state, updates); return updates

    def bind(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state)
        from .models import HardwareIdentity
        session, refresh = HardwareAdapter(self.repo_root, store, state["run_id"]).refresh_session(HardwareIdentity.model_validate(state["hardware_identity"]), state["baud"])
        if not refresh.success:
            raise ReceiptFailure(refresh)
        if not session:
            raise DiagnosticFailure(Diagnostic(
                code="HARDWARE_REFRESH_SESSION_MISSING",
                cause=FailureCategory.HARDWARE,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="hardware_adapter",
                summary="successful hardware refresh returned no hardware session",
            ))
        record = RunRecord(run_id=state["run_id"], project=state["project"], intent=state.get("intent", "new"), design_revision=state.get("design_revision", 1), design_digest=state["design_digest"], hardware_identity=state["hardware_identity"])
        atomic_write_json(store.execution / "run.json", record)
        updates = {"port": session.port, "receipt_ids": state.get("receipt_ids", []) + [refresh.receipt_id], "phase": "verification_batches", "cursor": "STAGE 1.5:bound", "next_action": "normalize compatible verification images", "progress_seq": state["progress_seq"] + 1}
        self._projection(state, updates); return updates

    def invariant_gate(self, state: HarnessState) -> dict:
        registry = load_invariant_registry()
        errors = validate_invariant_registry(registry)
        errors.extend(validate_graph_conformance(build_graph(self.repo_root)))
        if errors:
            raise DiagnosticFailure(Diagnostic(
                code="INVARIANT_REGISTRY_INVALID",
                cause=FailureCategory.TOOL,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="harness",
                summary="; ".join(errors),
                retry_scope="harness",
            ))
        receipt = self._pass_receipt(
            state,
            "invariants",
            "graph_conformance",
            inputs={"registry_schema_version": registry["registry_schema_version"]},
            outputs={
                "rule_ids": sorted(rule["rule_id"] for rule in registry["rules"]),
                "design_digest": state["design_digest"],
            },
            idempotency_key=self._transaction_key(
                state, "graph_conformance"
            ),
        )
        updates = {
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "invariant_passes": state.get("invariant_passes", []) + [
                "HR-013-SIDE-EFFECT-TRANSACTIONS"
            ],
            "phase": "schema_gate",
            "cursor": "STAGE 1.5:invariants:pass",
            "next_action": "require current executable schema",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def schema_gate(self, state: HarnessState) -> dict:
        contract = self._contract(state)
        errors = require_executable_schema(contract)
        if errors:
            raise DiagnosticFailure(Diagnostic(
                code="DESIGN_REVISION_REQUIRED",
                cause=FailureCategory.LIMITATION,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="design_harness",
                summary="; ".join(errors),
                retry_scope="new_unapproved_design_revision",
            ))
        receipt = self._pass_receipt(
            state,
            "invariants",
            "schema_gate",
            inputs={
                "schema_version": contract["schema_version"],
                "design_digest": state["design_digest"],
            },
            outputs={"capabilities_complete": True},
            idempotency_key=self._transaction_key(
                state, "schema_gate",
                schema_version=contract["schema_version"],
            ),
        )
        updates = {
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "invariant_passes": state.get("invariant_passes", []) + [
                "HR-003-CURRENT-SCHEMA"
            ],
            "phase": "operation_authority",
            "cursor": "STAGE 1.5:schema:pass",
            "next_action": "compile typed operation authority",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def operation_authority(self, state: HarnessState) -> dict:
        _, store = self._context(state)
        available_receipts: set[str] = set()
        for path in store.receipts.rglob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if value.get("success") is True and value.get("receipt_id"):
                available_receipts.add(str(value["receipt_id"]))
        result = compile_operation_authority(
            self._contract(state),
            available_receipt_ids=available_receipts,
        )
        if not result.passed:
            raise DiagnosticFailure(Diagnostic(
                code="OPERATION_AUTHORITY_INCOMPLETE",
                cause=FailureCategory.DATASHEET,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="design_harness",
                summary="; ".join(result.errors),
                retry_scope="new_unapproved_design_revision",
            ))
        project_dir, store = self._context(state)
        addendum = {
            "schema_version": "1.0",
            "run_id": state["run_id"],
            "design_digest": state["design_digest"],
            "decisions": list(result.decisions),
        }
        addendum["authority_digest"] = digest(addendum)
        addendum_path = (
            store.execution / "operation-authority"
            / f"{addendum['authority_digest']}.json"
        )
        if addendum_path.exists():
            if json.loads(addendum_path.read_text(encoding="utf-8")) != addendum:
                raise ValueError("operation-authority addendum digest collision")
        else:
            atomic_write_json(addendum_path, addendum)
        artifact = file_ref(addendum_path, project_dir, "application/json")
        receipt = self._pass_receipt(
            state,
            "authority",
            "operation_authority",
            inputs={"design_digest": state["design_digest"]},
            outputs={
                "decisions": list(result.decisions),
                "authority_digest": addendum["authority_digest"],
            },
            artifacts=[artifact],
            idempotency_key=self._transaction_key(
                state, "operation_authority",
                authority_digest=addendum["authority_digest"],
            ),
        )
        updates = {
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "invariant_passes": state.get("invariant_passes", []) + [
                "HR-004-OPERATION-AUTHORITY"
            ],
            "phase": "bind",
            "cursor": "STAGE 1.6:operation-authority:pass",
            "next_action": "bind approved authority to the hardware session",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def verification_batches(self, state: HarnessState) -> dict:
        project_dir, _ = self._context(state)
        contract = self._contract(state)
        images = normalize_verification_images(contract, project_dir)
        serialized = [
            {
                "image_id": image.image_id,
                "owners": list(image.owners),
                "test_ids": list(image.test_ids),
                "setup": image.setup,
                "stimulus_adapter": image.stimulus_adapter,
                "hardware_resources": list(image.hardware_resources),
                "isolation_required": image.isolation_required,
            }
            for image in images
        ]
        receipt = self._pass_receipt(
            state,
            "verification-plan",
            "verification_plan",
            inputs={"design_digest": state["design_digest"]},
            outputs={
                "images": serialized,
                "counts": report_counts(contract, images),
            },
            idempotency_key=self._transaction_key(
                state, "verification_plan",
                image_ids=[item["image_id"] for item in serialized],
            ),
        )
        updates = {
            "verification_images": serialized,
            "verification_image_index": 0,
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "invariant_passes": state.get("invariant_passes", []) + [
                "HR-008-IMAGE-BATCHING"
            ],
            "phase": "implementation_materialize",
            "cursor": "STAGE 1.7:verification-plan:pass",
            "next_action": "materialize the next image implementation",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def _current_image(
        self, state: HarnessState,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        images = state.get("verification_images", [])
        index = int(state.get("verification_image_index", 0))
        if index >= len(images):
            return None, []
        image = images[index]
        tests = set(image["test_ids"])
        rows = [
            row for row in self._contract(state).get("verification", [])
            if row.get("test_id") in tests
        ]
        return image, rows

    def implementation_materialize(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state)
        image, rows = self._current_image(state)
        if image is None:
            return {
                "phase": "implementation_completeness",
                "cursor": "STAGE 2:verification-images:pass",
                "next_action": "validate all required operations globally",
                "progress_seq": state["progress_seq"] + 1,
            }
        receipt_ids: list[str] = []
        if not state.get("all_implementations_materialized"):
            all_rows = self._contract(state).get("verification", [])
            all_owners = [
                str(item["id"])
                for item in self._contract(state).get("subsystems", [])
                if item.get("id")
            ]
            for owner in all_owners:
                receipt_ids.extend(self._materialize_owner_source(
                    state, project_dir, store, owner,
                    [
                        row for row in all_rows
                        if row.get("owner") == owner
                    ],
                ))
        receipt = self._pass_receipt(
            state,
            "transactions",
            "implementation_materialize",
            inputs={
                "image_id": image["image_id"],
                "owners": image["owners"],
                "design_digest": state["design_digest"],
            },
            outputs={"materialized": True},
            idempotency_key=self._transaction_key(
                state, "implementation_materialize",
                image_id=image["image_id"],
                owners=image["owners"],
            ),
        )
        updates = {
            "transaction": {
                "scope": "verification",
                "image": image,
                "rows": rows,
                "receipt_ids": [receipt.receipt_id],
            },
            "all_implementations_materialized": True,
            "receipt_ids": state.get("receipt_ids", []) + receipt_ids + [receipt.receipt_id],
            "phase": "implementation_completeness",
            "cursor": f"STAGE 2:{image['image_id'][:12]}:materialized",
            "next_action": "validate all required operations globally",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def implementation_completeness(
        self, state: HarnessState,
    ) -> dict:
        project_dir, _ = self._context(state)
        contract = self._contract(state)
        errors = validate_implementation_completeness(project_dir, contract)
        if errors:
            raise DiagnosticFailure(Diagnostic(
                code="IMPLEMENTATION_COMPLETENESS_FAILED",
                cause=FailureCategory.API,
                disposition=FailureDisposition.REPAIR_INTERNAL,
                responsible_party="implementation_agent",
                summary="; ".join(errors),
                retry_scope="owner_and_consumers",
            ))
        receipt = self._pass_receipt(
            state,
            "transactions",
            "implementation_completeness",
            inputs={"design_digest": state["design_digest"]},
            outputs={"all_required_operations_complete": True},
            idempotency_key=self._transaction_key(
                state, "implementation_completeness"
            ),
        )
        transaction = dict(state.get("transaction") or {})
        if transaction:
            transaction.setdefault("receipt_ids", []).append(
                receipt.receipt_id
            )
        has_image = bool(transaction.get("image"))
        updates = {
            "transaction": transaction,
            "receipt_ids": state.get("receipt_ids", []) + [
                receipt.receipt_id
            ],
            "invariant_passes": state.get("invariant_passes", []) + [
                "HR-005-IMPLEMENTATION-COMPLETE"
            ],
            "phase": (
                "source_validate" if has_image else "component_architecture"
            ),
            "cursor": "STAGE 2:implementation-completeness:pass",
            "next_action": (
                "validate exact verification image source"
                if has_image else "validate component architecture"
            ),
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def source_validate(self, state: HarnessState) -> dict:
        project_dir, _ = self._context(state)
        transaction = dict(state["transaction"])
        owners = set(transaction["image"]["owners"])
        contract = self._contract(state)
        for owner in sorted(owners):
            addenda, addendum_errors = self._validated_implementation_addenda(
                state, {owner}
            )
            owner_contract = {
                **contract,
                "operations": [
                    item for item in contract.get("operations", [])
                    if item.get("owner") == owner
                ],
            }
            errors = (
                addendum_errors
                + validate_source_facts(
                    project_dir,
                    contract,
                    {owner},
                    additional_facts=[
                        fact for addendum in addenda
                        for fact in addendum.get("implementation_facts", [])
                    ],
                )
                + validate_implementation_completeness(
                    project_dir, owner_contract
                )
            )
            if errors:
                raise DiagnosticFailure(Diagnostic(
                    code="IMPLEMENTATION_COMPLETENESS_FAILED",
                    cause=FailureCategory.API,
                    disposition=FailureDisposition.REPAIR_INTERNAL,
                    responsible_party="implementation_agent",
                    affected_owner=owner,
                    subsystem_id=owner,
                    summary="; ".join(errors),
                    retry_scope="owner_and_consumers",
                ))
        receipt = self._pass_receipt(
            state,
            "transactions",
            "source_validation",
            inputs={"image_id": transaction["image"]["image_id"]},
            outputs={"operation_completeness": True},
            idempotency_key=self._transaction_key(
                state, "source_validation",
                image_id=transaction["image"]["image_id"],
            ),
        )
        transaction["receipt_ids"].append(receipt.receipt_id)
        updates = {
            "transaction": transaction,
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "invariant_passes": state.get("invariant_passes", []),
            "phase": "configure",
            "cursor": "STAGE 2:source:validated",
            "next_action": "configure exact verification image",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def configure(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state)
        transaction = dict(state["transaction"])
        scope = str(transaction.get("scope") or "verification")
        image = transaction["image"]
        setup = image["setup"]
        idf = IdfAdapter(self.repo_root, store, state["run_id"])
        verify_dir = self._verification_workspace(
            project_dir, state, image["image_id"][:12]
        )
        verify_dir.mkdir(parents=True, exist_ok=True)
        build_dir = verify_dir / "build"
        sdkconfig = verify_dir / "sdkconfig"
        baseline = project_dir / "sdkconfig"
        receipt_ids: list[str] = []
        if scope == "integration":
            if not baseline.is_file():
                raise DiagnosticFailure(Diagnostic(
                    code="INTEGRATION_BASELINE_CONFIG_MISSING",
                    cause=FailureCategory.TOOL,
                    disposition=FailureDisposition.INTERNAL_FAULT,
                    responsible_party="harness",
                    summary="production-path Integration requires an sdkconfig baseline",
                ))
            symbol = self._release_selftest_symbol(self._contract(state))
            defaults = verify_dir / "sdkconfig.integration.defaults"
            defaults.write_text(
                "# Generated production-path Integration configuration.\n"
                f"{symbol}=n\n",
                encoding="utf-8",
            )
            receipt = idf.configure_isolated(
                project_dir, build_dir, sdkconfig, [baseline, defaults],
                "integration_configure",
                idempotency_key=self._transaction_key(
                    state, "integration_configure",
                    image_id=image["image_id"],
                ),
            )
        elif setup.get("kind") == "firmware_selftest":
            if not baseline.is_file():
                raise DiagnosticFailure(Diagnostic(
                    code="VERIFICATION_BASELINE_CONFIG_MISSING",
                    cause=FailureCategory.TOOL,
                    disposition=FailureDisposition.INTERNAL_FAULT,
                    responsible_party="harness",
                    summary="isolated verification requires an existing sdkconfig baseline",
                ))
            defaults = verify_dir / "sdkconfig.verification.defaults"
            defaults.write_text(
                "# Generated verification configuration.\n"
                + "".join(
                    f"{symbol}={value}\n"
                    for symbol, value in sorted(
                        (setup.get("kconfig_overrides") or {}).items()
                    )
                ),
                encoding="utf-8",
            )
            receipt = idf.configure_isolated(
                project_dir, build_dir, sdkconfig, [baseline, defaults],
                f"{scope}_configure",
                idempotency_key=self._transaction_key(
                    state, f"{scope}_configure",
                    image_id=image["image_id"],
                ),
            )
        else:
            receipt = idf.set_target(
                project_dir,
                state.get("target", "esp32"),
                operation=f"{scope}_configure",
                idempotency_key=self._transaction_key(
                    state, f"{scope}_configure",
                    image_id=image["image_id"],
                ),
            )
            build_dir = project_dir / "build"
        receipt_ids.append(receipt.receipt_id)
        if not receipt.success:
            raise ReceiptFailure(receipt)
        transaction.update({
            "build_dir": str(build_dir),
            "workspace": str(verify_dir),
            "receipt_ids": transaction["receipt_ids"] + receipt_ids,
        })
        updates = {
            "transaction": transaction,
            "receipt_ids": state.get("receipt_ids", []) + receipt_ids,
            "phase": "build",
            "cursor": "STAGE 2:configured",
            "next_action": "build exact verification image",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def build(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state)
        transaction = dict(state["transaction"])
        scope = str(transaction.get("scope") or "verification")
        build_dir = Path(transaction["build_dir"])
        receipt = IdfAdapter(self.repo_root, store, state["run_id"]).build(
            project_dir,
            None if build_dir == project_dir / "build" else build_dir,
            f"{scope}_build",
            idempotency_key=self._transaction_key(
                state, f"{scope}_build",
                image_id=transaction["image"]["image_id"],
            ),
        )
        if not receipt.success:
            raise ReceiptFailure(receipt)
        if schema_has(
            str(self._contract(state).get("schema_version")),
            "typed_operations",
        ):
            contract = self._contract(state)
            for owner in transaction["image"]["owners"]:
                owner_contract = {
                    **contract,
                    "operations": [
                        item for item in contract.get("operations", [])
                        if item.get("owner") == owner
                    ],
                }
                link_errors = validate_linked_operations(
                    build_dir, owner_contract
                )
                if link_errors:
                    raise DiagnosticFailure(Diagnostic(
                        code="IMPLEMENTATION_LINK_COMPLETENESS_FAILED",
                        cause=FailureCategory.LINK,
                        disposition=FailureDisposition.REPAIR_INTERNAL,
                        responsible_party="implementation_agent",
                        affected_owner=owner,
                        subsystem_id=owner,
                        summary="; ".join(link_errors),
                        retry_scope="owner_and_consumers",
                    ))
        firmware_hash = IdfAdapter(
            self.repo_root, store, state["run_id"]
        ).firmware_hash(build_dir if build_dir != project_dir / "build" else project_dir)
        transaction.update({
            "firmware_sha256": firmware_hash,
            "receipt_ids": transaction["receipt_ids"] + [receipt.receipt_id],
            "build_receipt_id": receipt.receipt_id,
        })
        updates = {
            "transaction": transaction,
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "phase": "flash",
            "cursor": "STAGE 2:built",
            "next_action": "flash the bound firmware image",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def flash(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state)
        transaction = dict(state["transaction"])
        scope = str(transaction.get("scope") or "verification")
        build_dir = Path(transaction["build_dir"])
        SerialAdapter(self.repo_root, store, state["run_id"]).stop()
        receipt = IdfAdapter(self.repo_root, store, state["run_id"]).flash(
            project_dir,
            state["port"],
            None if build_dir == project_dir / "build" else build_dir,
            f"{scope}_flash",
            idempotency_key=self._transaction_key(
                state, f"{scope}_flash",
                image_id=transaction["image"]["image_id"],
                firmware_sha256=transaction["firmware_sha256"],
                port=state["port"],
            ),
        )
        if not receipt.success:
            raise ReceiptFailure(receipt)
        transaction.update({
            "receipt_ids": transaction["receipt_ids"] + [receipt.receipt_id],
            "flash_receipt_id": receipt.receipt_id,
        })
        updates = {
            "transaction": transaction,
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "phase": "observe",
            "cursor": "STAGE 2:flashed",
            "next_action": "acquire raw serial observation",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def observe(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state)
        transaction = dict(state["transaction"])
        scope = str(transaction.get("scope") or "verification")
        rows = transaction["rows"]
        marker = self._serial_completion_marker(rows)
        receipt = SerialAdapter(
            self.repo_root, store, state["run_id"]
        ).capture_boot(
            state["port"],
            state["baud"],
            self._serial_timeout(rows, transaction["image"]["setup"]),
            marker,
            operation=f"{scope}_observe",
            idempotency_key=self._transaction_key(
                state, f"{scope}_observe",
                image_id=transaction["image"]["image_id"],
                firmware_sha256=transaction["firmware_sha256"],
                marker=marker,
            ),
        )
        if not receipt.success:
            raise ReceiptFailure(receipt)
        transaction.update({
            "receipt_ids": transaction["receipt_ids"] + [receipt.receipt_id],
            "observation_receipt_id": receipt.receipt_id,
            "observation_artifact": receipt.artifacts[0].model_dump(mode="json"),
        })
        updates = {
            "transaction": transaction,
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "phase": "evaluate",
            "cursor": "STAGE 2:observed",
            "next_action": "evaluate observation against frozen expectations",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def evaluate(self, state: HarnessState) -> dict:
        project_dir, _ = self._context(state)
        transaction = dict(state["transaction"])
        artifact = ArtifactRef.model_validate(transaction["observation_artifact"])
        text = (project_dir / artifact.path).read_text(
            encoding="utf-8", errors="replace"
        )
        evaluations: list[dict[str, Any]] = []
        reasons: list[str] = []
        for row in transaction["rows"]:
            passed, actual, row_reasons = evaluate_text(text, row["expected"])
            evaluations.append({
                "test_id": row["test_id"],
                "passed": passed,
                "actual": actual,
                "reasons": row_reasons,
            })
            reasons.extend(row_reasons if not passed else [])
        if transaction.get("scope") == "integration":
            observations = parse_runtime_observations(text)
            composition_errors = validate_production_composition(
                project_dir,
                self._contract(state),
                runtime_observations=observations,
            )
            for row in transaction["rows"]:
                if row.get("row_kind") != "integration_test":
                    continue
                selected = set(map(str, row.get("runtime_step_ids", [])))
                observed = {
                    str(item["runtime_step_id"])
                    for item in observations if item.get("runtime_step_id")
                }
                missing = sorted(selected - observed)
                if missing:
                    composition_errors.append(
                        f"integration test {row['test_id']!r} lacks "
                        f"correlated observations for {missing}"
                    )
            reasons.extend(composition_errors)
            transaction["runtime_observations"] = observations
        receipt = self._pass_receipt(
            state,
            "transactions",
            f"{transaction.get('scope', 'verification')}_evaluation",
            inputs={
                "observation_receipt_id": transaction["observation_receipt_id"],
                "test_ids": [row["test_id"] for row in transaction["rows"]],
            },
            outputs={"evaluations": evaluations, "passed": not reasons},
            idempotency_key=self._transaction_key(
                state,
                f"{transaction.get('scope', 'verification')}_evaluation",
                observation_receipt_id=transaction["observation_receipt_id"],
                test_ids=sorted(row["test_id"] for row in transaction["rows"]),
            ),
        )
        if reasons:
            first_failure = next(
                item for item in evaluations if not item["passed"]
            )
            failed_row = next(
                row for row in transaction["rows"]
                if row["test_id"] == first_failure["test_id"]
            )
            raise DiagnosticFailure(Diagnostic(
                code="VERIFICATION_EXPECTATION_FAILED",
                cause=FailureCategory.DATA_PATH,
                disposition=FailureDisposition.REPAIR_INTERNAL,
                responsible_party="implementation_agent",
                affected_owner=failed_row["owner"],
                subsystem_id=failed_row["owner"],
                test_id=failed_row["test_id"],
                summary="; ".join(first_failure["reasons"]),
                evidence=[artifact],
                retry_scope="owner_and_consumers",
            ))
        transaction.update({
            "receipt_ids": transaction["receipt_ids"] + [receipt.receipt_id],
            "evaluation_receipt_id": receipt.receipt_id,
            "evaluations": evaluations,
        })
        updates = {
            "transaction": transaction,
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "phase": "evidence_commit",
            "cursor": "STAGE 2:evaluated",
            "next_action": "commit independent Evidence rows",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def evidence_commit(self, state: HarnessState) -> dict:
        _, store = self._context(state)
        transaction = dict(state["transaction"])
        evaluation_by_test = {
            item["test_id"]: item for item in transaction["evaluations"]
        }
        evidence_ids: list[str] = []
        upstream = [
            transaction["build_receipt_id"],
            transaction["flash_receipt_id"],
            transaction["observation_receipt_id"],
            transaction["evaluation_receipt_id"],
        ]
        hardware_key = "|".join(
            str(state["hardware_identity"].get(key) or "")
            for key in ("chip", "mac", "usb_serial", "board_profile")
        )
        for row in transaction["rows"]:
            actual = evaluation_by_test[row["test_id"]]["actual"]
            requirement_ids = list(
                row.get("requirement_ids")
                or [row.get("requirement_id")]
            )
            existing_evidence_id = self._equivalent_evidence_id(
                store,
                run_id=state["run_id"],
                design_digest=state["design_digest"],
                test_id=row["test_id"],
                firmware_sha256=transaction["firmware_sha256"],
                receipt_ids=upstream,
            )
            if existing_evidence_id:
                evidence_ids.append(existing_evidence_id)
                continue
            evidence = Evidence(
                evidence_id=store.new_id("evidence"),
                run_id=state["run_id"],
                design_digest=state["design_digest"],
                requirement_ids=requirement_ids,
                test_id=row["test_id"],
                owner=row["owner"],
                tier=Tier(row["tier"]),
                expected=row["expected"],
                actual=actual,
                verdict=Verdict.PASS,
                receipt_ids=upstream,
                evidence_kinds=self._evidence_kinds(
                    row,
                    {
                        "build_receipt", "flash_receipt", "serial_log",
                        "firmware_hash", "hardware_identity",
                    },
                ),
                firmware_sha256=transaction["firmware_sha256"],
                hardware_identity_key=hardware_key,
            )
            store.write_evidence(
                evidence,
                "integration"
                if transaction.get("scope") == "integration"
                else "subsystem",
            )
            evidence_ids.append(evidence.evidence_id)
        producer_receipt_ids: list[str] = []
        if transaction.get("scope") == "integration":
            contract = self._contract(state)
            for item in contract.get("tier_c", []):
                if (
                    (item.get("artifact_contract") or {}).get("access_method")
                    == "physical_observation"
                ):
                    continue
                producer_test_id = str(item.get("producer_test_id") or "")
                evaluation = evaluation_by_test.get(producer_test_id)
                if not evaluation:
                    continue
                for kind in item.get("producer_receipt_kinds", []):
                    receipt = self._pass_receipt(
                        state,
                        "tier-c-producer",
                        str(kind),
                        inputs={
                            "source_observation_receipt_id": transaction[
                                "observation_receipt_id"
                            ],
                            "producer_operation_ids": item.get(
                                "producer_operation_ids", []
                            ),
                        },
                        outputs={
                            "producer_test_id": producer_test_id,
                            "correlation_key": item.get("correlation_key"),
                            "design_digest": state["design_digest"],
                            "firmware_sha256": transaction["firmware_sha256"],
                            "runtime_observations": transaction.get(
                                "runtime_observations", []
                            ),
                            "actual": evaluation["actual"],
                        },
                        idempotency_key=self._transaction_key(
                            state,
                            f"tier_c_producer:{kind}",
                            item_id=item["id"],
                            firmware_sha256=transaction["firmware_sha256"],
                            observation_receipt_id=transaction[
                                "observation_receipt_id"
                            ],
                        ),
                    )
                    producer_receipt_ids.append(receipt.receipt_id)
        receipt = self._pass_receipt(
            state,
            "transactions",
            f"{transaction.get('scope', 'verification')}_evidence_commit",
            inputs={
                "evaluation_receipt_id": transaction["evaluation_receipt_id"],
                "upstream_receipt_ids": upstream,
            },
            outputs={"evidence_ids": evidence_ids},
            idempotency_key=self._transaction_key(
                state,
                f"{transaction.get('scope', 'verification')}_evidence_commit",
                firmware_sha256=transaction["firmware_sha256"],
                evaluation_receipt_id=transaction["evaluation_receipt_id"],
            ),
        )
        scope = str(transaction.get("scope") or "verification")
        next_index = (
            int(state.get("verification_image_index", 0)) + 1
            if scope == "verification"
            else int(state.get("verification_image_index", 0))
        )
        updates = {
            "verification_image_index": next_index,
            "transaction": {},
            "completed_transaction_scope": scope,
            "receipt_ids": (
                state.get("receipt_ids", [])
                + producer_receipt_ids
                + [receipt.receipt_id]
            ),
            "evidence_ids": state.get("evidence_ids", []) + evidence_ids,
            "invariant_passes": state.get("invariant_passes", []) + [
                "HR-013-SIDE-EFFECT-TRANSACTIONS",
                *(
                    ["HR-006-PRODUCTION-COMPOSITION"]
                    if scope == "integration" else []
                ),
            ],
            "phase": "implementation_materialize",
            "cursor": "STAGE 2:evidence:committed",
            "next_action": (
                "materialize next verification image"
                if scope == "verification"
                and next_index < len(state.get("verification_images", []))
                else (
                    "validate component architecture"
                    if scope == "verification"
                    else "materialize Tier C artifacts"
                )
            ),
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def component_architecture(self, state: HarnessState) -> dict:
        project_dir, _ = self._context(state)
        errors = validate_component_architecture(project_dir, self._contract(state))
        if errors:
            raise DiagnosticFailure(Diagnostic(
                code="COMPONENT_ARCHITECTURE_INVALID",
                cause=FailureCategory.API,
                disposition=FailureDisposition.REPAIR_INTERNAL,
                responsible_party="implementation_agent",
                summary="; ".join(errors),
                retry_scope="architecture_and_consumers",
            ))
        receipt = self._pass_receipt(
            state,
            "architecture",
            "component_architecture",
            inputs={"design_digest": state["design_digest"]},
            outputs={"resolved_dependencies_match": True},
            idempotency_key=self._transaction_key(
                state, "component_architecture"
            ),
        )
        updates = {
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "invariant_passes": state.get("invariant_passes", []) + [
                "HR-010-COMPONENT-ARCHITECTURE"
            ],
            "phase": "production_composition",
            "cursor": "STAGE 2.5:component-architecture:pass",
            "next_action": "prove production entrypoint composition",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def production_composition(self, state: HarnessState) -> dict:
        project_dir, _ = self._context(state)
        errors = validate_production_composition(project_dir, self._contract(state))
        if errors:
            raise DiagnosticFailure(Diagnostic(
                code="PRODUCTION_COMPOSITION_INVALID",
                cause=FailureCategory.INTEGRATION,
                disposition=FailureDisposition.REPAIR_INTERNAL,
                responsible_party="implementation_agent",
                affected_owner=self._integration_owner(state),
                summary="; ".join(errors),
                retry_scope="production_orchestrator_and_consumers",
            ))
        receipt = self._pass_receipt(
            state,
            "architecture",
            "production_composition",
            inputs={"design_digest": state["design_digest"]},
            outputs={"selftest_off_reachability": True},
            idempotency_key=self._transaction_key(
                state, "production_composition"
            ),
        )
        updates = {
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "invariant_passes": state.get("invariant_passes", []),
            "phase": "integration",
            "cursor": "STAGE 2.6:production-composition:pass",
            "next_action": "run controlled production-path integration",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def integration_prepare(self, state: HarnessState) -> dict:
        project_dir, _ = self._context(state)
        contract = self._contract(state)
        tests = contract.get("integration", {}).get("tests") or []
        if not tests:
            raise DiagnosticFailure(Diagnostic(
                code="INTEGRATION_PLAN_EMPTY",
                cause=FailureCategory.INTEGRATION,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="design_harness",
                summary="integration test plan is empty",
            ))
        setup = tests[0].get("setup") or {"kind": "normal_boot"}
        if any((item.get("setup") or {"kind": "normal_boot"}) != setup for item in tests):
            raise DiagnosticFailure(Diagnostic(
                code="INTEGRATION_MIXED_IMAGES",
                cause=FailureCategory.INTEGRATION,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="design_harness",
                summary="integration tests mix setup images",
            ))
        if setup.get("kind") == "firmware_selftest":
            raise DiagnosticFailure(Diagnostic(
                code="INTEGRATION_PARALLEL_SELFTEST_FORBIDDEN",
                cause=FailureCategory.INTEGRATION,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="design_harness",
                summary="current Integration must enter through the normal production orchestrator",
            ))
        owner = self._integration_owner(state)
        rows: list[dict[str, Any]] = []
        for item in tests:
            test_id = str(item.get("test_id") or item.get("id") or "")
            expected = item.get("expected") or item.get("metrics")
            if not test_id or not expected:
                raise DiagnosticFailure(Diagnostic(
                    code="INTEGRATION_TEST_INCOMPLETE",
                    cause=FailureCategory.INTEGRATION,
                    disposition=FailureDisposition.INTERNAL_FAULT,
                    responsible_party="design_harness",
                    summary="integration tests require test_id and expected",
                ))
            rows.append({
                **item,
                "row_kind": "integration_test",
                "test_id": test_id,
                "owner": owner,
                "tier": "B",
                "expected": expected,
                "requirement_ids": item.get("requirement_ids") or [
                    req["id"] for req in contract["requirements"]
                ],
            })
        for row in contract.get("verification", []):
            if row.get("owner") in {owner, "integration"} and row.get("tier") in {"A", "B"}:
                rows.append({
                    **row,
                    "row_kind": "verification",
                    "requirement_ids": [row["requirement_id"]],
                })
        image_id = digest({
            "schema": "integration-image-v1",
            "design_digest": state["design_digest"],
            "setup": setup,
            "runtime_step_ids": sorted({
                str(step) for item in tests
                for step in item.get("runtime_step_ids", [])
            }),
        })
        receipt = self._pass_receipt(
            state,
            "transactions",
            "integration_prepare",
            inputs={"design_digest": state["design_digest"]},
            outputs={
                "image_id": image_id,
                "test_ids": [row["test_id"] for row in rows],
            },
            idempotency_key=self._transaction_key(
                state, "integration_prepare", image_id=image_id
            ),
        )
        transaction = {
            "scope": "integration",
            "image": {
                "image_id": image_id,
                "owners": [owner],
                "test_ids": [row["test_id"] for row in rows],
                "setup": setup,
                "stimulus_adapter": "production_seam",
                "hardware_resources": [],
                "isolation_required": False,
            },
            "rows": rows,
            "receipt_ids": [receipt.receipt_id],
        }
        updates = {
            "transaction": transaction,
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "phase": "configure",
            "cursor": "STAGE 2.7:integration:prepared",
            "next_action": "configure controlled production-path Integration",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def integration_configure(self, state: HarnessState) -> dict:
        return self.configure(state)

    def integration_build(self, state: HarnessState) -> dict:
        return self.build(state)

    def integration_flash(self, state: HarnessState) -> dict:
        return self.flash(state)

    def integration_observe(self, state: HarnessState) -> dict:
        return self.observe(state)

    def integration_evaluate(self, state: HarnessState) -> dict:
        return self.evaluate(state)

    def integration_evidence_commit(self, state: HarnessState) -> dict:
        return self.evidence_commit(state)

    def readiness(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state)
        batches = state.get("verification_batches") or [
            [owner] for owner in state["subsystems"]
        ]
        index = state.get("batch_index", 0)
        if index >= len(batches):
            return {
                "phase": "integration",
                "cursor": "STAGE 1.6:readiness:pass",
                "next_action": "run integration plan",
                "progress_seq": state["progress_seq"] + 1,
            }
        owners = batches[index]
        receipt_ids: list[str] = []
        for owner in owners:
            _, produced = self._ensure_implementation_readiness(
                state, project_dir, store, owner
            )
            receipt_ids.extend(produced)
        updates = {
            "receipt_ids": state.get("receipt_ids", []) + receipt_ids,
            "phase": "subsystems",
            "cursor": f"STAGE 1.6:{'+'.join(owners)}:readiness-pass",
            "next_action": "materialize and verify the ready implementation batch",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def subsystem(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state); contract = self._contract(state)
        batches = state.get("verification_batches") or [[owner] for owner in state["subsystems"]]
        index = state.get("batch_index", 0)
        if index >= len(batches):
            return {"phase": "integration", "cursor": "STAGE 2:subsystems:pass", "next_action": "run integration plan", "progress_seq": state["progress_seq"] + 1}
        owners = batches[index]
        all_rows = [
            row for owner in owners for row in expected_for_owner(contract, owner)
            if row.get("tier") in {"A", "B"}
        ]
        # Legacy 1.2 contracts may contain several isolated selftest setups
        # for one owner.  A setup is an image boundary, not an implementation
        # boundary: execute each frozen setup serially, preserving one build /
        # flash / serial evidence set per setup instead of rejecting a valid
        # owner after its source was materialized.
        setup_groups: list[list[dict]] = []
        for row in all_rows:
            setup_key = json.dumps(
                row.get("test_setup") or {"kind": "normal_boot"}, sort_keys=True
            )
            group = next(
                (candidate for candidate in setup_groups if json.dumps(
                    candidate[0].get("test_setup") or {"kind": "normal_boot"},
                    sort_keys=True,
                ) == setup_key),
                None,
            )
            if group is None:
                group = []; setup_groups.append(group)
            group.append(row)
        setup_index = int(state.get("verification_setup_index", 0))
        if setup_index >= len(setup_groups) and setup_groups:
            raise ValueError("verification setup cursor exceeds frozen setup groups")
        rows = setup_groups[setup_index] if setup_groups else []
        if not rows:
            implementation_receipts: list[str] = []
            for owner in owners:
                implementation_receipts.extend(self._materialize_owner_source(
                    state, project_dir, store, owner, [],
                ))
            updates = {"batch_index": index + 1, "subsystem_index": state.get("subsystem_index", 0) + len(owners), "receipt_ids": state.get("receipt_ids", []) + implementation_receipts, "cursor": f"STAGE 2:{'+'.join(owners)}:bootstrap-pass", "next_action": ("execute next verification batch" if index + 1 < len(batches) else "run integration plan"), "progress_seq": state["progress_seq"] + 1}
            self._projection(state, updates)
            return updates
        implementation_receipts: list[str] = []
        for owner in owners:
            implementation_receipts.extend(self._materialize_owner_source(
                state, project_dir, store, owner,
                [row for row in all_rows if row.get("owner") == owner],
            ))
        addenda, addendum_errors = self._validated_implementation_addenda(
            state, set(owners)
        )
        source_errors = addendum_errors + validate_runtime_flow_source(
            project_dir, contract
        ) + validate_source_facts(
            project_dir,
            contract,
            set(owners),
            additional_facts=[
                fact
                for addendum in addenda
                for fact in addendum.get("implementation_facts", [])
            ],
        )
        if source_errors:
            raise ValueError("source/contract consistency failed before hardware verification: " + "; ".join(source_errors))
        idf = IdfAdapter(self.repo_root, store, state["run_id"])
        target_receipts: list[str] = []
        if not (project_dir / "sdkconfig").is_file():
            target = idf.set_target(
                project_dir,
                state.get("target", "esp32"),
                idempotency_key=self._transaction_key(
                    state, "set_target", target=state.get("target", "esp32")
                ),
            )
            if not target.success:
                raise ReceiptFailure(target)
            target_receipts.append(target.receipt_id)
        attempt_receipts: list[str] = []
        batch = "+".join(owners)
        setup = self._verification_setup(rows)
        build_dir: Path | None = None
        workspace_scope = "project-build"
        if setup.get("kind") == "firmware_selftest":
            baseline = project_dir / "sdkconfig"
            if not baseline.is_file():
                raise ValueError("firmware_selftest requires an existing ESP-IDF sdkconfig baseline")
            overrides = setup.get("kconfig_overrides") or {}
            missing_kconfig = self._missing_project_kconfig_overrides(
                project_dir, overrides
            )
            if missing_kconfig:
                raise DiagnosticFailure(Diagnostic(
                    code="PROJECT_KCONFIG_OVERRIDE_UNDECLARED",
                    cause=FailureCategory.API,
                    disposition=FailureDisposition.REPAIR_INTERNAL,
                    responsible_party="implementation_agent",
                    affected_owner=owners[0],
                    subsystem_id=owners[0],
                    summary=(
                        "verification Kconfig override is not declared by "
                        f"project source: {missing_kconfig}"
                    ),
                    retry_scope="owner_and_consumers",
                ))
            verify_dir = self._verification_workspace(project_dir, state, batch)
            verify_dir.mkdir(parents=True, exist_ok=True)
            build_dir = verify_dir / "build"
            workspace_scope = build_dir.relative_to(project_dir).as_posix()
            sdkconfig = verify_dir / "sdkconfig"
            defaults = verify_dir / "sdkconfig.verification.defaults"
            defaults.write_text(
                "# Generated by the execution harness for an isolated verification build.\n"
                + "".join(f"{symbol}={value}\n" for symbol, value in sorted(overrides.items())),
                encoding="utf-8",
            )
            configure = idf.configure_isolated(
                project_dir, build_dir, sdkconfig, [baseline, defaults],
                "verification_reconfigure",
                idempotency_key=self._transaction_key(
                    state, "verification_configure", batch=batch,
                    setup=json.dumps(setup, sort_keys=True),
                    workspace=workspace_scope,
                ),
            )
            attempt_receipts.append(configure.receipt_id)
            if not configure.success:
                raise ReceiptFailure(configure)
        build_key = self._transaction_key(
            state, "subsystem_build", batch=batch,
            setup=json.dumps(setup, sort_keys=True), workspace=workspace_scope,
        )
        build = idf.build(
            project_dir, build_dir, "verification_build" if build_dir else "build",
            idempotency_key=build_key
        ); attempt_receipts.append(build.receipt_id)
        if not build.success:
            raise ReceiptFailure(build)
        serial_adapter = SerialAdapter(
            self.repo_root, store, state["run_id"]
        )
        serial_adapter.stop()
        firmware_hash = idf.firmware_hash(build_dir or project_dir)
        flash = idf.flash(
            project_dir,
            state["port"],
            build_dir,
            "verification_flash" if build_dir else "flash",
            idempotency_key=self._transaction_key(
                state,
                "subsystem_flash",
                batch=batch,
                setup=json.dumps(setup, sort_keys=True),
                workspace=workspace_scope,
                firmware_sha256=firmware_hash,
                port=state["port"],
            ),
        ); attempt_receipts.append(flash.receipt_id)
        if not flash.success:
            raise ReceiptFailure(flash)
        marker = self._serial_completion_marker(rows)
        serial = serial_adapter.capture_boot(
            state["port"],
            state["baud"],
            self._serial_timeout(rows, setup),
            marker,
            idempotency_key=self._transaction_key(
                state,
                "subsystem_serial",
                    batch=batch,
                    firmware_sha256=firmware_hash,
                    marker=marker,
                    port=state["port"],
                    baud=state["baud"],
            ),
        ); attempt_receipts.append(serial.receipt_id)
        text = (project_dir / serial.artifacts[0].path).read_text(
            encoding="utf-8", errors="replace"
        )
        evaluations = [
            (row, *evaluate_text(text, row["expected"])) for row in rows
        ]
        if not serial.success:
            raise ReceiptFailure(serial)
        if not all(passed for _, passed, _, _ in evaluations):
            reasons = [
                reason
                for _, passed, _, row_reasons in evaluations
                if not passed
                for reason in row_reasons
            ]
            raise DiagnosticFailure(Diagnostic(
                code="SUBSYSTEM_EXPECTATION_FAILED",
                cause=FailureCategory.DATA_PATH,
                disposition=FailureDisposition.REPAIR_INTERNAL,
                responsible_party="implementation_agent",
                affected_owner=owners[0] if len(owners) == 1 else "+".join(owners),
                subsystem_id=owners[0] if len(owners) == 1 else None,
                summary="serial evidence failed: " + "; ".join(reasons or ["unknown"]),
                evidence=serial.artifacts,
                retry_scope="owner_and_consumers",
            ))
        firmware_hash = idf.firmware_hash(build_dir or project_dir)
        evidence_ids = []
        for row, _, actual, _ in evaluations:
            kinds = self._evidence_kinds(row, {"build_receipt", "flash_receipt", "serial_log", "firmware_hash", "hardware_identity"})
            evidence = Evidence(evidence_id=store.new_id("evidence"), run_id=state["run_id"], design_digest=state["design_digest"], requirement_ids=[row["requirement_id"]], test_id=row["test_id"], owner=row["owner"], tier=Tier(row["tier"]), expected=row["expected"], actual=actual, verdict=Verdict.PASS, receipt_ids=[build.receipt_id, flash.receipt_id, serial.receipt_id], evidence_kinds=kinds, firmware_sha256=firmware_hash, hardware_identity_key="|".join(str(state["hardware_identity"].get(k) or "") for k in ("chip", "mac", "usb_serial", "board_profile")))
            store.write_evidence(evidence, "subsystem"); evidence_ids.append(evidence.evidence_id)
        has_next_setup = setup_index + 1 < len(setup_groups)
        updates = {"batch_index": index if has_next_setup else index + 1, "verification_setup_index": setup_index + 1 if has_next_setup else 0, "subsystem_index": state.get("subsystem_index", 0) + (0 if has_next_setup else len(owners)), "receipt_ids": state.get("receipt_ids", []) + implementation_receipts + target_receipts + attempt_receipts, "evidence_ids": state.get("evidence_ids", []) + evidence_ids, "cursor": f"STAGE 2:{'+'.join(owners)}:setup-{setup_index + 1}:pass", "next_action": ("execute next verification setup" if has_next_setup else ("execute next verification batch" if index + 1 < len(batches) else "run integration plan")), "progress_seq": state["progress_seq"] + 1}
        self._projection(state, updates); return updates

    def subsystem_route(self, state: HarnessState) -> str:
        batches = state.get("verification_batches") or [[owner] for owner in state.get("subsystems", [])]
        if state.get("batch_index", 0) < len(batches):
            return "readiness"
        contract = self._contract(state)
        return "release_smoke" if contract.get("release", {}).get("early_smoke") and not state.get("release_smoke_complete") else "integration"

    def release_smoke(self, state: HarnessState) -> dict:
        """Prove the normal product can boot before expensive integration work.

        This is intentionally optional and contract-declared.  It uses an
        isolated selftest-off configuration and has no closure authority; the
        final release transaction remains mandatory and fresh.
        """
        project_dir, store = self._context(state); contract = self._contract(state)
        marker = release_marker(contract)
        symbol = self._release_selftest_symbol(contract)
        baseline = project_dir / "sdkconfig"
        if not baseline.is_file():
            raise ValueError("release smoke requires an existing development sdkconfig")
        smoke_dir = store.execution / "smoke" / state["run_id"]
        build_dir, sdkconfig = smoke_dir / "build", smoke_dir / "sdkconfig"
        defaults = smoke_dir / "sdkconfig.smoke.defaults"; smoke_dir.mkdir(parents=True, exist_ok=True)
        # A prior failed smoke attempt can leave an isolated sdkconfig whose
        # values take precedence over updated defaults on retry.  It is
        # generated runtime material, never user configuration; rebuild it
        # from the declared baseline and explicit selftest-off override.
        sdkconfig.unlink(missing_ok=True)
        defaults.write_text(f"# Generated early normal-runtime smoke configuration.\n{symbol}=n\n", encoding="utf-8")
        idf = IdfAdapter(self.repo_root, store, state["run_id"])
        configure = idf.configure_release(
            project_dir,
            build_dir,
            sdkconfig,
            [baseline, defaults],
            idempotency_key=self._transaction_key(
                state, "smoke_configure", symbol=symbol
            ),
        )
        if not configure.success:
            raise ReceiptFailure(configure)
        effective = sdkconfig.read_text(encoding="utf-8", errors="replace") if sdkconfig.is_file() else ""
        if f"# {symbol} is not set" not in effective and f"{symbol}=n" not in effective:
            raise ValueError("release smoke did not disable selftest")
        build = idf.build(
            project_dir,
            build_dir,
            "smoke_build",
            idempotency_key=self._transaction_key(
                state, "smoke_build", symbol=symbol
            ),
        )
        if not build.success:
            raise ReceiptFailure(build)
        serial_adapter = SerialAdapter(self.repo_root, store, state["run_id"]); serial_adapter.stop()
        smoke_hash = idf.firmware_hash(build_dir)
        flash = idf.flash(
            project_dir,
            state["port"],
            build_dir,
            "smoke_flash",
            idempotency_key=self._transaction_key(
                state,
                "smoke_flash",
                firmware_sha256=smoke_hash,
                port=state["port"],
            ),
        )
        if not flash.success:
            raise ReceiptFailure(flash)
        serial = serial_adapter.capture_boot(
            state["port"],
            state["baud"],
            contract.get("release", {}).get("timeout_s", 15),
            marker,
            idempotency_key=self._transaction_key(
                state,
                "smoke_serial",
                firmware_sha256=smoke_hash,
                marker=marker,
                port=state["port"],
                baud=state["baud"],
            ),
        )
        if not serial.success:
            raise ReceiptFailure(serial)
        updates = {
            "release_smoke_complete": True,
            "receipt_ids": state.get("receipt_ids", []) + [configure.receipt_id, build.receipt_id, flash.receipt_id, serial.receipt_id],
            "phase": "integration", "cursor": "STAGE 2.5:release-smoke:pass",
            "next_action": "run integration plan", "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates); self._event({**state, **updates}, "release_smoke", {"marker": marker})
        return updates

    def tier_c(self, state: HarnessState) -> dict:
        contract = self._contract(state)
        pending = set(state.get("tier_c_pending_ids") or [])
        items = [item for item in contract.get("tier_c", []) if not pending or item["id"] in pending]
        evidence_ids: list[str] = []; receipt_ids: list[str] = []
        if items:
            # ``interrupt`` raises before a node can return its state update.
            # Persist the compatibility projection first so an interrupted
            # runner is truthfully visible as a declared human gate, never as
            # a misleading CONTINUOUS run.  This is idempotent on resume.
            waiting = {
                "mode": RunMode.WAITING_TIER_C.value,
                "phase": "tier_c",
                "cursor": "STAGE 2.9:tier-c:waiting",
                "next_action": "submit the approved batched Tier C observation",
                "progress_seq": state.get("progress_seq", 0) + 1,
            }
            self._projection(state, waiting)
            self._event({**state, **waiting}, "tier_c", {"result": "waiting", "item_ids": [item["id"] for item in items]})
            artifacts = state.get("tier_c_artifacts", {})
            response = interrupt({"kind": "TIER_C_BATCH", "items": items, "artifacts": {item["id"]: artifacts.get(item["id"]) for item in items}, "required_response": "object per item: status, artifact_sha256 (when presented), and notes"})
            if not isinstance(response, dict):
                raise ValueError("Tier C batch response must be an object")
            _, store = self._context(state); now = datetime.now(timezone.utc).isoformat()
            receipt = Receipt(receipt_id=store.new_id("tier-c"), run_id=state["run_id"], operation="tier_c_confirmation", started_at=now, finished_at=now, success=True, inputs={"item_ids": [item["id"] for item in items]}, outputs={"responses": response})
            store.write_receipt(receipt, "tier-c"); receipt_ids.append(receipt.receipt_id)
            requirement_owner = {row["id"]: row["owner"] for row in contract["requirements"]}
            failed_items: list[dict] = []; unavailable: list[dict] = []
            for item in items:
                answer = response.get(item["id"])
                if not isinstance(answer, dict):
                    raise ValueError(f"Tier C item {item['id']} requires a structured response object")
                status = answer.get("status")
                artifact = artifacts.get(item["id"], {})
                if artifact.get("sha256") and answer.get("artifact_sha256") != artifact["sha256"]:
                    raise ValueError(f"Tier C item {item['id']} confirmation is not bound to the presented artifact")
                if status not in {"confirmed", "failed", "unable", "not-performed"}:
                    raise ValueError(f"Tier C item {item['id']} has invalid status {status!r}")
                verdict = Verdict.PASS if status == "confirmed" else (Verdict.FAIL if status == "failed" else Verdict.BLOCKED)
                available = {"user_confirmation", "hardware_identity"}
                artifact_refs: list[ArtifactRef] = []
                artifact_receipt = state.get("tier_c_artifact_receipts", {}).get(item["id"])
                bound_receipts = [receipt.receipt_id]
                if artifact.get("path"):
                    available.add("artifact")
                    artifact_refs.append(ArtifactRef(path=artifact["path"], sha256=artifact["sha256"], size=artifact["size"], media_type=artifact["media_type"]))
                    if artifact_receipt:
                        bound_receipts.append(artifact_receipt)
                kinds = self._evidence_kinds(item, available)
                evidence = Evidence(evidence_id=store.new_id("evidence"), run_id=state["run_id"], design_digest=state["design_digest"], requirement_ids=[item["requirement_id"]], test_id=item.get("test_id", item["id"]), owner=item.get("owner", requirement_owner[item["requirement_id"]]), tier=Tier.C, expected=item.get("expected", {"confirmation": "confirmed"}), actual={"response": answer, "artifact": artifact}, verdict=verdict, receipt_ids=bound_receipts, evidence_kinds=kinds, artifacts=artifact_refs, hardware_identity_key="|".join(str(state["hardware_identity"].get(k) or "") for k in ("chip", "mac", "usb_serial", "board_profile")))
                store.write_evidence(evidence, "tier-c"); evidence_ids.append(evidence.evidence_id)
                if status == "failed": failed_items.append(item)
                elif status != "confirmed": unavailable.append(item)
            if unavailable:
                blocker = Blocker(kind="tier_c_unavailable", summary="Tier C observation could not be performed", evidence=f"execution/evidence/tier-c/{evidence_ids[-1]}.json", needed="make the approved physical observation/artifact available")
                updates = {"receipt_ids": state.get("receipt_ids", []) + receipt_ids, "evidence_ids": state.get("evidence_ids", []) + evidence_ids, "mode": RunMode.BLOCKED.value, "blocker": blocker.model_dump(), "phase": "tier_c", "cursor": "BLOCKED:tier_c", "next_action": blocker.needed, "progress_seq": state["progress_seq"] + 1}
                self._projection(state, updates); return updates
            if failed_items:
                from .adapters.agent import AgentAction, AgentAdapter
                retry_owners = sorted({owner for item in failed_items for owner in item.get("retry_owners", [item["owner"]])})
                for owner in retry_owners:
                    observations = {item["id"]: response[item["id"]] for item in failed_items if owner in item.get("retry_owners", [item["owner"]])}
                    result = AgentAdapter(self.repo_root, store, state["run_id"], timeout=1200).execute(AgentAction(
                        project=state["project"], owner=owner,
                        contract_path=Path(state["design_dir"]) / "execution-contract.json",
                        requirement_path=self.repo_root / "requirements" / f"{state['project']}.md",
                        connection_path=self.repo_root / "connections" / f"{state['project']}.md",
                        instruction=f"Repair the Tier C failure and preserve frozen behavior. Observations: {json.dumps(observations, ensure_ascii=False)}",
                        implementation_addendum_path=(
                            self._implementation_addendum_path(state, owner)
                        ),
                    ))
                    receipt_ids.append(result.receipt_id)
                    if not result.success:
                        raise ReceiptFailure(result)
                batches = state.get("verification_batches") or [[owner] for owner in state.get("subsystems", [])]
                affected = [index for index, batch in enumerate(batches) if set(batch).intersection(retry_owners)]
                first = min(affected) if affected else 0
                updates = {"receipt_ids": state.get("receipt_ids", []) + receipt_ids, "evidence_ids": state.get("evidence_ids", []) + evidence_ids, "mode": RunMode.CONTINUOUS.value, "batch_index": first, "subsystem_index": sum(len(batch) for batch in batches[:first]), "tier_c_pending_ids": [item["id"] for item in failed_items], "tier_c_rework": True, "tier_c_artifacts": {}, "tier_c_artifact_receipts": {}, "phase": "subsystems", "cursor": "STAGE 2.9:tier-c:rework", "next_action": "reverify affected owners and integration", "progress_seq": state["progress_seq"] + 1}
                self._projection(state, updates); return updates
        updates = {"receipt_ids": state.get("receipt_ids", []) + receipt_ids, "evidence_ids": state.get("evidence_ids", []) + evidence_ids, "mode": RunMode.CONTINUOUS.value, "tier_c_pending_ids": [], "tier_c_rework": False, "phase": "closure", "cursor": "STAGE 2.9:tier-c:pass", "next_action": "recompute R/DR closure", "progress_seq": state["progress_seq"] + 1}
        self._projection(state, updates); return updates

    @staticmethod
    def tier_c_route(state: HarnessState) -> str:
        if state.get("mode") == RunMode.BLOCKED.value:
            return "end"
        return "verification_batches" if state.get("tier_c_rework") else "closure"

    def integration(self, state: HarnessState) -> dict:
        contract = self._contract(state); tests = contract.get("integration", {}).get("tests")
        if not tests:
            raise ValueError("integration test plan is empty")
        project_dir, store = self._context(state)
        self._require_owned_source(project_dir, "integration", tests)
        integration_owner = self._integration_owner(state)
        # Integration owners are excluded from component verification batches,
        # but still need the same bound readiness/addendum transaction before
        # their source assertions are evaluated.
        _, readiness_receipts = self._ensure_implementation_readiness(
            state, project_dir, store, integration_owner
        )
        addenda, addendum_errors = self._validated_implementation_addenda(state)
        source_errors = addendum_errors + validate_runtime_flow_source(
            Path(state["project_dir"]), contract
        ) + validate_source_facts(
            project_dir,
            contract,
            additional_facts=[
                fact
                for addendum in addenda
                for fact in addendum.get("implementation_facts", [])
            ],
        )
        if source_errors:
            raise ValueError("source/contract consistency failed before integration: " + "; ".join(source_errors))
        idf = IdfAdapter(self.repo_root, store, state["run_id"]); serial_adapter = SerialAdapter(self.repo_root, store, state["run_id"])
        # Integration is a real verification image boundary.  It may declare
        # the same isolated firmware-selftest setup as a component row; never
        # silently fall back to the normal runtime project build, or the
        # frozen integration markers cannot be emitted.
        integration_setup = tests[0].get("setup") or {"kind": "normal_boot"}
        if any((test.get("setup") or {"kind": "normal_boot"}) != integration_setup for test in tests):
            raise ValueError("integration tests mix setup values")
        if (
            schema_has(str(contract.get("schema_version")), "typed_operations")
            and integration_setup.get("kind") == "firmware_selftest"
        ):
            raise DiagnosticFailure(Diagnostic(
                code="INTEGRATION_PARALLEL_SELFTEST_FORBIDDEN",
                cause=FailureCategory.INTEGRATION,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="design_harness",
                summary=(
                    "current-schema Integration must enter through the normal "
                    "production orchestrator; a parallel firmware_selftest is invalid"
                ),
                retry_scope="new_unapproved_design_revision",
            ))
        build_dir: Path | None = None
        workspace_scope = "project-build"
        configure_receipts: list[str] = []
        if integration_setup.get("kind") == "firmware_selftest":
            baseline = project_dir / "sdkconfig"
            if not baseline.is_file():
                raise ValueError("integration firmware_selftest requires an existing ESP-IDF sdkconfig baseline")
            overrides = integration_setup.get("kconfig_overrides") or {}
            missing_kconfig = self._missing_project_kconfig_overrides(project_dir, overrides)
            if missing_kconfig:
                raise DiagnosticFailure(Diagnostic(
                    code="PROJECT_KCONFIG_OVERRIDE_UNDECLARED",
                    cause=FailureCategory.API,
                    disposition=FailureDisposition.REPAIR_INTERNAL,
                    responsible_party="implementation_agent",
                    affected_owner=integration_owner,
                    summary=("integration Kconfig override is not declared by project source: "
                             f"{missing_kconfig}"),
                    retry_scope="integration_test_and_consumers",
                ))
            verify_dir = self._verification_workspace(project_dir, state, "integration")
            verify_dir.mkdir(parents=True, exist_ok=True)
            build_dir = verify_dir / "build"
            workspace_scope = build_dir.relative_to(project_dir).as_posix()
            sdkconfig = verify_dir / "sdkconfig"
            defaults = verify_dir / "sdkconfig.integration.defaults"
            defaults.write_text(
                "# Generated by the execution harness for an isolated integration build.\n"
                + "".join(f"{symbol}={value}\n" for symbol, value in sorted(overrides.items())),
                encoding="utf-8",
            )
            configure = idf.configure_isolated(
                project_dir, build_dir, sdkconfig, [baseline, defaults],
                "integration_configure",
                idempotency_key=self._transaction_key(
                    state, "integration_configure",
                    setup=json.dumps(integration_setup, sort_keys=True),
                    workspace=workspace_scope,
                ),
            )
            configure_receipts.append(configure.receipt_id)
            if not configure.success:
                raise ReceiptFailure(configure)
        build = idf.build(
            project_dir,
            build_dir,
            "integration_build" if build_dir else "build",
            idempotency_key=self._transaction_key(
                state, "integration_build",
                setup=json.dumps(integration_setup, sort_keys=True),
                workspace=workspace_scope,
            ),
        )
        if not build.success:
            raise ReceiptFailure(build)
        integration_hash = idf.firmware_hash(build_dir or project_dir)
        serial_adapter.stop(); flash = idf.flash(
            project_dir,
            state["port"],
            build_dir,
            "integration_flash" if build_dir else "flash",
            idempotency_key=self._transaction_key(
                state,
                "integration_flash",
                firmware_sha256=integration_hash,
                setup=json.dumps(integration_setup, sort_keys=True),
                workspace=workspace_scope,
                port=state["port"],
            ),
        )
        if not flash.success:
            raise ReceiptFailure(flash)
        evidence_ids = []; receipt_ids = readiness_receipts + configure_receipts + [build.receipt_id, flash.receipt_id]; firmware_hash = idf.firmware_hash(build_dir or project_dir)
        integration_rows = [
            row
            for row in contract.get("verification", [])
            if row.get("owner") in {integration_owner, "integration"}
            and row.get("tier") in {"A", "B"}
        ]
        for row in tests:
            integration_test_id = row.get("test_id") or row.get("id")
            if not integration_test_id:
                raise ValueError("integration test requires id or test_id")
            expected = row.get("expected") or row.get("metrics")
            if not expected: raise ValueError(f"integration test {row.get('id')} requires expected rules")
            serial = serial_adapter.capture_boot(
                state["port"],
                state["baud"],
                row.get("timeout_s", expected.get("timeout_s", 60)),
                expected.get("marker"),
                idempotency_key=self._transaction_key(
                    state,
                    "integration_serial",
                    test_id=integration_test_id,
                    firmware_sha256=integration_hash,
                    marker=expected.get("marker"),
                    port=state["port"],
                    baud=state["baud"],
                ),
            ); receipt_ids.append(serial.receipt_id)
            text = (Path(state["project_dir"]) / serial.artifacts[0].path).read_text(encoding="utf-8", errors="replace")
            observations = parse_runtime_observations(text)
            composition_errors = validate_production_composition(
                project_dir,
                contract,
                runtime_observations=observations,
            )
            selected_steps = set(map(str, row.get("runtime_step_ids", [])))
            observed_steps = {
                str(item["runtime_step_id"])
                for item in observations if item.get("runtime_step_id")
            }
            if schema_has(str(contract.get("schema_version")), "typed_operations"):
                missing_steps = sorted(selected_steps - observed_steps)
                if missing_steps:
                    composition_errors.append(
                        f"integration test {integration_test_id!r} lacks "
                        f"correlated runtime observations for {missing_steps}"
                    )
            if composition_errors:
                raise DiagnosticFailure(Diagnostic(
                    code="INTEGRATION_PRODUCTION_PATH_UNPROVEN",
                    cause=FailureCategory.INTEGRATION,
                    disposition=FailureDisposition.REPAIR_INTERNAL,
                    responsible_party="implementation_agent",
                    affected_owner=integration_owner,
                    test_id=integration_test_id,
                    summary="; ".join(composition_errors),
                    evidence=serial.artifacts,
                    retry_scope="production_orchestrator_and_consumers",
                ))
            passed, actual, reasons = evaluate_text(text, expected)
            if not serial.success:
                raise ReceiptFailure(serial)
            if not passed:
                raise DiagnosticFailure(Diagnostic(
                    code="INTEGRATION_EXPECTATION_FAILED",
                    cause=FailureCategory.INTEGRATION,
                    disposition=FailureDisposition.REPAIR_INTERNAL,
                    responsible_party="implementation_agent",
                    affected_owner=integration_owner,
                    test_id=row.get("test_id") or row.get("id"),
                    summary=f"integration {row.get('test_id', row.get('id', 'unnamed'))} failed: {'; '.join(reasons)}",
                    evidence=serial.artifacts,
                    retry_scope="integration_test_and_consumers",
                ))
            # A protocol-oriented integration test needs a durable audit object,
            # not merely a serial transcript.  Derive it deterministically from
            # the just-captured, integrity-bound transcript and bind both the
            # test expectation and evaluated result to a receipt.
            audit_path = project_dir / "execution" / "artifacts" / f"{integration_test_id}-{serial.receipt_id}.json"
            atomic_write_json(audit_path, {
                "schema_version": "1.0", "test_id": integration_test_id,
                "source_serial_receipt": serial.receipt_id,
                "source_serial_artifact": serial.artifacts[0].model_dump(mode="json"),
                "expected": expected, "actual": actual,
            })
            audit_artifact = file_ref(audit_path, project_dir, "application/json")
            protocol_receipt = Receipt(
                receipt_id=store.new_id("protocol"), run_id=state["run_id"],
                operation="integration_protocol_audit",
                started_at=datetime.now(timezone.utc).isoformat(),
                finished_at=datetime.now(timezone.utc).isoformat(), success=True,
                inputs={"test_id": integration_test_id, "serial_receipt_id": serial.receipt_id},
                outputs={
                    "actual": actual,
                    "runtime_observations": observations,
                    "producer_test_id": integration_test_id,
                    "design_digest": state["design_digest"],
                    "firmware_sha256": firmware_hash,
                    "correlation_key": next(
                        (
                            item.get("correlation_key")
                            for item in contract.get("tier_c", [])
                            if item.get("producer_test_id") == integration_test_id
                        ),
                        row.get("correlation_key"),
                    ),
                }, artifacts=[audit_artifact], failure=None,
            )
            store.write_receipt(protocol_receipt, "protocol"); receipt_ids.append(protocol_receipt.receipt_id)
            requirement_ids = row.get("requirement_ids") or [req["id"] for req in contract["requirements"]]
            kinds = self._evidence_kinds(row, {"build_receipt", "flash_receipt", "serial_log", "firmware_hash", "hardware_identity", "artifact", "protocol_receipt"})
            evidence = Evidence(evidence_id=store.new_id("evidence"), run_id=state["run_id"], design_digest=state["design_digest"], requirement_ids=requirement_ids, test_id=integration_test_id, owner=integration_owner, tier=Tier.B, expected=expected, actual=actual, verdict=Verdict.PASS, receipt_ids=[build.receipt_id, flash.receipt_id, serial.receipt_id, protocol_receipt.receipt_id], evidence_kinds=kinds, artifacts=[audit_artifact], firmware_sha256=firmware_hash, hardware_identity_key="|".join(str(state["hardware_identity"].get(k) or "") for k in ("chip", "mac", "usb_serial", "board_profile")))
            store.write_evidence(evidence, "integration"); evidence_ids.append(evidence.evidence_id)
            # Contract rows owned by integration are independent R/DR evidence,
            # not implicit side effects of a broad integration test. Evaluate
            # them against the same fresh serial capture when their declared
            # marker is present, then persist their own evidence identity.
            for verification in self._integration_rows_for_test(
                row, integration_rows
            ):
                passed, actual, reasons = evaluate_text(text, verification["expected"])
                if not passed:
                    raise DiagnosticFailure(Diagnostic(
                        code="INTEGRATION_VERIFICATION_FAILED",
                        cause=FailureCategory.INTEGRATION,
                        disposition=FailureDisposition.REPAIR_INTERNAL,
                        responsible_party="implementation_agent",
                        affected_owner=integration_owner,
                        test_id=verification["test_id"],
                        summary=(
                            f"integration verification "
                            f"{verification['test_id']} failed: "
                            f"{'; '.join(reasons)}"
                        ),
                        evidence=serial.artifacts,
                        retry_scope=(
                            f"integration:{row.get('test_id') or row.get('id')}"
                        ),
                    ))
                verified = Evidence(evidence_id=store.new_id("evidence"), run_id=state["run_id"], design_digest=state["design_digest"], requirement_ids=[verification["requirement_id"]], test_id=verification["test_id"], owner=verification.get("owner", integration_owner), tier=Tier(verification["tier"]), expected=verification["expected"], actual=actual, verdict=Verdict.PASS, receipt_ids=[build.receipt_id, flash.receipt_id, serial.receipt_id], evidence_kinds=self._evidence_kinds(verification, {"build_receipt", "flash_receipt", "serial_log", "firmware_hash", "hardware_identity"}), firmware_sha256=firmware_hash, hardware_identity_key="|".join(str(state["hardware_identity"].get(k) or "") for k in ("chip", "mac", "usb_serial", "board_profile")))
                store.write_evidence(verified, "integration"); evidence_ids.append(verified.evidence_id)
        updates = {
            "receipt_ids": state.get("receipt_ids", []) + receipt_ids,
            "evidence_ids": state.get("evidence_ids", []) + evidence_ids,
            "integration_firmware_sha256": firmware_hash,
            "tier_c_rework": False,
            "phase": "tier_c_artifact_materialization",
            "cursor": "STAGE 3:integration:pass",
            "next_action": "materialize receipt-bound Tier C artifacts",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates); return updates

    def tier_c_artifact_materialization(self, state: HarnessState) -> dict:
        from .adapters.tier_c import TierCArtifactAdapter

        project_dir, store = self._context(state)
        contract = self._contract(state)
        firmware_hash = str(state.get("integration_firmware_sha256") or "")
        if not firmware_hash:
            raise DiagnosticFailure(Diagnostic(
                code="TIER_C_INTEGRATION_FIRMWARE_UNBOUND",
                cause=FailureCategory.INTEGRATION,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="integration_executor",
                summary="Tier C materialization lacks the final integration firmware hash",
            ))
        receipt_ids: list[str] = []
        tier_c_artifacts: dict[str, dict[str, Any]] = {}
        artifact_receipts: dict[str, str] = {}
        pending = set(state.get("tier_c_pending_ids") or [])
        producer_receipts: list[dict[str, Any]] = []
        for receipt_path in store.receipts.rglob("*.json"):
            try:
                producer_receipts.append(
                    json.loads(receipt_path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError):
                continue
        for item in contract.get("tier_c", []):
            if pending and item["id"] not in pending:
                continue
            errors = validate_tier_c_producer_chain(
                item,
                producer_receipts,
                run_id=state["run_id"],
                design_digest=state["design_digest"],
                firmware_sha256=firmware_hash,
            )
            if errors:
                raise DiagnosticFailure(Diagnostic(
                    code="TIER_C_PRODUCER_CHAIN_MISSING",
                    cause=FailureCategory.INTEGRATION,
                    disposition=FailureDisposition.INTERNAL_FAULT,
                    responsible_party="integration_executor",
                    affected_owner=item.get("owner"),
                    test_id=item.get("test_id", item["id"]),
                    summary="; ".join(errors),
                    retry_scope="tier_c_artifact_materialization",
                ))
            receipt, metadata = TierCArtifactAdapter(
                project_dir, store, state["run_id"]
            ).materialize(
                item,
                idempotency_key=self._transaction_key(
                    state,
                    "tier_c_artifact_materialize",
                    item_id=item["id"],
                    firmware_sha256=firmware_hash,
                    correlation_key=item.get("correlation_key"),
                ),
            )
            receipt_ids.append(receipt.receipt_id)
            if not receipt.success:
                raise ReceiptFailure(receipt)
            tier_c_artifacts[item["id"]] = metadata
            artifact_receipts[item["id"]] = receipt.receipt_id
        gate = self._pass_receipt(
            state,
            "tier-c",
            "tier_c_producer",
            inputs={
                "design_digest": state["design_digest"],
                "firmware_sha256": firmware_hash,
            },
            outputs={"artifact_ids": sorted(tier_c_artifacts)},
            idempotency_key=self._transaction_key(
                state, "tier_c_producer",
                firmware_sha256=firmware_hash,
                artifact_ids=sorted(tier_c_artifacts),
            ),
        )
        receipt_ids.append(gate.receipt_id)
        updates = {
            "receipt_ids": state.get("receipt_ids", []) + receipt_ids,
            "tier_c_artifacts": tier_c_artifacts,
            "tier_c_artifact_receipts": artifact_receipts,
            "invariant_passes": state.get("invariant_passes", []) + [
                "HR-007-TIER-C-PRODUCER"
            ],
            "phase": "tier_c",
            "cursor": "STAGE 3.1:tier-c-artifacts:materialized",
            "next_action": "present the single predeclared Tier C batch",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def closure(self, state: HarnessState) -> dict:
        contract = self._contract(state); total = len(contract["requirements"])
        if schema_has(str(contract.get("schema_version")), "typed_operations"):
            required_invariants = set(
                required_rule_ids_before(
                    str(contract["schema_version"]), "closure"
                )
            )
            missing_invariants = sorted(
                required_invariants - set(state.get("invariant_passes", []))
            )
            if missing_invariants:
                raise DiagnosticFailure(Diagnostic(
                    code="CLOSURE_INVARIANT_RECEIPTS_MISSING",
                    cause=FailureCategory.DATA_PATH,
                    disposition=FailureDisposition.INTERNAL_FAULT,
                    responsible_party="orchestrator",
                    summary=(
                        "closure is missing invariant PASS bindings for "
                        f"{missing_invariants}"
                    ),
                    retry_scope="owning_invariant_producer",
                ))
        covered: set[str] = set()
        tier_c_pass: set[str] = set()
        passed_tests: set[str] = set()
        project_dir, store = self._context(state)
        addenda, addendum_errors = self._validated_implementation_addenda(state)
        source_errors = addendum_errors + validate_runtime_flow_source(
            project_dir, contract
        ) + validate_source_facts(
            Path(state["project_dir"]),
            contract,
            additional_facts=[
                fact
                for addendum in addenda
                for fact in addendum.get("implementation_facts", [])
            ],
        )
        if source_errors:
            raise DiagnosticFailure(Diagnostic(
                code="CLOSURE_IMPLEMENTATION_BINDING_INVALID",
                cause=FailureCategory.DATA_PATH,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="implementation_readiness",
                summary=(
                    "closure implementation authority/source binding failed: "
                    + "; ".join(source_errors)
                ),
                retry_scope="implementation_addendum_and_source",
            ))
        from .corrections import invalidated_evidence_ids
        invalidated = invalidated_evidence_ids(Path(state["project_dir"]))
        for path in store.evidence.rglob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("evidence_id") in invalidated:
                continue
            if value.get("run_id") == state["run_id"] and value.get("design_digest") == state["design_digest"] and value.get("verdict") == "PASS":
                covered.update(value.get("requirement_ids", []))
                passed_tests.add(str(value.get("test_id")))
                if value.get("tier") == "C":
                    tier_c_pass.add(str(value.get("test_id")))
        missing = {row["id"] for row in contract["requirements"]} - covered
        if missing:
            raise DiagnosticFailure(Diagnostic(
                code="CLOSURE_REQUIREMENT_EVIDENCE_MISSING",
                cause=FailureCategory.DATA_PATH,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="orchestrator",
                summary=(
                    "closure missing current PASS evidence for "
                    f"{sorted(missing)}"
                ),
                retry_scope="evidence_producer",
            ))
        required_tests = {
            str(row["test_id"])
            for row in contract.get("verification", [])
            if row.get("tier") in {"A", "B"}
        }
        required_tests.update(
            str(item.get("test_id") or item.get("id"))
            for item in contract.get("integration", {}).get("tests", [])
        )
        missing_tests = required_tests - passed_tests
        if missing_tests:
            raise DiagnosticFailure(Diagnostic(
                code="CLOSURE_TEST_EVIDENCE_MISSING",
                cause=FailureCategory.DATA_PATH,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="orchestrator",
                summary=(
                    "closure missing test-level PASS evidence for "
                    f"{sorted(missing_tests)}"
                ),
                retry_scope="test_evidence_producer",
            ))
        missing_tier_c = {item.get("test_id", item["id"]) for item in contract.get("tier_c", [])} - tier_c_pass
        if missing_tier_c:
            raise DiagnosticFailure(Diagnostic(
                code="CLOSURE_TIER_C_EVIDENCE_MISSING",
                cause=FailureCategory.DATA_PATH,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="orchestrator",
                summary=(
                    "closure missing current Tier C PASS evidence for "
                    f"{sorted(missing_tier_c)}"
                ),
                retry_scope="tier_c",
            ))
        conflicts = [item.get("id", item.get("description", "unknown")) for item in contract.get("limitations", []) if item.get("conflicts_with_release")]
        if conflicts:
            raise DiagnosticFailure(Diagnostic(
                code="RELEASE_LIMITATION_CONFLICT",
                cause=FailureCategory.LIMITATION,
                disposition=FailureDisposition.HARD_EXTERNAL_BLOCKER,
                responsible_party="product_owner",
                summary=(
                    "closure has release-conflicting limitations: "
                    f"{conflicts}"
                ),
                user_action_required=(
                    "create and approve a revision that resolves the "
                    "release-conflicting limitation"
                ),
            ))
        closure = Closure(required_total=total, **{"pass": total}).model_dump(by_alias=True)
        updates = {"closure": closure, "phase": "release", "cursor": "STAGE 3.5:closure:pass", "next_action": "fresh selftest-off release build/flash/runtime verification", "progress_seq": state["progress_seq"] + 1}
        self._projection(state, updates); return updates

    def release_prepare(self, state: HarnessState) -> dict:
        if not state.get("closure") or (
            state["closure"].get("pass") != state["closure"].get("required_total")
        ):
            raise DiagnosticFailure(Diagnostic(
                code="RELEASE_PREPARE_WITHOUT_CLOSURE",
                cause=FailureCategory.DATA_PATH,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="orchestrator",
                summary="release preparation requires all-PASS closure",
            ))
        project_dir, _ = self._context(state)
        contract = self._contract(state)
        symbol = self._release_selftest_symbol(contract)
        baseline = project_dir / "sdkconfig"
        if not baseline.is_file():
            raise DiagnosticFailure(Diagnostic(
                code="RELEASE_BASELINE_CONFIG_MISSING",
                cause=FailureCategory.TOOL,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="harness",
                summary="release requires an existing ESP-IDF sdkconfig baseline",
            ))
        attempt = int(state.get("release_attempt", 0)) + 1
        release_dir = (
            project_dir / "execution" / "release" / state["run_id"]
            / f"attempt-{attempt:04d}"
        )
        # Deterministic attempt identity makes crash-after-directory-create
        # replay safe; downstream nodes cannot run before this checkpoint.
        release_dir.mkdir(parents=True, exist_ok=True)
        build_dir = release_dir / "build"
        sdkconfig = release_dir / "sdkconfig"
        defaults = release_dir / "sdkconfig.release.defaults"
        defaults_value = (
            "# Generated clean release configuration.\n"
            f"{symbol}=n\n"
        )
        if defaults.exists() and defaults.read_text(encoding="utf-8") != defaults_value:
            raise ValueError("release prepare found conflicting attempt defaults")
        defaults.write_text(defaults_value, encoding="utf-8")
        receipt = self._pass_receipt(
            state,
            "release",
            "release_prepare",
            inputs={
                "design_digest": state["design_digest"],
                "closure": state["closure"],
                "attempt": attempt,
            },
            outputs={
                "release_dir": release_dir.relative_to(project_dir).as_posix(),
                "empty_build_root": not build_dir.exists(),
                "selftest_symbol": symbol,
            },
            idempotency_key=self._transaction_key(
                state, "release_prepare", attempt=attempt
            ),
        )
        transaction = {
            "scope": "release",
            "attempt": attempt,
            "release_dir": str(release_dir),
            "build_dir": str(build_dir),
            "sdkconfig": str(sdkconfig),
            "defaults": str(defaults),
            "selftest_symbol": symbol,
            "receipt_ids": [receipt.receipt_id],
        }
        updates = {
            "release_attempt": attempt,
            "transaction": transaction,
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "phase": "release_fullclean",
            "cursor": "STAGE 3.6:release:prepared",
            "next_action": "fullclean the attempt-scoped release root",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def release_fullclean(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state)
        transaction = dict(state["transaction"])
        receipt = IdfAdapter(
            self.repo_root, store, state["run_id"]
        ).fullclean(
            project_dir,
            Path(transaction["build_dir"]),
            "release_fullclean",
            idempotency_key=self._transaction_key(
                state, "release_fullclean", attempt=transaction["attempt"]
            ),
        )
        if not receipt.success:
            raise ReceiptFailure(receipt)
        transaction.update({
            "fullclean_receipt_id": receipt.receipt_id,
            "receipt_ids": transaction["receipt_ids"] + [receipt.receipt_id],
        })
        updates = {
            "transaction": transaction,
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "phase": "release_configure",
            "cursor": "STAGE 3.6:release:fullclean",
            "next_action": "configure selftest-off release image",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def release_configure(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state)
        transaction = dict(state["transaction"])
        receipt = IdfAdapter(
            self.repo_root, store, state["run_id"]
        ).configure_release(
            project_dir,
            Path(transaction["build_dir"]),
            Path(transaction["sdkconfig"]),
            [project_dir / "sdkconfig", Path(transaction["defaults"])],
            idempotency_key=self._transaction_key(
                state, "release_configure", attempt=transaction["attempt"]
            ),
        )
        if not receipt.success:
            raise ReceiptFailure(receipt)
        config_text = Path(transaction["sdkconfig"]).read_text(
            encoding="utf-8", errors="replace"
        )
        symbol = transaction["selftest_symbol"]
        if (
            f"# {symbol} is not set" not in config_text
            and f"{symbol}=n" not in config_text
        ):
            raise DiagnosticFailure(Diagnostic(
                code="RELEASE_SELFTEST_CONFIG_ENABLED",
                cause=FailureCategory.API,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="release_executor",
                summary="effective release config did not disable the declared selftest",
            ))
        transaction.update({
            "configure_receipt_id": receipt.receipt_id,
            "receipt_ids": transaction["receipt_ids"] + [receipt.receipt_id],
        })
        updates = {
            "transaction": transaction,
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "phase": "release_build",
            "cursor": "STAGE 3.6:release:configured",
            "next_action": "build fresh production release",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def release_build(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state)
        transaction = dict(state["transaction"])
        build_dir = Path(transaction["build_dir"])
        receipt = IdfAdapter(self.repo_root, store, state["run_id"]).build(
            project_dir,
            build_dir,
            "release_build",
            idempotency_key=self._transaction_key(
                state, "release_build", attempt=transaction["attempt"]
            ),
        )
        if not receipt.success:
            raise ReceiptFailure(receipt)
        contract = self._contract(state)
        for operation_owner in {
            str(item.get("owner"))
            for item in contract.get("operations", [])
            if item.get("owner")
        }:
            errors = validate_linked_operations(
                build_dir,
                {
                    **contract,
                    "operations": [
                        item for item in contract.get("operations", [])
                        if item.get("owner") == operation_owner
                    ],
                },
            )
            if errors:
                raise DiagnosticFailure(Diagnostic(
                    code="RELEASE_OPERATION_LINK_MISSING",
                    cause=FailureCategory.LINK,
                    disposition=FailureDisposition.INTERNAL_FAULT,
                    responsible_party="release_executor",
                    affected_owner=operation_owner,
                    summary="; ".join(errors),
                ))
        firmware_hash = IdfAdapter(
            self.repo_root, store, state["run_id"]
        ).firmware_hash(build_dir)
        transaction.update({
            "build_receipt_id": receipt.receipt_id,
            "firmware_sha256": firmware_hash,
            "receipt_ids": transaction["receipt_ids"] + [receipt.receipt_id],
        })
        updates = {
            "transaction": transaction,
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "phase": "release_flash",
            "cursor": "STAGE 3.6:release:built",
            "next_action": "flash fresh release image",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def release_flash(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state)
        transaction = dict(state["transaction"])
        SerialAdapter(self.repo_root, store, state["run_id"]).stop()
        receipt = IdfAdapter(self.repo_root, store, state["run_id"]).flash(
            project_dir,
            state["port"],
            Path(transaction["build_dir"]),
            "release_flash",
            idempotency_key=self._transaction_key(
                state,
                "release_flash",
                firmware_sha256=transaction["firmware_sha256"],
                port=state["port"],
            ),
        )
        if not receipt.success:
            raise ReceiptFailure(receipt)
        transaction.update({
            "flash_receipt_id": receipt.receipt_id,
            "receipt_ids": transaction["receipt_ids"] + [receipt.receipt_id],
        })
        updates = {
            "transaction": transaction,
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "phase": "release_observe",
            "cursor": "STAGE 3.6:release:flashed",
            "next_action": "observe normal production runtime",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def release_observe(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state)
        transaction = dict(state["transaction"])
        contract = self._contract(state)
        marker = release_marker(contract)
        receipt = SerialAdapter(
            self.repo_root, store, state["run_id"]
        ).capture_boot(
            state["port"],
            state["baud"],
            contract.get("release", {}).get("timeout_s", 30),
            marker,
            operation="release_observe",
            idempotency_key=self._transaction_key(
                state,
                "release_observe",
                firmware_sha256=transaction["firmware_sha256"],
                marker=marker,
            ),
        )
        if not receipt.success:
            raise ReceiptFailure(receipt)
        transaction.update({
            "serial_receipt_id": receipt.receipt_id,
            "serial_artifact": receipt.artifacts[0].model_dump(mode="json"),
            "runtime_marker": marker,
            "receipt_ids": transaction["receipt_ids"] + [receipt.receipt_id],
        })
        updates = {
            "transaction": transaction,
            "receipt_ids": state.get("receipt_ids", []) + [receipt.receipt_id],
            "phase": "release_validate",
            "cursor": "STAGE 3.6:release:observed",
            "next_action": "validate core production behavior and forbidden states",
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        return updates

    def release_validate(self, state: HarnessState) -> dict:
        project_dir, _ = self._context(state)
        transaction = dict(state["transaction"])
        contract = self._contract(state)
        artifact = ArtifactRef.model_validate(transaction["serial_artifact"])
        text = (project_dir / artifact.path).read_text(
            encoding="utf-8", errors="replace"
        )
        forbidden = list(
            contract.get("release", {}).get("forbidden_runtime_patterns", [])
        )
        runtime_errors = validate_release_runtime_text(text, forbidden)
        if runtime_errors:
            raise DiagnosticFailure(Diagnostic(
                code="RELEASE_FORBIDDEN_STATE_OBSERVED",
                cause=FailureCategory.INTEGRATION,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="release_executor",
                summary="; ".join(runtime_errors),
                evidence=[artifact],
            ))
        observations = parse_runtime_observations(text)
        composition_errors = validate_production_composition(
            project_dir, contract, runtime_observations=observations
        )
        scenarios = {
            str(item["scenario_id"]): item
            for item in contract.get("integration", {}).get(
                "production_scenarios", []
            )
        }
        selected = [
            str(item)
            for item in contract.get("release", {}).get(
                "core_production_scenario_ids", []
            )
        ]
        scenario_receipts: list[str] = []
        for scenario_id in selected:
            scenario = scenarios.get(scenario_id)
            if scenario is None:
                composition_errors.append(
                    f"release core scenario {scenario_id!r} is undeclared"
                )
                continue
            passed, actual, reasons = evaluate_text(text, scenario["expected"])
            observed_steps = {
                str(item["runtime_step_id"])
                for item in observations if item.get("runtime_step_id")
            }
            missing_steps = sorted(
                set(map(str, scenario["runtime_step_ids"])) - observed_steps
            )
            if not passed or missing_steps:
                composition_errors.append(
                    f"release scenario {scenario_id!r} failed: "
                    + "; ".join(reasons + (
                        [f"missing runtime steps {missing_steps}"]
                        if missing_steps else []
                    ))
                )
                continue
            receipt = self._pass_receipt(
                state,
                "release",
                "release_production_scenario",
                inputs={
                    "scenario_id": scenario_id,
                    "serial_receipt_id": transaction["serial_receipt_id"],
                },
                outputs={
                    "actual": actual,
                    "runtime_observations": observations,
                    "design_digest": state["design_digest"],
                    "firmware_sha256": transaction["firmware_sha256"],
                },
                artifacts=[artifact],
                idempotency_key=self._transaction_key(
                    state,
                    "release_production_scenario",
                    scenario_id=scenario_id,
                    firmware_sha256=transaction["firmware_sha256"],
                    serial_receipt_id=transaction["serial_receipt_id"],
                ),
            )
            scenario_receipts.append(receipt.receipt_id)
        if not selected:
            composition_errors.append("release selects no core production scenario")
        if composition_errors:
            raise DiagnosticFailure(Diagnostic(
                code="RELEASE_PRODUCTION_BEHAVIOR_UNPROVEN",
                cause=FailureCategory.INTEGRATION,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="release_executor",
                summary="; ".join(composition_errors),
                evidence=[artifact],
            ))
        build_dir = Path(transaction["build_dir"])
        application_binary = next(iter(sorted(build_dir.glob("*.bin"))), None)
        if application_binary is None:
            raise DiagnosticFailure(Diagnostic(
                code="RELEASE_APPLICATION_BINARY_MISSING",
                cause=FailureCategory.BUILD,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="release_executor",
                summary="release build produced no application binary",
            ))
        build_receipt = self._load_receipt_by_id(
            project_dir, transaction["build_receipt_id"]
        )
        flash_receipt = self._load_receipt_by_id(
            project_dir, transaction["flash_receipt_id"]
        )
        serial_receipt = self._load_receipt_by_id(
            project_dir, transaction["serial_receipt_id"]
        )
        release_evidence = ReleaseEvidence(
            closure_pass=True,
            selftest_disabled=True,
            fullclean_receipt_id=transaction["fullclean_receipt_id"],
            configure_receipt_id=transaction["configure_receipt_id"],
            build_log=build_receipt.artifacts[0].path,
            flash_log=flash_receipt.artifacts[0].path,
            serial_log=serial_receipt.artifacts[0].path,
            runtime_marker=transaction["runtime_marker"],
            firmware_sha256=transaction["firmware_sha256"],
            firmware_binary=application_binary.relative_to(project_dir).as_posix(),
            build_receipt_id=transaction["build_receipt_id"],
            flash_receipt_id=transaction["flash_receipt_id"],
            serial_receipt_id=transaction["serial_receipt_id"],
            production_scenario_receipt_ids=scenario_receipts,
            production_scenario_ids=selected,
        )
        release_errors = validate_release_transaction(release_evidence)
        if release_errors:
            raise DiagnosticFailure(Diagnostic(
                code="RELEASE_TRANSACTION_INCOMPLETE",
                cause=FailureCategory.INTEGRATION,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="release_executor",
                summary="; ".join(release_errors),
            ))
        release_evidence = release_evidence.model_dump()
        runtime = ProjectRuntime(self.repo_root, state["project"]).ensure()
        sequence_state = (
            json.loads(runtime.control_event_sequence.read_text(
                encoding="utf-8"
            ))
            if runtime.control_event_sequence.is_file() else {}
        )
        control_errors = validate_control_event_delivery(
            read_control_events(
                self.repo_root,
                state["project"],
                model_action_only=False,
            ),
            sequence_state,
        )
        if control_errors:
            raise DiagnosticFailure(Diagnostic(
                code="CONTROL_EVENT_DELIVERY_INVALID",
                cause=FailureCategory.TOOL,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="harness",
                summary="; ".join(control_errors),
            ))
        control_receipt = self._pass_receipt(
            state,
            "control",
            "control_event_report",
            inputs={
                "event_seq": int(sequence_state.get("event_seq") or 0),
            },
            outputs={
                "model_polling_calls": 0,
                "suppressed_progress_events": int(
                    sequence_state.get("suppressed_progress_events") or 0
                ),
            },
            idempotency_key=self._transaction_key(
                state,
                "control_event_report",
                event_seq=int(sequence_state.get("event_seq") or 0),
            ),
        )
        updates = {
            "release_evidence": release_evidence,
            "release_verified": True,
            "mode": RunMode.COMPLETE.value,
            "blocker": None,
            "receipt_ids": (
                state.get("receipt_ids", [])
                + scenario_receipts + [control_receipt.receipt_id]
            ),
            "invariant_passes": state.get("invariant_passes", []) + [
                "HR-001-EVENT-DRIVEN-SUPERVISION",
                "HR-011-CLEAN-RELEASE"
            ],
            "phase": "complete",
            "cursor": "STAGE 3.6:release:pass",
            "next_action": "terminal validation complete",
            "progress_seq": state["progress_seq"] + 1,
        }
        combined = {**state, **updates}
        projection = RunStateProjection(
            run_id=state["run_id"],
            mode=RunMode.COMPLETE,
            cursor=updates["cursor"],
            next_action=updates["next_action"],
            progress_seq=updates["progress_seq"],
            progress_fingerprint=progress_fingerprint(combined),
            release_verified=True,
            closure=state["closure"],
            release_evidence=release_evidence,
            blocker=None,
        )
        errors = validate_terminal(projection, project_dir)
        if errors:
            raise DiagnosticFailure(Diagnostic(
                code="TERMINAL_VALIDATION_FAILED",
                cause=FailureCategory.DATA_PATH,
                disposition=FailureDisposition.INTERNAL_FAULT,
                responsible_party="terminal_validator",
                summary="; ".join(errors),
            ))
        self._projection(state, updates)
        self._event(
            {**state, **updates},
            "release_validate",
            {"firmware_sha256": transaction["firmware_sha256"]},
        )
        return updates

    def release(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state); contract = self._contract(state)
        addenda, addendum_errors = self._validated_implementation_addenda(state)
        source_errors = addendum_errors + validate_runtime_flow_source(
            project_dir, contract
        ) + validate_source_facts(
            project_dir,
            contract,
            additional_facts=[
                fact
                for addendum in addenda
                for fact in addendum.get("implementation_facts", [])
            ],
        )
        if source_errors:
            raise ValueError("source/contract consistency failed before release: " + "; ".join(source_errors))
        marker = release_marker(contract)
        self._require_owned_source(project_dir, "integration", [{"expected": {"marker": marker}}])
        symbol = self._release_selftest_symbol(contract)
        development_config = project_dir / "sdkconfig"
        if not development_config.is_file():
            raise ValueError("release requires an existing ESP-IDF sdkconfig baseline")
        # A release must not mutate the developer's selftest-on configuration.
        # Use it as a baseline, then let ESP-IDF write an isolated config/build
        # in the run workspace with an explicit selftest-off final override.
        release_dir = store.execution / "release" / state["run_id"]
        release_build = release_dir / "build"
        release_sdkconfig = release_dir / "sdkconfig"
        release_defaults = release_dir / "sdkconfig.release.defaults"
        release_dir.mkdir(parents=True, exist_ok=True)
        # As with early smoke, never reuse a generated isolated sdkconfig
        # after a failed/repaired release attempt.
        release_sdkconfig.unlink(missing_ok=True)
        release_defaults.write_text(
            "# Generated by the execution harness; release diagnostics must be disabled.\n"
            f"{symbol}=n\n",
            encoding="utf-8",
        )
        idf = IdfAdapter(self.repo_root, store, state["run_id"])
        configure = idf.configure_release(
            project_dir,
            release_build,
            release_sdkconfig,
            [development_config, release_defaults],
            idempotency_key=self._transaction_key(
                state, "release_configure", symbol=symbol
            ),
        )
        if not configure.success:
            raise ReceiptFailure(configure)
        sdkconfig = release_sdkconfig.read_text(encoding="utf-8", errors="replace") if release_sdkconfig.exists() else ""
        disabled = f"# {symbol} is not set" in sdkconfig or f"{symbol}=n" in sdkconfig
        if not disabled: raise ValueError(f"release effective config does not prove {symbol}=disabled")
        build = idf.build(
            project_dir,
            release_build,
            "release_build",
            idempotency_key=self._transaction_key(
                state, "release_build", symbol=symbol
            ),
        )
        if not build.success:
            raise ReceiptFailure(build)
        firmware_hash = idf.firmware_hash(release_build)
        serial_adapter = SerialAdapter(self.repo_root, store, state["run_id"]); serial_adapter.stop()
        flash = idf.flash(
            project_dir,
            state["port"],
            release_build,
            "release_flash",
            idempotency_key=self._transaction_key(
                state,
                "release_flash",
                firmware_sha256=firmware_hash,
                port=state["port"],
            ),
        )
        if not flash.success:
            raise ReceiptFailure(flash)
        serial = serial_adapter.capture_boot(
            state["port"],
            state["baud"],
            contract.get("release", {}).get("timeout_s", 15),
            marker,
            idempotency_key=self._transaction_key(
                state,
                "release_serial",
                firmware_sha256=firmware_hash,
                marker=marker,
                port=state["port"],
                baud=state["baud"],
            ),
        )
        if not serial.success:
            raise ReceiptFailure(serial)
        def log_path(receipt): return receipt.artifacts[0].path
        application_binary = next(iter(sorted(release_build.glob("*.bin"))), None)
        if application_binary is None:
            raise FileNotFoundError("release build did not produce an application binary")
        release_evidence = ReleaseEvidence(closure_pass=True, selftest_disabled=True, build_log=log_path(build), flash_log=log_path(flash), serial_log=log_path(serial), runtime_marker=marker, firmware_sha256=firmware_hash, firmware_binary=str(application_binary.relative_to(project_dir)), build_receipt_id=build.receipt_id, flash_receipt_id=flash.receipt_id, serial_receipt_id=serial.receipt_id).model_dump()
        updates = {
            "release_evidence": release_evidence,
            "release_verified": True,
            "receipt_ids": state.get("receipt_ids", []) + [configure.receipt_id, build.receipt_id, flash.receipt_id, serial.receipt_id],
            "mode": RunMode.COMPLETE.value,
            "phase": "complete",
            "cursor": "STAGE 3.6:release:pass",
            "next_action": "none",
            "blocker": None,
            "failure": None,
            "failed_node": None,
            "pause_reason": None,
            "pause_next_node": None,
            "progress_seq": state["progress_seq"] + 1,
        }
        self._projection(state, updates)
        projection = RunStateProjection.model_validate(json.loads((store.execution / "run-state.json").read_text(encoding="utf-8")))
        errors = validate_terminal(projection, project_dir)
        if errors: raise ValueError("terminal validation failed: " + "; ".join(errors))
        self._event({**state, **updates}, "release", {"firmware_sha256": firmware_hash})
        return updates


def build_graph(repo_root: Path, checkpointer=None):
    nodes = HarnessNodes(repo_root)
    graph = StateGraph(HarnessState)
    graph.add_node("initialize", nodes.initialize)
    graph.add_node("pause_control", nodes.pause_control)
    for name in (
        "preflight", "design", "approval", "bind", "invariant_gate",
        "schema_gate", "operation_authority", "verification_batches",
        "implementation_materialize", "implementation_completeness",
        "source_validate", "configure", "build", "flash", "observe",
        "evaluate", "evidence_commit", "component_architecture",
        "production_composition", "integration_prepare",
        "integration_configure", "integration_build", "integration_flash",
        "integration_observe", "integration_evaluate",
        "integration_evidence_commit", "release_smoke",
        "tier_c_artifact_materialization", "tier_c",
        "closure", "release_prepare", "release_fullclean",
        "release_configure", "release_build", "release_flash",
        "release_observe", "release_validate", "release",
    ):
        graph.add_node(name, nodes.safe(name))
    # Legacy node identities remain readable in historical checkpoints but are
    # not reachable in the current executable topology.
    graph.add_node("readiness", nodes.safe("readiness"))
    graph.add_node("subsystem", nodes.safe("subsystem"))
    graph.add_node("integration", nodes.safe("integration"))
    graph.add_node("recover", nodes.recover)
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "preflight")
    for current, following in (
        ("preflight", "design"),
        ("design", "approval"),
        ("approval", "invariant_gate"),
        ("invariant_gate", "schema_gate"),
        ("schema_gate", "operation_authority"),
        ("operation_authority", "bind"),
        ("bind", "verification_batches"),
        ("verification_batches", "implementation_materialize"),
        ("implementation_materialize", "implementation_completeness"),
        ("source_validate", "configure"),
        ("configure", "build"),
        ("build", "flash"),
        ("flash", "observe"),
        ("observe", "evaluate"),
        ("evaluate", "evidence_commit"),
        ("component_architecture", "production_composition"),
        ("production_composition", "integration_prepare"),
        ("integration_prepare", "integration_configure"),
        ("integration_configure", "integration_build"),
        ("integration_build", "integration_flash"),
        ("integration_flash", "integration_observe"),
        ("integration_observe", "integration_evaluate"),
        ("integration_evaluate", "integration_evidence_commit"),
        ("integration_evidence_commit", "tier_c_artifact_materialization"),
        ("tier_c_artifact_materialization", "tier_c"),
        ("closure", "release_prepare"),
        ("release_prepare", "release_fullclean"),
        ("release_fullclean", "release_configure"),
        ("release_configure", "release_build"),
        ("release_build", "release_flash"),
        ("release_flash", "release_observe"),
        ("release_observe", "release_validate"),
    ):
        graph.add_conditional_edges(current, nodes.route_after(following), {following: following, "recover": "recover"})
    graph.add_conditional_edges(
        "implementation_completeness",
        lambda state: (
            "recover"
            if state.get("failure")
            else (
                "source_validate"
                if state.get("transaction", {}).get("image")
                else "component_architecture"
            )
        ),
        {
            "source_validate": "source_validate",
            "component_architecture": "component_architecture",
            "recover": "recover",
        },
    )
    graph.add_conditional_edges(
        "evidence_commit",
        lambda state: (
            "recover"
            if state.get("failure")
            else (
                "tier_c_artifact_materialization"
                if state.get("completed_transaction_scope") == "integration"
                else (
                    "implementation_materialize"
                    if state.get("verification_image_index", 0)
                    < len(state.get("verification_images", []))
                    else "component_architecture"
                )
            )
        ),
        {
            "implementation_materialize": "implementation_materialize",
            "component_architecture": "component_architecture",
            "tier_c_artifact_materialization": "tier_c_artifact_materialization",
            "recover": "recover",
        },
    )
    graph.add_conditional_edges("tier_c", lambda state: "recover" if state.get("failure") else nodes.tier_c_route(state), {"closure": "closure", "verification_batches": "verification_batches", "end": END, "recover": "recover"})
    graph.add_conditional_edges("subsystem", lambda state: "recover" if state.get("failure") else nodes.subsystem_route(state), {"readiness": "readiness", "release_smoke": "release_smoke", "integration": "integration", "recover": "recover"})
    graph.add_conditional_edges("readiness", nodes.route_after("subsystem"), {"subsystem": "subsystem", "recover": "recover"})
    graph.add_conditional_edges("release_smoke", nodes.route_after("integration"), {"integration": "integration", "recover": "recover"})
    graph.add_conditional_edges("release_validate", nodes.route_after("end"), {"end": END, "recover": "recover"})
    graph.add_conditional_edges("release", nodes.route_after("end"), {"end": END, "recover": "recover"})
    recovery_targets = {
        name: name for name in (
            "preflight", "design", "approval", "bind", "invariant_gate",
            "schema_gate", "operation_authority", "verification_batches",
            "implementation_materialize", "source_validate", "configure",
            "implementation_completeness",
            "build", "flash", "observe", "evaluate", "evidence_commit",
            "component_architecture", "production_composition",
            "integration_prepare", "integration_configure",
            "integration_build", "integration_flash", "integration_observe",
            "integration_evaluate", "integration_evidence_commit", "readiness",
            "subsystem", "release_smoke", "tier_c_artifact_materialization",
            "tier_c", "integration", "closure",
            "release_prepare", "release_fullclean", "release_configure",
            "release_build", "release_flash", "release_observe",
            "release_validate", "release",
        )
    }
    recovery_targets["end"] = END
    graph.add_conditional_edges("recover", nodes.recovery_route, recovery_targets)
    return graph.compile(checkpointer=checkpointer)
