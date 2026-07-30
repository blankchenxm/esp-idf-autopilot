from orchestrator.resource_resolution import resolve_resource_requirements


def test_resolver_selects_compatible_unclaimed_candidates_without_protocol_rules():
    allocations, errors = resolve_resource_requirements({
        "resource_requirements": [
            {"id": "r1", "owner": "sensor", "kind": "serial_bus", "constraints": {"rate_hz": {"maximum": 400_000}}},
            {"id": "r2", "owner": "storage", "kind": "serial_bus", "constraints": {"rate_hz": {"minimum": 1_000_000}}},
        ],
        "resource_capabilities": [
            {"id": "bus-a", "kind": "serial_bus", "priority": 0, "attributes": {"rate_hz": 400_000}},
            {"id": "bus-b", "kind": "serial_bus", "priority": 1, "attributes": {"rate_hz": 8_000_000}},
        ],
    })
    assert errors == []
    assert [item["candidate_id"] for item in allocations] == ["bus-a", "bus-b"]


def test_resolver_reports_an_evidenced_candidate_gap():
    allocations, errors = resolve_resource_requirements({
        "resource_requirements": [{"owner": "codec", "kind": "link", "constraints": {"lanes": {"equals": 2}}}],
        "resource_capabilities": [{"id": "link0", "kind": "link", "attributes": {"lanes": 1}}],
    })
    assert allocations == []
    assert errors == ["codec has no compatible link resource candidate"]
