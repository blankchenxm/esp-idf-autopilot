from pathlib import Path

import pytest

from orchestrator.secrets import assert_no_secret_values, redact_text, secret_values, write_crumb_credentials


def test_authorized_requirement_secret_is_redacted_for_models():
    text = "wifi ssid: private-network\nwifi password: private-value\napi_key: second-value\n"
    values = secret_values(text); redacted = redact_text(text)
    assert values == ["private-network", "private-value", "second-value"]
    assert all(value not in redacted for value in values)
    assert redacted.count("[REDACTED: user-owned local secret]") == 3


def test_private_header_is_generated_without_printing_or_source_copy(tmp_path: Path):
    requirements = tmp_path / "crumb.md"; output = tmp_path / "private" / "crumb_credentials.h"
    requirements.write_text("wifi ssid: lab\nwifi password: private-value\n", encoding="utf-8")
    write_crumb_credentials(requirements, output)
    value = output.read_text(encoding="utf-8")
    assert "CRUMB_WIFI_SSID" in value and "private-value" in value


def test_secret_leak_guard_rejects_generated_design_or_source():
    with pytest.raises(ValueError, match="plaintext secret"):
        assert_no_secret_values("generated private-value", ["private-value"], "artifact")


def test_wifi_ssid_leak_guard_rejects_generated_design_or_source():
    requirements = "wifi ssid: private-network\nwifi password: private-value\n"
    with pytest.raises(ValueError, match="plaintext secret"):
        assert_no_secret_values(
            "forbidden marker private-network",
            secret_values(requirements),
            "generated design",
        )
