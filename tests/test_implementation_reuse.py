from pathlib import Path

from orchestrator.implementation_reuse import assess_existing_implementation


def _component(project: Path, owner: str = "sensor") -> None:
    (project / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.16)\n")
    component = project / "components" / owner
    (component / "include").mkdir(parents=True)
    (component / "CMakeLists.txt").write_text("idf_component_register(SRCS \"sensor.c\" INCLUDE_DIRS \"include\")\n")
    (component / "sensor.c").write_text("void sensor_init(void) { /* 0x6A */ }\n")
    (component / "include" / "sensor.h").write_text("#pragma once\n")


def test_existing_compliant_component_is_reused_without_agent(tmp_path: Path):
    _component(tmp_path)
    contract = {"implementation_facts": [{
        "id": "IF_SENSOR", "subsystem_id": "sensor", "source_assertions": [{
            "path_glob": "components/sensor/**/*", "required_tokens": ["0x6A"],
        }],
    }]}

    decision = assess_existing_implementation(tmp_path, "sensor", contract, [])

    assert decision.reusable is True
    assert decision.source_digest
    assert decision.reasons == []


def test_existing_component_with_failed_contract_assertion_requires_minimal_repair(tmp_path: Path):
    _component(tmp_path)
    contract = {"implementation_facts": [{
        "id": "IF_SENSOR", "subsystem_id": "sensor", "source_assertions": [{
            "path_glob": "components/sensor/**/*", "required_tokens": ["required-token"],
        }],
    }]}

    decision = assess_existing_implementation(tmp_path, "sensor", contract, [])

    assert decision.reusable is False
    assert "missing tokens" in decision.reasons[0]


def test_component_selftest_must_be_composed_by_main(tmp_path: Path):
    _component(tmp_path)
    component = tmp_path / "components" / "sensor"
    (component / "sensor.c").write_text(
        "void sensor_init(void) {}\nvoid sensor_selftest(void) {}\n"
    )
    contract = {"implementation_facts": []}

    decision = assess_existing_implementation(tmp_path, "sensor", contract, [])

    assert decision.reusable is False
    assert decision.reasons == [
        "component 'sensor' selftest is not composed by main/"
    ]

    main = tmp_path / "main"
    main.mkdir()
    (main / "app.c").write_text("void app_main(void) { sensor_selftest(); }\n")
    assert assess_existing_implementation(
        tmp_path, "sensor", contract, []
    ).reusable is True
