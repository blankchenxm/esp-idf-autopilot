from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from orchestrator.component_architecture import validate_component_architecture
from orchestrator.execution_jobs import read_execution_job
from orchestrator.failure_lineage import (
    failure_lineage_id,
    relevant_material_revision,
    validate_recovery_admission,
)
from orchestrator.graph import build_graph
from orchestrator.invariants import (
    GraphView,
    IMPLEMENTED_VALIDATORS,
    load_invariant_registry,
    load_scenario_registry,
    validate_graph_conformance,
    validate_invariant_registry,
    validate_transaction_topology,
)
from orchestrator.operation_authority import (
    compile_operation_authority,
    validate_implementation_completeness,
)
from orchestrator.production_composition import (
    parse_runtime_observations,
    validate_production_composition,
)
from orchestrator.project_hygiene import validate_generated_file_hygiene
from orchestrator.run_metrics import token_reduction_percent
from orchestrator.models import ReleaseEvidence
from orchestrator.runtime_paths import ProjectRuntime
from orchestrator.schema_capabilities import (
    current_schema_version,
    require_executable_schema,
)
from orchestrator.validators import (
    validate_release_runtime_text,
    validate_release_transaction,
)
from orchestrator.storage import atomic_write_json
from orchestrator.tier_c_producer import (
    validate_tier_c_contract,
    validate_tier_c_producer_chain,
)
from orchestrator.verification_plan import normalize_verification_images


def operation_contract() -> dict:
    return {
        "schema_version": "1.7",
        "subsystems": [{"id": "driver"}],
        "operations": [{
            "operation_id": "op.read",
            "owner": "driver",
            "kind": "hardware_transport",
            "risk": "reversible",
            "required_capabilities": ["cap.i2c.read"],
            "authority_sources": [{
                "source_type": "datasheet",
                "authority_ref": "sheet:read",
                "provider_receipt_id": "receipt-sheet",
                "capability_ids": ["cap.i2c.read"],
            }],
            "implementation_assertions": [{
                "path_glob": "components/driver/*.c",
                "symbol": "driver_read",
            }],
            "runtime_probe": {"test_id": "T-READ", "observation": "serial"},
            "consumers": ["step:read"],
        }],
        "architecture": {
            "runtime_flow": {"steps": [{"id": "read"}]},
        },
    }


def production_contract() -> dict:
    contract = operation_contract()
    contract["architecture"]["runtime_flow"] = {
        "entrypoint": {"owner": "app", "symbol": "app_main"},
        "steps": [{
            "id": "read",
            "owner": "driver",
            "symbol": "driver_read",
            "operation_ids": ["op.read"],
        }],
    }
    contract["integration"] = {
        "production_scenarios": [{
            "scenario_id": "normal-read",
            "entrypoint": "app_main",
            "runtime_step_ids": ["read"],
            "operation_ids": ["op.read"],
            "observation_points": ["serial"],
            "expected": {"marker": "READ_OK"},
        }]
    }
    return contract


def test_scn_020_registry_and_current_graph_are_machine_conformant():
    repo = Path(__file__).resolve().parents[1]
    assert validate_graph_conformance(build_graph(repo)) == []
    scenarios = load_scenario_registry()
    assert len(scenarios["scenarios"]) == 21
    assert len({item["scenario_id"] for item in scenarios["scenarios"]}) == 21


def test_scn_020_registry_rejects_missing_producer_validator_and_scenario():
    registry = load_invariant_registry()
    broken = json.loads(json.dumps(registry))
    del broken["rules"][0]["producer_node"]
    assert any("producer_node" in item for item in validate_invariant_registry(broken))
    broken = json.loads(json.dumps(registry))
    broken["rules"][0]["validator"] = "prose_only"
    errors = validate_invariant_registry(
        broken,
        validator_names=IMPLEMENTED_VALIDATORS,
        scenario_ids={
            item["scenario_id"]
            for item in load_scenario_registry()["scenarios"]
        },
    )
    assert any("not implemented" in item for item in errors)


def test_scn_015_removed_transaction_edge_fails_conformance():
    graph = build_graph(Path(__file__).resolve().parents[1]).get_graph()
    view = GraphView(
        frozenset(graph.nodes),
        frozenset(
            (edge.source, edge.target)
            for edge in graph.edges
            if (edge.source, edge.target) != ("flash", "observe")
        ),
    )
    assert any(
        "observe" in error for error in validate_transaction_topology(view)
    )


def test_scn_016_legacy_schema_requires_new_revision():
    errors = require_executable_schema({"schema_version": "1.6"})
    assert current_schema_version() == "1.7"
    assert errors and errors[0].startswith("DESIGN_REVISION_REQUIRED:")
    assert require_executable_schema({"schema_version": "1.7"}) == []


