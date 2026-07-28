from __future__ import annotations

import json
from pathlib import Path

from ..validators import design_digest, validate_contract


def materialize_validation(design_dir: Path) -> dict:
    contract = json.loads((design_dir / "execution-contract.json").read_text(encoding="utf-8"))
    manifest = json.loads((design_dir / "manifest.json").read_text(encoding="utf-8"))
    errors = validate_contract(contract)
    calculated = design_digest(contract, manifest)
    return {"schema_version": "1.0", "valid": not errors, "errors": errors, "warnings": [], "design_digest": calculated}

