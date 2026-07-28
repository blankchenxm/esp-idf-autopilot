from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_all_f01_f37_have_future_carrier():
    text = (ROOT / "docs" / "archive" / "REFACTOR-DISCUSSION-2026-07-21.md").read_text(encoding="utf-8")
    assert set(re.findall(r"\| (F\d{2}) \|", text)) == {f"F{i:02d}" for i in range(1, 38)}


def test_archived_baselines_and_active_harness_exist():
    archive = ROOT / "docs" / "archive" / "firmware-skill-baselines"
    assert (archive / "v0-claude-original" / "SKILL_claude.md").is_file() and (archive / "v1-complete-pre-harness" / "SKILL.md").is_file()
    candidate = archive / "v2-harness-candidate" / "SKILL.md"; active = ROOT / ".agents" / "skills" / "esp-idf-firmware" / "SKILL.md"
    assert candidate.is_file() and "orchestrator" in candidate.read_text(encoding="utf-8").lower()
    active_text = active.read_text(encoding="utf-8")
    assert "orchestrator.cli" in active_text.lower()
    assert 150 <= len(active_text.splitlines()) <= 250
    for command in ("design --project", "resume --project", "status --project", "validate --project"):
        assert command in active_text
    assert "design_running" in active_text.lower()
    assert "repeating `design` observes the same" in active_text.lower()
    assert "project-scoped `job_id`" in active_text.lower()


def test_runtime_and_tests_do_not_depend_on_smoke_project():
    retired_fixture = "harness" + "_smoke"
    for area in (ROOT / "orchestrator", ROOT / "tests"):
        for path in area.rglob("*.py"):
            if path == Path(__file__):
                continue
            assert retired_fixture not in path.read_text(encoding="utf-8")
