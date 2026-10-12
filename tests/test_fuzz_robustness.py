"""Robustness: malformed images must fail cleanly, never with a traceback.

Fixed seed => deterministic. Covers the image-only commands (list, verify),
which is where all parsing happens and which write nothing.
"""

import random

import pytest
from helpers import (
    ALLOC_LEN,
    ALLOC_OFF,
    MTR_BASE,
    TOTAL,
    build_image,
    image_bytes,
)

from tascam_dp008ex_sd_audio_extractor.cli import main
from tascam_dp008ex_sd_audio_extractor.mbr import SECTOR

OK_EXITS = (0, 1, 2)


def write(tmp_path, data, name="fuzz.img"):
    p = tmp_path / name
    p.write_bytes(bytes(data))
    return str(p)


@pytest.mark.parametrize("cmd", ["list", "verify"])
def test_pristine_card_never_tracebacks(tmp_path, capsys, cmd):
    p = build_image(tmp_path / "fuzz.img")
    assert main([cmd, str(p)]) in OK_EXITS
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "length",
    [0, 1, 64, 0x1BE, 0x1FE, SECTOR, 0x1000, MTR_BASE + 0x800 + 64, TOTAL - 1],
)
def test_truncated_images_fail_cleanly(tmp_path, capsys, length):
    p = write(tmp_path, image_bytes()[:length])
    for cmd in ("list", "verify"):
        assert main([cmd, p]) in OK_EXITS
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_bitflips_in_alloc_and_header_never_traceback(tmp_path, capsys):
    rng = random.Random(20260927)
    base = image_bytes()
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


@pytest.mark.parametrize("length", [0, 64, MTR_BASE + 0x800 + 64, TOTAL - 1])
def test_fuzz_allow_incomplete_never_tracebacks(tmp_path, capsys, length):
    """Intentionally-incomplete images with the opt-out flag: no traceback,
    exit stays in {0, 1, 2} (strict off; 1 = check fails, 2 = unreadable)."""
    p = write(tmp_path, image_bytes()[:length])
    for cmd in ("list", "verify"):
        assert main([cmd, p, "--allow-incomplete"]) in OK_EXITS
    assert "Traceback" not in capsys.readouterr().err
