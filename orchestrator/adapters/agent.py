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
            atomic_write_json(context / "owner-contract.json", owner_contract_view(contract, action.owner))
            if action.implementation_addendum_path is not None:
                shutil.copy2(
                    action.implementation_addendum_path,
                    context / "implementation-addendum.json",
                )
            (context / "requirements.md").write_text(redact_text(raw_requirements), encoding="utf-8")
            shutil.copy2(action.connection_path, context / "connections.md")
            for index, source in enumerate(select_owner_failure_logs(self.store, self.run_id, action.owner)):
                if source.is_file(): shutil.copy2(source, context / f"failure-{index}.log")
            prompt = f"""Implement or repair ESP-IDF subsystem {action.owner!r} for project {action.project!r}.
Work only inside project/. Read context/owner-contract.json, context/implementation-addendum.json
when present, requirements.md, connections.md,
and failure logs. ESP-IDF only. Create/modify project-owned CMake, sdkconfig defaults, main orchestration,
components/{action.owner}/ semantic API and retained selftest as required. Never create Arduino code,
credentials, evidence, receipts, approval, design-package, managed_components, or build outputs.
For Crumb, use project/private/crumb_credentials.h; never copy, print, summarize, or hardcode its values.
Keep main/ orchestration-only. Follow exact contract expected values and local ESP-IDF APIs; if a fact
is unavailable, report failure instead of guessing registers/pins/timing. When the implementation
addendum selects a Registry component, adopt that exact namespace/version behind the owner semantic
wrapper; custom replacement is not permitted unless a later typed repair proves an objective
capability gap. Use addendum facts only for operations not covered by that component.
{action.instruction}
This is a disposable writable mirror. Do not report a permission restriction unless an actual attempted
write fails. Materialize the requested project/component source before replying; prose alone is never a
successful implementation result.
When done, build is performed by the outer harness; summarize changed files and remaining blockers.
"""
            # This process is already isolated to a disposable mirror and the
            # outer Harness whitelists what can be imported.  The nested Codex
            # client's interactive approval policy otherwise rejects its own
            # patch operation despite workspace-write, causing a false
            # implementation success with no source artifacts.
            # Keep large repair instructions off Windows' bounded command
            # line.  Codex reads ``-`` as the complete stdin prompt.
            command = codex_command() + ["exec", "--ephemeral", "--ignore-user-config", "--dangerously-bypass-approvals-and-sandbox", "-C", str(work), "-"]
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
        receipt = Receipt(receipt_id=receipt_id, run_id=self.run_id, operation="implement_or_repair", started_at=started, finished_at=datetime.now(timezone.utc).isoformat(), success=success, command=["codex", "exec", "disposable-mirror"], inputs={"owner": action.owner, "instruction": action.instruction}, outputs={"imported_files": imported}, artifacts=[file_ref(log_path, self.store.project_dir, "text/plain")], failure=failure)
        self.store.write_receipt(receipt, "agent"); return receipt
