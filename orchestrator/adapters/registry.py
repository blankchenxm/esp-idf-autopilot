from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import sys
from pathlib import Path
from collections.abc import Callable
from typing import Any

from ..codex_runner import background_creationflags

# The CLI adds .vendor to sys.path for project dependencies.  MCP is currently
# installed in the user Python environment; importing it with vendor httpx
# mixes incompatible transport implementations.  Import MCP and HTTPX from the
# same environment, then restore the project path for the rest of the harness.
_vendor = str(Path(__file__).resolve().parents[2] / ".vendor")
_removed = [entry for entry in sys.path if Path(entry or ".").resolve() == Path(_vendor).resolve()]
sys.path[:] = [entry for entry in sys.path if entry not in _removed]
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
sys.path[:0] = _removed


class RegistryUnavailable(RuntimeError):
    pass


def _exception_summary(exc: BaseException) -> str:
    """Keep nested async transport failures useful in immutable Receipts."""
    if isinstance(exc, BaseExceptionGroup):
        children = "; ".join(_exception_summary(item) for item in exc.exceptions)
        return f"async transport failure: {children}"
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _content(result: Any) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") if hasattr(item, "model_dump") else {"text": str(item)} for item in result.content]


def _candidate_ids(values: list[dict[str, Any]]) -> list[str]:
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            namespace = value.get("namespace_name") or value.get("namespace")
            name = value.get("component_name") or value.get("name")
            component = value.get("component")
            if namespace and name:
                found.add(f"{namespace}/{name}")
            if isinstance(component, str) and "/" in component:
                found.add(component)
            text = value.get("text")
            if isinstance(text, str):
                try:
                    visit(json.loads(text))
                except json.JSONDecodeError:
                    pass
            for child in value.values():
                if child is not text:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(values)
    return sorted(found)


class RegistryAdapter:
    """Actual ESP Component Registry MCP client with injectable test seams."""

    def __init__(self, search: Callable[[str], list[dict[str, Any]]] | None = None, details: Callable[[str], dict[str, Any]] | None = None, endpoint: str = "https://components.espressif.com/mcp", attempts: int = 3):
        self._search, self._details, self.endpoint, self.attempts = search, details, endpoint, attempts

    async def _call(self, tool: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        headers = {"User-Agent": "esp-idf-autopilot-harness/1.0", "Accept-Language": "en-US,en;q=0.9"}
        async with httpx.AsyncClient(headers=headers, timeout=45) as client:
            async with streamable_http_client(self.endpoint, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments)
                    if result.isError:
                        raise RegistryUnavailable(f"Registry MCP {tool} returned an error")
                    return _content(result)

    def _isolated_search(self, query: str) -> list[dict[str, Any]]:
        """Use MCP with its matching site-packages, outside the CLI vendor path."""
        program = """import asyncio,json\nfrom mcp import ClientSession\nfrom mcp.client.streamable_http import streamable_http_client\nasync def main():\n async with streamable_http_client('https://components.espressif.com/mcp') as (read,write,_):\n  async with ClientSession(read,write) as s:\n   await s.initialize(); r=await s.call_tool('search_components',{'query':QUERY})\n   items=[x.model_dump(mode='json') if hasattr(x,'model_dump') else {'text':str(x)} for x in r.content]\n   if r.isError:\n    text=' '.join(str(x.get('text','')) for x in items).lower()\n    if 'failed to fetch the components' in text: items=[]\n    else: raise RuntimeError('Registry MCP returned an error: '+text)\n   print(json.dumps(items))\nasyncio.run(main())\n""".replace("QUERY", repr(query))
        env = os.environ.copy(); env.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, "-c", program], env=env, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, creationflags=background_creationflags())
        if result.returncode:
            raise RuntimeError(result.stderr[-1000:] or result.stdout[-1000:])
        return json.loads(result.stdout)

    def search(self, query: str) -> list[dict[str, Any]]:
        if self._search:
            return self._search(query)
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                return self._isolated_search(query)
            except Exception as exc:
                last = exc
                if attempt + 1 < self.attempts: time.sleep(1 << attempt)
        raise RegistryUnavailable(
            f"Registry search failed after {self.attempts} attempts for {query!r}: "
            f"{_exception_summary(last)}"
        ) from None

    def search_pair(self, exact: str, capability: str) -> dict[str, Any]:
        exact_results = self.search(exact)
        capability_results = self.search(capability)
        candidates = _candidate_ids(exact_results + capability_results)
        details: list[dict[str, Any]] = []
        detail_errors: list[dict[str, str]] = []
        # A search result is not auditable until every discovered candidate has
        # either a detail payload or its own failed-detail receipt.  Do not cap
        # this loop: doing so silently omitted candidates after the first eight.
        for component in candidates:
            try:
                details.append(self.candidate_details(component))
            except Exception as exc:
                detail_errors.append(
                    {
                        "component": component,
                        "summary": f"{type(exc).__name__}: {exc}",
                    }
                )
        return {
            "status": "ok",
            "exact_query": exact,
            "exact_results": exact_results,
            "capability_query": capability,
            "capability_results": capability_results,
            "candidate_ids": candidates,
            "candidate_details": details,
            "candidate_detail_errors": detail_errors,
        }

    def candidate_details(self, component: str) -> dict[str, Any]:
        if self._details:
            last: Exception | None = None
            for attempt in range(self.attempts):
                try:
                    return self._details(component)
                except Exception as exc:
                    last = exc
                    if attempt + 1 < self.attempts:
                        time.sleep(1 << attempt)
            raise RegistryUnavailable(
                f"Registry detail lookup failed for {component!r} after "
                f"{self.attempts} attempts: {_exception_summary(last)}"
            ) from None
        if "/" not in component:
            raise RegistryUnavailable("Registry component identifier must be namespace/name")
        namespace, name = component.split("/", 1)
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                return {"component": component, "content": asyncio.run(self._call("fetch_component_detailed_information", {"namespace_name": namespace, "component_name": name}))}
            except Exception as exc:
                last = exc
                if attempt + 1 < self.attempts:
                    time.sleep(1 << attempt)
        raise RegistryUnavailable(
            f"Registry detail lookup failed for {component!r} after "
            f"{self.attempts} attempts: {_exception_summary(last)}"
        ) from None
