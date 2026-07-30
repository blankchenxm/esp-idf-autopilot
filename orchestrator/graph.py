from __future__ import annotations

import json
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
from .policies import classify_failure, disposition_for, failure_fingerprint, material_fingerprint, progress_fingerprint, recovery_budget
from .evaluation import evaluate_text
from .contract_consistency import validate_source_facts
from .implementation_reuse import assess_existing_implementation
from .state import HarnessState
from .storage import ProjectStore, atomic_write_json, digest, file_ref
from .subgraphs.release import release_marker
from .subgraphs.subsystem import expected_for_owner, subsystem_order, verification_batches
from .validators import validate_design_package, validate_terminal
from .implementation_readiness import assess_implementation_readiness
from .implementation_addendum import (
    build_implementation_addendum,
    validate_addendum_source_bindings,
    validate_implementation_addendum,
)
from .component_selection import required_operations_for_subsystem


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
                prefix + match.group(1)
                for match in re.finditer(r"(?m)^\s*config\s+([A-Z0-9_]+)\s*$", text)
            )
        return sorted(requested - declared)

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
        return digest(
            {
                "schema": "side-effect-v1",
                "run_id": state["run_id"],
                "design_digest": state.get("design_digest"),
                "hardware_identity": state.get("hardware_identity"),
                "material_fingerprint": material_fingerprint(project_dir, state),
                "operation": operation,
                "scope": scope,
            }
        )

    def _event(self, state: HarnessState, node: str, payload: dict[str, Any]) -> None:
        _, store = self._context(state)
        store.append_event(ExecutionEvent(event_id=store.new_id("event"), run_id=state["run_id"], event_type="node", node=node, payload=payload))

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
                    category = classify_failure(summary)
                    retryable = True
                    evidence = []
                if category == FailureCategory.UNKNOWN and typed_diagnostic is None and source_receipt is None:
                    # A failed live board probe is a retryable hardware fact, not
                    # an environment-authority failure.  Treating it as the latter
                    # bypassed the Hardware recovery policy and blocked after one
                    # transient port/workspace collision.
                    category = {
                        "preflight": FailureCategory.HARDWARE,
                        "approval": FailureCategory.LIMITATION,
                        "integration": FailureCategory.INTEGRATION,
                    }.get(node, FailureCategory.UNKNOWN)
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
                owner = (
                    (typed_diagnostic.affected_owner if typed_diagnostic else None)
                    or (source_receipt.failure.owner if source_receipt and source_receipt.failure else None)
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
                    else disposition_for(category, retryable=retryable, node=node)
                )
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
                    summary=summary,
                    evidence=evidence or [boundary_artifact],
                    retry_scope=("owner_and_consumers" if disposition == FailureDisposition.REPAIR_INTERNAL else node),
                    material_fingerprint=material,
                    failure_fingerprint=signature,
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
        # A missing firmware marker may be a short-lived serial-startup race on
        # its first observation.  Repeating the identical capture, however,
        # cannot make a marker appear when the component selftest was never
        # composed into main/.  Promote the second identical observation to an
        # owner repair before consuming the transient-retry budget.  This is a
        # graph policy (and is receipt/evidence driven), not an agent prompt.
        if (
            diagnostic.disposition == FailureDisposition.RETRY_TRANSIENT
            and diagnostic.cause == FailureCategory.SERIAL
            and diagnostic.affected_owner
            and diagnostic.summary.startswith("expected marker missing:")
            and attempts[signature] >= 2
        ):
            diagnostic = diagnostic.model_copy(update={
                "disposition": FailureDisposition.REPAIR_INTERNAL,
                "responsible_party": "implementation_agent",
                "retry_scope": "owner_and_consumers",
            })
            self._event(state, "recover", {
                "result": "promote_missing_marker_to_owner_repair",
                "owner": diagnostic.affected_owner,
                "fingerprint": signature,
                "attempt": attempts[signature],
            })
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
        # A changed Harness fingerprint is itself material retry input.  Do
        # not send a stale adapter/workspace failure to an owner agent and
        # then reject that agent for leaving firmware source unchanged.
        harness_material_changed = bool(
            diagnostic.material_fingerprint
            and diagnostic.material_fingerprint
            != material_fingerprint(project_dir, state)
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
            if not repair_owners:
                blocker = Blocker(
                    kind="repair_owner_ambiguous",
                    summary=diagnostic.summary,
                    evidence=evidence_path,
                    needed="fix the Harness owner attribution before retrying this internal repair",
                )
                updates = {
                    "failure_attempts": attempts,
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
                    "receipt_ids": state.get("receipt_ids", []) + receipt_ids,
                    "mode": RunMode.FAULTED.value,
                    "blocker": blocker.model_dump(),
                    "cursor": f"FAULTED:{target}:no-change",
                    "next_action": "repair the unchanged internal failure",
                    "progress_seq": state["progress_seq"] + 1,
                }
                self._projection(state, updates)
                return updates
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
        updates = {"failure_attempts": attempts, "failure": None, "diagnostic": None, "recovery_target": retry_target, "receipt_ids": state.get("receipt_ids", []) + receipt_ids, "phase": f"retry:{retry_target}", "cursor": f"RECOVERY:{retry_target}:attempt-{attempts[signature]}", "next_action": f"retry {retry_target} from checkpoint boundary", "progress_seq": state["progress_seq"] + 1}
        if state.get("port") != updates.get("port") and state.get("port"): updates["port"] = state["port"]
        self._projection(state, updates); self._event({**state, **updates}, "recover", {"result": "retry", "target": retry_target, "fingerprint": signature, "attempt": attempts[signature]})
        return updates

    def recovery_route(self, state: HarnessState) -> str:
        return "end" if state.get("mode") in {RunMode.BLOCKED.value, RunMode.FAULTED.value} else str(state.get("recovery_target") or state.get("failed_node") or "end")

    def initialize(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state)
        run_id = state.get("run_id") or f"run-{uuid.uuid4().hex[:16]}"
        design_dir = Path(state.get("design_dir") or project_dir / "design-package" / "rev-0001").resolve()
        updates = {"run_id": run_id, "design_dir": str(design_dir), "mode": RunMode.CONTINUOUS.value, "phase": "preflight", "cursor": "STAGE 0:initialize", "next_action": "run environment and hardware preflight", "progress_seq": 1, "receipt_ids": [], "evidence_ids": [], "subsystem_index": 0, "batch_index": 0, "failure_attempts": {}}
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
        updates = {"port": session.port, "receipt_ids": state.get("receipt_ids", []) + [refresh.receipt_id], "phase": "readiness", "cursor": "STAGE 1.5:bound", "next_action": "prove implementation readiness for the next batch", "progress_seq": state["progress_seq"] + 1}
        self._projection(state, updates); return updates

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
        source_errors = addendum_errors + validate_source_facts(
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
        setting = str(contract.get("release", {}).get("selftest_config") or "")
        symbol, separator, requested = setting.partition("=")
        if not symbol or (separator and requested not in {"", "n"}):
            raise ValueError("release smoke requires a selftest-off config symbol")
        baseline = project_dir / "sdkconfig"
        if not baseline.is_file():
            raise ValueError("release smoke requires an existing development sdkconfig")
        smoke_dir = store.execution / "smoke" / state["run_id"]
        build_dir, sdkconfig = smoke_dir / "build", smoke_dir / "sdkconfig"
        defaults = smoke_dir / "sdkconfig.smoke.defaults"; smoke_dir.mkdir(parents=True, exist_ok=True)
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
        return "readiness" if state.get("tier_c_rework") else "closure"

    def integration(self, state: HarnessState) -> dict:
        contract = self._contract(state); tests = contract.get("integration", {}).get("tests")
        if not tests:
            raise ValueError("integration test plan is empty")
        project_dir, store = self._context(state)
        self._require_owned_source(project_dir, "integration", tests)
        addenda, addendum_errors = self._validated_implementation_addenda(state)
        source_errors = addendum_errors + validate_source_facts(
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
        build = idf.build(
            project_dir,
            idempotency_key=self._transaction_key(
                state, "integration_build"
            ),
        )
        if not build.success:
            raise ReceiptFailure(build)
        integration_hash = idf.firmware_hash(project_dir)
        serial_adapter.stop(); flash = idf.flash(
            project_dir,
            state["port"],
            idempotency_key=self._transaction_key(
                state,
                "integration_flash",
                firmware_sha256=integration_hash,
                port=state["port"],
            ),
        )
        if not flash.success:
            raise ReceiptFailure(flash)
        evidence_ids = []; receipt_ids = [build.receipt_id, flash.receipt_id]; firmware_hash = idf.firmware_hash(project_dir)
        integration_owner = self._integration_owner(state)
        integration_rows = [
            row
            for row in contract.get("verification", [])
            if row.get("owner") in {integration_owner, "integration"}
            and row.get("tier") in {"A", "B"}
        ]
        for row in tests:
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
                    test_id=row.get("test_id") or row.get("id"),
                    firmware_sha256=integration_hash,
                    marker=expected.get("marker"),
                    port=state["port"],
                    baud=state["baud"],
                ),
            ); receipt_ids.append(serial.receipt_id)
            text = (Path(state["project_dir"]) / serial.artifacts[0].path).read_text(encoding="utf-8", errors="replace")
            passed, actual, reasons = evaluate_text(text, expected)
            if not serial.success:
                raise ReceiptFailure(serial)
            if not passed:
                raise DiagnosticFailure(Diagnostic(
                    code="INTEGRATION_EXPECTATION_FAILED",
                    cause=FailureCategory.INTEGRATION,
                    disposition=FailureDisposition.REPAIR_INTERNAL,
                    responsible_party="implementation_agent",
                    affected_owner="integration",
                    test_id=row.get("test_id") or row.get("id"),
                    summary=f"integration {row.get('test_id', row.get('id', 'unnamed'))} failed: {'; '.join(reasons)}",
                    evidence=serial.artifacts,
                    retry_scope="integration_test_and_consumers",
                ))
            # A protocol-oriented integration test needs a durable audit object,
            # not merely a serial transcript.  Derive it deterministically from
            # the just-captured, integrity-bound transcript and bind both the
            # test expectation and evaluated result to a receipt.
            audit_path = project_dir / "execution" / "artifacts" / f"{row['test_id']}-{serial.receipt_id}.json"
            atomic_write_json(audit_path, {
                "schema_version": "1.0", "test_id": row["test_id"],
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
                inputs={"test_id": row["test_id"], "serial_receipt_id": serial.receipt_id},
                outputs={"actual": actual}, artifacts=[audit_artifact], failure=None,
            )
            store.write_receipt(protocol_receipt, "protocol"); receipt_ids.append(protocol_receipt.receipt_id)
            requirement_ids = row.get("requirement_ids") or [req["id"] for req in contract["requirements"]]
            kinds = self._evidence_kinds(row, {"build_receipt", "flash_receipt", "serial_log", "firmware_hash", "hardware_identity", "artifact", "protocol_receipt"})
            evidence = Evidence(evidence_id=store.new_id("evidence"), run_id=state["run_id"], design_digest=state["design_digest"], requirement_ids=requirement_ids, test_id=row.get("test_id") or row.get("id", "unnamed"), owner=integration_owner, tier=Tier.B, expected=expected, actual=actual, verdict=Verdict.PASS, receipt_ids=[build.receipt_id, flash.receipt_id, serial.receipt_id, protocol_receipt.receipt_id], evidence_kinds=kinds, artifacts=[audit_artifact], firmware_sha256=firmware_hash, hardware_identity_key="|".join(str(state["hardware_identity"].get(k) or "") for k in ("chip", "mac", "usb_serial", "board_profile")))
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
                        affected_owner="integration",
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
        from .adapters.tier_c import TierCArtifactAdapter
        tier_c_artifacts: dict[str, dict] = {}; tier_c_artifact_receipts: dict[str, str] = {}
        pending = set(state.get("tier_c_pending_ids") or [])
        for item in contract.get("tier_c", []):
            if pending and item["id"] not in pending:
                continue
            artifact_receipt, metadata = TierCArtifactAdapter(project_dir, store, state["run_id"]).materialize(item)
            receipt_ids.append(artifact_receipt.receipt_id)
            if not artifact_receipt.success:
                raise ReceiptFailure(artifact_receipt)
            tier_c_artifacts[item["id"]] = metadata
            tier_c_artifact_receipts[item["id"]] = artifact_receipt.receipt_id
        updates = {"receipt_ids": state.get("receipt_ids", []) + receipt_ids, "evidence_ids": state.get("evidence_ids", []) + evidence_ids, "tier_c_artifacts": tier_c_artifacts, "tier_c_artifact_receipts": tier_c_artifact_receipts, "tier_c_rework": False, "phase": "tier_c", "cursor": "STAGE 3:integration:pass", "next_action": "evaluate optional Tier C artifact batch", "progress_seq": state["progress_seq"] + 1}
        self._projection(state, updates); return updates

    def closure(self, state: HarnessState) -> dict:
        contract = self._contract(state); total = len(contract["requirements"])
        covered: set[str] = set()
        tier_c_pass: set[str] = set()
        passed_tests: set[str] = set()
        _, store = self._context(state)
        addenda, addendum_errors = self._validated_implementation_addenda(state)
        source_errors = addendum_errors + validate_source_facts(
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

    def release(self, state: HarnessState) -> dict:
        project_dir, store = self._context(state); contract = self._contract(state)
        addenda, addendum_errors = self._validated_implementation_addenda(state)
        source_errors = addendum_errors + validate_source_facts(
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
        setting = contract.get("release", {}).get("selftest_config")
        if not setting: raise ValueError("release.selftest_config is required")
        # v1.0 contracts used either a Kconfig symbol or the full historical
        # spelling ``CONFIG_X=n``.  Normalize both forms before producing or
        # validating the release override; new contracts should prefer just
        # the symbol, but a safe release cannot misinterpret legacy input.
        symbol, separator, requested = str(setting).partition("=")
        if separator and requested not in {"n", ""}:
            raise ValueError("release.selftest_config must request a disabled Kconfig symbol")
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
    for name in ("preflight", "design", "approval", "bind", "readiness", "subsystem", "release_smoke", "tier_c", "integration", "closure", "release"):
        graph.add_node(name, nodes.safe(name))
    graph.add_node("recover", nodes.recover)
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "preflight")
    for current, following in (("preflight", "design"), ("design", "approval"), ("approval", "bind"), ("bind", "readiness"), ("readiness", "subsystem"), ("integration", "tier_c"), ("closure", "release")):
        graph.add_conditional_edges(current, nodes.route_after(following), {following: following, "recover": "recover"})
    graph.add_conditional_edges("tier_c", lambda state: "recover" if state.get("failure") else nodes.tier_c_route(state), {"closure": "closure", "readiness": "readiness", "end": END, "recover": "recover"})
    graph.add_conditional_edges("subsystem", lambda state: "recover" if state.get("failure") else nodes.subsystem_route(state), {"readiness": "readiness", "release_smoke": "release_smoke", "integration": "integration", "recover": "recover"})
    graph.add_conditional_edges("release_smoke", nodes.route_after("integration"), {"integration": "integration", "recover": "recover"})
    graph.add_conditional_edges("release", nodes.route_after("end"), {"end": END, "recover": "recover"})
    recovery_targets = {name: name for name in ("preflight", "design", "approval", "bind", "readiness", "subsystem", "release_smoke", "tier_c", "integration", "closure", "release")}; recovery_targets["end"] = END
    graph.add_conditional_edges("recover", nodes.recovery_route, recovery_targets)
    return graph.compile(checkpointer=checkpointer)
