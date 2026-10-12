"""FAT32 harvest tests: parser exactness, F-site triggers, copy semantics.

Site mapping: F1 short-read, F2 cap, F3 OOB, F4 loop, F5 dir caps,
F6 omission (exFAT/invalid BPB), F7 hostile-name sanitize. CLI-level
strict/summary/shape tests live in test_cli_e2e.py (added with T5).

AC-20 audit (rg continue|break over fat.py): reported F1 (dir short
168, file short 312-break→F1), F5 cap; structural: deleted 0xE5 (174),
LFN/volume (177), non-WAVE/BACKUP root dirs incl. UTILITY/SysVol (226),
absent side (233), no-recursion into subdirs — flat dirs only (238),
WAVE non-WAV filter (241); normal exit: copy-complete break (308).
Zero unreported loss.
"""

import hashlib
import io
import os
import struct

from helpers import (
    FAT_BACKUP_PAYLOAD,
    FAT_PAYLOAD,
    image_bytes,
    image_bytes_fat,
)

from tascam_dp008ex_sd_audio_extractor.fat import (
    MAX_DIR_ENTRIES,
    harvest_exports,
    parse_bpb,
)
from tascam_dp008ex_sd_audio_extractor.mbr import fat_bounds, parse_mbr


def open_variant(variant):
    return io.BytesIO(image_bytes_fat(variant=variant))


def mbr_of(f):
    f.seek(0)
    return parse_mbr(f)


def test_parse_bpb_offsets():
    f = open_variant("clean")
    mbr = mbr_of(f)
    base, end = fat_bounds(mbr)
    assert (base, end) == (63 * 512, (63 + 2048) * 512)
    bpb = parse_bpb(f, base, end)
    assert bpb["bytes_per_sector"] == 512
    assert bpb["sectors_per_cluster"] == 8
    assert bpb["fat_start"] == base + 32 * 512
    assert bpb["data_start"] == base + (32 + 2 * 2) * 512
    assert bpb["cluster_size"] == 4096
    assert bpb["root_cluster"] == 2
    assert bpb["max_clusters"] == (end - bpb["data_start"]) // 4096


def test_harvest_clean_copies_wave_and_backup(tmp_path):
    f = open_variant("clean")
    mbr = mbr_of(f)
    skips = []
    assert harvest_exports(f, mbr, str(tmp_path), skips) == 2
    assert skips == []
    with open(os.path.join(tmp_path, "MILPED01.WAV"), "rb") as fh:
        assert (
            hashlib.sha256(fh.read()).hexdigest()
            == hashlib.sha256(FAT_PAYLOAD).hexdigest()
        )
    with open(os.path.join(tmp_path, "DATA.BIN"), "rb") as fh:
        assert fh.read() == FAT_BACKUP_PAYLOAD


def test_f1_short_reads_keep_partial(tmp_path):
    # Cut mid first file cluster: the file copy keeps its 100-byte prefix
    # (F1 #1) and the later BACKUP dir read also runs short (F1 #2).
    # Linear layout makes the co-fire unavoidable; both are SHORT_READ.
    part_base = 63 * 512
    cut = part_base + 36 * 512 + 2 * 4096 + 100
    data = image_bytes_fat(variant="clean")
    f = io.BytesIO(data[:cut])
    mbr = mbr_of(io.BytesIO(data))
    skips = []
    assert harvest_exports(f, mbr, str(tmp_path), skips) == 1
    assert [s.site for s in skips] == ["F1", "F1"]
    assert all(s.kind.value == "short-read" for s in skips)
    assert os.path.getsize(tmp_path / "MILPED01.WAV") == 100
    assert not (tmp_path / "DATA.BIN").exists()


