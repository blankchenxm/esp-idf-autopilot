from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.design_package import (
    DesignDraft,
    _bind_contract_project,
    _codex_design_command,
    _decode_codex_design_output,
    _normalize_automatic_evidence_kinds,
    _repair_contract_slice,
    normalize_design_execution_contract,
    _preserve_blocking_unknowns,
    _preserve_complete_contract_rows,
    _preserve_grounding_facts,
    _stage_grounding_context,
    _write_design_authority_bundle,
)


def test_isolated_provider_context_does_not_inherit_interactive_skill_directives() -> None:
    """The nested provider must receive only its explicit, read-only authority."""
    source = Path("orchestrator/design_package.py").read_text(encoding="utf-8")
    assert 'shutil.copy2(repo_root / "AGENTS.md", work / "AGENTS.md")' not in source
    assert "Isolated Design Provider Context" in source
    assert "Read the files named in the caller's prompt with read-only tools" in source


def test_design_authority_bundle_is_redacted_and_does_not_copy_full_tree(
    tmp_path: Path,
) -> None:
    project = "crumb"
    requirements = tmp_path / "requirements" / f"{project}.md"
    connections = tmp_path / "connections" / f"{project}.md"
    requirements.parent.mkdir(parents=True)
    connections.parent.mkdir(parents=True)
    requirements.write_text("wifi password: secret-value\n", encoding="utf-8")
    connections.write_text("GPIO input\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "schemas").mkdir()
    (tmp_path / "docs" / "DESIGN-HARNESS.md").write_text("rules\n", encoding="utf-8")
    (tmp_path / "schemas" / "execution-contract.schema.json").write_text("{}\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    bundle = _write_design_authority_bundle(
        tmp_path, work, project, requirements.read_text(encoding="utf-8")
    )

    content = bundle.read_text(encoding="utf-8")
    assert "secret-value" not in content
    assert "[REDACTED: user-owned local secret]" in content
    assert "docs/HARNESS-HARDENING-TODO.md" not in content


def test_decodes_strict_codex_design_output() -> None:
    contract = {"schema_version": "1.0", "project": "crumb"}
    providers = [{"name": "codex", "status": "generated"}]

    draft = _decode_codex_design_output(
        {
            "spec_markdown": "# Crumb\n\n" + "review " * 40,
            "execution_contract_json": json.dumps(contract),
            "providers_json": json.dumps(providers),
            "blocking_unknowns": [],
        }
    )

    assert draft.execution_contract == contract
    assert draft.providers == providers


def test_normalizes_provider_name_strings() -> None:
    draft = _decode_codex_design_output({
        "spec_markdown": "# Crumb\n\n" + "review " * 40,
        "execution_contract_json": "{}",
        "providers_json": '["Codex Design Subgraph"]',
        "blocking_unknowns": [],
    })
    assert draft.providers == [{"name": "Codex Design Subgraph"}]


def test_normalizes_single_provider_object() -> None:
    draft = _decode_codex_design_output({
        "spec_markdown": "# Crumb\n\n" + "review " * 40,
        "execution_contract_json": "{}",
        "providers_json": '{"name": "Codex Design Subgraph"}',
        "blocking_unknowns": [],
    })
    assert draft.providers == [{"name": "Codex Design Subgraph"}]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_contract_json", "[]"),
        ("execution_contract_json", "not-json"),
    ],
)
def test_rejects_invalid_embedded_design_json(field: str, value: str) -> None:
    output = {
        "spec_markdown": "# Crumb\n\n" + "review " * 40,
        "execution_contract_json": "{}",
        "providers_json": "[]",
        "blocking_unknowns": [],
    }
    output[field] = value

    with pytest.raises(ValueError):
        _decode_codex_design_output(output)


def test_binds_omitted_project_but_rejects_conflicting_project() -> None:
    omitted = DesignDraft("# Crumb", {"project": ""}, [], [])
    assert _bind_contract_project(omitted, "crumb").execution_contract["project"] == "crumb"

    conflicting = DesignDraft("# Other", {"project": "other"}, [], [])
    with pytest.raises(ValueError, match="wrong project"):
        _bind_contract_project(conflicting, "crumb")


def test_provider_repair_cannot_drop_prior_blocking_unknowns() -> None:
    returned = DesignDraft(
        "# Repaired",
        {"project": "crumb"},
        [],
        ["[GROUNDING_PENDING] new acquisition fact"],
    )
    protected = _preserve_blocking_unknowns(
        returned, ["[USER_DECISION] existing owner choice"]
    )
    assert protected.blocking_unknowns == [
        "[USER_DECISION] existing owner choice",
        "[GROUNDING_PENDING] new acquisition fact",
    ]


