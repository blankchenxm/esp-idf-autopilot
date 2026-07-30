# Verification and Evidence

Tests declare setup, stimulus, observation, typed expected value/unit/tolerance, sample/duration,
deterministic pass expression, evidence types, cleanup, timeout, and side-effect boundary before
execution.

The deterministic text evaluator supports required/forbidden/ordered markers, exact/min/max counts,
legacy regex captures, and repeated numeric measurements. A measurement declares regex/group,
unit, scale, minimum/maximum sample count, and `each`, `min`, `max`, `mean`, `p95`, or `stdev`
evaluation. Bounds may be min/max, target plus tolerance, mean bounds, p95 maximum, and standard
deviation maximum. Actual samples (bounded in evidence) and computed statistics are preserved.

For Tier A/B, `test_setup` and `stimulus` are executable contract fields, not prose. A
`firmware_selftest` must provide isolated-build Kconfig overrides and a `firmware_simulation`
stimulus; its build/flash/serial receipts are distinct from selftest-off release evidence.

| Tier | Meaning | User involvement |
|---|---|---|
| A | identity/readback/CRC/write-read/device selftest/exact event injection | none |
| B | quantitative range/timing/count/distribution/rate/stability/fault injection | none |
| C | final physical property unavailable to current hardware/protocol after maximizing A/B | one approved batch |

Every receipt/evidence binds design digest, run/attempt/test ID, firmware/ELF SHA-256, hardware
identity/session, artifact path/hash, timestamps, expected/actual, and verdict. Closure rejects stale
bindings, missing artifacts, mock-only evidence, untested/partial rows, and limitations conflicting
with original or required-derived requirements.

New contracts additionally make this machine-readable per verification row with
`evidence_contract.observation` and `evidence_contract.required_kinds`. Valid kinds are
`build_receipt`, `flash_receipt`, `serial_log`, `firmware_hash`, `hardware_identity`, `artifact`,
`protocol_receipt`, and `user_confirmation`. A/B rows may not request `user_confirmation`; a Tier
C observation must request it. This prevents a prose evidence list from silently becoming an
unverified implementation assumption.

Schema 1.2 also requires Tier C admission and artifact delivery. Local/download artifacts are copied
to `execution/tier-c/<run-id>/<item-id>/<sha256>.<ext>` before interruption. WAV metadata is
machine-read, and confirmation binds the presented SHA-256. Bare confirmation, missing artifacts,
`unable`, and `not-performed` cannot satisfy closure.

An artifact source is an executable address, never a description of an anticipated result.
`local_file` uses a project-relative path (optionally templated only by `{run_id}` and `{item_id}`);
`download_url` uses an absolute HTTP(S) URL. If neither can exist, the contract must explicitly use
`physical_observation`. Invalid or prose-only sources are rejected during Design validation.

Evidence is immutable but can be superseded by an immutable correction when a later audit proves an
observation false. Closure and terminal validation ignore corrected Evidence, preserve the original
history, and require fresh evidence before release can become COMPLETE again.

Persistent verification data requires a component-owned cleanup boundary. A selftest may remove
only records it created under an explicit, test-only namespace or returned exact identifier; it
must never use capacity recovery, erase-all, or an unscoped prefix to remove production data.
When retained test data can consume bounded record slots, the component exposes a constrained
test-prefix cleanup operation and records the removed count in runtime evidence.

Verification PASS consumes distinct configure, build, flash, raw-observation, evaluation, and
Evidence-commit Receipts. A raw observation can never create its own verdict. Every replayable
Receipt stores an authority-derived idempotency key and the readable run/design/hardware/material/
operation/scope tuple; reuse also re-hashes every artifact.

Compatible rows may share one normalized image's configure/build/flash/observation Receipts, but
verdicts remain owner/test-specific. Image identity includes source, Kconfig, setup, isolation,
resources, and stimulus, so component count is not flash count. Integration adds
concurrent/sustained/restart/failure/resource call patterns through the production orchestrator.

Release is a separate fresh transaction: prepare an attempt root, explicit fullclean, selftest-off
configure, build, flash, observe, and validate. It must execute a declared core production scenario
and reject READY-only output, selftest markers, unsupported operations, secret-like plaintext,
test junk/destructive paths, and fatal/reset loops. Fullclean/configure/build/flash/observation/
scenario Receipts, application binary, firmware hash, design digest, and hardware identity must
agree before terminal `COMPLETE`.

Execution-time implementation addenda are authority inputs to evidence, not PASS evidence
themselves. Before build/flash and again at closure/release, the Harness verifies their design
digest, owner, operation coverage, addendum digest, source Receipt identities/hashes, and every
project-source assertion. Missing or tampered bindings are an internal fault and cannot be
reclassified as a user blocker.
