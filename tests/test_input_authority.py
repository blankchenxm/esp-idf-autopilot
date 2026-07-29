from __future__ import annotations

from pathlib import Path

from orchestrator.input_authority import compile_input_authority, exact_authorized_identifier
from orchestrator.design_inventory import reconcile_grounding_unknowns


def test_authority_keeps_exact_part_and_redacts_secret(tmp_path: Path) -> None:
    (tmp_path / "requirements").mkdir(); (tmp_path / "connections").mkdir()
    (tmp_path / "requirements" / "p.md").write_text("Part W25N01GV; password: super-secret\n", encoding="utf-8")
    (tmp_path / "connections" / "p.md").write_text("GPIO 5;\n#define CAMERA_PIN 6\nhttps://example.test/upload\n", encoding="utf-8")
    authority = compile_input_authority(tmp_path, "p")
    assert exact_authorized_identifier(authority, "W25N01GV")
    assert not exact_authorized_identifier(authority, "W25N01GVZEIG")
    assert "super-secret" not in str(authority)
    assert authority["secret_references"]
    assert {item["gpio"] for item in authority["pins"]} == {5, 6}


def test_authority_does_not_classify_alphanumeric_secret_as_part(tmp_path: Path) -> None:
    (tmp_path / "requirements").mkdir(); (tmp_path / "connections").mkdir()
    secret = "networkpass19720530"
    (tmp_path / "requirements" / "p.md").write_text(
        f"Part W25N01GV; wifi password: {secret}\n", encoding="utf-8"
    )
    (tmp_path / "connections" / "p.md").write_text("GPIO 5\n", encoding="utf-8")

    authority = compile_input_authority(tmp_path, "p")

    assert exact_authorized_identifier(authority, "W25N01GV")
    assert not exact_authorized_identifier(authority, secret)
    assert secret not in str(authority)


def test_authority_extracts_chinese_gpio_phrase_adjacent_to_a_field() -> None:
    # Compile directly from a temporary project so Unicode word-boundary rules
    # cannot silently drop `gpio引脚27` after a Chinese field label.
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        project_root = Path(directory)
        (project_root / "requirements").mkdir(); (project_root / "connections").mkdir()
        (project_root / "requirements" / "p.md").write_text("part X1Y2", encoding="utf-8")
        (project_root / "connections" / "p.md").write_text("scl_pin连接gpio引脚27", encoding="utf-8")
        assert compile_input_authority(project_root, "p")["pins"][0]["gpio"] == 27


def test_typed_unknown_routing_does_not_inspect_prose() -> None:
    active, resolved = reconcile_grounding_unknowns({}, [
        {"code": "IMPLEMENTATION_READINESS", "owner": "camera", "category": "implementation_readiness", "phase": "execution", "authority": "external_fact", "severity": "blocking_before_implementation", "required_operations": ["capture"], "resolution": None},
        {"code": "USER_DECISION", "owner": "product", "category": "policy", "phase": "design", "authority": "user_input", "severity": "blocking_before_approval", "required_operations": [], "resolution": None},
    ], [])
    assert active == [{"code": "USER_DECISION", "owner": "product", "category": "policy", "phase": "design", "authority": "user_input", "severity": "blocking_before_approval", "required_operations": [], "resolution": None}]
    assert resolved[0]["resolution_kind"] == "IMPLEMENTATION_READINESS"


def test_authoritative_pins_close_model_pin_manifest_unknown() -> None:
    active, resolved = reconcile_grounding_unknowns({}, [{
        "code": "INPUT_AMBIGUITY", "owner": "board", "category": "pin_authority_empty",
        "phase": "design", "authority": "user_input", "severity": "blocking_before_approval",
        "required_operations": [], "resolution": None,
    }], [], {"pins": [{"gpio": 4}]})
    assert not active
    assert resolved[0]["resolution"] == "Resolved from hash-bound user pin records."
