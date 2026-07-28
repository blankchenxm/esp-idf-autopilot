from __future__ import annotations


def integration_rows(contract: dict) -> list[dict]:
    return [row for row in contract["verification"] if row.get("owner") == "integration"]

