# Harness Hardening TODO

This document records framework work discovered by the first Crumb end-to-end
exercise. The work is intentionally project-agnostic. Do not encode Crumb
owners, pins, parts, markers, URLs, or component names in Harness nodes or
validators.

Crumb execution is paused as an audit fixture. Do not resume, rebuild, flash,
repair, or mutate its approved revision while completing this TODO. A later
clean Crumb run should begin only after the P0 acceptance suite passes.

## Status legend

| Status | Meaning |
|---|---|
| `DONE` | Deterministic implementation and regression coverage exist. |
| `PARTIAL` | A local invariant exists, but an end-to-end bypass remains. |
| `OPEN` | The required deterministic contract or graph boundary is absent. |

## Audit disposition

| Area | Status | Current boundary |
|---|---|---|
| Design repair preserves required rows | `DONE` | Empty repair collections no longer delete required contract rows. |
| Hidden Windows child processes and stable retry fingerprints | `DONE` | Harness-owned child windows are hidden and random workspace paths are normalized. |
| Failure traceback, serial transcript, and fatal-owner routing | `DONE` | Typed failures retain actionable evidence across adapters and recovery. |
| Targeted Datasheet reread and fact retention | `DONE` | Failed/incomplete targeted reads are not silently reused and valid prior facts survive repair. |
| Existing source reuse before implementation repair | `DONE` | Compliant source can proceed to verification without a default rewrite. |
| Operation authority | `PARTIAL` | Addenda bind operation authority, but several blanket/inferred coverage paths remain. |
| Production runtime flow | `PARTIAL` | Schema 1.6 and source-token checks exist; executable reachability and runtime proof do not. |
| Integration uses the production path | `OPEN` | A parallel selftest/simulation can still emit all expected integration markers. |
| Tier C admission and missing-artifact handling | `PARTIAL` | Missing artifacts cannot PASS, but artifacts do not yet require a declared producer/delivery chain. |
| Verification batching and flash minimization | `PARTIAL` | Frozen batches exist, but newer schemas currently fall back to singleton owner batches. |
| Worker liveness and state reconciliation | `OPEN` | A dead worker can leave a `CONTINUOUS` projection or stale active-job pointer. |
| Clean project lifecycle and generated-file hygiene | `OPEN` | Isolated build trees can pollute the worktree and consume unbounded local space. |

## P0 — Compile operation authority into one deterministic gate

### Problem

An operation name currently mixes several different concepts:

- a physical device transaction;
- an ESP-IDF peripheral/API operation;
- a host-side algorithm;
- a product-policy choice;
- a lifecycle or recovery action.

Local fixes improved the current readiness/addendum path, but these bypasses
remain:

- a receipt-bound `local_idf` selection may blanket-cover every required
  operation for an owner;
- `project_custom` owners are treated as implementation-covered from their
  design declaration;
- legacy contracts infer operations from English requirement/marker text;
- implementation facts are matched to operations mainly by
  `fact.parameter == operation`;
- source-token presence does not prove the operation is executable;
- an API can exist while returning `ESP_ERR_NOT_SUPPORTED`.

### Required contract

Add a typed operation table to the Design contract. Every operation must bind:

| Field | Purpose |
|---|---|
| `operation_id` | Stable project-local identity. |
| `owner` | Semantic implementation owner. |
| `kind` | `hardware_register`, `hardware_transport`, `idf_api`, `host_algorithm`, `product_policy`, or `system_orchestration`. |
| `risk` | Safety/destructive/reversible classification. |
| `required_capabilities` | Named API, register, protocol, or algorithm capabilities. |
| `authority_sources` | Exact Registry, Datasheet, local ESP-IDF, input, or versioned policy authorities. |
| `default_policy` | Optional explicit policy ID/version; forbidden unless admitted for this kind/risk. |
| `implementation_assertions` | Source/build/link assertions, not prose. |
| `runtime_probe` | Executable probe or verification identity proving non-stub behavior. |
| `consumers` | Runtime-flow steps and requirements that depend on the operation. |

### Required graph boundary

Add a dedicated `operation_authority` node after design binding and before any
implementation agent or hardware side effect.

The node must:

1. compile every required operation from typed contract fields only;
2. resolve each operation to exactly one admissible authority path;
3. invoke targeted readers only for named missing hardware/API facts;
4. reject blanket owner-level coverage;
5. reject legacy prose inference for new schema revisions;
6. write an immutable operation-authority Receipt and addendum;
7. stop with `INTERNAL_FAULT` when the contract is structurally incomplete;
8. route only true product/input decisions to a human design gate.

### Acceptance tests

- A custom driver declaring `covered_operations` but returning
  `ESP_ERR_NOT_SUPPORTED` cannot pass.
