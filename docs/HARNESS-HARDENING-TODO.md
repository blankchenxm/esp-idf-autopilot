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
| Operation authority | `DONE` | Schema 1.7 typed operations, exact capability authorities, non-stub/source/link probes, and an immutable authority addendum execute before implementation or hardware work. |
| Production runtime flow | `DONE` | Static entrypoint/edge reachability is followed by selftest-off runtime step/operation/edge observations. |
| Integration uses the production path | `DONE` | Integration rejects firmware-selftest setup and requires the declared production entrypoint, APIs, and correlated observations. |
| Tier C admission and missing-artifact handling | `DONE` | Every nonphysical item has a producer/delivery/correlation contract and final-image Receipt chain before interruption. |
| Verification batching and flash minimization | `DONE` | All current-schema rows normalize to compatible image identities independently of component count. |
| Worker liveness and state reconciliation | `DONE` | Status atomically reconciles pointer/job/PID/checkpoint/thread/projection and records dead workers as `INTERRUPTED`. |
| Clean project lifecycle and generated-file hygiene | `DONE` | Ignored `.v/` roots are free-space checked, retention-bounded, and pruned only below their resolved project root. |
| Hard-rule registry and graph conformance | `DONE` | `harness-invariants.json` binds callable validators, producers, Receipts, scenarios, dispositions, and protected graph gates. |
| Side-effect checkpoint granularity | `DONE` | Materialize, completeness/source, configure, build, flash, observe, evaluate, and Evidence commit are separate authority-bound idempotent nodes. |
| Typed recovery and lineage retry budget | `DONE` | Untyped faults cannot reach agents; stable lineage/material ledgers allow one model repair and invalidate only declared descendants. |
| Model polling and progress observation | `DONE` | Monotonic/coalesced control events wake models only for meaningful typed transitions; `status` is read-only. |
| Model context minimization | `DONE` | Fresh digest-bound owner packets enforce scope, redaction, byte/token/reasoning/tool budgets, and bounded artifact excerpts. |
| Release cleanliness and production behavior | `DONE` | Explicit fresh-root fullclean/configure/build/flash/observe/validate nodes require a core production scenario and reject forbidden states. |
| Schema enforcement and legacy migration | `DONE` | The capability matrix admits only schema 1.7 for new authoritative runs; migration creates an unapproved reported revision. |

## Implementation completion record

All framework items in this TODO are implemented in the generic Harness. The
machine-checkable evidence is:

- registries: `schemas/harness-invariants.json`,
  `schemas/harness-scenarios.json`, and `schemas/schema-capabilities.json`;
- graph/runtime: explicit transaction nodes in `orchestrator/graph.py`,
  authority-bound keys in `orchestrator/transactions.py`, typed recovery in
  `orchestrator/failure_lineage.py`, and worker reconciliation in
  `orchestrator/execution_jobs.py`;
- deterministic validators: operation authority, production composition,
  Tier C producer chain, component architecture, image normalization,
  generated-root hygiene, Release runtime, and graph conformance;
- regression fixtures: control events, model context/accounting, hardening
  scenarios, transaction crash matrix, topology, Design migration, adapters,
  storage integrity, Release negative gates, and run reporting;
- final generic suite: `313 passed`; Python compilation, JSON parsing, graph
  conformance, and `git diff --check` also pass;
- token replay: `benchmarks/harness-token-replay.json`; this is a deterministic
  budget ceiling, while a later clean physical E2E must record measured usage;
- human audit summary: `docs/HARNESS-HARDENING-IMPLEMENTATION.md`.

This completion does **not** authorize resuming the old Crumb run. Its source,
approved package, runtime, hardware, and user inputs remain an untouched audit
fixture until the user starts a new clean test.

## Ordered implementation plan

The order below is mandatory. It prevents another real hardware run from paying
the model/token cost of discovering framework defects interactively.

### Phase 0 — Remove model polling and bound model context first

