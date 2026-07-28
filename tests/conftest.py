from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / ".vendor"
for path in (ROOT, VENDOR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

