---
name: esp-idf-firmware
description: Build, flash, and validate ESP-IDF firmware from requirements/<project>.md and connections/<project>.md into projects/<project>/. Use for ESP32 firmware, driver bring-up, hardware debugging, and build/flash/serial verification. Not for Arduino, non-ESP chips, editing serial_mcp.py, or general documentation.
---

# ESP-IDF Firmware

End-to-end target: requirements -> correct behavior on real hardware. Build success or plausible serial output alone is not completion.

## Inputs

| Field | Required | Notes |
|---|---:|---|
| `requirements/<project>.md` | yes | User-owned behavior; read, do not rewrite unless asked. |
| `connections/<project>.md` | yes | User-owned wiring; read, do not rewrite unless asked. |
| `activate.local.ps1` | yes | IDF source; if missing or broken, stop without guessing paths. |
| Hardware preflight | yes | Must PASS before continuous firmware work. |
| Serial port | flash/monitor | Auto-detect is allowed; record the actual port. |
| `esp-component-registry` MCP | yes | Mandatory selection search for every external part or reusable subsystem. |
| `esp-serial` MCP | yes on Windows hardware runs | Boot capture and interactive serial verification. |
| `esp-docs` MCP | optional | Conceptual/API-guide search; local installed headers and examples still control signatures. |

At project start, read `references/run-state.md`, `references/devlog-format.md`, and `references/datasheet-notes-format.md` completely.

## Pipeline and gates

```text
0 PREFLIGHT
-> 1.0 INPUTS -> 1.1 DATASHEETS -> 1.2 GROUNDED SPEC
-> 1.3 COMPONENT SELECTION -> 1.5 USER REVIEW
-> [2 SUBSYSTEM: SELECTION GATE -> KNOWLEDGE -> CODE -> BUILD -> FLASH -> SERIAL -> LEDGER] x N
-> 2.9 BATCHED TIER C (only if unavoidable)
-> 3 INTEGRATION -> 3.5 CLOSURE -> 3.6 RELEASE
```

| Gate | Exit condition |
|---|---|
| 1.3 Selection | Every external part/reusable subsystem has Registry search evidence and an adopt/reject decision |
| 1.5 Spec | Docs and selection evidence READY; all `POLICY`, credentials, protocols, bounds, dependencies, and Tier C plan approved |
| 2 Subsystem | Selection gate satisfied; build + flash + serial evidence; Tier A/B=`PASS`, unavoidable Tier C=`PASS_PENDING_TIER_C` |
| 2.9 Tier C | Every pending physical observation confirmed in one batch; skip if none |
| 3 Integration | Full call pattern and failure paths pass quantitative checks |
| 3.5 Closure | Every `R*` and required `DR*` is `PASS`; no conflicting limitation |
| 3.6 Release | Selftest off; release build, flash, and runtime serial verification pass |

## Hard rules

| # | Rule |
|---|---|
| H0 | After Stage 1.5 approval, run continuously through release, except one predeclared Stage 2.9 Tier C batch or a verified hard blocker. |
| H1 | ESP-IDF only; no Arduino abstractions. |
| H2 | Never run bare `idf.py`; use `tools/idf.ps1` and verify its version before the first build. |
| H3 | `main.c` calls semantic component APIs only; peripheral details stay in `components/<name>/`. |
| H4 | Validated datasheets, trusted component docs, local headers, and local examples outrank memory. |
| H5 | Work in dependency order: classify -> smallest reproduction -> targeted patch -> reverify. |
| H6 | Read prior DEVLOG failures and local API signatures before changing a subsystem. |
| H7 | Raw evidence goes in `projects/<project>/logs/`; summaries cite paths and key values. |
| H8 | Every original and required-derived requirement needs measurable evidence before release. |
| H9 | A known limitation conflicting with required behavior forbids PASS and release. |
| H10 | Before implementing any external part or reusable subsystem, search ESP Component Registry through MCP and save candidate plus adopt/reject evidence. Search is mandatory; adoption is not. |
| H11 | On Windows, use `esp-serial` MCP for boot capture and interactive serial sessions. Always release the port before flashing and during timeout/error cleanup. |
| H12 | Keep subsystem selftests in the delivered source. Enable them during bring-up, disable them by default for release, then rebuild, reflash, and reverify release behavior. |
| H13 | A revision for speed, power, memory, stability, endurance, latency, or throughput needs a quantified acceptance baseline before code changes. |

