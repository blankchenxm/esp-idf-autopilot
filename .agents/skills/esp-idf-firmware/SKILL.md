---
name: esp-idf-firmware
description: Build, flash, resume, revise, and validate ESP-IDF firmware from matching requirements/project-name.md and connections/project-name.md inputs through the repository Design Harness and LangGraph Execution Harness. Use for new ESP32 projects, driver/component bring-up, hardware debugging, interrupted-run recovery, and release verification. Do not use for Arduino, non-ESP targets, editing serial_mcp.py, or documentation-only work.
---

# ESP-IDF Firmware Harness

The repository harness is workflow authority. Do not manually imitate its state machine.
LangGraph checkpoints hold the control cursor. Approved designs, Receipts, Evidence, and events
hold facts. `run-state.json` is generated compatibility output and must never be hand-edited.

## Inputs and routing

| Input | Rule |
|---|---|
| `requirements/<project>.md` | User-owned; read/hash, never rewrite without an explicit request. |
| `connections/<project>.md` | User-owned; read/hash, never rewrite without an explicit request. |
| `projects/<project>/` | Only project-owned source/config may be changed. |
| `design-package/rev-NNNN/` | Immutable five-file design authority; never edit an approved revision. |
| `execution/`, `logs/` | Generated facts; never hand-edit state, receipts, evidence, events, or verdicts. |

The workflow is project-agnostic. Any new matching requirements/connections pair can begin at
design without Smoke, Crumb, copied firmware, or a pre-existing package.

Read root `AGENTS.md`, then explicitly read the routed guide:

| Work | Read first |
|---|---|
| Design/package | `docs/DESIGN-HARNESS.md` |
| Graph/orchestration | `orchestrator/AGENTS.md`, `docs/EXECUTION-HARNESS.md` |
| Firmware/components | `projects/AGENTS.md`, `docs/ARCHITECTURE.md` |
| Wrappers/hardware | `tools/AGENTS.md`, `docs/HARDWARE-SAFETY.md` |
| Evidence/closure | `docs/VERIFICATION-EVIDENCE.md` |

Explicitly authorized plaintext credentials may remain in the user-owned requirements input and an
ignored local private build artifact. Redact them before model prompts and never copy them into
contracts, source, logs, receipts, evidence, or replies. Prefer references/environment inputs when
the user has not granted that project exception.

## Commands

```powershell
# Launch or observe the project's durable Design job
python -m orchestrator.cli design --project <project>

# Sole normal design interaction, after reviewing projects/<project>/design-package/.../spec.md
python -m orchestrator.cli resume --project <project> --approve

python -m orchestrator.cli start --project <project>
python -m orchestrator.cli resume --project <project>
python -m orchestrator.cli status --project <project>
python -m orchestrator.cli validate --project <project>

# Maintenance/audit operations; these use Harness APIs, never hand-edit facts
python -m orchestrator.cli reconcile-state --project <project>
python -m orchestrator.cli migrate-datasheets --project <project>
python -m orchestrator.cli archive-runs --project <project>
python -m orchestrator.cli snapshot-project --project <project> --output <outside-repo-path>
python -m orchestrator.cli reset-project --project <project> --snapshot <outside-repo-path>
python -m orchestrator.cli restore-project --project <project> --snapshot <outside-repo-path>

# Unapproved amendment; impact analysis and revalidation remain required
python -m orchestrator.cli prepare-revision --project <project> --from-revision 1 --to-revision 2
```

## Durable Design job

`design` is programmatically detached from the invoking shell. It returns `DESIGN_RUNNING` with a
project-scoped `job_id`; the worker continues through provider repair, grounding, validation, and
atomic promotion even if the caller or conversation exits. Repeating `design` observes the same
live job rather than launching a duplicate. Poll `status` until `WAITING_DESIGN_INPUT`,
`WAITING_SPEC`, or `BLOCKED`.
Do not infer progress from stdout silence or manage Design worker PIDs manually.

Use `--revision N` to select a non-latest revision. `WAITING_DESIGN_INPUT` is an unnumbered staged
review surface for a user-owned product or policy fact and cannot be approved. `WAITING_SPEC`
always identifies a clean immutable revision that waits only for digest-bound approval. Never
implement, build, or flash around approval.

Never run bare `idf.py`. Before the first build the adapter must execute:

```powershell
powershell -ExecutionPolicy Bypass -File tools\idf.ps1 --version
```

If activation is missing/broken, preserve the exact evidence and stop. Never guess an IDF path,
target, toolchain, port, pin, voltage, polarity, timing, destructive behavior, or protocol fact.

## Design workflow

Every project compiles to exactly five authority files:

