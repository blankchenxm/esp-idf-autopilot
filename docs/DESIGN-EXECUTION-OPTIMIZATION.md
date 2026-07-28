# Design and Execution Optimization

## Objective

Keep the durable Design and Execution graphs, while preventing implementation
facts from being deferred to firmware repair and preventing software-only
defects from consuming hardware flash cycles.

## Contract model

New contracts use schema `1.3` and declare:

- `implementation_facts`: receipt-bound external-part or ESP-IDF facts that
  the Harness may derive without a product-owner question. Each fact has a
  source, a grounding receipt, and source assertions that bind it to firmware.
- `product_decisions`: user-facing choices about behavior, safety, resource
  budget, or external protocol. A change to this section always needs review.
- `responsibility_layer`: one of board resource, device driver, data service,
  product policy, system orchestration, or integration. ESP-IDF facilities are
  internal capabilities unless they own a stable shared resource boundary.

Exact external part numbers trigger a mandatory minimum design survey:

```text
datasheet interface/electrical/timing facts
  -> supported ESP-IDF API and target capability
  -> implementation facts and source assertions
  -> approval-ready spec and contract
```

Unknown product intent remains a user gate. Unknown implementation facts do
not: Design acquires them or remains blocked with a receipt-backed reason.

## Revision and approval

When project autonomy is enabled, a revision is auto-approved only if its
product projection is byte-equivalent to a prior approved revision and all
changed entries are receipt-bound `implementation_facts`, datasheet/provider
grounding, or resolved grounding limitations. Legacy recovery is not used for
new revisions. Any product decision, requirement, acceptance, pin, resource,
protocol, safety, or behavior change remains `WAITING_SPEC`.

## Verification layers

1. Component verification proves each component’s real hardware boundary.
2. Integration verification proves cross-component transactions, concurrency,
   restart, backpressure, and recovery. It may use deterministic fault
   injection or fixtures for policy paths, but cannot replace an applicable
   component hardware observation.
3. Release smoke runs selftest-off after product orchestration first becomes
   available. The final release remains a fresh isolated build/flash/runtime
   transaction.

Tier C is reserved for a physical property which A/B cannot observe; it is
never the default substitute for automation.

## Time-control gates

The following run before a flash attempt:

1. Contract schema and deterministic validation.
2. Source/contract consistency assertions for grounded facts.
3. Static marker, evidence-kind, lifecycle, and source-boundary checks.
4. Local incremental build.

Only then can a verified batch flash hardware. Adjacent components may share a
batch only when the approved contract marks them compatible and neither has an
exclusive/destructive/attribution-ambiguous resource. Components with DMA,
register mutation, storage mutation, or other declared isolation retain a
single-owner batch.

This changes preconditions and batch generation, not the durable graph's
ownership of receipts, evidence, retries, integration, closure, and release.