## Tool and knowledge routing

Use the source that is authoritative for the decision being made. A higher-level source never overrides a version-matched local signature or a validated part datasheet.

| Need | Required route | Authority and constraints |
|---|---|---|
| Decide whether reusable code already exists | `esp-component-registry.search_components(query=...)`, then the Registry component-detail tool with `namespace_name` and `component_name` for credible candidates | Mandatory before implementation. Record every serious candidate and the final adopt/reject reason. The detail tool may be exposed with a shortened/generated callable name; use the actual listed schema. |
| Install an adopted Registry component | Use the exact namespace and compatible version returned by Registry; invoke Component Manager only through `tools/idf.ps1` | Do not edit generated `managed_components/` code. Pin or constrain the dependency in the project manifest. |
| MCU-native implementation | Installed `$IDF_PATH/examples/` first, then installed `$IDF_PATH/components/` headers and sources | These match the active local ESP-IDF and control function names, signatures, Kconfig, and component dependencies. |
| ESP-IDF concepts and API guides | `esp-docs.search_espressif_sources` | Conceptual reference only when its version is not proven identical to the local installation. Confirm every used signature locally. |
| External-part registers, timing, limits, and initialization | Validated PDF plus `datasheet_notes.md` | Datasheet is final ground truth. Registry component code does not replace datasheet validation of required behavior. |
| Prior project-specific failures | Existing `DEVLOG.md`, logs, and component notes | Read before changing that subsystem so a documented failure is not repeated. |
| Boot/runtime observation on Windows | `esp-serial.monitor_boot` or `monitor_start`/`monitor_send`/`monitor_read`/`monitor_stop` | Serial output is evidence only after comparison with a declared expected marker or quantitative baseline. |
| Model memory | Search terms and draft hypotheses only | Never use memory as an unverified API signature, register value, timing limit, or acceptance baseline. |

MCP dependencies are declared in `agents/openai.yaml` and project `.codex/config.toml`. At Stage 0, confirm the required tools are callable rather than assuming configuration means availability.

- `esp-component-registry` is required for the selection gate. If a part-number query returns no match, search by capability and interface before concluding there is no usable component.
- A valid zero-result search is not the same as Registry being unavailable. Record the queries and zero-result response, then a custom implementation may be selected if the datasheet is ready.
- If Registry is unreachable after bounded retries with distinct queries, record the service error as an unresolved external dependency for the single Stage 1.5 review. Do not silently skip the gate.
- `esp-serial` is required for Windows hardware verification. If it is unavailable, diagnose the local MCP/Python dependency before firmware work; do not substitute an interactive bare `idf.py monitor` shell.
- `esp-docs` is optional. Authentication, quota, or service failure falls back to installed examples/headers and validated primary documents without user interruption.

## Continuous execution contract

After Stage 1.5 approval:

- Set `run-state.json` to `CONTINUOUS`; keep `cursor`, `next_action`, `remaining`, and evidence current.
- A subsystem/gate PASS, progress update, DEVLOG write, failure, elapsed time, or context compaction is never a stopping point.
- After each tool result, choose and execute the next diagnostic, repair, verification, or dependency action in the same turn.
- Internal build, flash, serial, runtime, protocol, and integration failures are fix-reverify loops.
- Status questions receive a short commentary answer; continue unless the user explicitly pauses or redirects.
- Do not ask for ordinary engineering judgment, a datasheet, or permission to continue.
- Stage 2.9 is the last permitted user interaction and only exists for Tier C items declared in the approved spec.
- Before release completes, use commentary for progress and do not send a final response.

Final is allowed only when the hook-verifiable state is `COMPLETE`, `WAITING_SPEC`, `WAITING_TIER_C`, `PAUSED`, or a fully evidenced hard `BLOCKED`. Otherwise read `next_action` and continue. The only successful terminal cursor is `STAGE 3.6:release:pass`.

## Workflow

