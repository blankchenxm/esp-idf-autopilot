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


def test_implementation_fact_does_not_require_evidence_locators_in_source(tmp_path: Path):
    source = tmp_path / "components" / "charger"; source.mkdir(parents=True)
    (source / "charger.c").write_text("#define BQ25180YBGR 1\n", encoding="utf-8")
    contract = {"implementation_facts": [{
        "id": "charger.part", "subsystem_id": "charger",
        "source_assertions": [{"path_glob": "components/charger/*.c", "required_tokens": [
            "BQ25180YBGR", "datasheet-extracted.txt", "sha256:" + "a" * 64,
        ]}],
    }]}
    assert validate_source_facts(tmp_path, contract) == []


def test_recursive_component_glob_includes_files_at_component_root(tmp_path: Path):
    source = tmp_path / "components" / "sensor"; source.mkdir(parents=True)
    (source / "sensor.c").write_text("#define SENSOR_ADDRESS 0x6A\n", encoding="utf-8")
    contract = {"implementation_facts": [{
        "id": "sensor.address", "subsystem_id": "sensor",
        "source_assertions": [{"path_glob": "components/sensor/**/*", "required_tokens": ["0x6A"]}],
    }]}
    assert validate_source_facts(tmp_path, contract) == []
