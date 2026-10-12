# Benchmark baseline (B0) — fidelity-efficiency-a, post-apply

Baseline for change `fidelity-efficiency-b` (B1–B4): efficiency work
compares against these numbers on the same image and commands. Recorded
2026-10-12, `bench/bench.py` on this host (Linux, tmpfs image).

## Image

`bench/gen_image.py --size-gb 4 --clusters 1024 --seed 0` (defaults):
4 GiB sparse file; master pair = two 1024-cluster chains, identical boff
spans (`k * 0xC000`), ramp audio (seed shifts phase); stem chain = 8 frags,
last boff `0x2000000` (long timeline exercises stem memory); size fields
match rendered durations. Regenerate: `python bench/bench.py --rebuild`.

## Methodology (read before comparing)

- **read/seek counts are a stdlib proxy, not syscalls.** A `CountingReader`
  wraps the buffered file object `open_device` returns and tallies its
  `read()` calls/bytes and `seek()` calls. Buffered-I/O batching means
  these undercount kernel reads; they are stable relative signals for
  B2-style coalescing comparisons, not absolute I/O profiles.
- **RSS is getrusage peak per command.** On POSIX each command runs in a
  forked child (parent stays lean), so `rssMB` is that command's own peak
  (base interpreter ~35MB included). Without fork it would be cumulative.
- **Carve uses `--limit 200`.** A full 4 GiB sweep at Python speed is not
  a useful baseline; B1 compares fused-vs-naive `cluster_stats` on the
  same 200 clusters.
- **verify exits 1 on this image by design.** The 12-bit ramp repeats
  every 2M samples, so 50M-sample chains fail the no-dup-blocks check
  (deterministic; timing-neutral). AC-25's PASS applies to the
  boundary-continuous small fixture (e2e), not here.

## Numbers

| command | exit | wall_s | rssMB | reads | readMB | seeks |
|---|---|---|---|---|---|---|
| list | 0 | 0.31 | 35.3 | 39 | 130.0 | 8 |
| extract-all | 0 | 3.44 | 591.4 | 2051 | 193.0 | 2052 |
| stems | 0 | 1.59 | 142.9 | 11 | 1.3 | 12 |
| carve --stride 16 --limit 200 | 0 | 3.73 | 19.7 | 202 | 18.8 | 203 |
| verify | 1 | 0.88 | 17.1 | 4106 | 194.3 | 4107 |

Readout guide: extract-all's 2051 reads = 2048 audio clusters + 3 table
reads (B2 target: boff-consecutive + disk-adjacent coalescing); its
591MB peak is the two 100MB renders plus the 400MB interleaved stereo
buffer. Stems' 143MB peak is the 67MB timeline `bytearray` plus the
`bytes(out)` copy (B3 target: stream/sparse-write it away). List's 130MB
read is the 128MB song-slot window (bounded scan, by design).

## Reproduce

```
python bench/bench.py --rebuild
```

Per-command stdout lands in `<out-dir>/logs/<command>.log`. Re-runs reuse
`/tmp/dp008ex-bench.img` unless `--rebuild` is passed.
