# Crumb firmware design

Crumb is ESP-IDF firmware for an ESP32-PICO-D4. Holding the active-low GPIO4 button records stereo PCM from two ICS-41350 microphones; releasing it ends the recording. The firmware packages complete recordings as WAV files and commits them atomically to W25N01GV storage. Recording remains available without Wi-Fi. Committed recordings are uploaded oldest-first, one at a time, to `http://crumb.xiaomeng-research.xyz/upload`; local deletion occurs only after HTTP 200, a matching `bytes` value, and a non-empty `saved` response field.

BQ25180YBGR access is strictly read-only. Storage reclamation deletes only the oldest complete recording and cannot affect an active write or upload. Wi-Fi credentials are read from NVS and provisioned from an ignored private build reference only when absent. SNTP-synchronized filenames use real time; recordings made before synchronization use BOOT/uptime names.

The authoritative pins are GPIO27/GPIO14 for charger I2C, GPIO18/GPIO23/GPIO19/GPIO5 for flash SPI, GPIO26/GPIO25 for microphone I2S, and GPIO4 for the active-low button. Audio format, duration, DMA/resource bounds, accepted loss, and device-level implementation details remain execution-readiness facts that must be grounded from authoritative device and local ESP-IDF sources; no hardware values are guessed in this design.

## Harness grounding reconciliation and readiness

The following provider statements are not open product decisions. Their authoritative handling is recorded here:

- Bind authoritative BQ25180YBGR datasheet facts and source assertions before implementing or exercising charger access.

- Bind authoritative W25N01GV datasheet facts and source assertions before implementing flash transactions.

- Bind authoritative ICS-41350 and local ESP-IDF I2S facts before selecting numeric audio or buffering parameters.

