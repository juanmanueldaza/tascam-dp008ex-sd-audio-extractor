"""Shared synthetic card-image builder for tests (single source of truth).

Consolidates the former duplicates tests/test_cli_e2e.py::build_image and
tests/test_fuzz_robustness.py::base_card into ONE builder (AC-1).

Layout: MBR + MTR header + BFS ROOT + one S001/SONG001 slot + one 4-cluster
master chain (ramp audio) + one 2-fragment stem chain + size fields.
"""

import math
import struct
from pathlib import Path

from tascam_dp008ex_sd_audio_extractor.carve import CLUSTER, POOL_OFF
from tascam_dp008ex_sd_audio_extractor.extract import ALLOC_LEN, ALLOC_OFF
from tascam_dp008ex_sd_audio_extractor.mbr import SECTOR
from tascam_dp008ex_sd_audio_extractor.stems import FRAG, FRAG_SAMPLES, RAW_SECTOR

MTR_BASE = (63 + 64) * SECTOR
POOL_CLUSTER0 = POOL_OFF // CLUSTER  # first pool cluster id (38)
MASTER_SECTORS = (
    POOL_CLUSTER0,
    POOL_CLUSTER0 + 1,
    POOL_CLUSTER0 + 2,
    POOL_CLUSTER0 + 3,
)
MASTER_BOFFS = (0, 0xC000, 0x18000, 0x24000)
# stereo partner chain (A4): identical boff span on different clusters
# (even/odd interleave style); both chains carry identical chain-local
# bytes so the pair renders one stereo song (taxonomy.md)
PARTNER_SECTORS = (
    POOL_CLUSTER0 + 4,
    POOL_CLUSTER0 + 5,
    POOL_CLUSTER0 + 6,
    POOL_CLUSTER0 + 7,
)
PARTNER_BOFFS = MASTER_BOFFS
# stem fragments live well past the master clusters so nothing overlaps
STEM_SECTOR0 = (POOL_OFF + 16 * CLUSTER) // RAW_SECTOR
STEM_BOFFS = (0, 0x48000)
MASTER_SAMPLES = len(MASTER_SECTORS) * (CLUSTER // 2)
STEM_SAMPLES = max(STEM_BOFFS) + FRAG_SAMPLES
TOTAL = MTR_BASE + POOL_OFF + 64 * CLUSTER


def ramp_value(g):
    """Smooth, non-repeating sample value for master audio (boundary-continuous).

    Period must exceed the whole chain so no two 4KiB blocks are identical.
    """
    return ((g // 512) % 4096) - 2048


# --- sine+impulse known signal (A1 ground truth) ---------------------------
# Single source of truth: the builder paints with these functions and the
# fidelity tests assert with them, so any deviation is a renderer bug.
SINE_AMPLITUDE = 8000
IMPULSE_AMPLITUDE = 16000
IMPULSE_SPACING = 8192  # samples between impulses (train start varies by seed)
_SWEEP_RATE = 2e-6  # quadratic phase coefficient: gentle chirp over the chain


def _sine_value(g, seed):
    """Chirped sine sample at absolute timeline position g (deterministic)."""
    phase = 2 * math.pi * (220.0 * g / 44100.0 + _SWEEP_RATE * g * g / 44100.0) + seed
    return int(SINE_AMPLITUDE * math.sin(phase))


def impulse_offsets(limit, seed=0):
    """Known impulse positions below limit; every one is mid-cluster (AC-3)."""
    start = 1000 + seed
    return [o for o in range(start, limit, IMPULSE_SPACING) if o % (CLUSTER // 2) != 0]


def _overlay_impulses(vals, seed):
    """Replace sine values at known offsets with alternating-polarity spikes."""
    imp = {}
    for k, o in enumerate(impulse_offsets(len(vals), seed)):
        imp[o] = IMPULSE_AMPLITUDE if k % 2 == 0 else -IMPULSE_AMPLITUDE
    return [imp.get(g, v) for g, v in enumerate(vals)]


def master_sine_impulse_pcm(seed=0):
    """Full expected master timeline (MASTER_SAMPLES int16le samples)."""
    vals = _overlay_impulses(
        [_sine_value(g, seed) for g in range(MASTER_SAMPLES)], seed
    )
    return struct.pack("<" + "h" * MASTER_SAMPLES, *vals)


def stem_fragment_payload(frag_index, seed=0):
    """Expected FRAG-byte payload of stem fragment 0/1 (sine+impulse).

    Sine phase uses timeline-absolute positions so the sweep is continuous;
    impulses sit at impulse_offsets(FRAG_SAMPLES, seed) relative to the
    fragment start, i.e. timeline base+rel with base a multiple of
    CLUSTER//2 (hence still mid-cluster, AC-3).
    """
    base = STEM_BOFFS[frag_index]
    vals = _overlay_impulses(
        [_sine_value(base + r, seed) for r in range(FRAG_SAMPLES)], seed
    )
    # fragment-relative impulse positions must themselves be mid-cluster and
    # land mid-cluster on the timeline (base % (CLUSTER//2) == 0 required)
    assert base % (CLUSTER // 2) == 0
    return struct.pack("<" + "h" * FRAG_SAMPLES, *vals)


def _paint_ramp_master(img, mtr_base):
    """Master audio: one continuous ramp across the four clusters.

    Both chains of the stereo pair carry identical chain-local bytes.
    """
    for sectors in (MASTER_SECTORS, PARTNER_SECTORS):
        for k, c in enumerate(sectors):
            off = mtr_base + c * CLUSTER
            pcm = struct.pack(
                "<" + "h" * (CLUSTER // 2),
                *[ramp_value(k * CLUSTER // 2 + i) for i in range(CLUSTER // 2)],
            )
            img[off : off + len(pcm)] = pcm


def _paint_ramp_stems(img, mtr_base):
    """Stem audio: two fragments, each with unique 4KiB blocks."""
    for k, s in enumerate((STEM_SECTOR0, STEM_SECTOR0 + 0x40)):
        off = mtr_base + s * RAW_SECTOR
        for blk in range(FRAG // 4096):
            img[off + blk * 4096 : off + (blk + 1) * 4096] = (
                bytes([k * 16 + blk]) * 4096
            )


def _paint_sine_impulse_master(img, seed, mtr_base):
    """Master audio: known sine sweep + impulse trains (A1 ground truth)."""
    pcm = master_sine_impulse_pcm(seed)
    for sectors in (MASTER_SECTORS, PARTNER_SECTORS):
        for k, c in enumerate(sectors):
            off = mtr_base + c * CLUSTER
            img[off : off + CLUSTER] = pcm[k * CLUSTER : (k + 1) * CLUSTER]


def _paint_sine_impulse_stems(img, seed, mtr_base):
    """Stem audio: sine+impulse payloads in both fragment slots."""
    for k, s in enumerate((STEM_SECTOR0, STEM_SECTOR0 + 0x40)):
        off = mtr_base + s * RAW_SECTOR
        payload = stem_fragment_payload(k, seed)
        img[off : off + FRAG] = payload


# --- FAT32 fixture (fat-export-harvest; opt-in, default image untouched) ----
# Minimal FAT32 inside partition 0: boot + 2 FATs + root/WAVE/BACKUP dirs +
# one export file with a known payload. Geometry is small and fixed.
FAT_PART_SECTORS = 2048
FAT_BPS = 512
FAT_SPC = 8  # 4KiB clusters keep the fixture tiny
FAT_RESERVED = 32
FAT_NFATS = 2
FAT_ROOT_CLUSTER = 2
FAT_WAVE_CLUSTER = 3
FAT_FILE_FIRST = 4  # MILPED01.WAV data starts here (2 clusters)
FAT_BACKUP_DIR = 6
FAT_BACKUP_FIRST = 7
FAT_PAYLOAD = bytes([(i * 7) % 256 for i in range(5000)])
FAT_BACKUP_PAYLOAD = bytes([(i * 13 + 5) % 256 for i in range(100)])
FAT_VARIANTS = (
    "clean",
    "nowave",
    "loop",
    "oob",
    "huge",
    "hostile",
    "exfat",
    "badbpb",
)


def _fat83(name):
    """Encode 'MILPED01.WAV' as an 11-byte 8.3 directory name."""
    stem, _, ext = name.partition(".")
    return (stem.upper() + " " * 8)[:8].encode() + (ext.upper() + " " * 3)[:3].encode()


def _dirent(name11, attr, cluster, size):
    """One 32-byte directory entry (only the fields the parser reads)."""
    return (
        name11
        + bytes([attr])
        + b"\x00" * 8
        + struct.pack("<H", (cluster >> 16) & 0xFFFF)
        + b"\x00" * 4
        + struct.pack("<H", cluster & 0xFFFF)
        + struct.pack("<I", size)
    )


def _paint_fat32(img, part_base, part_len, variant, seed):
    """Write the minimal FAT32 structures; variant selects corruption."""
    bps, spc, res, nfats = FAT_BPS, FAT_SPC, FAT_RESERVED, FAT_NFATS
    nclusters = (part_len // bps - res) // spc
    fat_sectors = (nclusters * 4 + bps - 1) // bps
    data_start = res + nfats * fat_sectors  # in sectors, partition-relative
    eoc = 0x0FFFFFFF

    fat = [0x0FFFFFF8, 0xFFFFFFFF] + [0] * (nclusters - 2)
    fat[FAT_ROOT_CLUSTER] = eoc
    fat[FAT_WAVE_CLUSTER] = eoc
    fat[FAT_BACKUP_DIR] = eoc
    if variant == "loop":
        fat[FAT_FILE_FIRST] = FAT_FILE_FIRST + 1
        fat[FAT_FILE_FIRST + 1] = FAT_FILE_FIRST  # cycle: visited-set must break
    else:
        fat[FAT_FILE_FIRST] = FAT_FILE_FIRST + 1
        fat[FAT_FILE_FIRST + 1] = eoc
    fat[FAT_BACKUP_FIRST] = eoc
    if variant == "oob":
        wave_first = nclusters + 999  # beyond the data region
    else:
        wave_first = FAT_FILE_FIRST

    def cluster_off(c):
        return part_base + (data_start + (c - 2) * spc) * bps

    if variant != "badbpb":
        boot = bytearray(bps)
        boot[0:3] = b"\xeb\x58\x90"
        struct.pack_into("<H", boot, 11, bps)
        boot[13] = spc
        struct.pack_into("<H", boot, 14, res)
        boot[16] = nfats
        struct.pack_into("<I", boot, 36, fat_sectors)
        struct.pack_into("<I", boot, 44, FAT_ROOT_CLUSTER)
        boot[510:512] = b"\x55\xaa"
        img[part_base : part_base + bps] = boot
    else:
        # nonzero garbage with an invalid bytes-per-sector: corrupt, not
        # empty (all-zero stays silent per R8; this must fire F6)
        boot = bytearray(bps)
        boot[0:3] = b"\xeb\x58\x90"
        struct.pack_into("<H", boot, 11, 123)
        boot[510:512] = b"\x55\xaa"
        img[part_base : part_base + bps] = boot
    for f in range(nfats):
        base = part_base + (res + f * fat_sectors) * bps
        img[base : base + nclusters * 4] = struct.pack("<" + "I" * nclusters, *fat)

    def put_dir(cluster, entries):
        off = cluster_off(cluster)
        for i, e in enumerate(entries):
            img[off + i * 32 : off + (i + 1) * 32] = e

    wave_entries = []
    if variant != "nowave":
        if variant == "hostile":
            # both names pass the WAVE filter so sanitize actually runs:
            # slash-stem and lowercase-stem with .WAV ext
            wave_entries = [
                _dirent(b"A/BAD   WAV", 0x20, wave_first, 100),
                _dirent(b"evil    wav", 0x20, FAT_FILE_FIRST + 1, 100),
            ]
        else:
            size = 0xFFFFFFFF if variant == "huge" else len(FAT_PAYLOAD)
            wave_entries = [_dirent(_fat83("MILPED01.WAV"), 0x20, wave_first, size)]
    root_entries = []
    if wave_entries:
        root_entries.append(_dirent(_fat83("WAVE"), 0x10, FAT_WAVE_CLUSTER, 0))
    root_entries.append(_dirent(_fat83("BACKUP"), 0x10, FAT_BACKUP_DIR, 0))
    put_dir(FAT_ROOT_CLUSTER, root_entries)
    if wave_entries:
        put_dir(FAT_WAVE_CLUSTER, wave_entries)
    backup_entries = [
        _dirent(_fat83("DATA.BIN"), 0x20, FAT_BACKUP_FIRST, len(FAT_BACKUP_PAYLOAD))
    ]
    if variant == "hostile":
        # dotdot file shares DATA.BIN's cluster; sanitize must fall back
        backup_entries.append(_dirent(b"..         ", 0x20, FAT_BACKUP_FIRST, 50))
    put_dir(FAT_BACKUP_DIR, backup_entries)
    # payloads (variant oob/huge reference unmapped WAVE clusters, so only
    # their file payload is unwritten; BACKUP data is always valid)
    if variant not in ("oob", "huge"):
        img[
            cluster_off(FAT_FILE_FIRST) : cluster_off(FAT_FILE_FIRST) + len(FAT_PAYLOAD)
        ] = FAT_PAYLOAD
    img[
        cluster_off(FAT_BACKUP_FIRST) : cluster_off(FAT_BACKUP_FIRST)
        + len(FAT_BACKUP_PAYLOAD)
    ] = FAT_BACKUP_PAYLOAD


def image_bytes(*, signal="ramp", seed=0):
    """Raw bytes of the shared synthetic card image.

    signal='ramp': boundary-continuous ramp (verify + legacy e2e).
    signal='sine_impulse': known sine sweep + impulse trains at recorded
    offsets in master AND stem regions (A1 fidelity ground truth).
    """
    if signal not in ("ramp", "sine_impulse"):
        raise ValueError(f"unknown signal: {signal!r}")
    return _build_image(signal=signal, seed=seed, mtr_base=MTR_BASE, part_sectors=64)


def _build_image(
    *,
    signal,
    seed,
    mtr_base,
    part_sectors,
    part_type=0x0B,
    paint_fat=None,
    fat_variant="clean",
):
    total = mtr_base + POOL_OFF + 64 * CLUSTER
    img = bytearray(total)

    # MBR: one FAT32 partition, valid signature
    img[0x1BE:0x1CE] = struct.pack(
        "<B3sB3sII", 0, b"\x00" * 3, part_type, b"\x00" * 3, 63, part_sectors
    )
    img[0x1FE:0x200] = b"\x55\xaa"

    # MTR header
    words = [0] * 32
    words[0] = 0x38974989
    words[9] = SECTOR
    words[11] = 192
    words[12] = 192 * SECTOR
    words[2] = (mtr_base + 0x800) // SECTOR  # self ptr
    words[14] = mtr_base // SECTOR  # mtr start
    words[15] = mtr_base // SECTOR + 10  # bitmap
    words[26] = (mtr_base + 0x2000) // SECTOR  # BFS ROOT ptr
    words[29] = 0
    struct.pack_into(">32I", img, mtr_base + 0x800, *words)

    img[mtr_base + 0x2000 : mtr_base + 0x2008] = b"BFS ROOT"
    slot = b"S001\x00\x00SONG001"
    img[mtr_base + 0x3000 : mtr_base + 0x3000 + len(slot)] = slot

    # master + stem audio payloads (geometry and alloc records identical
    # for every signal; only the audio bytes differ)
    if signal == "ramp":
        _paint_ramp_master(img, mtr_base)
        _paint_ramp_stems(img, mtr_base)
    else:
        _paint_sine_impulse_master(img, seed, mtr_base)
        _paint_sine_impulse_stems(img, seed, mtr_base)

    # alloc table records. Chain links reference the record's byte offset in
    # the table (slot * 64), which is what read_alloc/collect_chains key on.
    def key(slot):
        return slot * 64

    def put(slot, flag, prev, nxt, pairs, size=None):
        words = [key(slot), flag, prev, nxt] + [0] * 12
        for j, (sec, boff) in enumerate(pairs):
            words[4 + 2 * j] = sec
            words[5 + 2 * j] = boff
        if flag in (0x80030000, 0x80030001):  # size field: w11 == w12
            words[11] = size if size is not None else MASTER_SAMPLES
            words[12] = words[11]
        struct.pack_into(">16I", img, mtr_base + ALLOC_OFF + 64 * slot, *words)

    put(0, 0x80030001, 0, 0, [])  # size field (master song)
    put(1, 0x80030001, 0, 0, [], size=STEM_SAMPLES)  # size field (stem song)
    put(2, 0x90010001, 0, 0, [])  # audio inode
    put(
        3,
        0x90010006,
        0x9999,
        key(4),
        [*zip(MASTER_SECTORS, MASTER_BOFFS, strict=True), (0, 0), (0, 0)],
    )
    put(4, 0x90010006, key(3), 0, [])
    put(
        6,
        0x90010006,
        0x9999,
        key(7),
        [*zip(PARTNER_SECTORS, PARTNER_BOFFS, strict=True), (0, 0), (0, 0)],
    )
    put(7, 0x90010006, key(6), 0, [])
    put(
        5,
        0x90000006,
        0x9999,
        0,
        [
            (STEM_SECTOR0, STEM_BOFFS[0]),
            (STEM_SECTOR0 + 0x40, STEM_BOFFS[1]),
            (0, 0),
            (0, 0),
            (0, 0),
            (0, 0),
        ],
    )
    assert ALLOC_OFF + ALLOC_LEN < total
    if paint_fat is not None:
        paint_fat(img, 63 * SECTOR, part_sectors * SECTOR, seed)
    return bytes(img)


def build_image(path, *, signal="ramp", seed=0):
    """Write the shared synthetic card image to path; return the path."""
    Path(path).write_bytes(image_bytes(signal=signal, seed=seed))
    return path


def image_bytes_fat(*, signal="ramp", seed=0, variant="clean"):
    """Shared image plus a minimal FAT32 in partition 0 (opt-in).

    Default image_bytes() is untouched and byte-identical. variant picks
    the FAT shape: clean (WAVE MILPED01.WAV + BACKUP DATA.BIN), nowave
    (BACKUP only), loop (cyclic chain), oob (wild first cluster), huge
    (fileSize 0xFFFFFFFF), hostile (slash + dotdot names), exfat
    (partition type 0x07), badbpb (nonzero-invalid boot sector).
    """
    if variant not in FAT_VARIANTS:
        raise ValueError(f"unknown FAT variant: {variant!r}")
    fat_mtr_base = (63 + FAT_PART_SECTORS) * SECTOR
    part_type = 0x07 if variant == "exfat" else 0x0B
    return _build_image(
        signal=signal,
        seed=seed,
        mtr_base=fat_mtr_base,
        part_sectors=FAT_PART_SECTORS,
        part_type=part_type,
        paint_fat=lambda img, base, length, sd: _paint_fat32(
            img, base, length, variant, sd
        ),
    )


def build_image_fat(path, *, signal="ramp", seed=0, variant="clean"):
    """Write the FAT-variant synthetic card image to path; return the path."""
    Path(path).write_bytes(image_bytes_fat(signal=signal, seed=seed, variant=variant))
    return path
