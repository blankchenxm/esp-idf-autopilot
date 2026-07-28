# `datasheet_notes.md` format

Create one file per external part at `projects/<project>/components/<subsystem>/datasheet_notes.md`. It is the register-level ground truth distilled from a validated document, not model memory.

```markdown
# <PartNumber> Datasheet Notes

Source: <official/trusted URL>
Document ID/revision: <ID>
Model identity verified: <date and evidence>
Text extraction: PASS

## Function
<2–3 sentences>

## Interface
<bus, address/CS, modes, maximum frequency, voltage assumptions>

## Register map
| Address | Name | Bits | Reset | Driver meaning |
|---|---|---|---|---|

## Initialization
<ordered steps, delays, reset and ready conditions>

## Timing and limits
<quantitative values with page/section citations>

## Identity and selftest
<Tier A readback/ID/CRC/write-read checks>

## Quantitative baselines
<Tier B range, amplitude, timing, count, distribution and stability checks>

## Gotchas
<datasheet NOTE/WARNING items relevant to this driver>
```

Memory may suggest search terms only. Every driver-relevant bit, sequence, timing value, and baseline must be verified against the validated document. A mismatch voids the memory-derived value; rederive it from the source.
