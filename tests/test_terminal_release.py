from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.models import Receipt, RunStateProjection
from orchestrator.storage import (
    ProjectStore,
    atomic_write_json,
    file_ref,
)
from orchestrator.transactions import (
    authority_bound_key,
    idempotency_authority,
)
from orchestrator.validators import validate_terminal


def _terminal_fixture(
    root: Path, *, mismatched_flash_hardware: bool = False,
) -> tuple[Path, RunStateProjection]:
    project = root / "projects" / "probe"
    store = ProjectStore(project)
    store.ensure()
    design_digest = "d" * 64
    hardware = {"chip": "esp32", "usb_serial": "board-1"}
    run_id = "run-1"
    binary = project / "release" / "app.bin"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"firmware")
    firmware = hashlib.sha256(binary.read_bytes()).hexdigest()
    logs = {}
    for name, content in {
        "build": "build ok",
        "flash": "flash ok",
        "observe": "PRODUCT_OK",
    }.items():
        path = project / "logs" / f"{name}.log"
        path.write_text(content, encoding="utf-8")
        logs[name] = path

    atomic_write_json(store.execution / "run.json", {
        "schema_version": "1.0",
        "run_id": run_id,
        "project": "probe",
        "intent": "new",
        "design_revision": 1,
        "design_digest": design_digest,
        "hardware_identity": hardware,
    })
    ids = {}
    now = datetime.now(timezone.utc).isoformat()
    for operation in (
        "release_fullclean",
        "release_configure",
        "release_build",
        "release_flash",
        "release_observe",
        "release_production_scenario",
    ):
        receipt_id = operation
        ids[operation] = receipt_id
        operation_hardware = (
            {"chip": "esp32", "usb_serial": "other-board"}
            if mismatched_flash_hardware and operation == "release_flash"
            else hardware
        )
        scope = {"attempt": 1}
        if operation in {
            "release_flash", "release_observe",
            "release_production_scenario",
        }:
            scope["firmware_sha256"] = firmware
        key = authority_bound_key(
            run_id=run_id,
            design_digest=design_digest,
            hardware_identity=operation_hardware,
            material_fingerprint="m" * 64,
            operation=operation,
            scope=scope,
        )
        artifacts = []
        if operation == "release_build":
            artifacts = [
                file_ref(logs["build"], project, "text/plain"),
                file_ref(binary, project, "application/octet-stream"),
            ]
        elif operation == "release_flash":
            artifacts = [file_ref(logs["flash"], project, "text/plain")]
        elif operation in {
            "release_observe", "release_production_scenario",
        }:
            artifacts = [file_ref(logs["observe"], project, "text/plain")]
        outputs = {}
        if operation == "release_build":
            outputs["firmware_sha256"] = firmware
        elif operation == "release_production_scenario":
            outputs.update({
                "firmware_sha256": firmware,
                "design_digest": design_digest,
            })
        store.write_receipt(Receipt(
            receipt_id=receipt_id,
            run_id=run_id,
            operation=operation,
            started_at=now,
            finished_at=now,
            success=True,
            inputs={
                "idempotency_key": key,
                "idempotency_authority": idempotency_authority(key),
            },
            outputs=outputs,
            artifacts=artifacts,
        ), "release")

    projection = RunStateProjection(
        run_id=run_id,
        mode="COMPLETE",
        cursor="STAGE 3.6:release:pass",
        next_action="terminal validation complete",
        progress_fingerprint="p" * 64,
        release_verified=True,
        closure={"required_total": 1, "pass": 1},
        release_evidence={
            "closure_pass": True,
            "selftest_disabled": True,
            "fullclean_receipt_id": ids["release_fullclean"],
            "configure_receipt_id": ids["release_configure"],
            "build_log": logs["build"].relative_to(project).as_posix(),
            "flash_log": logs["flash"].relative_to(project).as_posix(),
            "serial_log": logs["observe"].relative_to(project).as_posix(),
            "runtime_marker": "PRODUCT_OK",
            "firmware_sha256": firmware,
            "firmware_binary": binary.relative_to(project).as_posix(),
            "build_receipt_id": ids["release_build"],
            "flash_receipt_id": ids["release_flash"],
            "serial_receipt_id": ids["release_observe"],
            "production_scenario_receipt_ids": [
                ids["release_production_scenario"]
            ],
            "production_scenario_ids": ["normal-operation"],
        },
    )
    return project, projection


def test_generic_positive_release_reaches_complete_terminal(
    tmp_path: Path,
) -> None:
    project, projection = _terminal_fixture(tmp_path)
    assert validate_terminal(projection, project) == []


def test_release_cannot_combine_different_hardware_receipts(
    tmp_path: Path,
) -> None:
    project, projection = _terminal_fixture(
        tmp_path, mismatched_flash_hardware=True
    )
    assert any(
        "different hardware" in error
        for error in validate_terminal(projection, project)
    )
