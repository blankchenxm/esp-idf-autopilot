from __future__ import annotations

import hashlib
import copy
import shutil
import subprocess
import sys
import re
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx

from ..codex_runner import background_creationflags


class DatasheetProvider(Protocol):
    def read(self, path: Path, level: str) -> dict: ...


def validate_datasheet_record(record: dict, required_level: str = "L1") -> list[str]:
    errors: list[str] = []
    for key in ("part_number", "variant", "document_id", "revision", "source", "content_hash", "coverage", "level"):
        if not record.get(key):
            errors.append(f"datasheet.{key} is missing")
    levels = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
    if levels.get(record.get("level", ""), -1) < levels[required_level]:
        errors.append(f"datasheet level {record.get('level')} is below required {required_level}")
    if record.get("technical_content_valid") is not True:
        errors.append("datasheet extraction has not been validated as technical content")
    return errors


class DatasheetArtifactAdapter:
    """Fetch and hash the exact datasheet artifact named by the design draft."""

    def __init__(self, repo_root: Path, fetch=None, project_id: str | None = None):
        self.repo_root, self._fetch, self.project_id = repo_root.resolve(), fetch, project_id

    def acquire_if_needed(self, record: dict, destination: Path) -> dict:
        """Resolve a model placeholder through the repository acquisition cascade.

        Design drafts are not trusted to invent URL or hash values.  When the
        draft names an unavailable source, acquire the exact part PDF through
        the established script, then return a repository-relative reference
        for the ordinary hash/text validation path.
        """
        source = str(record.get("source", ""))
        parsed = urlparse(source)
        local = (self.repo_root / source).resolve() if source and not parsed.scheme else None
        if parsed.scheme in {"http", "https"} or (local and local.is_file()):
            return copy.deepcopy(record)
        return self.acquire(record, destination)

    def acquire(self, record: dict, destination: Path) -> dict:
        """Always acquire the part through the repository's verified cascade."""
        part = str(record.get("part_number", "")).strip()
        if not part:
            raise ValueError("datasheet part_number is required to resolve an unavailable source")
        incoming_match = self._find_project_incoming(part)
        if incoming_match is not None:
            value = copy.deepcopy(record)
            value["source"] = incoming_match.relative_to(self.repo_root).as_posix()
            value["content_hash"] = hashlib.sha256(incoming_match.read_bytes()).hexdigest()
            return value
        destination = destination.resolve()
        try:
            destination.relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError("datasheet destination escapes repository") from exc
        destination.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(self.repo_root / "tools" / "fetch_datasheet.py"), part, "--out", str(destination)]
        result = subprocess.run(command, cwd=self.repo_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=180, creationflags=background_creationflags())
        path = destination / f"{part}.pdf"
        if result.returncode != 0 or not path.is_file():
            raise RuntimeError(f"datasheet acquisition failed for {part}: {result.stdout[-1000:]}")
        value = copy.deepcopy(record)
        value["source"] = path.relative_to(self.repo_root).as_posix()
        value["content_hash"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return value

    def _find_project_incoming(self, part: str) -> Path | None:
        """Find a user-supplied project PDF despite a regenerated owner ID."""
        if not self.project_id:
            return None
        root = self.repo_root / "hardware" / "datasheets" / "incoming" / self.project_id
        if not root.is_dir():
            return None
        normalized_part = re.sub(r"[^a-z0-9]", "", part.casefold())
        matches = [
            path for path in root.rglob("*.pdf")
            if normalized_part in re.sub(r"[^a-z0-9]", "", path.stem.casefold())
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f"multiple user-supplied Datasheet PDFs match {part!r}: "
                f"{[path.relative_to(root).as_posix() for path in matches]}"
            )
        return None

    def canonicalize(self, record: dict) -> dict:
        """Materialize one content-addressed repository copy of the artifact."""
        from ..datasheet_library import datasheet_objects, resolve_datasheet_alias
        source = resolve_datasheet_alias(
            self.repo_root, str(record.get("source", "")), self.project_id
        ); parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            response = self._fetch(source) if self._fetch else httpx.get(source, follow_redirects=True, timeout=60)
            if hasattr(response, "raise_for_status"): response.raise_for_status()
            data = response.content if hasattr(response, "content") else bytes(response)
        else:
            path = (self.repo_root / source).resolve() if not Path(source).is_absolute() else Path(source).resolve()
            try: path.relative_to(self.repo_root)
            except ValueError as exc: raise ValueError("datasheet source escapes repository") from exc
            data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        suffix = ".pdf" if data.startswith(b"%PDF-") else ".txt"
        target = datasheet_objects(self.repo_root) / f"{actual}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != data:
            raise ValueError("content-addressed datasheet collision")
        if not target.exists():
            target.write_bytes(data)
        value = copy.deepcopy(record)
        value["source"] = target.relative_to(self.repo_root).as_posix()
        value["content_hash"] = actual
        return value

    def inspect(self, record: dict) -> dict:
        from ..datasheet_library import resolve_datasheet_alias
        source = resolve_datasheet_alias(
            self.repo_root, str(record.get("source", "")), self.project_id
        ); parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            response = self._fetch(source) if self._fetch else httpx.get(source, follow_redirects=True, timeout=60)
            if hasattr(response, "raise_for_status"): response.raise_for_status()
            data = response.content if hasattr(response, "content") else bytes(response)
            resolved = source
        else:
            path = (self.repo_root / source).resolve() if not Path(source).is_absolute() else Path(source).resolve()
            try: path.relative_to(self.repo_root)
            except ValueError as exc: raise ValueError("datasheet source escapes repository") from exc
            if not path.is_file(): raise FileNotFoundError(f"datasheet source is missing: {path}")
            data, resolved = path.read_bytes(), path.relative_to(self.repo_root).as_posix()
        if len(data) < 256: raise ValueError("datasheet artifact is too small to contain technical content")
        actual_hash = hashlib.sha256(data).hexdigest()
        declared = str(record.get("content_hash", "")).lower().removeprefix("sha256:")
        if declared and declared != actual_hash: raise ValueError("datasheet content_hash does not match the fetched artifact")
        is_pdf = data.startswith(b"%PDF-")
        is_text = not is_pdf and b"\x00" not in data[:4096]
        if not (is_pdf or is_text): raise ValueError("datasheet artifact is neither a PDF nor readable technical text")
        if is_pdf:
            executable = shutil.which("pdftotext")
            if not executable: raise RuntimeError("pdftotext is required to validate PDF technical content")
            extracted = subprocess.run([executable, "-", "-"], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, creationflags=background_creationflags())
            if extracted.returncode != 0: raise RuntimeError(f"pdftotext failed: {extracted.stderr.decode(errors='replace')[-500:]}")
            text = extracted.stdout.decode("utf-8", errors="replace")
        else: text = data.decode("utf-8", errors="replace")
        normalized = text.lower()
        terms = [
            term
            for term in (
                "pin",
                "register",
                "timing",
                "voltage",
                "current",
                "interface",
                "reset",
                "command",
            )
            if term in normalized
        ]
        part_number = str(record.get("part_number") or "").strip()
        compact_part = re.sub(r"[^a-z0-9]", "", part_number.lower())
        compact_text = re.sub(r"[^a-z0-9]", "", normalized)
        identity_verified = bool(compact_part and compact_part in compact_text)
        coverage_terms = {
            "identity": identity_verified,
            "pins": any(term in normalized for term in ("pin", "terminal")),
            "interface": any(
                term in normalized
                for term in ("interface", "i2c", "i²c", "spi", "i2s", "serial")
            ),
            "electrical": any(
                term in normalized
                for term in ("voltage", "current", "absolute maximum", "supply")
            ),
            "timing": any(
                term in normalized for term in ("timing", "frequency", "clock")
            ),
            "reset_recovery": any(
                term in normalized for term in ("reset", "recovery", "power-on")
            ),
            "registers_commands": any(
                term in normalized
                for term in ("register", "command", "opcode", "bit field", "bitfield")
            ),
        }
        coverage = [name for name, present in coverage_terms.items() if present]
        revision_match = re.search(
            r"\b(?:revision|rev\.?)\s*[:#-]?\s*([a-z0-9][a-z0-9._-]{0,15})",
            normalized[:20000],
            re.IGNORECASE,
        )
        technical = len(text.strip()) >= 200 and bool(terms) and identity_verified
        if not technical: raise ValueError("datasheet extraction does not contain enough recognizable technical content")
        return {
            "source": source,
            "resolved_source": resolved,
            "sha256": actual_hash,
            "size": len(data),
            "media_type": "application/pdf" if is_pdf else "text/plain",
            "part_number": part_number,
            "identity_verified": identity_verified,
            "document_id": str(record.get("document_id") or f"sha256:{actual_hash}"),
            "revision": str(
                record.get("revision")
                or (revision_match.group(1) if revision_match else f"sha256:{actual_hash[:16]}")
            ),
            "level": record.get("level"),
            "coverage": coverage,
            "extracted_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "extracted_text_chars": len(text),
            # L2 implementation facts must be traceable to receipt-owned
            # evidence, not merely to an asserted PDF identity.  The receipt
            # raw-output artifact is hash-bound by DesignGroundingAdapter, so
            # retaining the complete extraction here gives a repair/deep
            # reader an auditable source for registers, timing and formats.
            # Keep text_sample as a convenient compact preview for tooling.
            "extracted_text": text,
            "technical_terms": terms,
            "technical_content_valid": technical,
            "text_sample": text[:1000],
        }