def test_scn_001_operation_authority_never_uses_owner_blanket():
    contract = operation_contract()
    assert compile_operation_authority(contract).passed
    contract["operations"][0]["authority_sources"][0]["capability_ids"] = [
        "all-owner-operations"
    ]
    result = compile_operation_authority(contract)
    assert not result.passed
    assert "found 0" in result.errors[0]


def test_scn_002_unsupported_operation_fails_before_build(tmp_path: Path):
    project = tmp_path / "project"
    source = project / "components" / "driver" / "driver.c"
    source.parent.mkdir(parents=True)
    source.write_text(
        "int driver_read(void) {\n return ESP_ERR_NOT_SUPPORTED;\n}\n",
        encoding="utf-8",
    )
    errors = validate_implementation_completeness(
        project, operation_contract()
    )
    assert any("ESP_ERR_NOT_SUPPORTED" in item for item in errors)


def test_scn_003_and_004_production_requires_reachable_correlated_steps(
    tmp_path: Path,
):
    project = tmp_path / "project"
    (project / "main").mkdir(parents=True)
    (project / "components" / "driver").mkdir(parents=True)
    (project / "main" / "app.c").write_text(
        "void app_main(void) { }\n", encoding="utf-8"
    )
    (project / "components" / "driver" / "driver.c").write_text(
        "int driver_read(void) { return 0; }\n", encoding="utf-8"
    )
    contract = production_contract()
    assert any(
        "unreachable" in item
        for item in validate_production_composition(project, contract)
    )
    (project / "main" / "app.c").write_text(
        "int driver_read(void);\nvoid app_main(void) { driver_read(); }\n",
        encoding="utf-8",
    )
    assert validate_production_composition(project, contract) == []
    assert any(
        "omit" in item
        for item in validate_production_composition(
            project, contract, runtime_observations=[]
        )
    )
    observations = parse_runtime_observations(
        "HARNESS_STEP step=read operation=op.read correlation=txn-1"
    )
    assert validate_production_composition(
        project, contract, runtime_observations=observations
    ) == []


def test_production_queue_edge_requires_source_and_runtime_observation(
    tmp_path: Path,
):
    project = tmp_path / "project"
    (project / "main").mkdir(parents=True)
    source = project / "main" / "app.c"
    source.write_text(
        "void sink(void) {}\n"
        "void producer(void) { sink(); /* QUEUE_SEND */ }\n"
        "void app_main(void) { producer(); }\n",
        encoding="utf-8",
    )
    contract = production_contract()
    contract["operations"].append({
        **contract["operations"][0],
        "operation_id": "op.sink",
        "implementation_assertions": [{
            "path_glob": "main/*.c", "symbol": "sink"
        }],
        "consumers": ["step:sink"],
    })
    contract["architecture"]["runtime_flow"] = {
        "entrypoint": {"owner": "app", "symbol": "app_main"},
        "steps": [
            {
                "id": "producer", "owner": "driver",
                "symbol": "producer", "operation_ids": ["op.read"],
            },
            {
                "id": "sink", "owner": "driver",
                "symbol": "sink", "operation_ids": ["op.sink"],
            },
        ],
        "edges": [{
            "from_step": "producer",
            "to_step": "sink",
            "kind": "queue",
            "source_assertions": [{
                "path_glob": "main/*.c",
                "required_tokens": ["QUEUE_SEND"],
            }],
        }],
    }
    contract["integration"]["production_scenarios"][0].update({
        "runtime_step_ids": ["producer", "sink"],
        "operation_ids": ["op.read", "op.sink"],
    })
    steps_only = parse_runtime_observations(
        "HARNESS_STEP step=producer operation=op.read correlation=txn\n"
        "HARNESS_STEP step=sink operation=op.sink correlation=txn\n"
    )
    assert any(
        "omit declared edges" in error
        for error in validate_production_composition(
            project, contract, runtime_observations=steps_only
        )
    )
    complete = parse_runtime_observations(
        "HARNESS_STEP step=producer operation=op.read correlation=txn\n"
        "HARNESS_STEP step=sink operation=op.sink correlation=txn\n"
        "HARNESS_EDGE from=producer to=sink kind=queue correlation=txn\n"
    )
    assert validate_production_composition(
        project, contract, runtime_observations=complete
    ) == []


