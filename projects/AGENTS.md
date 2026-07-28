# Generated ESP-IDF projects

- `requirements/` and `connections/` remain outside the generated project and are never rewritten.
- Public component APIs are semantic and live under `components/<name>/include/`.
- Register access, bus transactions, adopted-library wrapping, and raw peripheral setup stay inside
  the owning component. `main/` owns product policy and orchestration only.
- Each subsystem retains a deterministic selftest covering identity/init, normal, boundary, and
  required failure paths. Selftests are enabled for verification and effectively disabled in release.
- Cross-task communication uses queues, notifications, event groups, semaphores, or documented
  mutex ownership. `volatile`/bare shared variables are not synchronization.
- Persistent tests delete only records they created by exact returned key/handle/index.
- Do not edit `managed_components/`, checkpoint SQLite, receipts, evidence, events, or projections.
- Every source/config change invalidates evidence whose firmware hash no longer matches.
- Datasheets live in the content-addressed repository library. Components keep only
  `datasheet_refs.json`/notes with canonical path and SHA-256; never copy PDFs into components.
- Keep product runtime in `main/`; put selftest/fault-injection implementation in separate compile
  units guarded by the declared selftest Kconfig.
