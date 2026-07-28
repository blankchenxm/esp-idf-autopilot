from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stop_continue import evaluate


class StopContinueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "projects").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_state(self, project: str, state: dict) -> Path:
        project_dir = self.root / "projects" / project
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / "run-state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        return path

    def base_state(self, mode: str) -> dict:
        return {
            "schema_version": 1,
            "project": "demo",
            "mode": mode,
            "cursor": "STAGE 2:button:implement",
            "next_action": "Implement the button component.",
            "release_verified": False,
            "closure": {
                "required_total": 1,
                "pass": 0,
                "partial": 0,
                "fail": 0,
                "blocked": 0,
            },
            "release_evidence": {},
        }

    def test_no_state_does_not_affect_normal_chat(self) -> None:
        self.assertEqual(evaluate(self.root, {}), {})

    def test_continuous_blocks_stop(self) -> None:
        self.write_state("demo", self.base_state("CONTINUOUS"))
        result = evaluate(self.root, {})
        self.assertEqual(result["decision"], "block")
        self.assertIn("Implement the button component", result["reason"])

    def test_repeated_stop_requests_diagnosis_and_continuation(self) -> None:
        self.write_state("demo", self.base_state("CONTINUOUS"))
        result = evaluate(self.root, {"stop_hook_active": True})
        self.assertEqual(result["decision"], "block")
        self.assertIn("prior continuation", result["reason"])

    def test_waiting_spec_allows_stop(self) -> None:
        self.write_state("demo", self.base_state("WAITING_SPEC"))
        self.assertEqual(evaluate(self.root, {}), {})

    def test_faulted_is_a_known_terminal_mode(self) -> None:
        self.write_state("demo", self.base_state("FAULTED"))
        self.assertEqual(evaluate(self.root, {}), {})

    def test_incomplete_complete_state_blocks_stop(self) -> None:
        self.write_state("demo", self.base_state("COMPLETE"))
        result = evaluate(self.root, {})
        self.assertEqual(result["decision"], "block")
        self.assertIn("claims COMPLETE", result["reason"])

    def test_malformed_state_blocks_stop(self) -> None:
        project_dir = self.root / "projects" / "demo"
        project_dir.mkdir()
        (project_dir / "run-state.json").write_text("{broken", encoding="utf-8")
        result = evaluate(self.root, {})
        self.assertEqual(result["decision"], "block")
        self.assertIn("malformed", result["reason"])

    def test_blocked_requires_complete_evidence(self) -> None:
        self.write_state("demo", self.base_state("BLOCKED"))
        result = evaluate(self.root, {})
        self.assertEqual(result["decision"], "block")
        self.assertIn("claims BLOCKED", result["reason"])

    def test_fully_evidenced_blocker_allows_stop(self) -> None:
        state = self.base_state("BLOCKED")
        state["blocker"] = {
            "kind": "hardware_preflight",
            "summary": "No serial device is enumerated.",
            "evidence": "logs/preflight.log: port discovery failed",
            "needed": "Restore the board USB connection before a new run.",
        }
        self.write_state("demo", state)
        self.assertEqual(evaluate(self.root, {}), {})

    def test_complete_with_evidence_allows_stop(self) -> None:
        state = self.base_state("COMPLETE")
        state.update(
            {
                "cursor": "STAGE 3.6:release:pass",
                "next_action": "",
                "release_verified": True,
                "closure": {
                    "required_total": 1,
                    "pass": 1,
                    "partial": 0,
                    "fail": 0,
                    "blocked": 0,
                },
                "release_evidence": {
                    "closure_pass": True,
                    "selftest_disabled": True,
                    "build_log": "logs/build.log",
                    "flash_log": "logs/flash.log",
                    "serial_log": "logs/serial.log",
                    "runtime_marker": "APP_READY",
                },
            }
        )
        project_dir = self.root / "projects" / "demo"
        (project_dir / "logs").mkdir(parents=True)
        for name in ("build.log", "flash.log", "serial.log"):
            (project_dir / "logs" / name).write_text("PASS", encoding="utf-8")
        self.write_state("demo", state)
        self.assertEqual(evaluate(self.root, {}), {})

    def test_multiple_continuous_states_block_stop(self) -> None:
        self.write_state("one", self.base_state("CONTINUOUS"))
        self.write_state("two", self.base_state("CONTINUOUS"))
        result = evaluate(self.root, {})
        self.assertEqual(result["decision"], "block")
        self.assertIn("more than one", result["reason"])


if __name__ == "__main__":
    unittest.main()
