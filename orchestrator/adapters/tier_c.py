from __future__ import annotations

import hashlib
import mimetypes
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..models import Failure, FailureCategory, Receipt
from ..storage import ProjectStore, file_ref


class TierCArtifactAdapter:
    """Materialize final Integration artifacts before any human observation."""

    def __init__(self, project_dir: Path, store: ProjectStore, run_id: str, fetch=None):
        self.project_dir, self.store, self.run_id, self._fetch = project_dir.resolve(), store, run_id, fetch

    def materialize(self, item: dict) -> tuple[Receipt, dict]:
        started = datetime.now(timezone.utc).isoformat(); receipt_id = self.store.new_id("tier-c-artifact")
        contract = item.get("artifact_contract", {})
        access = contract.get("access_method")
        source_value = str(contract.get("source", "")).format(run_id=self.run_id, item_id=item["id"])
        artifacts = []; metadata: dict = {"access_method": access}
        failure = None
        try:
            if access == "physical_observation":
                metadata["instructions"] = item.get("instructions")
            else:
                if access == "download_url":
                    parsed = urlparse(source_value)
                    if parsed.scheme not in {"http", "https"}:
                        raise ValueError("Tier C download_url requires an http(s) source")
                    response = self._fetch(source_value) if self._fetch else httpx.get(source_value, follow_redirects=True, timeout=60)
                    if hasattr(response, "raise_for_status"): response.raise_for_status()
                    data = response.content if hasattr(response, "content") else bytes(response)
                    name = Path(parsed.path).name or f"{item['id']}.bin"
                elif access == "local_file":
                    source = (self.project_dir / source_value).resolve()
                    try: source.relative_to(self.project_dir)
                    except ValueError as exc: raise ValueError("Tier C local artifact escapes project") from exc
                    if not source.is_file():
                        raise FileNotFoundError(f"Tier C local artifact is missing: {source_value}")
                    data, name = source.read_bytes(), source.name
                else:
                    raise ValueError(f"unsupported Tier C access_method: {access!r}")
                if not data:
                    raise ValueError("Tier C artifact is empty")
                sha = hashlib.sha256(data).hexdigest()
                suffix = Path(name).suffix or mimetypes.guess_extension(contract.get("media_type", "")) or ".bin"
                target = self.store.execution / "tier-c" / self.run_id / item["id"] / f"{sha}{suffix}"
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_bytes(data)
                media_type = contract.get("media_type") or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                reference = file_ref(target, self.project_dir, media_type)
                artifacts.append(reference)
                metadata.update({"path": reference.path, "sha256": reference.sha256, "size": reference.size, "media_type": media_type})
                if media_type in {"audio/wav", "audio/x-wav"} or target.suffix.lower() == ".wav":
                    with wave.open(str(target), "rb") as audio:
                        frames, rate = audio.getnframes(), audio.getframerate()
                        metadata["wav"] = {
                            "channels": audio.getnchannels(),
                            "sample_rate_hz": rate,
                            "sample_width_bits": audio.getsampwidth() * 8,
                            "frames": frames,
                            "duration_s": frames / rate if rate else 0,
                        }
            success = True
        except Exception as exc:
            success = False
            failure = Failure(category=FailureCategory.INTEGRATION, summary=f"Tier C artifact materialization failed: {type(exc).__name__}: {exc}", owner=item.get("owner"))
            metadata = {"access_method": access, "error": str(exc)}
        receipt = Receipt(
            receipt_id=receipt_id, run_id=self.run_id, operation="tier_c_artifact_materialize",
            started_at=started, finished_at=datetime.now(timezone.utc).isoformat(), success=success,
            inputs={"item_id": item["id"], "artifact_contract": contract}, outputs=metadata,
            artifacts=artifacts, failure=failure,
        )
        self.store.write_receipt(receipt, "tier-c")
        return receipt, metadata
