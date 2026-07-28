# SPDX-FileCopyrightText: 2024-2026 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from click.core import Context

from idf_py_actions.errors import FatalError
from idf_py_actions.tools import PropertyDict
from idf_py_actions.tools import get_target
from idf_py_actions.tools import idf_version

try:
    from idf_component_tools.build_system_tools import CMAKE_PROJECT_LINE
except ImportError:
    CMAKE_PROJECT_LINE = [
        r'include($ENV{IDF_PATH}/tools/cmake/project.cmake)',
        r'include($ENV{IDF_PATH}/tools/cmakev2/idf.cmake)',
    ]

try:
    from mcp.server.fastmcp import FastMCP, Context
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    Context = None  # type: ignore

# pyserial is required for Windows monitor support.
try:
    import serial
    import serial.tools.list_ports
    _SERIAL_SUPPORTED = True
except ImportError:
    _SERIAL_SUPPORTED = False

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


async def _run_idf_streaming(cmd: list[str], cwd: str, label: str,
                             ctx: Any = None) -> tuple[int, str]:
    """
    Run an idf.py command, streaming output line-by-line via a background thread.

    Uses asyncio.to_thread() + subprocess.Popen so that the subprocess pipe is
    owned by a real OS thread rather than the asyncio event loop.  This makes
    the build immune to MCP request cancellation: even if the awaiting coroutine
    is cancelled the thread keeps running, the pipe stays open, and idf.py can
    finish writing without getting 'I/O operation on closed file'.
    """
    import asyncio

    print(f'[{label}] cwd : {cwd}', file=sys.stderr, flush=True)
    print(f'[{label}] cmd : {" ".join(cmd)}', file=sys.stderr, flush=True)
    if not os.path.isdir(cwd):
        print(f'[{label}] ERROR: cwd does not exist!', file=sys.stderr, flush=True)
        return 1, f'cwd does not exist: {cwd}'

    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    env['NINJA_STATUS'] = '[%f/%t] '
    for _msys_var in ('MSYSTEM', 'MSYS', 'MINGW_PREFIX', 'MINGW_CHOST',
                      'MINGW_PACKAGE_PREFIX', 'MSYS2_PATH_TYPE'):
        env.pop(_msys_var, None)

    lines: list[str] = []

    def _sync_popen() -> int:
        p = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            bufsize=0,
        )
        assert p.stdout is not None
        while True:
            raw = p.stdout.readline()
            if not raw:
                break
            line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
            lines.append(line)
            print(line, file=sys.stderr, flush=True)
        p.wait()
        print(f'[{label}] exit code: {p.returncode}', file=sys.stderr, flush=True)
        return p.returncode  # type: ignore[return-value]

    # asyncio.shield ensures the thread keeps running even if the MCP client
    # cancels this request mid-build, preventing the subprocess pipe from
    # closing prematurely and causing idf.py to crash.
    returncode = await asyncio.shield(asyncio.to_thread(_sync_popen))
    return returncode, '\n'.join(lines)


def _idf_cmd(*idf_args: str) -> list[str]:
    """Build an idf.py command list. Raises FatalError if IDF_PATH is not set."""
    idf_path = os.environ.get('IDF_PATH')
    if not idf_path:
        raise FatalError(
            'IDF_PATH environment variable is not set. '
            'Source the ESP-IDF export script before starting the MCP server.'
        )
    # -u: force CPython unbuffered mode so every print() in idf.py is
    # flushed immediately to our pipe, giving real-time progress on Windows.
    return [sys.executable, '-u', os.path.join(idf_path, 'tools', 'idf.py'), *idf_args]


# ============================================================
# Windows Serial Monitor Session
# Uses pyserial to read/write the serial port directly,
# bypassing the PTY requirement of idf_monitor.py.
# ============================================================