| File | Contract |
|---|---|
| `spec.md` | Sole human review surface; uncertainty remains visible. |
| `execution-contract.json` | R/DR, DAG, selections, grounding, architecture, verification, Tier C, integration, release, limitations. |
| `manifest.json` | Revision and hashes of inputs, providers, spec, contract, and complete digest. |
| `design-validation.json` | Deterministic schema, ownership, DAG, safety, evidence, and reachability result. |
| `approval.json` | User approval bound to revision and complete digest. |

The design provider must read this project's own inputs, inventory every MCU-native feature,
external part and reusable software subsystem, map dependencies/interfaces/resources, and assign
every R/DR owner. Mark facts USER, DERIVED, ASSUMPTION, or POLICY. Preserve UNKNOWN/DEFERRED;
unsafe unknowns block approval. Predeclare marker/value/range/count/duration/rate/tolerance,
timeout, Tier, evidence, and failure response for every acceptance row. Freeze FreeRTOS tasks,
priorities, stack measurement policy, ownership, queues, backpressure, synchronization, shutdown,
timeouts, and recovery before integrated implementation.

Ground implementation choices in this order:

- MCU-native: activated local ESP-IDF example/header/source first; installed source defines APIs.
- External part/reusable software: ESP Component Registry MCP exact plus capability search,
  candidate details, adopt/reject reason, and pinned namespace/version. Distinguish zero results
  from outage. The Harness itself must persist raw MCP/Datasheet Receipts and bind their hashes in
  `manifest.json`; a model-reported search is not evidence. Never edit `managed_components/`;
  wrap adopted libraries behind semantic APIs. For external parts, `part_number` is the exact
  query authority; keep descriptive/interface wording in the separate capability query.
- Datasheet L0/L1: exact part/variant/document/revision plus design, hardware, safety, and
  acceptance facts. Design approval stops here.
- After receipt-bound Registry details exist, let the Harness replace any draft component choice
  with the final compatible adoption/rejection and frozen operation coverage.
- Before implementing an owner, let Execution readiness compare its required operations with the
  selected component and existing facts. If gaps remain, invoke the replaceable reader only for
  those named gaps and persist the design/source-bound implementation addendum. A custom driver
  does not imply a complete L2 read. Destructive/safety constants require authoritative sources;
  L3 remains failure-directed.

Design-time datasheet reading must not duplicate the driver study. A compatible official
component remains preferred and must be adopted unless receipt-owned evidence identifies an
objective compatibility or required-operation gap. Datasheets validate board/safety/acceptance
constraints at Design and supply only missing operation facts during Execution.

Only clean deterministic validation may be approved. Any changed input, provider output, spec,
contract, or file hash invalidates approval.

Design generation is unnumbered staging. Provider repair, Registry/Datasheet grounding, Schema,
and deterministic analysis may retry internally; only a clean attempt is atomically promoted to
`rev-NNNN`. Failed attempts never consume a product revision. Successful grounding is reused only
when provider/tool version, query/source, requested level, and document hash remain unchanged.
Structural repair is not an authority event and cannot remove an existing product-owner decision.
Grounding-only provisional unknowns must use `[GROUNDING_PENDING]`; the Harness may resolve them
only after receipt-bound grounding validates, records the reconciliation, and leaves genuine
`[USER_DECISION]` items active. Credential redaction/provisioning is Harness policy, not a
design decision. Missing driver/API/register/DMA/recovery detail after valid L1 is likewise an
Execution-readiness obligation: record the deferral and acquire only named operation facts in the
immutable addendum. This includes format/buffer bounds deliberately selected from those facts.
Do not invoke a full Design repair or a human gate for either category.
Datasheets live once in `hardware/datasheets/objects/sha256/`; project manifests reference hashes.
Project control is isolated under `runtime/projects/<project>/`; never reuse another project's
checkpoint, provider workspace, lock, or thread registry. Snapshot/reset/restore keep user inputs
in place, preserve shared objects, and require an integrity-verified external snapshot.

## Execution workflow

```text
initialize -> environment/hardware preflight -> design validation -> approval
-> digest and hardware-session binding -> dependency-ordered verification batches
-> integration -> artifact materialization -> one optional Tier C batch -> closure
-> fresh selftest-off release -> COMPLETE
```

Tier C artifact sources must be directly usable: project-relative `local_file`, absolute HTTP(S)
`download_url`, or explicit `physical_observation`. Descriptions of future artifacts are invalid.

For every subsystem:

1. Pass the deterministic readiness checkpoint. Load the derived owner-specific contract view and
   immutable implementation addendum: final component/version, operation coverage, only required
   missing facts, receipt hashes, and current owner failure logs.
2. Adopt the selected Registry dependency behind `components/<owner>/` when one is frozen.
   Implement custom code only for uncovered operations. Keep register/bus/transport details out
   of `main/`; `main/` owns product orchestration and state machines.