1. Implement state-change event delivery without model polling.
2. Implement deterministic node-specific model context packets.
3. Add model-call, input-token, output-token, and tool-call accounting by node.
4. Prove the Phase 0 acceptance fixtures before implementing the remaining P0
   work with model assistance.

### Phase 1 — Compile authority and graph invariants

1. Add the hard-rule registry and graph-conformance validator.
2. Add typed operation authority and implementation completeness.
3. Split large side-effect nodes into resumable transactions.
4. Require the current schema or an explicit approved migration.

### Phase 2 — Prove production, Tier C, and release

1. Add executable production composition and production-path Integration.
2. Add Tier C producer/delivery/correlation contracts.
3. Normalize verification images and compatible batching.
4. Add typed, lineage-bounded recovery.
5. Add clean, behavior-bearing release verification.

No phase may be considered complete from documentation or prompt changes alone.
Each item requires schema/code, deterministic validation, Receipts/Evidence
where applicable, and negative regression scenarios.

## P0 — Eliminate model polling from run supervision

### Problem

The supervising model currently participates in progress polling:

```text
model -> status -> model -> wait -> model -> status
```

Every tool return is a new model boundary and can resend a large cached context.
An unchanged build, flash, serial capture, Design job, or execution cursor does
not require reasoning and must not wake a model.

### Required control-plane behavior

Add a durable state-change subscription boundary owned by the runner/control
plane, not by a conversational model:

1. Persist a monotonic `event_seq` for control-state transitions.
2. Separate `PROGRESS` events from `MODEL_ACTION_REQUIRED` events.
3. Stream or display progress directly to the client without creating a model
   turn.
4. Wake a model only for:
   - a new typed implementation/design task;
   - a new typed failure that admits model repair;
   - `WAITING_SPEC`;
   - `WAITING_TIER_C`;
   - `BLOCKED` or `FAULTED` requiring explanation;
   - terminal validation.
5. Coalesce repeated progress for the same node/image/operation.
6. Never use timeout expiry or unchanged status as a model wake reason.
7. Provide one blocking/event-driven CLI or API operation that returns only at
   a meaningful transition; internal heartbeat/polling remains non-model code.
8. Update the firmware skill and run-control documentation so agents never
   implement recurring `status`/`wait` loops.

### Required accounting

Every run report must distinguish:

- model calls by node and reason;
- cached and uncached input tokens;
- output and reasoning tokens;
- progress events delivered without a model;
- suppressed/coalesced progress events;
- tool calls initiated by a model versus by the deterministic runner.

The first Crumb audit is the optimization baseline, not an acceptance target:

| Metric | Audited value |
|---|---:|
| Model boundaries | 1,708 |
| Total input tokens | 224,078,716 |
| Cached input tokens | 221,088,256 (98.7%) |
| Uncached input tokens | 2,990,460 |
| Output tokens | 313,383 |
| Automatic turns without new user input | 60 |
| Input tokens in automatic turns | 189,217,068 |
| Monitor/diagnosis-only automatic input | 58,617,121 |
| Wait calls | 664 |
| Status/projection reads | 455 |

Token reduction must come from fewer model boundaries and smaller packets, not
from hiding usage fields, dropping required evidence, or weakening validation.

### Acceptance tests

- A synthetic 30-minute build with 1,000 unchanged progress ticks causes zero
  model calls between node start and its terminal state change.
- A serial capture with periodic heartbeats causes zero model calls until PASS
  or a typed failure.
- One typed failure produces at most one model wake for its admitted repair
  transaction.
- Reconnecting a client resumes from `event_seq` without replaying model turns.
- `status` is read-only and never schedules an automatic model continuation.
- The generic E2E fixture records zero input tokens attributable to unchanged
  polling.

## P0 — Build a minimal deterministic context packet for every model call

### Problem

Prompt caching reduces the price and latency of repeated prefixes, but it does
not remove model requests or token accounting. Broad conversation history,
complete contracts, raw logs, repeated source listings, and unrelated Receipts
must not be resent when one owner/node needs a small decision.

