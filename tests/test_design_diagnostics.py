from orchestrator.design_diagnostics import design_route


def test_repeated_external_acquisition_is_blocked_not_faulted():
    route, mode, phase = design_route([{
        "code": "DATASHEET_GROUNDING_FAILED", "severity": "BLOCKING",
        "disposition": "REPAIR_INTERNAL", "responsible_party": "design_grounding", "cause": "datasheet", "summary": "missing source",
        "affected_owner": "camera", "retry_scope": "grounding_item",
    }], repeated_material=True)
    assert (route, mode, phase) == ("blocked", "BLOCKED", "blocked")


def test_new_repairable_diagnostic_is_not_limited_by_attempt_count():
    route, mode, phase = design_route([{
        "code": "CONTRACT_VALIDATION_FAILED", "severity": "BLOCKING",
        "disposition": "REPAIR_INTERNAL", "responsible_party": "design_provider",
        "cause": "data_path", "summary": "new deterministic field error",
        "retry_scope": "contract_section",
    }], repeated_material=False)
    assert (route, mode, phase) == ("repair", "DESIGN_RUNNING", "repair")
