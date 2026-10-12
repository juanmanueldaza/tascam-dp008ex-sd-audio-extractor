"""Bounded read-only scans: BFS ROOT, song slots, alloc table."""

import re
import struct

from .extract import ALLOC_LEN, ALLOC_OFF
from .report import Kind, Skip

SECTOR = 512
SONG_RE = re.compile(rb"S(\d{3})\x00{0,4}SONG(\d{3})")


def find_bfs_roots(f, mtr_base, max_sectors=2048, skips=None):
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
    if len(hits) >= 16 and skips is not None:
        skips.append(
            Skip(
                site="SC1",
                kind=Kind.CAP,
                detail="BFS ROOT search capped at 16 hits; later hits unreported",
            )
        )
    return hits


def scan_song_slots(f, mtr_base, window_bytes=128 << 20, chunk=4 << 20, skips=None):
    """Scan first window_bytes of MTR for Sxxx/SONGxxx pairs. Bounded; returns matches.

    Consecutive chunks overlap by OVERLAP bytes so a match straddling a
    chunk boundary is found exactly once (dedupe by abs_offset). OVERLAP =
    14 because the longest SONG_RE match is 15 bytes (S + 3 digits + up to
    4 nulls + SONG + 3 digits): a straddler hides at most 14 bytes deep in
    the previous chunk. Fixes the SC3 boundary miss.
    The 400-match cap emits a single SC4 cap Skip at the truncation point
    (the loop guard SC2 exits on the same event; one cap, one skip).
    """
    OVERLAP = 14
    matches = []
    seen = set()
    f.seek(mtr_base)
    remaining = window_bytes
    base = mtr_base
    carry = b""
    while remaining > 0 and len(matches) < 400:
        buf = f.read(min(chunk, remaining))
        if not buf:
            break
        data = carry + buf
        data_base = base - len(carry)
        for m in SONG_RE.finditer(data):
            abs_off = data_base + m.start()
            if abs_off in seen:
                continue
            seen.add(abs_off)
            matches.append(
                {
                    "abs_offset": abs_off,
                    "abs_hex": hex(abs_off),
                    "short": m.group(0)[:16].hex(),
                    "s_idx": m.group(1).decode(),
                    "song": "SONG" + m.group(2).decode(),
                }
            )
            if len(matches) >= 400:
                if skips is not None:
                    skips.append(
                        Skip(
                            site="SC4",
                            kind=Kind.CAP,
                            detail=(
                                "song-slot report capped at 400 matches; "
                                "later slots unreported"
                            ),
                        )
                    )
                break
        carry = data[-OVERLAP:]
        base += len(buf)
        remaining -= len(buf)
    return matches


def parse_song_slots(f, mtr_base, start, n=250, stride=0x24):
    """Read n stride-byte slot records at MTR-relative start; one dict per slot.

    start is REQUIRED (MTR-relative byte offset): the slot-array locator is
    a single-card README observation with two competing readings, so no
    default is guessed (docs/song-slot-hypothesis.md H1/H2). Short reads
    yield fewer than n entries, never fabricated rows. Field meanings are
    HYPOTHESIS; slots are never linked to chains here (AC-20).
    """
    f.seek(mtr_base + start)
    slots = []
    for i in range(n):
        raw = f.read(stride)
        if len(raw) < stride:
            break
        m = SONG_RE.search(raw)
        slots.append(
            {
                "index": i,
                "abs_offset": mtr_base + start + i * stride,
                "abs_hex": hex(mtr_base + start + i * stride),
                "s_idx": m.group(1).decode() if m else None,
                "song": ("SONG" + m.group(2).decode()) if m else None,
                "raw_hex": raw.hex(),
            }
        )
    return slots


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
