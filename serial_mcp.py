"""
Standalone serial-monitor MCP server for ESP32 firmware development on Windows.

Why this exists:
- Everything ESP-IDF can do from one line (build / flash / set-target / clean /
  create-project) is left to plain Bash in the agent's already-activated shell.
- The ONE thing a shell cannot drive cleanly is an interactive serial session:
  idf_monitor.py needs a real TTY that an agent cannot operate on Windows.
- So this server keeps only the serial monitor, implemented directly on pyserial.
  It does NOT import idf.py and needs NO ESP-IDF environment — only `mcp` and
  `pyserial`. That makes it trivially portable: any machine that can `pip install
  -r requirements.txt` can run it, no IDF_PATH, no hardcoded toolchain paths.

Reset model (standard ESP32 USB-UART auto-reset circuit):
    RTS -> EN   (active-low reset)
    DTR -> GPIO0 (boot mode select)
We only toggle RTS to reset into the normal app (never into the bootloader).

Windows only. Linux/Mac would use idf.py monitor directly via Bash instead.
"""

from __future__ import annotations

import re
import threading
import time

from mcp.server.fastmcp import FastMCP

try:
    import serial
    import serial.tools.list_ports
    _SERIAL_SUPPORTED = True
except ImportError:  # pragma: no cover - surfaced as a tool error at call time
    _SERIAL_SUPPORTED = False

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class WindowsMonitorSession:
    """
    Background serial monitor backed by pyserial.

    idf_monitor.py requires a real TTY (unavailable on Windows without a PTY),
    so we read the port directly. Supports hardware reset via DTR/RTS, a
    line-buffered background reader thread, and a send/read/stop interface.
    """

    def __init__(self) -> None:
        self._ser: serial.Serial | None = None
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def is_running(self) -> bool:
        return (
            self._ser is not None
            and self._ser.is_open
            and self._thread is not None
            and self._thread.is_alive()
        )

    @property
    def has_session(self) -> bool:
        if self._ser is not None or self._thread is not None:
            return True
        with self._lock:
            return len(self._lines) > 0

    def _reader(self) -> None:
        buf = b""
        while not self._stop_event.is_set():
            try:
                if self._ser is None or not self._ser.is_open:
                    break
                waiting = self._ser.in_waiting
                if waiting > 0:
                    buf += self._ser.read(waiting)
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        decoded = line.decode("utf-8", errors="replace") + "\n"
                        with self._lock:
                            self._lines.append(decoded)
                else:
                    time.sleep(0.01)
            except (OSError, serial.SerialException):
                break
        if buf:
            with self._lock:
                self._lines.append(buf.decode("utf-8", errors="replace"))

    def _reset_device(self) -> None:
        """Pulse EN low->high via RTS to boot into the normal app (not bootloader)."""
        if self._ser is None:
            return
        self._ser.dtr = False
        self._ser.rts = True   # EN low -> reset asserted
        time.sleep(0.1)
        self._ser.rts = False  # EN high -> boot starts
        time.sleep(0.05)

    def start(self, port: str, baudrate: int = 115200, reset: bool = True) -> None:
        if self.has_session:
            raise RuntimeError("Monitor session is already active. Stop it first.")
        self._lines = []
        self._stop_event.clear()
        self._ser = serial.Serial(port, baudrate, timeout=0.1)
        if reset:
            self._reset_device()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def send(self, data: str) -> None:
        if not self.is_running:
            raise RuntimeError("No monitor session is running.")
        payload = data if data.endswith("\n") else data + "\n"
        self._ser.write(payload.encode("utf-8"))

    def read(self, max_lines: int = 200) -> tuple[list[str], int]:
        with self._lock:
            chunk = self._lines[:max_lines]
            self._lines = self._lines[max_lines:]
            remaining = len(self._lines)
        return chunk, remaining

    def stop(self) -> list[str]:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        with self._lock:
            remaining = list(self._lines)
            self._lines.clear()
        return remaining


mcp = FastMCP("esp-serial")

# One shared session for the lifetime of the server.
_session = WindowsMonitorSession()


def _require_serial(tool_name: str) -> str | None:
    if not _SERIAL_SUPPORTED:
        return f"{tool_name} requires pyserial. Install it with: pip install pyserial"
    return None


def _default_port() -> str | None:
    if not _SERIAL_SUPPORTED:
        return None
    ports = [p.device for p in serial.tools.list_ports.comports()]
    return ports[0] if ports else None


