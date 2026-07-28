# Archived firmware Skill baselines

These files are immutable migration evidence, not active Codex Skills and not runtime authority.
They live outside `.agents/skills/` to prevent duplicate activation.

| Baseline | SHA-256 | Role |
|---|---|---|
| `v0-claude-original/SKILL_claude.md` | `22785D62DF26744278158053C3E37B11D1BEEEF52BE03574F6E61EBE7049EE77` | oldest Claude workflow |
| `v1-complete-pre-harness/SKILL.md` | `095119AE956679936BE90250BB6AFD4E11A9BB7EE4534097BC895A839BFCA732` | complete pre-Harness migration baseline |
| `v2-harness-candidate/SKILL.md` | `9BC1DDA4A5946FBE91EB1DA41CE1AE9334928663845031D8FD98B432DCE8DC88` | detailed v2 migration candidate retained as archival evidence |

Each bundle includes the supporting references used at archival time. The F01-F37 parity inventory
in `docs/archive/REFACTOR-DISCUSSION-2026-07-21.md` records the normative migration inventory. The writable-session switch is
complete. The active Skill is a compact 176-line rendering of the same contract; the older active
duplicates were removed after the v0/v1 baselines were verified. `tests/test_skill_parity.py`
guards the active Harness entry point.
