"""Streaming multi-GB synthetic image generator (B0, dev-only, never shipped).

Mirrors tests/helpers.py STRUCTURE (MBR, header, BFS ROOT, slot, master
pair, stem chain, alloc records) at multi-GB sparse scale for benchmarking.
Writes cluster-by-cluster with bounded RAM: at most one 96KiB audio buffer
plus the (small) alloc-record list are ever materialized — no full-image
bytearray (AC-21). Seeded and deterministic.

Layout (function of clusters_per_chain N and seed):
- master pair: two N-cluster chains, identical boff spans (k * 0xC000),
  sectors POOL_CLUSTER0+k and POOL_CLUSTER0+N+k (even/odd style).
- stem chain: 8 fragments, sectors s0+k*0x40, boffs k*0x48000 for k<7 and
  a far-flung last boff (long timeline exercises render_stem memory).
- audio: position ramp (fast to generate); seed shifts its phase.

Known benchmark trait: the 12-bit ramp repeats every 2M samples, so long
master chains intentionally FAIL verify's no-dup-blocks check. Deterministic
and timing-neutral; AC-25's PASS applies to the boundary-continuous small
fixture (covered by e2e), not to this image.
"""

import argparse
import struct
import sys

from tascam_dp008ex_sd_audio_extractor.carve import CLUSTER, POOL_OFF
from tascam_dp008ex_sd_audio_extractor.extract import ALLOC_LEN, ALLOC_OFF
from tascam_dp008ex_sd_audio_extractor.mbr import SECTOR
from tascam_dp008ex_sd_audio_extractor.stems import FRAG, FRAG_SAMPLES, RAW_SECTOR

MTR_BASE = (63 + 64) * SECTOR
POOL_CLUSTER0 = POOL_OFF // CLUSTER
BOFF_STEP = 0xC000  # master boff step in samples (card2 observation)
STEM_STRIDE = 0x48000
LAST_STEM_BOFF = 0x2000000  # far-flung: long timeline for stem-memory weight


