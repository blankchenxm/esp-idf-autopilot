---
name: esp-idf-firmware
description: >-
  Use when asked to write, build, flash, or validate ESP-IDF firmware for an ESP32 board
  in this repo. Triggers: 写固件, 写驱动, build/flash this, bring up <sensor>, 读不到数据,
  烧录后验证. Not for Arduino, non-ESP chips, or editing serial_mcp.py.
type: rigid
---

# ESP-IDF Firmware

End-to-end: requirements → correct data on real hardware.
Build success ≠ goal. "Data appears on serial" ≠ success. Data must match board baselines.
User involvement is minimal and pre-disclosed in STAGE 1 — not discovered mid-flow.

```
┌──────────────────────────────────────────────────────────────────────┐
│ STAGE 1    requirements → spec.md (6 modules + verify tiers)         │
│            → [USER REVIEW ✓]  mandatory before continuing            │
├──────────────────────────────────────────────────────────────────────┤
│ STAGE 1.5  batch-fetch all [External Sensor] datasheets              │
│            missing PDF → ask user → block until resolved             │
├──────────────────────────────────────────────────────────────────────┤
│ STAGE 2    per subsystem, dependency order, one at a time:           │
│            classify(2.0) → knowledge(2.A|2.B) → lib(2.C) →          │
│            example(2.D) → hardware verify(2.E)  ↺ on fail           │
├──────────────────────────────────────────────────────────────────────┤
│ STAGE 3    integrate: design FreeRTOS tasks(3.1) →                   │
│            main.c state machine(3.2) → verify logic                  │
├──────────────────────────────────────────────────────────────────────┤
│ STAGE 4    revise: locate → patch → re-verify                        │
└──────────────────────────────────────────────────────────────────────┘
Shared: compile/serial failure → classify → directed fix (see Failure Handling)
```

## Hard Rules

| # | Rule |
|---|------|
| H1 | ESP-IDF only — no Arduino abstraction |
| H2 | User provides IDF version + install path; all APIs must match that local version |
| H3 | `main.c` calls semantic interfaces only (`network_connect()` etc.); IDF calls stay inside subsystem libs |
| H4 | Datasheet = sensor ground truth; LLM memory = draft accelerator only; one mismatch → void entire draft |
| H5 | Register errors don't compile-fail; validate before writing code — hardware verify is the only gate |
| H6 | On failure: classify → directed source → fix. Never paste error to LLM to guess |
| H7 | Subsystems in dependency order; verify each before starting the next |
| H8 | Before drafting a subsystem, check project memory for existing gotchas on that part/domain — a documented lesson unread is a bug re-earned |

## Stops — the only three; everything else proceeds

| Stop and ask the user | NOT a stop — keep going |
|---|---|
| STAGE 1 spec review (mandatory checkpoint) | compile / flash failure → fix-verify loop |
| Datasheet unobtainable after the full fetch cascade | `fetch_datasheet` failing one source → cascade handles it |
| Hardware preflight FAIL (env problem) | registry / tool error → degrade, don't ask |

Build all subsystems in one pass; auto-judge Tier A/B inline; batch genuine Tier C to
STAGE 3 end. Don't stop after each subsystem.

## Inputs

| Field | Required | Notes |
|---|---|---|
| `requirements/<project>.md` | yes | desired logic; user-written, never rewrite |
| `connections/<project>.md` | yes | pin wiring; user-written, never rewrite |
| IDF version + install path | yes | from `activate.local.ps1`; H2 applies |
| project dir | yes | `projects/<project>/`; pass as `-C <dir>` to idf.py |
| serial port | flash/monitor | e.g. `COM4` |

## Step 0 — Environment

The user has already activated ESP-IDF and launched Claude from that shell (CLAUDE.md
"Starting a project"). But every tool-call shell starts fresh and un-activated, so **run
`idf.py` only through the self-activating wrapper** — never bare `idf.py` (in Bash it hits a
bogus launcher reporting v1.0.3):

```powershell
powershell -ExecutionPolicy Bypass -File tools\idf.ps1 --version   # must print v6.0
```

Confirm that prints v6.0 before the first build. If it errors, `activate.local.ps1` is
missing/broken — stop and ask the user; never guess install paths.

## Step 0.5 — Hardware Preflight

```powershell
.\hwtest\Test-Hardware.ps1 -Port <PORT>          # activates + runs the gate
# already activated? python hwtest\preflight.py --port <PORT> --chip <chip>
```

