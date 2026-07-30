from __future__ import annotations

import json
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.adapters.tier_c import TierCArtifactAdapter
from orchestrator.graph import HarnessNodes
from orchestrator.models import Receipt
from orchestrator.storage import ProjectStore
from test_contract_views_batches import v12_contract


def tier_c_item(source: str = "integration/final.wav") -> dict:
    return {
        "id": "TC1", "requirement_id": "R1", "owner": "probe",
        "instructions": "Listen to the final recording.", "test_id": "TC-AUDIO",
        "expected": {"confirmation": "confirmed"},
        "evidence_contract": {"observation": "human", "required_kinds": ["artifact", "user_confirmation"]},
        "why_not_tier_a_b": "Subjective sound quality is a product requirement.",
        "automated_checks_completed": ["WAV format", "duration", "channel RMS"],
        "physical_property": "subjective sound quality",
        "artifact_contract": {"access_method": "local_file", "source": source, "media_type": "audio/wav"},
        "retry_owners": ["probe"],
    }


def write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2); output.setsampwidth(2); output.setframerate(16000)
        output.writeframes(b"\0\0\1\0" * 160)


def test_wav_is_materialized_locally_with_machine_metadata(tmp_path: Path):
    project = tmp_path / "demo"; source = project / "integration" / "final.wav"; write_wav(source)
    store = ProjectStore(project); store.ensure()
    receipt, metadata = TierCArtifactAdapter(project, store, "run-1").materialize(tier_c_item())
    assert receipt.success and metadata["wav"]["channels"] == 2
    assert metadata["path"].startswith("execution/tier-c/run-1/TC1/")


def test_v12_tier_c_requires_admission_and_artifact_contract():
    contract = v12_contract(); item = tier_c_item(); contract["tier_c"] = [item]
    from orchestrator.validators import validate_contract
    assert validate_contract(contract) == []
    del item["why_not_tier_a_b"]
    assert any("why_not_tier_a_b" in error for error in validate_contract(contract))


def test_v12_tier_c_rejects_prose_instead_of_actionable_artifact_source():
    from orchestrator.validators import validate_contract

    contract = v12_contract()
    contract["tier_c"] = [tier_c_item("current-run captured WAV returned by the audio selftest")]
    assert any("actionable relative file path" in error for error in validate_contract(contract))


def test_v12_tier_c_accepts_only_supported_artifact_path_templates():
    from orchestrator.validators import validate_contract

    contract = v12_contract()
    contract["tier_c"] = [tier_c_item("integration/{run_id}/{item_id}.wav")]
    assert validate_contract(contract) == []
    contract["tier_c"][0]["artifact_contract"]["source"] = "integration/{owner}/result.wav"
    assert any("unsupported template fields" in error for error in validate_contract(contract))


def test_missing_physical_tier_c_artifact_is_a_user_gate_not_owner_repair():
    source = Path("orchestrator/graph.py").read_text(encoding="utf-8")
    assert "TIER_C_ARTIFACT_AWAITING_PHYSICAL_CAPTURE" in source
    assert "FailureDisposition.HARD_EXTERNAL_BLOCKER" in source


def test_confirmation_must_bind_presented_artifact_hash(tmp_path: Path):
    project = tmp_path / "projects" / "demo"; design = project / "design-package" / "rev-0001"
    design.mkdir(parents=True)
    contract = v12_contract(); contract["tier_c"] = [tier_c_item()]
    (design / "execution-contract.json").write_text(json.dumps(contract), encoding="utf-8")
    artifact = {"path": "execution/tier-c/run/TC1/a.wav", "sha256": "a" * 64, "size": 10, "media_type": "audio/wav"}
    state = {"project": "demo", "project_dir": str(project), "run_id": "run", "design_dir": str(design), "design_digest": "0" * 64, "hardware_identity": {}, "mode": "CONTINUOUS", "progress_seq": 3, "receipt_ids": [], "evidence_ids": [], "tier_c_artifacts": {"TC1": artifact}, "tier_c_artifact_receipts": {"TC1": "artifact-r"}}
    nodes = HarnessNodes(tmp_path)
    with patch("orchestrator.graph.interrupt", return_value={"TC1": {"status": "confirmed", "artifact_sha256": "b" * 64, "notes": "heard"}}):
        with pytest.raises(ValueError, match="not bound"):
            nodes.tier_c(state)
