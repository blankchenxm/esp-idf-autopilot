---
name: esp-idf-firmware
description: Build, flash, resume, revise, and validate ESP-IDF firmware from requirements/<project>.md and connections/<project>.md through the repository Design Harness and LangGraph Execution Harness. Use for new ESP32 projects, component or driver bring-up, hardware debugging, interrupted-run recovery, and release verification. Do not use for Arduino, non-ESP targets, editing serial_mcp.py, or documentation-only work.
---

# ESP-IDF Firmware Harness

The repository harness is workflow authority. Do not reproduce its state machine manually.
LangGraph checkpoints hold the control cursor. Approved design packages, Receipts, Evidence,
and events hold facts. `run-state.json` is a generated compatibility projection, not an input.

## Inputs and authority

| Input | Required | Rule |
|---|---:|---|
| `requirements/<project>.md` | yes | User-owned; read and hash, never rewrite without an explicit request. |
| `connections/<project>.md` | yes | User-owned; read and hash, never rewrite without an explicit request. |
| `projects/<project>/` | generated or existing | Only project-owned source/config may be changed. |
| `design-package/rev-NNNN/` | before execution | Immutable five-file design authority. Never edit an approved revision. |
| `execution/` and `logs/` | generated | Never hand-edit checkpoints, receipts, evidence, events, verdicts, or projections. |

This workflow is project-agnostic. A new matching pair such as
`requirements/new_product.md` plus `connections/new_product.md` is sufficient to begin design;
no Crumb, Smoke, copied firmware tree, or pre-existing design package is required.

Read root `AGENTS.md`, then explicitly read the routed local guide before editing:

| Work | Read first |
|---|---|
| Design compiler or package | `docs/DESIGN-HARNESS.md` |
| Orchestrator, graph, state | `orchestrator/AGENTS.md`, `docs/EXECUTION-HARNESS.md` |
| Firmware/component source | `projects/AGENTS.md`, `docs/ARCHITECTURE.md` |
| Hardware or wrapper adapter | `tools/AGENTS.md`, `docs/HARDWARE-SAFETY.md` |
| Evidence or closure | `docs/VERIFICATION-EVIDENCE.md` |

Never place secret values in contracts, prompts, receipts, logs, evidence, or responses. Use
environment variables or named secret references only.

## Commands

```powershell
# First design for any new project input pair
python -m orchestrator.cli design --project <project>

# Review spec.md, then make the one normal design decision
python -m orchestrator.cli resume --project <project> --approve

# Start, resume, inspect, and validate
python -m orchestrator.cli start --project <project>
python -m orchestrator.cli resume --project <project>
python -m orchestrator.cli status --project <project>
python -m orchestrator.cli validate --project <project>

# Create an unapproved amendment; impact analysis and revalidation remain mandatory
python -m orchestrator.cli prepare-revision --project <project> --from-revision 1 --to-revision 2
```

Use `--revision N` when the latest revision is not intended. The runner returns `NEEDS_DESIGN`
when no package exists and identifies the exact design command. An unapproved package returns
`WAITING_SPEC`; do not build, flash, or implement around that gate.

Never run bare `idf.py`. Before the first build the adapter must run:

```powershell
powershell -ExecutionPolicy Bypass -File tools\idf.ps1 --version
```

If `activate.local.ps1` is absent or broken, preserve the exact failure and stop. Never guess
an IDF location, target, toolchain, port, pin, electrical limit, or protocol fact.

## Design workflow

The Design Harness compiles every project into exactly five authority files:

| File | Purpose |
|---|---|
| `spec.md` | Sole human-readable review and approval surface; uncertainty stays visible. |
| `execution-contract.json` | R/DR rows, subsystem DAG, selections, grounding, architecture, verification, Tier C, integration, release, limitations. |
| `manifest.json` | Revision plus hashes of project inputs, providers, spec, contract, and complete design digest. |
| `design-validation.json` | Deterministic schema, completeness, DAG, ownership, safety, evidence, and reachability verdict. |
| `approval.json` | User approval event bound to revision and complete design digest. |

The design provider must:

1. Read this project's requirements and connections; never substitute a sample project.
2. Inventory MCU-native features, external parts, reusable software, dependencies, interfaces,
   buses, pins, resources, lifecycle, and every R/DR owner before implementation.
3. Mark facts as USER, DERIVED, ASSUMPTION, or POLICY. Keep missing facts UNKNOWN/DEFERRED and
   blocking when unsafe; never turn model prior knowledge into hardware fact.
4. Declare expected marker/value/range/count/duration/rate/tolerance, timeout, Tier, evidence,
   and failure response for every acceptance row before hardware side effects.
5. Freeze FreeRTOS tasks, priorities, resource ownership, measured stack policy, queues,
   backpressure, synchronization, timeouts, shutdown, and recovery.
6. Produce a dependency order and integration plan that covers real cross-subsystem call paths.

Use staged grounding:

- MCU-native feature: activated local ESP-IDF examples, headers, and source first. Installed
  source defines API signatures; official docs supplement it.
- External part or reusable software: ESP Component Registry exact search and capability search
  through MCP, candidate details, adopt/reject reason, and pinned namespace/version. A zero-result
  is distinct from an MCP outage. Never patch `managed_components/`; wrap adopted libraries.
