from __future__ import annotations

import hashlib
import json
import copy
import string
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jsonschema

from .models import RunStateProjection, RunMode
from .storage import canonical_bytes
from .evaluation import EXECUTABLE_EXPECTATION_KEYS


REQUIRED_DESIGN_FILES = (
    "spec.md",
    "execution-contract.json",
    "manifest.json",
    "design-validation.json",
    "approval.json",
)

REQUIRED_CONTRACT_KEYS = {
    "schema_version", "project", "requirements", "subsystems", "component_selections",
    "verification", "architecture", "workflow", "tier_c", "limitations", "datasheets", "integration", "release",
}
MANDATORY_STAGES = [
    "preflight", "design", "approval", "bind", "subsystems", "tier_c",
    "integration", "closure", "release",
]
MANDATORY_STAGES_V12 = [
    "preflight", "design", "approval", "bind", "subsystems", "integration",
    "tier_c", "closure", "release",
]
_UNRESOLVED_APPROVAL_PHRASES = (
    "[grounding_pending]",
    "before approval",
    "remain grounding-dependent",
    "remains grounding-dependent",
)


def _unresolved_approval_paths(
    value: Any, path: str = "$"
) -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            errors.extend(
                _unresolved_approval_paths(item, f"{path}.{key}")
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(
                _unresolved_approval_paths(item, f"{path}[{index}]")
            )
    elif isinstance(value, str):
        folded = value.casefold()
        if any(token in folded for token in _UNRESOLVED_APPROVAL_PHRASES):
            errors.append(
                f"{path} contains unresolved approval-time grounding language"
            )
    return errors


def validate_spec_review_surface(spec: str) -> list[str]:
    # spec.md is a rendering, never a routing input.  Typed unknowns in the
    # contract/unknown artifact decide approval state; prose may describe a
    # historical or deferred fact without changing the state machine.
    return []


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _schema_errors(instance: dict[str, Any], schema_name: str) -> list[str]:
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / schema_name
    schema = _load(schema_path)
    artifact_schema = _load(schema_path.parent / "artifact-ref.schema.json")
    def localize(value: Any) -> Any:
        if isinstance(value, dict) and value.get("$ref") == "artifact-ref.schema.json":
            return copy.deepcopy(artifact_schema)
        if isinstance(value, dict):
            return {key: localize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [localize(item) for item in value]
        return value
    validator = jsonschema.Draft202012Validator(localize(schema))
    return [f"{schema_name} at {'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}" for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path))]


def topological_subsystems(subsystems: list[dict[str, Any]]) -> list[str]:
    ids = {item["id"] for item in subsystems}
    dependencies = {item["id"]: set(item.get("dependencies", [])) for item in subsystems}
    unknown = {dep for deps in dependencies.values() for dep in deps if dep not in ids}
    if unknown:
        raise ValueError(f"unknown subsystem dependencies: {sorted(unknown)}")
    order: list[str] = []
    while dependencies:
        ready = sorted(node for node, deps in dependencies.items() if not deps)
        if not ready:
            raise ValueError("subsystem dependency graph contains a cycle")
        order.extend(ready)
        for node in ready:
            dependencies.pop(node)
        for deps in dependencies.values():
            deps.difference_update(ready)
    return order


def validate_i2c_transport_bindings(
    contract: dict[str, Any], input_authority: dict[str, Any] | None = None,
) -> list[str]:
    """Require input-bound I2C controller and timing before implementation."""
    if contract.get("schema_version") != "1.5":
        return []
    owners = [
        str(item.get("id") or "")
        for item in contract.get("subsystems", [])
        if isinstance(item, dict)
        and item.get("classification") == "external_part"
        and any(str(resource).casefold() == "i2c" for resource in item.get("hardware_resources", []))
    ]
    if not owners:
        return []
    bindings = contract.get("board_transport_bindings", [])
    if not isinstance(bindings, list):
        return ["board_transport_bindings must be an array"]
    authorized = {
        (item.get("controller"), item.get("clock_hz"))
        for item in (input_authority or {}).get("i2c_configs", [])
        if isinstance(item, dict)
    }
    errors: list[str] = []
    for owner in owners:
        matches = [item for item in bindings if isinstance(item, dict) and item.get("owner") == owner and str(item.get("bus") or "").casefold() == "i2c"]
        if len(matches) != 1:
            errors.append(f"{owner} requires exactly one input-bound I2C transport binding with controller and clock_hz")
            continue
        controller, clock_hz = matches[0].get("controller"), matches[0].get("clock_hz")
        if type(controller) is not int or controller not in {0, 1} or type(clock_hz) is not int or clock_hz <= 0:
            errors.append(f"{owner} I2C transport binding requires controller 0 or 1 and positive clock_hz")
        elif input_authority is not None and (controller, clock_hz) not in authorized:
            errors.append(f"{owner} I2C transport binding is not present in input authority")
    return errors