PASS → port/flash/serial confirmed; treat all future failures as firmware bugs.
FAIL → hardware/environment problem; surface to user, don't start writing firmware.

---

## STAGE 1 — Spec Synthesis

Generate `projects/<project>/spec.md` from requirements + connections file:

| Module | Content |
|--------|---------|
| ① Hardware list | Part / model / interface / role |
| ② Pin table | Normalized from requirements |
| ③ Core logic | Structured restatement of requirements |
| ④ State machine | LLM-derived control flow (highest misread risk — must be reviewed) |
| ⑤ Subsystems | Each tagged `[MCU-native]` or `[External Sensor]` |
| ⑥ Verify plan | Each subsystem assigned tier A / B / C |

**Verification tiers:**

| Tier | Condition | Example |
|------|-----------|---------|
| A | Self-verifying — data itself proves correctness | Write-read-back; ID register matches datasheet value |
| B | Plausibility range from board profile baseline | Mic: silent≈0, voiced≠0 |
| C | Requires user physical confirmation | LED actually lit; camera frame correct |

**Assigning a tier** — ask in order, stop at first yes:
1. Can the data self-prove? (ID reg = datasheet value / write-read-back / a **digital GPIO
   reads back its own level**) → **A**
2. Has a plausible range from a baseline? (mic silent≈0 voiced≠0; temp 15–30°C) → **B**
3. Only a human can judge physical reality? → first try to downgrade (test-pattern register,
   write-read-back, baseline range); truly unavoidable → **C**, batched to subsystem end, y/n via serial.

Prefer A > B > C. A digital input/output is ALWAYS A (read the pin level) — never make the
user press a button to "verify" it.

*Crumb: BQ25180→A(device ID), W25N01GV→A(write-read-back), ICS-41350→B(PCM range), Button→A(read level)*

> **[MANDATORY CHECKPOINT]** User must review `spec.md` before STAGE 1.5.
> LLM may misread requirements (e.g. two mics → two I2S buses). Wrong spec = wrong everything.

---

## STAGE 1.5 — Datasheet Prefetch

Batch-fetch a REAL, text-extractable PDF for every `[External Sensor]` in spec.md:

```bash
python tools/fetch_datasheet.py <PART> --out projects/<project>/components/<sensor>/
```

