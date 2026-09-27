"""Bounded read-only scans: BFS ROOT, song slots, alloc table."""

import re
import struct

from .extract import ALLOC_LEN, ALLOC_OFF

SECTOR = 512
SONG_RE = re.compile(rb"S(\d{3})\x00{0,4}SONG(\d{3})")


def find_bfs_roots(f, mtr_base, max_sectors=2048):
    """Scan first max_sectors of MTR for b'BFS ROOT' (one bounded read)."""
    hits = []
    f.seek(mtr_base)
    data = f.read(max_sectors * SECTOR)
    start = 0
    while len(hits) < 16:
        i = data.find(b"BFS ROOT", start)
        if i < 0:
            break
        abs_off = mtr_base + i
        sec_off = i % SECTOR
        hits.append(
            {
                "abs_offset": abs_off,
                "abs_sector": (abs_off - sec_off) // SECTOR,
                "sector_off": sec_off,
                "abs_hex": hex(abs_off),
            }
        )
        start = i + 1
    return hits


def scan_song_slots(f, mtr_base, window_bytes=128 << 20, chunk=4 << 20):
    """Scan first window_bytes of MTR for Sxxx/SONGxxx pairs. Bounded; returns matches."""
    matches = []
    f.seek(mtr_base)
    remaining = window_bytes
    base = mtr_base
    while remaining > 0 and len(matches) < 400:
        buf = f.read(min(chunk, remaining))
        if not buf:
            break
        for m in SONG_RE.finditer(buf):
            matches.append(
                {
                    "abs_offset": base + m.start(),
                    "abs_hex": hex(base + m.start()),
                    "short": m.group(0)[:16].hex(),
                    "s_idx": m.group(1).decode(),
                    "song": "SONG" + m.group(2).decode(),
                }
            )
            if len(matches) >= 400:
                break
        base += len(buf)
        remaining -= len(buf)
    return matches


def scan_alloc_table(f, mtr_base):
    """Read 1MiB alloc table at MTR+0x288000. Returns flag histogram + record counts."""
    f.seek(mtr_base + ALLOC_OFF)
    blk = f.read(ALLOC_LEN)
    nrec = len(blk) // 64
    hist = {}
    for i in range(nrec):
        r = blk[i * 64 : (i + 1) * 64]
        if not any(r):
            continue
        fl = struct.unpack(">I", r[4:8])[0]
        hist[fl] = hist.get(fl, 0) + 1
    return {
        "records": nrec,
        "histogram": hist,
        # both size-field variants count as song masters (cf. verify)
        "song_masters": hist.get(0x80030000, 0) + hist.get(0x80030001, 0),
        "audio_inodes": hist.get(0x90010001, 0),
        "extent_first": hist.get(0x90010005, 0),
        "extent_second": hist.get(0x90010006, 0),
    }