### Context packet contract

Add a versioned `ModelContextEnvelope` produced deterministically for every
Harness-owned model invocation:

| Field | Purpose |
|---|---|
| `context_schema_version` | Versioned packet contract. |
| `reason` | Typed design, implementation, repair, or review reason. |
| `project`, `run_id`, `design_digest` | Exact authority identity. |
| `node`, `owner`, `operation_ids`, `test_ids` | Minimum work scope. |
| `contract_slice` | Only requirements, operations, architecture steps, and verification rows consumed by this invocation. |
| `authority_refs` | Addendum/provider Receipt IDs and hashes, not full unrelated Receipt bodies. |
| `source_manifest` | Allowed files with hashes and a bounded relevant diff. |
| `diagnostic` | One typed failure plus bounded artifact excerpts. |
| `modification_allowlist` | Exact writable project paths. |
| `budgets` | Maximum input, output, reasoning, and tool calls. |
| `redactions` | Deterministic proof that credentials/private values were excluded. |

Raw serial/build logs, complete event streams, unrelated source trees, complete
conversation transcripts, and entire design packages remain outside the packet.
The model may request a named artifact through a bounded read operation; the
request and returned excerpt become packet-linked Receipts.

### Context construction rules

1. Build packets from canonical checkpoint/contract/Receipt state, never from
   conversation memory.
2. Start each Harness-owned model transaction with a fresh isolated context.
3. Include summaries only when they are deterministic and linked to source
   hashes; never let a prior model summary become fact authority.
4. Cap each log excerpt by bytes and lines, retain the raw artifact externally,
   and include its hash/path.
5. Do not include other owners unless declared consumers are in the retry scope.
6. Return compact structured tool results; never inject full polling transcripts.
7. Record actual token/tool usage against the declared packet budget.
8. Exceeding a budget without material progress becomes a typed internal stall,
   not an automatic larger-context retry.

### Initial budgets and measurement gate

Initial limits are guardrails, not permanent model-specific constants:

- serialized non-Design context packet: at most 128 KiB;
- raw log excerpts in one packet: at most 16 KiB total;
- default owner repair: one model transaction and one bounded follow-up only
  when a new typed diagnostic is produced;
- unrelated full contract/history inclusion: zero;
- median non-Design input target: at most 40,000 tokens and at most 30% of the
  audited Crumb baseline, whichever is lower.

Adjusting a limit requires benchmark evidence and must not weaken authority or
verification.

### Acceptance tests

- Two identical state/material inputs produce the same context packet digest.
- An owner packet contains no unrelated owner source, rows, facts, or logs.
- Credential-like input never appears in a packet, prompt log, or model Receipt.
- A 10 MiB build log contributes only a bounded excerpt and artifact reference.
- Replaying a recorded packet reproduces the same allowed work scope.
- The generic E2E benchmark reduces total model input tokens by at least 55%
  from the audited baseline before the remaining P0 work begins.
- Combined with no-poll supervision, the clean stable-Harness target is a
  75–90% input-token reduction without reducing required verification.

## P0 — Register every hard rule and prove graph conformance

### Problem

Normative behavior is currently distributed across `AGENTS.md`, Skills, docs,
prompts, schemas, validators, adapters, graph nodes, and tests. A rule can be
documented and appear complete while lacking a producer, executable validator,
or blocking route.

### Canonical invariant registry

Create one machine-readable, versioned invariant registry. Documentation refers
to registry IDs; prose is never the enforcement source. Every invariant binds:

| Field | Purpose |
|---|---|
| `rule_id` | Stable Harness-wide identity. |
| `statement` | Short normative meaning for audit output. |
| `applicable_schema_versions` | Exact versions; no implicit legacy fallback. |
| `authority_inputs` | Contract/input/provider facts consumed. |
| `producer_node` | Node that creates the fact/Receipt/Evidence. |
| `validator` | Deterministic validator identity. |
| `required_receipt_kinds` | Immutable runtime proof. |
| `failure_disposition` | Typed stop/retry/repair policy. |
| `negative_test_ids` | Scenarios that must fail at the owning boundary. |
| `downstream_gates` | Nodes forbidden until the invariant passes. |

