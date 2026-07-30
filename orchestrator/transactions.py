from __future__ import annotations

from typing import Any, Mapping

from .storage import digest


SIDE_EFFECT_RECEIPT_OPERATIONS = frozenset({
    "project_hygiene",
    "graph_conformance",
    "schema_gate",
    "operation_authority",
    "hardware_refresh",
    "verification_plan",
    "implementation_materialize",
    "implementation_completeness",
    "source_validation",
    "verification_configure",
    "verification_build",
    "verification_flash",
    "verification_observe",
    "verification_evaluation",
    "verification_evidence_commit",
    "component_architecture",
    "production_composition",
    "integration_prepare",
    "integration_configure",
    "integration_build",
    "integration_flash",
    "integration_observe",
    "integration_evaluation",
    "integration_evidence_commit",
    "tier_c_artifact_materialize",
    "tier_c_producer",
    "release_prepare",
    "release_fullclean",
    "release_configure",
    "release_build",
    "release_flash",
    "release_observe",
    "release_production_scenario",
    "control_event_report",
})


class AuthorityBoundIdempotencyKey(str):
    """Opaque key carrying the canonical authority used to derive it.

    The string value remains compatible with persisted Receipt lookup while
    adapters can also serialize the exact, inspectable authority tuple.
    """

    authority: dict[str, Any]

    def __new__(
        cls, authority: Mapping[str, Any],
    ) -> "AuthorityBoundIdempotencyKey":
        canonical = dict(authority)
        value = str.__new__(cls, digest(canonical))
        value.authority = canonical
        return value


def authority_bound_key(
    *,
    run_id: str,
    design_digest: str | None,
    hardware_identity: Mapping[str, Any] | None,
    material_fingerprint: str,
    operation: str,
    scope: Mapping[str, Any],
) -> AuthorityBoundIdempotencyKey:
    return AuthorityBoundIdempotencyKey({
        "authority_schema_version": "1.0",
        "run_id": run_id,
        "design_digest": design_digest,
        "hardware_identity": dict(hardware_identity or {}),
        "material_fingerprint": material_fingerprint,
        "operation": operation,
        "scope": dict(scope),
    })


def idempotency_authority(
    key: str | None,
) -> dict[str, Any] | None:
    authority = getattr(key, "authority", None)
    return dict(authority) if isinstance(authority, dict) else None
