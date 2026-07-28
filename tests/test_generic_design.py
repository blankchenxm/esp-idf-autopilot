from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from orchestrator.design_package import (
    DesignCompilationError,
    DesignDraft,
    _ground_contract,
    compile_initial_design,
)
from orchestrator.adapters.datasheet import DatasheetArtifactAdapter
from orchestrator.adapters.design_grounding import (
    DesignGroundingAdapter,
    normalized_quote_anchor,
)
from orchestrator.adapters.registry import RegistryAdapter, RegistryUnavailable
from orchestrator.storage import ProjectStore
from tests.test_design_contract import valid_contract


class StaticDeepReader:
    def read(self, _project, _owner, _part, extracted, _text_hash):
        return [{
            "parameter": "register_command_timing",
            "value": {"mode": "documented"},
            "unit": None,
            "quote": extracted[:80],
            "source_tokens": ["register"],
        }]


class MixedDeepReader:
    def read(self, _project, _owner, _part, _extracted, _text_hash):
        return [
            {
                "parameter": "register_command_timing",
                "value": {"mode": "documented"},
                "unit": None,
                "quote": "part,register,value,timing",
                "source_tokens": ["register"],
            },
            {
                "parameter": "invented",
                "value": 99,
                "unit": None,
                "quote": "this sentence is not in the datasheet",
                "source_tokens": ["invented"],
            },
        ]


class FakeProvider:
    def __init__(self, unknowns: list[str] | None = None, wrong_project: str | None = None, contract: dict | None = None):
        self.unknowns = unknowns or []
        self.wrong_project = wrong_project
        self.contract = contract

    def generate(self, repo_root: Path, project: str) -> DesignDraft:
        selected = self.wrong_project or project
        return DesignDraft(
            spec_markdown=f"# {selected}\n\nGenerated from this project's own inputs.",
            execution_contract=self.contract or valid_contract(selected),
            providers=[{"name": "fake", "kind": "test"}],
            blocking_unknowns=self.unknowns,
        )


def inputs(root: Path, project: str) -> None:
    for area in ("requirements", "connections"):
        path = root / area / f"{project}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {project} {area}\nunique-{project}-{area}\n", encoding="utf-8")


@pytest.mark.parametrize("project", ["new_board", "camera_display", "product_2027"])
def test_any_new_project_pair_can_compile_initial_design(tmp_path: Path, project: str):
    inputs(tmp_path, project)
    design = compile_initial_design(tmp_path, project, provider=FakeProvider())
    manifest = json.loads((design / "manifest.json").read_text(encoding="utf-8"))
    contract = json.loads((design / "execution-contract.json").read_text(encoding="utf-8"))
    assert design.name == "rev-0001" and contract["project"] == project
    assert {item["path"] for item in manifest["inputs"]} == {f"requirements/{project}.md", f"connections/{project}.md"}
    assert json.loads((design / "approval.json").read_text(encoding="utf-8"))["status"] == "PENDING"


def test_new_project_requires_both_user_inputs(tmp_path: Path):
    inputs(tmp_path, "incomplete")
    (tmp_path / "connections" / "incomplete.md").unlink()
    with pytest.raises(FileNotFoundError, match="connections"):
        compile_initial_design(tmp_path, "incomplete", provider=FakeProvider())


def test_provider_cannot_return_a_different_project(tmp_path: Path):
    inputs(tmp_path, "right")
    with pytest.raises(ValueError, match="wrong project"):
        compile_initial_design(tmp_path, "right", provider=FakeProvider(wrong_project="wrong"))


def test_provider_omitted_project_binds_to_cli_project(tmp_path: Path):
    inputs(tmp_path, "right")
    contract = valid_contract("")
    design = compile_initial_design(tmp_path, "right", provider=FakeProvider(contract=contract))
    generated = json.loads((design / "execution-contract.json").read_text(encoding="utf-8"))
    assert generated["project"] == "right"


def test_provider_contract_missing_required_keys_fails_before_grounding(tmp_path: Path):
    inputs(tmp_path, "incomplete_contract")
    with pytest.raises(ValueError, match="missing required keys: .*requirements"):
        compile_initial_design(
            tmp_path,
            "incomplete_contract",
            provider=FakeProvider(contract={"schema_version": "1.2"}),
        )


