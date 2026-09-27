"""Synthetic tests for the image-structure layer: MBR, header, scans, carver."""

import io
import struct

import pytest

from tascam_dp008ex_sd_audio_extractor.carve import (
    CLUSTER,
    POOL_OFF,
    cluster_stats,
    sweep,
)
from tascam_dp008ex_sd_audio_extractor.cli import song_name
from tascam_dp008ex_sd_audio_extractor.extract import ALLOC_LEN, ALLOC_OFF
from tascam_dp008ex_sd_audio_extractor.header import (
    HEADER_LEN,
    HEADER_OFF,
    parse_header,
)
from tascam_dp008ex_sd_audio_extractor.mbr import SECTOR, mtr_bounds, parse_mbr
from tascam_dp008ex_sd_audio_extractor.scan import (
    find_bfs_roots,
    scan_alloc_table,
    scan_song_slots,
)


def ptype_entry(lba, count, ptype=0x0B, boot=0x00):
    return struct.pack("<B3sB3sII", boot, b"\x00" * 3, ptype, b"\x00" * 3, lba, count)


def make_image(part_lba=63, part_count=100, total=1 << 20, extra=None):
    img = bytearray(total)
    img[0x1BE : 0x1BE + 16] = ptype_entry(part_lba, part_count)
    img[0x1FE:0x200] = b"\x55\xaa"
    if extra:
        off, data = extra
        img[off : off + len(data)] = data
    return io.BytesIO(bytes(img))


# --- mbr -------------------------------------------------------------------


def test_parse_mbr_reads_partition_and_sig():
    f = make_image(part_lba=63, part_count=8385867, total=4 * 1024**3)
    mbr = parse_mbr(f)
    assert mbr["sig_ok"] is True
    assert mbr["total_bytes"] == 4 * 1024**3
    assert mbr["total_sectors"] == 4 * 1024**3 // SECTOR
    p0 = mbr["entries"][0]
    assert (p0["lba_start"], p0["sector_count"], p0["type"]) == (63, 8385867, 0x0B)


def test_parse_mbr_flags_bad_signature():
    f = make_image()
    f.seek(0x1FE)
    f.write(b"\x00\x00")
    assert parse_mbr(f)["sig_ok"] is False


def test_mtr_bounds_starts_after_partition():
    mbr = parse_mbr(make_image(part_lba=63, part_count=1000))
    base, end = mtr_bounds(mbr)
    assert base == (63 + 1000) * SECTOR
    assert end == mbr["total_bytes"]


# --- header ----------------------------------------------------------------


def test_parse_header_fields():
    base = 1000 * SECTOR
    words = [0] * 32
    words[0] = 0x38974989  # magic
    words[9] = 512  # sector size
    words[11] = 192  # reserved sectors
    words[12] = 192 * 512  # reserved bytes
    words[2] = base // SECTOR + (HEADER_OFF // SECTOR)  # self ptr
    words[14] = base // SECTOR
    words[15] = base // SECTOR + 10
    words[26] = 0xDEAD
    words[29] = 0xBEEF
    f = make_image(extra=(base + HEADER_OFF, struct.pack(">32I", *words)))
    hdr = parse_header(f, base)
    assert hdr["magic"] == 0x38974989
    assert hdr["sector_size"] == 512
    assert (hdr["reserved_sectors"], hdr["reserved_bytes"]) == (192, 192 * 512)
    assert hdr["self_ptr"] == hdr["hdr_sector"]  # self-ptr is correct
    assert hdr["bitmap_ptr"] == hdr["mtr_sector"] + 10
    assert hdr["bfs_root_ptr_cand"] == 0xDEAD
    assert hdr["backup_root_cand"] == 0xBEEF
    assert HEADER_LEN == 128


# --- scans -----------------------------------------------------------------


