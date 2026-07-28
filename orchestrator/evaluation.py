from __future__ import annotations

import math
import re
import statistics
from typing import Any


EXECUTABLE_EXPECTATION_KEYS = {"marker", "count_min", "count_max", "count_exact", "regex", "measurements", "ordered_markers", "forbidden_markers"}


def _percentile(values: list[float], percentile: float) -> float:
    if not values: raise ValueError("percentile requires samples")
    ordered = sorted(values); position = (len(ordered) - 1) * percentile / 100.0
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper: return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _check_bounds(name: str, value: float, rule: dict[str, Any], reasons: list[str]) -> None:
    if "min" in rule and value < float(rule["min"]): reasons.append(f"{name} {value} below {rule['min']}")
    if "max" in rule and value > float(rule["max"]): reasons.append(f"{name} {value} above {rule['max']}")
    if "target" in rule:
        tolerance = float(rule.get("tolerance", 0)); target = float(rule["target"])
        if abs(value - target) > tolerance: reasons.append(f"{name} {value} outside target {target} +/- {tolerance}")


def _measurement(text: str, rule: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    name = str(rule.get("name") or "measurement"); reasons: list[str] = []
    pattern = str(rule.get("regex", "")); group = int(rule.get("group", 1)); scale = float(rule.get("scale", 1.0))
    try: values = [float(match.group(group)) * scale for match in re.finditer(pattern, text, re.MULTILINE)]
    except (IndexError, ValueError, re.error) as exc: return {"name": name, "samples": []}, [f"{name} extraction failed: {exc}"]
    minimum = int(rule.get("sample_count_min", 1)); maximum = rule.get("sample_count_max")
    if len(values) < minimum: reasons.append(f"{name} sample count {len(values)} below {minimum}")
    if maximum is not None and len(values) > int(maximum): reasons.append(f"{name} sample count {len(values)} above {maximum}")
    actual: dict[str, Any] = {"name": name, "unit": rule.get("unit"), "sample_count": len(values), "samples": values[:100]}
    if values:
        summary = {"min": min(values), "max": max(values), "mean": statistics.fmean(values), "p95": _percentile(values, 95), "stdev": statistics.pstdev(values)}
        actual["summary"] = summary
        statistic = str(rule.get("statistic", "each"))
        if statistic == "each":
            for index, value in enumerate(values): _check_bounds(f"{name}[{index}]", value, rule, reasons)
        elif statistic in summary:
            _check_bounds(f"{name}.{statistic}", summary[statistic], rule, reasons)
        else: reasons.append(f"{name} uses unsupported statistic {statistic}")
        for key, bound in (("mean_min", ("mean", "min")), ("mean_max", ("mean", "max")), ("p95_max", ("p95", "max")), ("stdev_max", ("stdev", "max"))):
            if key in rule: _check_bounds(f"{name}.{bound[0]}", summary[bound[0]], {bound[1]: rule[key]}, reasons)
    return actual, reasons


def evaluate_text(text: str, expected: dict[str, Any]) -> tuple[bool, dict[str, Any], list[str]]:
    """Evaluate deterministic text, numeric samples, statistics, ordering, and exclusions."""
    actual: dict[str, Any] = {}; reasons: list[str] = []
    marker = expected.get("marker")
    if marker is not None:
        found = str(marker) in text; actual["marker_found"] = found
        if not found: reasons.append(f"marker missing: {marker}")
    for forbidden in expected.get("forbidden_markers", []):
        if str(forbidden) in text: reasons.append(f"forbidden marker present: {forbidden}")
    ordered = expected.get("ordered_markers", [])
    if ordered:
        cursor = -1
        for item in ordered:
            cursor = text.find(str(item), cursor + 1)
            if cursor < 0: reasons.append(f"ordered marker missing/out of order: {item}"); break
        actual["ordered_markers_matched"] = not any("ordered marker" in reason for reason in reasons)
    count_marker = expected.get("count_marker", marker)
    if any(key in expected for key in ("count_min", "count_max", "count_exact")):
        if count_marker is None: reasons.append("count rule requires count_marker or marker")
        else:
            count = text.count(str(count_marker)); actual["count"] = count
            if "count_min" in expected and count < int(expected["count_min"]): reasons.append(f"count {count} below {expected['count_min']}")
            if "count_max" in expected and count > int(expected["count_max"]): reasons.append(f"count {count} above {expected['count_max']}")
            if "count_exact" in expected and count != int(expected["count_exact"]): reasons.append(f"count {count} differs from {expected['count_exact']}")
    if expected.get("regex"):
        match = re.search(str(expected["regex"]), text, re.MULTILINE); actual["regex_matched"] = bool(match)
        if not match: reasons.append("regex did not match")
        elif "value_group" in expected:
            try:
                value = float(match.group(int(expected["value_group"]))); actual["value"] = value
                _check_bounds("value", value, expected, reasons)
            except (IndexError, ValueError) as exc: reasons.append(f"legacy numeric extraction failed: {exc}")
    measurements = []
    for rule in expected.get("measurements", []):
        measured, failures = _measurement(text, rule); measurements.append(measured); reasons.extend(failures)
    if measurements: actual["measurements"] = measurements
    if not EXECUTABLE_EXPECTATION_KEYS.intersection(expected):
        reasons.append("unsupported expectation: no executable rule")
    return not reasons, actual, reasons
