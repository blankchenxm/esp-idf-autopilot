# Architecture

ESP-IDF AutoDev separates design correctness from durable hardware execution:

```text
requirements + connections
        |
        v
Spec Kit Design Subgraph --five-file approved package--> LangGraph Execution Harness
        |                                                     |
        v                                                     v
 review-first spec.md                              receipts + evidence + release
```

The Design LangGraph owns input validation, deterministic inventory/grounding planning, provider
transactions, bounded repair, validation, and atomic revision promotion. The Execution LangGraph
starts only after approval and consumes the validated design digest; it may not silently
reinterpret policy, pins, verification, architecture, or Tier C.

## Ownership

| Concern | Owner |
|---|---|
| Product behavior, R/DR, pins, architecture, verification | approved execution contract |
| Workflow stages and legal transitions | Python graph + contract workflow + topology tests |
| Hardware/IDF/serial/registry side effects | adapters |
| Runtime cursor and resume | LangGraph checkpoint |
| Runtime facts | immutable receipts/evidence/events |
| Human explanation | spec/docs/DEVLOG projections |

Components expose lifecycle-oriented semantic APIs (`init`, operation, status, recovery, selftest).
`main/` composes them. Integration validates the actual concurrent/sustained call pattern rather
than treating an isolated component call as system evidence.

There is no target component count. Create a component only for a stable independent boundary:
device/register/transport ownership, shared resource arbitration, persistent state, external
protocol/security, reusable transformation, or independently scheduled lifecycle/failure
isolation. Do not create one for a marker, one-owner helper, parallel simulation, or policy fragment.
The component-architecture gate compares the typed API/resource/dependency manifest with resolved
CMake/source ownership, rejects unconsumed/test-only production components and duplicate resources,
and prevents `main/` from reaching low-level device headers. Components are linked into one image;
they are not flashed separately.

Execution-contract 1.2 separates design choice from runtime mechanics: the model proposes owner
verification batches and rare Tier C items, the deterministic validator accepts or rejects them,
and LangGraph executes only the frozen result. Owner-specific views reduce context without becoming
new authority. Datasheets and historical artifacts use content-addressed/hash-indexed stores.

FreeRTOS design is frozen before approval: task ownership, cadence, blocking boundaries, messages,
queue depth/backpressure/overflow, priority, stack/resource bounds, startup/shutdown/recovery, and
failure isolation. Integration measures these choices and returns material changes to a new design
revision.

Both graphs are project-agnostic. `project` selects matching user inputs and an isolated runtime
namespace; no node contains Crumb-specific devices, pins, URLs, or acceptance rules.

## Multi-project namespaces

`project_id` is the shared stem of `requirements/<project>.md`,
`connections/<project>.md`, and `projects/<project>/`. Durable product facts remain under the
project; transient control is isolated under `runtime/projects/<project>/`. Receipts, Evidence,
events, projections, new Design metadata, Datasheet manifests, and control indexes carry project
identity explicitly. Datasheet bytes alone are global because identical documents are safe to
deduplicate; project manifests and aliases preserve ownership.
