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
from .report import Kind, RenderResult, Skip

RAW_SECTOR = 512
FRAG = 64 * RAW_SECTOR  # 0x8000 bytes = 16,384 samples per extent
FRAG_SAMPLES = FRAG // 2
STEM_FLAGS = (0x90000006, 0x90000005)


def render_stem(f, mtr_base, mtr_end, chain):
    """Paste chain fragments at boff*2 on a silent timeline.

    Returns RenderResult (pcm, skips, coverage_frac). boff is in samples;
    timeline length = (max_boff + FRAG_SAMPLES) samples of mono 16-bit,
    clamped to the card span and MAX_PCM_BYTES so a corrupt boff cannot
    request a huge allocation. Every loss emits a Skip (S1-S5), never silent.
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
    skips = []
    for sector, boff in frags:
        # (0, 0) slots are structural padding, not loss: skip silently
        # (pristine renders stay skip-free; AC-4). A nonzero boff pointing
        # at raw sector 0 (the MTR header, never audio) is genuinely OOB.
        if sector == 0 and boff == 0:
            continue
        if sector == 0:
            skips.append(
                Skip(
                    site="S1",
                    kind=Kind.OOB,
                    detail=(
                        f"raw sector 0 with boff {boff:#x} (MTR header, never audio)"
                    ),
                )
            )
            continue
        o = mtr_base + sector * RAW_SECTOR
        if o < mtr_base or o + FRAG > mtr_end:
            skips.append(
                Skip(
                    site="S2",
                    kind=Kind.OOB,
                    detail=f"raw sector {sector} outside audio span",
                )
            )
            continue
        f.seek(o)
        d = f.read(FRAG)
        if len(d) < FRAG:
            skips.append(
                Skip(
                    site="S3",
                    kind=Kind.SHORT_READ,
                    detail=(f"raw sector {sector}: got {len(d)} of {FRAG} bytes"),
                )
            )
        p = boff * 2
        if p < 0 or p >= len(out):
            skips.append(
                Skip(
                    site="S4",
                    kind=Kind.OOB,
                    detail=f"boff {boff:#x} lands outside the timeline",
                )
            )
            continue
        if p + len(d) > len(out):
            skips.append(
                Skip(
                    site="S5",
                    kind=Kind.TRUNCATION,
                    detail=(
                        f"boff {boff:#x}: fragment crosses timeline end, "
                        f"kept {len(out) - p} of {len(d)} bytes"
                    ),
                )
            )
        out[p : p + len(d)] = d[: len(out) - p]
    nz = sum(1 for i in range(0, len(out), 4096) if any(out[i : i + 4096]))
    return RenderResult(
        pcm=bytes(out),
        skips=tuple(skips),
        coverage=nz / max(1, (len(out) + 4095) // 4096),
    )
