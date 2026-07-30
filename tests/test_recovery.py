from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from langgraph.types import Command

from orchestrator.graph import HarnessNodes
from orchestrator.errors import ReceiptFailure
from orchestrator.models import Diagnostic, Failure, FailureCategory, FailureDisposition, Receipt, RunMode
from orchestrator.cli import _resume_input


def state(root: Path) -> dict:
    project = root / "projects" / "demo"; project.mkdir(parents=True)
    return {"project": "demo", "project_dir": str(project), "run_id": "run-1", "design_dir": str(project / "missing-design"), "mode": RunMode.CONTINUOUS.value, "cursor": "x", "next_action": "x", "progress_seq": 1, "receipt_ids": [], "evidence_ids": [], "failure_attempts": {}, "subsystems": ["probe"], "subsystem_index": 0}


def test_safe_node_converts_exception_to_persisted_failure(tmp_path: Path):
    nodes = HarnessNodes(tmp_path); current = state(tmp_path)
    updates = nodes.safe("design")(current)
    assert updates["failed_node"] == "design" and updates["failure"]["fingerprint"]
    receipts = list((Path(current["project_dir"]) / "execution" / "receipts" / "failure").glob("*.json"))
    assert len(receipts) == 1 and json.loads(receipts[0].read_text(encoding="utf-8"))["success"] is False


def test_safe_node_preserves_adapter_failure_category_and_disposition(tmp_path: Path):
    nodes = HarnessNodes(tmp_path)
    current = state(tmp_path)
    receipt = Receipt(
        receipt_id="build-source",
        run_id="run-1",
        operation="build",
        started_at="a",
        finished_at="b",
        success=False,
        failure=Failure(
            category=FailureCategory.BUILD,
            summary="build exited 1",
            owner="probe",
        ),
    )

    def operation(_state):
        raise ReceiptFailure(receipt)

    nodes.synthetic_build = operation
    updates = nodes.safe("synthetic_build")(current)

    assert updates["failure"]["category"] == FailureCategory.BUILD.value
    assert updates["diagnostic"]["disposition"] == FailureDisposition.REPAIR_INTERNAL.value
    assert updates["receipt_ids"][0] == "build-source"


def test_design_consistency_failure_becomes_internal_fault(tmp_path: Path):
    nodes = HarnessNodes(tmp_path); current = state(tmp_path); failed = nodes.safe("design")(current)
    updates = nodes.recover({**current, **failed})
    assert updates["mode"] == RunMode.FAULTED.value
    assert updates["blocker"]["kind"] == "internal_fault"


def test_repeated_unchanged_failure_becomes_stall(tmp_path: Path):
    nodes = HarnessNodes(tmp_path); current = state(tmp_path); signature = "f" * 64
    current.update({"failed_node": "subsystem", "failure": {"category": "serial", "summary": "port busy", "fingerprint": signature, "evidence": [{"path": "logs/failure.log"}]}, "failure_attempts": {signature: 3}})
    updates = nodes.recover(current)
    assert updates["mode"] == RunMode.FAULTED.value and updates["blocker"]["kind"] == "internal_stall"


def test_firmware_selftest_uses_bounded_harness_capture_default(tmp_path: Path):
    nodes = HarnessNodes(tmp_path)
    rows = [{"expected": {"marker": "PASS"}}]

    assert nodes._serial_timeout(rows, {"kind": "firmware_selftest"}) == 60
    assert nodes._serial_timeout(rows, {"kind": "normal_boot"}) == 15
    assert nodes._serial_timeout(
        [{"timeout_s": 37}], {"kind": "firmware_selftest"}
    ) == 37


def test_complete_projection_never_retains_historical_blocker(tmp_path: Path):
    nodes = HarnessNodes(tmp_path); current = state(tmp_path)
    current["blocker"] = {"kind": "stall", "summary": "old", "evidence": "x", "needed": "change"}
    nodes._projection(current, {"mode": RunMode.COMPLETE.value, "cursor": "STAGE 3.6:release:pass"})
    value = json.loads((Path(current["project_dir"]) / "execution" / "run-state.json").read_text(encoding="utf-8"))
    assert value["mode"] == "COMPLETE" and value["blocker"] is None


def test_missing_owner_source_runs_the_materialization_adapter(tmp_path: Path):
    current = state(tmp_path)
    project = Path(current["project_dir"])
    design = project / "design-package" / "rev-0001"
    design.mkdir(parents=True)
    (design / "execution-contract.json").write_text("{}", encoding="utf-8")
    current["design_dir"] = str(design)
    nodes = HarnessNodes(tmp_path)

    def materialize(_action):
        (project / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.16)\n", encoding="utf-8")
        component = project / "components" / "probe" / "include"
        component.mkdir(parents=True)
        (component / "probe.h").write_text("#pragma once\n", encoding="utf-8")
        (component.parent / "CMakeLists.txt").write_text("idf_component_register(SRCS \\\"probe.c\\\" INCLUDE_DIRS \\\"include\\\")\n", encoding="utf-8")
        (component.parent / "probe.c").write_text('void probe(void) {} // READY\n', encoding="utf-8")
        return SimpleNamespace(success=True, receipt_id="agent-1", failure=None)

    with patch("orchestrator.graph.AgentAdapter.execute", side_effect=materialize):
        receipts = nodes._materialize_owner_source(
            current, project, nodes._context(current)[1], "probe",
            [{"expected": {"marker": "READY"}}],
        )
    assert receipts == ["agent-1"]