- One local ESP-IDF version Receipt cannot cover unrelated operations.
- A host algorithm may use only an allowlisted, versioned policy.
- Register, transport, power, erase, program, timing, and DMA operations cannot
  use a host-policy default.
- A fact about sample rate can satisfy a named capability without pretending
  its parameter name is `initialize`.
- Every authority decision is reproducible from contract and bound Receipts.

## P0 — Make production composition an executable graph contract

### Problem

Schema 1.6 requires `architecture.runtime_flow`, but current checks mainly prove
that declared symbols/tokens exist somewhere in non-selftest source. They do not
prove:

- reachability from the selftest-off application entrypoint;
- declared call/data edges between steps;
- execution order or state transitions;
- that required operations are non-stub;
- that integration invoked the same production path;
- that the user-visible output can actually be produced.

### Required contract

Extend `runtime_flow` steps with:

- typed input/output ports or messages;
- call, queue, event, storage, and protocol edges;
- startup, steady-state, failure, recovery, and shutdown transitions;
- production configuration predicate;
- operation IDs from the operation-authority table;
- executable integration scenarios and observation points;
- resource ownership, queue depth, timeout, and backpressure semantics.

### Required graph boundaries

Add these explicit checkpoints:

```text
operation_authority
  -> implementation_completeness
  -> verification_batches
  -> production_composition
  -> integration
  -> tier_c_artifact_materialization
  -> closure
  -> release
```

`implementation_completeness` must reject required operations that are absent,
unlinked, stubbed, or explicitly unsupported.

`production_composition` must build a selftest-off image and prove that the
normal entrypoint reaches the declared orchestration path. A generic READY
marker alone is insufficient.

### Integration rule

Integration may substitute controlled adapters at declared seams, but it must
invoke the production orchestrator and production component APIs. A separate
simulation that manually prints expected semantic markers is invalid evidence.

The integration executor must consume `runtime_step_ids`; declaring those IDs
only in the contract is not enough. Evidence must identify the executed steps
and bind observations to their operation IDs.

### Acceptance tests

- Symbols that exist but are unreachable from the normal entrypoint fail.
- A production operation returning `ESP_ERR_NOT_SUPPORTED` fails before Tier C.
- A selftest-only parallel flow cannot satisfy a production requirement.
- Hand-printed ordered markers without the declared step observations fail.
- Queue/storage/protocol edges omitted from source or runtime observations fail.
- The same production orchestrator is used by controlled integration and
  selftest-off runtime.

## P0 — Require a producer and delivery chain for every Tier C artifact

### Already enforced

- Tier C defaults empty.
- Missing, empty, invalid, or unconfirmed artifacts cannot PASS.
- User confirmation binds a materialized artifact hash.
- Missing physical work is not routed to an implementation rewrite.

### Remaining problem

`local_file` currently proves only that a path is syntactically actionable. It
does not prove that Integration or the production system can create that file.
A contract can therefore wait for an artifact with no producer.

### Required contract

Every non-`physical_observation` Tier C item must declare:

| Field | Purpose |
|---|---|
| `producer_phase` | Normally `integration` or an approved production-observation phase. |
| `producer_test_id` | Executable producer identity. |
| `producer_operation_ids` | Runtime operations that create the artifact. |
| `delivery_method` | Local generation, device extraction, or protocol download. |
| `correlation_key` | Device/server object ID, filename, sequence, or transaction ID. |
| `producer_receipt_kinds` | Receipts required before presenting the artifact. |
| `artifact_validation` | Media metadata, size, hash, duration, format, or other deterministic checks. |

Rules:

- `local_file` requires a declared Harness/integration producer.
- `download_url` requires a successful protocol Receipt and correlation key.
- device extraction requires an executable adapter and extraction Receipt.
- `physical_observation` must not claim a file artifact.
- materialization cannot interrupt the user until producer Receipts exist.

### Acceptance tests

- A local path with no producer is rejected during Design validation.
- A server artifact without a matching upload/object Receipt is rejected.
- A stale artifact from another run/design/firmware is rejected.
- The user is shown only an artifact produced by the final affected integration
  image and bound to its hash.

## P0 — Decouple component count from build/flash count

### Principle

An ESP-IDF component is a source ownership and dependency boundary. It is not a
firmware image and must not imply one flash transaction.

Keep four separate concepts:

| Concept | Meaning |
|---|---|
| Component | Source/API ownership boundary. |
| Verification row | One expected/actual verdict and Evidence object. |
| Verification batch | Compatible rows sharing one build/flash/serial transaction. |
| Setup image | Unique Kconfig/stimulus configuration requiring a separate firmware image. |

### Current defect

