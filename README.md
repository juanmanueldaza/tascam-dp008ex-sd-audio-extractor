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
tascam-dp008ex-sd-audio-extractor extract-all card.img       # master pairs -> stereo WAVs
tascam-dp008ex-sd-audio-extractor stems card.img             # stem timelines -> mono WAVs
tascam-dp008ex-sd-audio-extractor verify card.img            # cross-check model (exit 0 = PASS)
tascam-dp008ex-sd-audio-extractor songs card.img --start 0x3000  # slot table (report-only)
```

Also runnable as `python3 -m tascam_dp008ex_sd_audio_extractor ...`. Every read
command (`list`, `carve`, `extract-all`, `stems`, `verify`, `songs`) accepts
**either** a whole block device **or** an image file.

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
      labelled like a DP-008EX: songs live on /dev/mmcblk0; this mountpoint holds the WAVE/ exports and BACKUP/
  ```

  That mountpoint holds the exports and backups, not the songs — and do not
  point the tool at it. `extract-all` now reads the FAT32 partition directly
  from the whole device and copies the `WAVE/*.WAV` master exports (and
  `BACKUP/` verbatim), so a card whose multitrack projects were deleted
  still yields its exports; the songs themselves remain outside every
  partition.
- **`image` will not overwrite the card it is reading**, will not write to a
  device node, and will not clobber an existing file without `--overwrite`. An
  interrupted image continues with `--resume`.
- Reading raw devices needs root or the `disk` group. The tool never writes to a
  card, and the card never needs to be mounted.

On macOS use the whole disk (`/dev/rdisk2`, faster than `/dev/disk2`); on
Windows use `\\.\PhysicalDriveN`. Both are recognised. `devices` reports what it
can see — including an honest "unreadable (needs root or the disk group)" — and
the table above is explicit about which platform has been tested against real
hardware.

`list` prints: MBR + MTR bounds, header checks (magic, self-ptr, mtr-start, bitmap),
BFS ROOT hits, song slots (`Sxxx/SONGxxx`, 250 slots on empty card), alloc-table
flag histogram + master/inode/extent counts.

## What the commands do