def test_compliant_existing_source_never_runs_materialization_adapter(tmp_path: Path):
    current = state(tmp_path)
    project = Path(current["project_dir"])
    design = project / "design-package" / "rev-0001"
    design.mkdir(parents=True)
    (design / "execution-contract.json").write_text("{}", encoding="utf-8")
    current["design_dir"] = str(design)
    (project / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.16)\n")
    component = project / "components" / "probe"
    (component / "include").mkdir(parents=True)
    (component / "CMakeLists.txt").write_text("idf_component_register(SRCS \"probe.c\" INCLUDE_DIRS \"include\")\n")
    (component / "probe.c").write_text("void probe(void) {}\n")
    (component / "include" / "probe.h").write_text("#pragma once\n")

    with patch("orchestrator.graph.AgentAdapter.execute") as execute:
        receipts = HarnessNodes(tmp_path)._materialize_owner_source(
            current, project, HarnessNodes(tmp_path)._context(current)[1], "probe", []
        )

    assert receipts == []
    execute.assert_not_called()


def test_repairable_build_failure_runs_owner_patch_before_retry(tmp_path: Path):
    current = state(tmp_path)
    project = Path(current["project_dir"])
    component = project / "components" / "probe"
    component.mkdir(parents=True)
    source = component / "probe.c"
    source.write_text("int probe(void) { return 0; }\n", encoding="utf-8")
    design = project / "design-package" / "rev-0001"
    design.mkdir(parents=True)
    (design / "execution-contract.json").write_text("{}", encoding="utf-8")
    current["design_dir"] = str(design)
    for area in ("requirements", "connections"):
        path = tmp_path / area / "demo.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# demo\n", encoding="utf-8")
    signature = "a" * 64
    current.update({
        "failed_node": "subsystem",
        "failure": {
            "category": FailureCategory.BUILD.value,
            "summary": "build exited 1",
            "owner": "probe",
            "retryable": True,
            "fingerprint": signature,
            "evidence": [],
        },
        "diagnostic": Diagnostic(
            code="BUILD_COMPILE_FAILED",
            cause=FailureCategory.BUILD,
            disposition=FailureDisposition.REPAIR_INTERNAL,
            responsible_party="implementation_agent",
            affected_owner="probe",
            subsystem_id="probe",
            summary="build exited 1",
        ).model_dump(mode="json"),
    })

    def repair(_action):
        source.write_text("int probe(void) { return 1; }\n", encoding="utf-8")
        return Receipt(
            receipt_id="agent-repair",
            run_id="run-1",
            operation="implement_or_repair",
            started_at="a",
            finished_at="b",
            success=True,
        )

    with patch("orchestrator.graph.AgentAdapter.execute", side_effect=repair) as execute:
        updates = HarnessNodes(tmp_path).recover(current)

    assert execute.call_args.args[0].owner == "probe"
    assert updates["recovery_target"] == "subsystem"
    assert updates["failure"] is None
    assert "agent-repair" in updates["receipt_ids"]


def test_bootstrap_subsystem_materializes_owner_before_source_gate():
    source = Path("orchestrator/graph.py").read_text(encoding="utf-8")
    bootstrap = source.split("if not rows:", 1)[1].split("for owner in owners:", 1)[1]
    assert "_materialize_owner_source" in bootstrap


def test_audio_authority_repair_receives_versioned_product_defaults():
    assert "audio-pcm-v1" in HarnessNodes._product_default_instruction("audio_pipeline")
    assert "48_000 Hz" in HarnessNodes._product_default_instruction("audio_pipeline")


def test_project_kconfig_override_must_be_declared(tmp_path: Path):
    project = tmp_path / "demo"
    component = project / "components" / "owner"
    component.mkdir(parents=True)
    (component / "Kconfig").write_text(
        "config DEMO_TEST\n    bool \"test\"\n", encoding="utf-8"
    )
    assert HarnessNodes._missing_project_kconfig_overrides(
        project, {"CONFIG_DEMO_TEST": "y"}
    ) == []
    assert HarnessNodes._missing_project_kconfig_overrides(
        project, {"CONFIG_DEMO_MISSING": "y"}
    ) == ["CONFIG_DEMO_MISSING"]


def test_material_change_resume_clears_the_historical_blocker(tmp_path: Path):
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True)
    snapshot = SimpleNamespace(
        tasks=[],
        values={
            "mode": "BLOCKED", "recovery_target": "subsystem",
            "material_fingerprint": "old", "progress_seq": 2,
            "blocker": {"kind": "stall"},
        },
    )
    with patch("orchestrator.policies.material_fingerprint", return_value="new"):
        command = _resume_input(snapshot, {}, None, Command, project)
    assert command.update["mode"] == "CONTINUOUS"
    assert command.update["blocker"] is None
