# Design Harness

The durable Design LangGraph follows `Initialize -> Synthesize -> Inventory -> Ground -> Final
Selection -> Validate -> bounded Repair -> Promote`. Final selection is performed from the
receipt-owned Registry results inside grounding, never from the provider's pre-search preference.
Only `spec.md` is presented as a user document; inventory,
grounding plan, Plan/Tasks/DAG, and verification remain structured sections of the machine
contract.

## Authority boundary (schema 1.5)

The Harness compiles `requirements/<project>.md` and `connections/<project>.md`
into a hash-bound `input-authority.json` for every new package. It records exact
part identifiers, declared pins/endpoints, source offsets and secret references
(never secret values). A model may only repeat an external `part_number` exactly
from this record; it cannot add a variant/package suffix. Registry and Datasheet
calls use the Harness-bound exact identity.

`blocking_unknowns` are typed objects, not English routing messages. Only
`USER_DECISION` and `INPUT_AMBIGUITY` with no resolution can enter the design
review gate. `EXTERNAL_ACQUISITION` retries at its owner, implementation facts
are deferred as `IMPLEMENTATION_READINESS`, and credential handling is
`HARNESS_POLICY`. Legacy string unknowns remain readable for old revisions only.

The provider may propose a subsystem inventory, but the Harness materializes and validates the
authoritative `design_inventory` and `grounding_plan`. Every `external_part` receives Registry and
Datasheet work even if the provider omitted `datasheets[]`; every MCU-native subsystem receives a
local ESP-IDF grounding transaction. A model omission therefore cannot skip required grounding.

## Five-file immutable revision

```text
design-package/rev-NNNN/
  spec.md
  execution-contract.json
  manifest.json
  design-validation.json
  approval.json
```

`execution-contract.json` is canonical and contains metadata, requirements, decisions,
hardware/datasheet grounding, component selections, subsystem DAG, FreeRTOS architecture,
verification, Tier C, traceability, and workflow. `spec.md` is a review-first rendering.

`manifest.json` binds input hashes, provider/schema versions, every payload hash, and a canonical
design digest. `approval.json` records a user event against that digest. Approved payloads are never
edited in place; any material behavior, pin, architecture, acceptance, limitation, or Tier C change
creates a new revision and approval.

Generation happens under `design-package/.staging/<session>/attempt-NNN/`. Every node persists
small control state in the project LangGraph checkpoint and stores larger payloads as staging
artifacts. A Design job makes one complete draft and uses bounded repairs selected by typed
diagnostics. A `design_grounding` failure retries only the failed reader/fact generation; it does
not call the contract provider. A `design_provider` structural failure receives the grounded
contract plus typed summaries and performs one bounded contract repair. Repair counters are keyed
by responsible party, retry scope, owner, and diagnostic code, so one failing device/fact cluster
cannot consume another cluster's budget.
Repair context is stored in the disposable provider workspace, rather than repeated inline in the
model prompt. Structural repair is not an authority event: it may add unknowns but cannot remove
an existing user decision. User-owned unresolved facts are deterministically rendered back into
the staged spec and remain `WAITING_DESIGN_INPUT`; they do not allocate a product revision. Only a
clean Schema/grounding/validator result is atomically promoted to `rev-NNNN` and reported as
`WAITING_SPEC`.
Datasheet grounding is a staged transaction:
`acquire -> canonicalize -> extract -> deep-read -> fact-anchor`. Successful deterministic
acquisition/extraction and successful per-document reader output have separate immutable Receipts.
A repair generation reuses acquire/extract but deliberately changes the probabilistic reader cache
key. Raw reader output is always persisted. One unanchored fact is recorded as `REVIEW` and
discarded without losing the document or its valid facts; zero anchored L2/L3 facts remains a
blocking internal repair. Quotes are matched after Unicode/whitespace/dehyphenation normalization,
then bound back to an exact extraction span and SHA-256.

`orchestrator.cli design` launches a durable, project-scoped worker recorded under
`runtime/projects/<project>/design-jobs/` and returns `DESIGN_RUNNING` immediately. The worker owns
staging and atomic promotion independently of the invoking terminal or Codex conversation.
Repeated Design calls for the same project observe the same live job; different projects have
different job records, graph threads, locks, workspaces, and provider profiles.
`orchestrator.cli status` reports `DESIGN_RUNNING`, `WAITING_DESIGN_INPUT`, `WAITING_SPEC`,
`BLOCKED`, or `FAULTED`. `BLOCKED` is reserved for an evidenced external constraint;
Harness/provider exhaustion and broken staging invariants are `FAULTED`. A dead worker is recorded
as interrupted and the same graph thread is
resumed by the next Design call when inputs are unchanged. Node failures bind the latest persisted
phase and staging artifact/checkpoint evidence into the job record.

Registry and datasheet facts are not accepted merely because the design model reports them. The
Design Harness executes the provider call itself and writes a standard immutable Receipt under
`execution/receipts/design/`; raw provider output is stored in a hashed log artifact. The contract
stores `provider_receipt_id`, while `manifest.json` binds the Receipt path/hash/size into the design
digest. Missing, failed, altered, or unbound provider Receipts make the revision unapprovable.

