# tascam-dp008ex-sd-audio-extractor

[![PyPI version](https://img.shields.io/pypi/v/tascam-dp008ex-sd-audio-extractor?style=flat-square&label=PyPI)](https://pypi.org/project/tascam-dp008ex-sd-audio-extractor/)
[![Python versions](https://img.shields.io/pypi/pyversions/tascam-dp008ex-sd-audio-extractor)](https://pypi.org/project/tascam-dp008ex-sd-audio-extractor/)
[![License](https://img.shields.io/pypi/l/tascam-dp008ex-sd-audio-extractor)](https://github.com/juanmanueldaza/tascam-dp008ex-sd-audio-extractor/blob/main/LICENSE)
[![CI](https://github.com/juanmanueldaza/tascam-dp008ex-sd-audio-extractor/actions/workflows/ci.yml/badge.svg)](https://github.com/juanmanueldaza/tascam-dp008ex-sd-audio-extractor/actions/workflows/ci.yml)

Extract master mixes and multitrack stems from Tascam **DP-008EX** SD card images
as standard WAV files — read-only, stdlib only, no recorder export needed.

> **Tested on DP-008EX only.** The DP-006 / DP-008 / DP-24SD family is *not*
> verified — sibling models may shift the header fields. Verify with the
> `verify` command before trusting any extraction.

The DP-008EX stores songs in an undocumented proprietary "BFS" format on a raw
MTR partition. This tool decodes the alloc table, walks the extent chains, and
renders both the stereo master chains and the per-track stem timelines straight
from a `dd` card image.

## Install

```bash
pip install tascam-dp008ex-sd-audio-extractor
```

Or from source:

```bash
git clone https://github.com/juanmanueldaza/tascam-dp008ex-sd-audio-extractor
cd tascam-dp008ex-sd-audio-extractor
pip install .
```

## Use

```bash
tascam-dp008ex-sd-audio-extractor devices                    # what cards are attached?
tascam-dp008ex-sd-audio-extractor image /dev/sdb -o card.img # card -> image, verified
tascam-dp008ex-sd-audio-extractor list card.img              # MBR/MTR/BFS/songs/masters
tascam-dp008ex-sd-audio-extractor extract-all card.img       # master chains -> mono WAVs
tascam-dp008ex-sd-audio-extractor stems card.img             # stem timelines -> WAVs
tascam-dp008ex-sd-audio-extractor verify card.img            # cross-check model (exit 0 = PASS)
```

Also runnable as `python3 -m tascam_dp008ex_sd_audio_extractor ...`. Every read
command (`list`, `carve`, `extract-all`, `stems`, `verify`) accepts **either** a
whole block device **or** an image file.

## Working with SD cards directly

The DP-008EX stores songs in an **undeclared region after partition 0** — the
multitrack audio is not on any filesystem. That is why a mounted card looks
empty, and why copying files off it (or the recorder's own USB export) recovers
nothing. You have to read the **whole device**.

```bash
# 1. what did we plug in? (judge by CONTENTS, not by REMOVABLE)
tascam-dp008ex-sd-audio-extractor devices
#    /dev/mmcblk0    3.7 GiB  no  6 master chain(s), 16 stem chain(s)   <- your card

# 2. image it (read-only source; verified by re-reading the image)
sudo tascam-dp008ex-sd-audio-extractor image /dev/mmcblk0 -o ~/card.img

# 3. work on the image at leisure
tascam-dp008ex-sd-audio-extractor verify ~/card.img
tascam-dp008ex-sd-audio-extractor extract-all ~/card.img --out-dir ~/out
```

Or skip the image and read the card directly (needs privileges for the device):

```bash
sudo tascam-dp008ex-sd-audio-extractor stems /dev/mmcblk0 --out-dir ~/out
```

**Guard rails** — the two easy mistakes are handled for you:

- **Partition nodes are refused.** `/dev/sdb1`, `/dev/disk2s1` and `D:\` exit
  with an error naming the device you should use instead, because imaging one
  produces an empty-looking card and you would never know songs were missing.
- **A mounted card is called out.** If the desktop has already mounted the card,
  `devices` lists the mountpoint and says where the songs really are:

  ```text
  MOUNTED VOLUMES on candidate devices
    /run/media/you/DP-008EX  (vfat, /dev/mmcblk0p1)
      labelled like a DP-008EX: the songs are on /dev/mmcblk0, not in this mountpoint
  ```

  Do not copy WAVs out of that mountpoint, and do not point the tool at it: the
  `WAVE/` and `BACKUP/` folders there are exports and backups, not the songs.
- **`image` will not overwrite the card it is reading**, will not write to a
  device node, and will not clobber an existing file without `--overwrite`. An
  interrupted image continues with `--resume`.
- Reading raw devices needs root or the `disk` group. The tool never writes to a
  card, and the card never needs to be mounted.

On macOS use the whole disk (`/dev/rdisk2`, faster than `/dev/disk2`); on
Windows use `\\.\PhysicalDriveN`. Both are recognised, and `devices` reports
what it can see — including an honest "unreadable (needs root or the disk group)".

`list` prints: MBR + MTR bounds, header checks (magic, self-ptr, mtr-start, bitmap),
BFS ROOT hits, song slots (`Sxxx/SONGxxx`, 250 slots on empty card), alloc-table
flag histogram + master/inode/extent counts.

## What the commands do

- **extract-all** — walks the master extent chains (`0x90010006`, even/odd
  cluster stereo interleave) and concatenates each chain in boff order into a
  WAV. Pairs share identical boff spans = one stereo song each.
- **stems** — the second extent family (`0x90000006`/`0x90000005`, raw 512B
  sector addressing) rendered as 44.1kHz mono timelines with each 16,384-sample
  fragment pasted at its boff. Sparse punch-in timelines (coverage ~1/18);
  import all stems of a song starting at 0 in your DAW.
- **verify** — image-only cross-check of the render model: size-field pairing,
  master boundary smoothness (full vs half-cluster model), duplicate-block
  scan, stem durations vs song size fields, fragment packing adjacency,
  sparse-coverage sanity. Exit code 0 = PASS.

## Layout learned (2026-09-26, 31GB image, empty card)

- MBR: 1× FAT32 `0x0B` LBA63 ×8385867; MTR = raw after p0 (`MTR_BASE=0xFFEB1400`, 26.7GB)
- Header @MTR+0x800: magic `0x38974989`, BE-u32, sector 512, reserved 192s;
  self +0x08, mtr-start +0x38, bitmap +0x3C, BFS ptr +0x68, backup root +0x74
  (DP-03 has BFS ptr at +0x60 — 8B shift here)
- BFS ROOT @MTR sector 200 (`0xFFECA400`, +88 in sector), marker `08EX`
- Song slots @MTR+~35.7MB (`0x1000D9428`), stride 0x24, 250 entries
- Alloc @MTR+0x288000 (1MiB, 16384×64B): pool `0x80040003/0x80104900` ×256,
  no `0x80030000` masters / `0x9001000x` inodes on empty card

## Extract (card2, 2026-09-26) — songs recovered

Card2 (4GB): 2× `0x80030000`/`0x80030001` masters, 2× `0x90010001` inodes, 2062×
`0x90010006` extents in 6 chains (heads = prev not in A-set). No `0x90010005`
on this device — DP-008EX differs from DP-03 here.
Per-pair contribution = the whole `0x18000` cluster, concatenated in boff order
(boundary-discontinuity ratio ~1.0 full vs ~10–16 for the half-cluster model;
zero duplicate 4KB blocks; master boff step = `0xC000` samples = half a cluster,
unused by render). Alloc size fields (`0x6ee44c6`, `0xa9e5d72`, `0x9d706a` on
`0x8003000x` w11/w12) are sample counts: /44100 = 43.9/67.3/3.9min ≈ rendered
43.8/67.2/3.9min.

Output (mono 16-bit/44.1kHz): chain0/1 = 43.8min (song A), chain2/3 = 67.2min
(song B), chain4/5 = 3.9min — identical boff spans within each pair, so each
pair is one stereo song (even/odd cluster interleave, DP-03 style).

## Stems (card2): format proof (2026-09-27)

Second extent family (`0x90000006` ×402 + `0x90000005` ×12, 16 chains) uses
RAW 512B sector addressing:

- fragment = 64 sectors = `0x8000`B = 16,384 samples: other fragments pack
  against it at exactly ±`0x8000` (2475/2484), so longer extents would overlap
  on disk; energy and sample-continuity hold across the whole `0x8000`.
- boff = sample offset on the mono 44.1kHz track timeline (uniform stride
  `0x48000` samples, fragment = `0x4000` samples → sparse punch-ins; boff_max
  matches the master size fields) → paste at byte offset boff×2.
- chain pairs are simultaneous stereo/dual-mono takes: identical boff
  schedules, interleaved on disk (dense pairs sit same-boff back to back).

Rendered as silent timelines with fragments pasted (dense coverage 0.056 =
1/18 duty cycle). Durations match the masters: B 67.3, A 43.9, short 3.9min.
rms-0 chains = unrecorded tracks.

## Carve survey (2026-09-26, empty-card image)

Classifier per 96KiB cluster: zeros / near-silent / pcm-like / pcm-possible / noise
(rms + silent-fraction + peak + zero-crossings). Stride-64 full pool (4243
clusters) → 1 false-positive pcm-like; stride-8 half-pool → isolated singles
only, never consecutive runs. Verdict: fragments at most, no recoverable songs
on the empty card.

## Tests

```bash
python3 -m pytest tests/
```

Synthetic-data unit tests (no image needed) for extent parsing, chain walking,
and both renderers (full-cluster concat, stem boff×2 paste at FRAG=0x8000).

## License

MIT — see [LICENSE](LICENSE).
