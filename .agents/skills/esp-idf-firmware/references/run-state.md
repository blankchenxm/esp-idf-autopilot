# Run-state protocol

`projects/<project>/run-state.json` is the machine-readable authority used by the Stop hook. `DEVLOG.md` remains the human-readable evidence ledger. Only one project may be in `CONTINUOUS` mode at a time.

## Schema

```json
{
  "schema_version": 1,
  "project": "example",
  "mode": "WAITING_SPEC",
  "cursor": "STAGE 1.5:spec:review",
  "next_action": "Wait for the single spec review batch.",
  "remaining": ["stage2", "stage3", "stage3.5", "stage3.6"],
  "release_verified": false,
  "user_paused": false,
  "progress_seq": 4,
  "blocker": null,
  "tier_c": {
    "required": false,
    "pending": []
  },
  "closure": {
    "required_total": 0,
    "pass": 0,
    "partial": 0,
    "fail": 0,
    "blocked": 0
  },
  "release_evidence": {
    "closure_pass": false,
    "selftest_disabled": false,
    "build_log": null,
    "flash_log": null,
    "serial_log": null,
    "runtime_marker": null
  }
}
```

## Modes

| Mode | Stop hook | When valid |
|---|---|---|
| `WAITING_SPEC` | allow final | Single Stage 1.5 review batch is ready |
| `CONTINUOUS` | reject final and continue | Approved work remains |
| `WAITING_TIER_C` | allow final | Predeclared Stage 2.9 batch is ready |
| `PAUSED` | allow final | User explicitly paused or redirected |
| `BLOCKED` | allow final | Hard external/environment failure has exact evidence |
| `COMPLETE` | allow final only after evidence validation | Stage 3.6 release passed |

## Update rules

- Create the file during Stage 1.0 in `WAITING_SPEC`; enter `CONTINUOUS` immediately when Stage 1.5 is approved.
- Increment `progress_seq` after every gate, failure classification, repair, or material evidence update.
- `cursor` is the current work location, not the last reported result.
- `next_action` is one concrete tool or implementation action. It must be non-empty in `CONTINUOUS`.
- A subsystem PASS advances `cursor`, `remaining`, and `next_action` before any progress commentary.
- Internal failures remain `CONTINUOUS`; set `next_action` to diagnose, patch, build, flash, or reverify.
- Use `WAITING_TIER_C` only for items declared in the approved spec and only after all automated evidence passes.
- A blocker object contains `kind`, `summary`, `evidence`, and `needed`; never use it for an internal engineering failure.
- Set `COMPLETE` only at cursor `STAGE 3.6:release:pass`.

## Successful terminal validation

All conditions are mandatory:

```text
mode == COMPLETE
cursor == STAGE 3.6:release:pass
release_verified == true
closure.pass == closure.required_total
closure.partial == 0
closure.fail == 0
closure.blocked == 0
release_evidence.closure_pass == true
release_evidence.selftest_disabled == true
build_log, flash_log, serial_log, runtime_marker are non-empty
referenced evidence files exist inside projects/<project>/
```

If any condition is false, return to `CONTINUOUS`, set the exact missing verification as `next_action`, and continue.
