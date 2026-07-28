from orchestrator.design_diagnostics import design_route


def test_exhausted_external_acquisition_is_blocked_not_faulted():
    route, mode, phase = design_route([{
        "code": "DATASHEET_GROUNDING_FAILED", "severity": "BLOCKING",
        "disposition": "REPAIR_INTERNAL", "responsible_party": "design_grounding", "cause": "datasheet", "summary": "missing source",
        "affected_owner": "camera", "retry_scope": "grounding_item",
    }], attempt=2, max_attempts=2, repair_counts={"design_grounding:grounding_item:camera:DATASHEET_GROUNDING_FAILED": 2})
    assert (route, mode, phase) == ("blocked", "BLOCKED", "blocked")