- **extract-all** — walks the master extent chains (`0x90010006`, full-cluster
  concat in boff order) and groups them by identical boff span: each pair is
  one stereo song and ships as ONE interleaved stereo WAV (previously two
  mono files — breaking change, see card2 counts below). Pairing signal is
  proven (identical spans); **L/R assignment is conventional, not proven**
  (lowest head A-offset = L) — see `docs/taxonomy.md`. A chain with no
  partner renders mono plus an `unpaired` skip (strict failure). After the
  render loop, `extract-all` also harvests the FAT32 partition directly
  from the whole device: `WAVE/*.WAV` master exports are copied verbatim
  plus `BACKUP/` files opaque, so a card with no multitrack chains left
  still yields its exports (FAT32 only; copy-don't-decode).
- **stems** — the second extent family (`0x90000006`/`0x90000005`, raw 512B
  sector addressing) rendered as 44.1kHz mono timelines with each 16,384-sample
  fragment pasted at its boff. Always mono per chain, never interleaved:
  temporally-aligned stem pairs are ambiguous (true stereo vs simultaneous
  mono takes) and mono-pair preserves everything — see `docs/taxonomy.md`.
  Sparse punch-in timelines (coverage ~1/18); import all stems of a song
  starting at 0 in your DAW. (`0x90010005` first-extents are a DP-03-only
  HYPOTHESIS — absent from DP-008EX images; counted, never rendered.)
- **verify** — image-only cross-check of the render model: size-field pairing,
  master boundary smoothness (full vs half-cluster model), duplicate-block
  scan, stem durations vs song size fields, fragment packing adjacency,
  sparse-coverage sanity. Exit code 0 = PASS. Note: the boundary-smoothness
  metric does not apply to impulse-train test signals (deliberate
  discontinuities); sample-exactness there is proven by the A1 fidelity
  harness instead (`tests/test_fidelity.py`).
- **carve** — sweeps pool clusters for PCM-structured audio (survey tool).
- **songs** — report-only table of the 0x24-stride song-slot records
  (`--start` gives the MTR-relative array start, required): per-slot index,
  `Sxxx`/`SONGxxx` strings, and raw hex for all 36 bytes. Intra-record
  layout and chain linkage are unproven HYPOTHESES
  (`docs/song-slot-hypothesis.md`); output never renames or routes WAVs.

## Strict by default (incomplete reads fail loudly)

`list`, `carve`, `extract-all`, `stems`, and `verify` exit non-zero when
any read was incomplete, and print a one-line summary first:

```text
2 of 3 chains incomplete (2 skips)
```

`extract-all` prints a second line for FAT copies when those skip:

```text
1 of 2 files incomplete (1 skips)
```

Pass `--allow-incomplete` to get exit 0 anyway (skips are still reported).
Every drop, truncate, cap, out-of-range, short-read, odd-byte, omission,
and unpaired-chain event emits a named, counted `Skip` — there is no
silent data loss. Full per-site trigger tests: `tests/test_skip_reporting.py`;
audit table in its module docstring.

| site | where | kind | when |
|---|---|---|---|
| E1 | render_chain | short-read | short sector read (truncated image) |
| E2 | render_chain | cap | 12h render cap hit (corrupt alloc repeats sectors) |
| E3 | render_chain | oob | nonzero sector outside span (corrupt) |
| E4 | render_chain | truncation | output cut to render cap |
| S1 | render_stem | oob | raw sector 0 with nonzero boff (corrupt) |
| S2 | render_stem | oob | fragment outside span (corrupt) |
| S3 | render_stem | short-read | short fragment read (truncated) |
| S4 | render_stem | oob | boff outside timeline (corrupt boff, clamped) |
| S5 | render_stem | truncation | fragment crosses timeline end |
| C1 | carve sweep | short-read | short cluster read (truncated) |
| SC1 | BFS scan | cap | more than 16 `BFS ROOT` hits |
| SC4 | slot scan | cap | more than 400 slot matches |
| CL1 / CL3 | verify re-reads | short-read | short master/stem re-read (truncated) |
| CL2 | verify | omission | no size fields, durations unchecked |
| W1 | write_wav | odd-byte | trailing odd byte dropped (WAV needs whole samples) |
| A4 | extract-all | unpaired | lone master chain → mono fallback |
| F1 | FAT copy | short-read | short file read, partial kept (truncated image) |
| F2 | FAT copy | cap | declared file size over cap (corrupt) |
| F3 | FAT walk | oob | cluster outside data region (corrupt) |
| F4 | FAT walk | oob | cluster chain loops (corrupt) |
| F5 | FAT dir | cap | directory over entry/depth cap (corrupt) |
| F6 | FAT harvest | omission | non-FAT32 partition or invalid boot sector |
| F7 | FAT copy | omission | hostile name sanitized, copy proceeds safe |
| SC3 | slot scan | — | FIXED by the 14-byte chunk overlap (A3) |
| SC2 | slot scan | — | subsumed: same 400-cap event as SC4, one skip |
| W2 | pcm_stats mask | — | documented display-sampling (kept: `iter_unpack` raises on odd input) |
| — | `(0,0)` padding slots | — | structural filler, silent by design (pristine cards stay exit 0) |

## Benchmark (B0 baseline for efficiency work)

`bench/bench.py` (dev-only, never shipped) times `extract-all`, `stems`,
`carve --limit 200`, and `verify` on a 4 GiB sparse synthetic image
(`bench/gen_image.py`, seeded, streaming cluster-by-cluster build) and
records wall time, peak RSS, and stdlib read/seek counts. Counts are a
**proxy** — tallies on the buffered file object, not literal syscalls —
and RSS is the per-command process peak (forked child on POSIX).
Methodology plus the committed baseline numbers live in `BENCHMARK.md`;
reproduce with `python bench/bench.py --rebuild`.

## Layout learned (2026-09-26, 31GB image, empty card)

- MBR: 1× FAT32 `0x0B` LBA63 ×8385867; MTR = raw after p0 (`MTR_BASE=0xFFEB1400`, 26.7GB)
- Header @MTR+0x800: magic `0x38974989`, BE-u32, sector 512, reserved 192s;
  self +0x08, mtr-start +0x38, bitmap +0x3C, BFS ptr +0x68, backup root +0x74
  (DP-03 has BFS ptr at +0x60 — 8B shift here)
- BFS ROOT @MTR sector 200 (`0xFFECA400`, +88 in sector), marker `08EX`
- Song slots @MTR+~35.7MB (`0x1000D9428`), stride 0x24, 250 entries
- Alloc @MTR+0x288000 (1MiB, 16384×64B): pool `0x80040003/0x80104900` ×256,
  no `0x80030000` masters / `0x9001000x` inodes on empty card

## Permissions

Reading a raw device needs privileges that are deliberately *not* requested at
install time. Any of these work:

| | Linux | macOS | Windows |
|---|---|---|---|
| Per command | `sudo … image /dev/sdb` | `sudo … image /dev/rdisk2` | Administrator shell |
| Once, no sudo | `sudo usermod -aG disk $USER` then re-login | — | run as Administrator |
| Revert the group | `sudo gpasswd -d $USER disk` | — | — |

The `disk` group grants read access to *every* block device, so it is worth
knowing it is there. Membership also does not apply to a shell that is already
running — log out and back in, or use `newgrp disk -c '…'` for one command.

The tool never opens a card for writing, and never needs it mounted.

## Platform support — and what is actually verified

Being straight about the evidence, because "works on Linux, macOS and Windows" is
a claim that deserves receipts:

| | Linux | macOS | Windows |
|---|---|---|---|
| Read a real card or image | **verified on hardware** | *not yet executed* | verified on a real physical drive (CI) |
| `devices` discovery | sysfs (size/removable) | `/dev/disk*` + `diskutil` | `\\.\PhysicalDriveN` via `CreateFileW`/`DeviceIoControl` |
| Unaligned reads on raw drives | n/a | n/a | sector-aligned reader, verified |
| Mounted-card warning | `/proc/self/mounts` | `mount` output | removable drive letters |
| Tested on | a 4GB Tascam DP-008EX card | nothing yet | CI runner's `\\.\PhysicalDrive0` |
| Python versions exercised | 3.10, 3.12, 3.14 | 3.12 | 3.10, 3.12, 3.14 |

- **Linux** is the only platform with real hardware behind it. Everything below was
  read from an actual card (`/dev/mmcblk0`), and `stems /dev/mmcblk0` produces
  output byte-identical to rendering the same card from an image.
- **Windows** runs for real in CI, where the runner's own `\\.\PhysicalDrive0` is
  opened and queried. That is how three real bugs in the ctypes backend were
  caught, none of which type-checking could see:
  1. `STORAGE_PROPERTY_QUERY` is 12 bytes once padded — sending two bare DWORDs made
     Windows reject the query and the removable flag silently came back unknown.
  2. The descriptor offset was wrong: RemovableMedia is byte 10, not byte 8. Byte 8
     is the bus type, so the original code would have reported *every* disk as
     removable.
  3. Windows refuses a read that does not start on a sector boundary when the
     handle is a raw physical drive — and the MBR partition table is read 64 bytes
     at `0x1BE`. Image/device reads therefore go through a sector-aligning reader
     rather than a buffered `open()`. It also cannot `SEEK_END` such a handle, so
     the size comes from `DeviceIoControl`. CI proves the whole path: the runner's
     own `\\.\PhysicalDrive0` is read, its MBR parsed, and it is correctly reported
     as `no MTR region` (a Windows disk is not a DP-008EX card). A removable drive
     letter is reported but deliberately *not* mapped to a physical drive — that
     needs a device-stack walk, and guessing would be worse than saying so. Use
     `\\.\PhysicalDrive0`, not `E:\`; `E:\` is a partition and is refused.
- **macOS is the weakest link, and this table says so.** The code paths are
  implemented (`diskutil info -plist` for size and removable state, `mount` for
  volumes, `/dev/rdisk*` for the raw node) and the `diskutil` output parsing is
  unit-tested against real captured output, but the CI leg has been stuck in
  GitHub's macOS runner queue for hours at a time on both `macos-latest` and
  `macos-13` and has **never executed**; that leg is therefore `continue-on-error`,
  so a saturated runner pool cannot block releases. Nothing macOS-specific has been
  run against real hardware either. Use `/dev/rdisk2`, not `/dev/disk2` — the raw
  node skips the disk-arbiter cache and is several times faster.
- **Multi-partition cards** are warned about, not silently mis-parsed: only
  partition 0 is used to locate the MTR, so `verify`/`list` warn if a card declares
  more than one. A real DP-008EX always has exactly one.

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

Platform discovery is tested *per platform* rather than mocked: CI runs the
`devices` command on every runner, so the sysfs, `diskutil` and
`CreateFileW`/`DeviceIoControl` branches are all executed against real disks
instead of only being type-checked.

## License

MIT — see [LICENSE](LICENSE).
