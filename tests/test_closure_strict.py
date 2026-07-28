from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.graph import HarnessNodes
from orchestrator.errors import DiagnosticFailure
from orchestrator.models import Evidence, Tier, Verdict
from orchestrator.storage import ProjectStore
from tests.test_design_contract import valid_contract


def test_requirement_coverage_cannot_hide_skipped_declared_test(
    tmp_path: Path,
):
    project_dir = tmp_path / "projects" / "strict"
    design_dir = project_dir / "design-package" / "rev-0001"
    design_dir.mkdir(parents=True)
    contract = valid_contract("strict")
    (design_dir / "execution-contract.json").write_text(
        json.dumps(contract), encoding="utf-8"
    )
    store = ProjectStore(project_dir)
    store.ensure()
    # Broad evidence covers R1, but neither the declared DR1 verification nor
    # INT-READY integration identity has executed.
    evidence = Evidence(
        evidence_id="e-broad",
        run_id="run-1",
        design_digest="1" * 64,
        requirement_ids=["R1"],
        test_id="UNDECLARED_BROAD_CHECK",
        owner="probe",
        tier=Tier.A,
        expected={"marker": "READY"},
        actual={"marker": "READY"},
        verdict=Verdict.PASS,
        receipt_ids=["receipt-1"],
    )
    store.write_evidence(evidence, "test")

    with pytest.raises(DiagnosticFailure, match="test-level PASS evidence"):
        HarnessNodes(tmp_path).closure({
            "project": "strict",
            "project_dir": str(project_dir),
            "design_dir": str(design_dir),
            "run_id": "run-1",
            "design_digest": "1" * 64,
            "progress_seq": 1,
        })
