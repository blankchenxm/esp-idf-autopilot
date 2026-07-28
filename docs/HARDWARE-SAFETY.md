# Hardware Safety

- Preflight verifies wrapper/IDF, MCP capabilities, target identity, flash, and serial before design
  approval. The hardware session is refreshed again immediately before execution side effects.
- Port is a mutable locator. Stable identity prefers target chip, eFuse MAC, USB VID/PID/serial and
  location. A COM-number change with matching identity resumes automatically.
- Flash requires a clean build receipt, matching hardware session, exclusive port lock, and stopped
  monitor. Serial cleanup runs in `finally` for normal, timeout, cancellation, and error paths.
- Bounded capture and a predeclared marker/value are mandatory. Output without the marker is FAIL.
- Storage/NVS tests create uniquely tagged test-owned records and remove only those exact IDs.
- Network tests use declared endpoints/fixtures and redact credentials. Distinguish transport, TLS,
  authentication, protocol, and application status.
- Never store secret values, serial/process objects, raw PDFs, or huge logs in graph state/checkpoints.
- A release must not enter destructive test paths, create test junk, expose credentials, or depend
  on an interactive selftest command.