### 0. Environment

```powershell
powershell -ExecutionPolicy Bypass -File tools\idf.ps1 --version
.\hwtest\Test-Hardware.ps1 -Port <PORT>
```

- Run the wrapper version command before the first build. Accept the locally activated ESP-IDF version; do not require a hard-coded version such as v6.0.
- Wrapper failure or a missing/broken `activate.local.ps1`: stop; never guess the installation path.
- Preflight failure: report the failing layer before Stage 1.5.
- Preflight PASS makes later failures firmware/tooling bugs until evidence proves otherwise.
- Confirm `esp-component-registry` and `esp-serial` tools are callable. Treat `esp-docs` as optional and record when the local-source fallback is used.
- Record the actual target, local IDF version, port, and baud rate. Use 115200 only when the project configuration or device documentation does not specify another value.

### 1.0 Inputs and inventory

- Read both user-owned inputs and extract exact MCU, part numbers, interfaces, pins, active levels, roles, external services, credentials, and protocol dependencies.
- Create the ordered subsystem inventory before writing the full spec. For each row, record whether it is MCU-native, an external part, or a reusable software subsystem, and identify its owning requirements.
- Mark every external part and reusable subsystem as `REGISTRY_SEARCH_REQUIRED`. This includes reusable protocol/storage/network/audio/camera services even when ESP-IDF has a built-in implementation; the final decision may legitimately reject Registry in favor of the version-matched built-in component.

### 1.1 Datasheet acquisition

For every external part, fetch, validate model identity, extract text, and write `components/<sub>/datasheet_notes.md` using `references/datasheet-notes-format.md`.

```powershell
python tools\fetch_datasheet.py <PART> --out projects\<project>\components\<sub>\
python tools\extract_pdf.py projects\<project>\components\<sub>\<PART>.pdf
```

Use local files -> official manufacturer -> trusted distributor/registry document cascade. Do not guess registers. A file with PDF magic bytes is insufficient: verify exact model identity, useful technical content, readable text extraction, document ID/revision, and the sections needed by the driver. Reject certificates, package-only drawings, marketing sheets, wrong model variants, and PDFs without usable technical content.

Record a datasheet manifest with source, document ID/revision, model match evidence, extraction result, notes status, and key sections used. Read the pin configuration, electrical characteristics, bus timing, register map, detailed behavior, reset/ready sequence, and application requirements relevant to firmware. Mechanical/package/order sections may be skipped unless the requirement depends on them.

If automatic retrieval is exhausted, include the exact missing artifact in the single Stage 1.5 review. Validate a user-supplied PDF through the same identity and extraction checks before approval. Do not enter register-level implementation with an unverified document and do not generate guessed register code as a placeholder.

### 1.2 Grounded spec

Create `projects/<project>/spec.md` from inputs and validated evidence:

| Section | Required content |
|---|---|
| Hardware/pins | Exact part, interface, role, normalized pins, active levels |
| Datasheet manifest | Source, identity match, extraction and notes status |
| Core logic/state machine | States, transitions, ownership, failure paths |
| Subsystems | Dependency order; MCU-native or external part |
| Component selection | Registry queries, candidates, adopt/reject decision, selected version/source, dependency impact |
| Verification | Tier, quantitative baseline, method, evidence, user participation |
| Acceptance matrix | `R*`/`DR*`, derivation, owner, test, evidence, verdict |
| Assumptions/limits | Hardware-driven bounds and completion impact |
| Review batch | All `POLICY`, credentials, protocols, missing artifacts, and proposed Tier C |

Use stable `R1...` and `DR1...` IDs. `REQUIRED-DERIVED` preserves stated real-hardware behavior and is implemented autonomously; `RECOMMENDED` is optional unless low-risk and scoped; `POLICY` requires a product tradeoff and must be resolved at Stage 1.5. Never invent arbitrary bounds to simplify an unbounded requirement.

Verification tiers:

- Tier A: identity, register/readback, CRC, write-read, or device selftest; no user.
- Tier B: autonomous quantitative range, amplitude, timing, count, distribution, stability, or injected-event judgment; no user.
- Tier C: final physical phenomenon not observable by available hardware after maximizing A/B evidence; one Stage 2.9 batch only.
- Simple inputs such as buttons are A/B: verify idle level, active polarity, debounce, event injection, timing, and state transitions without live user action.
- Do not downgrade A/B to C because a judgment method is inconvenient; derive a defensible method from docs, architecture, and measurable signals.

### 1.3 Component selection

Perform this stage for every inventory row marked `REGISTRY_SEARCH_REQUIRED`, before any implementation begins.

1. Search Registry by exact part number or well-known component name.
2. If no credible match appears, search by capability, interface, and device class; for example, search `battery charger I2C` after a part-number query returns zero results.
3. Fetch details for each credible candidate. Inspect supported targets, compatible ESP-IDF versions, exact hardware/capability coverage, public API, examples, transitive dependencies, maintenance state, license, and any documented limitations relevant to `R*`/`DR*` requirements.
4. Compare candidates against the local built-in ESP-IDF facility and a custom datasheet-grounded implementation. Do not prefer a Registry component merely because it exists.
5. Write `projects/<project>/components/<sub>/component-selection.md` containing:
   - exact queries and returned candidate identifiers/versions;
   - candidate details consulted;
   - compatibility and requirement coverage;
   - security/license/maintenance or dependency concerns that affect adoption;
   - selected option and explicit reasons every serious alternative was rejected;
   - exact dependency constraint when adopted, or `custom implementation from validated datasheet` / `local ESP-IDF built-in` when rejected.
6. Add the selection summary to `spec.md` and `DEVLOG.md`. Save the raw MCP result or a faithful structured capture under `logs/stage1_3_<sub>_registry.md` so the decision is auditable.

When a Registry component is adopted:

```powershell
powershell -ExecutionPolicy Bypass -File tools\idf.ps1 -C projects\<project> add-dependency "<namespace>/<component>^<compatible-version>"
```

- Use the exact identifier/version constraint supported by the selected component; do not invent a namespace or version.
- Review the resulting manifest and lock/dependency resolution. `managed_components/` is generated output: do not patch it to implement project behavior.
- Wrap the adopted component behind the same project-owned semantic API used by `main/`.
- Build and hardware-verify it like custom code; Registry provenance is not proof that it works with this board, wiring, configuration, or call pattern.

When no component is adopted, the saved valid searches and rejection rationale satisfy the selection gate. A custom external-part driver still requires validated `datasheet_notes.md`; an MCU-native implementation still requires local examples and headers.

### 1.5 Review

Present `spec.md` once. Approval requires all documents READY, every mandatory Registry search completed, component decisions and dependency versions exposed, all external dependencies and `POLICY` rows resolved, every acceptance method measurable, and any unavoidable Stage 2.9 action explicit. Credentials remain user-supplied and must never be committed or copied into evidence logs. Approval enters `CONTINUOUS` immediately; do not pause before Stage 2.

### 2. Per-subsystem bring-up

For each dependency:

1. **Selection gate:** read `component-selection.md`; confirm the Registry evidence, decision, selected dependency constraint, and unresolved limitations still match the approved spec. Missing selection evidence forbids implementation.
2. **Prior evidence:** read that subsystem's previous DEVLOG failures, raw failure logs, datasheet gotchas, and any affected acceptance rows before editing.
3. **Version-matched knowledge:**
   - MCU-native or built-in path: locate the closest installed IDF example, then confirm every used API in installed headers/source and its required CMake/Kconfig dependencies. Use `esp-docs` only to fill conceptual gaps.
   - Adopted Registry path: read the selected version's README, public headers, examples, dependency manifest, configuration requirements, and documented limitations. Do not call undocumented internals.
   - Custom external-part path: implement only from validated `datasheet_notes.md`; revisit the source PDF when a required detail is absent rather than filling it from memory.