class WindowsMonitorSession:
    """
    Manages a background serial monitor session on Windows using pyserial.

    Since idf_monitor.py requires a real TTY (unavailable on Windows without
    PTY), this class reads the serial port directly via pyserial. It supports:
      - Hardware reset via DTR/RTS control lines
      - Background reader thread with line buffering
      - ANSI escape stripping
      - Send / read / stop interface matching the Unix MonitorSession API
    """

    def __init__(self) -> None:
        self._ser: serial.Serial | None = None
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def is_running(self) -> bool:
        """True if the serial port is open and reader thread is alive."""
        return (
            self._ser is not None
            and self._ser.is_open
            and self._thread is not None
            and self._thread.is_alive()
        )

    @property
    def has_session(self) -> bool:
        """True if session resources exist or buffered data remains."""
        if self._ser is not None or self._thread is not None:
            return True
        with self._lock:
            return len(self._lines) > 0

    def _reader(self) -> None:
        """Background thread: read lines from serial port into buffer."""
        buf = b''
        while not self._stop_event.is_set():
            try:
                if self._ser is None or not self._ser.is_open:
                    break
                waiting = self._ser.in_waiting
                if waiting > 0:
                    chunk = self._ser.read(waiting)
                    buf += chunk
                    # Split on newlines, keep partial last line in buf
                    while b'\n' in buf:
                        line, buf = buf.split(b'\n', 1)
                        decoded = line.decode('utf-8', errors='replace') + '\n'
                        with self._lock:
                            self._lines.append(decoded)
                else:
                    time.sleep(0.01)
            except (OSError, serial.SerialException):
                break
        # Flush remaining partial line
        if buf:
            with self._lock:
                self._lines.append(buf.decode('utf-8', errors='replace'))

    def _reset_device(self) -> None:
        """
        Trigger ESP32 hardware reset via DTR/RTS control lines.

        ESP32 reset circuit (standard USB-UART boards):
          RTS -> EN pin  (active low reset)
          DTR -> GPIO0   (boot mode select)

        Sequence to reset into normal app mode (not bootloader):
          1. Assert RTS (EN low)  -> hold in reset
          2. Release RTS (EN high) -> start booting
        """
        if self._ser is None:
            return
        self._ser.dtr = False
        self._ser.rts = True    # EN pulled low -> reset asserted
        time.sleep(0.1)
        self._ser.rts = False   # EN released -> boot starts
        time.sleep(0.05)

    def start(self, port: str, baudrate: int = 115200,
              reset: bool = True) -> None:
        """Open serial port and start background reader thread."""
        if self.has_session:
            raise RuntimeError('Monitor session is already active. Stop it first.')

        self._lines = []
        self._stop_event.clear()
        self._ser = serial.Serial(port, baudrate, timeout=0.1)

        if reset:
            self._reset_device()

        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def send(self, data: str) -> None:
        """Send text to the device over the serial port."""
        if not self.is_running:
            raise RuntimeError('No monitor session is running.')
        payload = data if data.endswith('\n') else data + '\n'
        self._ser.write(payload.encode('utf-8'))

    def read(self, max_lines: int = 200) -> tuple[list[str], int]:
        """
        Drain up to max_lines from the buffer.
        Returns (lines, remaining_count).
        """
        with self._lock:
            chunk = self._lines[:max_lines]
            self._lines = self._lines[max_lines:]
            remaining = len(self._lines)
        return chunk, remaining

    def stop(self) -> list[str]:
        """Stop the session and return all remaining buffered output."""
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


# ============================================================
# Project directory validation
# ============================================================

def is_valid_project_dir(directory: str) -> bool:
    """
    Determine if the given directory is a valid ESP-IDF project directory.
    - Must be a directory.
    - Must contain a CMakeLists.txt.
    - CMakeLists.txt must include CMAKE_PROJECT_LINE.
    """
    root = Path(directory)
    if not root.is_dir():
        return False
    cmakelists_path = root / 'CMakeLists.txt'
    if not cmakelists_path.is_file():
        return False
    try:
        with open(str(cmakelists_path), encoding='utf-8') as f:
            for line in f:
                if any(proj_line in line for proj_line in CMAKE_PROJECT_LINE):
                    return True
    except Exception:
        return False
    return False


# ============================================================
# MCP Server action extension
# ============================================================