### Graph-conformance gate

Add a deterministic validator that proves:

1. every applicable invariant has exactly one authoritative producer;
2. its validator executes before every declared downstream gate;
3. required side effects cannot occur before expected values are committed;
4. no closure/release path bypasses an invariant;
5. every node failure has a typed recovery or terminal route;
6. every negative test ID exists and stops at the declared owning node;
7. docs and Skills reference implemented registry IDs rather than inventing
   independent hard rules.

CI must fail when a hard rule is added without a complete registry entry or
when graph topology moves a producer/validator after its protected side effect.

### Acceptance tests

- Removing one validator edge makes graph-conformance CI fail.
- A rule with prose and tests but no producer node is rejected.
- A rule with a producer but no negative scenario is rejected.
- Closure cannot be topologically reached when any required registry invariant
  lacks a PASS Receipt/Evidence binding.

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

## P0 — Split every side effect into a resumable transaction node

### Problem

The current subsystem and Integration boundaries combine implementation,
configuration, build, flash, serial observation, evaluation, and Evidence
creation. A late failure can replay earlier expensive work, blur ownership, and
send a Harness defect to a firmware repair agent.

### Required execution topology

Use explicit transaction nodes for every batch/image:

```text
operation_authority
  -> implementation_materialize
  -> source_validate
  -> configure
  -> build
  -> flash
  -> observe
  -> evaluate
  -> evidence_commit
```

Production composition, Integration, Tier C production, and Release use the
same transaction primitives with their own typed scopes. A node performs one
side-effect class only.

### Transaction contract

Every side-effect node must:

1. consume immutable authority IDs and predeclared expectations;
2. compute an idempotency key from only relevant material;
3. write exactly one typed Receipt, including failed attempts;
4. commit its checkpoint before downstream scheduling;
5. validate referenced artifacts by hash before reuse;
6. expose a precise recovery target;
7. clean its own process, lock, serial, and temporary resources in `finally`;
8. never manufacture PASS Evidence in the same transaction that acquires an
   unvalidated observation.

`evidence_commit` must be deterministic and consume only validated observation,
evaluation, firmware hash, hardware identity, and upstream Receipt IDs.

### Acceptance tests

- Failure after flash resumes at observation without rebuilding or reflashing
  an identical, integrity-valid image.
- Failure before a completed flash never writes observation or PASS Evidence.
- A corrupted reused artifact invalidates only its node and descendants.
- A Harness configure fault cannot invoke an implementation owner agent.
- Crash-before-side-effect and crash-after-side-effect resume tests exist for
  every transaction node.
- One node cannot emit both the raw observation and its final PASS verdict.

## P0 — Make recovery typed, lineage-bounded, and model-sparse

### Problem

Text/regex classification, broad material fingerprints, and owner-level repair
can turn infrastructure defects into firmware edits. Small Harness/source
changes may also create a new fingerprint and effectively refresh a retry
budget, allowing long repair loops.

### Typed failure contract

Every adapter and validator must return a typed diagnostic with:

- `failure_code` and `category`;
- owning node, owner, operation, test, and image identities;
- disposition: deterministic retry, model repair, external blocker, internal
  fault, or terminal failure;
- relevant material fields and their hashes;
- invalidated descendants;
- exact Receipt/artifact evidence;
- whether a model call is admitted.

An untyped exception is always `INTERNAL_FAULT`. It cannot be regex-classified
into an automatic retry or model repair. Human-readable summaries are
presentation only and never routing authority.

### Failure lineage and budgets

Create a stable `failure_lineage_id` from invariant/node/owner/operation/test
identity. Track material revisions separately.

Rules:

1. only a change to declared relevant material admits a new attempt;
2. Harness code/version changes never count as firmware-owner material;
3. the retry budget persists across run resume and minor unrelated changes;
4. deterministic transient retry and model repair have separate budgets;
5. one lineage/material pair admits at most one implementation-agent call;
6. a second model call requires a new typed diagnostic or changed relevant
   authority, not merely a different summary string;
7. unchanged/no-progress repair becomes `FAULTED:INTERNAL_STALL`;
8. a Harness defect pauses the project run until generic Harness tests pass;
   it is never repaired inside the project transaction.

### Impact-aware invalidation

Recovery invalidates only the failed node and declared descendants. Previously
valid Evidence for unrelated owners/images remains valid when its source,
contract, firmware, and hardware hashes are unchanged.

### Acceptance tests

- Rewording an error cannot reset the failure lineage or budget.
- Changing an unrelated file cannot admit another owner repair.
- A Harness adapter exception becomes `INTERNAL_FAULT` with zero firmware-agent
  calls.
- The same lineage/material pair cannot invoke an agent twice.
- A relevant source change re-verifies the owner and declared consumers only.
- Restart/resume preserves attempts and reaches a deterministic stall.

## P0 — Require current-schema invariants or an explicit approved migration

### Problem

Immutable legacy contracts can predate operation tables, runtime flow, evidence
contracts, producer chains, or batch semantics. Allowing Execution to silently
fall back to weaker behavior makes new hard rules optional.

### Required schema policy

1. Define one current executable schema and a machine-readable capability
   matrix for older revisions.
2. Design always emits the current schema.
3. Execution refuses a revision missing any invariant required for its intended
   run, with typed `DESIGN_REVISION_REQUIRED`.
4. Migration creates a new unapproved revision through the Design Harness;
   approved legacy files remain immutable.
5. Migration reports every derived field and unresolved product decision.
6. No validator/helper may scatter schema sets such as
   `{"1.2", "1.3"}`; all feature admission uses the capability matrix.
7. Compatibility code may read old evidence for audit, but cannot grant modern
   closure/release authority.

### Acceptance tests

- A schema 1.2 design without production runtime flow cannot start a new
  release-authoritative run.
- Migration preserves user-owned requirements and creates a new digest-bound
  approval surface.
- Adding a schema version requires explicit capability entries for every
  registered invariant.
- A legacy fallback cannot change batching, authority, Tier C, or release
  semantics silently.

## P0 — Make Release clean, production-path, and forbidden-state aware

### Problem

A selftest-off config and READY marker prove that an image booted. They do not
prove a clean build, absence of test/secret/destructive behavior, or execution
of a user-visible product operation.

### Required release transaction

Release must use explicit nodes:

```text
release_prepare
  -> release_fullclean
  -> release_configure
  -> release_build
  -> release_flash
  -> release_observe
  -> release_validate
```

The Release transaction must:

1. create a new attempt-scoped empty build root;
2. execute and bind an explicit `fullclean` Receipt before the new build;
3. prove the effective selftest configuration disabled;
4. rebuild, flash, and observe fresh on the bound hardware identity;
5. execute at least one contract-declared core production scenario through the
   normal entrypoint and runtime-flow operation IDs;
6. reject selftest-only markers, secret-like values, test junk persistence,
   destructive test paths, fatal/reset loops, and unsupported required
   operations;
7. bind fullclean/configure/build/flash/serial/production-scenario Receipts,
   application binary, firmware hash, design digest, and hardware identity;
8. run terminal validation only after all Release invariants pass.

The early release smoke remains diagnostic only and can never satisfy
production composition, Integration, closure, or Release Evidence.

### Acceptance tests

- Reusing a prior release build directory without an explicit fullclean Receipt
  fails.
- A READY-only empty orchestrator fails Release.
- A selftest marker in normal release output fails.
- A required production operation returning `ESP_ERR_NOT_SUPPORTED` fails.
- Credential-like runtime output fails without storing the plaintext in the
  failure Receipt.