4. **Smallest owning implementation:** create or modify a reusable semantic API and selftest in `components/<sub>/`; keep `main/` orchestration-only. Do not duplicate an adopted component's internals—wrap it and add only project-owned policy or missing behavior.
5. **Selftest call chain:** exercise initialization, identity/readback, normal behavior, boundary behavior, and the failure paths required by the spec. A one-shot isolated test is not evidence for a later continuous or concurrent call pattern.
6. **Expected result first:** before running, write the exact marker, value, range, count, duration, rate, tolerance, or failure response that will determine PASS. “Looks plausible” is not an acceptance rule.
7. **Build:** set the target on the first build or target change, then build through `tools/idf.ps1`; capture raw output in the project logs.
8. **Flash:** defensively stop any active serial session, flash only after a clean build, and capture the raw flash log.
9. **Serial verification:** use the `esp-serial` MCP sequence below, save raw output, compare it with the declared expectation, and stop the monitor in normal, timeout, cancellation, and error paths.
10. **Judgment and repair:** classify a failure by layer, make the smallest directed repair, then repeat build -> flash -> serial -> judgment. Do not advance on build-only success or merely plausible serial data.
11. **Ledger and continuation:** append the selection, knowledge, implementation, configuration, evidence, expected/actual, requirement coverage, and verdict fields defined in `references/devlog-format.md`; update `run-state.json`; immediately start the next dependency.

```powershell
$P="projects\<project>"; $L="$P\logs"
New-Item -ItemType Directory -Force $L
powershell -ExecutionPolicy Bypass -File tools\idf.ps1 -C $P set-target <chip> 2>&1 | Tee-Object "$L\stage2_<sub>_set-target.log"
powershell -ExecutionPolicy Bypass -File tools\idf.ps1 -C $P build 2>&1 | Tee-Object "$L\stage2_<sub>_build.log"
powershell -ExecutionPolicy Bypass -File tools\idf.ps1 -C $P -p <PORT> flash 2>&1 | Tee-Object "$L\stage2_<sub>_flash.log"
```

Do not hard-code an expected IDF version in the skill. The wrapper's successfully activated local version controls all API and build decisions.

#### Serial MCP verification

Use a one-shot boot capture when reset/boot/runtime markers are sufficient:

```text
esp-serial.monitor_boot(
  port="COM<n>",
  baudrate=<configured baud>,
  wait_for="<expected boot/runtime marker>",
  capture_duration=<bounded seconds, maximum 120>
)
```

Use a persistent session when the firmware accepts a selftest command or a multi-step interaction is required:

```text
esp-serial.monitor_start(port="COM<n>", baudrate=<configured baud>, reset=true)
esp-serial.monitor_read(timeout=<0..30>, wait_for="<ready marker>", max_lines=<bounded maximum 1000>)
esp-serial.monitor_send(text="SELFTEST <subsystem>")
esp-serial.monitor_read(timeout=<0..30>, wait_for="<PASS/FAIL marker>", max_lines=<bounded maximum 1000>)
esp-serial.monitor_stop()
```

- Use the actual callable tool schema; never guess unsupported arguments. `monitor_boot` uses `capture_duration`, while `monitor_read` uses `timeout`, `wait_for`, and `max_lines`. Preserve the intent: bounded wait, explicit expected marker, bounded output capture, and deterministic cleanup.
- `monitor_boot` resets through the serial control lines and performs bounded capture. Use it for boot evidence instead of assuming a previous session includes a fresh boot.
- `monitor_start` owns the serial port until `monitor_stop`. Always stop it before every flash attempt. If flash reports the port busy, stop the MCP session and any verified stale monitor process, then retry.
- Call `monitor_read` until the declared marker is observed, the bounded timeout expires, or the session ends. Absence of a marker is a failed observation, not PASS.
- Use `monitor_send` only when the firmware's approved selftest/control protocol expects input. Do not invent hidden runtime commands solely to make verification easier; implement a documented selftest interface when needed.
- Save the complete relevant MCP response to `logs/stage2_<sub>_serial.log` or a more specific path. DEVLOG stores the path and key values, not a pasted wall of serial text.
- Before returning from any serial-related error or timeout path, call `monitor_stop` when a session may still exist.

Build-only evidence is never a subsystem PASS. Serial output without a predeclared expected/actual comparison is also incomplete. Missing selection or ledger evidence means the cursor remains inside that subsystem.

#### Selftest lifecycle

Selftests remain part of the delivered source and are controlled by project configuration rather than deleted after bring-up. Use a project-level Kconfig option unless the existing project already has an equivalent gate:

```kconfig
config APP_SELFTEST
    bool "Per-subsystem bring-up self-tests"
    default y
```

- Keep the default enabled while Stage 2 and Stage 3 verification depend on the selftests.
- Guard diagnostic call paths with `CONFIG_APP_SELFTEST`, but keep reusable selftest functions available for future Stage 4 diagnosis.
- A subsystem selftest reports the expected baseline, actual observation, and an unambiguous PASS/FAIL marker. Logging only “initialized” or printing raw samples is insufficient.
- Identity, register readback, CRC, write-read, device-provided selftest, and exact event injection are Tier A when they prove the relevant behavior.
- Range, amplitude, timing, count, distribution, throughput, stability, and bounded fault-injection measurements are Tier B when their limits come from validated evidence or a defensible approved baseline.
- Persistent-storage selftests must create uniquely identifiable test-owned records and delete exactly those records by returned handle/key/index. Never delete “newest”, “oldest”, or arbitrary user data as cleanup.
- Network/protocol selftests must use declared test endpoints or fixtures, must not leak credentials into logs, and must distinguish transport, TLS, authentication, protocol, and application-status failures.
- Stage 3.6 changes the default to disabled, then performs a fresh build, flash, and runtime verification. Do not treat an earlier selftest-enabled image as release evidence.

### 2.9 Batched Tier C

- Skip automatically when the approved spec contains no Tier C.
- First complete every possible automated check and mark only those items `PASS_PENDING_TIER_C`.
- Present one concise batch: exact action, expected observable pattern, and one response covering all items.
- Convert confirmed items to PASS and immediately resume `CONTINUOUS`. No later user confirmation is allowed.

### 3. Integration

- Require every Stage 2 subsystem ledger to be `PASS`; `PASS_PENDING_TIER_C` may proceed only when it corresponds to the approved single Stage 2.9 batch. No unrecorded selection, partial subsystem, or unresolved limitation enters integration.
- Read every component's semantic API, selftest call chain, blocking behavior, timing requirements, ownership rules, and failure contract before designing tasks.
- Design the task architecture before writing integrated `main.c`:

| Signal | Required design response |
|---|---|
| Blocking I/O or an operation with unbounded/long latency | Give it explicit task ownership or a bounded asynchronous interface; do not block a real-time producer. |
| Fixed-rate capture, display refresh, motor/control loop, or other real-time cadence | Isolate the cadence from network, storage, erase, reconnect, and debounce delays; measure missed periods or drops. |
| Producer/consumer rate mismatch | Use a bounded queue/ring buffer and define overflow, backpressure, drop, retry, and shutdown behavior. |
| One-to-one event or completion signal | Use a task notification, semaphore, or another explicit FreeRTOS primitive appropriate to the ownership model. |
| Multi-bit system state or readiness conditions | Use an event group or a single owning state machine; define which task may set/clear each condition. |
| Shared mutable resource | Prefer single-task ownership plus messages. If sharing is unavoidable, use a mutex and document lifetime, lock ordering, and forbidden blocking regions. |
| Long-lived task | Measure stack high-water mark and repeated-cycle heap/resource behavior; do not select final stack size from guesswork alone. |

- No bare mutable variables may be used as cross-task synchronization. `volatile` does not create ownership, atomicity, ordering, backpressure, or wake-up semantics.
- Determine task priorities, stack sizes, queue depths, and timeouts from required timing plus measured behavior. Record the chosen bounds and evidence; do not hard-code unexplained “large enough” values.
- Keep `main.c` as orchestration and state-machine code. It calls semantic component APIs and owns product policy; register operations, bus transactions, and raw ESP-IDF peripheral setup remain in the owning component.
- Build, flash, and serial-verify the complete state machine using the real integrated call pattern. Cover all patterns implied by requirements, including:
  - concurrent producer/consumer activity;
  - sustained operation for a justified duration or iteration count;
  - restart/reconnect/reboot reconstruction;
  - queue full/empty, storage full/reclaim, network unavailable, peripheral timeout, and invalid-response paths;
  - bounded RAM, heap, stack, handles, files, sockets, and persistent records over repeated iterations;
  - rate, throughput, latency, drop count, retry count, timing jitter, and recovery time where relevant.
