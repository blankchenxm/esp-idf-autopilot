from __future__ import annotations

import json
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .schema_capabilities import validate_capability_matrix


REGISTRY_PATH = Path(__file__).resolve().parents[1] / "schemas" / "harness-invariants.json"
SCENARIOS_PATH = Path(__file__).resolve().parents[1] / "schemas" / "harness-scenarios.json"
REQUIRED_FIELDS = frozenset({
    "rule_id", "statement", "applicable_schema_versions", "authority_inputs",
    "producer_node", "validator", "required_receipt_kinds",
    "failure_disposition", "negative_test_ids", "downstream_gates",
})
STATE_GUARDED_EDGES = frozenset({
    ("implementation_completeness", "component_architecture"),
    ("evidence_commit", "implementation_materialize"),
    ("evidence_commit", "component_architecture"),
    ("evidence_commit", "tier_c_artifact_materialization"),
    ("tier_c", "verification_batches"),
})


@dataclass(frozen=True)
class GraphView:
    nodes: frozenset[str]
    edges: frozenset[tuple[str, str]]


def load_invariant_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_scenario_registry(path: Path = SCENARIOS_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _reachable(edges: set[tuple[str, str]], start: str, target: str) -> bool:
    pending = [start]
    seen: set[str] = set()
    while pending:
        node = pending.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        pending.extend(right for left, right in edges if left == node)
    return False


def _reachable_avoiding(
    edges: set[tuple[str, str]],
    start: str,
    target: str,
    forbidden: str,
) -> bool:
    return _reachable(
        {
            (left, right) for left, right in edges
            if left != forbidden and right != forbidden
        },
        start,
        target,
    )


def validate_invariant_registry(
    registry: dict[str, Any],
    *,
    graph: GraphView | None = None,
    validator_names: Iterable[str] = (),
    scenario_ids: Iterable[str] = (),
) -> list[str]:
    errors: list[str] = []
    rules = registry.get("rules")
    if registry.get("registry_schema_version") != "1.0" or not isinstance(rules, list):
        return ["invalid invariant registry header"]
    known_validators = set(validator_names)
    known_scenarios = set(scenario_ids)
    ids: set[str] = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(f"rule[{index}] is not an object")
            continue
        missing = sorted(REQUIRED_FIELDS - set(rule))
        if missing:
            errors.append(f"rule[{index}] lacks fields {missing}")
            continue
        rule_id = str(rule["rule_id"])
        if not rule_id or rule_id in ids:
            errors.append(f"invalid or duplicate rule_id {rule_id!r}")
        ids.add(rule_id)
        errors.extend(
            f"{rule_id}: {message}"
            for message in validate_capability_matrix(rule["applicable_schema_versions"])
        )
        for field in ("authority_inputs", "required_receipt_kinds", "negative_test_ids", "downstream_gates"):
            if not isinstance(rule[field], list) or not rule[field]:
                errors.append(f"{rule_id}: {field} must be non-empty")
        if known_validators and rule["validator"] not in known_validators:
            errors.append(f"{rule_id}: validator {rule['validator']!r} is not implemented")
        if known_scenarios:
            absent = sorted(set(rule["negative_test_ids"]) - known_scenarios)
            if absent:
                errors.append(f"{rule_id}: missing negative scenarios {absent}")
        if graph:
            producer = str(rule["producer_node"])
            if producer not in graph.nodes:
                errors.append(f"{rule_id}: producer node {producer!r} is absent")
            for downstream in rule["downstream_gates"]:
                if downstream not in graph.nodes:
                    errors.append(f"{rule_id}: downstream node {downstream!r} is absent")
                elif producer in graph.nodes and not _reachable(set(graph.edges), producer, downstream):
                    errors.append(f"{rule_id}: producer {producer!r} cannot reach protected gate {downstream!r}")
                elif (
                    rule.get("activation", "always") == "always"
                    and
                    "__start__" in graph.nodes
                    and producer in graph.nodes
                    and _reachable(set(graph.edges), "__start__", producer)
                    and _reachable_avoiding(
                        {
                            (left, right)
                            for left, right in graph.edges
                            if left != "recover" and right != "recover"
                            and (left, right) not in STATE_GUARDED_EDGES
                        },
                        "__start__",
                        downstream,
                        producer,
                    )
                ):
                    errors.append(
                        f"{rule_id}: protected gate {downstream!r} can "
                        f"bypass producer {producer!r}"
                    )
    return errors


def required_runtime_rule_ids(schema_version: str) -> list[str]:
    registry = load_invariant_registry()
    return sorted(
        str(rule["rule_id"])
        for rule in registry["rules"]
        if rule["applicable_schema_versions"].get(schema_version) == "required"
    )


def required_rule_ids_before(schema_version: str, gate: str) -> list[str]:
    return sorted(
        str(rule["rule_id"])
        for rule in load_invariant_registry()["rules"]
        if rule["applicable_schema_versions"].get(schema_version) == "required"
        and gate in rule["downstream_gates"]
    )


def validate_invariant_receipts(
    schema_version: str,
    passed_rule_ids: Iterable[str],
) -> list[str]:
    missing = sorted(set(required_runtime_rule_ids(schema_version)) - set(passed_rule_ids))
    return [] if not missing else [f"required invariant Receipts are missing for {missing}"]


TRANSACTION_CHAINS = (
    (
        "operation_authority", "bind", "verification_batches",
        "implementation_materialize", "implementation_completeness",
        "source_validate", "configure", "build",
        "flash", "observe", "evaluate", "evidence_commit",
        "component_architecture", "production_composition",
        "integration_prepare", "integration_configure", "integration_build",
        "integration_flash", "integration_observe", "integration_evaluate",
        "integration_evidence_commit",
        "tier_c_artifact_materialization", "tier_c", "closure",
        "release_prepare", "release_fullclean",
        "release_configure", "release_build", "release_flash",
        "release_observe", "release_validate",
    ),
)


def validate_transaction_topology(graph: GraphView) -> list[str]:
    errors: list[str] = []
    edges = set(graph.edges)
    forward_edges = {
        (left, right)
        for left, right in edges
        if left != "recover" and right != "recover"
    }
    for chain in TRANSACTION_CHAINS:
        for left, right in zip(chain, chain[1:]):
            if not _reachable(forward_edges, left, right):
                errors.append(f"transaction topology cannot reach {right!r} from {left!r}")
            if (right, left) in forward_edges:
                errors.append(f"transaction topology can replay ancestor {left!r} from {right!r}")
    side_effects = {
        "implementation_materialize", "implementation_completeness",
        "source_validate",
        "configure", "build", "flash", "observe", "evaluate", "evidence_commit",
        "component_architecture", "production_composition",
        "integration_prepare",
        "integration_configure", "integration_build", "integration_flash",
        "integration_observe", "integration_evaluate",
        "integration_evidence_commit",
        "tier_c_artifact_materialization",
        "release_prepare",
        "release_fullclean", "release_configure", "release_build",
        "release_flash", "release_observe", "release_validate",
    }
    for node in side_effects:
        if node not in graph.nodes:
            errors.append(f"side-effect node {node!r} is absent")
        if "recover" not in {
            right for left, right in edges if left == node
        }:
            errors.append(f"side-effect node {node!r} has no typed recovery edge")
    return errors


VALIDATOR_IMPORTS = {
    "validate_control_event_delivery":
        "orchestrator.control_events:validate_control_event_delivery",
    "validate_model_context":
        "orchestrator.model_context:validate_model_context",
    "require_executable_schema":
        "orchestrator.schema_capabilities:require_executable_schema",
    "compile_operation_authority":
        "orchestrator.operation_authority:compile_operation_authority",
    "validate_implementation_completeness":
        "orchestrator.operation_authority:validate_implementation_completeness",
    "validate_production_composition":
        "orchestrator.production_composition:validate_production_composition",
    "validate_tier_c_producer_chain":
        "orchestrator.tier_c_producer:validate_tier_c_producer_chain",
    "normalize_verification_images":
        "orchestrator.verification_plan:normalize_verification_images",
    "validate_recovery_admission":
        "orchestrator.failure_lineage:validate_recovery_admission",
    "validate_component_architecture":
        "orchestrator.component_architecture:validate_component_architecture",
    "validate_release_transaction":
        "orchestrator.validators:validate_release_transaction",
    "reconcile_worker_state":
        "orchestrator.execution_jobs:reconcile_worker_state",
    "validate_transaction_topology":
        "orchestrator.invariants:validate_transaction_topology",
    "validate_generated_file_hygiene":
        "orchestrator.project_hygiene:validate_generated_file_hygiene",
}
IMPLEMENTED_VALIDATORS = frozenset(VALIDATOR_IMPORTS)


def validate_validator_implementations() -> list[str]:
    errors: list[str] = []
    for identity, reference in VALIDATOR_IMPORTS.items():
        module_name, attribute = reference.split(":", 1)
        try:
            implementation = getattr(
                importlib.import_module(module_name), attribute
            )
        except (ImportError, AttributeError) as exc:
            errors.append(
                f"validator {identity!r} cannot resolve {reference!r}: {exc}"
            )
            continue
        if not callable(implementation):
            errors.append(
                f"validator {identity!r} resolves to a non-callable object"
            )
    return errors


def validate_graph_conformance(compiled_graph: Any) -> list[str]:
    raw = compiled_graph.get_graph()
    nodes = set(raw.nodes)
    edges = {(edge.source, edge.target) for edge in raw.edges}
    conceptual_edges = {
        ("control_event_delivery", "terminal"),
        ("model_context", "implementation_materialize"),
        ("status_reconcile", "resume"),
        ("transaction_graph", "closure"),
        ("release_validate", "terminal"),
        ("terminal", "__end__"),
        ("resume", "initialize"),
    }
    nodes.update({
        "control_event_delivery", "model_context", "status_reconcile",
        "transaction_graph", "terminal", "resume",
    })
    edges.update(conceptual_edges)
    graph = GraphView(frozenset(nodes), frozenset(edges))
    scenarios = load_scenario_registry()
    scenario_ids = {
        str(item["scenario_id"]) for item in scenarios["scenarios"]
    }
    errors = validate_invariant_registry(
        load_invariant_registry(),
        graph=graph,
        validator_names=IMPLEMENTED_VALIDATORS,
        scenario_ids=scenario_ids,
    )
    missing_guarded_edges = sorted(STATE_GUARDED_EDGES - edges)
    if missing_guarded_edges:
        errors.append(
            "declared state-guarded graph edges are absent: "
            f"{missing_guarded_edges}"
        )
    errors.extend(validate_transaction_topology(graph))
    errors.extend(validate_validator_implementations())
    return errors
