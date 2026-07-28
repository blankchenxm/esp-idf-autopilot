from __future__ import annotations

from pathlib import Path
import hashlib

from orchestrator.adapters.datasheet import DatasheetArtifactAdapter


def test_placeholder_source_is_acquired_and_rewritten(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    tool = repo / "tools" / "fetch_datasheet.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("", encoding="utf-8")
    destination = repo / "projects" / "demo" / "components" / "sensor" / "datasheets"
    adapter = DatasheetArtifactAdapter(repo)

    class Result:
        returncode = 0
        stdout = "downloaded"

    def fake_run(command, **_kwargs):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "PART1.pdf").write_bytes(b"%PDF-" + b"technical register timing " * 30)
        return Result()

    monkeypatch.setattr("orchestrator.adapters.datasheet.subprocess.run", fake_run)
    resolved = adapter.acquire_if_needed({"part_number": "PART1", "source": "official document required"}, destination)

    assert resolved["source"] == "projects/demo/components/sensor/datasheets/PART1.pdf"
    assert len(resolved["content_hash"]) == 64


def test_user_supplied_project_pdf_is_found_and_preserved_after_canonicalization(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    supplied = (
        repo / "hardware" / "datasheets" / "incoming" / "demo"
        / "legacy_owner" / "PART1-revision-a.pdf"
    )
    supplied.parent.mkdir(parents=True)
    supplied.write_bytes(b"%PDF-" + b"technical register timing " * 30)
    adapter = DatasheetArtifactAdapter(repo, project_id="demo")

    acquired = adapter.acquire(
        {"part_number": "PART1", "source": "unavailable placeholder"},
        repo / "hardware" / "datasheets" / "incoming" / "demo" / "new_owner",
    )
    canonical = adapter.canonicalize(acquired)

    assert acquired["source"] == supplied.relative_to(repo).as_posix()
    assert supplied.is_file()
    assert (repo / canonical["source"]).is_file()


def test_inspection_keeps_full_extraction_for_receipt_bound_l2_facts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = repo / "datasheets" / "part.txt"
    source.parent.mkdir(parents=True)
    text = (
        "PART1 register map command timing voltage current interface reset "
        "page address ECC bad block recovery DMA I2S format "
    ) * 30
    source.write_text(text, encoding="utf-8")

    inspected = DatasheetArtifactAdapter(repo).inspect({
        "part_number": "PART1",
        "source": "datasheets/part.txt",
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "level": "L2",
    })

    assert inspected["extracted_text"] == text
    assert inspected["extracted_text_sha256"] == hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()
