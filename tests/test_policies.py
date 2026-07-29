from orchestrator.models import FailureCategory, FailureDisposition
from pathlib import Path
from orchestrator.policies import affected_consumers, classify_failure, disposition_for, failure_fingerprint, material_fingerprint, progress_fingerprint, recovery_budget, retry_allowed


def test_failure_signal_parity():
    samples = {"undefined reference": FailureCategory.LINK, "Access is denied opening serial port": FailureCategory.SERIAL, "task_wdt watchdog fired": FailureCategory.WATCHDOG, "queue full and dropped": FailureCategory.BACKPRESSURE, "TLS auth protocol error": FailureCategory.PROTOCOL, "latency deadline exceeded": FailureCategory.PERFORMANCE}
    for value, expected in samples.items(): assert classify_failure(value) == expected


def test_progress_fingerprint_changes_on_cursor():
    assert progress_fingerprint({"run_id": "r", "cursor": "a"}) != progress_fingerprint({"run_id": "r", "cursor": "b"})


def test_retry_budget_is_bounded():
    assert retry_allowed(FailureCategory.FLASH, 2) and not retry_allowed(FailureCategory.FLASH, 3)


def test_affected_set_includes_transitive_consumers():
    assert affected_consumers("sensor", {"sensor": [], "service": ["sensor"], "cloud": ["service"], "unrelated": []}) == {"sensor", "service", "cloud"}


def test_failure_fingerprint_ignores_volatile_numbers_but_changes_on_source(tmp_path: Path):
    project = tmp_path / "p"; source = project / "components" / "x" / "x.c"; source.parent.mkdir(parents=True); source.write_text("int x = 1;", encoding="utf-8")
    first = material_fingerprint(project, {"subsystem_index": 0}); a = failure_fingerprint("subsystem", FailureCategory.BUILD, "failed attempt 1 on COM4", first); b = failure_fingerprint("subsystem", FailureCategory.BUILD, "failed attempt 2 on COM9", first)
    assert a == b
    source.write_text("int x = 2;", encoding="utf-8")
    assert first != material_fingerprint(project, {"subsystem_index": 0})


def test_failure_fingerprint_ignores_disposable_agent_workspace_names():
    material = "same"
    first = failure_fingerprint(
        "subsystem", FailureCategory.TOOL,
        r"WinError 206 in runtime\\projects\\p\\agent-work\\crumb-button_input-a1b2c3d4\\project",
        material,
    )
    second = failure_fingerprint(
        "subsystem", FailureCategory.TOOL,
        r"WinError 206 in runtime\\projects\\p\\agent-work\\crumb-button_input-z9y8x7w6\\project",
        material,
    )
    assert first == second


def test_recovery_budgets_are_bounded():
    assert recovery_budget(FailureCategory.SERIAL) == 3 and recovery_budget(FailureCategory.LIMITATION) == 1


def test_failure_disposition_is_independent_from_summary_text():
    assert disposition_for(FailureCategory.BUILD) == FailureDisposition.REPAIR_INTERNAL
    assert disposition_for(FailureCategory.SERIAL) == FailureDisposition.RETRY_TRANSIENT
    assert disposition_for(FailureCategory.HARDWARE, retryable=False) == FailureDisposition.HARD_EXTERNAL_BLOCKER
    assert disposition_for(FailureCategory.LIMITATION) == FailureDisposition.HARD_EXTERNAL_BLOCKER


def test_idf_activation_failure_is_environment_not_unknown():
    assert classify_failure(
        "activate.local.ps1 is missing; ESP-IDF environment activation failed"
    ) == FailureCategory.ENVIRONMENT


def test_idf_activation_banner_does_not_mask_compiler_failure():
    output = """IDF PowerShell Environment
IDF_PATH: C:\\esp-idf
button_policy.c:174:11: error: called object 'gpio_config' is not a function
ninja: build stopped: subcommand failed.
"""
    assert classify_failure(output) == FailureCategory.BUILD
