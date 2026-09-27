"""Robustness: malformed images must fail cleanly, never with a traceback.

Fixed seed => deterministic. Covers the image-only commands (list, verify),
which is where all parsing happens and which write nothing.
"""

import random
import struct

import pytest

from tascam_dp008ex_sd_audio_extractor.carve import CLUSTER, POOL_OFF
from tascam_dp008ex_sd_audio_extractor.cli import main
from tascam_dp008ex_sd_audio_extractor.extract import ALLOC_LEN, ALLOC_OFF
from tascam_dp008ex_sd_audio_extractor.mbr import SECTOR
from tascam_dp008ex_sd_audio_extractor.stems import FRAG, FRAG_SAMPLES, RAW_SECTOR

MTR_BASE = (63 + 64) * SECTOR
POOL_CLUSTER0 = POOL_OFF // CLUSTER
STEM_SECTOR0 = (POOL_OFF + 16 * CLUSTER) // RAW_SECTOR
TOTAL = MTR_BASE + POOL_OFF + 8 * CLUSTER
OK_EXITS = (0, 1, 2)


def _put(img, slot, flag, prev, nxt, pairs, size=None):
    words = [slot * 64, flag, prev, nxt] + [0] * 12
    for j, (sec, boff) in enumerate(pairs):
        words[4 + 2 * j] = sec
        words[5 + 2 * j] = boff
    if size is not None:
        words[11] = words[12] = size
    struct.pack_into(">16I", img, MTR_BASE + ALLOC_OFF + 64 * slot, *words)


def base_card():
    img = bytearray(TOTAL)
    img[0x1BE:0x1CE] = struct.pack(
        "<B3sB3sII", 0, b"\x00" * 3, 0x0B, b"\x00" * 3, 63, 64
    )
    img[0x1FE:0x200] = b"\x55\xaa"
    words = [0] * 32
    words[0] = 0x38974989
    words[2] = (MTR_BASE + 0x800) // SECTOR
    words[14] = MTR_BASE // SECTOR
    words[15] = MTR_BASE // SECTOR + 10
    words[26] = (MTR_BASE + 0x2000) // SECTOR
    struct.pack_into(">32I", img, MTR_BASE + 0x800, *words)
    img[MTR_BASE + 0x2000 : MTR_BASE + 0x2008] = b"BFS ROOT"
    clusters = (POOL_CLUSTER0, POOL_CLUSTER0 + 1)
    for k, c in enumerate(clusters):
        pcm = struct.pack(
            "<" + "h" * (CLUSTER // 2), *[k + i // 512 for i in range(CLUSTER // 2)]
        )
        img[MTR_BASE + c * CLUSTER : MTR_BASE + c * CLUSTER + len(pcm)] = pcm
    for k, s in enumerate((STEM_SECTOR0, STEM_SECTOR0 + 0x40)):
        off = MTR_BASE + s * RAW_SECTOR
        for blk in range(FRAG // 4096):
            img[off + blk * 4096 : off + (blk + 1) * 4096] = (
                bytes([k * 16 + blk]) * 4096
            )
    _put(img, 0, 0x80030001, 0, 0, [], size=2 * (CLUSTER // 2))
    _put(img, 1, 0x80030001, 0, 0, [], size=0x48000 + FRAG_SAMPLES)
    _put(
        img,
        2,
        0x90010006,
        0x9999,
        0,
        [*zip(clusters, (0, 0xC000), strict=True), (0, 0), (0, 0), (0, 0), (0, 0)],
    )
    _put(
        img,
        3,
        0x90000006,
        0x9999,
        0,
        [
            (STEM_SECTOR0, 0),
            (STEM_SECTOR0 + 0x40, 0x48000),
            (0, 0),
            (0, 0),
            (0, 0),
            (0, 0),
        ],
    )
    return img


def write(tmp_path, data, name="fuzz.img"):
    p = tmp_path / name
    p.write_bytes(bytes(data))
    return str(p)


@pytest.mark.parametrize("cmd", ["list", "verify"])
def test_pristine_card_never_tracebacks(tmp_path, capsys, cmd):
    p = write(tmp_path, base_card())
    assert main([cmd, p]) in OK_EXITS
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "length",
    [0, 1, 64, 0x1BE, 0x1FE, SECTOR, 0x1000, MTR_BASE + 0x800 + 64, TOTAL - 1],
)
def test_truncated_images_fail_cleanly(tmp_path, capsys, length):
    p = write(tmp_path, base_card()[:length])
    for cmd in ("list", "verify"):
        assert main([cmd, p]) in OK_EXITS
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_bitflips_in_alloc_and_header_never_traceback(tmp_path, capsys):
    rng = random.Random(20260927)
    base = base_card()
    regions = [
        (MTR_BASE + 0x800, MTR_BASE + 0x880),  # header words
        (MTR_BASE + ALLOC_OFF, MTR_BASE + ALLOC_OFF + ALLOC_LEN),  # alloc table
    ]
    for i in range(24):
        img = bytearray(base)
        lo, hi = regions[i % len(regions)]
        for _ in range(24):  # burst of flips
            img[rng.randrange(lo, hi)] = rng.randrange(256)
        p = write(tmp_path, img, name=f"fuzz{i}.img")
        for cmd in ("list", "verify"):
            assert main([cmd, p]) in OK_EXITS, f"iteration {i} cmd {cmd}"
    assert "Traceback" not in capsys.readouterr().err


def test_missing_file_reports_cleanly(tmp_path, capsys):
    assert main(["verify", str(tmp_path / "nope.img")]) == 2
    assert "error:" in capsys.readouterr().err