def test_find_bfs_roots_locates_marker():
    base = 64 * SECTOR
    data = bytearray(2 << 20)
    data[base + 0x88 : base + 0x90] = b"BFS ROOT\x00"
    hits = find_bfs_roots(io.BytesIO(bytes(data)), base)
    assert len(hits) == 1
    h = hits[0]
    assert h["abs_offset"] == base + 0x88
    assert h["abs_sector"] == (base + 0x88) // SECTOR
    assert h["sector_off"] == 0x88 % SECTOR
    assert h["abs_hex"] == hex(base + 0x88)


def test_find_bfs_roots_caps_hits():
    data = bytearray(b"BFS ROOT\x00" * 64)
    assert len(find_bfs_roots(io.BytesIO(bytes(data)), 0, max_sectors=1024)) == 16


def test_scan_song_slots_matches_pattern_and_caps():
    base = 0x1000
    data = bytearray(8 << 20)
    slot = b"S001\x00\x00\x00SONG042"
    data[base + 0x40 : base + 0x40 + len(slot)] = slot
    hits = scan_song_slots(io.BytesIO(bytes(data)), base, window_bytes=1 << 20)
    assert len(hits) == 1
    assert hits[0]["s_idx"] == "001" and hits[0]["song"] == "SONG042"
    assert hits[0]["abs_offset"] == base + 0x40
    # bounded: never returns more than 400
    crowded = b"S001\x00SONG042" * 5000
    assert len(scan_song_slots(io.BytesIO(crowded), 0, window_bytes=1 << 20)) == 400


def test_scan_alloc_table_histogram():
    f = io.BytesIO(b"\x00" * (ALLOC_OFF + ALLOC_LEN))
    f.seek(ALLOC_OFF + 64 * 3)
    f.write(struct.pack(">16I", 0xC0, 0x90010006, 0, 0, *([0] * 12)))
    f.seek(ALLOC_OFF + 64 * 4)
    f.write(struct.pack(">16I", 0x100, 0x80030000, 0, 0, *([0] * 12)))
    f.seek(0)
    at = scan_alloc_table(f, 0)
    assert at["records"] == ALLOC_LEN // 64
    assert at["histogram"] == {0x90010006: 1, 0x80030000: 1}
    assert at["song_masters"] == 1
    assert at["extent_second"] == 1
    assert at["extent_first"] == 0


# --- carver ----------------------------------------------------------------


def test_cluster_stats_labels():
    assert cluster_stats(bytes(CLUSTER))["label"] == "zeros"

    quiet = struct.pack("<" + "h" * (CLUSTER // 2), *([3] * (CLUSTER // 2)))
    st = cluster_stats(quiet)
    assert st["label"] == "near-silent" and st["rms"] == 3 and st["peak"] == 3

    # 50% near-silent, moderate level, sign flip every sample => pcm-like
    n = CLUSTER // 2
    mixed = struct.pack("<" + "h" * n, *([3, -3, 8000, -8000] * (n // 4)))
    st = cluster_stats(mixed)
    assert st["label"] == "pcm-like"
    assert st["zc"] == n - 1
    assert st["peak"] == 8000
    assert st["silent_frac"] == 0.5

    loud = struct.pack("<" + "h" * n, *([30000, -30000] * (n // 2)))
    assert cluster_stats(loud)["label"] == "noise"


def test_sweep_honours_stride_and_limit():
    base = 1000
    end = base + POOL_OFF + 10 * CLUSTER
    f = io.BytesIO(b"\x00" * end)
    seen = [idx for idx, _, _ in sweep(f, base, end, stride=4, limit=2)]
    assert seen == [0, 4]
    f.seek(0)
    assert len(list(sweep(f, base, end, stride=8))) == 2  # 10 clusters, stride 8


# --- song classification ---------------------------------------------------


@pytest.mark.parametrize(
    ("top", "want"),
    [
        (0x9D0000, "short"),
        (0x6F00000, "A"),
        (0x6F00001, "B"),
        (0xA9E5D72, "B"),
    ],
)
def test_song_name_thresholds(top, want):
    assert song_name(top) == want
