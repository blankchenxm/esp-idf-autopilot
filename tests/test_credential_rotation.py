import json

from orchestrator.credential_rotation import authorize_credential_rotation
from orchestrator.design_package import _input_refs
from orchestrator.storage import atomic_write_json
from orchestrator.validators import design_digest, validate_design_package
from tests.test_design_contract import package


def test_explicit_rotation_allows_legacy_raw_manifest_only_after_public_check(tmp_path):
    design = package(tmp_path)
    requirement = tmp_path / "requirements" / "fixture.md"
    requirement.write_text("wifi password: one\nGPIO 4\n", encoding="utf-8")
    # Legacy package retains a raw input reference and a frozen public authority.
    from orchestrator.input_authority import write_input_authority
    write_input_authority(design / "input-authority.json", tmp_path, "fixture")
    manifest = json.loads((design / "manifest.json").read_text(encoding="utf-8"))
    from orchestrator.storage import file_ref
    manifest["inputs"] = [file_ref(requirement, tmp_path, "text/markdown").model_dump(), manifest["inputs"][1]]
    contract = json.loads((design / "execution-contract.json").read_text(encoding="utf-8"))
    manifest["design_digest"] = design_digest(contract, manifest)
    atomic_write_json(design / "manifest.json", manifest)
    for name in ("approval.json", "design-validation.json"):
        value = json.loads((design / name).read_text(encoding="utf-8")); value["design_digest"] = manifest["design_digest"]; atomic_write_json(design / name, value)
    requirement.write_text("wifi password: two\nGPIO 4\n", encoding="utf-8")
    assert any("hash/size mismatch" in error for error in validate_design_package(design)[1])
    result = authorize_credential_rotation(tmp_path, "fixture", design)
    assert result["authorized"] is True
    assert validate_design_package(design, require_approval=True)[1] == []


def test_rotation_rejects_public_authority_change(tmp_path):
    design = package(tmp_path)
    requirement = tmp_path / "requirements" / "fixture.md"
    requirement.write_text("wifi password: one\n", encoding="utf-8")
    from orchestrator.input_authority import write_input_authority
    write_input_authority(design / "input-authority.json", tmp_path, "fixture")
    requirement.write_text("wifi password: two\nGPIO 4\n", encoding="utf-8")
    try:
        authorize_credential_rotation(tmp_path, "fixture", design)
    except ValueError as exc:
        assert "public design authority" in str(exc)
    else:
        raise AssertionError("public design change was accepted as credential rotation")
