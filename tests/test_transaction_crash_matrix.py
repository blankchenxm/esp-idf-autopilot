from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.models import Receipt
from orchestrator.storage import ProjectStore, file_ref
from orchestrator.transactions import (
    SIDE_EFFECT_RECEIPT_OPERATIONS,
    authority_bound_key,
    idempotency_authority,
)


@pytest.mark.parametrize(
    "operation", sorted(SIDE_EFFECT_RECEIPT_OPERATIONS)
)
def test_crash_before_and_after_every_side_effect_is_resumable(
    tmp_path: Path, operation: str,
) -> None:
    project = tmp_path / "projects" / "probe"
    store = ProjectStore(project)
    store.ensure()
    key = authority_bound_key(
        run_id="run-1",
        design_digest="d" * 64,
        hardware_identity={"chip": "esp32", "usb_serial": "probe"},
        material_fingerprint="m" * 64,
        operation=operation,
        scope={"image_id": "image-1"},
    )

    # Crash before the external side effect/Receipt leaves nothing reusable.
    assert store.find_successful_receipt(operation, key) is None

    artifact_path = project / "logs" / f"{operation}.log"
    artifact_path.write_text("completed", encoding="utf-8")
    now = datetime.now(timezone.utc).isoformat()
    receipt = Receipt(
        receipt_id=store.new_id(operation),
        run_id="run-1",
        operation=operation,
        started_at=now,
        finished_at=now,
        success=True,
        inputs={
            "idempotency_key": key,
            "idempotency_authority": idempotency_authority(key),
        },
        outputs={"completed": True},
        artifacts=[file_ref(artifact_path, project, "text/plain")],
    )
    store.write_receipt(receipt, "crash-matrix")

    # Crash after side effect but before the graph checkpoint reuses the exact
    # immutable Receipt and its integrity-valid artifact.
    reused = store.find_successful_receipt(operation, key)
    assert reused is not None
    assert reused.receipt_id == receipt.receipt_id
    assert reused.inputs["idempotency_authority"]["operation"] == operation

    # Corruption invalidates only this operation's cached completion.
    artifact_path.write_text("corrupted", encoding="utf-8")
    assert store.find_successful_receipt(operation, key) is None


def test_observation_and_verdict_are_separate_receipt_operations() -> None:
    for scope in ("verification", "integration"):
        observation = f"{scope}_observe"
        evaluation = f"{scope}_evaluation"
        evidence = f"{scope}_evidence_commit"
        assert {
            observation, evaluation, evidence
        }.issubset(SIDE_EFFECT_RECEIPT_OPERATIONS)
        assert len({observation, evaluation, evidence}) == 3