def validate_contract(
    contract: dict[str, Any], input_authority: dict[str, Any] | None = None,
) -> list[str]:
    # The Design graph calls this validator before choosing either repair or
    # promotion.  Keep the JSON Schema check here as well as in
    # ``validate_design_package`` so a schema-only defect is routed back to
    # the provider instead of becoming an internal fault during promotion.
    errors: list[str] = _schema_errors(contract, "execution-contract.schema.json")
    missing = sorted(REQUIRED_CONTRACT_KEYS - contract.keys())
    if missing:
        errors.append(f"missing contract keys: {missing}")
        return errors
    if "design_inventory" in contract or "grounding_plan" in contract:
        from .design_inventory import validate_grounding_plan_coverage
        errors.extend(validate_grounding_plan_coverage(contract))
    try:
        subsystem_ids = set(topological_subsystems(contract["subsystems"]))
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(str(exc))
        subsystem_ids = set()
    subsystem_roles = {item.get("id"): item.get("execution_role", "component") for item in contract["subsystems"] if isinstance(item, dict)}
    integration_ids = {item_id for item_id, role in subsystem_roles.items() if role == "integration"}
    if len(integration_ids) > 1:
        errors.append("contract may declare at most one integration subsystem")
    strict_contract = contract.get("schema_version") in {"1.1", "1.2", "1.3", "1.4", "1.5"}
    # Schema 1.5 extends 1.3; it must retain the 1.2+ execution ordering
    # (integration before Tier C), not fall back to the legacy 1.0 order.
    strict_v12 = contract.get("schema_version") in {"1.2", "1.3", "1.4", "1.5"}
    strict_v13 = contract.get("schema_version") in {"1.3", "1.4", "1.5"}
    strict_v15 = contract.get("schema_version") == "1.5"
    errors.extend(validate_i2c_transport_bindings(contract, input_authority))
    if strict_v13:
        errors.extend(_unresolved_approval_paths(contract))
    if strict_contract:
        for item in contract["subsystems"]:
            if "execution_role" not in item:
                errors.append(f"subsystem {item.get('id')!r} lacks required execution_role")
    if strict_v12:
        component_order = [
            item_id for item_id in topological_subsystems(contract["subsystems"])
            if subsystem_roles.get(item_id) == "component"
        ] if subsystem_ids else []
        definitions = {item["id"]: item for item in contract["subsystems"]}
        positions: dict[str, list[int]] = {}
        for index, owner in enumerate(component_order):
            item = definitions[owner]
            for key in ("verification_batch", "isolation_required", "hardware_resources"):
                if key not in item:
                    errors.append(f"subsystem {owner!r} lacks required {key}")
            batch = item.get("verification_batch")
            if isinstance(batch, str) and batch:
                positions.setdefault(batch, []).append(index)
            if item.get("isolation_required") and not item.get("isolation_reason"):
                errors.append(f"isolated subsystem {owner!r} requires isolation_reason")
        for batch, indexes in positions.items():
            if indexes != list(range(min(indexes), max(indexes) + 1)):
                errors.append(f"verification batch {batch!r} is not contiguous in dependency order")
            members = [component_order[index] for index in indexes]
            isolated = [owner for owner in members if definitions[owner].get("isolation_required")]
            if isolated and len(members) != 1:
                errors.append(f"verification batch {batch!r} mixes isolated subsystem(s) {isolated}")
    if strict_v13:
        definitions = {item["id"]: item for item in contract["subsystems"]}
        fact_ids: set[str] = set()
        provider_ids = {
            str(item.get("provider_receipt_id"))
            for collection in (contract.get("component_selections", []), contract.get("datasheets", []))
            for item in collection if isinstance(item, dict) and item.get("provider_receipt_id")
        }
        facts_by_owner: dict[str, list[dict[str, Any]]] = {}
        for fact in contract.get("implementation_facts", []):
            fact_id = str(fact.get("id") or "")
            owner = str(fact.get("subsystem_id") or "")
            if not fact_id or fact_id in fact_ids:
                errors.append(f"invalid or duplicate implementation fact id: {fact_id!r}")
            fact_ids.add(fact_id)
            if owner not in definitions:
                errors.append(f"implementation fact {fact_id!r} has unknown subsystem {owner!r}")
            else:
                facts_by_owner.setdefault(owner, []).append(fact)
            if fact.get("provider_receipt_id") not in provider_ids:
                errors.append(f"implementation fact {fact_id!r} is not bound to a grounding provider receipt")
            if "[GROUNDING_PENDING]" in str(fact.get("value") or "") or str(fact.get("value") or "").strip() == "GROUNDING_PENDING":
                errors.append(f"implementation fact {fact_id!r} remains unresolved [GROUNDING_PENDING]")
            assertions = fact.get("source_assertions")
            if not isinstance(assertions, list) or not assertions:
                errors.append(f"implementation fact {fact_id!r} lacks source assertions")
            if fact.get("source_kind") == "datasheet":
                locator = fact.get("evidence_locator")
                if not isinstance(locator, dict) or not locator.get("extracted_text_sha256") or not locator.get("quote"):
                    errors.append(f"implementation fact {fact_id!r} lacks receipt-text evidence locator")
        decision_ids: set[str] = set()
        for decision in contract.get("product_decisions", []):
            decision_id = str(decision.get("id") or "")
            if not decision_id or decision_id in decision_ids:
                errors.append(f"invalid or duplicate product decision id: {decision_id!r}")
            decision_ids.add(decision_id)
        for owner, item in definitions.items():
            if item.get("execution_role") == "integration":
                if item.get("responsibility_layer") != "integration":
                    errors.append(f"integration subsystem {owner!r} requires responsibility_layer=integration")
                continue
            if not item.get("responsibility_layer"):
                errors.append(f"subsystem {owner!r} lacks responsibility_layer")
            if item.get("isolation_required") and item.get("batch_compatible"):
                errors.append(f"isolated subsystem {owner!r} cannot be batch_compatible")
    requirement_ids: set[str] = set()
    for req in contract["requirements"]:
        req_id, owner = req.get("id"), req.get("owner")
        if not req_id or req_id in requirement_ids:
            errors.append(f"invalid or duplicate requirement id: {req_id!r}")
        requirement_ids.add(req_id)
        if owner not in subsystem_ids:
            errors.append(f"requirement {req_id} has unknown owner {owner!r}")
    verification_ids = {item.get("requirement_id") for item in contract["verification"]}
    for req_id in sorted(requirement_ids - verification_ids):
        errors.append(f"requirement {req_id} has no verification row")
    for row in contract["verification"]:
        if row.get("tier") in {"A", "B"} and row.get("user_involvement", False):
            errors.append(f"{row.get('test_id')} illegally requires user involvement for Tier {row.get('tier')}")
        if not row.get("expected") or not row.get("evidence_required"):
            errors.append(f"{row.get('test_id')} lacks predeclared expected/evidence")
        if row.get("expected") and not EXECUTABLE_EXPECTATION_KEYS.intersection(row["expected"]):
            errors.append(f"{row.get('test_id')} expected uses no executable verification rule")
        setup = row.get("test_setup") or {}
        stimulus = row.get("stimulus") or {}
        if strict_v15 and not setup:
            errors.append(f"{row.get('test_id')} lacks executable test_setup")
        if strict_v15 and not stimulus:
            errors.append(f"{row.get('test_id')} lacks executable stimulus")
        if setup.get("kind") == "firmware_selftest":
            overrides = setup.get("kconfig_overrides")
            if not isinstance(overrides, dict) or not overrides:
                errors.append(f"{row.get('test_id')} firmware_selftest requires kconfig_overrides")
            if setup.get("isolated_build") is not True:
                errors.append(f"{row.get('test_id')} firmware_selftest requires isolated_build=true")
            if stimulus.get("kind") != "firmware_simulation":
                errors.append(f"{row.get('test_id')} firmware_selftest requires firmware_simulation stimulus")
        if setup.get("kind") == "automated_fixture" and stimulus.get("kind") != "fixture":
            errors.append(f"{row.get('test_id')} automated_fixture requires fixture stimulus")
        if row.get("tier") in {"A", "B"} and stimulus.get("kind") == "fixture" and setup.get("kind") != "automated_fixture":
            errors.append(f"{row.get('test_id')} fixture stimulus requires automated_fixture setup")
        coverage = row.get("evidence_contract")
        if strict_contract and coverage is None:
            errors.append(f"{row.get('test_id')} lacks required evidence_contract")
        if coverage is not None:
            kinds = set(coverage.get("required_kinds", [])) if isinstance(coverage, dict) else set()
            if row.get("tier") in {"A", "B"} and "user_confirmation" in kinds:
                errors.append(f"{row.get('test_id')} cannot require user_confirmation for Tier {row.get('tier')}")
            # Component and integration nodes currently bind build, flash,
            # bounded serial, firmware hash and hardware identity.  They do
            # not materialize arbitrary external artifacts or protocol
            # receipts.  Requiring either must therefore be a declared Tier C
            # artifact flow, not an unverifiable A/B promise.
            unsupported_auto = kinds.intersection({"artifact", "protocol_receipt"})
            if row.get("tier") in {"A", "B"} and unsupported_auto:
                errors.append(f"{row.get('test_id')} A/B evidence_contract requests unsupported runtime kinds: {sorted(unsupported_auto)}")
            if row.get("tier") == "C" and "user_confirmation" not in kinds:
                errors.append(f"{row.get('test_id')} Tier C evidence_contract requires user_confirmation")
    if strict_v15:
        batch_setups: dict[str, set[str]] = {}
        owner_batches = {
            str(item.get("id")): str(item.get("verification_batch") or item.get("id"))
            for item in contract.get("subsystems", [])
            if item.get("execution_role", "component") == "component"
        }
        for row in contract["verification"]:
            if row.get("tier") not in {"A", "B"}:
                continue
            batch = owner_batches.get(str(row.get("owner")))
            if batch:
                batch_setups.setdefault(batch, set()).add(
                    json.dumps(row.get("test_setup") or {}, sort_keys=True)
                )
        for batch, setups in batch_setups.items():
            if len(setups) > 1:
                errors.append(
                    f"verification batch {batch!r} mixes test_setup values; split the batch"
                )
    tier_c_ids = {item.get("requirement_id") for item in contract.get("tier_c", [])}
    for row in contract["verification"]:
        if row.get("tier") == "C" and row.get("requirement_id") not in tier_c_ids:
            errors.append(f"Tier C requirement {row.get('requirement_id')} has no batched confirmation item")
    for item in contract.get("tier_c", []):
        if not item.get("id") or not item.get("requirement_id") or not item.get("instructions"):
            errors.append("every Tier C item requires id, requirement_id, and instructions")
        if item.get("requirement_id") not in requirement_ids:
            errors.append(f"Tier C item {item.get('id')} names an unknown requirement")
        if item.get("owner") and item.get("owner") not in subsystem_ids:
            errors.append(f"Tier C item {item.get('id')} has unknown owner {item.get('owner')!r}")
        if strict_contract:
            for key in ("test_id", "owner", "expected", "evidence_contract"):
                if not item.get(key):
                    errors.append(f"Tier C item {item.get('id')} lacks required {key}")
        if item.get("evidence_contract") is not None:
            kinds = set(item["evidence_contract"].get("required_kinds", []))
            if "user_confirmation" not in kinds:
                errors.append(f"Tier C item {item.get('id')} requires user_confirmation evidence")
        if strict_v12:
            for key in ("why_not_tier_a_b", "automated_checks_completed", "physical_property", "artifact_contract", "retry_owners"):
                if not item.get(key):
                    errors.append(f"Tier C item {item.get('id')} lacks required {key}")
            artifact = item.get("artifact_contract") or {}
            access = artifact.get("access_method")
            source = artifact.get("source")
            if access in {"local_file", "download_url"} and not source:
                errors.append(f"Tier C item {item.get('id')} {access} requires an artifact source")
            rendered_source: str | None = None
            if isinstance(source, str):
                try:
                    fields = {
                        field_name
                        for _, field_name, _, _ in string.Formatter().parse(source)
                        if field_name is not None
                    }
                except ValueError:
                    fields = {"<invalid>"}
                unsupported = fields - {"run_id", "item_id"}
                if unsupported:
                    errors.append(
                        f"Tier C item {item.get('id')} artifact source has unsupported template fields "
                        f"{sorted(unsupported)}"
                    )
                else:
                    rendered_source = source.format(run_id="run", item_id="item")
            if access == "download_url" and rendered_source is not None:
                parsed = urlparse(rendered_source)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    errors.append(f"Tier C item {item.get('id')} download_url source must be an absolute HTTP(S) URL")
            if access == "local_file" and rendered_source is not None:
                normalized = rendered_source.replace("\\", "/")
                path = Path(normalized)
                if path.is_absolute() or ":" in normalized.split("/", 1)[0] or ".." in path.parts:
                    errors.append(f"Tier C item {item.get('id')} local_file source must stay inside the project")
                if len(path.parts) < 2 or not path.name or path.name in {".", ".."}:
                    errors.append(f"Tier C item {item.get('id')} local_file source must be an actionable relative file path")
            if access != "physical_observation" and "artifact" not in set((item.get("evidence_contract") or {}).get("required_kinds", [])):
                errors.append(f"Tier C item {item.get('id')} must require artifact evidence")
            unknown_retry = set(item.get("retry_owners", [])) - subsystem_ids
            if unknown_retry:
                errors.append(f"Tier C item {item.get('id')} has unknown retry owners {sorted(unknown_retry)}")
    selections = {item.get("subsystem_id"): item for item in contract["component_selections"]}
    for subsystem in contract["subsystems"]:
        subsystem_id = subsystem["id"]
        classification = subsystem.get("classification")
        registry_required = subsystem.get("registry_search_required")
        if classification == "mcu_native" and registry_required:
            errors.append(f"{subsystem_id} is mcu_native and must use local IDF grounding, not Registry")
        if classification == "project_custom" and registry_required:
            errors.append(f"{subsystem_id} is project_custom and must not query Registry")
        if classification in {"external_part", "reusable_software"} and not registry_required:
            errors.append(f"{subsystem_id} is {classification} and requires Registry candidate grounding")
        if classification == "project_custom" and subsystem_id in selections:
            errors.append(f"{subsystem_id} is project_custom and must not declare a component selection")
        if subsystem.get("registry_search_required"):
            selected = selections.get(subsystem_id)
            if not selected or not selected.get("exact_search") or not selected.get("capability_search"):
                errors.append(f"{subsystem_id} lacks mandatory Registry exact/capability search")
            if selected and selected.get("status") == "outage":
                errors.append(f"{subsystem_id} Registry search is an outage, not a valid zero-result")
            if selected and not selected.get("decision_reason"):
                errors.append(f"{subsystem_id} lacks adopt/reject reason")
            if selected and not selected.get("provider_receipt_id"):
                errors.append(f"{subsystem_id} Registry selection lacks a raw provider receipt")
            if selected and selected.get("decision") == "registry" and not selected.get("selected_component"):
                errors.append(f"{subsystem_id} Registry adoption lacks selected_component namespace/name")
            if selected and "grounding_plan" in contract:
                evidence = selected.get("selection_evidence")
                if not isinstance(evidence, dict) or evidence.get("policy") != "post-registry-evidence-v1":
                    errors.append(
                        f"{subsystem_id} lacks a post-Registry final selection decision"
                    )
                required_operations = set(subsystem.get("required_operations", []))
                covered_operations = set(selected.get("covered_operations", []))
                uncovered_operations = set(selected.get("uncovered_operations", []))
                if required_operations != covered_operations | uncovered_operations:
                    errors.append(
                        f"{subsystem_id} component selection does not partition required_operations"
                    )
                if covered_operations & uncovered_operations:
                    errors.append(
                        f"{subsystem_id} component selection operation coverage overlaps"
                    )
            candidates = set((selected or {}).get("registry_candidates", []))
            if selected and candidates:
                details = {
                    str(item.get("component"))
                    for item in selected.get("registry_candidate_details", [])
                    if isinstance(item, dict) and item.get("component")
                }
                detail_errors = {
                    str(item.get("component"))
                    for item in selected.get("registry_candidate_detail_errors", [])
                    if isinstance(item, dict) and item.get("component")
                }
                missing_detail_outcomes = sorted(candidates - details - detail_errors)
                if missing_detail_outcomes:
                    errors.append(
                        f"{subsystem_id} Registry candidates lack implementation details "
                        f"or explicit detail failures: {missing_detail_outcomes}"
                    )
                adopted = (
                    str(selected.get("selected_component"))
                    if selected.get("decision") == "registry"
                    else ""
                )
                if adopted and adopted not in candidates:
                    errors.append(
                        f"{subsystem_id} adopted Registry component was not "
                        f"discovered by the exact/capability searches: {adopted}"
                    )
                rejected = {
                    str(item.get("component")): str(item.get("reason") or "")
                    for item in selected.get("rejected_candidates", [])
                    if isinstance(item, dict)
                }
                missing_rejections = sorted(
                    candidate
                    for candidate in candidates - ({adopted} if adopted else set())
                    if not rejected.get(candidate)
                )
                if missing_rejections:
                    errors.append(
                        f"{subsystem_id} Registry candidates lack explicit rejection reasons: "
                        f"{missing_rejections}"
                    )
        if subsystem.get("classification") == "external_part":
            sheet = next((item for item in contract.get("datasheets", []) if item.get("subsystem_id") == subsystem["id"]), None)
            identity_invalid = (
                "grounding_plan" in contract
                and (not sheet or sheet.get("identity_verified") is not True)
            )
            coverage = set((sheet or {}).get("coverage", []))
            l1_coverage_invalid = (
                "grounding_plan" in contract
                and not {"identity", "interface", "electrical"}.issubset(coverage)
            )
            if not sheet or sheet.get("level") not in {"L1", "L2", "L3"} or sheet.get("technical_content_valid") is not True or identity_invalid or l1_coverage_invalid:
                errors.append(f"{subsystem['id']} lacks validated Datasheet L1 grounding")
            if sheet and not sheet.get("provider_receipt_id"):
                errors.append(f"{subsystem['id']} datasheet grounding lacks a raw provider receipt")
    stages = contract["workflow"].get("stages", [])
    cursor = -1
    for stage in (MANDATORY_STAGES_V12 if strict_v12 else MANDATORY_STAGES):
        try:
            cursor = stages.index(stage, cursor + 1)
        except ValueError:
            errors.append(f"mandatory workflow stage missing or out of order: {stage}")
            break
    architecture = contract["architecture"]
    for key in ("tasks", "queues", "resource_ownership", "failure_recovery"):
        if key not in architecture:
            errors.append(f"architecture.{key} is missing")
    release = contract.get("release", {})
    for key in ("runtime_marker", "selftest_disabled", "selftest_config"):
        if not release.get(key): errors.append(f"release.{key} is missing")
    # A revision is an approval-ready implementation contract, not merely a
    # record of open acquisition work.  In particular, allowing a
    # ``blocking_grounding`` limitation through here lets a reviewer approve
    # values which the contract itself says must not be guessed.  That failure
    # surfaces much later as an avoidable build/serial failure.  Keep such a
    # draft in design grounding until the fact is receipt-bound (or, for a
    # genuine product choice, represented as a user decision instead).
    for index, limitation in enumerate(contract.get("limitations", [])):
        status = str(limitation.get("status") or "").casefold()
        statement = str(limitation.get("statement") or "")
        if status == "blocking_grounding":
            identifier = limitation.get("id", index)
            errors.append(
                f"limitation {identifier!r} is blocking_grounding and prevents approval"
            )
        if strict_v13 and (
            ("grounding" in status and status != "resolved_grounding")
            or (
                "[grounding_pending]" in statement.casefold()
                and status != "resolved_grounding"
            )
        ):
            identifier = limitation.get("id", index)
            errors.append(
                f"limitation {identifier!r} leaves grounding unresolved; use receipt-bound implementation_facts before approval"
            )
    integration_tests = contract.get("integration", {}).get("tests", [])
    for test in integration_tests:
        expected = test.get("expected") or test.get("metrics", {})
        if not EXECUTABLE_EXPECTATION_KEYS.intersection(expected): errors.append(f"integration test {test.get('id')} lacks an executable expected rule")
        if not test.get("requirement_ids"): errors.append(f"integration test {test.get('id')} lacks requirement_ids")
        if strict_contract and not isinstance(test.get("timeout_s"), (int, float)):
            errors.append(f"integration test {test.get('id')} lacks required timeout_s")
    for row in contract.get("verification", []):
        if (
            row.get("owner") not in (integration_ids | {"integration"})
            or row.get("tier") not in {"A", "B"}
        ):
            continue
        explicit = row.get("integration_test_id")
        candidates = [
            test
            for test in integration_tests
            if (
                str(test.get("test_id") or test.get("id")) == str(explicit)
                if explicit
                else row.get("requirement_id") in set(test.get("requirement_ids") or [])
            )
        ]
        if len(candidates) != 1:
            errors.append(
                f"integration verification {row.get('test_id')} must map to "
                "exactly one integration test"
            )
    return errors


