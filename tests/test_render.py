"""Synthetic-data unit tests: extent parsing, chain walking, renderers, guards."""

import io
import struct
import wave

import pytest

from tascam_dp008ex_sd_audio_extractor.extract import (
    ALLOC_LEN,
    ALLOC_OFF,
    SECTOR_BYTES,
    collect_chains,
    extent_pairs,
    read_alloc,
    render_chain,
)
from tascam_dp008ex_sd_audio_extractor.header import parse_header
from tascam_dp008ex_sd_audio_extractor.mbr import parse_mbr
from tascam_dp008ex_sd_audio_extractor.stems import (
    FRAG,
    FRAG_SAMPLES,
    RAW_SECTOR,
    STEM_FLAGS,
    render_stem,
)
from tascam_dp008ex_sd_audio_extractor.wavio import pcm_stats, write_wav

STEM_FLAG = 0x90000006
EXT_FLAG = 0x90010006


def make_raw(a=0, flag=EXT_FLAG, prev=0, nxt=0, pairs=()):
    words = [a, flag, prev, nxt] + [0] * 12
    for k, (s, b) in enumerate(pairs):
        words[4 + 2 * k] = s
        words[5 + 2 * k] = b
    return struct.pack(">16I", *words)


def make_rec(a, flag=EXT_FLAG, prev=0, nxt=0, pairs=()):
    return {
        "slot": a // 64,
        "A": a,
        "flag": flag,
        "prev": prev,
        "next": nxt,
        "raw": make_raw(a, flag, prev, nxt, pairs),
    }


def alloc_image(records):
    """In-memory image holding the given 64B records in the alloc table."""
    f = io.BytesIO(b"\x00" * (ALLOC_OFF + ALLOC_LEN))
    for rec in records:
        f.seek(ALLOC_OFF + rec["A"])
        f.write(rec["raw"])
    f.seek(0)
    return f


# --- parsing ---------------------------------------------------------------


def test_extent_pairs_roundtrip():
    pairs = [
        (0x18FC0, 0x3C000),
        (0x19000, 0x84000),
        (0x19080, 0xCC000),
        (0x19100, 0x114000),
        (0x19140, 0x15C000),
        (0x19180, 0x1A4000),
    ]
    rec = make_rec(0x40, pairs=pairs)
    assert extent_pairs(rec) == pairs


def test_read_alloc_flags_and_slots():
    f = alloc_image([make_rec(0x40), make_rec(0x80, flag=0x80040003)])
    recs = read_alloc(f, 0)
    assert [r["slot"] for r in recs] == [1, 2]
    assert recs[0]["A"] == 64 and recs[1]["flag"] == 0x80040003


def test_read_alloc_filters_flags():
    f = alloc_image([make_rec(0x40, flag=STEM_FLAG), make_rec(0x80, flag=EXT_FLAG)])
    recs = read_alloc(f, 0, flags=STEM_FLAGS)
    assert len(recs) == 1 and recs[0]["flag"] == STEM_FLAG
    assert len(read_alloc(f, 0, flags=EXT_FLAG)) == 1
    assert len(read_alloc(f, 0)) == 2


# --- chains ----------------------------------------------------------------


def test_collect_chains_link_and_orphan():
    recs = [
        make_rec(0x40, prev=0x9999, nxt=0x80),
        make_rec(0x80, prev=0x40, nxt=0xC0),
        make_rec(0xC0, prev=0x80, nxt=0),
        make_rec(0x100, prev=0x40, nxt=0),  # prev in set but unreachable
    ]
    chains = collect_chains(recs)
    assert [[r["A"] for r in c] for c in chains] == [[0x40, 0x80, 0xC0], [0x100]]


def test_collect_chains_ignores_other_flag_records():
    """Other flags are filtered out; a dangling next pointer just ends the walk."""
    recs = [
        make_rec(0x40, flag=STEM_FLAG, prev=0x9999, nxt=0x80),
        make_rec(0x80, flag=EXT_FLAG, prev=0x40, nxt=0),  # filtered out
    ]
    chains = collect_chains(recs, STEM_FLAGS)
    assert [[r["A"] for r in c] for c in chains] == [[0x40]]


# --- master renderer -------------------------------------------------------


def test_render_chain_full_concat_sorted_by_boff():
    base, ncl = 1000, 4
    end = base + ncl * SECTOR_BYTES
    f = io.BytesIO(b"\x00" * end)
    c1 = bytes([0x11]) * SECTOR_BYTES
    c2 = bytes([0x22]) * SECTOR_BYTES
    f.seek(base + 1 * SECTOR_BYTES)
    f.write(c1)
    f.seek(base + 2 * SECTOR_BYTES)
    f.write(c2)
    f.seek(0)
    pairs = [(2, 0x200), (1, 0x100)]  # unsorted by boff
    out = render_chain(f, base, end, pairs)
    assert out == c1 + c2  # boff order, whole clusters


def test_render_chain_skips_out_of_range():
    base = 1000
    end = base + 3 * SECTOR_BYTES  # clusters 0..2 only
    f = io.BytesIO(b"\x00" * end)
    c1 = bytes([0x11]) * SECTOR_BYTES
    f.seek(base + 1 * SECTOR_BYTES)
    f.write(c1)
    f.seek(0)
    out = render_chain(f, base, end, [(1, 0x100), (9, 0x300)])
    assert out == c1


