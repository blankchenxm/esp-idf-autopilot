# Execution Harness

The runner uses one durable SQLite checkpointer per project at
`runtime/projects/<project>/checkpoints.sqlite`. A stable `thread_id/run_id` resumes the same run
across tool calls, context compaction, process restarts, and new Codex sessions. Agent workspaces,
Design-provider scratch, locks, thread registry, and active-thread pointer share that project
namespace; equal revision numbers in different projects cannot collide.

```text
initialize
 -> environment_and_hardware_preflight
 -> design_subgraph
 -> spec_approval_interrupt
 -> invariant_gate
 -> current_schema_gate
 -> operation_authority
 -> bind_approved_design_and_hardware
 -> verification_image_plan
 -> implementation_materialize
 -> implementation_completeness
 -> source_validate -> configure -> build -> flash -> observe -> evaluate -> evidence_commit
 -> component_architecture
 -> production_composition
 -> integration_prepare -> configure -> build -> flash -> observe -> evaluate -> evidence_commit
 -> tier_c_artifact_materialization
 -> tier_c_interrupt (conditional)
 -> requirements_closure
 -> release_prepare -> fullclean -> configure -> build -> flash -> observe -> validate
 -> finalize
```

`schemas/harness-invariants.json` is the hard-rule authority. Each `HR-*` row names its
producer, callable validator, required Receipt kinds, negative scenarios, disposition, and
protected downstream gates. `invariant_gate` resolves every validator implementation and checks
graph reachability, dominance, state-guarded branches, recovery edges, and the transaction chain.
Closure consumes registry IDs, not a separately maintained prose checklist.

Only schema 1.7 is executable for a new release-authoritative run.
`schemas/schema-capabilities.json` is the sole feature-admission matrix. Older packages remain
readable for audit, but Execution returns typed `DESIGN_REVISION_REQUIRED`; migration creates an
unapproved revision plus `migration-report.json` and never edits the approved legacy package.

Revision mode creates a new design revision, computes affected components/consumers/tests, redoes
only affected bring-up/integration work, then always performs current-revision closure and a fresh
selftest-off release.

## Failure policy

| Disposition | Response |
|---|---|
| `REPAIR_INTERNAL` | diagnose, smallest owning patch, verify material changed, then full affected reverify |
| `RETRY_TRANSIENT` | cleanup/re-probe and bounded retry |
| `WAITING_HUMAN` | stop only at the declared Spec or Tier C interrupt |
| `HARD_EXTERNAL_BLOCKER` | persist evidence and one exact required external change as `BLOCKED` |
| `INTERNAL_FAULT` | persist an actionable `FAULTED` result; never report it as user blockage |

Every executable node is wrapped by a failure boundary. Adapter failures preserve their typed
`Failure` and raw Receipt end to end; they are not flattened to a summary and reclassified.
Each boundary adds a typed `Diagnostic` containing cause, disposition, severity, responsible
party, owner/test scope, material fingerprint, failure fingerprint, and evidence. `recover`
routes only on those fields. It performs serial cleanup/hardware re-probe or an owner-directed
implementation patch, verifies that material actually changed, then retries the failed checkpoint
node. Retry budgets are category-specific. Repeating the same normalized failure without a
material change produces `FAULTED/internal_stall`, not `BLOCKED` or another blind retry.

Every external effect class has its own checkpoint: materialize, completeness/source validation,
configure, build, flash, observation, evaluation, and Evidence commit. Integration and Release
reuse those primitives with typed scopes. Observation never writes a PASS verdict. Each Receipt
stores both its idempotency hash and the canonical authority tuple that produced it: run, approved
design digest, hardware identity, relevant material fingerprint, operation, and exact scope.
`run-state.json` is atomically regenerated from checkpoint and committed facts for the legacy Stop
hook; an agent never writes it.

Build, flash, and serial transactions carry a key derived from run/design authority, hardware,
material fingerprint, owner/test scope, and firmware hash. On node replay the adapter reuses only
an exact successful Receipt whose artifacts still match their bound hashes; failed, legacy,
different-material, or tampered transactions execute again. Build Receipts bind the application
binary and firmware hash. Integration maps each verification row to exactly one integration test,
so `N` captures cannot generate `N×M` duplicated evidence.

Implementation agents receive a digest-bound owner contract view, not the complete contract.
Immediate dependencies/consumers and relevant architecture are included; unrelated owners and
history are excluded. They also receive the immutable implementation addendum. A frozen Registry
component/version must be adopted behind the semantic owner wrapper; custom code is limited to
uncovered operations. Failure context is selected from current-run failure Receipts by owner.