def action_extensions(base_actions: dict, project_path: str) -> dict:
    """ESP-IDF MCP Server Extension"""

    def start_mcp_server(action_name: str, ctx: Context, args: PropertyDict, **kwargs: Any) -> None:
        """Start MCP server for ESP-IDF project integration"""

        if not MCP_AVAILABLE:
            raise FatalError(
                'MCP dependencies not available. '
                'Install ESP-IDF using the EIM installer and select the "mcp" feature to be included. '
                'For more information, refer to the official Espressif EIM Installer documentation '
                'or use "idf.py docs" and search for EIM configuration instructions.'
            )

        # Verify that mcp-server was executed from a valid ESP-IDF project directory.
        if not is_valid_project_dir(project_path):
            current_project = None
            for candidate in [os.getcwd(), os.environ.get('IDF_MCP_WORKSPACE_FOLDER', '')]:
                if is_valid_project_dir(candidate):
                    current_project = candidate
                    break
            if not current_project:
                raise FatalError('Open the MCP server in a valid ESP-IDF project directory.')
            try:
                cmd = _idf_cmd('-C', current_project, 'mcp-server')
                print(
                    f'Starting ESP-IDF MCP Server with command: {" ".join(cmd)} in project path: {current_project}',
                    file=sys.stderr,
                )
                subprocess.run(cmd, cwd=current_project, check=True)
                return
            except Exception as e:
                print(f'ERROR: Failed to start ESP-IDF MCP Server: {str(e)}', file=sys.stderr)
                raise FatalError(f'Failed to start ESP-IDF MCP Server: {str(e)}') from e

        # Initialize MCP server
        mcp = FastMCP('ESP-IDF')

        # Persistent Windows monitor session (shared across tool calls)
        win_monitor = WindowsMonitorSession()

        # --------------------------------------------------------
        # Helper
        # --------------------------------------------------------

        def _require_serial(tool_name: str) -> str | None:
            """Return error string if pyserial is unavailable, else None."""
            if not _SERIAL_SUPPORTED:
                return (
                    f'{tool_name} requires pyserial. '
                    'Install it with: pip install pyserial --break-system-packages'
                )
            return None

        def _default_port() -> str | None:
            """Return the first available COM port, or None."""
            if not _SERIAL_SUPPORTED:
                return None
            ports = [p.device for p in serial.tools.list_ports.comports()]
            return ports[0] if ports else None

        # --------------------------------------------------------
        # TOOLS — Build / Flash / Clean / Set-target
        # --------------------------------------------------------

        @mcp.tool()
        async def build_project(ctx: Context) -> str:
            """Start a build or poll its status.

            First call: launches idf.py build in the background and returns immediately.
            Subsequent calls: return current progress or final result.
            Call repeatedly (e.g. every 60 s) until the build succeeds or fails.
            """
            import json as _json
            import time as _time

            state_path = os.path.join(project_path, 'mcp-build-state.json')
            log_path   = os.path.join(project_path, 'mcp-build.log')

            def _pid_running(pid: int) -> bool:
                try:
                    os.kill(pid, 0)
                    return True
                except OSError:
                    return False

            def _read_tail(path: str, n: int = 30) -> str:
                try:
                    with open(path, 'r', encoding='utf-8', errors='replace') as f:
                        lines = f.readlines()
                    return ''.join(lines[-n:]).strip()
                except OSError:
                    return '(log unreadable)'

            # ── Poll existing build ──────────────────────────────────────────
            if os.path.exists(state_path):
                try:
                    with open(state_path, 'r') as f:
                        state = _json.load(f)

                    pid       = state['pid']
                    exit_code = state.get('exit_code')
                    elapsed   = _time.time() - state['start_time']

                    if exit_code is None:
                        if _pid_running(pid):
                            tail = _read_tail(log_path)
                            return (
                                f'Build in progress — {elapsed/60:.1f} min elapsed (pid={pid}).\n\n'
                                f'Recent output:\n{tail}'
                            )
                        # Process gone but thread hasn't written exit_code yet;
                        # infer result from log content.
                        tail = _read_tail(log_path, 80)
                        succeeded = 'Project build complete' in tail or '.bin' in tail
                        exit_code = 0 if succeeded else 1

                    # Build finished — clean up state and report.
                    try:
                        os.remove(state_path)
                    except OSError:
                        pass
                    tail = _read_tail(log_path, 60)
                    if exit_code == 0:
                        print('INFO: Build successful', file=sys.stderr)
                        return f'Build SUCCEEDED in {elapsed/60:.1f} min.\n\n{tail}'
                    else:
                        return f'Build FAILED (exit {exit_code}) after {elapsed/60:.1f} min.\n\n{tail}'

                except Exception as e:
                    # Corrupt state — remove and fall through to fresh build.
                    try:
                        os.remove(state_path)
                    except OSError:
                        pass
                    print(f'[build] bad state file, restarting: {e}', file=sys.stderr)

            # ── Start new build ──────────────────────────────────────────────
            try:
                cmd = _idf_cmd('build')
                print(f'INFO: Building project in path: {project_path}', file=sys.stderr)

                log_f = open(log_path, 'w', encoding='utf-8', errors='replace')
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    cwd=project_path,
                    env={**os.environ, 'PYTHONUNBUFFERED': '1', 'NINJA_STATUS': '[%f/%t] '},
                )
                log_f.close()

                state = {'pid': proc.pid, 'log_path': log_path,
                         'start_time': _time.time(), 'exit_code': None}
                with open(state_path, 'w') as f:
                    _json.dump(state, f)

                # Background thread writes exit_code when process finishes.
                def _monitor() -> None:
                    proc.wait()
                    try:
                        with open(state_path, 'r') as f:
                            s = _json.load(f)
                        s['exit_code'] = proc.returncode
                        with open(state_path, 'w') as f:
                            _json.dump(s, f)
                    except Exception:
                        pass

                threading.Thread(target=_monitor, daemon=True).start()

                print(f'[build] pid={proc.pid}  log={log_path}', file=sys.stderr, flush=True)
                return (
                    f'Build started (pid={proc.pid}).\n'
                    f'Log: {log_path}\n'
                    'Call build_project again in ~60 s to check progress.'
                )

            except Exception as e:
                print(f'ERROR: Build failed to start: {str(e)}', file=sys.stderr)
                return f'Build failed to start: {str(e)}'

        @mcp.tool()
        async def set_target(target: str, ctx: Context) -> str:
            """Set the ESP-IDF target (esp32, esp32s3, esp32c6, etc.)"""
            try:
                cmd = _idf_cmd('set-target', target)
                print(f'INFO: Setting target to {target} in path: {project_path}', file=sys.stderr)
                returncode, output = await _run_idf_streaming(cmd, project_path, 'set-target', ctx=ctx)
                if returncode == 0:
                    print(f'INFO: Target set to: {target}', file=sys.stderr)
                    return f'Target set to: {target}'
                else:
                    return f'Failed to set target:\n{output}'
            except Exception as e:
                print(f'ERROR: Failed to set target: {str(e)}', file=sys.stderr)
                return f'Error setting target: {str(e)}'

        @mcp.tool()
        async def flash_project(port: str | None = None, ctx: Context = None) -> str:
            """Flash the built project to connected device"""
            try:
                port_args = ['-p', port] if port else []
                cmd = _idf_cmd(*port_args, 'flash')
                print(f'INFO: Flashing project in path: {project_path}', file=sys.stderr)
                returncode, output = await _run_idf_streaming(cmd, project_path, 'flash', ctx=ctx)
                tail = '\n'.join(output.splitlines()[-40:])
                if returncode == 0:
                    print('INFO: Flash successful', file=sys.stderr)
                    return f'Flash succeeded{" on " + port if port else ""}.\n\n{tail}'
                else:
                    return f'Flash FAILED (exit {returncode}):\n\n{tail}'
            except Exception as e:
                print(f'ERROR: Flash failed: {str(e)}', file=sys.stderr)
                return f'Error flashing: {str(e)}'

        @mcp.tool()
        async def fullclean_project(ctx: Context) -> str:
            """Full clean via idf.py fullclean (removes build directory and CMake cache)"""
            try:
                cmd = _idf_cmd('fullclean')
                print(f'INFO: Full cleaning project in path: {project_path}', file=sys.stderr)
                returncode, output = await _run_idf_streaming(cmd, project_path, 'fullclean', ctx=ctx)
                if returncode == 0:
                    print('INFO: Full clean successful', file=sys.stderr)
                    return 'Full clean successful. Build directory and CMake cache removed.'
                else:
                    return f'Full clean failed:\n{output}'
            except Exception as e:
                print(f'ERROR: Full clean failed: {str(e)}', file=sys.stderr)
                return f'Full clean failed: {str(e)}'

        # --------------------------------------------------------
        # TOOLS — Windows Serial Monitor
        # --------------------------------------------------------

        @mcp.tool()
        async def create_project(name: str, path: str | None = None, ctx: Context = None) -> str:
            """Create a new ESP-IDF project.

            By default, creates the new project under the parent directory of
            the current active project. Use `path` to choose a different
            destination folder.
            """
            try:
                workspace_dir = os.path.dirname(project_path)
                destination = os.path.abspath(path) if path else workspace_dir
                cmd = _idf_cmd('create-project', name, '--path', destination)
                print(
                    f'INFO: Creating project {name} in destination: {destination}',
                    file=sys.stderr,
                )
                returncode, output = await _run_idf_streaming(
                    cmd, workspace_dir, 'create-project', ctx=ctx
                )
                target_dir = destination
                project_subdir = os.path.join(destination, name)
                if os.path.isdir(project_subdir):
                    target_dir = project_subdir
                if returncode == 0:
                    return f'Project created successfully: {target_dir}\n\n{output}'
                return f'Project creation failed (exit {returncode}):\n\n{output}'
            except Exception as e:
                print(f'ERROR: Project creation failed: {str(e)}', file=sys.stderr)
                return f'Error creating project: {str(e)}'

        @mcp.tool()
        async def create_component(name: str, project_dir: str | None = None, ctx: Context = None) -> str:
            """Create a new ESP-IDF component inside a project.

            Defaults to the current active project. Use `project_dir` to target
            a different project directory.
            """
            try:
                target_project_dir = os.path.abspath(project_dir) if project_dir else project_path
                cmd = _idf_cmd('-C', target_project_dir, 'create-component', name)
                print(
                    f'INFO: Creating component {name} in project: {target_project_dir}',
                    file=sys.stderr,
                )
                returncode, output = await _run_idf_streaming(
                    cmd, target_project_dir, 'create-component', ctx=ctx
                )
                component_dir = os.path.join(target_project_dir, 'components', name)
                if not os.path.isdir(component_dir):
                    alt_component_dir = os.path.join(target_project_dir, name)
                    if os.path.isdir(alt_component_dir):
                        component_dir = alt_component_dir
                if returncode == 0:
                    return f'Component created successfully: {component_dir}\n\n{output}'
                return f'Component creation failed (exit {returncode}):\n\n{output}'
            except Exception as e:
                print(f'ERROR: Component creation failed: {str(e)}', file=sys.stderr)
                return f'Error creating component: {str(e)}'

        @mcp.tool()
        def monitor_boot(
            port: str | None = None,
            baudrate: int = 115200,
            capture_duration: int = 15,
            wait_for: str | None = None,
        ) -> str:
            """Reset the device and capture the boot log (Windows).

            Opens the serial port, triggers a hardware reset via the DTR/RTS
            control lines, captures output for capture_duration seconds, then
            closes the port. Returns the full boot log including bootloader
            messages, component init, and startup diagnostics.

            If wait_for is set, stops capturing early as soon as the given
            string appears in the output.

            Args:
                port:             Serial port (e.g. COM4). Auto-detected if omitted.
                baudrate:         Baud rate (default 115200).
                capture_duration: Seconds to capture (default 15, max 120).
                wait_for:         Stop early when this string appears
                                  (e.g. 'Sensor scan:', 'Guru Meditation Error').
            """
            err = _require_serial('monitor_boot')
            if err:
                return err

            if win_monitor.has_session:
                return (
                    'A monitor session is already active. '
                    'Call monitor_stop first.'
                )

            resolved_port = port or _default_port()
            if not resolved_port:
                return 'No serial port found. Plug in the device or specify port explicitly.'

            capture_duration = max(1, min(capture_duration, 120))

            session = WindowsMonitorSession()
            try:
                print(
                    f'INFO: Resetting device on {resolved_port} and capturing boot log ({capture_duration}s)',
                    file=sys.stderr,
                )
                session.start(resolved_port, baudrate=baudrate, reset=True)

                collected: list[str] = []
                deadline = time.monotonic() + capture_duration
                while time.monotonic() < deadline:
                    lines, _ = session.read()
                    collected.extend(lines)
                    if wait_for and wait_for in ''.join(collected):
                        break
                    time.sleep(0.05)

            except Exception as e:
                print(f'ERROR: monitor_boot failed: {str(e)}', file=sys.stderr)
                return f'Error capturing boot log: {str(e)}'
            finally:
                session.stop()

            output = _strip_ansi(''.join(collected))
            if not output.strip():
                output = '(no output captured — check port and baud rate)'

            header = f'Boot log from {resolved_port}'
            if wait_for and wait_for in output:
                header += f' (stopped on "{wait_for}")'
            else:
                header += f' ({capture_duration}s)'

            print(f'INFO: Boot log capture complete from {resolved_port}', file=sys.stderr)
            return f'{header}:\n{output}'

        @mcp.tool()
        def monitor_start(
            port: str | None = None,
            baudrate: int = 115200,
            reset: bool = True,
        ) -> str:
            """Start the serial monitor in the background (Windows).

            Opens the serial port and starts a background reader thread.
            Optionally resets the device on connect via DTR/RTS.
            Use monitor_read to retrieve output, monitor_send to send
            commands, and monitor_stop to terminate the session.

            Args:
                port:     Serial port (e.g. COM4). Auto-detected if omitted.
                baudrate: Baud rate (default 115200).
                reset:    Trigger hardware reset on connect (default True).
            """
            err = _require_serial('monitor_start')
            if err:
                return err

            if win_monitor.has_session:
                return 'A monitor session is already active. Call monitor_stop first.'

            resolved_port = port or _default_port()
            if not resolved_port:
                return 'No serial port found. Plug in the device or specify port explicitly.'

            try:
                print(
                    f'INFO: Starting monitor on {resolved_port} (reset={reset})',
                    file=sys.stderr,
                )
                win_monitor.start(resolved_port, baudrate=baudrate, reset=reset)

                # Give the board time to boot
                time.sleep(2)

                if not win_monitor.is_running:
                    win_monitor.stop()
                    return f'Monitor failed to start on {resolved_port}.'

                return f'Monitor started on {resolved_port}. Use monitor_read to retrieve output.'

            except Exception as e:
                print(f'ERROR: monitor_start failed: {str(e)}', file=sys.stderr)
                return f'Error starting monitor: {str(e)}'

        @mcp.tool()
        def monitor_send(text: str) -> str:
            """Send text to the device through the running monitor session (Windows).

            Writes the text to the serial port. A newline is appended
            automatically if not already present. Use monitor_read to
            retrieve the device response.

            Args:
                text: Text to send (e.g. 'STATUS', 'START', 'hello\\n').
            """
            if not win_monitor.is_running:
                return 'No monitor session is running. Call monitor_start first.'
            try:
                win_monitor.send(text)
                print(f'INFO: Sent to device: {text.strip()!r}', file=sys.stderr)
                return f'Sent: {text.strip()}'
            except Exception as e:
                print(f'ERROR: monitor_send failed: {str(e)}', file=sys.stderr)
                return f'Error sending to device: {str(e)}'

        @mcp.tool()
        def monitor_read(
            max_lines: int = 200,
            timeout: float = 0,
            wait_for: str | None = None,
        ) -> str:
            """Read buffered output from the monitor session (Windows).

            Returns up to max_lines of new output since the last read.
            Can be called repeatedly to poll for new device output.

            If timeout > 0, waits up to that many seconds for new output.
            If wait_for is set, returns as soon as the given string appears
            in the buffered output, or when the timeout expires.

            Args:
                max_lines: Maximum lines to return (default 200, max 1000).
                timeout:   Seconds to wait for output (default 0 = instant, max 30).
                wait_for:  Return early when this string appears
                           (e.g. 'OK', 'FAIL', 'Guru Meditation').
            """
            if not win_monitor.has_session:
                return 'No monitor session is active. Call monitor_start first.'

            max_lines = max(1, min(max_lines, 1000))
            timeout = max(0.0, min(float(timeout), 30.0))

            collected: list[str] = []
            deadline = time.monotonic() + timeout if timeout > 0 else 0.0

            while True:
                lines, remaining = win_monitor.read(max_lines)
                collected.extend(lines)
                if wait_for and wait_for in ''.join(collected):
                    break
                if deadline == 0.0:
                    break
                if time.monotonic() >= deadline:
                    break
                if not win_monitor.is_running:
                    break
                time.sleep(0.05)

            if not collected:
                if not win_monitor.is_running:
                    return '(no new output; monitor session is no longer running)'
                if timeout > 0:
                    msg = f'(no output after {timeout}s'
                    if wait_for:
                        msg += f'; "{wait_for}" not found'
                    return msg + ')'
                return '(no new output)'

            output = _strip_ansi(''.join(collected))

            # Append informational suffixes
            _, remaining = win_monitor.read(0)
            if remaining > 0:
                output += f'\n[{remaining} more lines in buffer]'
            if not win_monitor.is_running:
                output += '\n[note: monitor session is no longer running]'
            if wait_for:
                if wait_for in output:
                    output += f'\n[matched: "{wait_for}"]'
                else:
                    output += f'\n["{wait_for}" not found in output]'

            return output

        @mcp.tool()
        def monitor_stop() -> str:
            """Stop the monitor session and clean up resources (Windows).

            Works even if the session has already ended on its own.
            Call monitor_read before stopping if you need remaining output.
            """
            if not win_monitor.has_session:
                return 'No monitor session is active.'
            was_running = win_monitor.is_running
            win_monitor.stop()
            status = 'Monitor stopped.' if was_running else 'Monitor had already stopped; session cleaned up.'
            print(f'INFO: {status}', file=sys.stderr)
            return status

        # --------------------------------------------------------
        # RESOURCES — Data Access
        # --------------------------------------------------------

        @mcp.resource('project://config')
        def get_project_config() -> str:
            """Get current project configuration"""
            build_dir = args.get('build_dir', '')
            config: dict[str, Any] = {}

            if not os.path.exists(build_dir):
                config['build_dir_exists'] = False
                return json.dumps(config, indent=2)

            config['build_dir'] = build_dir
            proj_desc_fn = f'{build_dir}/project_description.json'
            config['project_description'] = 'Project description does not exist'

            try:
                with open(proj_desc_fn, encoding='utf-8') as f:
                    config['project_description'] = json.load(f)
            except (OSError, ValueError):
                pass

            return json.dumps(config, indent=2)

        @mcp.resource('project://status')
        def get_project_status() -> str:
            """Get current project build status"""
            try:
                status = {
                    'project_path': project_path,
                    'target': get_target(project_path),
                    'idf_version': idf_version(),
                }
                build_dir = args.build_dir
                if os.path.exists(build_dir):
                    status['build_dir'] = build_dir
                    artifacts = ['bootloader', 'partition_table', 'app-flash', 'flash_args']
                    status['artifacts'] = {}
                    for artifact in artifacts:
                        artifact_path = os.path.join(build_dir, artifact)
                        status['artifacts'][artifact] = os.path.exists(artifact_path)
                else:
                    status['build_dir_exists'] = False
                return json.dumps(status, indent=2)
            except Exception as e:
                return f'Error getting status: {str(e)}'

        @mcp.resource('project://devices')
        def get_connected_devices() -> str:
            """Get list of connected devices"""
            try:
                import serial.tools.list_ports
                devices_on_ports = [p.device.strip() for p in serial.tools.list_ports.comports()]
                print(f'Devices: {devices_on_ports}', file=sys.stderr)
                devices = {'available_ports': devices_on_ports if devices_on_ports else []}
                return json.dumps(devices, indent=2)
            except Exception as e:
                return f'Error getting devices: {str(e)}'

        # Start the MCP server
        print('MCP Server running on stdio...')
        try:
            mcp.run()
        except KeyboardInterrupt:
            print('\nMCP Server stopped.')
        except Exception as e:
            print(f'MCP Server error: {e}')
        finally:
            if win_monitor.has_session:
                win_monitor.stop()
                print('INFO: Monitor session cleaned up on server exit', file=sys.stderr)

    # Return the action extension
    return {
        'actions': {
            'mcp-server': {
                'callback': start_mcp_server,
                'help': 'Start MCP (Model Context Protocol) server for AI integration',
                'options': [],
            },
        }
    }
