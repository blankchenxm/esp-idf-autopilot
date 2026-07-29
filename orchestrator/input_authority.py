"""Deterministically compile user inputs into a hash-bound authority record."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .storage import atomic_write_json, file_ref


_PART = re.compile(r"\b[A-Z][A-Z0-9-]*\d[A-Z0-9-]*\b", re.IGNORECASE)
_GPIO = re.compile(r"(?:GPIO\s*(?:pin|引脚)?\s*|PIN[_ ]?)(\d+)\b", re.IGNORECASE)
_C_PIN_DEFINE = re.compile(r"(?m)^\s*#define\s+([A-Za-z][A-Za-z0-9_]*)\s+(\d+)\b")
_I2C_LINE = re.compile(r"(?i)\bi2c\b[^\r\n]{0,120}")
_I2C_CONTROLLER = re.compile(r"(?i)(?:controller|bus|port|控制器)\s*(?:number|num|#|号)?\s*([01])\b")
_I2C_CLOCK = re.compile(r"(?i)\b(\d+)\s*(khz|mhz|hz)\b")
_SECRET = re.compile(r"(?i)\b(?:wifi\s*)?(?:password|passwd|token|api[_ -]?key)\s*[:：=]\s*`?([^\s`]+)")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compile_input_authority(repo_root: Path, project: str) -> dict[str, Any]:
    """Return only user-stated identities and constraints, never inferred variants.

    The record deliberately stores source hashes and positions rather than
    credentials.  Extraction is permissive; enforcement later requires an
    exact identifier match, so a missed token cannot authorize a substitution.
    """
    sources: list[dict[str, Any]] = []
    text_by_area: dict[str, str] = {}
    for area in ("requirements", "connections"):
        path = repo_root / area / f"{project}.md"
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        text_by_area[area] = text
        sources.append({**file_ref(path, repo_root, "text/markdown").model_dump(), "sha256": _sha256(raw)})

    identifiers: list[dict[str, Any]] = []
    pins: list[dict[str, Any]] = []
    protocols: list[dict[str, Any]] = []
    i2c_configs: list[dict[str, Any]] = []
    for area, text in text_by_area.items():
        secret_spans = [match.span(1) for match in _SECRET.finditer(text)]
        for match in _PART.finditer(text):
            if any(
                match.start() < secret_end and match.end() > secret_start
                for secret_start, secret_end in secret_spans
            ):
                continue
            value = match.group(0)
            # Numeric prose such as 50 MiB does not match this pattern; retain
            # only conventional component-like tokens.
            if len(value) >= 4 and any(char.isalpha() for char in value):
                item = {"value": value, "source": area, "offset": match.start()}
                if item not in identifiers:
                    identifiers.append(item)
        for match in _GPIO.finditer(text):
            pins.append({"gpio": int(match.group(1)), "source": area, "offset": match.start()})
        for match in _C_PIN_DEFINE.finditer(text):
            item = {"name": match.group(1), "gpio": int(match.group(2)), "source": area, "offset": match.start()}
            if item not in pins:
                pins.append(item)
        for match in re.finditer(r"https?://[^\s`)>]+", text):
            protocols.append({"endpoint": match.group(0), "source": area, "offset": match.start()})
        for match in _I2C_LINE.finditer(text):
            controller = _I2C_CONTROLLER.search(match.group(0))
            clock = _I2C_CLOCK.search(match.group(0))
            if controller is None or clock is None:
                continue
            multiplier = {"hz": 1, "khz": 1_000, "mhz": 1_000_000}[clock.group(2).casefold()]
            item = {"controller": int(controller.group(1)), "clock_hz": int(clock.group(1)) * multiplier, "source": area, "offset": match.start()}
            if item not in i2c_configs:
                i2c_configs.append(item)
    secret_references = [
        {"kind": "user_input_secret", "source": area, "offset": match.start()}
        for area, text in text_by_area.items() for match in _SECRET.finditer(text)
    ]
    return {
        "schema_version": "1.0",
        "project_id": project,
        "inputs": sources,
        "identifiers": identifiers,
        "pins": pins,
        "protocols": protocols,
        "i2c_configs": i2c_configs,
        "secret_references": secret_references,
        "digest": _sha256("".join(item["sha256"] for item in sources).encode()),
    }


def write_input_authority(path: Path, repo_root: Path, project: str) -> dict[str, Any]:
    value = compile_input_authority(repo_root, project)
    atomic_write_json(path, value)
    return value


def exact_authorized_identifier(authority: dict[str, Any], value: str) -> bool:
    return value in {str(item.get("value")) for item in authority.get("identifiers", [])}
