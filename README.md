<div align="center">
  <img src="./figs/esp-idf-autopilot.png" alt="ESP-IDF Autopilot" width="250" />

  <h1>ESP-IDF Autopilot</h1>

  <p><strong>Connect your hardware. Describe what it should do. Star the project. Autopilot builds the ESP-IDF firmware.</strong></p>

  <p>
    <a href="https://github.com/blankchenxm/esp-idf-autopilot/stargazers">⭐ Star ESP-IDF Autopilot</a> ·
    <a href="#why-esp-idf-autopilot">Why Autopilot?</a> ·
    <a href="#quick-start">Quick start</a> ·
    <a href="#revision-100-validation-in-progress">Validation</a>
  </p>

  [![ESP-IDF](https://img.shields.io/badge/framework-ESP--IDF-E7352C?logo=espressif&logoColor=white)](https://docs.espressif.com/projects/esp-idf/)
  [![License](https://img.shields.io/badge/license-MIT-22C55E.svg)](LICENSE)
</div>

---

## What does Autopilot do?

Autopilot helps you build your ESP-IDF project in three simple steps:

1. **Connect your hardware** — a breadboard prototype or your own ESP32 PCB, over USB/serial.
2. **Write down the features you want** — the parts, wiring, behavior, and success criteria.
3. **Start ESP-IDF Autopilot** — it designs, implements, builds, flashes, observes, repairs, and verifies the firmware loop on the connected board.

ESP-IDF Autopilot is a hardware-in-the-loop development harness for turning a product brief into a hardware-verified ESP-IDF project. You remain the product owner; the harness performs the repetitive engineering loop.

## Why ESP-IDF Autopilot?

You have an idea for a hardware project. Arduino gets the first LED blinking quickly—but as the product grows, you need FreeRTOS tasks, native peripherals, DMA, precise timing, power control, and the vendor SDK. You open ESP-IDF and suddenly face components, CMake, build configuration, driver APIs, serial logs, and datasheets. The question becomes:

> *Could I connect my board, describe the result I want, press Start, and come back to firmware that is already flashed and checked on my hardware?*

That is the purpose of ESP-IDF Autopilot.

Connect an ESP32 project—whether it lives on a breadboard or a custom PCB—and describe its features. Autopilot creates a reviewable design, implements each component, then repeatedly builds, flashes, reads serial/runtime evidence, diagnoses failures, and repairs the smallest responsible part. The result is not merely generated source code: it is a complete ESP-IDF project with hardware evidence attached to the run.

## Background

LLMs have made Arduino-style experimentation dramatically easier. Native MCU SDKs remain essential, however, when a system needs precise control and a maintainable multi-component architecture: ESP-IDF for ESP32, STM32Cube for STM32, or Nordic's SDKs for nRF devices. These SDKs are powerful precisely because they expose the details that a real embedded product must manage—and those details are difficult for both newcomers and one-shot code generation.

Recent research makes the gap concrete. [EmbedAgent](https://arxiv.org/abs/2506.11003) reports a best ESP-IDF cross-platform-migration score of **29.4% pass@1**; in its ESP32 analysis, **211 of 504 generated programs (41.9%)** contained syntax errors, including incompatible headers and version-specific API use. [Skilled AI Agents for Embedded and IoT Systems Development](https://arxiv.org/abs/2603.19583) finds that successful compilation and flashing are not enough for hardware-in-the-loop work: timing, peripheral initialization, and hardware-specific behavior can still fail at runtime. In its ESP-IDF Level-3 system-integration tasks, an agent without skills completed only **7/14** tasks across five attempts.

The root causes are familiar to embedded developers:

1. **A real project has structure.** Components, `main/` orchestration, CMake, FreeRTOS, configuration, and integration behavior must work together.
2. **The training signal is thin.** Vendor SDKs and their idioms are far less represented than general-purpose code.
3. **APIs move.** The local ESP-IDF version and installed headers—not a model's recollection—are the authority.
4. **Peripherals are long-tail.** A less common part may require the agent to read its datasheet and implement a driver from primary documentation.

An experienced developer compensates by reading the local API and examples, studying datasheets, implementing one component at a time, and observing the real board. Autopilot turns that workflow into an explicit, repeatable harness for an agent to execute.

## Architecture

```text
Your hardware + feature brief
              │
              ▼
┌───────────────────────────────────────────────────────────┐
│ Design Harness                                             │
│ inventory hardware → inspect local SDK / official sources  │
│ → acquire datasheets → design components and verification  │
└──────────────────────────┬────────────────────────────────┘
                           │ review and approve spec.md
                           ▼
┌───────────────────────────────────────────────────────────┐
│ Execution Harness                                          │
│ implement → build → flash → read serial/runtime evidence   │
│                  ↑                 │                       │
│                  └── diagnose ← verify ── repair & retry   │
└──────────────────────────┬────────────────────────────────┘
                           ▼
      ESP-IDF firmware + evidence-bound release artifacts
```

The design harness makes the plan reviewable before implementation. The execution harness owns the build/flash/serial loop and keeps durable, project-scoped state so an interrupted run can resume rather than begin again.

## Automated development pipeline

The diagram is the whole workflow at a glance. The following stages explain what Autopilot does inside it.

| Stage | What Autopilot does | What you do |
|---|---|---|
| **1. Design** | Reads the requirements and wiring, inventories hardware, gathers SDK/component/datasheet context, and creates a versioned execution contract. | Review the concise generated `spec.md` to confirm the intended product. |
| **2. Implement** | Creates project components and `main/` orchestration, or adapts a compatible component behind a project wrapper. | — |
| **3. Hardware loop** | Builds with the local ESP-IDF, flashes the board, gathers serial/runtime data, evaluates declared checks, and repairs failures. | Only perform a predeclared physical observation when software cannot observe it. |
| **4. Release validation** | Runs a fresh build, flash, and runtime verification; binds the evidence to the firmware hash. | Inspect the final result at a glance. |

## Features

| Capability | What it means |
|---|---|
| **ESP-IDF-native** | Works with ESP-IDF projects and local SDK headers. |
| **Design before implementation** | Converts your requirements into a complete project plan for your approval, so the implementation stays aligned with your intent. |
| **Context acquisition** | Queries the ESP Component Registry, official ESP-IDF sources, and datasheet artifacts before implementation decisions are made. |
| **Firmware generation** | Implements missing components from the available evidence, or adopts compatible components when appropriate. |
| **Closed-loop repair** | Builds, flashes, reads serial/runtime data, diagnoses failures, repairs the responsible code, and verifies again. |
| **Minimal user involvement** | Prefers detailed component self-tests and quantitative evidence; asks for a human check only when hardware reality cannot be observed automatically. |

## Quick start

Open Codex in this repository, connect your ESP32 board, then paste this prompt:

```text
I have connected my ESP32 hardware. Help me create a new ESP-IDF Autopilot project called my_device.
Ask me only for the hardware wiring and the features I want. Write the project requirements and connections,
run the design, let me review the generated spec, then start and monitor the automated hardware loop.
```

Behind the scenes, the harness uses this short handoff:

```powershell
python -m orchestrator.cli design --project my_device
python -m orchestrator.cli status --project my_device
```

When the status is `WAITING_SPEC`, read `projects/my_device/design-package/rev-NNNN/spec.md`. After you approve the plan, start the implementation and hardware loop:

```powershell
python -m orchestrator.cli resume --project my_device --approve
python -m orchestrator.cli status --project my_device
python -m orchestrator.cli validate --project my_device
```

Before the first build, configure your local ESP-IDF installation and verify it with:

```powershell
powershell -ExecutionPolicy Bypass -File tools\idf.ps1 --version
```

## Revision 1.0.0 validation — in progress

Before the 1.0.0 release, we are validating the harness on two very different ESP32 projects:

| Project | Hardware | Intended result |
|---|---|---|
| **Breadboard camera pipeline** | ESP32-S3-WROOM-1, HM01B0-MWA camera, and ST7789 display on a breadboard. | Capture camera frames and display them in real time, while investigating the best practical frame rate with the available hardware and ESP-IDF capabilities. |
| **Crumb custom PCB** | A custom ESP32-PICO-D4 PCB with BQ25180YBGR charging monitor, W25N01GV flash, two ICS-41350 microphones, and a button. | Record stereo audio while the button is held, store it safely, and upload queued recordings when Wi-Fi becomes available. |

These are ongoing hardware validations. The goal is to prove the complete loop on both a flexible breadboard prototype and a purpose-built PCB: from requirements and wiring, through implementation, to firmware flashed and verified on the actual device.

## Future features after 1.0.0

1. **GUI workflow** — operate project design, progress, evidence, and approval from a graphical interface.
2. **Deeper PDF understanding** — build components for uncommon parts that have no official library or online implementation, using their datasheets as the primary source.
3. **Lower-cost development** — reduce unnecessary agent work and context so complete projects consume fewer tokens without sacrificing verification quality.

## Project layout

```text
requirements/<project>.md   # feature brief and success criteria
connections/<project>.md    # board, parts, wiring, power, and constraints
projects/<project>/         # generated ESP-IDF project and design package
runtime/projects/<project>/ # durable project-scoped run state
hardware/datasheets/        # content-addressed datasheet artifacts
```

For the implementation contracts, read [Design Harness](docs/DESIGN-HARNESS.md), [Execution Harness](docs/EXECUTION-HARNESS.md), [Architecture](docs/ARCHITECTURE.md), and [Verification Evidence](docs/VERIFICATION-EVIDENCE.md).

## Contributing

Contributions are welcome across harness design, ESP-IDF components, hardware adapters, verification, workflow resilience, and documentation.

## License

Licensed under the [MIT License](LICENSE).

<div align="center">
  <strong>Connect the hardware. Describe the product. Start the autopilot.</strong>
</div>
