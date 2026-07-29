from __future__ import annotations

import re
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import serial.tools.list_ports

from ..models import Failure, FailureCategory, HardwareIdentity, HardwareSession, Receipt
from ..codex_runner import background_creationflags, hidden_powershell_command, hidden_startupinfo
from ..storage import ProjectStore, file_ref


class HardwareAdapter:
    def __init__(self, repo_root: Path, store: ProjectStore, run_id: str):
        self.repo_root, self.store, self.run_id = repo_root.resolve(), store, run_id

    def enumerate(self) -> list[dict[str, str | None]]:
        return [{"port": item.device, "description": item.description, "hwid": item.hwid, "serial_number": item.serial_number} for item in serial.tools.list_ports.comports()]

    def preflight(self, port: str | None = None, baud: int = 115200, baseline_log: Path | None = None) -> tuple[HardwareSession | None, Receipt]:
        ports = self.enumerate()
        chosen = port or (ports[0]["port"] if len(ports) == 1 else None)
        started = datetime.now(timezone.utc).isoformat()
        receipt_id = self.store.new_id("preflight")
        log_path = self.store.logs / self.run_id / f"{receipt_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not chosen:
            log_path.write_text(f"Detected ports: {ports}\n", encoding="utf-8")
            failure = Failure(category=FailureCategory.HARDWARE, summary="hardware port is absent or ambiguous", retryable=False)
            receipt = Receipt(receipt_id=receipt_id, run_id=self.run_id, operation="hardware_preflight", started_at=started, finished_at=datetime.now(timezone.utc).isoformat(), success=False, inputs={"port": port, "baud": baud}, outputs={"ports": ports}, artifacts=[file_ref(log_path, self.store.project_dir, "text/plain")], failure=failure)
            self.store.write_receipt(receipt, "preflight")
            return None, receipt
        command = hidden_powershell_command("-ExecutionPolicy", "Bypass", "-File", str(self.repo_root / "hwtest" / "Test-Hardware.ps1"), "-Port", chosen)
        source = baseline_log.resolve() if baseline_log else None
        # A historical baseline may be retained as context, but it is never accepted as
        # current hardware fact. Every run performs a live probe of the enumerated port.
        if source and not source.is_file():
            raise FileNotFoundError(f"preflight baseline does not exist: {source}")
        child_env = os.environ.copy()
        child_env.pop("PYTHONPATH", None)
        # First-use ESP-IDF example builds can legitimately take several minutes.
        # The preflight program itself serializes this shared workspace and board
        # access, so allow its bounded lock/build/flash/serial transaction to end
        # normally rather than externally killing it midway through cleanup.
        process = subprocess.Popen(command, cwd=self.repo_root, env=child_env, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=background_creationflags(), startupinfo=hidden_startupinfo())
        timed_out = False
        try:
            output, _ = process.communicate(timeout=600)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            output, _ = process.communicate()
            returncode = -1
        log_path.write_text(output, encoding="utf-8")
        passed = "[PREFLIGHT] PASS" in output
        chip = re.search(r"Detecting chip type\.\.\.\s*(.+)", output)
        mac = re.search(r"MAC:\s*([0-9a-f:]{17})", output, re.I)
        revision = re.search(r"revision\s+(v?[0-9.]+)", output, re.I)
        port_info = next((item for item in ports if item["port"] == chosen), {})
        identity = HardwareIdentity(chip=(chip.group(1).strip() if chip else "ESP32"), revision=(revision.group(1) if revision else None), mac=(mac.group(1).lower() if mac else None), usb_serial=port_info.get("serial_number")) if passed else None
        summary = "hardware preflight timed out after 600 seconds" if timed_out else "hardware preflight did not emit PASS"
        failure = None if passed else Failure(category=FailureCategory.HARDWARE, summary=summary, retryable=True)
        receipt = Receipt(receipt_id=receipt_id, run_id=self.run_id, operation="hardware_preflight", started_at=started, finished_at=datetime.now(timezone.utc).isoformat(), success=passed, command=command, inputs={"port": chosen, "baud": baud, "baseline_log": str(source) if source else None}, outputs={"ports": ports, "identity": identity.model_dump() if identity else None, "process_returncode": returncode, "timed_out": timed_out}, artifacts=[file_ref(log_path, self.store.project_dir, "text/plain")], failure=failure)
        self.store.write_receipt(receipt, "preflight")
        return (HardwareSession(identity=identity, port=chosen, baud=baud) if identity else None), receipt

    @staticmethod
    def same_device(expected: HardwareIdentity, actual: HardwareIdentity) -> bool:
        return expected.stable_key() == actual.stable_key()

    def _identity_probe(self, port: str, baud: int, operation: str) -> tuple[HardwareSession | None, Receipt]:
        """Perform a bounded, read-only chip/MAC probe without flashing firmware."""
        started = datetime.now(timezone.utc).isoformat(); receipt_id = self.store.new_id("hardware_probe")
        log_path = self.store.logs / self.run_id / f"{receipt_id}.log"; log_path.parent.mkdir(parents=True, exist_ok=True)
        command = hidden_powershell_command("-ExecutionPolicy", "Bypass", "-File", str(self.repo_root / "hwtest" / "Probe-Hardware.ps1"), "-Port", port)
        child_env = os.environ.copy(); child_env.pop("PYTHONPATH", None)
        process = subprocess.Popen(command, cwd=self.repo_root, env=child_env, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=background_creationflags(), startupinfo=hidden_startupinfo())
        try:
            output, _ = process.communicate(timeout=45)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            output, _ = process.communicate()
            output = (output or "") + "\nidentity probe timed out\n"
            returncode = -1
        log_path.write_text(output, encoding="utf-8")
        chip = re.search(r"Detecting chip type\.\.\.\s*(.+)", output)
        mac = re.search(r"MAC:\s*([0-9a-f:]{17})", output, re.I)
        revision = re.search(r"revision\s+(v?[0-9.]+)", output, re.I)
        port_info = next((item for item in self.enumerate() if item["port"] == port), {})
        identity = HardwareIdentity(chip=(chip.group(1).strip() if chip else "ESP32"), revision=(revision.group(1) if revision else None), mac=(mac.group(1).lower() if mac else None), usb_serial=port_info.get("serial_number")) if returncode == 0 and mac else None
        success = identity is not None
        failure = None if success else Failure(category=FailureCategory.HARDWARE, summary="read-only hardware identity probe did not return chip MAC", retryable=True)
        receipt = Receipt(receipt_id=receipt_id, run_id=self.run_id, operation=operation, started_at=started, finished_at=datetime.now(timezone.utc).isoformat(), success=success, command=command, inputs={"port": port, "baud": baud}, outputs={"identity": identity.model_dump() if identity else None, "process_returncode": returncode}, artifacts=[file_ref(log_path, self.store.project_dir, "text/plain")], failure=failure)
        self.store.write_receipt(receipt, "preflight")
        return (HardwareSession(identity=identity, port=port, baud=baud) if identity else None), receipt

    def refresh_session(self, expected: HardwareIdentity, baud: int = 115200) -> tuple[HardwareSession | None, Receipt]:
        ports = self.enumerate(); matches = []
        if expected.usb_serial:
            matches = [item for item in ports if item.get("serial_number") == expected.usb_serial]
        elif len(ports) == 1:
            matches = ports
        if len(matches) != 1:
            started = datetime.now(timezone.utc).isoformat(); receipt_id = self.store.new_id("hardware_refresh")
            failure = Failure(category=FailureCategory.HARDWARE, summary="approved hardware identity is absent or ambiguous during session refresh", retryable=False)
            output = {"expected_identity": expected.model_dump(), "ports": ports, "matched_ports": matches}
            log_path = self.store.logs / self.run_id / f"{receipt_id}.log"; log_path.parent.mkdir(parents=True, exist_ok=True); log_path.write_text(str(output) + "\n", encoding="utf-8")
            receipt = Receipt(receipt_id=receipt_id, run_id=self.run_id, operation="hardware_session_refresh", started_at=started, finished_at=datetime.now(timezone.utc).isoformat(), success=False, inputs={"expected_key": expected.stable_key()}, outputs=output, artifacts=[file_ref(log_path, self.store.project_dir, "text/plain")], failure=failure)
            self.store.write_receipt(receipt, "preflight")
            return None, receipt
        current, receipt = self._identity_probe(matches[0]["port"], baud, "hardware_session_refresh")
        if current and self.same_device(expected, current.identity):
            return current, receipt
        started = datetime.now(timezone.utc).isoformat(); receipt_id = self.store.new_id("hardware_mismatch")
        failure = Failure(category=FailureCategory.HARDWARE, summary="live post-approval probe does not match the preflight hardware identity", retryable=False)
        output = {"expected_identity": expected.model_dump(), "actual_identity": current.identity.model_dump() if current else None, "probe_receipt_id": receipt.receipt_id}
        log_path = self.store.logs / self.run_id / f"{receipt_id}.log"; log_path.parent.mkdir(parents=True, exist_ok=True); log_path.write_text(str(output) + "\n", encoding="utf-8")
        mismatch = Receipt(receipt_id=receipt_id, run_id=self.run_id, operation="hardware_identity_binding", started_at=started, finished_at=datetime.now(timezone.utc).isoformat(), success=False, inputs={"expected_key": expected.stable_key()}, outputs=output, artifacts=[file_ref(log_path, self.store.project_dir, "text/plain")], failure=failure)
        self.store.write_receipt(mismatch, "preflight")
        return None, mismatch