The runtime batch helper groups owners only for schema 1.2/1.3. Newer schema
revisions can fall back to singleton owner batches, causing unnecessary
build/flash cycles.

### Required behavior

1. Normalize batches for every current schema version.
2. Compute image identity from source digest, Kconfig overrides, setup,
   isolation, hardware resources, and stimulus adapter.
3. Share one build/flash/capture Receipt across all compatible rows with the
   same image identity.
4. Preserve one independent Evidence verdict per row/owner.
5. Isolate only destructive, attribution-ambiguous, register/DMA/audio/storage,
   incompatible-resource, or distinct-setup work.
6. Reuse a successful exact image Receipt only when artifact hashes still
   match.
7. Run one production integration image and one fresh selftest-off release
   image after affected component batches.

### Acceptance tests

- Schema 1.6 batch-compatible owners share one flash.
- Different Kconfig overrides never share an image.
- One failed row does not invalidate unrelated Evidence from the same capture,
  but repair re-verifies the affected owner and consumers.
- Component count can increase without linearly increasing flash count.
- The run report shows components, rows, image identities, builds, and flashes
  as separate counts.

## P1 — Derive component boundaries without a fixed target count

Do not set a global target such as “9–10 components.” A small project may need
three production components; a hardware-rich product may need more.

Create a component only when at least one boundary is stable and independently
valuable:

- external device/register/transport ownership;
- shared board resource arbitration;
- persistent data ownership and cleanup;
- external protocol/security boundary;
- reusable transformation/algorithm;
- independently scheduled lifecycle or failure-isolation boundary.

Do not create a production component merely for:

- one small helper with no independent state/resource/failure boundary;
- a test marker;
- a parallel integration simulation;
- an ESP-IDF facility already internal to one owner;
- a product-policy fragment used only by the one application orchestrator.

Design validation should flag:

- test-only components linked into selftest-off production;
- `main` listing every component and masking missing transitive dependencies;
- components with no production consumer;
- duplicate owners over one resource/state boundary;
- tiny policy/helper owners that add no independent lifecycle or verification
  value.

The Design provider may propose boundaries, but a deterministic
`component_architecture` validator must accept or reject them before approval.

## P1 — Make worker liveness and project lifecycle durable

### Worker reconciliation

- `status` must reconcile the active job pointer, process liveness, job file,
  checkpoint cursor, and projection.
- A dead nonterminal worker becomes `INTERRUPTED`, never indefinitely
  `CONTINUOUS`.
- A stale active-job pointer is replaced only through an atomic control
  transaction.
- Resume must select the exact nonterminal thread or require `--thread-id`.
- Add crash-before-node, crash-after-side-effect, and stale-PID tests.

### Generated-file hygiene

- Ignore the Harness verification build root explicitly.
- Give isolated build trees a retention/archival policy.
- Report local disk usage before starting an expensive new run.
- Never stage runtime, raw Evidence, build caches, secrets, or private config.
- Preserve approved packages and immutable Receipt/Evidence indexes through
  snapshot/reset/restore.

## P1 — Replace local fixes with scenario-level regression coverage

Retain unit tests, but add generic end-to-end Harness fixtures that reproduce
the structural failures without Crumb-specific identifiers:

1. custom hardware driver with missing transport facts;
2. component APIs present but returning unsupported;
3. normal runtime with an empty orchestrator;
4. integration simulation that bypasses production;
5. Tier C local artifact with no producer;
6. several compatible components sharing one setup image;
7. incompatible setup requiring a second image;
8. dead worker with a nonterminal checkpoint;
9. source reuse followed by one affected-consumer repair;
10. fresh selftest-off release bound to closure and one firmware hash.

The final scenario must reach `COMPLETE`. Every negative variant must stop at
its owning deterministic checkpoint before an invalid downstream side effect.

## Definition of done before restarting Crumb

- All P0 schema, validator, graph, adapter, Receipt, and Evidence changes exist.
- Graph topology tests prove the new checkpoint order and recovery routes.
- Resume/idempotency tests cover every new side-effect boundary.
- Generic negative scenarios cannot reach Integration, Tier C, or Release.
- Generic positive scenario reaches terminal `COMPLETE`.
- Schema 1.6 compatible owners demonstrate shared build/flash transactions.
- A production-path integration test proves it cannot be replaced by a parallel
  marker simulation.
- A Tier C artifact demonstrates a valid producer/delivery/correlation chain.
- Dead-worker status reconciliation is deterministic.
- Generated verification build roots are ignored and bounded.
- Documentation describes implemented behavior; it is not the enforcement
  mechanism.
- Only after these checks pass should the old Crumb namespace be snapshotted or
  removed and a clean Crumb design/run begin from user-owned inputs.
