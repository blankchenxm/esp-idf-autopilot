# Firmware Orchestrator

- Nodes return structured state updates only. They do not edit checkpoint files or projections.
- Routing is decided by deterministic `GateResult`/failure policy, never free-form model prose.
- Checkpoint state stores IDs and small JSON values; logs, PDFs, secrets, processes, locks, and
  serial objects remain outside it.
- Every build, flash, serial, approval, closure, and release side effect writes an immutable receipt.
- Expected values are committed before the side effect that produces the observation.
- Code before `interrupt()` must be read-only or idempotent; commit approval/Tier C after resume.
- An adapter owns cleanup in `finally`: serial monitor, port lock, child process, and temporary files.
- Repeated failure without a material input/artifact change must become a stalled/hard-blocker
  decision; never loop on the same prompt.
- Add or update unit, topology, resume, and idempotency tests with every graph/policy change.

