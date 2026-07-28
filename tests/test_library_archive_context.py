from __future__ import annotations

import json
from pathlib import Path

from orchestrator.archive import archive_historical_failures
from orchestrator.datasheet_library import (
    project_alias_path,
    resolve_datasheet_alias,
    sync_datasheet_references,
)
from orchestrator.design_package import _ground_contract
from orchestrator.failure_context import select_owner_failure_logs
from orchestrator.models import Failure, FailureCategory, Receipt
from orchestrator.storage import ProjectStore, file_ref, resolve_artifact_bytes


def test_datasheet_manifest_references_one_canonical_object(tmp_path: Path):
    import hashlib
    source = tmp_path / "incoming" / "sensor.pdf"
    source.parent.mkdir(parents=True); source.write_bytes(b"%PDF-" + b"x" * 300)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    project = tmp_path / "projects" / "demo"
    (project / "components" / "sensor").mkdir(parents=True)
    contract = {"datasheets": [{"subsystem_id": "sensor", "part_number": "S1", "source": source.relative_to(tmp_path).as_posix(), "content_hash": sha, "level": "L2"}]}
    sync_datasheet_references(tmp_path, project, contract)
    ref = json.loads((project / "components" / "sensor" / "datasheet_refs.json").read_text(encoding="utf-8"))
    assert ref["project_id"] == "demo"
    assert ref["datasheets"][0]["source"].startswith("hardware/datasheets/objects/sha256/")


def test_cached_datasheet_grounding_restores_canonical_source(tmp_path: Path):
    project = tmp_path / "projects" / "demo"
    store = ProjectStore(project)
    store.ensure()
    old_source = "projects/demo/components/sensor/datasheets/PART1.pdf"
    canonical_source = f"hardware/datasheets/objects/sha256/{'a' * 64}.pdf"
    aliases = project_alias_path(tmp_path, "demo")
    aliases.parent.mkdir(parents=True)
    aliases.write_text(json.dumps({
        "schema_version": "1.1",
        "project_id": "demo",
        "aliases": {old_source: {"source": canonical_source, "sha256": "a" * 64}},
    }), encoding="utf-8")
    receipt = Receipt(
        receipt_id="cached-sheet",
        run_id="design",
        operation="datasheet_artifact_inspect",
        started_at="a",
        finished_at="b",
        success=True,
        outputs={
            "source": old_source,
            "sha256": "a" * 64,
            "technical_content_valid": True,
        },
    )
    store.write_receipt(receipt, "design")

    class CachedGrounding:
        def __init__(self):
            self.store = store

        def datasheet_inspect(self, _subsystem_id, _record):
            return receipt

    contract = {"subsystems": [], "component_selections": [], "datasheets": [{
        "subsystem_id": "sensor",
        "source": old_source,
    }]}
    grounded, _, errors = _ground_contract(contract, CachedGrounding(), tmp_path)
    assert errors == []
    assert grounded["datasheets"][0]["source"] == canonical_source


def test_grounding_rebinds_implementation_facts_to_current_receipts(tmp_path: Path):
    project = tmp_path / "projects" / "demo"
    store = ProjectStore(project)
    store.ensure()
    datasheet_receipt = Receipt(
        receipt_id="datasheet-current",
        run_id="design",
        operation="datasheet_artifact_inspect",
        started_at="a",
        finished_at="b",
        success=True,
        outputs={
            "source": "hardware/datasheets/objects/sha256/" + "a" * 64 + ".pdf",
            "sha256": "a" * 64,
            "technical_content_valid": True,
        },
    )
    store.write_receipt(datasheet_receipt, "design")

    class Grounding:
        def __init__(self):
            self.store = store

        def datasheet_inspect(self, _subsystem_id, _record):
            return datasheet_receipt

    contract = {
        "subsystems": [{"id": "sensor", "classification": "external_part"}],
        "component_selections": [],
        "datasheets": [{"subsystem_id": "sensor", "source": "input.pdf"}],
        "implementation_facts": [{
            "id": "IF-SENSOR",
            "subsystem_id": "sensor",
            "source_kind": "datasheet",
            "provider_receipt_id": "PENDING_SENSOR_DATASHEET_RECEIPT",
        }],
    }
    grounded, _, errors = _ground_contract(contract, Grounding(), tmp_path)
    assert errors == []
    assert grounded["implementation_facts"][0]["provider_receipt_id"] == "datasheet-current"


def test_datasheet_aliases_are_project_scoped(tmp_path: Path):
    logical = "components/sensor/datasheets/PART.pdf"
    alpha = project_alias_path(tmp_path, "alpha")
    beta = project_alias_path(tmp_path, "beta")
    alpha.parent.mkdir(parents=True)
    alpha.write_text(json.dumps({
        "schema_version": "1.1", "project_id": "alpha",
        "aliases": {logical: {"source": "hardware/datasheets/objects/sha256/a.pdf"}},
    }), encoding="utf-8")
    beta.write_text(json.dumps({
        "schema_version": "1.1", "project_id": "beta",
        "aliases": {logical: {"source": "hardware/datasheets/objects/sha256/b.pdf"}},
    }), encoding="utf-8")
    assert resolve_datasheet_alias(tmp_path, logical, "alpha").endswith("/a.pdf")
    assert resolve_datasheet_alias(tmp_path, logical, "beta").endswith("/b.pdf")


def test_owner_failure_context_excludes_other_runs_and_owners(tmp_path: Path):
    store = ProjectStore(tmp_path); store.ensure()
    for run, owner, suffix in (("run-1", "sensor", "keep"), ("run-1", "wifi", "skip-owner"), ("run-2", "sensor", "skip-run")):
        log = tmp_path / "logs" / run / f"{suffix}.log"; log.parent.mkdir(parents=True, exist_ok=True); log.write_text(suffix, encoding="utf-8")
        failure = Failure(category=FailureCategory.TOOL, summary=suffix, owner=owner)
        receipt = Receipt(receipt_id=suffix, run_id=run, operation="subsystem_failure", started_at="a", finished_at=suffix, success=False, artifacts=[file_ref(log, tmp_path, "text/plain")], failure=failure)
        store.write_receipt(receipt, "failure")
    assert [path.name for path in select_owner_failure_logs(store, "run-1", "sensor")] == ["keep.log"]


def test_failed_run_archive_remains_resolvable(tmp_path: Path):
    project = tmp_path / "demo"; store = ProjectStore(project); store.ensure()
    log = project / "logs" / "old-run" / "failure.log"; log.parent.mkdir(parents=True); log.write_text("failure evidence", encoding="utf-8")
    failure = Failure(category=FailureCategory.TOOL, summary="failed", owner="sensor")
    receipt = Receipt(receipt_id="f1", run_id="old-run", operation="subsystem_failure", started_at="a", finished_at="b", success=False, artifacts=[file_ref(log, project, "text/plain")], failure=failure)
    store.write_receipt(receipt, "failure")
    (project / "execution" / "run-state.json").write_text('{"run_id":"current-run"}', encoding="utf-8")
    result = archive_historical_failures(project)
    assert result["archived_runs"] == ["old-run"] and not log.exists()
    assert resolve_artifact_bytes(project, "logs/old-run/failure.log") == b"failure evidence"
