# Active-Skill switch record

The switch was completed from a writable repository session on 2026-07-21:

1. The old complete Skill remains immutable at `../v1-complete-pre-harness/SKILL.md`.
2. `.agents/skills/esp-idf-firmware/SKILL.md` now routes to the executable Harness.
3. The duplicate active `SKILL_claude.md` and `crumb-agent-framework.md` were removed.
4. The test suite verifies the active Skill and generic design compiler.
5. The temporary `harness_smoke` project is no longer a runtime or test dependency.

The switch changed instruction ownership only and did not restore the user-deleted Crumb tree.
