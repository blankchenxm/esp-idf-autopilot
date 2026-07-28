from pathlib import Path

from orchestrator.contract_consistency import validate_source_facts


def test_implementation_fact_binds_required_tokens_to_project_source(tmp_path: Path):
    source = tmp_path / "components" / "audio"; source.mkdir(parents=True)
    (source / "audio.c").write_text("#define RATE 48000\n", encoding="utf-8")
    contract = {"implementation_facts": [{
        "id": "audio.rate", "subsystem_id": "audio",
        "source_assertions": [{"path_glob": "components/audio/*.c", "required_tokens": ["RATE", "48000"]}],
    }]}
    assert validate_source_facts(tmp_path, contract) == []


def test_implementation_fact_rejects_missing_source_token_before_flash(tmp_path: Path):
    source = tmp_path / "main"; source.mkdir(parents=True)
    (source / "app.c").write_text("void app_main(void) {}\n", encoding="utf-8")
    contract = {"implementation_facts": [{
        "id": "audio.rate", "subsystem_id": "audio",
        "source_assertions": [{"path_glob": "main/*.c", "required_tokens": ["48000"]}],
    }]}
    assert "missing tokens" in validate_source_facts(tmp_path, contract)[0]
