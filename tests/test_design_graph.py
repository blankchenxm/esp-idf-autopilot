from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver

from orchestrator.adapters.design_grounding import DesignGroundingAdapter
from orchestrator.adapters.registry import RegistryAdapter
from orchestrator.design_graph import build_design_graph
from orchestrator.design_package import DesignDraft
from orchestrator.storage import ProjectStore
from tests.test_design_contract import valid_contract


def inputs(root: Path, project: str) -> None:
    for area in ("requirements", "connections"):
        path = root / area / f"{project}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {project} {area}\n", encoding="utf-8")


class Provider:
    def __init__(self, contract: dict, unknowns: list[str] | None = None):
        self.contract = contract
        self.unknowns = unknowns or []

    def generate(self, _root: Path, project: str) -> DesignDraft:
        return DesignDraft(
            f"# {project}\n\nReview.",
            self.contract,
            [{"name": "test-provider"}],
            self.unknowns,
        )

    def repair(
        self, _root: Path, _project: str, draft: DesignDraft, _errors: list[str]
    ) -> DesignDraft:
        return draft


class Datasheets:
    def __init__(self, root: Path):
        self.root = root

    def acquire_if_needed(self, record: dict, _destination: Path) -> dict:
        path = self.root / "datasheets" / f"{record['part_number']}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            (
                f"{record['part_number']} pin interface voltage timing reset "
                "register command\n"
            )
            * 20,
            encoding="utf-8",
        )
        value = dict(record)
        value["source"] = path.relative_to(self.root).as_posix()
        value["content_hash"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return value

    acquire = acquire_if_needed

    @staticmethod
    def canonicalize(record: dict) -> dict:
        return dict(record)

    def inspect(self, record: dict) -> dict:
        path = self.root / record["source"]
        data = path.read_bytes()
        text = data.decode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        return {
            "source": record["source"],
            "resolved_source": record["source"],
            "sha256": sha,
            "technical_content_valid": True,
            "identity_verified": True,
            "document_id": f"sha256:{sha}",
            "revision": f"sha256:{sha[:16]}",
            "coverage": [
                "identity",
                "pins",
                "interface",
                "electrical",
                "timing",
                "reset_recovery",
                "registers_commands",
            ],
            "extracted_text": text,
            "extracted_text_sha256": hashlib.sha256(data).hexdigest(),
        }


class StaticDeepReader:
    def read(self, _project, _owner, _part, text, _text_hash):
        return [{
            "parameter": "register_command_timing",
            "value": {"mode": "documented"},
            "unit": None,
            "quote": text[:80],
            "source_tokens": ["register"],
        }]


def external_without_datasheet(project: str) -> dict:
    value = valid_contract(project)
    value["subsystems"][0].update(
        {
            "classification": "external_part",
            "part_number": "SENSOR1",
            "registry_search_required": True,
        }
    )
    value["component_selections"] = [
        {
            "subsystem_id": "probe",
            "status": "pending",
            "exact_search": "SENSOR1 ESP-IDF component driver",
            "capability_search": "ESP-IDF I2C sensor driver",
            "decision": "custom",
            "decision_reason": "no compatible component selected",
            "provider_receipt_id": "",
        }
    ]
    value["datasheets"] = []
    return value


def grounding_factory(root: Path, project: str):
    def factory(store: ProjectStore, run_id: str) -> DesignGroundingAdapter:
        return DesignGroundingAdapter(
            root,
            store,
            run_id,
            registry=RegistryAdapter(search=lambda query: [{"query": query}]),
            datasheets=Datasheets(root),
            deep_reader=StaticDeepReader(),
        )

    return factory


def test_design_graph_has_deterministic_inventory_and_grounding_nodes(tmp_path: Path):
    graph = build_design_graph(tmp_path).get_graph()
    nodes = set(graph.nodes)
    edges = {(edge.source, edge.target) for edge in graph.edges}
    assert {
        "initialize",
        "synthesize",
        "inventory",
        "ground",
        "validate",
        "repair",
        "promote",
        "waiting_input",
        "blocked",
        "faulted",
    } <= nodes
    assert ("synthesize", "inventory") in edges
    assert ("inventory", "ground") in edges
    assert ("ground", "validate") in edges


def test_missing_staging_draft_blocks_repair_without_traceback(tmp_path: Path):
    nodes = build_design_graph(tmp_path).get_graph().nodes
    assert "repair" in nodes

    from orchestrator.design_graph import DesignGraphNodes

    repair = DesignGraphNodes(tmp_path).repair(
        {
            "project": "crumb",
            "attempt": 1,
            "staging_root": str(tmp_path / "projects" / "crumb" / "design-package" / ".staging" / "session"),
            "draft_path": str(tmp_path / "projects" / "crumb" / "design-package" / ".staging" / "session" / "attempt-001" / "draft.json"),
        }
    )

    assert repair["mode"] == "FAULTED"
    assert repair["route"] == "faulted"
    assert "start a new design transaction" in repair["summary"]


def test_external_part_omitted_datasheet_is_grounded_and_promoted(tmp_path: Path):
    project = "generic_sensor"
    inputs(tmp_path, project)
    contract = external_without_datasheet(project)
    graph = build_design_graph(
        tmp_path,
        provider_factory=lambda: Provider(contract),
        grounding_factory=grounding_factory(tmp_path, project),
    )
    result = graph.invoke(
        {"project": project, "job_id": "job-1", "revision": None},
        {"recursion_limit": 32},
    )
    assert result["mode"] == "WAITING_SPEC"
    design = Path(result["design_dir"])
    generated = json.loads(
        (design / "execution-contract.json").read_text(encoding="utf-8")
    )
    assert generated["design_inventory"][0]["part_number"] == "SENSOR1"
    assert {
        (item["kind"], item["subsystem_id"])
        for item in generated["grounding_plan"]
    } >= {("registry", "probe"), ("datasheet", "probe")}
    assert generated["datasheets"][0]["identity_verified"] is True
    assert generated["datasheets"][0]["provider_receipt_id"]
    selection = generated["component_selections"][0]
    assert selection["exact_search"] == "SENSOR1"
    assert (
        selection["provider_exact_search"]
        == "SENSOR1 ESP-IDF component driver"
    )


def test_design_promotes_over_prepare_revision_shell(tmp_path: Path):
    project = "prepared_revision"
    inputs(tmp_path, project)
    contract = valid_contract(project)
    graph = build_design_graph(tmp_path, provider_factory=lambda: Provider(contract))
    first = graph.invoke(
        {"project": project, "job_id": "job-first", "revision": 1},
        {"recursion_limit": 32},
    )
    assert first["mode"] == "WAITING_SPEC"

    from orchestrator.design_package import create_revision
    revision = create_revision(tmp_path, project, 1, 2)
    migration = json.loads(
        (revision / "migration-report.json").read_text(encoding="utf-8")
    )
    assert migration["target_contract_schema"] == "1.7"
    assert migration["input_files_unchanged"] == [
        f"requirements/{project}.md",
        f"connections/{project}.md",
    ]
    assert migration["status"] in {
        "DESIGN_RECOMPILATION_REQUIRED",
        "IMPACT_ANALYSIS_REQUIRED",
    }
    assert json.loads(
        (revision / "approval.json").read_text(encoding="utf-8")
    )["status"] == "PENDING"

    revised = build_design_graph(
        tmp_path, provider_factory=lambda: Provider(contract)
    ).invoke(
        {"project": project, "job_id": "job-revised", "revision": 2},
        {"recursion_limit": 32},
    )
    assert revised["mode"] == "WAITING_SPEC"
    assert (tmp_path / "projects" / project / "design-package" / "rev-0002" / "input-authority.json").is_file()


def test_design_l1_does_not_invoke_deep_reader(tmp_path: Path):
    project = "local_grounding_retry"
    inputs(tmp_path, project)
    contract = external_without_datasheet(project)

    class CountingProvider(Provider):
        repair_calls = 0

        def repair(self, *args, **kwargs):
            self.repair_calls += 1
            return super().repair(*args, **kwargs)

    class FlakyReader:
        calls = 0

        def read(self, _project, _owner, _part, text, _text_hash):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("temporary reader process failure")
            return StaticDeepReader().read(
                _project, _owner, _part, text, _text_hash
            )

    provider = CountingProvider(contract)
    reader = FlakyReader()

    def factory(store: ProjectStore, run_id: str) -> DesignGroundingAdapter:
        return DesignGroundingAdapter(
            tmp_path,
            store,
            run_id,
            registry=RegistryAdapter(search=lambda query: [{"query": query}]),
            datasheets=Datasheets(tmp_path),
            deep_reader=reader,
        )

    result = build_design_graph(
        tmp_path,
        provider_factory=lambda: provider,
        grounding_factory=factory,
    ).invoke(
        {"project": project, "job_id": "job-local-retry", "revision": None},
        {"recursion_limit": 32},
    )

    assert result["mode"] == "WAITING_SPEC"
    assert result["attempt"] == 1
    assert reader.calls == 0
    assert provider.repair_calls == 0


def test_grounding_reconciliation_resolves_only_receipt_proven_unknowns(
    tmp_path: Path,
):
    project = "reconciled_sensor"
    inputs(tmp_path, project)
    contract = external_without_datasheet(project)
    graph = build_design_graph(
        tmp_path,
        provider_factory=lambda: Provider(
            contract,
            [
                (
                    "[GROUNDING_PENDING] Datasheet and Registry results remain "
                    "pending until Harness grounding."
                ),
                "[USER_DECISION] maximum recording duration requires user decision",
            ],
        ),
        grounding_factory=grounding_factory(tmp_path, project),
    )
    result = graph.invoke(
        {"project": project, "job_id": "job-reconcile", "revision": None},
        {"recursion_limit": 32},
    )
    assert result["mode"] == "WAITING_DESIGN_INPUT"
    audit = json.loads(
        Path(result["blocking_unknowns_path"]).read_text(encoding="utf-8")
    )
    assert audit["active"] == [
        "[USER_DECISION] maximum recording duration requires user decision"
    ]
    assert len(audit["resolved_by_grounding"]) == 1
    errors = json.loads(
        Path(result["errors_path"]).read_text(encoding="utf-8")
    )["errors"]
    assert all("GROUNDING_PENDING" not in item for item in errors)
    assert any("USER_DECISION" in item for item in errors)
    assert "Harness grounding reconciliation" in Path(result["spec"]).read_text(
        encoding="utf-8"
    )


def test_grounding_reconciliation_resolves_legacy_acquisition_status(
    tmp_path: Path,
):
    project = "legacy_grounding_status"
    inputs(tmp_path, project)
    contract = external_without_datasheet(project)
    graph = build_design_graph(
        tmp_path,
        provider_factory=lambda: Provider(
            contract,
            [
                "No receipt-bound exact-part datasheet grounding or implementation facts are available yet.",
                "Registry candidate inventories and provider receipts remain pending.",
                "Local ESP-IDF API grounding remains pending, so exact resource limits cannot yet be fixed without guessing.",
            ],
        ),
        grounding_factory=grounding_factory(tmp_path, project),
    )
    result = graph.invoke(
        {"project": project, "job_id": "job-legacy-reconcile", "revision": None},
        {"recursion_limit": 32},
    )
    assert result["mode"] == "WAITING_SPEC"
    audit = json.loads(
        Path(result["blocking_unknowns_path"]).read_text(encoding="utf-8")
    )
    assert audit["active"] == []
    assert len(audit["resolved_by_grounding"]) == 3


def test_grounding_reconciliation_resolves_contract_limitations(
    tmp_path: Path,
):
    project = "resolved_limitation"
    inputs(tmp_path, project)
    contract = external_without_datasheet(project)
    contract["limitations"] = [
        {
            "id": "L1",
            "status": "BLOCKING",
            "statement": "[GROUNDING_PENDING] Datasheet values remain pending.",
        }
    ]
    graph = build_design_graph(
        tmp_path,
        provider_factory=lambda: Provider(contract, []),
        grounding_factory=grounding_factory(tmp_path, project),
    )
    result = graph.invoke(
        {"project": project, "job_id": "job-limit-reconcile", "revision": None},
        {"recursion_limit": 32},
    )
    assert result["mode"] == "WAITING_SPEC"
    promoted = json.loads(
        (
            tmp_path
            / "projects"
            / project
            / "design-package"
            / "rev-0001"
            / "execution-contract.json"
        ).read_text(encoding="utf-8")
    )
    assert promoted["limitations"][0]["status"] == "resolved_grounding"
    assert promoted["limitations"][0]["provider_receipt_ids"]


def test_user_decision_unknown_waits_without_allocating_revision(tmp_path: Path):
    project = "needs_policy"
    inputs(tmp_path, project)
    contract = valid_contract(project)
    graph = build_design_graph(
        tmp_path,
        provider_factory=lambda: Provider(
            contract, ["[USER_DECISION] maximum recording duration requires user decision"]
        ),
    )
    result = graph.invoke(
        {"project": project, "job_id": "job-2", "revision": None},
        {"recursion_limit": 32},
    )
    assert result["mode"] == "WAITING_DESIGN_INPUT"
    assert Path(result["spec"]).is_file()
    assert not list(
        (tmp_path / "projects" / project / "design-package").glob("rev-*")
    )
    spec = Path(result["spec"]).read_text(encoding="utf-8")
    assert "Harness active design decisions" in spec
    assert "non-authoritative proposal" in spec


def test_structural_repair_cannot_erase_user_decision(tmp_path: Path):
    project = "repair_authority"
    inputs(tmp_path, project)
    invalid = valid_contract(project)
    del invalid["release"]["runtime_marker"]
    repaired = valid_contract(project)

    class DroppingRepairProvider:
        def generate(self, _root: Path, _project: str) -> DesignDraft:
            return DesignDraft(
                "# Initial\n",
                invalid,
                [{"name": "test-provider"}],
                ["[USER_DECISION] choose the retention policy"],
            )

        def repair(
            self,
            _root: Path,
            _project: str,
            _draft: DesignDraft,
            _errors: list[str],
        ) -> DesignDraft:
            return DesignDraft(
                "# Repaired but omitted decision\n",
                repaired,
                [{"name": "test-provider"}],
                [],
            )

    result = build_design_graph(
        tmp_path, provider_factory=DroppingRepairProvider
    ).invoke(
        {"project": project, "job_id": "job-repair", "revision": None},
        {"recursion_limit": 32},
    )

    assert result["attempt"] == 2
    assert result["mode"] == "WAITING_DESIGN_INPUT"
    audit = json.loads(
        Path(result["blocking_unknowns_path"]).read_text(encoding="utf-8")
    )
    assert audit["active"] == [
        "[USER_DECISION] choose the retention policy"
    ]
    assert not list(
        (tmp_path / "projects" / project / "design-package").glob("rev-*")
    )


def test_graph_honors_single_attempt_limit(tmp_path: Path):
    project = "single_attempt"
    inputs(tmp_path, project)
    invalid = valid_contract(project)
    del invalid["release"]["runtime_marker"]
    graph = build_design_graph(
        tmp_path, provider_factory=lambda: Provider(invalid)
    )

    result = graph.invoke(
        {
            "project": project,
            "job_id": "job-single",
            "revision": None,
            "max_attempts": 1,
        },
        {"recursion_limit": 32},
    )

    assert result["attempt"] == 1
    assert result["mode"] == "FAULTED"


def test_mcu_native_omitted_selection_gets_local_idf_grounding(tmp_path: Path):
    project = "native_only"
    inputs(tmp_path, project)
    contract = valid_contract(project)
    contract["component_selections"] = []
    graph = build_design_graph(
        tmp_path,
        provider_factory=lambda: Provider(contract),
    )
    result = graph.invoke(
        {"project": project, "job_id": "job-3", "revision": None},
        {"recursion_limit": 32},
    )
    assert result["mode"] == "WAITING_SPEC"
    generated = json.loads(
        (
            Path(result["design_dir"]) / "execution-contract.json"
        ).read_text(encoding="utf-8")
    )
    selection = generated["component_selections"][0]
    assert selection["decision"] == "local_idf"
    assert selection["provider_receipt_id"]
    assert ("local_idf", "probe") in {
        (item["kind"], item["subsystem_id"])
        for item in generated["grounding_plan"]
    }


def test_design_graph_resumes_failed_node_from_checkpoint(tmp_path: Path):
    project = "resume_sensor"
    inputs(tmp_path, project)
    contract = valid_contract(project)
    holder = {"provider": None}

    class Failing:
        def generate(self, _root: Path, _project: str):
            raise RuntimeError("provider process interrupted")

    holder["provider"] = Failing()
    saver = MemorySaver()
    config = {"configurable": {"thread_id": f"{project}:design:job"}, "recursion_limit": 32}
    graph = build_design_graph(
        tmp_path,
        checkpointer=saver,
        provider_factory=lambda: holder["provider"],
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        graph.invoke(
            {"project": project, "job_id": "job", "revision": None},
            config,
        )
    snapshot = graph.get_state(config)
    assert snapshot.next == ("synthesize",)

    holder["provider"] = Provider(contract)
    resumed = build_design_graph(
        tmp_path,
        checkpointer=saver,
        provider_factory=lambda: holder["provider"],
    ).invoke(None, config)
    assert resumed["mode"] == "WAITING_SPEC"


def test_design_graph_rejects_secret_in_provider_artifacts(tmp_path: Path):
    project = "secret_sensor"
    inputs(tmp_path, project)
    secret = "never-copy-this-value"
    (tmp_path / "requirements" / f"{project}.md").write_text(
        f"wifi password: {secret}\n", encoding="utf-8"
    )
    contract = valid_contract(project)
    contract["limitations"] = [{"text": secret}]
    graph = build_design_graph(
        tmp_path,
        provider_factory=lambda: Provider(contract),
    )
    with pytest.raises(ValueError, match="plaintext secret"):
        graph.invoke(
            {"project": project, "job_id": "secret-job", "revision": None},
            {"recursion_limit": 32},
        )
    assert not list(
        (tmp_path / "projects" / project / "design-package").glob("rev-*")
    )