def test_scn_005_tier_c_local_artifact_requires_final_image_producer():
    contract = operation_contract()
    contract["integration"] = {"tests": [{"id": "produce"}]}
    item = {
        "id": "TC1",
        "artifact_contract": {"access_method": "local_file"},
    }
    contract["tier_c"] = [item]
    assert validate_tier_c_contract(contract)
    item.update({
        "producer_phase": "integration",
        "producer_test_id": "produce",
        "producer_operation_ids": ["op.read"],
        "delivery_method": "local_generation",
        "correlation_key": "object-1",
        "producer_receipt_kinds": ["artifact_producer"],
        "artifact_validation": {"min_bytes": 1},
    })
    assert validate_tier_c_contract(contract) == []
    stale = [{
        "run_id": "old",
        "success": True,
        "operation": "artifact_producer",
        "outputs": {
            "design_digest": "d",
            "firmware_sha256": "f",
            "producer_test_id": "produce",
            "correlation_key": "object-1",
        },
    }]
    assert validate_tier_c_producer_chain(
        item, stale, run_id="new", design_digest="d", firmware_sha256="f"
    )


def test_scn_006_and_007_batching_tracks_images_not_components():
    base = {
        "schema_version": "1.7",
        "subsystems": [
            {
                "id": "a", "execution_role": "component",
                "batch_compatible": True, "isolation_required": False,
                "hardware_resources": ["gpio:1"],
            },
            {
                "id": "b", "execution_role": "component",
                "batch_compatible": True, "isolation_required": False,
                "hardware_resources": ["gpio:2"],
            },
        ],
        "verification": [
            {
                "owner": owner, "test_id": f"T-{owner}", "tier": "A",
                "test_setup": {"kind": "normal_boot"},
                "stimulus": {"kind": "none"},
            }
            for owner in ("a", "b")
        ],
    }
    images = normalize_verification_images(base)
    assert len(images) == 1
    assert images[0].owners == ("a", "b")
    base["verification"][1]["test_setup"] = {
        "kind": "firmware_selftest",
        "kconfig_overrides": {"CONFIG_FIXTURE": "y"},
        "isolated_build": True,
    }
    assert len(normalize_verification_images(base)) == 2


def test_scn_013_and_014_lineage_is_summary_independent_and_model_sparse():
    lineage_a = failure_lineage_id(
        invariant_id="HR-005", node="source_validate", owner="driver",
        operation_id="op.read", test_id="T-READ",
    )
    lineage_b = failure_lineage_id(
        invariant_id="HR-005", node="source_validate", owner="driver",
        operation_id="op.read", test_id="T-READ",
    )
    assert lineage_a == lineage_b
    material = relevant_material_revision({"driver.c": "sha"})
    ledger: dict = {}
    first = validate_recovery_admission(
        ledger,
        lineage_id=lineage_a,
        material_revision=material,
        disposition="REPAIR_INTERNAL",
        typed=True,
        new_diagnostic_id="SOURCE_STUB",
    )
    second = validate_recovery_admission(
        ledger,
        lineage_id=lineage_a,
        material_revision=material,
        disposition="REPAIR_INTERNAL",
        typed=True,
        new_diagnostic_id="SOURCE_STUB_REWORDED",
    )
    untyped = validate_recovery_admission(
        ledger,
        lineage_id=lineage_a,
        material_revision=material,
        disposition="REPAIR_INTERNAL",
        typed=False,
    )
    assert first.admitted and not second.admitted and not untyped.admitted


def test_scn_008_dead_worker_reconciles_to_interrupted(tmp_path: Path):
    runtime = ProjectRuntime(tmp_path, "demo").ensure()
    record = {
        "schema_version": "1.0",
        "job_id": "job",
        "project": "demo",
        "mode": "CONTINUOUS",
        "pid": 999999,
    }
    atomic_write_json(runtime.execution_jobs / "job.json", record)
    atomic_write_json(runtime.active_execution_job, record)
    with patch("orchestrator.execution_jobs._pid_is_alive", return_value=False):
        result = read_execution_job(tmp_path, "demo")
    assert result["mode"] == "INTERRUPTED"
    reconciliations = list(runtime.worker_reconciliations.glob("*.json"))
    assert len(reconciliations) == 1
    reconciliation = json.loads(
        reconciliations[0].read_text(encoding="utf-8")
    )
    assert reconciliation["reasons"] == ["dead_nonterminal_worker"]
    assert reconciliation["job_id"] == "job"