## Datasheet grounding

- L0 identity: exact part/variant/document/revision/content validity.
- L1 design: electrical/pins/interface/reset/ready/rate/resource/safety/verification facts before approval.
- L2 implementation: registers, bitfields, init, timing, state/error recovery before a custom driver.
- L3 deep/edge: errata, complex timing/recovery/power/long-duration behavior when risk or failure triggers it.

The reader is a replaceable provider. Missing figure/table knowledge is `UNKNOWN`/`DEFERRED`, never
invented. Registry/library existence does not replace L0/L1 or real hardware verification.
The built-in artifact provider verifies the exact source, SHA-256, PDF/text extraction, recognizable
technical content, and extraction hash. A future deep reader can replace it for L2/L3 without
changing the five-file package or Receipt contract.

Canonical documents are content-addressed under `hardware/datasheets/objects/sha256/`. Project
`hardware/datasheet-manifest.json` and component `datasheet_refs.json` files include `project_id`
and cite the object hash; components do not carry duplicate PDFs. Aliases are isolated in
`hardware/datasheets/aliases/<project>.json`. The global `index.json` is derived from project
manifests and shows every project consuming each shared object.

## Design facts, operation intent, and product decisions

Schema 1.3 separates facts the Harness must acquire from choices the product
owner must make. Design freezes L0/L1 identity, interface, electrical, safety,
acceptance facts and each owner's `required_operations`; it does not require a
complete register/API/DMA study or receipt-bound implementation facts before
approval. Existing grounded facts may remain in the contract. Execution
readiness later binds only missing operation facts to an immutable addendum and
project-source assertions before any hardware transaction.

`product_decisions` contain behavior, safety, resource budget, and external
protocol choices. Grounding-only revisions may be delegated only when this
product projection is unchanged; any product-decision change remains a review
gate.

## Selection gate

Every external part or reusable subsystem receives exact-name and capability/interface Registry
queries. The provider choice before Registry is non-authoritative. After candidate details return,
the Harness records final compatibility, required-operation coverage,
API/examples, dependencies, license, maintenance, limitations, selection/rejections, and version
constraint. Adoption is optional; auditable search is mandatory.

For an external part, the Harness normalizes the exact query to the inventory `part_number`;
provider-added words are retained only as proposal metadata and cannot weaken the exact lookup.
Capability search remains a separate query. Candidate detail fetch is mandatory before adoption or
an explicit per-candidate rejection.

Provider unknowns are categorized before routing. Product and safety decisions remain active and
produce `WAITING_DESIGN_INPUT`. Acquisition-only `[GROUNDING_PENDING]` statements are resolved only
after the deterministic grounding plan and all relevant validator checks succeed. The reconciliation
is stored in staged `blocking-unknowns.json` and rendered into `spec.md`; it is never a silent model
rewrite.

Credential-redaction/provisioning statements are Harness policy rather than product decisions.
Likewise, statements that only identify missing driver/API/register/DMA/recovery facts, including
format/buffer bounds that are deliberately selected from those operation facts, are routed to
Execution readiness after L1 is valid; they cannot trigger a full Design-provider rewrite. The review
renderer removes the stale provisional sentence and records the typed policy/readiness resolution.
Only an explicit `[USER_DECISION]` remains an approval-time human gate.

## Verification batching and Tier C admission

The provider proposes contiguous `verification_batch` groups and declares isolation, reason, and
hardware resources. The deterministic validator rejects unsafe sharing; runtime only executes the
frozen result. Register/DMA/audio/storage/destructive or attribution-ambiguous tests are isolated.
Non-destructive wrappers over verified dependencies may share a batch while retaining independent
owner Evidence.

Every Tier A/B verification row declares executable `test_setup` and `stimulus`. A
`firmware_selftest` setup carries explicit Kconfig overrides and `isolated_build=true`; runtime
builds, flashes, and captures that image separately from normal/release firmware. A marker that
requires selftest or an injected event must never be awaited from a normal boot. Physical input is
Tier C unless an `automated_fixture` setup and fixture stimulus are declared.

Tier C is empty unless a required physical property cannot be verified after maximizing A/B.
Schema 1.2 requires the reason, completed automated checks, observable artifact contract, physical
property, and retry owners. Integration precedes Tier C so the user observes the final system
artifact, not an intermediate component state. Artifact sources must already be actionable:
a project-relative generated file, an absolute HTTP(S) download, or an explicit physical
observation. Descriptive placeholders are Design errors.

## Design terminal modes

| Mode | Meaning |
|---|---|
| `DESIGN_RUNNING` | A graph node is runnable or in progress. |
| `WAITING_DESIGN_INPUT` | A staged spec exposes a user-owned product/policy fact; no revision exists. |
| `WAITING_SPEC` | A clean immutable revision exists and waits only for digest-bound approval. |
| `BLOCKED` | An evidenced external constraint requires a material user/environment change. |
| `FAULTED` | Internal grounding/provider/Harness repair is exhausted or its invariant is broken. |
