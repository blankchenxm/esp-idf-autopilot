from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import asyncio
import os
import subprocess

from orchestrator.adapters.hardware import HardwareAdapter
from orchestrator.adapters.idf import IdfAdapter
from orchestrator.adapters.serial import SerialAdapter
from orchestrator.codex_runner import background_creationflags, codex_creationflags, codex_environment, hidden_powershell_command, hidden_startupinfo, isolated_codex_profile, is_authentication_failure, terminate_process_tree
from orchestrator.models import HardwareIdentity
from orchestrator.models import FailureCategory
from orchestrator.policies import classify_failure
from orchestrator.storage import ProjectStore
from orchestrator.graph import HarnessNodes


def test_idf_adapter_always_uses_wrapper(tmp_path: Path):
    repo = tmp_path / "repo"; (repo / "tools").mkdir(parents=True); (repo / "tools" / "idf.ps1").write_text("", encoding="utf-8")
    project = repo / "projects" / "p"; store = ProjectStore(project); store.ensure(); adapter = IdfAdapter(repo, store, "run")
    class Process:
        returncode = 0

        def communicate(self, timeout=None):
            return "ESP-IDF v6", None

    with patch("subprocess.Popen", return_value=Process()):
        receipt = adapter.version()
    assert any("idf.ps1" in argument for argument in receipt.command) and receipt.command[-1] == "--version"


def test_serial_cleanup_runs_after_error(tmp_path: Path):
    store = ProjectStore(tmp_path); store.ensure(); adapter = SerialAdapter(tmp_path, store, "run")
    with patch.object(adapter, "_worker_call", side_effect=RuntimeError("busy")) as call:
        receipt = adapter.capture_boot("COM4", 115200, 1, "MARKER")
    assert not receipt.success and call.call_count == 1


def test_serial_capture_excludes_monitor_status_line_from_firmware_evidence(tmp_path: Path):
    store = ProjectStore(tmp_path); store.ensure(); adapter = SerialAdapter(tmp_path, store, "run")
    monitor_output = 'Boot log from COM4 (stopped on "MARKER"):\nI app: MARKER\n'
    with patch.object(adapter, "_worker_call", return_value=monitor_output):
        receipt = adapter.capture_boot("COM4", 115200, 1, "MARKER")
    log = (store.logs / "run" / f"{receipt.receipt_id}.log").read_text(encoding="utf-8")
    assert receipt.success
    assert log.count("MARKER") == 1


def test_serial_transaction_reuses_exact_integrity_valid_receipt(tmp_path: Path):
    store = ProjectStore(tmp_path)
    store.ensure()
    adapter = SerialAdapter(tmp_path, store, "run")
    with patch.object(
        adapter, "_worker_call", return_value="I app: READY\n"
    ) as call:
        first = adapter.capture_boot(
            "COM4", 115200, 1, "READY", idempotency_key="serial-key"
        )
        second = adapter.capture_boot(
            "COM4", 115200, 1, "READY", idempotency_key="serial-key"
        )
    assert first.receipt_id == second.receipt_id
    assert call.call_count == 1


def test_integration_source_gate_accepts_component_owned_integration_logic(tmp_path: Path):
    project = tmp_path / "projects" / "p"
    (project / "main").mkdir(parents=True)
    (project / "components" / "system" ).mkdir(parents=True)
    (project / "CMakeLists.txt").write_text("", encoding="utf-8")
    (project / "main" / "main.c").write_text("void app_main(void) {}", encoding="utf-8")
    (project / "components" / "system" / "logic.c").write_text(
        'const char *marker = "INTEGRATION_PASS";', encoding="utf-8")
    HarnessNodes._require_owned_source(project, "integration", [
        {"expected": {"marker": "INTEGRATION_PASS"}}
    ])


def test_serial_capture_bounds_a_stuck_mcp_call(tmp_path: Path):
    store = ProjectStore(tmp_path); store.ensure(); adapter = SerialAdapter(tmp_path, store, "run")

    async def stuck(*_args, **_kwargs):
        await asyncio.sleep(60)

    with patch.object(adapter, "_call", side_effect=stuck):
        try:
            asyncio.run(adapter._bounded_call("monitor_boot", {}, 0.01))
        except TimeoutError:
            pass
        else:
            raise AssertionError("stuck MCP call was not bounded")


def test_serial_worker_timeout_kills_the_owned_process_tree(tmp_path: Path):
    store = ProjectStore(tmp_path); store.ensure(); adapter = SerialAdapter(tmp_path, store, "run")

    class Process:
        pid = 1234
        returncode = None
        calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise __import__("subprocess").TimeoutExpired("serial-worker", timeout)
            return "", None

    process = Process()
    with patch("subprocess.Popen", return_value=process), patch("subprocess.run") as kill:
        try:
            adapter._worker_call("monitor_boot", {}, 0.01)
        except TimeoutError:
            pass
        else:
            raise AssertionError("worker timeout was not surfaced")
    assert kill.call_args.args[0][:4] == ["taskkill", "/PID", "1234", "/T"]


