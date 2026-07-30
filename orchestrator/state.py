from __future__ import annotations

from typing import Any, TypedDict


class HarnessState(TypedDict, total=False):
    project: str
    project_dir: str
    run_id: str
    intent: str
    design_revision: int
    design_dir: str
    design_digest: str
    target: str
    port: str
    baud: int
    preflight_baseline: str
    hardware_identity: dict[str, Any]
    phase: str
    mode: str
    cursor: str
    next_action: str
    pause_next_node: str | None
    pause_reason: str | None
    progress_seq: int
    subsystem_index: int
    subsystems: list[str]
    batch_index: int
    verification_setup_index: int
    verification_batches: list[list[str]]
    verification_images: list[dict[str, Any]]
    verification_image_index: int
    transaction: dict[str, Any]
    completed_transaction_scope: str | None
    all_implementations_materialized: bool
    invariant_passes: list[str]
    recovery_ledger: dict[str, dict[str, Any]]
    failure_lineage_id: str | None
    failure_material_revision: str | None
    release_smoke_complete: bool
    tier_c_items: list[dict[str, Any]]
    tier_c_artifacts: dict[str, dict[str, Any]]
    tier_c_artifact_receipts: dict[str, str]
    tier_c_pending_ids: list[str]
    tier_c_rework: bool
    integration_firmware_sha256: str
    receipt_ids: list[str]
    evidence_ids: list[str]
    failure: dict[str, Any] | None
    diagnostic: dict[str, Any] | None
    failed_node: str | None
    recovery_target: str | None
    failure_attempts: dict[str, int]
    material_fingerprint: str | None
    last_failure_fingerprint: str | None
    blocker: dict[str, Any]
    closure: dict[str, Any]
    release_evidence: dict[str, Any]
    release_verified: bool
    release_attempt: int