def design_digest(contract: dict[str, Any], manifest: dict[str, Any]) -> str:
    material = {
        "contract": contract,
        "inputs": manifest.get("inputs", []),
        "providers": manifest.get("providers", []),
        "files": manifest.get("files", []),
    }
    return hashlib.sha256(canonical_bytes(material)).hexdigest()


def validate_design_package(design_dir: Path, require_approval: bool = True) -> tuple[dict[str, Any], list[str]]:
    errors = [f"missing design file: {name}" for name in REQUIRED_DESIGN_FILES if not (design_dir / name).is_file()]
    if errors:
        return {}, errors
    contract, manifest, approval = _load(design_dir / "execution-contract.json"), _load(design_dir / "manifest.json"), _load(design_dir / "approval.json")
    validation = _load(design_dir / "design-validation.json")
    errors.extend(_schema_errors(contract, "execution-contract.schema.json"))
    errors.extend(_schema_errors(manifest, "manifest.schema.json"))
    errors.extend(_schema_errors(approval, "approval.schema.json"))
    errors.extend(_schema_errors(validation, "design-validation.schema.json"))
    project_id = str(contract.get("project", ""))
    for label, value in (
        ("manifest", manifest),
        ("approval", approval),
        ("design-validation", validation),
    ):
        declared_project = value.get("project_id")
        if declared_project is not None and declared_project != project_id:
            errors.append(f"{label} project_id does not match execution contract project")
    errors.extend(validate_contract(contract))
    repo_root = design_dir.resolve().parents[3]
    for section, base in (("inputs", repo_root), ("files", design_dir)):
        for reference in manifest.get(section, []):
            path = (base / reference.get("path", "")).resolve()
            try: path.relative_to(base.resolve())
            except ValueError:
                errors.append(f"manifest {section} path escapes authority root: {path}"); continue
            if not path.is_file(): errors.append(f"manifest {section} file missing: {path}"); continue
            current = hashlib.sha256(path.read_bytes()).hexdigest()
            if current != reference.get("sha256") or path.stat().st_size != reference.get("size"):
                errors.append(f"manifest {section} hash/size mismatch: {path}")
    expected = design_digest(contract, manifest)
    provider_ids: set[str] = set()
    for provider in manifest.get("providers", []):
        reference = provider.get("receipt_ref")
        if not reference: continue
        provider_ids.add(str(provider.get("receipt_id")))
        path = (repo_root / reference.get("path", "")).resolve()
        try: path.relative_to(repo_root)
        except ValueError: errors.append(f"provider receipt path escapes repository: {path}"); continue
        if not path.is_file(): errors.append(f"provider receipt is missing: {path}"); continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != reference.get("sha256") or path.stat().st_size != reference.get("size"):
            errors.append(f"provider receipt hash/size mismatch: {path}"); continue
        receipt = _load(path)
        errors.extend(_schema_errors(receipt, "receipt.schema.json"))
        if receipt.get("project_id") is not None and receipt.get("project_id") != project_id:
            errors.append(f"provider receipt project_id does not match design project: {path}")
        if receipt.get("receipt_id") != provider.get("receipt_id") or receipt.get("success") is not True:
            errors.append(f"provider receipt is unsuccessful or has the wrong ID: {path}")
        expected_operation = {"registry": "registry_search_pair", "datasheet": "datasheet_artifact_inspect"}.get(provider.get("kind"))
        if expected_operation and receipt.get("operation") != expected_operation:
            errors.append(f"provider receipt operation does not match kind {provider.get('kind')}: {path}")
        if provider.get("subsystem_id") and receipt.get("inputs", {}).get("subsystem_id") != provider.get("subsystem_id"):
            errors.append(f"provider receipt subsystem does not match manifest: {path}")
    contract_provider_ids = {str(item.get("provider_receipt_id")) for item in contract.get("component_selections", []) + contract.get("datasheets", []) if item.get("provider_receipt_id")}
    if not contract_provider_ids.issubset(provider_ids):
        errors.append("contract references provider receipts not bound by manifest")
    if manifest.get("design_digest") != expected:
        errors.append("manifest design_digest does not match canonical contract/input/provider digest")
    if require_approval:
        if approval.get("status") != "APPROVED":
            errors.append("design is not approved")
        if approval.get("design_digest") != expected:
            errors.append("approval is not bound to the current design digest")
        if approval.get("spec_revision") != manifest.get("revision"):
            errors.append("approval revision does not match manifest revision")
    if validation.get("design_digest") != expected:
        errors.append("design-validation is not bound to the current design digest")
    if validation.get("valid") is not True or validation.get("errors"):
        errors.append("design-validation does not report a clean deterministic pass")
    return contract, errors


