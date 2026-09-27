"""End-to-end CLI tests against a synthetic in-memory card image.

Builds a tiny but structurally complete DP-008EX-style image (MBR, MTR header,
BFS ROOT, song slot, master chain, stem chain, size fields) so every command is
exercised without needing a real card.
"""

import struct
import sys
import wave

import pytest

from tascam_dp008ex_sd_audio_extractor.carve import CLUSTER, POOL_OFF
from tascam_dp008ex_sd_audio_extractor.cli import main
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


def build_image(path):
    img = bytearray(TOTAL)

    # MBR: one FAT32 partition, valid signature
    img[0x1BE:0x1CE] = struct.pack(
        "<B3sB3sII", 0, b"\x00" * 3, 0x0B, b"\x00" * 3, 63, 64
    )
    img[0x1FE:0x200] = b"\x55\xaa"

    # MTR header
    words = [0] * 32
    words[0] = 0x38974989
    words[9] = SECTOR
    words[11] = 192
    words[12] = 192 * SECTOR
    words[2] = (MTR_BASE + 0x800) // SECTOR  # self ptr
    words[14] = MTR_BASE // SECTOR  # mtr start
    words[15] = MTR_BASE // SECTOR + 10  # bitmap
    words[26] = (MTR_BASE + 0x2000) // SECTOR  # BFS ROOT ptr
    words[29] = 0
    struct.pack_into(">32I", img, MTR_BASE + 0x800, *words)

    img[MTR_BASE + 0x2000 : MTR_BASE + 0x2008] = b"BFS ROOT"
    slot = b"S001\x00\x00SONG001"
    img[MTR_BASE + 0x3000 : MTR_BASE + 0x3000 + len(slot)] = slot

    # master audio: one continuous ramp across the four clusters
    for k, c in enumerate(MASTER_SECTORS):
        off = MTR_BASE + c * CLUSTER
        pcm = struct.pack(
            "<" + "h" * (CLUSTER // 2),
            *[ramp_value(k * CLUSTER // 2 + i) for i in range(CLUSTER // 2)],
        )
        img[off : off + len(pcm)] = pcm

    # stem audio: two fragments, each with unique 4KiB blocks
    for k, s in enumerate((STEM_SECTOR0, STEM_SECTOR0 + 0x40)):
        off = MTR_BASE + s * RAW_SECTOR
        for blk in range(FRAG // 4096):
            img[off + blk * 4096 : off + (blk + 1) * 4096] = (
                bytes([k * 16 + blk]) * 4096
            )

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
        struct.pack_into(">16I", img, MTR_BASE + ALLOC_OFF + 64 * slot, *words)

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
    assert ALLOC_OFF + ALLOC_LEN < TOTAL
    path.write_bytes(bytes(img))
    return path


@pytest.fixture
def card(tmp_path):
    return build_image(tmp_path / "card.img")


def read_wav(p):
    with wave.open(str(p)) as w:
        return w.getnframes(), w.readframes(w.getnframes())


def test_verify_passes_on_synthetic_card(card, capsys):
    assert main(["verify", str(card)]) == 0
    out = capsys.readouterr().out
    assert "VERIFY PASS" in out
    assert "size fields" in out and "stem packing adjacency" in out


def test_extract_all_renders_full_clusters(card, tmp_path, capsys):
    d = tmp_path / "m"
    assert main(["extract-all", str(card), "--out-dir", str(d)]) == 0
    assert "chains=1" in capsys.readouterr().out
    wavs = list(d.glob("*.wav"))
    assert len(wavs) == 1
    nframes, data = read_wav(wavs[0])
    # zero-padded slots skipped: exactly four clusters, not six
    assert nframes == MASTER_SAMPLES
    assert struct.unpack_from("<h", data, 0)[0] == ramp_value(0)
    assert struct.unpack_from("<h", data, (nframes - 1) * 2)[0] == ramp_value(
        nframes - 1
    )


def test_stems_pastes_fragments_at_boff_times_two(card, tmp_path, capsys):
    d = tmp_path / "s"
    assert main(["stems", str(card), "--out-dir", str(d)]) == 0
    assert "stem chains=1" in capsys.readouterr().out
    wavs = list(d.glob("*.wav"))
    assert len(wavs) == 1
    nframes, data = read_wav(wavs[0])
    assert nframes == STEM_SAMPLES
    assert data[0:FRAG] != b"\x00" * FRAG  # fragment 0 present
    p = STEM_BOFFS[1] * 2
    assert data[p : p + FRAG] != b"\x00" * FRAG  # fragment 1 present
    assert data[FRAG:p] == b"\x00" * (p - FRAG)  # gap in between
    # zero-padded slots skipped: no audio before the first fragment / after last
    assert data[STEM_SAMPLES * 2 - FRAG :] != b"\x00" * FRAG


def test_list_reports_structure(card, capsys):
    assert main(["list", str(card)]) == 0
    out = capsys.readouterr().out
    assert "sig_ok=True" in out
    assert f"MTR_BASE={MTR_BASE}" in out
    assert "BFS ROOT hits: 1" in out
    assert "S001/SONG001" in out
    assert "masters=2" in out and "ext6=2" in out


def test_carve_sweeps_pool(card, capsys):
    assert main(["carve", str(card), "--stride", "8", "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert "scanned=3" in out
    assert "pool: off=" in out


def test_verify_reports_no_songs_on_empty_card(tmp_path, capsys):
    p = tmp_path / "empty.img"
    p.write_bytes(bytes(SECTOR * 300))  # valid MBR, no MTR content
    assert main(["verify", str(p)]) == 1
    assert "NO SONGS" in capsys.readouterr().out


def test_cli_is_importable_as_module():
    """python -m tascam_dp008ex_sd_audio_extractor must work on every platform."""
    import os
    import subprocess
    from pathlib import Path

    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH", "")]))
    r = subprocess.run(
        [sys.executable, "-m", "tascam_dp008ex_sd_audio_extractor", "--help"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "tascam-dp008ex-sd-audio-extractor" in r.stdout
