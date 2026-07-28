from pathlib import Path
from unittest.mock import patch
import pytest
from orchestrator.graph import build_graph
from orchestrator.graph import HarnessNodes
from orchestrator.models import RunMode


def test_graph_contains_mandatory_gates_in_code():
    graph = build_graph(Path(__file__).resolve().parents[1]).get_graph(); nodes = set(graph.nodes); edges = {(edge.source, edge.target) for edge in graph.edges}
    assert {"initialize", "pause_control", "preflight", "design", "approval", "bind", "readiness", "subsystem", "tier_c", "integration", "closure", "release", "recover"} <= nodes
    assert ("design", "approval") in edges and ("bind", "readiness") in edges
    assert ("readiness", "subsystem") in edges and ("closure", "release") in edges
    assert ("integration", "tier_c") in edges and ("tier_c", "closure") in edges
    assert any(edge.target == "recover" for edge in graph.edges)


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