The tool cascades `known-URL table → manufacturer direct → DigiKey API`, and validates
every candidate (must be a PDF, have a real text layer, not be a "quality certificate").
- **DigiKey is the broad catch-all (~9/9 parts)** — set it up once per shell:
  `$env:DIGIKEY_CLIENT_ID=...; $env:DIGIKEY_CLIENT_SECRET=...` (creds are user-supplied,
  never committed). Verify coverage with `--report` (tries every source, doesn't stop).
- Fetched → stash in `components/<sensor>/`, mark "PDF ready" — **do not read yet**.
- `[FAIL]` after cascade → ask user to drop the PDF in manually; block before STAGE 2.
  If it's a stubborn part (unpredictable filename / JS search page) whose URL you later
  find by hand, add it to `KNOWN_URLS` in `tools/fetch_datasheet.py`.

Purpose: surface datasheet availability risk before any code is written. Never trust a
downloaded PDF by magic bytes alone — the old LCSC scrape returned valid-but-useless
"quality certificate" PDFs. Content validation is mandatory (the tool does it).

---

## STAGE 2 — Per-Subsystem Implementation

```
2.0  Classify
      │
      ├─ [MCU-native]                          ├─ [External Sensor]
      │  WiFi / NVS / I2S / GPIO / HTTP…       │  sensors / PMIC / ext-flash…
      ▼                                         ▼
2.A  Knowledge (high → low priority)        2.B  Knowledge (decision tree)
     ① IDF examples  — compile-correct,          2.B.1  search_components("<part>")
        version-matched; copy-adapt                      found  → read example/README → 2.C
        ls $IDF_PATH/examples/<feature>/                 not found → 2.B.2
     ② Local headers — exact signatures           2.B.2  Fetch PDF (cascade, stop at first ✓):
        grep -rn "<fn>" $IDF_PATH/components/            a) components/<sensor>/ has PDF → use
     ③ esp-docs MCP  — conceptual ref only        b) python tools/fetch_datasheet.py <PART>
        ⚠ version may differ from local                  c) failed → block, ask user to supply
     ④ LLM memory    — direction only              2.B.3  Extract + fill datasheet_notes.md:
        any signature must be confirmed by ①②            python tools/extract_pdf.py <PART>.pdf
                                                          Read: Pin Config / Elec Char & Timing /
      │                                                     Register Map / Detailed Desc / App+Impl
      │                                           Skip: Layout / Mech / Pkg / Ordering / Rev Hist
      └─────────────────┬───────────────────────────────────────┘
                        ▼
             2.C  Write library
                  [MCU-native]:  wrap IDF calls → semantic interface  (<sub>.h + <sub>.c)
                  [External]:    implement driver from datasheet_notes.md
                                 not from raw PDF, not from memory
                        ▼
             2.D  Write example  (call chain + selftest, combined)
                  Correct init order, our pin config, expected output
                        ▼
             2.E  Verify on hardware  (tier A / B / C)
                  build → flash → run example → judge vs baseline
                  Pass → next subsystem
                  Fail → Failure Handling → fix → back to 2.C
                  ★ Register errors only surface here, not at compile time (H5)
```

**Build + flash** (`W` = `powershell -ExecutionPolicy Bypass -File tools\idf.ps1`):
```bash
W -C <project> set-target <chip>   # first build / on target change only
W -C <project> build               # run in the BACKGROUND
grep -nE "error:|error |undefined reference|fatal error" <project>/build/log/idf_py_stderr_output_* | head
W -C <project> -p <PORT> flash      # only after a clean build
```

**Monitor (esp-serial MCP):**
```
monitor_boot(wait_for="app_main")
monitor_start → monitor_send("SELFTEST") → monitor_read(timeout=5, wait_for="...") → monitor_stop
```

**datasheet_notes.md** — format + memory-draft rule in `references/datasheet-notes-format.md`.
One per sensor, distilled from the validated PDF; it is the driver's only ground truth (H4).

**Subsystem output layout:**
```
components/<subsystem>/
  ├── <subsystem>.h    — semantic API (what main.c calls)
  ├── <subsystem>.c    — implementation
  └── example/         — call chain + selftest
```

No datasheet after asking user → `#error "unverified register map — drop <PART>.pdf into components/<sensor>/"`.

---

## STAGE 3 — Integration

### 3.1 Task Architecture (design before writing main.c)

Read each subsystem example. Plan FreeRTOS tasks first. Signals a subsystem needs its own task:

- **Blocks** on I/O (button / WiFi / DMA) — can't live in main loop
- **Real-time** rate requirement (audio capture, camera)
- **Rate mismatch** between producer and consumer → queue
- **Long operations** (network upload, flash erase) that would block real-time paths

Tasks communicate via queues or event groups. No bare shared variables across tasks.
*(Priority values and stack sizes are determined by profiling — not hardcoded here.)*

### 3.2 Implement + Verify

`main.c` state machine calls semantic interfaces only (H3). No IDF calls in main.c.
STAGE 2 selftest only proves a subsystem in its own **isolated call pattern** (one-shot
read, single write). If STAGE 3 puts that same call in a **tight loop inside a task**
(continuous capture, streaming write), that's a new, unverified call pattern. Add a
quantitative selftest for it: log expected-vs-actual throughput/count (e.g. "held Ns →
captured N bytes, expect ~N·byte_rate") and mark OK/FAIL like any other Tier A/B check.
A polling loop that also blocks on an unrelated debounce/delay call is a common way to
starve a producer without crashing — count reads/failures, don't just watch for "no crash".

**Default: do not modify subsystem internals during integration.**
On integration failure, determine first:
- Logic error in main.c → fix main.c
- Missing subsystem interface → return to STAGE 2, add interface, re-verify subsystem, return

Full-system pass → flip the self-test Kconfig default off (see Self-test section) before
declaring STAGE 3 done.

---

## STAGE 4 — Revision

Handles a bug **or** a new non-functional requirement (speed / power / stability the spec
didn't cover, e.g. "upload is too slow"):
1. Locate — subsystem-internal, or main.c logic?
2. Non-functional requirement → set a **quantified acceptance baseline first** (e.g.
   "upload 1 MB < 10 s"); without it "done" is unprovable.
3. Targeted patch → re-verify the affected part only (subsystem selftest, or the metric).
4. Interface unchanged → re-integration is free; interface changed → re-verify integration.

---

## Failure Handling (STAGE 2 + 3 shared)

**Compile:**

| Pattern | Cause | Fix |
|---|---|---|
| unknown function / wrong args | API from memory, wrong version | `grep -rn "<fn>" $IDF_PATH/components/` → use actual signature |
| undefined reference | missing CMakeLists dependency | add component to `REQUIRES` |
| implicit declaration | missing `#include` | find header for that function, add it |

**Runtime (serial):**

| Symptom | Layer | Fix |
|---|---|---|
| No output at all | Boot crash | Check boot log / panic trace |
| All-zero or full-scale | Data bug (STAGE 2 should have caught this) | Return to 2.E |
| Data plausible, wrong state flow | State machine logic | Fix main.c |
| Watchdog reset / stutter / drops | FreeRTOS task issue | Add `vTaskDelay`; fix blocking calls; check priorities |
| Data corruption across tasks | Missing synchronization | Add queue or mutex |

---

## Self-test

Kconfig gate — code stays, only the default flips:

```kconfig
config APP_SELFTEST
    bool "Per-subsystem bring-up self-tests"
    default y
```

`default y` while STAGE 2/3 are still verifying. Once full-system verification (every
subsystem + integration) is confirmed within baseline, **flip `default y` → `default n`
and reflash as the last STAGE 3 action** — delivered firmware boots silently by default.
Never delete the `#if CONFIG_APP_SELFTEST` blocks or the functions; `idf.py menuconfig`
flips it back on for any future debugging (STAGE 4).

```c
#if CONFIG_APP_SELFTEST
    // per subsystem: exercise → print "expected <baseline> / actual <value>"
#endif
```

**Persistent-storage selftests** (writes to flash/NVS) must delete exactly the entry
they created, by the handle/index returned at creation — never by "assume I'm the
newest/oldest". Dev boards reboot repeatedly against the same persistent state within a
session; a wrong assumption leaks junk entries that can permanently jam a downstream
consumer (e.g. an uploader that drains oldest-first forever).

## Outputs

| Path | Purpose |
|---|---|
| `projects/<name>/spec.md` | structured requirements + subsystem plan (STAGE 1) |
| `components/<name>/datasheet_notes.md` | verified register map + init sequence (STAGE 2) |
| `projects/<name>/components/<name>/` | subsystem lib + example |
| `projects/<name>/main/` | state machine only — no IDF calls |
| `projects/<name>/managed_components/` | registry pulls (git-ignored) |
| `projects/<name>/build/log/` | build logs — cite path, never paste whole |
| `projects/<name>/DEVLOG.md` | decisions + results; see `references/devlog-format.md` |
| `projects/<name>/DEVLOG-stats.md` | per-stage time + token report (`tools/session_stats.py`) |

## Response

Lead every stage with a `STAGE:` line as its FIRST output — it both structures the reply
and marks the stage boundary that `session_stats.py` segments the cost log on. The cost
report is generated **on demand only** (user trigger, see CLAUDE.md), never automatically.

```
STAGE: <1 | 1.5 | 2:<subsystem> | 3 | 4>
Build: PASS (<chip>)   Flash: COM<n>
Selftest: <N/N subsystems within baseline>
  <subsystem>: expected <baseline> / actual <value> → OK | FAIL
Logic: <requirements met? one line>
DEVLOG: updated
```

Early stop:
```
Stopped at STAGE <N>: <reason>
Need from you: <one specific thing>
```

## Failure Signals

| Signal | Meaning | Action |
|---|---|---|
| `idf.py: command not found` / version `1.0.3` | Ran bare `idf.py` in Bash | Use `tools\idf.ps1` wrapper instead |
| Build: unknown function / arg | API written from memory | `grep` signature in `$IDF_PATH/components/`; fix |
| SoC peripheral wrong behavior | SoC API from memory | Check IDF examples + local headers |
| `search_components` → `Failed to fetch the components` | Zero matches for THIS query (not registry down) | Re-search by capability, not part number (`bq25180`✗ → `battery charger`✓); found official driver → use it, don't hand-write |
| `add-dependency` fails | Wrong namespace | Re-`search_components`; none → need datasheet |
| No PDF, register code needed | Blocked | Ask user; leave `#error` stub |
| Serial all-zero or full-scale | Not "working" — data bug | Check profile `stuck_values`; return to STAGE 2 |
| Flash OK, no serial output | Wrong port/baud or no reset | Check port; `monitor_boot` resets via DTR/RTS; baud 115200 |
| Watchdog reset | Task not yielding | Add `vTaskDelay`; fix blocking operations |
| `esp-docs` 401 / rate-limited | OAuth not done or quota hit | Auth once; fall back to local headers |
