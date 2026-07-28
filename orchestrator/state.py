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
    verification_batches: list[list[str]]
    release_smoke_complete: bool
    tier_c_items: list[dict[str, Any]]
    tier_c_artifacts: dict[str, dict[str, Any]]
    tier_c_artifact_receipts: dict[str, str]
    tier_c_pending_ids: list[str]
    tier_c_rework: bool
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
