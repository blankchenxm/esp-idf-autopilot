from pathlib import Path
from unittest.mock import patch
import pytest
from orchestrator.graph import build_graph
from orchestrator.graph import HarnessNodes
from orchestrator.models import (
    Diagnostic,
    FailureCategory,
    FailureDisposition,
    RunMode,
)


def test_graph_contains_mandatory_gates_in_code():
    graph = build_graph(Path(__file__).resolve().parents[1]).get_graph(); nodes = set(graph.nodes); edges = {(edge.source, edge.target) for edge in graph.edges}
    assert {
        "initialize", "pause_control", "preflight", "design", "approval",
        "invariant_gate", "schema_gate", "operation_authority", "bind",
        "verification_batches", "implementation_materialize",
        "implementation_completeness",
        "source_validate", "configure", "build", "flash", "observe",
        "evaluate", "evidence_commit", "component_architecture",
        "production_composition", "integration_prepare",
        "integration_configure", "integration_build", "integration_flash",
        "integration_observe", "integration_evaluate",
        "integration_evidence_commit", "tier_c_artifact_materialization",
        "tier_c", "closure", "release_prepare", "release_fullclean",
        "release_configure", "release_build", "release_flash",
        "release_observe", "release_validate", "recover",
    } <= nodes
    assert ("design", "approval") in edges
    assert ("approval", "invariant_gate") in edges
    assert ("operation_authority", "bind") in edges
    assert ("bind", "verification_batches") in edges
    assert (
        "implementation_materialize", "implementation_completeness"
    ) in edges
    assert ("configure", "build") in edges and ("observe", "evaluate") in edges
    assert ("integration_evidence_commit", "tier_c_artifact_materialization") in edges
    assert ("tier_c", "closure") in edges
    assert ("closure", "release_prepare") in edges
    assert ("release_fullclean", "release_configure") in edges
    assert any(edge.target == "recover" for edge in graph.edges)


def test_isolated_verification_workspace_uses_short_project_local_path(tmp_path: Path):
    project = tmp_path / "projects" / "crumb"
    workspace = HarnessNodes(tmp_path)._verification_workspace(
        project,
        {"run_id": "run-12345678abcdef", "batch_index": 12},
        "charger_monitor",
    )

    assert workspace == project / ".v" / "12345678" / "b12-charger_monitor"
    assert "execution" not in workspace.parts


def test_verification_transaction_key_binds_isolated_workspace(tmp_path: Path):
    project = tmp_path / "projects" / "crumb"
    project.mkdir(parents=True)
    nodes = HarnessNodes(tmp_path)
    state = {
        "project_dir": str(project),
        "run_id": "run-test",
        "design_digest": "0" * 64,
        "hardware_identity": {},
    }

    old_key = nodes._transaction_key(
        state, "verification_configure", batch="charger", workspace="execution/verification/old/build"
    )
    short_key = nodes._transaction_key(
        state, "verification_configure", batch="charger", workspace=".v/1234/b2-charger/build"
    )

    assert old_key != short_key


def test_recovery_retries_when_harness_material_changed(tmp_path: Path):
    project = tmp_path / "projects" / "crumb"
    state = {
        "project": "crumb",
        "project_dir": str(project),
        "run_id": "run-test",
        "design_digest": "0" * 64,
        "subsystem_index": 0,
        "failure": {
            "category": "build",
            "summary": "verification build failed",
            "owner": "charger_monitor",
            "retryable": True,
            "fingerprint": "old-fingerprint",
            "evidence": [],
        },
        "diagnostic": Diagnostic(
            code="SUBSYSTEM_BUILD_FAILED",
            cause=FailureCategory.BUILD,
            disposition=FailureDisposition.REPAIR_INTERNAL,
            responsible_party="implementation_agent",
            affected_owner="charger_monitor",
            summary="verification build failed",
            material_fingerprint="obsolete-harness-fingerprint",
        ).model_dump(mode="json"),
        "failed_node": "subsystem",
        "failure_attempts": {},
        "receipt_ids": [],
        "progress_seq": 1,
    }
    nodes = HarnessNodes(tmp_path)
    nodes._projection = lambda *_args, **_kwargs: None
    nodes._event = lambda *_args, **_kwargs: None

    updates = nodes.recover(state)

    assert updates["failure"] is None
    assert updates["recovery_target"] == "subsystem"
    assert "mode" not in updates


def test_tier_c_interrupt_projects_waiting_state_before_interrupt(tmp_path: Path):
    project = tmp_path / "projects" / "demo"; design = project / "design-package" / "rev-0001"
    design.mkdir(parents=True)
    (design / "execution-contract.json").write_text('{"tier_c":[{"id":"TC1","requirement_id":"R1","instructions":"observe"}]}', encoding="utf-8")
    state = {"project": "demo", "project_dir": str(project), "run_id": "run", "design_dir": str(design), "mode": "CONTINUOUS", "progress_seq": 3}
    nodes = HarnessNodes(tmp_path)
    with patch("orchestrator.graph.interrupt", side_effect=RuntimeError("interrupt")):
        try:
            nodes.tier_c(state)
        except RuntimeError:
            pass
    import json
    projection = json.loads((project / "execution" / "run-state.json").read_text(encoding="utf-8"))
    assert projection["mode"] == RunMode.WAITING_TIER_C.value
    assert projection["cursor"] == "STAGE 2.9:tier-c:waiting"


def test_spec_interrupt_projects_waiting_state_before_interrupt(
    tmp_path: Path,
):
    import json

    project = tmp_path / "projects" / "demo"
    design = project / "design-package" / "rev-0001"
    design.mkdir(parents=True)
    (design / "spec.md").write_text("# Review\n", encoding="utf-8")
    (design / "approval.json").write_text(
        json.dumps({"status": "PENDING", "design_digest": "0" * 64}),
        encoding="utf-8",
    )
    state = {
        "project": "demo",
        "project_dir": str(project),
        "run_id": "run",
        "design_dir": str(design),
        "design_digest": "0" * 64,
        "mode": "CONTINUOUS",
        "progress_seq": 3,
    }
    nodes = HarnessNodes(tmp_path)
    with patch(
        "orchestrator.graph.interrupt", side_effect=RuntimeError("interrupt")
    ):
        with pytest.raises(RuntimeError):
            nodes.approval(state)
    projection = json.loads(
        (project / "execution" / "run-state.json").read_text(encoding="utf-8")
    )
    assert projection["mode"] == RunMode.WAITING_SPEC.value
    assert projection["cursor"] == "STAGE 1.4:approval:waiting"
