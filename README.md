<div align="center">

# ESP-IDF Autopilot

### Automated ESP-IDF firmware development—from hardware brief to hardware-verified release.

[![ESP-IDF](https://img.shields.io/badge/framework-ESP--IDF-E7352C?logo=espressif&logoColor=white)](https://docs.espressif.com/projects/esp-idf/)
[![License](https://img.shields.io/badge/license-MIT-22C55E.svg)](LICENSE)

<a href="#why-esp-idf-autopilot">Why Autopilot?</a> ·
<a href="#automated-development-pipeline">Automation pipeline</a> ·
<a href="#quick-start">Quick start</a> ·
<a href="#what-you-provide">What you provide</a> ·
<a href="#architecture">Architecture</a>

</div>

---

## Automate the complete ESP-IDF firmware workflow

ESP-IDF gives ESP32 projects the control that real products need: native peripherals, FreeRTOS, DMA, memory, power, timing, and direct access to the platform SDK. But that control usually means a long manual workflow—reading APIs, finding components, interpreting datasheets, writing drivers, building, flashing, checking serial output, diagnosing failures, and repeating.

**ESP-IDF Autopilot turns that workflow into an automated development loop.**

Connect the board by serial. Describe the hardware, wiring, and product requirements. Autopilot then drives the remaining ESP-IDF lifecycle: it reads and structures the inputs, fetches relevant component/API/datasheet context, generates or updates firmware, builds, flashes, collects runtime data, evaluates the result, and automatically repairs and re-verifies the responsible code until the declared checks close.

The goal is simple: **you define what to build; Autopilot automates the engineering loop required to build and verify it.**

## Why ESP-IDF Autopilot?

Arduino is excellent for a fast start, but projects eventually need SDK-level control. At that point, LLM-based development faces two recurring issues:

- **SDK version mismatch** — a model may know an old API while the local ESP-IDF installation has changed.
- **Long-tail peripherals** — a device may not have a suitable component, leaving the developer to derive a driver from the datasheet and verify it on real hardware.

Autopilot is built around those realities.

| | Automated capability |
|---|---|
| **⚙️ ESP-IDF-native** | Works with ESP-IDF projects and local SDK headers instead of replacing the framework with a simplified abstraction. |
| **🔎 Context acquisition** | Queries the ESP Component Registry, official ESP-IDF sources, and datasheet artifacts before implementation decisions are made. |
| **🧩 Firmware generation** | Adopts compatible components or creates the missing project components and orchestration code. |
| **🔄 Closed-loop repair** | Builds, flashes, reads serial/runtime data, diagnoses failures, patches the responsible owner, and runs the affected checks again. |
| **📊 Automated verification** | Collects quantitative subsystem and integration evidence instead of stopping at “build succeeded.” |
| **💾 Durable runs** | Keeps each project’s automation state in SQLite so an interrupted terminal or agent session can resume the same run. |

## Automated development pipeline

The pipeline has two automation stages. The first turns your hardware brief into an executable plan; the second repeatedly turns that plan into working firmware on the connected board.

### 01 — Design automation

| Step | Autopilot automatically does | Output |
|---|---|---|
| **Read inputs** | Reads and hashes your requirements and connection files. | A stable project input set. |
| **Inventory hardware** | Identifies the MCU features, external ICs, buses, pins, dependencies, and constraints. | A complete hardware/software inventory. |
| **Fetch technical context** | Searches official ESP-IDF context and the Component Registry; acquires and extracts datasheet data when needed. | Grounded component and driver facts. |
| **Build the plan** | Defines components, FreeRTOS behavior, dependency order, verification checks, integration behavior, and release criteria. | An executable firmware plan. |
| **Validate and package** | Runs deterministic validation and produces a revisioned Design Package. | `spec.md` plus a machine-readable execution contract. |

At the end of this stage, you review one concise `spec.md` and approve the plan. This is the main human checkpoint; it prevents the automation from implementing the wrong product.

### 02 — Hardware automation

| Step | Autopilot automatically does | Loop behavior |
|---|---|---|
| **Prepare** | Checks the ESP-IDF environment, serial connection, and hardware session. | Re-probes before a retry when the transport changes. |
| **Implement** | Generates/updates ESP-IDF components and product orchestration, or adopts an approved component behind a project wrapper. | Works owner by owner in dependency order. |
| **Build & flash** | Builds with the local ESP-IDF installation and flashes the connected ESP32. | Reuses only integrity-valid successful transactions. |
| **Collect data** | Reads serial output and runtime measurements, and writes immutable receipts/evidence. | Binds measurements to the firmware hash and run. |
| **Evaluate & repair** | Compares results with declared expectations, classifies the failure, and patches the smallest responsible owner. | Rebuilds and re-runs the affected verification path. |
| **Integrate & release** | Tests the complete system, closes requirements, and performs a fresh release build/flash/runtime check. | Continues until terminal validation passes or reports an explicit blocker. |

```text
  Hardware brief + wiring
            │
            ▼
  [ AUTOMATED DESIGN ]
  read → inventory → fetch context → plan → validate
            │
            │  one human review: approve spec.md
            ▼
  [ AUTOMATED HARDWARE LOOP ]
  implement → build → flash → collect data → verify
                   ▲                         │
                   └──── repair & retry ─────┘
            │
            ▼
  ESP-IDF firmware + verification results + release artifacts
```

Most work is autonomous. Human participation is limited to the approved design review and rare, predeclared physical checks that software and serial data cannot observe by themselves.

## What you provide

For each project, Autopilot starts from two user-owned files:

```text
requirements/<project>.md   # desired behavior, constraints, and success criteria
connections/<project>.md    # board, exact parts, wiring, power, and known constraints
```

In practice, you provide:

1. **A connected ESP32 board** over USB/serial.
2. **A hardware description**: board model, peripheral part numbers, interfaces, pins, power assumptions, and safety limits.
3. **A product description**: the features to implement and the behavior or performance you expect.

You do not need to preselect a component, write an ESP-IDF driver, or manually operate the build/flash/serial loop first.

## Quick start

### 1. Prepare ESP-IDF once

Install ESP-IDF with the official installer or Espressif IDE Manager. Then create the ignored local activation file for your own installation:

```powershell
Copy-Item activate.local.ps1.example activate.local.ps1
# Edit activate.local.ps1 and enable the line matching your ESP-IDF installation.

powershell -ExecutionPolicy Bypass -File tools\idf.ps1 --version
pip install -r requirements.txt
```

The automation uses the local toolchain as its SDK authority. If activation fails, fix the local installation before starting a project.

### Portable local configuration

This repository intentionally does not contain a user's ESP-IDF install path,
Python executable, Codex home, COM port, or credentials. Configure these only
on your machine:

- `activate.local.ps1` is ignored by Git and should dot-source your own
  ESP-IDF `export.ps1` or EIM profile. The committed wrapper
  `tools/idf.ps1` then works from any fresh PowerShell session.
- `.codex/config.toml` resolves `python` from `PATH` (or an active virtual
  environment) and uses the repository working directory; do not replace it
  with an absolute Python or repository path. Keep personal MCP overrides in
  your user-level Codex configuration.
- VS Code discovers ESP-IDF from the extension setup or your activated shell.
  Select your local ESP-IDF installation in the extension UI; do not commit
  `.vscode` settings containing its absolute path.

Before running the Harness, verify the local environment with:

```powershell
powershell -ExecutionPolicy Bypass -File tools\idf.ps1 --version
python --version
codex --version
```

### 2. Add your project inputs

Create a matching pair, for example:

```text
requirements/my_device.md
connections/my_device.md
```

Existing examples are available under [`requirements/`](requirements/) and [`connections/`](connections/).

### 3. Launch the automation

```powershell
python -m orchestrator.cli design --project my_device
python -m orchestrator.cli status --project my_device
```

When the design status becomes `WAITING_SPEC`, review the generated specification:

```text
projects/my_device/design-package/rev-NNNN/spec.md
```

Approve it to start the fully automated implementation and hardware loop:

```powershell
python -m orchestrator.cli resume --project my_device --approve
```

### 4. Monitor or resume the same run

```powershell
python -m orchestrator.cli status --project my_device
python -m orchestrator.cli validate --project my_device
```

The worker is project-scoped and durable. It continues independently of the original terminal and can resume from the saved checkpoint rather than restarting the whole pipeline.

## Automation outputs

Every run produces reusable project artifacts—not just source code:

| Artifact | Purpose |
|---|---|
| `projects/<project>/design-package/rev-NNNN/spec.md` | The reviewable automated design plan. |
| `execution-contract.json` | The machine-readable plan for components, verification, integration, and release. |
| `execution/receipts/` | Build, flash, registry, source, and transport transaction records. |
| `execution/evidence/` | Captured verification results and measurements. |
| `execution/events.jsonl` | Append-only engineering activity history. |
| `runtime/projects/<project>/checkpoints.sqlite` | The durable cursor for the automation loop. |

## Architecture

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Your project                                                         │
│ requirements/<project>.md  +  connections/<project>.md              │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Design Harness — automated context and planning                       │
│ input parsing · hardware inventory · registry/API lookup · datasheets │
│ contract generation · deterministic validation                        │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  approved specification
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Execution Harness — automated firmware and verification loop          │
│ implement · build · flash · serial data · evaluate · repair · retry   │
│ integration · closure · release                                       │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                                ▼
                    ESP32 hardware + ESP-IDF firmware
```

| Area | Responsibility |
|---|---|
| `orchestrator/` | The durable graph, adapters, retry policy, validators, storage, and CLI. |
| `schemas/` | Versioned contracts for designs, receipts, evidence, and state projections. |
| `projects/<project>/` | Generated ESP-IDF source, design packages, execution data, and logs. |
| `runtime/projects/<project>/` | Isolated checkpoints, worker state, locks, and provider workspaces. |
| `hardware/datasheets/` | Content-addressed datasheet artifacts and project references. |
| `tools/idf.ps1` | A reliable local ESP-IDF activation wrapper. |
| `serial_mcp.py` | Persistent serial monitoring for Windows workflows. |

Read [Design Harness](docs/DESIGN-HARNESS.md), [Execution Harness](docs/EXECUTION-HARNESS.md), [Architecture](docs/ARCHITECTURE.md), and [Verification Evidence](docs/VERIFICATION-EVIDENCE.md) for the detailed contracts.

## Contributing

Contributions are welcome across automated design, ESP-IDF component work, hardware adapters, verification, workflow resilience, and documentation.

## License

Licensed under the [MIT License](LICENSE).

<div align="center">

**Describe the hardware. Start the automation. Ship ESP-IDF firmware.**

</div>