`operation_authority` compiles only the schema 1.7 typed operation table. Owner blanket coverage,
English-text inference, parameter-name matching, and product-policy defaults for hardware
operations are forbidden. `implementation_completeness` rejects missing symbols, unlinked
assertions, explicit unsupported stubs, and absent runtime probes before configure or flash.
Targeted readers may fill only named capability gaps and bind their immutable provider Receipts.

Components, verification rows, images, and flash transactions are separate concepts. Compatible
rows share an image identity computed from source, Kconfig, setup, isolation, resources, and
stimulus; every row still receives independent Evidence. Component count therefore does not imply
flash count. Incompatible or destructive setups remain isolated.

Schema 1.3 adds deterministic pre-flash source/contract assertions for
grounded implementation facts. It permits only explicitly `batch_compatible`
adjacent owners to share a batch; isolated, destructive, register, DMA, audio,
and storage owners remain independent by contract. Optional
`release.early_smoke` performs an isolated selftest-off normal-runtime
build/flash/boot after component bring-up and before integration. It is
diagnostic only: final release remains a fresh closure-bound transaction.

Official LangGraph persistence stores state per thread at each graph step, enabling fault-tolerant
resume. Human interrupts require the same thread ID and restart their node, so all code before an
interrupt is idempotent.

Control supervision is event driven. `event_seq` is monotonic, repeated progress is coalesced, and
only `MODEL_ACTION_REQUIRED` or a terminal/human transition can wake a model. `status` is read-only;
`await-event --after-seq N` blocks in the control plane without creating recurring model turns.
Every Harness-owned model call starts from a fresh deterministic `ModelContextEnvelope`, is scoped
to one node/owner, stores only bounded log excerpts plus artifact hashes, and enforces byte, token,
reasoning, and tool-call budgets. `orchestrator.cli report` separates model/cached tokens,
model-versus-runner tool calls, non-model progress, and suppressed progress.

`start` and `resume` launch a detached project-scoped execution worker under
`runtime/projects/<project>/execution-jobs/`. The caller returns immediately; the worker runs until
`COMPLETE`, a typed terminal failure, or the next Spec/Tier C interrupt. Spec and Tier C both
project their waiting mode before raising the interrupt, so status never presents a waiting graph
as generic continuous work.

Status also reconciles the active pointer, job record, worker PID, checkpoint database, selected
thread, and compatibility projection. A dead nonterminal worker becomes `INTERRUPTED` and writes
an immutable reconciliation record; ambiguous nonterminal threads require `--thread-id`.

Closure requires current-run PASS evidence for every requirement *and* every declared A/B
verification identity, integration-test identity, and Tier C identity. Broad requirement coverage
cannot hide a skipped test. It also revalidates every required implementation addendum, source
Receipt hash, operation coverage, and source assertion; integration and release repeat that check.

Production composition is not a marker check. Static entrypoint/call/edge analysis is followed by
a selftest-off Integration image that must emit correlated typed step/operation/edge observations
from the declared production orchestrator. Tier C materialization starts only after producer
Receipts bind the same run, design, final Integration firmware, test, operation, delivery method,
and correlation key.

The thread registry records every project/revision thread and terminal mode. Default resume selects
the sole nonterminal thread; ambiguity requires `--thread-id`. COMPLETE clears live blocker/failure
fields. An evidence correction is append-only and demotes a contradicted terminal projection.

Current-run artifacts stay directly readable. `archive-runs` compresses only historical failed-run
raw logs/build trees, keeps Receipt/Evidence facts, and writes a hash-bound archive index.

Verification roots live under ignored `projects/<project>/.v/`, are retention-bounded, and are
checked with local free space before expensive work. Harness cleanup resolves every deletion below
that exact root and never prunes approved packages or immutable Receipt/Evidence indexes.

Project lifecycle operations are recoverable control transactions. `snapshot-project` writes an
external hash manifest plus project/runtime copies without copying user-owned inputs or shared
Datasheet objects. `reset-project` refuses changed inputs/state and moves the verified originals
under the snapshot. `restore-project` never overwrites an existing project/runtime namespace.

## Explicit pause and durable resume

`python -m orchestrator.cli pause --project <project> --reason "..."` is a control transaction,
not a process kill or a hand edit of `run-state.json`. It records the one pending safe graph node in
the LangGraph checkpoint, clears its scheduled work through the graph state API, then regenerates
the compatibility projection as `PAUSED`. `resume` dispatches that recorded node with a
`CONTINUOUS` update. It never injects a new initial state into an existing checkpoint.

An approved legacy revision without `execution_role` remains readable with the conservative
default `component`; new Design Packages must declare `component` or `integration` explicitly.
The component loop filters by that role, never by a special subsystem name. Tier C uses its own
test identity, expectation, and confirmation evidence; it can supplement an A/B requirement
without reclassifying or overwriting the automated evidence row.