def test_f2_huge_cap_writes_nothing(tmp_path):
    # the corrupt WAVE file is refused, but BACKUP still copies: per-file
    # independence, not all-or-nothing
    f = open_variant("huge")
    mbr = mbr_of(f)
    skips = []
    assert harvest_exports(f, mbr, str(tmp_path), skips) == 1
    assert [s.site for s in skips] == ["F2"]
    assert (tmp_path / "DATA.BIN").read_bytes() == FAT_BACKUP_PAYLOAD
    assert not (tmp_path / "MILPED01.WAV").exists()


def test_f3_oob_writes_nothing(tmp_path):
    f = open_variant("oob")
    mbr = mbr_of(f)
    skips = []
    assert harvest_exports(f, mbr, str(tmp_path), skips) == 1
    assert [s.site for s in skips] == ["F3"]
    assert (tmp_path / "DATA.BIN").read_bytes() == FAT_BACKUP_PAYLOAD


def test_f4_loop_terminates_without_write(tmp_path):
    f = open_variant("loop")
    mbr = mbr_of(f)
    skips = []
    assert harvest_exports(f, mbr, str(tmp_path), skips) == 1
    assert [s.site for s in skips] == ["F4"]
    assert (tmp_path / "DATA.BIN").read_bytes() == FAT_BACKUP_PAYLOAD


def test_f5_dir_entry_cap(tmp_path):
    from tascam_dp008ex_sd_audio_extractor.fat import _read_dir

    bpb = {
        "fat_start": 0,
        "data_start": 4096,
        "cluster_size": 4096,
        "max_clusters": 16,
    }
    # root chain 2->3->4->5->EOC holds 401 entries across 4 clusters
    fat = struct.pack(
        "<16I", *([0x0FFFFFF8, 0xFFFFFFFF, 3, 4, 5, 0x0FFFFFFF] + [0] * 10)
    )
    img = bytearray(6 * 4096)
    img[0 : len(fat)] = fat
    for i in range(401):
        name = f"F{i % 10000:04d}    WAV".encode()
        off = 4096 + i * 32
        img[off : off + 11] = name
        img[off + 11] = 0x20
        struct.pack_into("<H", img, off + 26, 8)
        struct.pack_into("<I", img, off + 28, 10)
    skips = []
    entries = _read_dir(io.BytesIO(bytes(img)), 0, len(img), bpb, 2, skips)
    assert len(entries) == MAX_DIR_ENTRIES
    assert [s.site for s in skips] == ["F5"]


def test_f6_exfat_and_badbpb_omit(tmp_path):
    for variant in ("exfat", "badbpb"):
        f = open_variant(variant)
        mbr = mbr_of(f)
        skips = []
        assert harvest_exports(f, mbr, str(tmp_path), skips) == 0
        assert [s.site for s in skips] == ["F6"]


def test_f7_hostile_names_sanitized_without_escape(tmp_path):
    f = open_variant("hostile")
    mbr = mbr_of(f)
    skips = []
    assert harvest_exports(f, mbr, str(tmp_path), skips) == 4
    assert sorted(s.site for s in skips) == ["F7", "F7", "F7"]
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["A_BAD.WAV", "DATA.BIN", "EVIL.WAV", "export_00000007"]
    root = os.path.realpath(tmp_path)
    for p in tmp_path.iterdir():
        assert os.path.realpath(p).startswith(root)


def test_collision_suffix_never_overwrites(tmp_path):
    (tmp_path / "MILPED01.WAV").write_bytes(b"original")
    f = open_variant("clean")
    mbr = mbr_of(f)
    skips = []
    assert harvest_exports(f, mbr, str(tmp_path), skips) == 2
    assert (tmp_path / "MILPED01.WAV").read_bytes() == b"original"
    assert (tmp_path / "MILPED01_1.WAV").read_bytes() == FAT_PAYLOAD


def test_all_zero_boot_sector_silent(tmp_path):
    f = io.BytesIO(image_bytes())
    mbr = mbr_of(f)
    skips = []
    assert harvest_exports(f, mbr, str(tmp_path), skips) == 0
    assert skips == []