def test_scn_019_main_cannot_include_low_level_owner_header(tmp_path: Path):
    repo = tmp_path
    project = repo / "projects" / "demo"
    (project / "main").mkdir(parents=True)
    (project / "components" / "driver").mkdir(parents=True)
    (project / "main" / "CMakeLists.txt").write_text(
        "idf_component_register(SRCS app.c REQUIRES driver)",
        encoding="utf-8",
    )
    (project / "main" / "app.c").write_text(
        '#include "driver_low.h"\nvoid app_main(void) {}\n',
        encoding="utf-8",
    )
    (project / "components" / "driver" / "CMakeLists.txt").write_text(
        "idf_component_register(SRCS driver.c)", encoding="utf-8"
    )
    contract = {
        "architecture": {"component_api_manifest": [
            {
                "component": "main",
                "responsibility_layer": "system_orchestration",
                "resources": [],
                "dependencies": ["driver"],
                "allowed_dependency_layers": ["device_driver"],
                "exported_semantic_apis": [{
                    "header": "app.h", "symbol": "app_main"
                }],
                "test_only": False,
            },
            {
                "component": "driver",
                "responsibility_layer": "device_driver",
                "resources": ["i2c:0"],
                "dependencies": [],
                "allowed_dependency_layers": [],
                "exported_semantic_apis": [{
                    "header": "driver.h", "symbol": "driver_read"
                }],
                "exported_low_level_apis": [{"header": "driver_low.h"}],
                "test_only": False,
            },
        ]}
    }
    errors = validate_component_architecture(project, contract)
    assert any("low-level" in item for item in errors)
    contract["architecture"]["component_api_manifest"][1][
        "test_only"
    ] = True
    assert any(
        "test-only" in item
        for item in validate_component_architecture(project, contract)
    )
    contract["architecture"]["component_api_manifest"][1][
        "test_only"
    ] = False
    duplicate = {
        **contract["architecture"]["component_api_manifest"][1],
        "component": "driver_copy",
        "exported_semantic_apis": [{
            "header": "driver_copy.h", "symbol": "driver_copy_read"
        }],
    }
    contract["architecture"]["component_api_manifest"].append(duplicate)
    duplicate_errors = validate_component_architecture(project, contract)
    assert any("duplicate owners" in item for item in duplicate_errors)
    assert any("no normal-runtime consumer" in item for item in duplicate_errors)


def test_project_hygiene_is_ignored_and_token_gate_exceeds_55_percent(
    tmp_path: Path,
):
    (tmp_path / ".gitignore").write_text(
        "projects/*/.v/\n", encoding="utf-8"
    )
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True)
    _, errors = validate_generated_file_hygiene(tmp_path, project)
    assert errors == []
    # Sixty owner transactions at the enforced 40k non-Design ceiling is a
    # deliberately pessimistic replay of the audited 60 automatic turns.
    bounded_tokens = 60 * 40_000
    assert token_reduction_percent(224_078_716, bounded_tokens) >= 55.0


def test_scn_021_project_hygiene_blocks_unignored_verification_roots(
    tmp_path: Path,
):
    (tmp_path / ".gitignore").write_text("", encoding="utf-8")
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True)
    _, errors = validate_generated_file_hygiene(tmp_path, project)
    assert "verification root projects/*/.v/ is not ignored" in errors


def _release_evidence(**overrides) -> ReleaseEvidence:
    values = {
        "closure_pass": True,
        "selftest_disabled": True,
        "fullclean_receipt_id": "fullclean",
        "configure_receipt_id": "configure",
        "build_log": "build.log",
        "flash_log": "flash.log",
        "serial_log": "serial.log",
        "runtime_marker": "PRODUCT_OK",
        "firmware_sha256": "f" * 64,
        "firmware_binary": "app.bin",
        "build_receipt_id": "build",
        "flash_receipt_id": "flash",
        "serial_receipt_id": "observe",
        "production_scenario_receipt_ids": ["scenario-receipt"],
        "production_scenario_ids": ["normal-operation"],
    }
    values.update(overrides)
    return ReleaseEvidence(**values)


def test_scn_017_and_018_release_requires_behavior_and_fullclean():
    ready_only = _release_evidence(
        production_scenario_receipt_ids=[],
        production_scenario_ids=[],
    )
    assert "release selects no core production scenario" in (
        validate_release_transaction(ready_only)
    )
    no_fullclean = _release_evidence(fullclean_receipt_id=None)
    assert any(
        "fullclean_receipt_id" in error
        for error in validate_release_transaction(no_fullclean)
    )


def test_release_runtime_rejects_selftest_unsupported_and_redacts_secret():
    secret = "do-not-persist-this-value"
    errors = validate_release_runtime_text(
        "SELFTEST\nESP_ERR_NOT_SUPPORTED\n"
        f"authorization={secret}\n",
        [],
    )
    assert errors
    assert secret not in json.dumps(errors)