def test_blocking_unknown_waits_in_staging_without_allocating_revision(tmp_path: Path):
    inputs(tmp_path, "unknown")
    with pytest.raises(DesignCompilationError) as caught:
        compile_initial_design(
            tmp_path,
            "unknown",
            provider=FakeProvider(["[USER_DECISION] supply voltage is unspecified"]),
        )
    assert "blocking unknown" in str(caught.value)
    assert not list(
        (tmp_path / "projects" / "unknown" / "design-package").glob("rev-*")
    )


def test_spec_change_invalidates_complete_design_digest(tmp_path: Path):
    inputs(tmp_path, "digest")
    design = compile_initial_design(tmp_path, "digest", provider=FakeProvider())
    original = json.loads((design / "manifest.json").read_text(encoding="utf-8"))["design_digest"]
    (design / "spec.md").write_text("# silently changed\n", encoding="utf-8")
    from orchestrator.validators import validate_design_package
    _, errors = validate_design_package(design, require_approval=False)
    assert original and any("files hash/size mismatch" in item for item in errors)


def external_contract(root: Path, project: str) -> dict:
    data = ("SENSOR1 part,register,value,timing,voltage,interface,reset\n" * 40).encode(); source = root / "datasheets" / "sensor.txt"
    source.parent.mkdir(); source.write_bytes(data)
    value = valid_contract(project)
    value["subsystems"][0].update({"classification": "external_part", "registry_search_required": True})
    value["component_selections"] = [{"subsystem_id": "probe", "status": "ok", "exact_search": {"query": "vendor/sensor"}, "capability_search": {"query": "i2c temperature sensor"}, "decision": "custom", "decision_reason": "no compatible maintained component"}]
    value["datasheets"] = [{"subsystem_id": "probe", "part_number": "SENSOR1", "variant": "A", "document_id": "DS-1", "revision": "1", "source": "datasheets/sensor.txt", "content_hash": hashlib.sha256(data).hexdigest(), "coverage": ["registers", "timing"], "level": "L2", "technical_content_valid": True}]
    return value


def grounding(root: Path, project: str, search=None) -> DesignGroundingAdapter:
    store = ProjectStore(root / "projects" / project); store.ensure()
    registry = RegistryAdapter(search=search or (lambda query: [{"query": query}]))
    return DesignGroundingAdapter(root, store, f"design-{project}", registry=registry, datasheets=DatasheetArtifactAdapter(root), deep_reader=StaticDeepReader())


def test_registry_and_datasheet_are_bound_by_raw_receipts(tmp_path: Path):
    project = "grounded"; inputs(tmp_path, project); contract = external_contract(tmp_path, project)
    design = compile_initial_design(tmp_path, project, provider=FakeProvider(contract=contract), grounding=grounding(tmp_path, project))
    generated = json.loads((design / "execution-contract.json").read_text(encoding="utf-8")); manifest = json.loads((design / "manifest.json").read_text(encoding="utf-8"))
    ids = {generated["component_selections"][0]["provider_receipt_id"], generated["datasheets"][0]["provider_receipt_id"]}
    assert ids == {item["receipt_id"] for item in manifest["providers"] if item.get("receipt_id")}
    assert {item["kind"] for item in manifest["providers"] if item.get("receipt_id")} == {"registry", "datasheet"}
    for item in manifest["providers"]:
        if item.get("receipt_ref"): assert (tmp_path / item["receipt_ref"]["path"]).is_file()


def test_l2_deep_reader_facts_replace_provider_placeholders(tmp_path: Path):
    project = "deep_facts"; inputs(tmp_path, project)
    contract = external_contract(tmp_path, project)
    contract["schema_version"] = "1.3"
    contract["product_decisions"] = []
    contract["implementation_facts"] = [{
        "id": "PENDING", "subsystem_id": "probe", "parameter": "pending",
        "value": "GROUNDING_PENDING", "source_kind": "datasheet",
        "provider_receipt_id": "pending", "source_assertions": [{
            "path_glob": "components/probe/**/*", "required_tokens": ["SENSOR1"],
        }],
    }]
    generated, _, errors = _ground_contract(
        contract, grounding(tmp_path, project), tmp_path
    )
    assert errors == []
    facts = generated["implementation_facts"]
    assert len(facts) == 1
    assert facts[0]["value"] == {"mode": "documented"}
    assert facts[0]["provider_receipt_id"] == generated["datasheets"][0]["provider_receipt_id"]
    assert facts[0]["evidence_locator"]["quote"]


