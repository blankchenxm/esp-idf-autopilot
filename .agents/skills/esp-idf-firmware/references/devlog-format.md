# DEVLOG format

Use one append-oriented file per project: `projects/<project>/DEVLOG.md`. Raw command and serial output belongs in `logs/`; DEVLOG cites paths and records judgments.

## Component-selection ledger

Append one selection record before implementing every external part or reusable subsystem:

```markdown
### STAGE 1.3:<subsystem>:component-selection

| Field | Content |
|---|---|
| Classification | External part / reusable subsystem / local ESP-IDF built-in candidate |
| Requirements | `R*` / required `DR*` rows this selection must satisfy |
| Registry queries | Exact part-name and capability/interface queries |
| Candidates | Exact Registry identifiers and versions fetched for evaluation |
| Evaluation | Target/IDF compatibility, coverage, API/examples, dependencies, maintenance, license, limitations |
| Decision | Adopt `<namespace>/<component>@<constraint>` / local IDF built-in / custom implementation |
| Rejections | Explicit reason each serious alternative was rejected |
| Evidence | `component-selection.md` and raw/structured Registry MCP log paths |
| Verdict | `READY` or `BLOCKED` |
```

Rules:

- Search evidence is mandatory even when the decision is to use local ESP-IDF or a custom driver.
- A part-number zero result must be followed by a capability/interface query before `READY`.
- A Registry service/config failure is not a zero-result search and cannot satisfy the gate.
- Registry provenance does not replace datasheet validation or hardware verification.
- When a revision changes the dependency or invalidates the selection assumptions, append a new record; never rewrite the old decision.

## Subsystem ledger

Append one ledger for every subsystem verdict:

```markdown
### STAGE 2:<subsystem>:<phase>

| Field | Content |
|---|---|
| Class | MCU-native or external part |
| Component selection | Selection record, adopted version or rejection decision, and evidence paths |
| Knowledge | Exact datasheet/docs/examples/headers used |
| Implementation | Component and semantic API paths |
| Selftest | Test call chain, `CONFIG_APP_SELFTEST` state, cleanup ownership, and expected marker |
| Evidence | Raw build, flash, and serial log paths |
| Expected / actual | Quantified baseline and observation |
| Requirements | `R*` / required `DR*` rows exercised |
| Tier | `A`, `B`, or approved `C` |
| Verdict | `PASS`, `PASS_PENDING_TIER_C`, `PARTIAL`, `FAIL`, or `BLOCKED` |
| Failure / repair | Root cause and smallest repair, if any |
```

Rules:

- `PASS` requires build, flash, serial, and quantitative judgment; build-only is incomplete.
- Missing or `BLOCKED` component-selection evidence forbids subsystem implementation and PASS.
- A/B never require live user action.
- Only an approved Tier C may use `PASS_PENDING_TIER_C`.
- Every failure records its classified layer and repair before reverification.

## Stage 1 evidence

```markdown
### STAGE 1.1 — Datasheet acquisition

| Part | Source/doc ID | Model match | Extract | Notes | Verdict |
|---|---|---:|---:|---:|---|
| <PART> | <URL / ID> | PASS | PASS | ready | READY |

### STAGE 1.5 — Review

- Spec approved: yes/no
- Component selections ready: <ready>/<required>; blocked=<count>
- Adopted Registry dependencies: <identifiers and version constraints, or none>
- Registry rejections/custom implementations: <subsystems and evidence paths>
- POLICY decisions: <IDs and resolutions>
- External dependencies: ready/not ready
- Tier C batch declared: none / <IDs>
```

## Stage 2.9 evidence

Omit this section when no Tier C is approved.

```markdown
### STAGE 2.9 — Batched physical confirmation

| ID | Automated evidence already PASS | Expected observation | User result | Verdict |
|---|---|---|---|---|
| C1 | <logs and values> | <specific visible/audible pattern> | confirmed | PASS |
```

## Integration, closure, and release

```markdown
### STAGE 3 — Integration
- Task design: <tasks, blocking/real-time ownership, communication>
- Synchronization/backpressure: <queues/notifications/event groups/mutex ownership, depths, overflow behavior>
- Resource bounds: <stack high-water, heap, handles, queue occupancy, duration/iterations>
- Call patterns: <continuous/concurrent/sustained/restart/failure and quantitative expectations>
- Evidence: <build/flash/serial paths>
- Verdict: PASS/FAIL

### STAGE 3.5 — Requirements closure
| ID | Test | Evidence | Verdict |
|---|---|---|---|
| R1 | <measurable test> | <raw path + key value> | PASS |

Known limitations:
- <none, or non-conflicting limitations>

### STAGE 3.6 — Release
- Selftest source: retained
- Selftest default: disabled in release configuration
- Build: PASS <path>
- Flash: PASS <path/port>
- Runtime serial: PASS <path/marker; normal runtime does not depend on selftest>
- Required rows: <pass>/<total>; partial=0; blocked=0
```

## Process observations

Record only actionable workflow feedback: repeated stages, unclear rules, missing automation, or a failure pattern that should prevent recurrence.