- A Stage 2 one-shot API call does not validate a tight loop, concurrent use, repeated reconnect, continuous DMA, streaming upload, or long-duration write. Add quantitative integration tests for every materially different call pattern.
- On failure, first classify ownership:
  - orchestration/state transition/scheduling error: fix `main/` and rerun integration;
  - missing or incorrect component behavior: return to that component's Stage 2 selection/knowledge/implementation gate, patch the owning layer, rebuild/flash/serial-verify the component, then rerun integration;
  - changed semantic interface or dependency version: reverify every direct consumer and affected acceptance row;
  - protocol uncertainty: reproduce and verify the exact request/response on a host before changing the MCU path.
- Do not modify component internals during integration merely to suppress a symptom. Locate the owning layer and preserve the component boundary.

### 3.5 Requirements closure

- Review every `R*` and required `DR*`: `PASS` needs implementation plus raw evidence; `PARTIAL`, `FAIL`, `BLOCKED`, mocked, bounded, or untested work forbids release.
- Review component-selection decisions again: an adopted dependency's limitation or a rejected candidate does not excuse missing required behavior.
- Apply when relevant: reboot reconstruction and commit ordering; power-loss consistency; storage capacity/reclaim and test-owned cleanup; bounded RAM/heap/stack over multiple iterations; synchronization/backpressure/failure isolation; exact host-verified external protocol; credential redaction; dependency and license constraints.
- A limitation is acceptable only when it does not conflict with any original or required-derived requirement. Record the reason and affected scope rather than hiding it in prose.

### 3.6 Release