def test_datasheet_quote_anchor_normalizes_layout_without_losing_exact_span():
    source = "prefix imple-\nmentation   fact suffix"
    anchor = normalized_quote_anchor(source, "implementation fact")
    assert anchor["exact_source_slice"] == "imple-\nmentation   fact"
    assert source[anchor["start"]:anchor["end"]] == anchor["exact_source_slice"]
    assert len(anchor["source_slice_sha256"]) == 64


def test_one_bad_deep_reader_fact_does_not_discard_valid_document_or_raw_output(
    tmp_path: Path,
):
    project = "partial_facts"
    inputs(tmp_path, project)
    contract = external_contract(tmp_path, project)
    adapter = grounding(tmp_path, project)
    adapter.deep_reader = MixedDeepReader()
    record = dict(contract["datasheets"][0])

    receipt = adapter.datasheet_inspect("probe", record)

    assert receipt.success
    assert receipt.outputs["technical_content_valid"] is True
    assert len(receipt.outputs["implementation_facts"]) == 1
    assert receipt.outputs["grounding_diagnostics"][0]["code"] == (
        "DATASHEET_FACT_ANCHOR_REJECTED"
    )
    raw_receipts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in adapter.store.receipts.joinpath("design").glob("*.json")
    ]
    deep_read = next(
        item for item in raw_receipts if item["operation"] == "datasheet_deep_read"
    )
    assert len(deep_read["outputs"]["facts"]) == 2


def test_provider_outage_remains_unnumbered_staging_attempt(tmp_path: Path):
    project = "outage"; inputs(tmp_path, project); contract = external_contract(tmp_path, project)
    def failed(_query): raise RuntimeError("network unavailable")
    with pytest.raises(DesignCompilationError):
        compile_initial_design(tmp_path, project, provider=FakeProvider(contract=contract), grounding=grounding(tmp_path, project, failed))
    assert not list((tmp_path / "projects" / project / "design-package").glob("rev-*"))


class RepairingProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.first = True

    def generate(self, repo_root: Path, project: str) -> DesignDraft:
        draft = super().generate(repo_root, project)
        if self.first:
            draft.execution_contract["architecture"].pop("queues")
        return draft

    def repair(self, repo_root: Path, project: str, draft: DesignDraft, errors: list[str]) -> DesignDraft:
        self.first = False
        return super().generate(repo_root, project)


class GroundingAwareRepairingProvider(FakeProvider):
    def __init__(self, contract: dict | None = None):
        super().__init__(unknowns=["first attempt"], contract=contract)
        self.repair_contract: dict | None = None

    def repair(self, repo_root: Path, project: str, draft: DesignDraft, errors: list[str]) -> DesignDraft:
        self.repair_contract = draft.execution_contract
        return DesignDraft(draft.spec_markdown, draft.execution_contract, draft.providers, [])


def test_repaired_staging_attempt_allocates_only_one_revision(tmp_path: Path):
    inputs(tmp_path, "repaired")
    design = compile_initial_design(tmp_path, "repaired", provider=RepairingProvider())
    assert design.name == "rev-0001"
    assert [path.name for path in design.parent.glob("rev-*")] == ["rev-0001"]


def test_untyped_technical_unknown_triggers_contract_repair(tmp_path: Path):
    project = "grounded_repair"; inputs(tmp_path, project)
    provider = GroundingAwareRepairingProvider(external_contract(tmp_path, project))
    design = compile_initial_design(
        tmp_path,
        project,
        provider=provider,
        grounding=grounding(tmp_path, project),
    )
    assert provider.repair_contract is not None
    assert design.name == "rev-0001"


def test_tampered_provider_receipt_invalidates_design(tmp_path: Path):
    project = "tamper"; inputs(tmp_path, project); contract = external_contract(tmp_path, project)
    design = compile_initial_design(tmp_path, project, provider=FakeProvider(contract=contract), grounding=grounding(tmp_path, project))
    manifest = json.loads((design / "manifest.json").read_text(encoding="utf-8")); receipt = tmp_path / next(item["receipt_ref"]["path"] for item in manifest["providers"] if item.get("receipt_ref"))
    receipt.write_text("{}\n", encoding="utf-8")
    from orchestrator.validators import validate_design_package
    assert any("provider receipt hash/size mismatch" in item for item in validate_design_package(design, require_approval=False)[1])