def test_provider_repair_collapses_equivalent_unknown_rephrasings() -> None:
    returned = DesignDraft(
        "# Repaired",
        {"project": "demo"},
        [],
        [
            "[USER_DECISION] Supply the GPIO connected to the shared "
            "microphone WS/LRCLK input."
        ],
    )
    protected = _preserve_blocking_unknowns(
        returned,
        [
            "[USER_DECISION] Which GPIO is connected to the shared "
            "microphone WS/LRCLK signal?"
        ],
    )
    assert len(protected.blocking_unknowns) == 1


def test_provider_repair_preserves_registry_facts_but_allows_reclassification():
    prior = {
        "subsystems": [],
        "component_selections": [
            {
                "subsystem_id": "nand",
                "registry_candidates": ["vendor/nand"],
                "registry_candidate_details": [{"component": "vendor/nand"}],
                "provider_receipt_id": "receipt-1",
            },
            {
                "subsystem_id": "button",
                "registry_candidates": [],
                "provider_receipt_id": "receipt-2",
            },
        ],
        "datasheets": [],
    }
    returned = DesignDraft(
        "# Repair",
        {
            "project": "demo",
            "subsystems": [
                {
                    "id": "nand",
                    "classification": "external_part",
                    "registry_search_required": True,
                },
                {
                    "id": "button",
                    "classification": "project_custom",
                    "registry_search_required": False,
                },
            ],
            "component_selections": [],
            "datasheets": [],
        },
        [],
        [],
    )
    protected = _preserve_grounding_facts(returned, prior)
    assert [
        item["subsystem_id"]
        for item in protected.execution_contract["component_selections"]
    ] == ["nand"]
    assert protected.execution_contract["component_selections"][0][
        "registry_candidates"
    ] == ["vendor/nand"]


