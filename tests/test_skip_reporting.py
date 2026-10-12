"""Per-site skip triggers (A2): every runtime loss site fires its named Skip.

Inventory mapping (19 sites):
- runtime, asserted here: E1, E4, S1, S3, S5, C1, SC1, SC4, W1.
- runtime, site ids locked in test_render.py: E2, E3, S2, S4.
- runtime, asserted behaviorally below (exit codes + summary on fixtures
  built so only the target category can fire): CL1, CL2, CL3. The CL3
  fixture doubles as the AC-8 strict gate (non-zero without the flag,
  0 with it).
- eliminated: SC3 (fixed by the A3 overlap), W2 (documented
  display-sampling; mask kept, see wavio.pcm_stats docstring).
- subsumed: SC2 loop guard (same 400-cap event as SC4; one cap, one skip).

AC-9 audit (rg 'continue|break|[:-1]|[:max|& ~1' over src/): every hit is
a reported Skip, structural filtering, a normal loop exit, or documented
display-sampling. Zero unreported audio-data loss.
- Reported: E1 extract.py:124, E2 :94, E3 :113, E4 :136; S1 stems.py:54,
  S2 :66, S3 :77, S4 :86, S5 :95; C1 carve.py:66; SC1 scan.py:37,
  SC4 scan.py:87; CL1 cli.py:536, CL2 cli.py:661, CL3 cli.py:613;
  W1 wavio.py:24; A4 cli.py:421 (unpaired master, T11 addition).
- Structural (not loss): extract.py:34,37 zero/flag record filters; :107
  (0,0) extent padding (AC-4 needs pristine skip-free); :68 dead branch
  (unreachable: by_a holds only want-flagged records); stems.py:50 (0,0)
  padding; scan.py:140 zero-record filter; scan.py:72 overlap-dedupe skip
  (already-reported offset, not loss); cli.py:644 zero-sector filter.
- Normal loop exits: scan.py:22 (no more hits), :66 (EOF); carve.py:79
  user --limit; imaging.py:34,143,158 stream EOF (source defines length).
- Visible truncation (count printed, never silent): scan.py:116 songs
  short-read break — `songs` prints entries=N, so a short tail shows as a
  short count; songs is report-only, outside the strict command set.
- Structural (not loss): extract.py:34,37 zero/flag record filters; :107
  (0,0) extent padding (AC-4 needs pristine skip-free); :68 dead branch
  (unreachable: by_a holds only want-flagged records); stems.py:50 (0,0)
  padding; scan.py:96 zero-record filter; cli.py:535 zero-sector filter.
- Normal loop exits: scan.py:22 (no more hits), :58 (EOF); carve.py:79
  user --limit; imaging.py:34,143,158 stream EOF (source defines length).
- Display-sampling (documented, never audio data): cli.py pcm_stats
  bounded slices; wavio.py:51 even-length mask (load-bearing:
  iter_unpack raises on odd input); cli.py roots[:8]/slots[:20] print caps.
"""

import io
import wave
from pathlib import Path

from helpers import (
    ALLOC_OFF,
    CLUSTER,
    FRAG,
    MTR_BASE,
    POOL_CLUSTER0,
    STEM_SECTOR0,
    image_bytes,
)
from test_render import STEM_FLAG, make_rec

from tascam_dp008ex_sd_audio_extractor.carve import POOL_OFF, sweep
from tascam_dp008ex_sd_audio_extractor.cli import main
from tascam_dp008ex_sd_audio_extractor.extract import SECTOR_BYTES, render_chain
from tascam_dp008ex_sd_audio_extractor.report import Kind
from tascam_dp008ex_sd_audio_extractor.scan import find_bfs_roots, scan_song_slots
from tascam_dp008ex_sd_audio_extractor.stems import RAW_SECTOR, render_stem
from tascam_dp008ex_sd_audio_extractor.wavio import write_wav


def sites(skips):
    return [s.site for s in skips]


def test_e1_short_cluster_read():
    f = io.BytesIO(b"\x00" * (SECTOR_BYTES + 100))
    res = render_chain(f, 0, 1 << 20, [(1, 0)])
    assert res.pcm == b"\x00" * 100
    assert sites(res.skips) == ["E1"]
    assert res.skips[0].kind is Kind.SHORT_READ


def test_e4_render_cap_cut():
    end = 3 * SECTOR_BYTES
    f = io.BytesIO(b"\x00" * end)
    res = render_chain(f, 0, end, [(1, 0), (2, 0)], max_bytes=SECTOR_BYTES + 100)
    assert len(res.pcm) == SECTOR_BYTES + 100
    assert sites(res.skips) == ["E4"]
    assert res.skips[0].kind is Kind.TRUNCATION


def _stem_chain(pairs):
    return [make_rec(0x40, flag=STEM_FLAG, prev=0x9999, pairs=pairs)]


def test_s1_sector_zero_nonzero_boff():
    chain = _stem_chain([(0, 0x1000)])
    res = render_stem(io.BytesIO(b"\x00" * 64), 0, 1 << 20, chain)
    assert sites(res.skips) == ["S1"]
    assert res.skips[0].kind is Kind.OOB


def test_s3_short_fragment_read():
    o = 8 * RAW_SECTOR
    f = io.BytesIO(b"\x00" * (o + 100))
    res = render_stem(f, 0, 1 << 20, _stem_chain([(8, 0)]))
    assert sites(res.skips) == ["S3"]
    assert res.pcm[:100] == b"\x00" * 100


