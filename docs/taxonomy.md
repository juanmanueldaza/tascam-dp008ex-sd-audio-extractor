# Chain taxonomy: what each extent class is, and how it renders

Gate document for stereo output (A4): no interleave code ships without
this classification. Every claim is either cited to observed evidence or
explicitly labelled HYPOTHESIS.

## Classes

| Flag | Name | Evidence | Output |
|---|---|---|---|
| `0x90010006` | master / mixdown extent | PROVEN stereo pairing: on card2 the 6 master chains form pairs with identical boff spans, each pair one stereo song, even/odd cluster interleave DP-03 style (README "Masters (card2)"). Full-cluster concat in boff order; boff step `0xC000` samples is unused by render | ONE interleaved stereo WAV per pair |
| `0x90000006` / `0x90000005` | individual track (stem) extent | PROVEN mono fragments: 64 raw 512B sectors = `0x8000`B pasted at boff×2 on a silent timeline (README "Stems (card2)"). Temporally-aligned pairs exist but are AMBIGUOUS (see below) | mono WAV per chain, never interleaved |
| `0x80030000` / `0x80030001` | size field (w11 == w12 sample counts) | PROVEN metadata: counts match rendered durations (README, verify) | never rendered |
| `0x90010001` | audio inode | PROVEN metadata (alloc-table role only) | never rendered |
| `0x90010005` | first extent (DP-03 family) | HYPOTHESIS (DP-03-only): absent from the DP-008EX card2 image entirely — "No `0x90010005` on this device — DP-008EX differs from DP-03 here" (README). Alloc code counts it as `extent_first` for forward compatibility, but no DP-008EX render path depends on it | never rendered |

## Ambiguous: temporally-aligned stem pairs

Stem chains sometimes come in pairs with identical boff schedules, described
on card2 as "simultaneous stereo/dual-mono takes" (README "Stems (card2)"):
the evidence cannot decide between a true stereo pair and two mono takes
recorded simultaneously. Default: render each chain as its own mono WAV
(mono-pair) and document the pairing in output. Rationale: emitting a
false-stereo file destroys information (two independent takes collapsed
into L/R), while two mono files preserve everything — the safe direction
under uncertainty.

## L/R assignment: conventional, NOT proven

No extent, slot, or header field encodes channel parity: extent pairs carry
(sector, boff) only, and no parity bit exists anywhere in the format.
Pairing is proven (identical boff spans); which member is Left is not.

Convention: within a pair, the chain whose head record has the lowest
A-offset (alloc-table byte offset) renders as L, the other as R. This is
deterministic but arbitrary — output and README state it as conventional.
A known-L/R recording (same signal fed to known channels) is required to
promote this to PROVEN; no such card exists to date.

## Unpaired masters

A master chain with no identical-boff-span partner renders as a mono WAV
plus an `unpaired`-kind Skip (strict failure unless `--allow-incomplete`).
A lone mixdown is far more likely than a half-missing stereo pair, so mono
fallback preserves the audio while the skip records the anomaly.
