# Tooling and hardware wrappers

- All `idf.py` operations go through `tools/idf.ps1`; wrappers must propagate the real exit code.
- Machine-local paths belong only in ignored `activate.local.ps1`; committed scripts discover them
  from the activated environment.
- Do not mutate global Git, PowerShell, driver, or ESP-IDF settings. Any compatibility override is
  process-local and recorded.
- Hardware probes must distinguish stable identity (chip/MAC/USB serial) from mutable port/session.
- Serial sessions and locks are released on success, timeout, cancellation, and exception paths.
- Raw output is written under the project logs; responses contain paths and relevant lines only.
- Destructive tests operate only on explicitly test-owned records/addresses and clean those exact IDs.

