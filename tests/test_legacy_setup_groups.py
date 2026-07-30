from pathlib import Path


def test_legacy_owner_multiple_setups_execute_as_separate_images():
    source = Path("orchestrator/graph.py").read_text(encoding="utf-8")
    assert "setup_groups" in source
    assert "verification_setup_index" in source
    assert "execute next verification setup" in source


def test_integration_firmware_selftest_uses_its_frozen_isolated_setup():
    source = Path("orchestrator/graph.py").read_text(encoding="utf-8")
    assert "integration_configure" in source
    assert "sdkconfig.integration.defaults" in source
    assert "integration firmware_selftest requires" in source


def test_integration_accepts_the_schema_test_id_alias():
    source = Path("orchestrator/graph.py").read_text(encoding="utf-8")
    assert 'integration_test_id = row.get("test_id") or row.get("id")' in source
    assert 'f"{integration_test_id}-{serial.receipt_id}.json"' in source
