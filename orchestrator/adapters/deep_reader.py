from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol

from ..codex_runner import (
    codex_command,
    codex_creationflags,
    isolated_codex_profile,
    terminate_process_tree,
)
from ..runtime_paths import ProjectRuntime


class DatasheetDeepReader(Protocol):
    """Extract only implementable L2 facts from receipt-owned text."""

    def read(
        self, project: str, subsystem_id: str, part_number: str,
        extracted_text: str, extracted_text_sha256: str,
        requested_facts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]: ...


class CodexDatasheetDeepReader:
    """Replaceable, bounded L2 reader with a narrow structured output.

    It deliberately receives neither the complete design contract nor product
    requirements.  That keeps fact acquisition separate from product design
    and prevents a broad repair prompt from dropping extracted facts.
    """

    def __init__(self, repo_root: Path, timeout: int = 300):
        self.repo_root = repo_root.resolve()
        self.timeout = timeout

    def read(
        self, project: str, subsystem_id: str, part_number: str,
        extracted_text: str, extracted_text_sha256: str,
        requested_facts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        runtime = ProjectRuntime(self.repo_root, project).ensure().design_provider
        with tempfile.TemporaryDirectory(prefix=f"{subsystem_id}-l2-", dir=runtime) as raw:
            work = Path(raw)
            text_path = work / "datasheet-extracted.txt"
            text_path.write_text(extracted_text, encoding="utf-8")
            output = work / "output.json"
            schema = self.repo_root / "schemas" / "datasheet-deep-reader-output.schema.json"
            requested = requested_facts or []
            scope = (
                "Return facts only for these requested operation gaps, using "
                "the operation string verbatim as each fact's parameter:\n"
                + json.dumps(requested, ensure_ascii=False, indent=2)
                if requested
                else (
                    "Return only implementation facts explicitly supported by "
                    "the text for the requested subsystem."
                )
            )
            prompt = f"""Read datasheet-extracted.txt for exact part {part_number!r}.
{scope}
Do not summarize unrelated registers, commands, timing, geometry, or formats. Omit a requested
operation if the text does not establish it. `value` must be one concise factual string, never an
object or JSON. Never infer values. Every quote must be a verbatim searchable excerpt from the
text. The extraction SHA-256 is {extracted_text_sha256}."""
            command = codex_command() + [
                "exec", "--ephemeral", "--ignore-user-config",
                "--sandbox", "read-only", "-c", "mcp_servers={}",
                "-C", str(work), "--output-schema", str(schema),
                "--output-last-message", str(output), "-",
            ]
            with isolated_codex_profile(runtime / "codex-tmp") as profile:
                process = subprocess.Popen(
                    command, cwd=work, env=profile.environment, text=True,
                    encoding="utf-8", errors="replace", stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=codex_creationflags(),
                )
                try:
                    stdout, _ = process.communicate(prompt, timeout=self.timeout)
                except subprocess.TimeoutExpired as exc:
                    terminate_process_tree(process)
                    raise RuntimeError(
                        f"datasheet deep reader timed out after {self.timeout} seconds"
                    ) from exc
            if process.returncode != 0:
                raise RuntimeError(f"datasheet deep reader exited {process.returncode}: {stdout[-1000:]}")
            value = json.loads(output.read_text(encoding="utf-8"))
            facts = value.get("facts")
            if not isinstance(facts, list) or not facts:
                raise ValueError("datasheet deep reader produced no implementation facts")
            return facts
