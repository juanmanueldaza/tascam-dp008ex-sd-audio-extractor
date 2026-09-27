"""Stem rendering: sparse fragments placed at boff offsets on a silent timeline.

Stem extents (flags 0x90000006 / 0x90000005) address RAW 512B sectors.
Proven layout (card2, 2026-09-27):
  - fragment = 64 sectors = 0x8000 bytes = 16,384 samples (packed pairs:
    partner chain fragment starts at exactly +0x8000, 504/606; energy and
    continuity hold across 0x4000, no boundary at extent edge).
  - two chains of a stereo/simultaneous pair interleave on disk at the same
    boff (boff delta 0), so own-chain sector step is usually 0x10000.
  - boff is a SAMPLE offset in the 44.1kHz mono track timeline (boff_max
    matches the master size fields in samples; stride 0x48000 samples with
    fragment 0x4000 = sparse punch-ins). Paste at byte offset boff*2.

Reading and chain walking are shared with the master path
(extract.read_alloc / extract.collect_chains with STEM_FLAGS).
"""

from .extract import MAX_PCM_BYTES, extent_pairs

RAW_SECTOR = 512
FRAG = 64 * RAW_SECTOR  # 0x8000 bytes = 16,384 samples per extent
FRAG_SAMPLES = FRAG // 2
STEM_FLAGS = (0x90000006, 0x90000005)


def render_stem(f, mtr_base, mtr_end, chain):
    """Paste chain fragments at boff*2 on a silent timeline.

    Returns (pcm, coverage_frac). boff is in samples; timeline length =
    (max_boff + FRAG_SAMPLES) samples of mono 16-bit, clamped to the card span
    and MAX_PCM_BYTES so a corrupt boff cannot request a huge allocation.
    """
    frags = []
    for r in chain:
        frags += extent_pairs(r)
    top = min(
        max(b for _, b in frags) + FRAG_SAMPLES,
        (mtr_end - mtr_base) // 2,
        MAX_PCM_BYTES // 2,
    )
    out = bytearray(top * 2)
    for sector, boff in frags:
        if sector == 0:  # zero padding slot; raw sector 0 is the MTR header
            continue
        o = mtr_base + sector * RAW_SECTOR
        if o < mtr_base or o + FRAG > mtr_end:
            continue
        f.seek(o)
        d = f.read(FRAG)
        p = boff * 2
        if p < 0 or p >= len(out):
            continue
        out[p : p + len(d)] = d[: len(out) - p]
    nz = sum(1 for i in range(0, len(out), 4096) if any(out[i : i + 4096]))
    return bytes(out), nz / max(1, (len(out) + 4095) // 4096)