def test_design_command_disables_outer_plugins_and_mcp(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("orchestrator.design_package.codex_command", lambda: ["codex"])
    command = _codex_design_command(tmp_path / "work", tmp_path / "schema.json", tmp_path / "output.json")
    assert "--ignore-user-config" in command
    assert "--skip-git-repo-check" in command
    for feature in ("apps", "plugins", "remote_plugin", "workspace_dependencies"):
        assert ["--disable", feature] == command[command.index(feature) - 1:command.index(feature) + 1]
    assert ["-c", "mcp_servers={}"] == command[command.index("mcp_servers={}") - 1:command.index("mcp_servers={}") + 1]
    assert ["--sandbox", "read-only"] == command[command.index("--sandbox"):command.index("--sandbox") + 2]
    assert "--dangerously-bypass-approvals-and-sandbox" not in command


def test_design_repairs_read_contracts_from_local_context() -> None:
    source = Path("orchestrator/design_package.py").read_text(encoding="utf-8")
    assert 'repair_context / "affected-contract.json"' in source
    assert 'structural-repair-contract.json' in source
    assert 'CURRENT CONTRACT:' not in source


def test_provider_prompt_states_runtime_evidence_and_batch_setup_constraints() -> None:
    source = Path("orchestrator/design_package.py").read_text(encoding="utf-8")
    assert "Do not require artifact or\nprotocol_receipt for Tier A/B" in source
    assert "Each verification_batch has exactly one test_setup value" in source


def test_automatic_evidence_normalization_keeps_tier_c_artifact_contract() -> None:
    draft = DesignDraft("spec", {"verification": [
        {"tier": "A", "evidence_contract": {"required_kinds": ["serial_log", "artifact"]}},
        {"tier": "B", "evidence_contract": {"required_kinds": ["protocol_receipt", "firmware_hash"]}},
        {"tier": "C", "evidence_contract": {"required_kinds": ["artifact", "user_confirmation"]}},
    ]}, [], [])
    normalized = _normalize_automatic_evidence_kinds(draft)
    rows = normalized.execution_contract["verification"]
    assert rows[0]["evidence_contract"]["required_kinds"] == ["serial_log"]
    assert rows[1]["evidence_contract"]["required_kinds"] == ["firmware_hash"]
    assert rows[2]["evidence_contract"]["required_kinds"] == ["artifact", "user_confirmation"]


def test_final_contract_normalization_aligns_tier_c_and_workflow() -> None:
    value = normalize_design_execution_contract({
        "workflow": {"stages": ["preflight", "approval"]},
        "tier_c": [{"test_id": "physical", "evidence_contract": {"required_kinds": ["artifact"]}}],
        "verification": [
            {"tier": "A", "test_id": "http", "evidence_required": ["protocol_receipt"],
             "evidence_contract": {"observation": "protocol", "required_kinds": ["protocol_receipt", "serial_log"]}},
            {"tier": "C", "test_id": "physical", "evidence_contract": {"observation": "artifact", "required_kinds": ["artifact"]}},
        ],
    })
    assert value["workflow"]["stages"] == ["preflight", "design", "approval", "bind", "subsystems", "integration", "tier_c", "closure", "release"]
    assert value["verification"][0]["evidence_contract"] == {"observation": "serial", "required_kinds": ["serial_log"]}
    assert value["verification"][0]["evidence_required"] == ["serial_log"]
    assert value["verification"][1]["evidence_contract"]["required_kinds"] == ["artifact", "user_confirmation"]
    assert value["tier_c"][0]["evidence_contract"]["required_kinds"] == ["artifact", "user_confirmation"]


def test_final_contract_normalization_preserves_legacy_workflow_order() -> None:
    value = normalize_design_execution_contract({
        "schema_version": "1.1",
        "workflow": {"stages": []},
        "tier_c": [],
        "verification": [],
    })

    assert value["workflow"]["stages"] == [
        "preflight", "design", "approval", "bind", "subsystems",
        "tier_c", "integration", "closure", "release",
    ]


def test_repair_slice_exposes_only_rows_named_by_diagnostics() -> None:
    value = _repair_contract_slice({
        "project": "crumb",
        "requirements": [{"id": "R1"}, {"id": "R2"}],
        "subsystems": [{"id": "button"}, {"id": "audio"}],
        "verification": [
            {"test_id": "button_physical", "requirement_id": "R1", "owner": "button"},
            {"test_id": "audio_physical", "requirement_id": "R2", "owner": "audio"},
        ],
        "tier_c": [{"test_id": "button_physical"}, {"test_id": "audio_physical"}],
    }, ["button_physical Tier C evidence_contract requires user_confirmation"])
    assert [row["test_id"] for row in value["verification"]] == ["button_physical"]
    assert [row["id"] for row in value["requirements"]] == ["R1"]
    assert [row["id"] for row in value["subsystems"]] == ["button"]
    assert [row["test_id"] for row in value["tier_c"]] == ["button_physical"]


def test_provider_prompt_uses_file_backed_stdin_for_authoritative_timeout() -> None:
    source = Path("orchestrator/design_package.py").read_text(encoding="utf-8")
    assert 'prompt_path = work / "provider-prompt.txt"' in source
    assert "stdin=prompt_handle" in source
    assert "communicate(input=instruction" not in source


def test_repair_context_carries_receipt_bound_datasheet_and_registry_sources(tmp_path: Path) -> None:
    project = "crumb"
    project_dir = tmp_path / "projects" / project
    project_dir.joinpath("execution", "receipts", "design").mkdir(parents=True)
    datasheet = tmp_path / "hardware" / "datasheets" / "objects" / "sha256" / "part.pdf"
    datasheet.parent.mkdir(parents=True)
    datasheet.write_bytes(b"official datasheet")
    datasheet_receipt = project_dir / "execution" / "receipts" / "design" / "ds-1.json"
    datasheet_receipt.write_text("{\"receipt_id\":\"ds-1\"}\n", encoding="utf-8")
    registry_receipt = project_dir / "execution" / "receipts" / "design" / "reg-1.json"
    registry_receipt.write_text("{\"receipt_id\":\"reg-1\"}\n", encoding="utf-8")

    context = _stage_grounding_context(
        tmp_path,
        project,
        tmp_path / "work",
        {
            "datasheets": [{"subsystem_id": "chip", "source": "hardware/datasheets/objects/sha256/part.pdf", "provider_receipt_id": "ds-1"}],
            "component_selections": [{"subsystem_id": "chip", "provider_receipt_id": "reg-1"}],
        },
    )

    assert (context / "chip.pdf").read_bytes() == b"official datasheet"
    assert (context / "chip-datasheet-receipt.json").is_file()
    assert (context / "chip-selection-receipt.json").is_file()
    manifest = json.loads((context / "manifest.json").read_text(encoding="utf-8"))
    assert {item["kind"] for item in manifest["sources"]} >= {"datasheet", "datasheet_receipt", "selection_receipt"}