- Keep all selftest source and diagnostic APIs, but change the release configuration from `default y` to `default n` (or the existing project's equivalent release default) so selftests are disabled by default:

```kconfig
config APP_SELFTEST
    bool "Per-subsystem bring-up self-tests"
    default n
```

- Perform a clean release build through `tools/idf.ps1`, capture the build log, stop any serial session, flash the release image, and capture the flash log.
- Reset and serial-verify the release runtime with `esp-serial`. Confirm normal startup and required runtime markers without relying on selftest-only output.
- Confirm the release image does not enter destructive test paths, create test-owned persistent records, expose credentials, or depend on interactive test commands for normal operation.
- Append closure and release evidence, set `release_verified=true`, and set the successful terminal state only when every condition in `references/run-state.md` is satisfied.

### 4. Revision

Use this workflow for a bug fix or a new requirement applied to an existing project. Do not automatically restart unrelated subsystems, but do not under-test affected dependencies or consumers.

1. Restate the requested behavior and map it to existing/new `R*` or `DR*` rows.
2. Locate ownership: component internal behavior, component interface, dependency selection/version, task architecture, state-machine policy, external protocol, or release configuration.
3. For a non-functional request—speed, power, memory, stability, endurance, latency, throughput, startup time, reconnect time, or data-loss tolerance—define a quantified acceptance baseline before editing. Include workload, measurement method, duration/sample count, threshold/tolerance, and evidence path.
4. Read the previous selection decision, prior failures, local signatures, relevant datasheet sections, and existing evidence. If the selected dependency no longer meets the requirement, repeat Registry selection and record the new adopt/reject decision.
5. Patch the smallest owning layer. Do not widen an interface, replace a dependency, or alter task ownership unless the requirement actually needs it.
6. Rebuild, flash, and serial-verify the owning subsystem or integration path against the new baseline.
7. Reverify all affected consumers, failure paths, acceptance rows, closure conditions, and release configuration. An unchanged interface reduces scope but does not make reintegration “free” when timing/resource behavior changed.
8. Append revision evidence to DEVLOG and update the run-state cursor through a new verified Stage 3.6 release. Preserve the old evidence; do not rewrite history.

## Outputs

| Path | Purpose |
|---|---|
| `projects/<project>/spec.md` | Grounded state machine, review batch, verification and acceptance |
| `projects/<project>/run-state.json` | Hook-readable authoritative cursor and terminal state |
| `projects/<project>/components/<sub>/component-selection.md` | Registry queries, candidates, compatibility, and adopt/reject decision |
| `projects/<project>/components/<sub>/datasheet_notes.md` | Validated external-part register, timing, initialization, and verification ground truth |
| `projects/<project>/components/<sub>/` | Reusable semantic components and selftests |
| `projects/<project>/main/idf_component.yml` or owning manifest | Adopted Registry dependency constraints; exact location follows project structure |
| `projects/<project>/managed_components/` | Generated Registry component sources; inspect but do not hand-edit |
| `projects/<project>/main/` | Orchestration and state machine only |
| `projects/<project>/logs/` | Raw build, flash, serial, and protocol evidence |
| `projects/<project>/DEVLOG.md` | Decisions, failures, subsystem, closure, and release ledgers |

## Response

Successful final:

```text
STAGE: 3.6:release:pass
Build: PASS (<chip>)   Flash: COM<n>
Selftest: disabled in release; retained for diagnostics
Requirements: <required>/<required>; partial=0; blocked=0
Runtime: release serial verification PASS
Logs: <key paths>   DEVLOG/run-state: updated
```

At Stage 1.5 or 2.9, request only the single declared review batch. For a hard blocker, report the exact failed external layer and evidence; do not ask open-ended questions.

Early stop at an allowed gate:

```text
STAGE: <1.5:spec:review | 2.9:tier-c:review | blocked cursor>
Stopped because: <allowed gate or exact evidenced external blocker>
Completed evidence: <paths and key results>
Need from you: <one predeclared review batch or one exact external artifact/action>
Next after resolution: <concrete cursor/action>
```

## Failure signals

| Signal | Meaning | Action |
|---|---|---|
| Bare `idf.py` or wrong version | Wrong environment | Use wrapper; verify version |
| Unknown API/function | Unverified signature | Search local headers/examples and official docs |
| Undefined reference | Dependency/symbol error | Inspect symbols and `REQUIRES` |
| Registry part-number search returns zero | Query too narrow, not proof of outage | Search by capability/interface; save both queries and result |
| Registry MCP request fails across distinct queries | Service/config/network failure | Diagnose MCP availability; record unresolved dependency for Stage 1.5 instead of skipping selection |
| Adopted component does not build | Version/target/config/dependency mismatch | Re-read fetched details and manifest; select a compatible version or record rejection and reevaluate alternatives |
| `add-dependency` rejects the identifier | Guessed namespace/version or invalid constraint | Use the exact Registry identifier and compatible version; do not patch `managed_components/` |
| `esp-docs` authentication/quota failure | Optional documentation source unavailable | Fall back to installed examples/headers and validated primary docs |
| Serial MCP unavailable on Windows | Verification transport/tooling failure | Diagnose `.codex/config.toml`, Python/MCP, and pyserial; do not substitute bare interactive `idf.py monitor` |
| Serial zero/full-scale/stuck | Data-path failure | Return to subsystem baseline |
| Serial has output but expected marker is absent | Observation did not meet the declared test | Save output, classify boot/runtime/test-path failure, patch, and reverify |
| Plausible data, wrong flow | State-machine failure | Fix orchestration; reverify integration |
| Watchdog/stack overflow | Task/resource error | Measure stack, isolate blocking work |
| Drops/stutter without crash | Producer starvation, blocking, or backpressure failure | Measure rates/counts/queue occupancy; separate blocking work and define overflow behavior |
| Cross-task corruption or intermittent state | Shared ownership/synchronization error | Move to single-owner messaging or add the correct FreeRTOS primitive and reverify sustained behavior |
| Flash port busy | Monitor owns port | Stop monitor/process; retry |
| Persistent test damages state | Cleanup ownership wrong | Remove only test-owned records |
| External non-2xx | Protocol/transport failure | Host-verify exact request; isolate MCU path |
| Performance revision has no threshold | Completion cannot be judged | Define workload, metric, duration/sample count, and pass limit before editing |
| Limitation conflicts with requirement | False completion | Mark non-PASS; continue implementation |
| Subsystem PASS then no next action | Continuous run stalled | Advance state and start next dependency now |
| Premature final | Stop contract violated | Discard final; execute hook-provided next action |
