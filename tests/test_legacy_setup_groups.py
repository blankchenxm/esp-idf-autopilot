from pathlib import Path


def test_legacy_owner_multiple_setups_execute_as_separate_images():
    source = Path("orchestrator/graph.py").read_text(encoding="utf-8")
    assert "setup_groups" in source
    assert "verification_setup_index" in source
    assert "execute next verification setup" in source
