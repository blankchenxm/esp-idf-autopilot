from orchestrator.validators import (
    validate_contract,
    validate_spec_review_surface,
)
from orchestrator.design_graph import _render_review_spec
from orchestrator.design_inventory import reconcile_grounding_unknowns
from tests.test_design_contract import valid_contract


def test_pending_grounding_cannot_hide_outside_implementation_facts():
    contract = valid_contract()
    contract["schema_version"] = "1.3"
    contract["implementation_facts"] = []
    contract["product_decisions"] = []
    contract["integration"]["tests"][0]["duration"] = (
        "[GROUNDING_PENDING] choose later"
    )

    assert any(
        "integration.tests[0].duration" in error
        for error in validate_contract(contract)
    )


def test_spec_prose_does_not_control_review_readiness():
    errors = validate_spec_review_surface(
        "# Spec\n\nAudio parameters remain grounding-dependent before approval."
    )

    assert not errors


def test_harness_policy_and_execution_readiness_are_not_product_unknowns():
    contract = valid_contract()
    active, resolved = reconcile_grounding_unknowns(
        contract,
        [
            (
                "Wi-Fi credentials must be consumed only from an ignored local "
                "private build artifact and remain redacted from evidence."
            ),
            (
                "No immutable implementation facts bind the I2S clock mode, DMA "
                "sizing, or safe stop/recovery behavior."
            ),
            (
                "Audio sample rate, sample width, maximum recording duration, "
                "DMA/buffer bounds, and allowed loss threshold remain unset until "
                "receipt-bound implementation facts are available."
            ),
            "[USER_DECISION] choose the product retention period",
        ],
        [],
    )

    assert active == ["[USER_DECISION] choose the product retention period"]
    assert [item["resolution_kind"] for item in resolved] == [
        "harness_policy",
        "execution_readiness",
        "execution_readiness",
    ]


def test_review_renderer_removes_only_stale_provisional_language():
    resolved_statement = "No immutable implementation facts bind DMA sizing."
    spec = _render_review_spec(
        (
            "# Review\n\nThis must remain grounding-dependent before approval.\n"
            f"\n- {resolved_statement}\n\nKeep this scope."
        ),
        resolved_unknowns=[
            {
                "statement": resolved_statement,
                "resolution": "Execution readiness owns the named operation facts.",
            }
        ],
        resolved_limitations=[],
    )

    assert "remain grounding-dependent" not in spec
    assert resolved_statement not in spec
    assert "Keep this scope." in spec
    assert not validate_spec_review_surface(spec)
