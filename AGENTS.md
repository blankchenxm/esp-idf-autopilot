# ESP-IDF AutoDev Agent Guide

This repository is a closed-loop ESP-IDF development harness. A firmware run compiles
user inputs into an approved design contract, executes build/flash/serial verification
against real ESP32 hardware, and closes every required R/DR row before release.

## Global invariants

- ESP-IDF only. Never introduce Arduino abstractions.
- Never run bare `idf.py`; use `powershell -ExecutionPolicy Bypass -File tools\idf.ps1 ...`.
- Before the first build, run `powershell -ExecutionPolicy Bypass -File tools\idf.ps1 --version`.
- If `activate.local.ps1` is missing or broken, stop with exact evidence. Never guess an IDF path.
- `requirements/<project>.md` and `connections/<project>.md` are user-owned and read-only unless
  the user explicitly asks to edit them.
- Preserve unrelated working-tree changes. Do not patch generated `managed_components/`.
- Build success is not completion. Hardware work needs declared expected values and raw evidence.
- Keep hardware/register/transport details in project components; keep `main/` as product
  orchestration and state-machine code.
- Tier A/B are autonomous. Only the approved spec and one predeclared Tier C batch are human gates.
- User-owned requirements may contain plaintext device credentials when the user explicitly
  authorizes it. Consume them only through ignored local private build artifacts; redact them from
  model inputs, contracts, receipts, evidence, logs, source files, and responses.

## Authority and routing

| Work | Read first | Authority |
|---|---|---|
| Run firmware development | `.agents/skills/esp-idf-firmware/SKILL.md` | Harness CLI + approved contract |
| Change orchestration | `orchestrator/AGENTS.md`, `docs/EXECUTION-HARNESS.md` | Graph, validators, tests |
| Change project/component code | `projects/AGENTS.md`, `docs/ARCHITECTURE.md` | Project contract + source tests |
| Change wrappers/hardware tools | `tools/AGENTS.md`, `docs/HARDWARE-SAFETY.md` | Adapter contract + tests |
| Change design package | `docs/DESIGN-HARNESS.md` | JSON Schema + deterministic validator |
| Change verification/evidence | `docs/VERIFICATION-EVIDENCE.md` | Evidence schema + closure validator |

Nested `AGENTS.md` files are not assumed to load when Codex starts at the repo root. Read the
routed file explicitly before editing that area.

## Canonical state

- `runtime/projects/<project>/checkpoints.sqlite`: isolated control cursor and resumable thread state.
- Approved `design-package/<revision>/`: immutable design authority.
- `execution/receipts/`, `execution/evidence/`, `execution/events.jsonl`: runtime fact authority.
- `execution/run-state.json`: orchestrator-generated compatibility projection for the Stop hook.
- `DEVLOG.md`: generated human audit view, never a machine gate.

No agent or node may hand-edit a checkpoint, receipt, evidence verdict, approval digest, or
`run-state.json`. Use the orchestrator storage API.

## Standard commands

```powershell
python -m orchestrator.cli validate --project <project>
python -m orchestrator.cli start --project <project>
python -m orchestrator.cli resume --project <project> --approve
python -m orchestrator.cli status --project <project>
python -m orchestrator.cli snapshot-project --project <project> --output <outside-repo-path>
python -m orchestrator.cli reset-project --project <project> --snapshot <outside-repo-path>
python -m orchestrator.cli restore-project --project <project> --snapshot <outside-repo-path>
powershell -ExecutionPolicy Bypass -File tools\idf.ps1 --version
powershell -ExecutionPolicy Bypass -File tools\idf.ps1 -C projects\<project> build
```

## Done criteria

A run is complete only when the terminal validator proves: approved design digest bound; every
required R/DR PASS; no conflicting limitation; release selftest effectively disabled; release
build, flash, and fresh runtime evidence bound to the same firmware hash and existing artifacts.
