from __future__ import annotations

import re
from pathlib import Path


SECRET_LINE = re.compile(
    r"(?im)^(\s*(?:(?:wifi\s+)?(?:ssid|password|passwd)|token|api[_ -]?key|secret)\s*:\s*)(.+?)\s*$"
)


def secret_values(text: str) -> list[str]:
    return [match.group(2).strip() for match in SECRET_LINE.finditer(text) if match.group(2).strip()]


def redact_text(text: str) -> str:
    return SECRET_LINE.sub(lambda match: match.group(1) + "[REDACTED: user-owned local secret]", text)


def redact_values(text: str, values: list[str]) -> str:
    for value in sorted(set(values), key=len, reverse=True):
        if value: text = text.replace(value, "[REDACTED]")
    return text


def assert_no_secret_values(text: str, values: list[str], context: str) -> None:
    if any(value and value in text for value in values):
        raise ValueError(f"{context} contains a user-owned plaintext secret")


def write_crumb_credentials(requirements: Path, output: Path) -> None:
    text = requirements.read_text(encoding="utf-8")
    def value(label: str) -> str:
        found = re.search(rf"^\s*wifi\s+{label}\s*:\s*(\S.*?)\s*$", text, re.I | re.M)
        if not found: raise ValueError(f"missing wifi {label} in {requirements}")
        return found.group(1).strip()
    def c_string(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("#pragma once\n" f'#define CRUMB_WIFI_SSID "{c_string(value("ssid"))}"\n' f'#define CRUMB_WIFI_PASSWORD "{c_string(value("password"))}"\n', encoding="utf-8")
