from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..models import Failure, FailureCategory, Receipt
from ..codex_runner import background_creationflags, hidden_powershell_command, hidden_startupinfo
from ..policies import classify_failure
from ..storage import ProjectStore, file_ref


class IdfAdapter:
    def __init__(self, repo_root: Path, store: ProjectStore, run_id: str):
        self.repo_root, self.store, self.run_id = repo_root.resolve(), store, run_id
        self.wrapper = self.repo_root / "tools" / "idf.ps1"

    def run(
        self,
        operation: str,
        args: list[str],
        category: str,
        timeout: int = 900,
        *,
        idempotency_key: str | None = None,
        build_output: Path | None = None,
    ) -> Receipt:
        if not self.wrapper.is_file():
            raise FileNotFoundError("tools/idf.ps1 is required; ESP-IDF paths must not be guessed")
        if idempotency_key and (
            cached := self.store.find_successful_receipt(
                operation, idempotency_key
            )
        ):
            if build_output is None or any(build_output.glob("*.bin")):
                return cached
        started = datetime.now(timezone.utc).isoformat()
        receipt_id = self.store.new_id(operation)
        log_path = self.store.logs / self.run_id / f"{receipt_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = hidden_powershell_command(
            "-ExecutionPolicy", "Bypass", "-File", str(self.wrapper), *args
        )
        child_env = os.environ.copy()
        child_env.pop("PYTHONPATH", None)
        # idf.py starts CMake/Ninja descendants.  A timeout must clean up the
        # whole process tree; otherwise a later resume competes for build files
        # and can incorrectly look like a firmware or hardware fault.
        process = subprocess.Popen(
            command,
            cwd=self.repo_root,
            env=child_env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=background_creationflags(),
            startupinfo=hidden_startupinfo(),
        )
        timed_out = False
        try:
            output, _ = process.communicate(timeout=timeout)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            output, _ = process.communicate()
            returncode = -1
        output = output or ""
        log_path.write_text(output, encoding="utf-8")
        success = returncode == 0
        summary = f"{operation} timed out after {timeout} seconds" if timed_out else f"{operation} exited {returncode}"
        failure_category = classify_failure(output)
        failure = None if success else Failure(
            category=failure_category,
            summary=summary,
            retryable=failure_category != FailureCategory.ENVIRONMENT,
        )
        artifacts = [file_ref(log_path, self.store.project_dir, "text/plain")]
        outputs = {"returncode": returncode, "timed_out": timed_out}
        if success and build_output is not None:
            binaries = sorted(build_output.glob("*.bin"))
            if not binaries:
                success = False
                failure = Failure(
                    category=FailureCategory.BUILD,
                    summary=f"{operation} succeeded but produced no application binary",
                    retryable=False,
                )
            else:
                binary = binaries[0]
                binary_ref = file_ref(
                    binary, self.store.project_dir, "application/octet-stream"
                )
                artifacts.append(binary_ref)
                outputs.update(
                    {
                        "firmware_sha256": binary_ref.sha256,
                        "firmware_binary": binary_ref.path,
                    }
                )
        receipt = Receipt(receipt_id=receipt_id, run_id=self.run_id, operation=operation, started_at=started, finished_at=datetime.now(timezone.utc).isoformat(), success=success, command=command, inputs={"args": args, "idempotency_key": idempotency_key}, outputs=outputs, artifacts=artifacts, failure=failure)
        self.store.write_receipt(receipt, category)
        return receipt

    def version(self) -> Receipt:
        return self.run("idf_version", ["--version"], "preflight", 120)

    def set_target(
        self,
        project_dir: Path,
        target: str,
        *,
        idempotency_key: str | None = None,
    ) -> Receipt:
        return self.run(
            "set_target",
            ["-C", str(project_dir), "set-target", target],
            "build",
            idempotency_key=idempotency_key,
        )

    def build(
        self,
        project_dir: Path,
        build_dir: Path | None = None,
        operation: str = "build",
        *,
        idempotency_key: str | None = None,
    ) -> Receipt:
        args = ["-C", str(project_dir)]
        if build_dir is not None:
            args.extend(["-B", str(build_dir)])
        return self.run(
            operation,
            [*args, "build"],
            "build",
            idempotency_key=idempotency_key,
            build_output=build_dir or (project_dir / "build"),
        )

    def fullclean(self, project_dir: Path) -> Receipt:
        return self.run("fullclean", ["-C", str(project_dir), "fullclean"], "build")

    def flash(
        self,
        project_dir: Path,
        port: str,
        build_dir: Path | None = None,
        operation: str = "flash",
        *,
        idempotency_key: str | None = None,
    ) -> Receipt:
        args = ["-C", str(project_dir)]
        if build_dir is not None:
            args.extend(["-B", str(build_dir)])
        args.extend(["-p", port, "flash"])
        return self.run(
            operation,
            args,
            "flash",
            idempotency_key=idempotency_key,
        )

    def configure_isolated(
        self,
        project_dir: Path,
        build_dir: Path,
        sdkconfig_path: Path,
        defaults: list[Path],
        operation: str = "isolated_reconfigure",
        *,
        idempotency_key: str | None = None,
    ) -> Receipt:
        """Configure an isolated build from explicit configuration inputs."""
        # The normal project sdkconfig remains developer-owned.  ESP-IDF reads
        # it only as a baseline and writes the release configuration elsewhere.
        joined_defaults = ";".join(str(path) for path in defaults)
        return self.run(
            operation,
            ["-C", str(project_dir), "-B", str(build_dir),
             "-D", f"SDKCONFIG={sdkconfig_path}",
             "-D", f"SDKCONFIG_DEFAULTS={joined_defaults}", "reconfigure"],
            "build",
            idempotency_key=idempotency_key,
        )

    def configure_release(
        self,
        project_dir: Path,
        build_dir: Path,
        sdkconfig_path: Path,
        defaults: list[Path],
        *,
        idempotency_key: str | None = None,
    ) -> Receipt:
        """Backward-compatible release-specific isolated configuration."""
        return self.configure_isolated(
            project_dir, build_dir, sdkconfig_path, defaults,
            "release_reconfigure", idempotency_key=idempotency_key,
        )

    @staticmethod
    def firmware_hash(project_dir: Path) -> str:
        # Callers may provide either a project root (normal subsystem build)
        # or an explicit isolated build directory (release build).
        build_dir = project_dir / "build" if (project_dir / "build").is_dir() else project_dir
        binaries = sorted(build_dir.glob("*.bin"))
        if not binaries:
            raise FileNotFoundError(f"no application binary found in {build_dir}")
        return hashlib.sha256(binaries[0].read_bytes()).hexdigest()
