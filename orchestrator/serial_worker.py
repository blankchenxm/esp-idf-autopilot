from __future__ import annotations

"""Disposable process boundary for one esp-serial MCP call.

The parent adapter owns this worker's process tree and can therefore enforce a
wall-clock deadline even if an async stdio context blocks while being cancelled.
"""

import argparse
import asyncio
import json
from pathlib import Path

from .adapters.serial import SerialAdapter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--arguments-json", required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    call = SerialAdapter(Path(args.repo), None, args.run_id)._call(  # type: ignore[arg-type]
        args.name, json.loads(args.arguments_json), timeout_s=args.timeout
    )
    print(asyncio.run(call), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