def test_successful_registry_grounding_is_reused_by_cache(tmp_path: Path):
    project = "cache"; inputs(tmp_path, project)
    calls = []
    store = ProjectStore(tmp_path / "projects" / project); store.ensure()
    registry = RegistryAdapter(search=lambda query: calls.append(query) or [{"query": query}])
    adapter = DesignGroundingAdapter(tmp_path, store, "design-cache", registry=registry, datasheets=DatasheetArtifactAdapter(tmp_path))
    first = adapter.registry_search("probe", "vendor/sensor", "temperature sensor", None)
    second = adapter.registry_search("probe", "vendor/sensor", "temperature sensor", None)
    assert first.receipt_id == second.receipt_id and calls == ["vendor/sensor", "temperature sensor"]


def test_registry_search_fetches_discovered_candidate_details():
    details = []
    registry = RegistryAdapter(
        search=lambda _query: [
            {
                "text": json.dumps(
                    [
                        {
                            "namespace_name": "vendor",
                            "component_name": "sensor",
                        }
                    ]
                )
            }
        ],
        details=lambda component: details.append(component)
        or {"component": component, "version": "1.0.0"},
    )
    result = registry.search_pair("SENSOR1", "ESP-IDF sensor")
    assert result["candidate_ids"] == ["vendor/sensor"]
    assert result["candidate_details"] == [
        {"component": "vendor/sensor", "version": "1.0.0"}
    ]
    assert details == ["vendor/sensor"]


def test_registry_search_fetches_every_discovered_candidate_and_records_failures():
    calls = []
    components = [
        {"namespace_name": "vendor", "component_name": f"sensor_{index}"}
        for index in range(10)
    ]

    def details(component: str):
        calls.append(component)
        if component == "vendor/sensor_9":
            raise RuntimeError("detail endpoint unavailable")
        return {"component": component, "version": "1.0.0"}

    registry = RegistryAdapter(
        search=lambda _query: [{"text": json.dumps(components)}],
        details=details,
        attempts=1,
    )

    result = registry.search_pair("SENSOR", "ESP-IDF sensor")

    assert calls == [f"vendor/sensor_{index}" for index in range(10)]
    assert [item["component"] for item in result["candidate_details"]] == calls[:-1]
    assert result["candidate_detail_errors"] == [
        {
            "component": "vendor/sensor_9",
            "summary": "RegistryUnavailable: Registry detail lookup failed for 'vendor/sensor_9' after 1 attempts: RuntimeError: detail endpoint unavailable",
        }
    ]


def test_registry_candidate_detail_retries_transient_failures(monkeypatch):
    calls = []

    def details(component: str):
        calls.append(component)
        if len(calls) < 3:
            raise RuntimeError("temporary MCP transport failure")
        return {"component": component, "version": "1.0.0"}

    monkeypatch.setattr("orchestrator.adapters.registry.time.sleep", lambda _seconds: None)
    registry = RegistryAdapter(details=details, attempts=3)

    assert registry.candidate_details("vendor/sensor")["version"] == "1.0.0"
    assert calls == ["vendor/sensor", "vendor/sensor", "vendor/sensor"]


def test_registry_candidate_detail_preserves_nested_failure_summary(monkeypatch):
    def details(_component: str):
        raise ExceptionGroup("transport task failed", [TimeoutError("MCP response timeout")])

    monkeypatch.setattr("orchestrator.adapters.registry.time.sleep", lambda _seconds: None)
    registry = RegistryAdapter(details=details, attempts=1)

    with pytest.raises(RegistryUnavailable, match=r"async transport failure: TimeoutError: MCP response timeout") as error:
        registry.candidate_details("vendor/sensor")
    assert "ExceptionGroup: unhandled errors in a TaskGroup" not in str(error.value)


def test_second_repository_project_inputs_compile_without_crumb_assumptions(tmp_path: Path):
    source_root = Path(__file__).resolve().parents[1]
    project = "hm01b0_st7789"
    for area in ("requirements", "connections"):
        source = source_root / area / f"{project}.md"
        target = tmp_path / area / f"{project}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    design = compile_initial_design(tmp_path, project, provider=FakeProvider())
    contract = json.loads((design / "execution-contract.json").read_text(encoding="utf-8"))
    assert contract["project"] == project
    assert "crumb" not in json.dumps(contract, ensure_ascii=False).lower()
