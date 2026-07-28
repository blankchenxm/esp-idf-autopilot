import json

import orchestrator.cli as cli


def test_unexpected_cli_failure_is_structured_blocker(tmp_path, monkeypatch, capsys):
    (tmp_path / "projects").mkdir(); monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    code = cli.main(["design", "--project", "missing"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2 and payload["mode"] == "BLOCKED" and payload["project"] == "missing"
    assert "FileNotFoundError" in payload["summary"]


def test_maintenance_commands_are_project_scoped():
    for arguments in (
        ["archive-runs", "--project", "demo"],
        ["migrate-datasheets", "--project", "demo"],
        ["reconcile-state", "--project", "demo"],
        ["invalidate-evidence", "--project", "demo", "--evidence-id", "e1", "--reason", "wrong"],
    ):
        assert cli._parser().parse_args(arguments).project == "demo"