- Release Evidence from different firmware hashes or hardware identities cannot
  be combined.

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

### Deterministic `main/` and semantic-API boundary

Do not enforce this boundary with a prompt or a fragile token blacklist. Add a
typed dependency/API manifest and compile-assisted source analysis:

1. every component declares responsibility layer, resources, exported semantic
   APIs, and allowed dependency layers;
2. `main/` may compose product-policy/system-orchestration APIs but may not own
   register maps, bus transactions, protocol framing, transport setup, or
   device-specific recovery;
3. low-level driver headers/symbols are reachable only through their owning
   semantic component;
4. every production component has a normal-runtime consumer;
5. every test-only source/dependency is excluded when selftest is disabled;
6. the resolved CMake dependency graph must match the approved architecture;
7. source/link analysis writes a deterministic component-architecture Receipt.

Acceptance tests include low-level calls hidden in `main/`, a test-only
component linked into Release, an unconsumed production component, duplicate
resource owners, and a semantically valid wrapper around an adopted Registry
component.

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
11. 1,000 unchanged progress ticks with zero model wakeups;
12. one owner repair packet excluding all unrelated contract/source/log data;
13. untyped adapter exception with zero firmware-agent calls;
14. failure-summary and unrelated-file changes that cannot reset retry lineage;
15. crash at every configure/build/flash/observe/evidence boundary;
16. legacy schema blocked until a new approved migration;
17. READY-only release with an empty product orchestrator;
18. release attempt without explicit fullclean authority;
19. low-level device/transport implementation placed directly in `main/`;
20. invariant registry entry missing its producer, validator, or negative test.

The final scenario must reach `COMPLETE`. Every negative variant must stop at
its owning deterministic checkpoint before an invalid downstream side effect.

## Definition of done before restarting Crumb

- Unchanged build/flash/serial/Design progress causes zero model polling calls.
- Every Harness-owned model call uses a deterministic, digest-bound
  `ModelContextEnvelope`; raw conversation/log history is not its fact source.
- The generic E2E benchmark reduces total model input tokens by at least 55%
  from the audited baseline before functional P0 implementation continues.
- Node/run reports expose model calls, cached/uncached input, output, reasoning,
  tool calls, non-model progress events, and packet budgets.
- Every hard rule has a complete invariant-registry entry, graph-conformance
  coverage, and at least one negative scenario.
- All P0 schema, validator, graph, adapter, Receipt, and Evidence changes exist.
- Graph topology tests prove the new checkpoint order and recovery routes.
- Implementation, source validation, configure, build, flash, observation,
  evaluation, and Evidence commit are independent resumable boundaries.
- Resume/idempotency tests cover every new side-effect boundary.
- Untyped errors cannot retry or invoke a firmware agent; failure lineage and
  budgets survive resume and unrelated material changes.
- Generic negative scenarios cannot reach Integration, Tier C, or Release.
- Generic positive scenario reaches terminal `COMPLETE`.
- New release-authoritative runs use the current schema or a newly approved
  migration; legacy fallbacks cannot bypass current invariants.
- Schema 1.6 compatible owners demonstrate shared build/flash transactions.
- A production-path integration test proves it cannot be replaced by a parallel
  marker simulation.
- A Tier C artifact demonstrates a valid producer/delivery/correlation chain.
- Component architecture analysis proves `main/`/semantic-API boundaries and
  excludes test-only or unconsumed components from normal production.
- Release has explicit fullclean authority and executes a core normal production
  scenario while rejecting selftest, secrets, test junk, destructive paths,
  fatal loops, and unsupported required operations.
- Dead-worker status reconciliation is deterministic.
- Generated verification build roots are ignored and bounded.
- Documentation describes implemented behavior; it is not the enforcement
  mechanism.
- Only after these checks pass should the old Crumb namespace be snapshotted or
  removed and a clean Crumb design/run begin from user-owned inputs.