def test_s5_fragment_crosses_timeline_end():
    boff = 0x10000
    end = 2 * boff + 100
    f = io.BytesIO(b"\x00" * end)
    f.seek(8 * RAW_SECTOR)
    f.write(bytes([7]) * FRAG)
    f.seek(0)
    res = render_stem(f, 0, end, _stem_chain([(8, boff)]))
    assert sites(res.skips) == ["S5"]
    assert res.pcm[boff * 2 :] == bytes([7]) * 100


def test_c1_sweep_short_read():
    end = POOL_OFF + CLUSTER
    f = io.BytesIO(b"\x00" * (POOL_OFF + 100))
    skips = []
    assert list(sweep(f, 0, end, skips=skips)) == []
    assert sites(skips) == ["C1"]


def test_sc1_bfs_root_cap():
    f = io.BytesIO(b"\x00" * (17 * 512))
    for i in range(17):
        f.seek(i * 512)
        f.write(b"BFS ROOT")
    f.seek(0)
    skips = []
    hits = find_bfs_roots(f, 0, skips=skips)
    assert len(hits) == 16
    assert sites(skips) == ["SC1"]


def test_sc4_slot_cap():
    f = io.BytesIO(
        b"".join(b"S%03d\x00\x00SONG%03d" % (i % 1000, i % 1000) for i in range(401))
    )
    skips = []
    matches = scan_song_slots(f, 0, skips=skips)
    assert len(matches) == 400
    assert sites(skips) == ["SC4"]


def test_sc3_straddling_slot_found_exactly_once():
    """A3: a 15-byte Sxxx/SONGxxx match straddling a chunk boundary is
    returned exactly once with the correct abs_offset (SC3 fixed)."""
    f = io.BytesIO(b"\x00" * 1024)
    f.seek(250)  # straddles the 256-byte chunk edge (250..265)
    f.write(b"S007\x00\x00SONG007")
    f.seek(300)  # fully inside chunk 2: control match
    f.write(b"S008\x00\x00SONG008")
    f.seek(0)
    matches = scan_song_slots(f, 0, window_bytes=1024, chunk=256)
    offs = [m["abs_offset"] for m in matches]
    assert offs == [250, 300]
    assert matches[0]["s_idx"] == "007" and matches[0]["song"] == "SONG007"


def test_list_output_locked_to_baseline(tmp_path, capsys):
    """AC-12: <=400 slots list output byte-identical to pre-A3 (the banner
    line carries the tmp path by design, so the lock-in starts after it;
    pristine emits no skips, hence no strict summary line)."""
    from helpers import build_image

    p = build_image(tmp_path / "card.img")
    assert main(["list", str(p)]) == 0
    out = capsys.readouterr().out.splitlines(keepends=True)[1:]
    baseline = (Path(__file__).parent / "data" / "list_baseline.txt").read_text()
    assert "".join(out) == baseline


def test_w1_odd_byte_reported(tmp_path):
    p = str(tmp_path / "o.wav")
    skips = write_wav(p, b"\x00\x01\x02")
    assert sites(skips) == ["W1"]
    assert skips[0].kind is Kind.ODD_BYTE
    with wave.open(p) as w:
        assert w.getnframes() == 1


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(bytes(data))
    return str(p)


def test_cl1_short_master_read_strict(tmp_path, capsys):
    """Truncation mid-master-cluster: CL1 fires on the short master reads
    (CL3 co-fires on the missing stem tail); size fields and metadata
    checks are intact so fails stay empty and --allow-incomplete exits 0."""
    img = bytearray(image_bytes())
    cut = MTR_BASE + (POOL_CLUSTER0 + 1) * CLUSTER + 1000
    p = _write(tmp_path, "cl1.img", img[:cut])
    assert main(["verify", p]) == 1
    assert "incomplete" in capsys.readouterr().out
    assert main(["verify", p, "--allow-incomplete"]) == 0


def test_cl3_short_stem_read_strict(tmp_path, capsys):
    """Truncation mid-first-stem-fragment: masters and size fields intact,
    so only CL3 can fire; fails stay empty. AC-7 exact summary shape +
    AC-8 strict gate are locked here."""
    img = bytearray(image_bytes())
    cut = MTR_BASE + STEM_SECTOR0 * RAW_SECTOR + 100
    p = _write(tmp_path, "cl3.img", img[:cut])
    assert main(["verify", p]) == 1
    out = capsys.readouterr().out
    assert "2 of 3 chains incomplete (2 skips)" in out
    assert main(["verify", p, "--allow-incomplete"]) == 0


def test_cl2_missing_size_fields_omission(tmp_path, capsys):
    """Zeroed size-field records: durations uncheckable, so CL2 fires; the
    omission also fails its own 'size fields found' check, hence exit 1
    even with --allow-incomplete (real fails dominate strict)."""
    img = bytearray(image_bytes())
    img[MTR_BASE + ALLOC_OFF : MTR_BASE + ALLOC_OFF + 128] = b"\x00" * 128
    p = _write(tmp_path, "cl2.img", img)
    assert main(["verify", p]) == 1
    out = capsys.readouterr().out
    assert "incomplete" in out
    assert "size fields found" in out
    assert main(["verify", p, "--allow-incomplete"]) == 1