@mcp.tool()
def monitor_boot(
    port: str | None = None,
    baudrate: int = 115200,
    capture_duration: int = 15,
    wait_for: str | None = None,
) -> str:
    """Reset the device and capture the boot log (one-shot).

    Opens the serial port, triggers a hardware reset via DTR/RTS, captures
    output for capture_duration seconds (or until wait_for appears), then closes
    the port. Returns the boot log: bootloader, component init, startup logs.

    Args:
        port:             Serial port (e.g. COM4). Auto-detected if omitted.
        baudrate:         Baud rate (default 115200).
        capture_duration: Seconds to capture (default 15, max 120).
        wait_for:         Stop early when this string appears
                          (e.g. 'app_main', 'Guru Meditation Error').
    """
    err = _require_serial("monitor_boot")
    if err:
        return err
    if _session.has_session:
        return "A monitor session is already active. Call monitor_stop first."

    resolved_port = port or _default_port()
    if not resolved_port:
        return "No serial port found. Plug in the device or specify port explicitly."

    capture_duration = max(1, min(capture_duration, 120))
    session = WindowsMonitorSession()
    collected: list[str] = []
    try:
        session.start(resolved_port, baudrate=baudrate, reset=True)
        deadline = time.monotonic() + capture_duration
        while time.monotonic() < deadline:
            lines, _ = session.read()
            collected.extend(lines)
            if wait_for and wait_for in "".join(collected):
                break
            time.sleep(0.05)
    except Exception as e:
        return f"Error capturing boot log: {e}"
    finally:
        session.stop()

    output = _strip_ansi("".join(collected))
    if not output.strip():
        output = "(no output captured — check port and baud rate)"
    header = f"Boot log from {resolved_port}"
    if wait_for and wait_for in output:
        header += f' (stopped on "{wait_for}")'
    else:
        header += f" ({capture_duration}s)"
    return f"{header}:\n{output}"


@mcp.tool()
def monitor_start(
    port: str | None = None,
    baudrate: int = 115200,
    reset: bool = True,
) -> str:
    """Start a background serial monitor session.

    Opens the port and starts a background reader thread, optionally resetting
    the device on connect. Use monitor_read / monitor_send / monitor_stop next.

    Args:
        port:     Serial port (e.g. COM4). Auto-detected if omitted.
        baudrate: Baud rate (default 115200).
        reset:    Trigger hardware reset on connect (default True).
    """
    err = _require_serial("monitor_start")
    if err:
        return err
    if _session.has_session:
        return "A monitor session is already active. Call monitor_stop first."

    resolved_port = port or _default_port()
    if not resolved_port:
        return "No serial port found. Plug in the device or specify port explicitly."

    try:
        _session.start(resolved_port, baudrate=baudrate, reset=reset)
        time.sleep(2)  # let the board boot
        if not _session.is_running:
            _session.stop()
            return f"Monitor failed to start on {resolved_port}."
        return f"Monitor started on {resolved_port}. Use monitor_read to retrieve output."
    except Exception as e:
        return f"Error starting monitor: {e}"


@mcp.tool()
def monitor_read(
    max_lines: int = 200,
    timeout: float = 0,
    wait_for: str | None = None,
) -> str:
    """Read buffered output from the monitor session.

    Returns up to max_lines of new output since the last read. Keep reads
    bounded: use wait_for + max_lines instead of dumping the whole buffer.

    Args:
        max_lines: Maximum lines to return (default 200, max 1000).
        timeout:   Seconds to wait for output (default 0 = instant, max 30).
        wait_for:  Return early when this string appears (e.g. 'OK', 'FAIL').
    """
    if not _session.has_session:
        return "No monitor session is active. Call monitor_start first."

    max_lines = max(1, min(max_lines, 1000))
    timeout = max(0.0, min(float(timeout), 30.0))

    collected: list[str] = []
    deadline = time.monotonic() + timeout if timeout > 0 else 0.0
    while True:
        lines, _ = _session.read(max_lines)
        collected.extend(lines)
        if wait_for and wait_for in "".join(collected):
            break
        if deadline == 0.0 or time.monotonic() >= deadline:
            break
        if not _session.is_running:
            break
        time.sleep(0.05)

    if not collected:
        if not _session.is_running:
            return "(no new output; monitor session is no longer running)"
        if timeout > 0:
            msg = f"(no output after {timeout}s"
            if wait_for:
                msg += f'; "{wait_for}" not found'
            return msg + ")"
        return "(no new output)"

    output = _strip_ansi("".join(collected))
    _, remaining = _session.read(0)
    if remaining > 0:
        output += f"\n[{remaining} more lines in buffer]"
    if not _session.is_running:
        output += "\n[note: monitor session is no longer running]"
    if wait_for:
        output += f'\n[matched: "{wait_for}"]' if wait_for in output else f'\n["{wait_for}" not found in output]'
    return output


@mcp.tool()
def monitor_send(text: str) -> str:
    """Send text to the device through the running monitor session.

    Writes text to the serial port; a newline is appended if absent. Use
    monitor_read to retrieve the device response.

    Args:
        text: Text to send (e.g. 'STATUS', 'START').
    """
    if not _session.is_running:
        return "No monitor session is running. Call monitor_start first."
    try:
        _session.send(text)
        return f"Sent: {text.strip()}"
    except Exception as e:
        return f"Error sending to device: {e}"


@mcp.tool()
def monitor_stop() -> str:
    """Stop the monitor session and clean up resources.

    Works even if the session already ended on its own. Call monitor_read
    before stopping if you still need buffered output.
    """
    if not _session.has_session:
        return "No monitor session is active."
    was_running = _session.is_running
    _session.stop()
    return "Monitor stopped." if was_running else "Monitor had already stopped; session cleaned up."


if __name__ == "__main__":
    mcp.run()