- Datasheet L0/L1: exact part, variant, document, revision, coverage, pins, power, timing, init,
  and acceptance constraints needed for design.
- Custom driver: Datasheet L2 is mandatory before coding. Invoke the replaceable deep-reader
  skill for complex figures/tables or missing implementation facts. L3 is failure-directed.

Datasheet reading at design time need not duplicate a complete driver study. Library/example
implementations remain preferred when compatible; the datasheet validates hardware constraints
and acceptance. Deep reading is deferred until a custom driver or an evidenced failure needs it.

Only a clean deterministic validation may be approved. Approval binds the full digest. A changed
input, spec, contract, provider result, or manifest reference invalidates that approval.

## Execution workflow

```text
initialize -> environment/hardware preflight -> design validation -> approval
-> digest and hardware-session binding -> dependency-ordered subsystem loop
-> one optional Tier C batch -> integration -> deterministic closure
-> fresh selftest-off release -> COMPLETE
```

For each subsystem, the graph must continue automatically:

1. Load the frozen owner rows, selection/grounding, dependencies, history, previous failure logs,
   and allowed source boundary.
2. Implement in `projects/<project>/components/<owner>/` behind a semantic API. Keep registers,
   bus transactions, and transport details out of `main/`; `main/` owns product orchestration.
3. Retain a reusable selftest covering initialization, identity/readback, normal, boundary, and
   failure behavior. Never weaken frozen expected values to make a test pass.
4. Build only through `tools/idf.ps1`; stop stale serial ownership; flash the current build;
   capture bounded boot/persistent output; release port ownership on success and failure.
5. Evaluate actual output against the frozen rule and write immutable Receipts and Evidence bound
   to run ID, design digest, firmware hash, and stable hardware identity.
6. Classify a failure, apply the smallest owner-directed repair within retry policy, fully
   reverify the affected owner and consumers, then advance without asking for routine help.

Port renumbering, transient USB disconnect, stale monitor ownership, port busy, build error,
missing marker, and repairable source defects are internal recovery paths. Refresh the session by
stable hardware identity. Never bind solely to a remembered COM number.

The implementation agent works in a disposable mirror and may import only CMake/Kconfig/sdkconfig,
`main/`, and `components/` source. It may not alter user inputs, approved designs, generated
components, receipts, evidence, logs, checkpoints, or approvals.

Tier A/B is autonomous. Tier C is only an unavoidable physical observation, appears in the
approved contract, is asked once as a batch, and becomes formal current-run Evidence. Missing
external service, ambiguous/wrong hardware, an unsafe unknown, new policy, explicit pause, or an
exhausted evidenced hard blocker may stop continuous execution; ordinary tool failures may not.

Integration must compose and rebuild the full product firmware, flash it, and collect fresh
cross-subsystem evidence. It cannot relabel component evidence. Cover applicable concurrency,
sustained operation, reboot/reconnect, backpressure, failures, throughput, latency, jitter, drops,
recovery, and quantitative resource bounds. Route failures to the smallest owner.

Closure accepts only PASS evidence from the current run and current design digest, with no
release-conflicting limitation. Missing, stale, mock, PARTIAL, FAIL, BLOCKED, or contradictory
evidence forbids release.

Release retains diagnostic/selftest source but proves the contract-specific selftest config is
effectively disabled, performs fullclean plus a new build, stops serial, flashes, captures fresh
normal runtime, and binds successful build/flash/serial receipts and the application binary hash.
Normal operation must not depend on selftest, expose secrets, retain test junk, or take destructive
test paths.

## Outputs and response

| Mode | Response |
|---|---|
| `NEEDS_DESIGN` | Give the exact generic `design --project` command. |
| `WAITING_SPEC` | Cite `spec.md`, revision, digest, unresolved items, and the one approval command. |
| `WAITING_TIER_C` | Present the single predeclared batch only after autonomous checks pass. |
| `BLOCKED` | Give kind, summary, attempts, evidence path, and one required external change. |
| `PAUSED` | Use only for an explicit user pause or changed request. |
| `COMPLETE` | Cite terminal validation, R/DR closure, release receipt IDs, firmware hash, and evidence paths. |

Never announce completion from chat reasoning or build success. While mode is `CONTINUOUS`, invoke
or resume the runner's next action. Recover projection from checkpoint plus immutable artifacts,
never from conversation memory. Preserve unrelated working-tree changes and all historical design,
events, receipts, evidence, and releases.

## Failure signals

| Signal | Meaning | Required action |
|---|---|---|
| Missing/broken activation | Environment authority unavailable | Stop with exact wrapper evidence; do not guess. |
| Input hash mismatch | Design authority is stale | Generate a new revision; never patch approval. |
| Registry outage | Provider result is unknown | Record outage; do not claim zero candidates. |
| L2 absent for custom driver | Implementation facts are insufficient | Run deep datasheet grounding before coding. |
| Hardware identity mismatch | Wrong or ambiguous target | Block before flash and name required resolution. |
| Repeated progress fingerprint | Repair loop stalled | Emit evidenced STALL blocker after policy exhaustion. |
| Evidence from another run/digest/hash | Stale evidence | Reject it and execute fresh verification. |
| Selftest config still enabled | Release is test firmware | Disable through project config, fullclean, and rebuild. |
| Terminal validation error | Closure/release is incomplete | Continue from the owning failed gate; never say COMPLETE. |
