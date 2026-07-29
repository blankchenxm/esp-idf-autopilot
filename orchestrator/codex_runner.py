from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


def is_authentication_failure(output: str) -> bool:
    """Identify a Codex process that must be relaunched after account rotation.

    Cockpit switches the official ``auth.json`` atomically, but an already
    running Codex process keeps the credentials it loaded at startup.  A new
    process must be created to pick up the selected account.
    """
    normalized = output.casefold()
    return any(marker in normalized for marker in (
        "not logged in",
        "missing bearer or basic authentication",
        "401 unauthorized",
        "authentication required",
    ))


def codex_command() -> list[str]:
    """Return an executable Codex command on Windows and POSIX hosts."""
    if os.name == "nt":
        wrapper = shutil.which("codex.cmd")
        # The stable npm launcher is a batch file that explicitly gives its
        # console the title of COMSPEC.  Even with CREATE_NO_WINDOW, Windows
        # can flash that console.  Invoke the same installed codex.js through
        # node directly, preserving its version and CODEX_HOME-based Cockpit
        # account selection without the visible cmd.exe wrapper.
        if wrapper:
            wrapper_path = Path(wrapper).resolve()
            script = wrapper_path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            node = shutil.which("node.exe") or shutil.which("node")
            if node and script.is_file():
                return [str(Path(node).resolve()), str(script)]
        command = wrapper or shutil.which("codex.exe")
    else:
        command = shutil.which("codex")
    if not command:
        raise FileNotFoundError("Codex CLI is not installed or is absent from PATH")
    return [str(Path(command).resolve())]


def background_creationflags() -> int:
    """Start harness-owned Windows children without creating a console window."""
    if os.name != "nt":
        return 0
    return (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )


def hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    """Hide non-interactive Windows child windows, including shell descendants."""
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return startupinfo


def codex_creationflags() -> int:
    """Backward-compatible name for nested Codex CLI launchers."""
    return background_creationflags()


def terminate_process_tree(process: subprocess.Popen[str], wait_seconds: int = 5) -> None:
    """Bound cleanup of a Codex CLI process and all descendants."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        process.terminate()
    try:
        process.wait(timeout=wait_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=wait_seconds)
        except subprocess.TimeoutExpired:
            pass


def codex_environment(
    base: dict[str, str] | None = None,
    temporary_dir: Path | None = None,
) -> dict[str, str]:
    """Return child environment bound to the invoking user's Codex login.

    Some managed execution environments redirect an unset CODEX_HOME to an
    isolated, credential-free home for nested processes.  That makes the
    Harness provider appear logged out even when the invoking user has a
    valid Codex CLI login.  Preserve an explicit CODEX_HOME so account/profile
    selection remains under the user's control; otherwise bind it to the
    standard per-user Codex state directory.

    Managed Windows sandboxes can allow reading the login state while denying
    writes beneath that profile.  Callers that own a project-scoped runtime
    directory can provide a writable ``temporary_dir`` so the nested CLI
    retains its selected login state but places ephemeral files outside the
    protected profile directory.  Windows consumers do not consistently honor
    ``TMPDIR``; set all three conventional temporary-directory variables.
    """
    environment = dict(os.environ if base is None else base)
    if not environment.get("CODEX_HOME"):
        environment["CODEX_HOME"] = str(Path.home() / ".codex")
    if temporary_dir is not None:
        resolved_temp = temporary_dir.resolve()
        if not resolved_temp.is_dir():
            raise ValueError(f"Codex temporary directory must exist: {resolved_temp}")
        temporary_path = str(resolved_temp)
        environment["TMPDIR"] = temporary_path
        environment["TMP"] = temporary_path
        environment["TEMP"] = temporary_path
    return environment


@dataclass
class IsolatedCodexProfile:
    """A writable, project-local Codex home backed by the active login."""

    environment: dict[str, str]
    source_home: Path
    home: Path

    def refresh_auth(self) -> None:
        """Atomically refresh the disposable login after Cockpit rotates it."""
        source = self.source_home / "auth.json"
        destination = self.home / "auth.json"
        if not source.is_file():
            return
        temporary = self.home / ".auth.json.refresh"
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)


@contextmanager
def isolated_codex_profile(
    parent: Path,
    base: dict[str, str] | None = None,
) -> Iterator[IsolatedCodexProfile]:
    """Create a disposable Codex home below a declared Harness runtime path.

    Codex CLI creates sandbox helpers below ``CODEX_HOME`` before consulting
    TMP/TEMP.  Managed sessions may expose the active login home as read-only,
    so a nested CLI needs a writable home.  Keep that home under the current
    project's ignored runtime namespace, copy only the active login, and
    remove the complete profile on every normal or exceptional exit.

    User ``config.toml`` is deliberately not inherited.  A nested provider
    must not initialize the outer Codex session's plugins, marketplaces,
    hooks, or MCP OAuth state.
    """
    environment = dict(os.environ if base is None else base)
    source_home = Path(environment.get("CODEX_HOME") or (Path.home() / ".codex")).resolve()
    resolved_parent = parent.resolve()
    resolved_parent.mkdir(parents=True, exist_ok=True)
    profile_home = Path(tempfile.mkdtemp(prefix="profile-", dir=resolved_parent))
    try:
        (profile_home / "tmp").mkdir()
        environment["CODEX_HOME"] = str(profile_home)
        temporary_path = str((profile_home / "tmp").resolve())
        environment["TMPDIR"] = temporary_path
        environment["TMP"] = temporary_path
        environment["TEMP"] = temporary_path
        profile = IsolatedCodexProfile(environment, source_home, profile_home)
        profile.refresh_auth()
        yield profile
    finally:
        shutil.rmtree(profile_home, ignore_errors=True)
