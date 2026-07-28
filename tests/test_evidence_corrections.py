from __future__ import annotations

from pathlib import Path

from orchestrator.corrections import invalidated_evidence_ids, record_evidence_correction
from orchestrator.models import Evidence, RunStateProjection, Tier, Verdict
from orchestrator.storage import ProjectStore


def test_correction_supersedes_without_editing_evidence(tmp_path: Path):
    store = ProjectStore(tmp_path); store.ensure()
    evidence = Evidence(evidence_id="e1", run_id="run", design_digest="0" * 64, requirement_ids=["R1"], test_id="TC1", owner="sensor", tier=Tier.C, expected={"confirmation": "confirmed"}, actual={"confirmation": "claimed"}, verdict=Verdict.PASS, receipt_ids=["r1"])
    path = store.write_evidence(evidence, "tier-c"); before = path.read_bytes()
    correction = record_evidence_correction(tmp_path, ["e1"], "artifact was not available to the observer")
    assert correction["correction_id"] and invalidated_evidence_ids(tmp_path) == {"e1"}
    assert path.read_bytes() == before


def test_correction_demotes_affected_complete_projection(tmp_path: Path):
    store = ProjectStore(tmp_path); store.ensure()
    evidence = Evidence(evidence_id="e1", run_id="run", design_digest="0" * 64, requirement_ids=["R1"], test_id="TC1", owner="sensor", tier=Tier.C, expected={}, actual={}, verdict=Verdict.PASS, receipt_ids=["r1"])
    store.write_evidence(evidence, "tier-c")
    store.project_state(RunStateProjection(run_id="run", mode="COMPLETE", cursor="STAGE 3.6:release:pass", next_action="none", progress_fingerprint="0" * 64, release_verified=True, closure={"required_total": 1, "pass": 1}, release_evidence={"closure_pass": True, "selftest_disabled": True, "build_log": "b", "flash_log": "f", "serial_log": "s", "runtime_marker": "ok", "firmware_sha256": "0" * 64, "firmware_binary": "app.bin", "build_receipt_id": "b", "flash_receipt_id": "f", "serial_receipt_id": "s"}))
    record_evidence_correction(tmp_path, ["e1"], "not observed")
    value = __import__("json").loads((tmp_path / "execution" / "run-state.json").read_text(encoding="utf-8"))
    assert value["mode"] == "BLOCKED" and value["blocker"]["kind"] == "evidence_correction"
