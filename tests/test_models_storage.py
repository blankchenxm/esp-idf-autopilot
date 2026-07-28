from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.models import (
    Diagnostic,
    DiagnosticSeverity,
    Evidence,
    FailureCategory,
    FailureDisposition,
    Receipt,
    Tier,
    Verdict,
)
from orchestrator.storage import ProjectStore, atomic_write_json, digest


def test_receipt_rejects_failed_without_failure():
    with pytest.raises(ValidationError):
        Receipt(receipt_id="r", run_id="run", operation="x", started_at="a", finished_at="b", success=False)


def test_receipt_and_evidence_are_immutable(tmp_path: Path):
    store = ProjectStore(tmp_path); store.ensure()
    receipt = Receipt(receipt_id="r1", run_id="run", operation="x", started_at="a", finished_at="b", success=True)
    store.write_receipt(receipt, "test")
    with pytest.raises(FileExistsError): store.write_receipt(receipt, "test")
    evidence = Evidence(evidence_id="e1", run_id="run", design_digest="0" * 64, requirement_ids=["R1"], test_id="T1", owner="x", tier=Tier.A, expected={"marker": "ok"}, actual={"marker": "ok"}, verdict=Verdict.PASS, receipt_ids=["r1"])
    store.write_evidence(evidence, "test")
    with pytest.raises(FileExistsError): store.write_evidence(evidence, "test")
    assert json.loads((store.receipts / "test" / "r1.json").read_text(encoding="utf-8"))["project_id"] == tmp_path.name
    assert json.loads((store.evidence / "test" / "e1.json").read_text(encoding="utf-8"))["project_id"] == tmp_path.name


def test_atomic_json_is_complete(tmp_path: Path):
    path = tmp_path / "nested" / "value.json"; atomic_write_json(path, {"n": 1})
    assert json.loads(path.read_text(encoding="utf-8"))["n"] == 1


def test_digest_is_key_order_independent():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


def test_diagnostic_separates_technical_cause_from_policy_disposition():
    value = Diagnostic(
        code="BUILD_COMPILE_FAILED",
        cause=FailureCategory.BUILD,
        disposition=FailureDisposition.REPAIR_INTERNAL,
        severity=DiagnosticSeverity.BLOCKING,
        responsible_party="implementation_agent",
        affected_owner="sensor",
        summary="component compilation failed",
    )
    assert value.cause == FailureCategory.BUILD
    assert value.disposition == FailureDisposition.REPAIR_INTERNAL


def test_receipt_reuse_requires_matching_key_and_untampered_artifacts(
    tmp_path: Path,
):
    from orchestrator.storage import file_ref

    store = ProjectStore(tmp_path)
    store.ensure()
    artifact = tmp_path / "logs" / "run" / "one.log"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("stable\n", encoding="utf-8")
    receipt = Receipt(
        receipt_id="tx-1",
        run_id="run",
        operation="build",
        started_at="a",
        finished_at="b",
        success=True,
        inputs={"idempotency_key": "key-1"},
        artifacts=[file_ref(artifact, tmp_path, "text/plain")],
    )
    store.write_receipt(receipt, "build")
    assert store.find_successful_receipt("build", "key-1") is not None
    assert store.find_successful_receipt("build", "other") is None
    artifact.write_text("changed\n", encoding="utf-8")
    assert store.find_successful_receipt("build", "key-1") is None