def test_hardware_identity_survives_port_change():
    expected = HardwareIdentity(chip="ESP32", mac="aa:bb:cc:dd:ee:ff"); actual = HardwareIdentity(chip="ESP32", mac="aa:bb:cc:dd:ee:ff")
    assert HardwareAdapter.same_device(expected, actual)


def test_mcp_capability_transport_failure_is_retryable_tooling():
    assert classify_failure("mandatory MCP capability probe failed: ExceptionGroup: unhandled errors in a TaskGroup") == FailureCategory.TOOL


def test_codex_child_environment_uses_user_home_only_when_not_explicit(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("orchestrator.codex_runner.Path.home", lambda: tmp_path / "user")
    assert codex_environment({})["CODEX_HOME"] == str(tmp_path / "user" / ".codex")
    assert codex_environment({"CODEX_HOME": "D:/codex-account-b"})["CODEX_HOME"] == "D:/codex-account-b"


def test_codex_child_environment_keeps_login_home_and_uses_project_temp(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("orchestrator.codex_runner.Path.home", lambda: tmp_path / "user")
    temporary_dir = tmp_path / "runtime" / "codex-tmp"; temporary_dir.mkdir(parents=True)
    environment = codex_environment({"CODEX_HOME": "D:/cockpit-active-account"}, temporary_dir=temporary_dir)
    assert environment["CODEX_HOME"] == "D:/cockpit-active-account"
    assert environment["TMPDIR"] == str(temporary_dir.resolve())
    assert environment["TMP"] == str(temporary_dir.resolve())
    assert environment["TEMP"] == str(temporary_dir.resolve())


def test_codex_authentication_failure_detection_supports_cockpit_rotation():
    assert is_authentication_failure("Codex CLI: Not logged in")
    assert is_authentication_failure("401 Unauthorized: Missing bearer or basic authentication")
    assert not is_authentication_failure("design provider completed successfully")


def test_nested_codex_uses_no_window_creation_flags_on_windows():
    flags = codex_creationflags()
    if os.name == "nt":
        assert flags & getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        assert flags & getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    else:
        assert flags == 0


def test_all_background_children_use_no_window_creation_flags_on_windows():
    flags = background_creationflags()
    if os.name == "nt":
        assert flags & getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    else:
        assert flags == 0


def test_background_children_use_hidden_startupinfo_on_windows():
    startupinfo = hidden_startupinfo()
    if os.name == "nt":
        assert startupinfo is not None
        assert startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert startupinfo.wShowWindow == subprocess.SW_HIDE
    else:
        assert startupinfo is None


def test_harness_powershell_command_is_noninteractive_and_hidden():
    command = hidden_powershell_command("-Command", "Write-Output ok")
    assert command[:6] == [
        "powershell", "-NoLogo", "-NoProfile", "-NonInteractive",
        "-WindowStyle", "Hidden",
    ]


def test_isolated_codex_profile_stays_under_project_runtime_and_cleans_up(tmp_path: Path):
    source_home = tmp_path / "user-home"; source_home.mkdir()
    (source_home / "auth.json").write_text("first-login", encoding="utf-8")
    (source_home / "config.toml").write_text("model = \"test\"", encoding="utf-8")
    parent = tmp_path / "repo" / "runtime" / "projects" / "p" / "codex-tmp"

    with isolated_codex_profile(parent, {"CODEX_HOME": str(source_home)}) as profile:
        assert profile.home.parent == parent.resolve()
        assert profile.environment["CODEX_HOME"] == str(profile.home)
        assert (profile.home / "auth.json").read_text(encoding="utf-8") == "first-login"
        assert not (profile.home / "config.toml").exists()
        assert Path(profile.environment["TEMP"]).parent == profile.home
        (source_home / "auth.json").write_text("rotated-login", encoding="utf-8")
        profile.refresh_auth()
        assert (profile.home / "auth.json").read_text(encoding="utf-8") == "rotated-login"

    assert parent.is_dir()
    assert list(parent.glob("profile-*")) == []


def test_terminate_process_tree_kills_windows_descendants():
    class Process:
        pid = 4321
        waits = 0
        killed = False

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise __import__("subprocess").TimeoutExpired("codex", timeout)
            return 0

        def kill(self):
            self.killed = True

    process = Process()
    with patch("subprocess.run") as taskkill:
        terminate_process_tree(process, wait_seconds=1)
    assert taskkill.call_args.args[0] == ["taskkill", "/PID", "4321", "/T", "/F"]
    assert process.killed
