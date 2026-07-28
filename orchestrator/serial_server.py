from __future__ import annotations

"""Tagged entry point for an esp-serial MCP server owned by the Harness.

The tag is intentionally present in the Windows command line.  If an outer
agent process is interrupted, a later safe serial boundary can reap only the
orphan that belongs to the same execution run; it never guesses about a
user-launched serial utility.
"""

import argparse

from serial_mcp import mcp


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.parse_args()
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
