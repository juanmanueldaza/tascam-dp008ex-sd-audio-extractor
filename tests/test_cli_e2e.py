"""End-to-end CLI tests against a synthetic in-memory card image.

Builds a tiny but structurally complete DP-008EX-style image (MBR, MTR header,
BFS ROOT, song slot, master chain, stem chain, size fields) so every command is
exercised without needing a real card.
"""

import struct
import sys
import wave

import pytest
from helpers import (
    ALLOC_OFF,
    MASTER_SAMPLES,
    MTR_BASE,
    STEM_BOFFS,
    STEM_SAMPLES,
    build_image,
    image_bytes,
    ramp_value,
)
from test_render import make_rec

from tascam_dp008ex_sd_audio_extractor.cli import (
    group_master_chains,
    interleave_stereo,
    main,
)
from tascam_dp008ex_sd_audio_extractor.mbr import SECTOR
from tascam_dp008ex_sd_audio_extractor.stems import FRAG


@pytest.fixture
def card(tmp_path):
    return build_image(tmp_path / "card.img")


def read_wav(p):
    with wave.open(str(p)) as w:
        return w.getnframes(), w.getnchannels(), w.readframes(w.getnframes())


def test_verify_passes_on_synthetic_card(card, capsys):
    assert main(["verify", str(card)]) == 0
    out = capsys.readouterr().out
    assert "VERIFY PASS" in out
    assert "size fields" in out and "stem packing adjacency" in out


def test_extract_all_renders_full_clusters(card, tmp_path, capsys):
    d = tmp_path / "m"
    assert main(["extract-all", str(card), "--out-dir", str(d)]) == 0
    assert "chains=2" in capsys.readouterr().out
    wavs = list(d.glob("*.wav"))
    assert len(wavs) == 1
    nframes, nchannels, data = read_wav(wavs[0])
    # paired masters ship ONE stereo WAV (A4)
    assert nchannels == 2
    assert nframes == MASTER_SAMPLES
    # zero-padded slots skipped: exactly four clusters, not six
    assert struct.unpack_from("<h", data, 0)[0] == ramp_value(0)
    # last frame's L sample (stereo frames are 4 bytes: byte 4k = L_k)
    assert struct.unpack_from("<h", data, (nframes - 1) * 4)[0] == ramp_value(
        nframes - 1
    )
    # interleaved frames == the two mono renders byte-for-byte (AC-14)
    samples = struct.unpack("<" + "h" * (len(data) // 2), data)
    mono = struct.pack(
        "<" + "h" * MASTER_SAMPLES,
        *[ramp_value(g) for g in range(MASTER_SAMPLES)],
    )
    exp = struct.unpack("<" + "h" * MASTER_SAMPLES, mono)
    assert list(samples[0::2]) == list(exp)
    assert list(samples[1::2]) == list(exp)


def test_extract_all_unpaired_master_mono_fallback(tmp_path, capsys):
    """AC-15: a lone master chain renders mono + unpaired skip (strict 1)."""
    img = bytearray(image_bytes())
    img[MTR_BASE + ALLOC_OFF + 64 * 6 : MTR_BASE + ALLOC_OFF + 64 * 8] = b"\x00" * 128
    p = tmp_path / "lone.img"
    p.write_bytes(bytes(img))
    d = tmp_path / "m"
    assert main(["extract-all", str(p), "--out-dir", str(d)]) == 1
    assert "incomplete" in capsys.readouterr().out
    wavs = list(d.glob("*.wav"))
    assert len(wavs) == 1
    nframes, nchannels, _ = read_wav(wavs[0])
    assert (nchannels, nframes) == (1, MASTER_SAMPLES)
    assert main(["extract-all", str(p), "--out-dir", str(d), "--allow-incomplete"]) == 0


def _mkchain(a, pairs):
    return [make_rec(a, prev=0x9999, pairs=pairs)]


def test_group_pairs_identical_spans():
    pad = [(0, 0)] * 4
    a = _mkchain(0x40, [(38, 0), (39, 0xC000), *pad])
    b = _mkchain(0x80, [(42, 0), (43, 0xC000), *pad])
    c = _mkchain(0xC0, [(50, 0), (51, 0x9999), *pad])
    pairs, singles = group_master_chains([a, b, c])
    assert [(ci, cj) for _, _, ci, cj in pairs] == [(0, 1)]
    assert pairs[0][0] == a and pairs[0][1] == b  # L = lower head A
    assert singles == [(2, c)]


def test_group_third_chain_stays_single():
    pad = [(0, 0)] * 5
    a = _mkchain(0x40, [(38, 0), *pad])
    b = _mkchain(0x80, [(42, 0), *pad])
    c = _mkchain(0xC0, [(46, 0), *pad])
    pairs, singles = group_master_chains([a, b, c])
    assert len(pairs) == 1 and singles == [(2, c)]


def test_interleave_stereo_frames():
    left = struct.pack("<2h", 1, 2)
    right = struct.pack("<2h", 3, 4)
    assert interleave_stereo(left, right) == struct.pack("<4h", 1, 3, 2, 4)


def test_stems_pastes_fragments_at_boff_times_two(card, tmp_path, capsys):
    d = tmp_path / "s"
    assert main(["stems", str(card), "--out-dir", str(d)]) == 0
    assert "stem chains=1" in capsys.readouterr().out
    wavs = list(d.glob("*.wav"))
    assert len(wavs) == 1
    nframes, nchannels, data = read_wav(wavs[0])
    assert nchannels == 1  # stems stay mono (AC-17)
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
    assert "masters=2" in out and "ext6=4" in out


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


def test_songs_reports_250_slots(tmp_path, capsys):
    """AC-18: report-only 0x24-stride table, one entry per slot + raw hex."""
    img = bytearray(image_bytes())
    array_start = MTR_BASE + 0x3000
    for i in range(250):
        rec = b"S%03d\x00\x00SONG%03d" % (i % 1000, i % 1000)
        rec = rec + b"\x00" * (0x24 - len(rec))
        img[array_start + i * 0x24 : array_start + (i + 1) * 0x24] = rec
    p = tmp_path / "slots.img"
    p.write_bytes(bytes(img))
    assert main(["songs", str(p), "--start", "0x3000"]) == 0
    out = capsys.readouterr().out
    assert "entries=250" in out
    assert "S000/SONG000" in out and "S249/SONG249" in out
