from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..models import Failure, FailureCategory, Receipt
from ..codex_runner import background_creationflags, hidden_startupinfo
from ..storage import ProjectStore, file_ref


class SerialAdapter:
    """Bounded esp-serial MCP client; every call owns and releases its stdio server."""

    def __init__(self, repo_root: Path, store: ProjectStore, run_id: str):
        self.repo_root, self.store, self.run_id = repo_root.resolve(), store, run_id

    async def _call(self, name: str, arguments: dict, timeout_s: float | None = None) -> str:
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "orchestrator.serial_server", "--run-id", self.run_id],
            cwd=str(self.repo_root),
        )
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=10)
                try:
                    operation = session.call_tool(name, arguments)
                    result = await asyncio.wait_for(operation, timeout=timeout_s) if timeout_s else await operation
                    return "\n".join(getattr(item, "text", "") for item in result.content)
                finally:
                    # A failed monitor server must not make its own cleanup an
                    # unbounded second failure.  Stdio context teardown then
                    # owns process termination.
                    with suppress(Exception, asyncio.CancelledError):
                        await asyncio.wait_for(session.call_tool("monitor_stop", {}), timeout=5)

    def stop(self) -> None:
        try:
            self._worker_call("monitor_stop", {}, timeout_s=10)
        except Exception:
            pass
        finally:
            self._reap_interrupted_server()

    def _reap_interrupted_server(self) -> None:
        """Release only a tagged server orphan from this exact Harness run."""
        if not self.run_id or not re.fullmatch(r"[A-Za-z0-9._:-]+", self.run_id):
            return
        # A normal worker exits its stdio child.  This is solely a recovery
        # boundary for a parent process killed by an external session timeout.
        script = (
            "$run = " + repr(self.run_id) + "; "
            "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
            "Where-Object { $_.CommandLine -match 'orchestrator\\.serial_server' -and $_.CommandLine -match [regex]::Escape($run) } | "
            "ForEach-Object { taskkill /PID $_.ProcessId /T /F | Out-Null }"
        )
        with suppress(Exception):
            subprocess.run(["powershell", "-NoProfile", "-Command", script], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False, timeout=10)

    async def _bounded_call(self, name: str, arguments: dict, timeout_s: float) -> str:
        return await asyncio.wait_for(self._call(name, arguments, timeout_s=timeout_s), timeout=timeout_s + 5.0)

    def _worker_call(self, name: str, arguments: dict, timeout_s: float) -> str:
        """Execute one MCP call in a killable process group.

        Async cancellation is cooperative; on Windows a stuck stdio transport
        can otherwise keep a LangGraph node alive past its declared deadline.
        The worker contains the MCP server as a descendant, so ``taskkill /T``
        releases both the monitor and the COM port deterministically.
        """
        command = [sys.executable, "-m", "orchestrator.serial_worker", "--repo", str(self.repo_root),
                   "--name", name, "--arguments-json", json.dumps(arguments), "--timeout", str(timeout_s),
                   "--run-id", self.run_id]
        process = subprocess.Popen(command, cwd=self.repo_root, text=True, encoding="utf-8", errors="replace",
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   creationflags=background_creationflags(), startupinfo=hidden_startupinfo())
        try:
            output, _ = process.communicate(timeout=timeout_s + 5.0)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
            output, _ = process.communicate()
            raise TimeoutError(f"serial MCP worker exceeded {timeout_s + 5.0:.1f}s")
        if process.returncode:
            raise RuntimeError(f"serial MCP worker exited {process.returncode}: {output[-1000:]}")
        return output

    def capture_boot(
        self,
        port: str,
        baud: int,
        timeout: float,
        marker: str | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> Receipt:
        if idempotency_key and (
            cached := self.store.find_successful_receipt(
                "serial_boot_capture", idempotency_key
            )
        ):
            return cached
        started = datetime.now(timezone.utc).isoformat(); receipt_id = self.store.new_id("serial")
        log_path = self.store.logs / self.run_id / f"{receipt_id}.log"; log_path.parent.mkdir(parents=True, exist_ok=True)
        error = None
        try:
            arguments = {"port": port, "baudrate": baud, "capture_duration": max(1, min(int(timeout), 120))}
            if marker: arguments["wait_for"] = marker
            # MCP transport/process setup is outside monitor_boot's own capture
            # duration. Bound the complete transaction so a stuck stdio child
            # cannot strand the graph after its declared serial timeout.
            transaction_timeout = float(arguments["capture_duration"]) + 15.0
            text = self._worker_call("monitor_boot", arguments, transaction_timeout)
        except Exception as exc:
            error = exc; text = f"{type(exc).__name__}: {exc}"
        finally:
            self._reap_interrupted_server()
        # The monitor prepends a human transport-status line such as
        # ``Boot log ... (stopped on \"MARKER\")``.  It is not firmware output;
        # retaining it makes exact marker counts report a phantom event.
        text = "\n".join(
            line for line in text.splitlines()
            if not line.startswith("Boot log from ")
        )
        log_path.write_text(text, encoding="utf-8")
        marker_found = bool(marker and marker in text)
        success = error is None and (not marker or marker_found)
        failure = None if success else Failure(category=FailureCategory.SERIAL, summary=(f"serial MCP error: {error}" if error else f"expected marker missing: {marker}"))
        receipt = Receipt(receipt_id=receipt_id, run_id=self.run_id, operation="serial_boot_capture", started_at=started, finished_at=datetime.now(timezone.utc).isoformat(), success=success, inputs={"port": port, "baud": baud, "timeout": timeout, "expected_marker": marker, "idempotency_key": idempotency_key}, outputs={"marker_found": marker_found, "bounded_capture": not bool(marker)}, artifacts=[file_ref(log_path, self.store.project_dir, "text/plain")], failure=failure)
        self.store.write_receipt(receipt, "serial"); return receipt