def ramp_value(g):
    return ((g // 512) % 4096) - 2048


def _chain_record_slots(pairs_all):
    """Split a pair list into 6-per-record (prev, next, pairs) link specs."""
    chunks = [pairs_all[i : i + 6] for i in range(0, len(pairs_all), 6)]
    specs = []
    for j, ch in enumerate(chunks):
        ch = list(ch) + [(0, 0)] * (6 - len(ch))
        specs.append({"first": j == 0, "last": j == len(chunks) - 1, "pairs": ch})
    return specs


def gen_image(path, size_bytes=4 << 30, seed=0, clusters_per_chain=1024):
    """Write the sparse image; return (path, info dict). Bounded RAM."""
    n = clusters_per_chain
    a_sectors = [POOL_CLUSTER0 + k for k in range(n)]
    b_sectors = [POOL_CLUSTER0 + n + k for k in range(n)]
    a_pairs = [(s, k * BOFF_STEP) for k, s in enumerate(a_sectors)]
    b_pairs = [(s, k * BOFF_STEP) for k, s in enumerate(b_sectors)]
    stem_s0 = (POOL_OFF + (2 * n + 32) * CLUSTER) // RAW_SECTOR
    # Far-flung last boff gives stems a long timeline (memory weight), but it
    # must fit the image span or the clamp legitimately fires S4: scale down
    # for small images, keep the constant when it fits.
    room = (size_bytes - MTR_BASE) // 2 - FRAG_SAMPLES
    last_boff = min(LAST_STEM_BOFF, room)
    if last_boff <= 6 * STEM_STRIDE:
        raise ValueError(f"size {size_bytes} too small for the stem timeline")
    stem_pairs = [
        (stem_s0 + k * 0x40, k * STEM_STRIDE if k < 7 else last_boff) for k in range(8)
    ]
    master_samples = n * BOFF_STEP
    stem_samples = max(b for _, b in stem_pairs) + FRAG_SAMPLES

    audio_end = MTR_BASE + (POOL_CLUSTER0 + 2 * n) * CLUSTER
    alloc_end = MTR_BASE + ALLOC_OFF + ALLOC_LEN
    stem_end = MTR_BASE + (stem_s0 + 8 * 0x40) * RAW_SECTOR
    need = max(audio_end, alloc_end, stem_end)
    if size_bytes < need:
        raise ValueError(f"size {size_bytes} below layout need {need}")

    with open(path, "wb") as f:
        # MBR: one FAT32 partition, valid signature (mirrors the fixture)
        f.seek(0x1BE)
        f.write(struct.pack("<B3sB3sII", 0, b"\x00" * 3, 0x0B, b"\x00" * 3, 63, 64))
        f.seek(0x1FE)
        f.write(b"\x55\xaa")
        # MTR header
        words = [0] * 32
        words[0] = 0x38974989
        words[9] = SECTOR
        words[11] = 192
        words[12] = 192 * SECTOR
        words[2] = (MTR_BASE + 0x800) // SECTOR
        words[14] = MTR_BASE // SECTOR
        words[15] = MTR_BASE // SECTOR + 10
        words[26] = (MTR_BASE + 0x2000) // SECTOR
        words[29] = 0
        f.seek(MTR_BASE + 0x800)
        f.write(struct.pack(">32I", *words))
        # BFS ROOT + song slot (cosmetic markers, as in the fixture)
        f.seek(MTR_BASE + 0x2000)
        f.write(b"BFS ROOT")
        f.seek(MTR_BASE + 0x3000)
        f.write(b"S001\x00\x00SONG001")
        # master audio, cluster by cluster (one 96KiB buffer at a time)
        for sectors in (a_sectors, b_sectors):
            for k, c in enumerate(sectors):
                pcm = struct.pack(
                    "<" + "h" * (CLUSTER // 2),
                    *[
                        ramp_value(k * CLUSTER // 2 + i + seed * 7919)
                        for i in range(CLUSTER // 2)
                    ],
                )
                f.seek(MTR_BASE + c * CLUSTER)
                f.write(pcm)
        # stem fragments (distinct bytes per 4KiB block, as in the fixture)
        for k, (s, _) in enumerate(stem_pairs):
            off = MTR_BASE + s * RAW_SECTOR
            for blk in range(FRAG // 4096):
                f.seek(off + blk * 4096)
                f.write(bytes([k * 16 + blk]) * 4096)
        # alloc records
        recs = []

        def put(slot, flag, prev, nxt, pairs, size=None):
            words = [slot * 64, flag, prev, nxt] + [0] * 12
            for j, (sec, boff) in enumerate(pairs):
                words[4 + 2 * j] = sec
                words[5 + 2 * j] = boff
            if flag in (0x80030000, 0x80030001):
                words[11] = words[12] = size
            recs.append((slot, struct.pack(">16I", *words)))

        put(0, 0x80030001, 0, 0, [], size=master_samples)
        put(1, 0x80030001, 0, 0, [], size=stem_samples)
        put(2, 0x90010001, 0, 0, [], size=None)
        slot = 3
        for pairs_all in (a_pairs, b_pairs):
            specs = _chain_record_slots(pairs_all)
            for spec in specs:
                prev = 0x9999 if spec["first"] else (slot - 1) * 64
                nxt = 0 if spec["last"] else (slot + 1) * 64
                put(slot, 0x90010006, prev, nxt, spec["pairs"])
                slot += 1
        stem_specs = _chain_record_slots(stem_pairs)
        for spec in stem_specs:
            prev = 0x9999 if spec["first"] else (slot - 1) * 64
            nxt = 0 if spec["last"] else (slot + 1) * 64
            put(slot, 0x90000006, prev, nxt, spec["pairs"])
            slot += 1
        for slot_no, raw in recs:
            f.seek(MTR_BASE + ALLOC_OFF + 64 * slot_no)
            f.write(raw)
        f.truncate(size_bytes)
    return path, {
        "size_bytes": size_bytes,
        "seed": seed,
        "clusters_per_chain": n,
        "master_samples": master_samples,
        "stem_samples": stem_samples,
        "alloc_records": len(recs),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bench-gen-image")
    ap.add_argument("--out", required=True)
    ap.add_argument("--size-gb", type=float, default=4.0)
    ap.add_argument("--clusters", type=int, default=1024)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    path, info = gen_image(
        a.out,
        size_bytes=int(a.size_gb * (1 << 30)),
        seed=a.seed,
        clusters_per_chain=a.clusters,
    )
    print(f"wrote {path}: " + ", ".join(f"{k}={v}" for k, v in info.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
