from __future__ import annotations

import re
from pathlib import Path
from typing import Any


LAYER_ORDER = {
    "board_resource": 0,
    "device_driver": 1,
    "data_service": 2,
    "product_policy": 3,
    "system_orchestration": 4,
    "integration": 5,
}
_CMAKE_REQUIRES = re.compile(r"\b(?:PRIV_)?REQUIRES\s+([^)]+)", re.I | re.S)
_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)


def _cmake_dependencies(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8", errors="ignore")
    result: set[str] = set()
    for match in _CMAKE_REQUIRES.finditer(text):
        result.update(token for token in re.split(r"\s+", match.group(1).strip()) if token)
    return result


def validate_component_architecture(project_dir: Path, contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    manifest = contract.get("architecture", {}).get("component_api_manifest")
    if not isinstance(manifest, list) or not manifest:
        return ["architecture.component_api_manifest is required"]
    entries = {
        str(item.get("component")): item
        for item in manifest if isinstance(item, dict)
    }
    resource_owners: dict[str, str] = {}
    exported_headers: dict[str, str] = {}
    for component, item in entries.items():
        layer = str(item.get("responsibility_layer") or "")
        if layer not in LAYER_ORDER:
            errors.append(f"component {component!r} has invalid responsibility_layer")
        apis = item.get("exported_semantic_apis")
        if not isinstance(apis, list) or not apis:
            errors.append(f"component {component!r} has no exported_semantic_apis")
        for api in apis or []:
            header = str(api.get("header") or "")
            symbol = str(api.get("symbol") or "")
            if not header or not symbol:
                errors.append(f"component {component!r} has an incomplete semantic API")
            elif header in exported_headers and exported_headers[header] != component:
                errors.append(f"header {header!r} has duplicate semantic owners")
            else:
                exported_headers[header] = component
        for resource in item.get("resources", []):
            resource = str(resource)
            if resource in resource_owners:
                errors.append(
                    f"resource {resource!r} has duplicate owners "
                    f"{resource_owners[resource]!r} and {component!r}"
                )
            resource_owners[resource] = component
    consumers: set[str] = set()
    for component, item in entries.items():
        component_dir = project_dir / ("main" if component == "main" else f"components/{component}")
        deps = _cmake_dependencies(component_dir / "CMakeLists.txt")
        declared = set(map(str, item.get("dependencies", [])))
        project_deps = deps & set(entries)
        if project_deps != declared:
            errors.append(
                f"component {component!r} CMake dependencies {sorted(project_deps)} "
                f"do not match manifest {sorted(declared)}"
            )
        layer = LAYER_ORDER.get(str(item.get("responsibility_layer")), -1)
        allowed_layers = set(map(str, item.get("allowed_dependency_layers", [])))
        for dependency in declared:
            target = entries.get(dependency)
            if target is None:
                errors.append(f"component {component!r} depends on undeclared component {dependency!r}")
                continue
            target_layer = str(target.get("responsibility_layer") or "")
            if target_layer not in allowed_layers:
                errors.append(f"component {component!r} may not depend on layer {target_layer!r}")
            if LAYER_ORDER.get(target_layer, 99) > layer:
                errors.append(f"component {component!r} depends upward on {dependency!r}")
            consumers.add(dependency)
        if item.get("test_only"):
            errors.append(f"test-only component {component!r} is present in production manifest")
        for path in component_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".c", ".h", ".cc", ".cpp"}:
                continue
            for header in _INCLUDE.findall(path.read_text(encoding="utf-8", errors="ignore")):
                owner = exported_headers.get(header)
                if owner and owner != component and owner not in declared:
                    errors.append(f"{path.relative_to(project_dir).as_posix()} includes {header!r} without dependency authority")
    main_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted((project_dir / "main").rglob("*"))
        if path.is_file() and path.suffix.lower() in {".c", ".h", ".cc", ".cpp"}
    ) if (project_dir / "main").is_dir() else ""
    low_level_headers = {
        str(api.get("header"))
        for component, item in entries.items()
        if item.get("responsibility_layer") in {"board_resource", "device_driver"}
        for api in item.get("exported_low_level_apis", [])
    }
    included = set(_INCLUDE.findall(main_text))
    forbidden = sorted(included & low_level_headers)
    if forbidden:
        errors.append(f"main includes low-level owner headers {forbidden}")
    for component, item in entries.items():
        if component != "main" and not item.get("test_only") and component not in consumers:
            errors.append(f"production component {component!r} has no normal-runtime consumer")
    return errors