def validate_terminal(projection: RunStateProjection, project_dir: Path) -> list[str]:
    errors: list[str] = []
    if projection.project_id is not None and projection.project_id != project_dir.resolve().name:
        errors.append("run-state project_id does not match project directory")
    if projection.mode != RunMode.COMPLETE:
        return ["mode is not COMPLETE"]
    if projection.blocker is not None:
        errors.append("COMPLETE state cannot retain a blocker")
    if not projection.release_verified or projection.cursor != "STAGE 3.6:release:pass":
        errors.append("release terminal cursor/flag is invalid")
    if not projection.closure or projection.closure.passed != projection.closure.required_total or any((projection.closure.partial, projection.closure.fail, projection.closure.blocked)):
        errors.append("closure is not all PASS")
    if not projection.release_evidence or not projection.release_evidence.selftest_disabled:
        errors.append("release evidence/selftest-off assertion missing")
    elif any(not (project_dir / getattr(projection.release_evidence, name)).is_file() for name in ("build_log", "flash_log", "serial_log")):
        errors.append("one or more release raw logs are missing")
    else:
        release = projection.release_evidence
        receipt_ids = {release.build_receipt_id, release.flash_receipt_id, release.serial_receipt_id}
        receipts = {}
        for path in (project_dir / "execution" / "receipts").rglob("*.json"):
            value = _load(path)
            if value.get("project_id") is not None and value.get("project_id") != project_dir.resolve().name:
                errors.append(f"release receipt project_id mismatch: {path}")
                continue
            if value.get("receipt_id") in receipt_ids: receipts[value["receipt_id"]] = value
        if set(receipts) != receipt_ids or any(not item.get("success") for item in receipts.values()):
            errors.append("release receipt IDs are missing or unsuccessful")
        expected_paths = {release.build_log, release.flash_log, release.serial_log}
        artifact_paths = {artifact.get("path") for item in receipts.values() for artifact in item.get("artifacts", [])}
        if not expected_paths.issubset(artifact_paths): errors.append("release log paths are not bound to release receipts")
        serial_text = (project_dir / release.serial_log).read_text(encoding="utf-8", errors="replace")
        if release.runtime_marker not in serial_text: errors.append("release marker is absent from serial artifact")
        binary = project_dir / release.firmware_binary
        if not binary.is_file():
            errors.append("release application binary is missing")
        elif hashlib.sha256(binary.read_bytes()).hexdigest() != release.firmware_sha256:
            errors.append("release firmware hash does not match its bound application binary")
    run_path = project_dir / "execution" / "run.json"
    if run_path.is_file():
        run = _load(run_path)
        design_dir = project_dir / "design-package" / f"rev-{int(run['design_revision']):04d}"
        contract_path = design_dir / "execution-contract.json"
        if contract_path.is_file():
            from .corrections import invalidated_evidence_ids
            invalidated = invalidated_evidence_ids(project_dir)
            covered: set[str] = set()
            tier_c_pass: set[str] = set()
            passed_tests: set[str] = set()
            for path in (project_dir / "execution" / "evidence").rglob("*.json"):
                value = _load(path)
                if value.get("project_id") is not None and value.get("project_id") != project_dir.resolve().name:
                    errors.append(f"evidence project_id mismatch: {path}")
                    continue
                if value.get("evidence_id") in invalidated:
                    continue
                if value.get("run_id") == projection.run_id and value.get("design_digest") == run.get("design_digest") and value.get("verdict") == "PASS":
                    covered.update(value.get("requirement_ids", []))
                    passed_tests.add(str(value.get("test_id")))
                    if value.get("tier") == "C":
                        tier_c_pass.add(str(value.get("test_id")))
            contract = _load(contract_path)
            missing = {item["id"] for item in contract.get("requirements", [])} - covered
            if missing:
                errors.append(f"terminal evidence closure is missing requirements: {sorted(missing)}")
            required_tests = {
                str(item.get("test_id"))
                for item in contract.get("verification", [])
                if item.get("tier") in {"A", "B"}
            }
            required_tests.update(
                str(item.get("test_id") or item.get("id"))
                for item in contract.get("integration", {}).get("tests", [])
            )
            missing_tests = required_tests - passed_tests
            if missing_tests:
                errors.append(
                    "terminal evidence closure is missing declared tests: "
                    f"{sorted(missing_tests)}"
                )
            missing_tier_c = {item.get("test_id", item["id"]) for item in contract.get("tier_c", [])} - tier_c_pass
            if missing_tier_c:
                errors.append(f"terminal evidence closure is missing Tier C tests: {sorted(missing_tier_c)}")
    return errors
