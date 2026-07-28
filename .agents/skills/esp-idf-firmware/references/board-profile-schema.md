# Board profile schema (`boards/<board>.yaml`)

A board profile describes the chip, module, pinout, and machine-checkable baselines reusable across projects. Project-specific expectations belong in the project requirements/spec.

## Peripheral fields

```yaml
- name: <identifier>
  type: <gpio_input | i2c_temp | pdm_mic_stereo | spi_nand | display | ...>
  model: <part number>
  # bus and pin fields
  expect:
    <field>: <value>
  selftest: "<autonomous quantitative procedure>"
```

## Common baseline fields

| Field | Meaning | Applies to |
|---|---|---|
| `idle_range: [lo, hi]` | Sane rest value | Scalar sensors |
| `invalid_values: [...]` | Uninitialized/error signatures | Sensors and ADCs |
| `stuck_values: [...]` | Stuck/all-zero/full-scale signatures | Mic, ADC, IMU |
| `amplitude_range: [lo, hi]` | Autonomous signal energy bound | Mic and analog data |
| `timing_range_ms: [lo, hi]` | Event/response timing bound | Inputs and protocols |
| `jedec_id` / `who_am_i` | Identity baseline | Addressable parts |
| `frame_crc` | Known generated frame checksum | Displays/cameras |

Tier A/B selftests never ask the user to act. Buttons use idle-level checks plus injected events to validate active polarity, debounce, timing, and state transitions. Microphones use captured-window amplitude, RMS/distribution, channel, stuck-value, sample-rate, and overflow checks. Only a final physical property unavailable to the MCU may be proposed as Tier C in the Stage 1.5 review.

## Example

```yaml
name: crumb
chip: esp32
module: ESP32-PICO-D4

peripherals:
  - name: btn_record
    type: gpio_input
    pin: 4
    active_level: low
    pull: internal_pullup
    expect:
      idle_level: high
      debounce_ms: [20, 80]
    selftest: "Verify idle level, then inject press/release edges and check polarity, debounce, event count, and state transitions."

  - name: mic_pair
    type: pdm_mic_stereo
    model: ICS-41350
    count: 2
    i2s: I2S0
    clk_pin: 26
    data_pin: 25
    channel_select:
      left: SELECT -> GND
      right: SELECT -> VDD33
    expect:
      rms_range: [20, 12000]
      stuck_values: [0, 32767, -32768]
      max_overflows: 0
    selftest: "Capture multiple windows; validate sample count/rate, both channels, RMS/distribution bounds, non-stuck data, and zero overflow."

  - name: nand_flash
    type: spi_nand
    model: W25N01GV
    host: SPI2_HOST
    clk_pin: 18
    mosi_pin: 23
    miso_pin: 19
    cs_pin: 5
    expect:
      jedec_id: "0xEFAA21"
    selftest: "Read JEDEC ID, then write/read/compare one test-owned page and clean up only that page."
```