3. Retain selftests for init, identity/readback, normal, boundary, and failure behavior. Never
   weaken expected values to make tests pass.
4. Execute the frozen verification batch. Risky hardware/destructive/ambiguous owners remain
   isolated; compatible software owners may share build/flash/serial. Store each owner's Evidence
   separately and release ports on every exit path. Build success is not PASS.
5. Write immutable Receipts and Evidence bound to run ID, design digest, firmware hash, and stable
   hardware identity, then immediately advance.
6. Classify failures, apply the smallest owner-directed repair under retry policy, and reverify the
   owner plus affected consumers. Routine source/build/port/USB/serial failures require no user.

Node exceptions route through the Graph `recover` gate. Persist the normalized failure and material
fingerprint, clean/re-probe or repair the owning source, and retry the failed checkpoint boundary.
The same failure without material change must become an evidenced `STALL`, never an infinite loop.

Refresh changed COM ports by stable hardware identity. The implementation agent works in a
disposable mirror and imports only project CMake/Kconfig/sdkconfig, `main/`, and `components/`.
It may not change user inputs, approved design, `managed_components/`, or generated fact stores.

Batch proposals are made during Design, checked deterministically for dependency order, isolation,
attribution, and resources, then frozen. Runtime never lets a model improvise batches.

Tier A/B is autonomous and Tier C defaults empty. Tier C must prove why A/B cannot verify a required
physical property, list completed automated checks, declare retry owners, and bind a user-accessible
artifact or explicit physical observation. Integration materializes the final artifact first.
Confirmation must bind its SHA-256; bare `confirmed`, missing artifacts, `unable`, or
`not-performed` cannot PASS. Failure repairs declared owners, reruns affected batches and
Integration, and presents a new artifact.

Every Tier A/B row must declare executable `test_setup` and `stimulus`. When a required marker is
selftest-only, use `firmware_selftest` with explicit Kconfig overrides, `isolated_build=true`, and
`firmware_simulation`; build/flash/capture that image before the separately configured selftest-off
release. Never wait for selftest-only logs from a release image. Real physical input is Tier C unless
an approved `automated_fixture` setup declares its own stimulus.

Integration must compose, rebuild, flash, and collect fresh cross-subsystem evidence; it cannot
relabel component evidence. Cover applicable concurrency, sustained operation, reboot/reconnect,
backpressure, failure recovery, throughput, latency, jitter, drops, and resource bounds.

Closure accepts only current-run/current-design PASS evidence and no release-conflicting
limitation. Stale, mock, PARTIAL, FAIL, BLOCKED, missing, or contradictory evidence forbids release.
An immutable correction can supersede mistaken Evidence without editing history and demotes an
affected COMPLETE projection until fresh verification closes the row.

Release proves the contract-specific selftest config effectively disabled, performs fullclean and
a new build, stops serial, flashes, captures fresh normal runtime, and binds successful build,
flash and serial receipts to the application binary hash. Normal operation must not depend on
selftest, expose secrets, retain test junk, or take destructive test paths.

## Output contract

| Mode | Required response |
|---|---|
| `NEEDS_DESIGN` | Exact generic `design --project` command. |
| `WAITING_DESIGN_INPUT` | Staged `spec.md`, exact unresolved user-owned facts, and the input file that must change. |
| `WAITING_SPEC` | `spec.md`, revision, digest, unresolved items, and approval command. |
| `WAITING_TIER_C` | One predeclared batch after autonomous checks pass. |
| `BLOCKED` | Kind, summary, attempts, evidence path, and one needed external change. |
| `PAUSED` | Only an explicit pause/change. |
| `COMPLETE` | Terminal validation, full R/DR closure, release receipt IDs, firmware hash, evidence paths. |

Never announce completion from reasoning or build success. While mode is `CONTINUOUS`, invoke or
resume the runner. Recover projection from checkpoint plus immutable facts, never conversation
memory. Preserve unrelated worktree changes and all historical revisions/evidence.

## Failure signals

| Signal | Meaning | Action |
|---|---|---|
| Activation broken | Environment authority absent | Stop with wrapper evidence; never guess. |
| Input/file hash mismatch | Design stale | Create a new revision; never patch approval. |
| Registry outage | Provider unknown | Record outage; do not claim zero candidates. |
| Readiness has operation gaps | Facts insufficient | Target-read only named gaps; persist and bind an addendum before coding. |
| Hardware mismatch | Wrong/ambiguous target | Block before flash. |
| Repeated fingerprint | Repair stalled | Emit STALL after retry exhaustion. |
| Evidence run/digest/hash mismatch | Evidence stale | Reject and verify fresh. |
| Selftest enabled | Not release firmware | Disable config, fullclean, rebuild. |
| Terminal validator fails | Run incomplete | Continue from owning gate; never say COMPLETE. |
