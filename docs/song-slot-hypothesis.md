# Song-slot records: hypothesis log (A5 report-only)

Every statement below is HYPOTHESIS unless it cites observed bytes. No
song-to-chain wiring ships on the back of this document (AC-20): the gate
for wiring is a slot field provably equal to a chain head A-offset or a
size field on card2, demonstrated, not asserted.

## Observed (single-card, empty card, README "Layout learned")

- A run of records with stride `0x24`, 250 entries, holding `Sxxx` /
  `SONGxxx` strings (parsed by `SONG_RE`; `scan_song_slots` finds them).
- Location as recorded: "Song slots @MTR+~35.7MB (`0x1000D9428`)".

## H1 vs H2: the locator is ambiguous

The recorded hex and the recorded relative offset do not reconcile cleanly:

- H1 (hex is ABSOLUTE device offset): array at device `0x1000D9428`
  on that card, i.e. MTR-relative `0x1000D9428 - 0xFFEB1400 = 0x228028`
  (~2.2MB), using that card's `MTR_BASE=0xFFEB1400`.
- H2 (hex is MTR-RELATIVE): array at MTR+`0x1000D9428` (~4.3GB into the
  MTR region — inside the 26.7GB MTR, so not excludable on range alone).

The "~35.7MB" gloss matches neither exactly; the observation is one
empty card, one reading. Therefore `parse_song_slots` takes a REQUIRED
`start` (MTR-relative) and the `songs` command exposes `--start`: the
tool reports bytes at the address it is told, shows raw hex per slot so
a wrong start is visible as garbage, and guesses nothing.

## Intra-record layout: unknown

Beyond the `Sxxx\x00{0,4}SONGxxx` string (13–17 bytes inside a 36-byte
record), no field has a proven meaning. The report prints `raw_hex` for
all 36 bytes of every slot so future cards can be compared byte-for-byte.
Any field assignment proposed later must cite two independent cards.

## Chain linkage: none proven

Zero evidence connects a `0x24` record to an alloc-table head: the
extraction code historically assigned songs only via the `song_name()`
timeline-top heuristic (`cli.py`), which is unchanged by A5. Candidate
linkage signals to test on card2 (all HYPOTHESIS until demonstrated):

1. A record word equal to a chain head's A-offset (alloc-table byte offset).
2. A record word equal to a `0x8003000x` size field (w11/w12 sample count).
3. Record order matching chain order (pairing sequence L/R/L/R...).

None of (1)–(3) has been checked. Until one is demonstrated on card2,
`songs` output must never rename, merge, or route extracted WAVs.
