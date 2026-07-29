"""
Hardware preflight gate — run this BEFORE asking the agent to start a project.

Purpose: prove the physical chain (port → flash → serial) works while you are
present, so that any later failure is unambiguously a firmware bug, not hardware.
Once this passes, you can walk away and let the agent develop autonomously.

What it does (3 checks, using ESP-IDF's own hello_world as a known-good app):
  1. Port      — a serial port is present (auto-detect or --port).
  2. Flash     — build + flash hello_world to the board succeeds.
  3. Serial    — reset the board and read back "Hello world!" over serial.

Usage (from an ESP-IDF-activated shell):
    python scripts/preflight.py                 # auto-detect port, chip=esp32
    python scripts/preflight.py --port COM4 --chip esp32
    python scripts/preflight.py --keep          # keep the temp build dir

Windows only for the serial read step (reuses serial_mcp's pyserial session).
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Self-contained: only needs pyserial (which ESP-IDF ships). No dependency on the
# MCP SDK, so the hardware gate runs even where `mcp` isn't installed.
try:
    import serial
    import serial.tools.list_ports
    _SERIAL_OK = True
except ImportError:
    _SERIAL_OK = False

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PREFLIGHT_DIR = REPO_ROOT / ".preflight"
LOCK_DIR = PREFLIGHT_DIR / ".hardware-preflight.lock"


def _pid_is_alive(pid: int) -> bool:
    """Windows-safe liveness query; ``os.kill(pid, 0)`` is invalid on Windows."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _acquire_preflight_lock(timeout_seconds: float = 420.0) -> None:
    """Serialize the disposable hello-world workspace and board side effects.

    A runner can be resumed while a previous client process is still unwinding.
    The preflight workspace is intentionally shared, so allowing two instances to
    remove/copy/build it concurrently corrupts a valid preflight into a false
    toolchain failure.  Directory creation is atomic on the supported filesystems
    and needs no third-party dependency.
    """
    PREFLIGHT_DIR.mkdir(exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            LOCK_DIR.mkdir()
            (LOCK_DIR / "owner.txt").write_text(
                f"pid={os.getpid()}\nstarted={time.time()}\n", encoding="utf-8"
            )
            return
        except FileExistsError:
            owner = LOCK_DIR / "owner.txt"
            owner_text = owner.read_text(encoding="utf-8", errors="replace") if owner.is_file() else ""
            match = re.search(r"^pid=(\d+)$", owner_text, re.MULTILINE)
            if match and not _pid_is_alive(int(match.group(1))):
                # A cancelled runner cannot execute its finally block.  Its dead
                # PID is authoritative evidence that this lock is stale; remove
                # only the lock, never a live workspace.
                shutil.rmtree(LOCK_DIR, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                detail = owner_text.strip() or "unknown owner"
                _fail(f"hardware preflight lock remained busy for {int(timeout_seconds)} seconds ({detail}).")
            time.sleep(0.25)


def _release_preflight_lock() -> None:
    shutil.rmtree(LOCK_DIR, ignore_errors=True)


def _capture_serial(port: str, baud: int, seconds: float, needle: str) -> str:
    """Reset the board (RTS=EN, DTR=GPIO0 high) and read serial until needle or timeout."""
    ser = serial.Serial(port, baud, timeout=0.1)
    ser.dtr = False
    ser.rts = True
    time.sleep(0.1)
    ser.rts = False
    time.sleep(0.05)
    buf = b""
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            n = ser.in_waiting
            if n:
                buf += ser.read(n)
                if needle.encode() in buf:
                    break
            else:
                time.sleep(0.02)
    finally:
        ser.close()
    return _ANSI_RE.sub("", buf.decode("utf-8", errors="replace"))


def _fail(msg: str) -> None:
    print(f"\n[PREFLIGHT] FAIL: {msg}")
    sys.exit(1)


def _idf(*args: str, cwd: Path) -> int:
    """Run IDF only through the repository's self-activating wrapper."""
    wrapper = REPO_ROOT / "tools" / "idf.ps1"
    cmd = [
        "powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(wrapper),
        "-C",
        str(cwd),
        *args,
    ]
    print(f"[PREFLIGHT] $ tools/idf.ps1 -C {cwd} {' '.join(args)}")
    return subprocess.run(cmd, cwd=str(REPO_ROOT)).returncode


def _resolve_port(requested: str | None) -> str:
    if requested:
        return requested
    if not _SERIAL_OK:
        _fail("pyserial not available; install requirements.txt or pass --port.")
    ports = [p.device for p in serial.tools.list_ports.comports()]
    if not ports:
        _fail("no serial port detected. Plug in the board (USB) or pass --port COMx.")
    if len(ports) > 1:
        print(f"[PREFLIGHT] multiple ports {ports}; using {ports[0]} (override with --port).")
    return ports[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="ESP32 hardware preflight gate.")
    ap.add_argument("--port", default=None, help="serial port, e.g. COM4 (auto-detect if omitted)")
    ap.add_argument("--chip", default="esp32", help="target chip (default esp32)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--keep", action="store_true", help="keep the temp build dir")
    args = ap.parse_args()

    # --- Step 0: environment ------------------------------------------------
    # A set IDF_PATH is NOT proof of a working shell — it can point at a broken or
    # wrong-version install. Require idf.py to actually run (activate first).
    idf_path = os.environ.get("IDF_PATH")
    if not idf_path:
        _fail("IDF_PATH is not set. Activate ESP-IDF (export script / EIM profile) first.")
    if _idf("--version", cwd=REPO_ROOT) != 0:
        _fail("idf.py does not run — this shell is not properly activated.\n"
              "        Activate ESP-IDF first (e.g. `. .\\activate.local.ps1`), then retry.")
    hello_src = Path(idf_path) / "examples" / "get-started" / "hello_world"
    if not hello_src.is_dir():
        _fail(f"hello_world example not found at {hello_src}")

    # --- Step 1: port -------------------------------------------------------
    port = _resolve_port(args.port)
    print(f"[PREFLIGHT] 1/3 port OK: {port}")

    # --- Step 2: build + flash a known-good app -----------------------------
    _acquire_preflight_lock()
    try:
        proj = PREFLIGHT_DIR / "hello_world"
        if proj.exists():
            shutil.rmtree(proj, ignore_errors=True)
        shutil.copytree(hello_src, proj)

        if _idf("set-target", args.chip, cwd=proj) != 0:
            _fail(f"set-target {args.chip} failed.")
        if _idf("build", cwd=proj) != 0:
            _fail("build failed – toolchain/environment problem, not your board.")
        if _idf("-p", port, "flash", cwd=proj) != 0:
            _fail(f"flash failed on {port} – check cable/port/driver and that the board is in boot mode.")
        print(f"[PREFLIGHT] 2/3 flash OK on {port}")

        # --- Step 3: serial read-back ---------------------------------------
        if not _SERIAL_OK:
            _fail("pyserial missing; cannot verify serial read-back.")
        print(f"[PREFLIGHT] 3/3 reading serial on {port} (reset + capture, wait for 'Hello world!')...")
        out = _capture_serial(port, args.baud, seconds=12, needle="Hello world!")
    finally:
        # Never delete the lock's parent while still holding the lock.
        _release_preflight_lock()

    if not args.keep:
        shutil.rmtree(proj, ignore_errors=True)

    if "Hello world!" in out:
        print("[PREFLIGHT] 3/3 serial OK: captured 'Hello world!'")
        print("\n[PREFLIGHT] PASS — port, flash, and serial all verified.")
        print("            Hardware is good. You can start the project; later failures are firmware.")
        return 0

    print("\n[PREFLIGHT] captured serial (no 'Hello world!'):")
    print(out[-800:] if out.strip() else "(nothing captured)")
    _fail("flashed OK but no expected serial output — check baud, wiring, or the USB-UART reset lines.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
