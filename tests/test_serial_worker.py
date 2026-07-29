from __future__ import annotations

import io

from orchestrator import serial_worker


class _Stdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()


def test_serial_worker_writes_utf8_without_console_code_page(monkeypatch):
    output = _Stdout()
    monkeypatch.setattr(serial_worker.sys, "stdout", output)
    serial_worker._write_result('{"transcript":"\ufffd"}')
    assert output.buffer.getvalue() == b'{"transcript":"\xef\xbf\xbd"}'
