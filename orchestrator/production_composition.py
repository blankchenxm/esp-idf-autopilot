from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_FUNCTION = re.compile(
    r"(?m)^[A-Za-z_][\w\s\*]*?\b(?P<name>[A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{"
)
_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_CONTROL = frozenset({"if", "for", "while", "switch", "sizeof", "return"})
_OBSERVATION = re.compile(
    r"HARNESS_STEP\s+step=(?P<step>[A-Za-z0-9_.:-]+)\s+"
    r"operation=(?P<operation>[A-Za-z0-9_.:-]+)\s+"
    r"correlation=(?P<correlation>[A-Za-z0-9_.:-]+)"
)
_EDGE_OBSERVATION = re.compile(
    r"HARNESS_EDGE\s+from=(?P<from>[A-Za-z0-9_.:-]+)\s+"
    r"to=(?P<to>[A-Za-z0-9_.:-]+)\s+kind=(?P<kind>[a-z_]+)\s+"
    r"correlation=(?P<correlation>[A-Za-z0-9_.:-]+)"
)


def parse_runtime_observations(text: str) -> list[dict[str, str]]:
    steps = [
        {
            "record_type": "step",
            "runtime_step_id": match.group("step"),
            "operation_id": match.group("operation"),
            "correlation_id": match.group("correlation"),
        }
        for match in _OBSERVATION.finditer(text)
    ]
    edges = [
        {
            "record_type": "edge",
            "from_step": match.group("from"),
            "to_step": match.group("to"),
            "kind": match.group("kind"),
            "correlation_id": match.group("correlation"),
        }
        for match in _EDGE_OBSERVATION.finditer(text)
    ]
    return [*steps, *edges]


def _function_bodies(project_dir: Path) -> dict[str, tuple[Path, str]]:
    result: dict[str, tuple[Path, str]] = {}
    for root in (project_dir / "main", project_dir / "components"):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".c", ".cc", ".cpp"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in _FUNCTION.finditer(text):
                depth = 1
                cursor = match.end()
                while cursor < len(text) and depth:
                    depth += (text[cursor] == "{") - (text[cursor] == "}")
                    cursor += 1
                result[match.group("name")] = (path, text[match.end():cursor - 1])
    return result


def validate_production_composition(
    project_dir: Path,
    contract: dict[str, Any],
    *,
    runtime_observations: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Compile a conservative call graph and bind runtime observations to steps."""
    errors: list[str] = []
    flow = contract.get("architecture", {}).get("runtime_flow") or {}
    entry = str((flow.get("entrypoint") or {}).get("symbol") or "")
    bodies = _function_bodies(project_dir)
    if entry not in bodies:
        return [f"production entrypoint {entry!r} is absent from compiled source set"]
    calls = {
        name: {
            called for called in _CALL.findall(body)
            if called not in _CONTROL and called != name
        }
        for name, (_, body) in bodies.items()
    }
    reachable: set[str] = set()
    pending = [entry]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(calls.get(name, set()) - reachable)
    operation_ids = {str(item.get("operation_id")) for item in contract.get("operations", [])}
    steps = flow.get("steps") or []
    steps_by_id = {str(step.get("id")): step for step in steps}
    for step in steps:
        step_id = str(step.get("id") or "")
        symbol = str(step.get("symbol") or "")
        ids = [str(item) for item in step.get("operation_ids", [])]
        if symbol not in reachable:
            errors.append(f"runtime step {step_id!r} symbol {symbol!r} is unreachable from {entry!r}")
        unknown = sorted(set(ids) - operation_ids)
        if unknown:
            errors.append(f"runtime step {step_id!r} names unknown operation IDs {unknown}")
        if not ids:
            errors.append(f"runtime step {step_id!r} has no operation_ids")
        path_body = bodies.get(symbol)
        if path_body and "ESP_ERR_NOT_SUPPORTED" in path_body[1]:
            errors.append(f"runtime step {step_id!r} reaches an unsupported operation")
    for edge in flow.get("edges", []):
        left = str(edge.get("from_step") or "")
        right = str(edge.get("to_step") or "")
        kind = str(edge.get("kind") or "")
        if left not in steps_by_id or right not in steps_by_id:
            errors.append(f"runtime edge {left!r}->{right!r} has unknown step")
            continue
        if kind == "call":
            left_symbol = str(steps_by_id[left].get("symbol") or "")
            right_symbol = str(steps_by_id[right].get("symbol") or "")
            if right_symbol not in calls.get(left_symbol, set()):
                errors.append(
                    f"runtime call edge {left!r}->{right!r} is absent from source"
                )
        assertions = edge.get("source_assertions") or []
        if not assertions:
            errors.append(f"runtime edge {left!r}->{right!r} lacks source_assertions")
        for assertion in assertions:
            matched = False
            for path in project_dir.glob(str(assertion.get("path_glob") or "")):
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                if all(
                    str(token) in text
                    for token in assertion.get("required_tokens", [])
                ):
                    matched = True
                    break
            if not matched:
                errors.append(
                    f"runtime edge {left!r}->{right!r} source assertion failed"
                )
    scenarios = contract.get("integration", {}).get("production_scenarios") or []
    if not scenarios:
        errors.append("integration has no production_scenarios")
    step_ids = {str(step.get("id")) for step in steps}
    for scenario in scenarios:
        selected = set(str(item) for item in scenario.get("runtime_step_ids", []))
        if not selected or selected - step_ids:
            errors.append(f"production scenario {scenario.get('scenario_id')!r} has invalid runtime_step_ids")
        if scenario.get("entrypoint") != entry:
            errors.append(f"production scenario {scenario.get('scenario_id')!r} bypasses the normal entrypoint")
    if runtime_observations is not None:
        observed_by_step = {
            str(item.get("runtime_step_id")): str(item.get("operation_id"))
            for item in runtime_observations
            if item.get("correlation_id") and item.get("operation_id")
        }
        missing = sorted(step_ids - set(observed_by_step))
        if missing:
            errors.append(f"runtime observations omit declared production steps {missing}")
        for step in steps:
            step_id = str(step.get("id"))
            observed_operation = observed_by_step.get(step_id)
            declared_operations = set(map(str, step.get("operation_ids", [])))
            if (
                observed_operation is not None
                and observed_operation not in declared_operations
            ):
                errors.append(
                    f"runtime observation for {step_id!r} reports undeclared "
                    f"operation {observed_operation!r}"
                )
        observed_edges = {
            (
                str(item.get("from_step")),
                str(item.get("to_step")),
                str(item.get("kind")),
            )
            for item in runtime_observations
            if item.get("record_type") == "edge"
            and item.get("correlation_id")
        }
        missing_edges = sorted(
            (
                str(edge.get("from_step")),
                str(edge.get("to_step")),
                str(edge.get("kind")),
            )
            for edge in flow.get("edges", [])
            if (
                str(edge.get("from_step")),
                str(edge.get("to_step")),
                str(edge.get("kind")),
            ) not in observed_edges
        )
        if missing_edges:
            errors.append(f"runtime observations omit declared edges {missing_edges}")
    return errors
