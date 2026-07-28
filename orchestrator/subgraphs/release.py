from __future__ import annotations


def release_marker(contract: dict) -> str:
    marker = contract.get("release", {}).get("runtime_marker")
    if not marker:
        raise ValueError("execution contract must declare release.runtime_marker")
    return marker

