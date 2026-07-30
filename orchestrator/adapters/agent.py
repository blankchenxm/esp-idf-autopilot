from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..models import Failure, FailureCategory, Receipt
from ..storage import ProjectStore, file_ref
from ..codex_runner import codex_command, codex_creationflags, isolated_codex_profile, is_authentication_failure
from ..secrets import assert_no_secret_values, redact_text, redact_values, secret_values, write_crumb_credentials
from ..contract_views import owner_contract_view
from ..failure_context import select_owner_failure_logs
from ..storage import atomic_write_json
from ..runtime_paths import ProjectRuntime
from ..model_context import (
    build_owner_context_envelope,
    parse_codex_jsonl_usage,
)


@dataclass(frozen=True)
class AgentAction:
    project: str
    owner: str
    contract_path: Path
    requirement_path: Path
    connection_path: Path
    instruction: str
    failure_logs: tuple[Path, ...] = ()
    implementation_addendum_path: Path | None = None


class AgentAdapter:
    """Run Codex in a disposable project mirror, then import only project-owned source."""

    EXCLUDED = {".v", "build", "logs", "execution", "design-package", "managed_components", ".git"}
    ALLOWED_ROOT_FILES = {"CMakeLists.txt", "Kconfig.projbuild", "sdkconfig", "sdkconfig.defaults"}

    def __init__(self, repo_root: Path, store: ProjectStore, run_id: str, timeout: int = 30):
        self.repo_root, self.store, self.run_id, self.timeout = repo_root.resolve(), store, run_id, timeout

    def _copy_source(self, source: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        if not source.exists(): return
        for item in source.iterdir():
            if item.name in self.EXCLUDED: continue
            destination = target / item.name
            if item.is_dir(): shutil.copytree(item, destination, dirs_exist_ok=True)
            elif item.is_file(): shutil.copy2(item, destination)

    def execute(self, action: AgentAction) -> Receipt:
        started = datetime.now(timezone.utc).isoformat(); receipt_id = self.store.new_id("agent")
        log_path = self.store.logs / self.run_id / f"{receipt_id}.log"; log_path.parent.mkdir(parents=True, exist_ok=True)
        work_root = ProjectRuntime(self.repo_root, action.project).ensure().agent_work
        work = Path(tempfile.mkdtemp(prefix=f"{action.project}-{action.owner}-", dir=work_root))
        mirror = work / "project"; context = work / "context"
        failure = None; imported: list[str] = []
        try:
            raw_requirements = action.requirement_path.read_text(encoding="utf-8"); secrets = secret_values(raw_requirements)
            if action.project == "crumb": write_crumb_credentials(action.requirement_path, self.store.project_dir / "private" / "crumb_credentials.h")
            self._copy_source(self.store.project_dir, mirror); context.mkdir()
            contract = json.loads(action.contract_path.read_text(encoding="utf-8"))
            owner_view = owner_contract_view(contract, action.owner)
            atomic_write_json(context / "owner-contract.json", owner_view)
            addendum: dict | None = None
            if action.implementation_addendum_path is not None:
                addendum = json.loads(
                    action.implementation_addendum_path.read_text(
                        encoding="utf-8"
                    )
                )
                atomic_write_json(
                    context / "implementation-addendum.json", addendum
                )
            selected_logs = tuple(dict.fromkeys((
                *action.failure_logs,
                *select_owner_failure_logs(
                    self.store, self.run_id, action.owner
                ),
            )))
            manifest_path = action.contract_path.parent / "manifest.json"
            manifest = (
                json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.is_file()
                else {}
            )
            envelope = build_owner_context_envelope(
                project=action.project,
                run_id=self.run_id,
                design_digest=str(
                    manifest.get("design_digest")
                    or owner_view["authority"]["execution_contract_sha256"]
                ),
                owner=action.owner,
                reason="implementation_or_repair",
                instruction=action.instruction,
                contract=contract,
                project_dir=mirror,
                implementation_addendum=addendum,
                failure_logs=selected_logs,
                secret_values=secrets,
            )
            packet_path = context / "model-context.json"
            atomic_write_json(packet_path, envelope)
            context_bytes = packet_path.stat().st_size
            persisted_packet = (
                self.store.execution / "model-contexts" / self.run_id
                / f"{receipt_id}.json"
            )
            atomic_write_json(persisted_packet, envelope)
            prompt = """Execute the single scoped ESP-IDF task in
context/model-context.json. Treat that digest-bound packet as the complete
authority for this transaction. Work only inside project/ and only within its
modification_allowlist. Do not read conversation/session history or unrelated
owners. Never create Arduino code, credentials, evidence, receipts,
design-package, managed_components, or build outputs. Materialize source before
replying; prose alone is not success. The outer Harness performs build and
verification."""
            # This process is already isolated to a disposable mirror and the
            # outer Harness whitelists what can be imported.  The nested Codex
            # client's interactive approval policy otherwise rejects its own
            # patch operation despite workspace-write, causing a false
            # implementation success with no source artifacts.
            # Keep large repair instructions off Windows' bounded command
            # line.  Codex reads ``-`` as the complete stdin prompt.
            command = codex_command() + ["exec", "--json", "--ephemeral", "--ignore-user-config", "--dangerously-bypass-approvals-and-sandbox", "-C", str(work), "-"]
            codex_temp = work_root / "codex-tmp"
            with isolated_codex_profile(codex_temp) as profile:
                child_env = profile.environment.copy(); child_env.pop("PYTHONPATH", None)
                for auth_attempt in range(2):
                    process = subprocess.Popen(command, cwd=work, env=child_env, text=True, encoding="utf-8", errors="replace", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=codex_creationflags())
                    try:
                        output, _ = process.communicate(input=prompt, timeout=self.timeout)
                        returncode = process.returncode
                    except subprocess.TimeoutExpired:
                        # Codex on Windows launches cmd -> node descendants.  Terminate
                        # the complete owned process tree so a timed-out agent cannot
                        # strand the LangGraph checkpoint indefinitely.
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                        output, _ = process.communicate()
                        returncode = -1
                    if returncode == 0 or auth_attempt == 1 or not is_authentication_failure(output or ""):
                        break
                    # Reload the auth.json Cockpit just switched before retrying.
                    time.sleep(2)
                    profile.refresh_auth()
            log_path.write_text(redact_values(output or "", secrets), encoding="utf-8")
            model_usage = parse_codex_jsonl_usage(output or "")
            if returncode != 0:
                failure = Failure(category=FailureCategory.TOOL, summary=(f"implementation agent timed out after {self.timeout} seconds" if returncode == -1 else f"implementation agent exited {returncode}"))
            else:
                for source in mirror.rglob("*"):
                    if not source.is_file(): continue
                    relative = source.relative_to(mirror)
                    if any(part in self.EXCLUDED for part in relative.parts): continue
                    if len(relative.parts) == 1 and relative.name not in self.ALLOWED_ROOT_FILES and not relative.name.startswith("sdkconfig."):
                        continue
                    if len(relative.parts) > 1 and relative.parts[0] not in {"main", "components"}:
                        continue
                    assert_no_secret_values(source.read_text(encoding="utf-8", errors="replace"), secrets, f"generated source {relative}")
                    destination = self.store.project_dir / relative; destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.exists() or destination.read_bytes() != source.read_bytes():
                        shutil.copy2(source, destination); imported.append(relative.as_posix())
            success = returncode == 0
            if success:
                required_component = mirror / "components" / action.owner
                required_project = mirror / "CMakeLists.txt"
                has_component_source = required_component.is_dir() and any(path.is_file() for path in required_component.rglob("*"))
                if not required_project.is_file() or not has_component_source:
                    success = False
                    failure = Failure(category=FailureCategory.TOOL, summary=f"implementation agent returned without required project/component artifacts for {action.owner}")
        except Exception as exc:
            if not log_path.exists(): log_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            success = False; failure = Failure(category=FailureCategory.TOOL, summary=f"implementation agent failed: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        artifacts = [file_ref(log_path, self.store.project_dir, "text/plain")]
        if "persisted_packet" in locals() and persisted_packet.is_file():
            artifacts.append(file_ref(
                persisted_packet, self.store.project_dir, "application/json"
            ))
        receipt = Receipt(receipt_id=receipt_id, run_id=self.run_id, operation="implement_or_repair", started_at=started, finished_at=datetime.now(timezone.utc).isoformat(), success=success, command=["codex", "exec", "--json", "disposable-mirror"], inputs={"owner": action.owner, "context_digest": envelope.get("context_digest") if "envelope" in locals() else None}, outputs={"imported_files": imported, "model_usage": model_usage if "model_usage" in locals() else {"source": "unavailable"}, "context_bytes": context_bytes if "context_bytes" in locals() else None}, artifacts=artifacts, failure=failure)
        self.store.write_receipt(receipt, "agent"); return receipt
