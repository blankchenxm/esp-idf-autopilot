# Harness Hardening Implementation Record

## Outcome

The structural defects found during the first Crumb exercise are now enforced
by generic schema, validators, graph nodes, immutable Receipts, and negative
scenarios. No rule in this record depends on a Crumb owner, component, pin,
part, marker, URL, or artifact name. The paused Crumb project was not built,
flashed, resumed, repaired, or used to manufacture new evidence.

## Enforcement changes

| Boundary | Executable result |
|---|---|
| Hard rules | `HR-*` registry entries resolve callable validators, producer nodes, Receipt kinds, failure dispositions, negative scenarios, and downstream gates. |
| Schema | Schema 1.7 is the only current release-authoritative contract; legacy execution stops with `DESIGN_REVISION_REQUIRED`. |
| Operation authority | Typed capabilities replace owner blankets, prose inference, parameter-name matching, and inadmissible hardware policy defaults. |
| Implementation | A global completeness node plus per-image source/link checks reject missing, unreachable, unsupported, and unprobed operations. |
| Production | Static normal-entrypoint reachability is followed by selftest-off Integration observations correlated to step, operation, edge, and scenario IDs. |
| Tier C | Nonphysical artifacts require a declared producer, delivery method, correlation key, validation contract, and same-run/design/final-firmware producer Receipts. |
| Images | Component, verification row, normalized image, build, and flash counts are reported separately; compatible rows share transactions. |
| Side effects | Materialize, validate, configure, build, flash, observe, evaluate, and Evidence commit are separate resumable checkpoints. |
| Recovery | Stable lineage and relevant-material revisions replace summary-text routing; one lineage/material admits at most one model repair. |
| Release | A new root executes prepare, fullclean, configure, build, flash, observe, core production scenario, forbidden-state checks, and terminal validation. |
| Components | A typed API/resource/dependency manifest rejects low-level `main/` ownership, duplicate resources, test-only production links, and unconsumed components. |
| Lifecycle | Dead workers reconcile to `INTERRUPTED`; `.v/` roots are ignored, free-space checked, retention-bounded, and safely pruned. |

## Token and supervision changes

Repeated status/wait turns are removed from model supervision. The control
plane persists monotonic `event_seq`, coalesces unchanged progress, and wakes a
model only for typed action/human/terminal transitions. `await-event` resumes
from a sequence without replaying model turns.

Every Harness-owned model call starts from a fresh deterministic context
packet built from canonical state. Owner packets contain only the relevant
contract slice, authority hashes, source manifest, bounded diagnostics, exact
writable paths, redaction proof, and transaction/tool limits. Raw histories and
full logs stay outside the packet. Design input/output/reasoning token totals
are metrics rather than failure gates. Actual cached/uncached input, output, reasoning, tool calls,
non-model progress, and suppressed progress are available through:

```powershell
python -m orchestrator.cli report --project <project> --run-id <run-id>
```

The deterministic replay estimate is 2,400,000 input tokens versus the audited
224,078,716-token baseline, a 98.9289% reduction estimate for the modeled
automatic-turn workload and above the 55% gate. This is not a claim about a
new hardware run or a runtime token cap. The realistic whole-run target remains 75–90%; the next
clean device E2E must supply measured report data.

## Component-count clarification

ESP-IDF components are compiled and linked into a firmware image; they are not
burned one by one. Nine or ten components therefore do not imply nine or ten
flashes. The Harness now derives component boundaries from stable ownership,
resource, persistence, protocol, reuse, scheduling, or failure-isolation
value, and independently derives the minimum compatible verification images.

## Validation surface

The regression surface covers the 21 generic scenarios, 1,000 coalesced
progress ticks with zero model wake, deterministic owner-context isolation,
Design token accounting, transaction/tool limit enforcement, operation authority, unsupported stubs, production
bypass, Tier C producer binding, image sharing/isolation, worker death,
lineage stability, low-level code in `main/`, Release fullclean/behavior/
secret checks, project hygiene, migration, and crash-before/crash-after
replay for every declared side-effect Receipt operation.

Final generic validation on 2026-07-30:

```text
python -m compileall -q orchestrator tests
python -m pytest -q
313 passed
git diff --check
```

Physical ESP-IDF build/flash validation is intentionally deferred to the next
clean project run requested by the user.
