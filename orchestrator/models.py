from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunMode(StrEnum):
    WAITING_SPEC = "WAITING_SPEC"
    CONTINUOUS = "CONTINUOUS"
    WAITING_TIER_C = "WAITING_TIER_C"
    PAUSED = "PAUSED"
    INTERRUPTED = "INTERRUPTED"
    BLOCKED = "BLOCKED"
    FAULTED = "FAULTED"
    COMPLETE = "COMPLETE"


class Verdict(StrEnum):
    PASS = "PASS"
    PASS_PENDING_TIER_C = "PASS_PENDING_TIER_C"
    REPAIR = "REPAIR"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class Tier(StrEnum):
    A = "A"
    B = "B"
    C = "C"


class FailureCategory(StrEnum):
    ENVIRONMENT = "environment"
    TOOL = "tool"
    REGISTRY = "registry"
    DATASHEET = "datasheet"
    BUILD = "build"
    FLASH = "flash"
    SERIAL = "serial"
    HARDWARE = "hardware"
    API = "api"
    LINK = "link"
    DATA_PATH = "data_path"
    STATE_MACHINE = "state_machine"
    WATCHDOG = "watchdog"
    BACKPRESSURE = "backpressure"
    SYNCHRONIZATION = "synchronization"
    STORAGE = "storage"
    PROTOCOL = "protocol"
    PERFORMANCE = "performance"
    INTEGRATION = "integration"
    LIMITATION = "limitation"
    STALL = "stall"
    UNKNOWN = "unknown"


class FailureDisposition(StrEnum):
    """What the orchestrator must do about a diagnosed technical cause."""

    RETRY_TRANSIENT = "RETRY_TRANSIENT"
    REPAIR_INTERNAL = "REPAIR_INTERNAL"
    WAITING_HUMAN = "WAITING_HUMAN"
    HARD_EXTERNAL_BLOCKER = "HARD_EXTERNAL_BLOCKER"
    INTERNAL_FAULT = "INTERNAL_FAULT"


class DiagnosticSeverity(StrEnum):
    BLOCKING = "BLOCKING"
    REVIEW = "REVIEW"
    ADVISORY = "ADVISORY"


class ArtifactRef(StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    media_type: str = "application/octet-stream"


class HardwareIdentity(StrictModel):
    chip: str
    revision: str | None = None
    mac: str | None = None
    usb_serial: str | None = None
    board_profile: str | None = None

    def stable_key(self) -> str:
        return "|".join(str(value or "") for value in (self.chip, self.mac, self.usb_serial, self.board_profile))


class HardwareSession(StrictModel):
    identity: HardwareIdentity
    port: str
    baud: int = Field(default=115200, gt=0)
    probed_at: str = Field(default_factory=utc_now)


class Failure(StrictModel):
    category: FailureCategory
    summary: str
    owner: str | None = None
    retryable: bool = True
    attempt: int = Field(default=1, ge=1)
    evidence: list[ArtifactRef] = Field(default_factory=list)


class Diagnostic(StrictModel):
    """Typed routing fact; summaries are presentation only, never policy input."""

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    cause: FailureCategory
    disposition: FailureDisposition
    severity: DiagnosticSeverity = DiagnosticSeverity.BLOCKING
    responsible_party: str
    affected_owner: str | None = None
    subsystem_id: str | None = None
    invariant_id: str | None = None
    operation_id: str | None = None
    test_id: str | None = None
    image_id: str | None = None
    summary: str
    evidence: list[ArtifactRef] = Field(default_factory=list)
    retry_scope: str | None = None
    user_action_required: str | None = None
    material_fingerprint: str | None = None
    failure_fingerprint: str | None = None
    invalidated_descendants: list[str] = Field(default_factory=list)
    model_call_admitted: bool = False


class Receipt(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    project_id: str | None = None
    receipt_id: str
    run_id: str
    operation: str
    started_at: str
    finished_at: str
    success: bool
    command: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    failure: Failure | None = None

    @model_validator(mode="after")
    def failure_matches_success(self) -> "Receipt":
        if self.success and self.failure is not None:
            raise ValueError("successful receipt cannot contain failure")
        if not self.success and self.failure is None:
            raise ValueError("failed receipt requires failure")
        return self


class Evidence(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    project_id: str | None = None
    evidence_id: str
    run_id: str
    design_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    requirement_ids: list[str] = Field(min_length=1)
    test_id: str
    owner: str
    tier: Tier
    expected: dict[str, Any]
    actual: dict[str, Any]
    verdict: Verdict
    receipt_ids: list[str] = Field(min_length=1)
    evidence_kinds: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    firmware_sha256: str | None = None
    hardware_identity_key: str | None = None
    created_at: str = Field(default_factory=utc_now)


class GateResult(StrictModel):
    gate: str
    passed: bool
    verdict: Verdict
    reasons: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    next_node: str | None = None


class NodeResult(StrictModel):
    node: str
    success: bool
    next_node: str | None = None
    updates: dict[str, Any] = Field(default_factory=dict)
    receipt_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    failure: Failure | None = None


class Blocker(StrictModel):
    kind: str
    summary: str
    evidence: str
    needed: str


class Closure(StrictModel):
    required_total: int = Field(ge=0)
    passed: int = Field(alias="pass", ge=0)
    partial: int = Field(default=0, ge=0)
    fail: int = Field(default=0, ge=0)
    blocked: int = Field(default=0, ge=0)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ReleaseEvidence(StrictModel):
    closure_pass: bool
    selftest_disabled: bool
    fullclean_receipt_id: str | None = None
    configure_receipt_id: str | None = None
    build_log: str
    flash_log: str
    serial_log: str
    runtime_marker: str
    firmware_sha256: str
    firmware_binary: str
    build_receipt_id: str
    flash_receipt_id: str
    serial_receipt_id: str
    production_scenario_receipt_ids: list[str] = Field(default_factory=list)
    production_scenario_ids: list[str] = Field(default_factory=list)


class RunRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    project: str
    intent: Literal["new", "resume", "revision"]
    created_at: str = Field(default_factory=utc_now)
    design_revision: int
    design_digest: str
    hardware_identity: HardwareIdentity | None = None


class RunStateProjection(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    project_id: str | None = None
    run_id: str
    mode: RunMode
    cursor: str
    next_action: str
    progress_seq: int = Field(default=0, ge=0)
    progress_fingerprint: str
    release_verified: bool = False
    closure: Closure | None = None
    release_evidence: ReleaseEvidence | None = None
    blocker: Blocker | None = None


class ExecutionEvent(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    project_id: str | None = None
    event_id: str
    run_id: str
    event_type: str
    node: str
    timestamp: str = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)
