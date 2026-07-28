from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .storage import atomic_write_json


def datasheet_root(repo_root: Path) -> Path:
    return repo_root.resolve() / "hardware" / "datasheets"


def datasheet_objects(repo_root: Path) -> Path:
    return datasheet_root(repo_root) / "objects" / "sha256"


def project_alias_path(repo_root: Path, project_id: str) -> Path:
    return datasheet_root(repo_root) / "aliases" / f"{project_id}.json"


def _load_aliases(path: Path, project_id: str | None = None) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": "1.0", "project_id": project_id, "aliases": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("aliases"), dict):
        raise ValueError(f"datasheet alias file is malformed: {path}")
    return value


def resolve_datasheet_alias(repo_root: Path, source: str, project_id: str | None = None) -> str:
    direct = repo_root.resolve() / source
    if direct.is_file():
        return source
    paths: list[Path] = []
    if project_id:
        paths.append(project_alias_path(repo_root, project_id))
    # Read-only compatibility with the pre-namespace alias store.
    paths.append(datasheet_root(repo_root) / "aliases.json")
    key = Path(source).as_posix()
    for aliases in paths:
        record = _load_aliases(aliases, project_id).get("aliases", {}).get(key)
        if record:
            return str(record["source"])
    return source


def _canonical_object(repo_root: Path, source: Path, declared_hash: str | None = None) -> Path:
    data = source.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    declared = str(declared_hash or "").lower().removeprefix("sha256:")
    if declared and declared != actual:
        raise ValueError(f"datasheet hash mismatch for {source}")
    suffix = ".pdf" if data.startswith(b"%PDF-") else source.suffix.lower() or ".bin"
    target = datasheet_objects(repo_root) / f"{actual}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != data:
        raise ValueError("content-addressed datasheet collision")
    if not target.exists():
        target.write_bytes(data)
    return target


def rebuild_datasheet_index(repo_root: Path) -> dict[str, Any]:
    """Rebuild the global object-to-project view from project manifests."""
    objects: dict[str, dict[str, Any]] = {}
    projects_root = repo_root.resolve() / "projects"
    for manifest_path in sorted(projects_root.glob("*/hardware/datasheet-manifest.json")):
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        project_id = str(value.get("project_id") or manifest_path.parents[1].name)
        for record in value.get("datasheets", []):
            sha = str(record.get("sha256", ""))
            if len(sha) != 64:
                continue
            entry = objects.setdefault(sha, {
                "source": record.get("source"),
                "projects": [],
                "parts": [],
            })
            if project_id not in entry["projects"]:
                entry["projects"].append(project_id)
            part = record.get("part_number")
            if part and part not in entry["parts"]:
                entry["parts"].append(part)
    for entry in objects.values():
        entry["projects"].sort()
        entry["parts"].sort()
    index = {"schema_version": "1.0", "objects": objects}
    atomic_write_json(datasheet_root(repo_root) / "index.json", index)
    return index


def sync_datasheet_references(repo_root: Path, project_dir: Path, contract: dict[str, Any]) -> dict[str, Any]:
    """Write project/component references to shared content-addressed objects."""
    project_id = project_dir.resolve().name
    records: list[dict[str, Any]] = []
    for item in contract.get("datasheets", []):
        resolved_source = resolve_datasheet_alias(
            repo_root, str(item.get("source", "")), project_id
        )
        source = (repo_root.resolve() / resolved_source).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"canonical datasheet is missing: {source}")
        target = _canonical_object(repo_root, source, item.get("content_hash"))
        canonical_source = target.relative_to(repo_root.resolve()).as_posix()
        records.append({
            "project_id": project_id,
            "subsystem_id": item["subsystem_id"],
            "part_number": item.get("part_number"),
            "variant": item.get("variant"),
            "document_id": item.get("document_id"),
            "revision": item.get("revision"),
            "source": canonical_source,
            "sha256": item.get("content_hash"),
            "level": item.get("level"),
            "coverage": item.get("coverage", []),
            "provider_receipt_id": item.get("provider_receipt_id"),
        })
    manifest = {
        "schema_version": "1.1",
        "project_id": project_id,
        "datasheets": records,
    }
    atomic_write_json(project_dir / "hardware" / "datasheet-manifest.json", manifest)
    for record in records:
        component = project_dir / "components" / record["subsystem_id"]
        if component.is_dir():
            atomic_write_json(component / "datasheet_refs.json", {
                "schema_version": "1.1",
                "project_id": project_id,
                "datasheets": [record],
            })
    rebuild_datasheet_index(repo_root)
    return manifest


def migrate_project_datasheets(repo_root: Path, project_dir: Path, contract: dict[str, Any]) -> dict[str, Any]:
    """Deduplicate one project's component PDFs and create project aliases."""
    project_id = project_dir.resolve().name
    aliases_path = project_alias_path(repo_root, project_id)
    aliases = _load_aliases(aliases_path, project_id)
    aliases.update({"schema_version": "1.1", "project_id": project_id})
    migrated: list[str] = []
    for path in (project_dir / "components").glob("*/datasheets/*"):
        if not path.is_file():
            continue
        target = _canonical_object(repo_root, path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        old = path.relative_to(repo_root.resolve()).as_posix()
        aliases["aliases"][old] = {
            "source": target.relative_to(repo_root.resolve()).as_posix(),
            "sha256": sha,
        }
        path.unlink()
        migrated.append(old)
    atomic_write_json(aliases_path, aliases)
    for directory in sorted((project_dir / "components").glob("*/datasheets"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    derived = copy.deepcopy(contract)
    for item in derived.get("datasheets", []):
        item["source"] = resolve_datasheet_alias(
            repo_root, str(item.get("source", "")), project_id
        )
    sync_datasheet_references(repo_root, project_dir, derived)
    removed_components: list[str] = []
    components = project_dir / "components"
    if components.is_dir():
        for component in components.iterdir():
            if not component.is_dir():
                continue
            material = [path for path in component.rglob("*") if path.is_file()]
            if not material:
                component.rmdir()
                removed_components.append(component.name)
    return {
        "project_id": project_id,
        "migrated": migrated,
        "removed_empty_components": sorted(removed_components),
    }