def test_render_chain_respects_max_bytes():
    """Corrupt alloc table repeating sectors must not render unbounded PCM."""
    base = 1000
    end = base + 3 * SECTOR_BYTES
    f = io.BytesIO(b"\x00" * end)
    c1 = bytes([0x11]) * SECTOR_BYTES
    c2 = bytes([0x22]) * SECTOR_BYTES
    f.seek(base + 1 * SECTOR_BYTES)
    f.write(c1)
    f.seek(base + 2 * SECTOR_BYTES)
    f.write(c2)
    f.seek(0)
    pairs = [(1, 0x100), (2, 0x200)]
    assert render_chain(f, base, end, pairs, max_bytes=SECTOR_BYTES) == c1
    assert render_chain(f, base, end, pairs, max_bytes=3 * SECTOR_BYTES) == c1 + c2


# --- stem renderer ---------------------------------------------------------

STEM_PAIRS = [
    (8, 0x3C000),
    (72, 0x84000),
    (136, 0xCC000),
    (200, 0x114000),
    (264, 0x15C000),
    (328, 0x1A4000),
]


def stem_image(pairs=STEM_PAIRS, end=None):
    """In-memory card holding the given fragments.

    end defaults to a card end that fits the full timeline, so the timeline
    clamp in render_stem does not bite.
    """
    if end is None:
        end = (max(b for _, b in pairs) + FRAG_SAMPLES) * 2
    f = io.BytesIO(b"\x00" * end)
    for k, (s, _) in enumerate(pairs):
        if s * RAW_SECTOR + FRAG > end:
            continue
        f.seek(s * RAW_SECTOR)
        f.write(bytes([0x10 + k]) * FRAG)
    f.seek(0)
    chain = [make_rec(0x40, flag=STEM_FLAG, prev=0x9999, pairs=pairs)]
    return f, chain, end


def test_render_stem_paste_at_boff_times_two():
    f, chain, end = stem_image()
    out, cov = render_stem(f, 0, end, chain)
    # timeline = (max_boff + FRAG_SAMPLES) samples of mono 16-bit
    assert len(out) == (0x1A4000 + FRAG_SAMPLES) * 2
    # fragment pasted at byte offset boff*2, exactly FRAG bytes
    p = 0x3C000 * 2
    assert out[p : p + FRAG] == bytes([0x10]) * FRAG
    # zeros until the next fragment (catches FRAG over-read AND byte/sample mixup)
    p2 = 0x84000 * 2
    assert out[p + FRAG : p2] == bytes(p2 - p - FRAG)


def test_render_stem_gap_and_coverage():
    f, chain, end = stem_image()
    out, cov = render_stem(f, 0, end, chain)
    p1 = 0x3C000 * 2
    p2 = 0x84000 * 2
    assert out[p1 + FRAG : p2] == b"\x00" * (p2 - p1 - FRAG)
    # 6 fragments x FRAG/4096 nonzero blocks over the whole timeline
    assert cov == (6 * (FRAG // 4096)) / ((0x1A4000 + FRAG_SAMPLES) * 2 // 4096)


def test_render_stem_skips_extent_past_end():
    """A fragment whose raw sector lies past the card end is skipped, not pasted."""
    end = (0x1A4000 + FRAG_SAMPLES) * 2
    past = (end // RAW_SECTOR) + 8
    pairs = [*STEM_PAIRS[:5], (past, 0x1A4000)]
    f, chain, end = stem_image(pairs=pairs, end=end)
    out, _ = render_stem(f, 0, end, chain)
    last = 0x1A4000 * 2
    assert out[last : last + FRAG] == bytes(FRAG)  # skipped => zeros


def test_render_stem_clamps_corrupt_boff():
    """A wild boff must not request a huge allocation; clamp to the card span."""
    pairs = [(8, 0x7FFF0000)]
    f, chain, end = stem_image(pairs=pairs, end=1 << 20)
    out, cov = render_stem(f, 0, end, chain)
    assert len(out) == end  # clamped to card span, not boff
    assert cov == 0.0  # fragment offset lands outside the timeline => skipped


# --- robustness ------------------------------------------------------------


def test_parse_mbr_rejects_short_image():
    with pytest.raises(ValueError, match="truncated"):
        parse_mbr(io.BytesIO(b"\x00" * 32))


def test_parse_header_rejects_short_image():
    with pytest.raises(ValueError, match="truncated"):
        parse_header(io.BytesIO(b"\x00" * (0x800 + 64)), 0)


def test_cli_reports_truncated_image_cleanly(tmp_path, capsys):
    from tascam_dp008ex_sd_audio_extractor.cli import main

    p = tmp_path / "tiny.img"
    p.write_bytes(b"\x00" * 32)  # shorter than the MBR partition table
    assert main(["list", str(p)]) == 2
    err = capsys.readouterr().err
    assert "error: ValueError" in err and "truncated" in err


# --- wav -------------------------------------------------------------------


def test_pcm_stats():
    pcm = struct.pack("<4h", 100, -100, 0, 300)
    rms, peak = pcm_stats(pcm)
    assert peak == 300
    assert abs(rms - (10000 + 10000 + 90000) ** 0.5 / 2) < 1e-9
    assert pcm_stats(b"") == (0.0, 0)


def test_write_wav_roundtrip(tmp_path):
    pcm = struct.pack("<4h", 0, 1, -1, 32767)
    p = str(tmp_path / "t.wav")
    write_wav(p, pcm)
    with wave.open(p) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 44100)
        assert w.readframes(4) == pcm
