from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from ..models import Failure, FailureCategory, Receipt
from ..storage import ProjectStore, file_ref


SERIAL_TOOLS = {"monitor_boot", "monitor_start", "monitor_read", "monitor_send", "monitor_stop"}
REGISTRY_TOOLS = {"search_components"}


class CapabilityAdapter:
    """Protocol-level Stage 0 probes for mandatory MCP capabilities."""

    def __init__(self, repo_root: Path, store: ProjectStore, run_id: str):
        self.repo_root, self.store, self.run_id = repo_root.resolve(), store, run_id

    async def _probe(self) -> dict:
        server = StdioServerParameters(command=sys.executable, args=["serial_mcp.py"], cwd=str(self.repo_root))
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                serial_listing = await session.list_tools()
                serial_names = {tool.name for tool in serial_listing.tools}
                stop = await session.call_tool("monitor_stop", {})
                if stop.isError or not SERIAL_TOOLS.issubset(serial_names):
                    raise RuntimeError(f"esp-serial MCP missing tools: {sorted(SERIAL_TOOLS - serial_names)}")
        headers = {"User-Agent": "esp-idf-autopilot-harness/0.1", "Accept-Language": "en-US,en;q=0.9"}
        async with httpx.AsyncClient(headers=headers, timeout=30) as client:
            async with streamable_http_client("https://components.espressif.com/mcp", http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    registry_listing = await session.list_tools()
                    registry_names = {tool.name for tool in registry_listing.tools}
                    result = await session.call_tool("search_components", {"query": "wifi"})
                    if result.isError or not REGISTRY_TOOLS.issubset(registry_names):
                        raise RuntimeError("esp-component-registry MCP search capability failed")
        return {"esp_serial": {"status": "PASS", "tools": sorted(serial_names)}, "esp_component_registry": {"status": "PASS", "tools": sorted(registry_names)}, "esp_docs": {"status": "OPTIONAL_NOT_PROBED", "fallback": "local IDF sources and validated datasheets"}}

    def probe(self) -> Receipt:
        started = datetime.now(timezone.utc).isoformat(); receipt_id = self.store.new_id("capabilities")
        log_path = self.store.logs / self.run_id / f"{receipt_id}.log"; log_path.parent.mkdir(parents=True, exist_ok=True)
        failure = None; attempts: list[dict[str, str | int]] = []; outputs = {}
        for attempt in range(1, 4):
            try:
                outputs = asyncio.run(self._probe()); success = True
                attempts.append({"attempt": attempt, "status": "PASS"})
                break
            except Exception as exc:
                success = False
                attempts.append({"attempt": attempt, "status": "FAIL", "error_type": type(exc).__name__, "summary": str(exc)})
                if attempt < 3:
                    time.sleep(attempt)
        if not success:
            final = attempts[-1]
            outputs = {"status": "OUTAGE", "attempts": attempts, "error_type": final["error_type"], "summary": final["summary"]}
            failure = Failure(category=FailureCategory.TOOL, summary=f"mandatory MCP capability probe failed after 3 attempts: {final['error_type']}: {final['summary']}")
        else:
            outputs["attempts"] = attempts
        log_path.write_text(json.dumps(outputs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        receipt = Receipt(receipt_id=receipt_id, run_id=self.run_id, operation="mcp_capability_preflight", started_at=started, finished_at=datetime.now(timezone.utc).isoformat(), success=success, command=["MCP", "initialize/list_tools/call_tool"], outputs=outputs, artifacts=[file_ref(log_path, self.store.project_dir, "application/json")], failure=failure)
        self.store.write_receipt(receipt, "preflight")
        return receipt
