"""Master extraction: extent chains -> per-song WAVs."""

import struct
from collections.abc import Iterable

from .carve import CLUSTER

ALLOC_OFF = 0x288000
ALLOC_LEN = 1 << 20
EXT_FLAG = 0x90010006  # master extents (card2: 2062 records; 0x90010005 absent)
HALF = 0xC000  # half a cluster in samples; used by verify's boundary metric
SECTOR_BYTES = CLUSTER  # BFS cluster = 0x18000 (header reserved field)
MAX_PCM_BYTES = 12 * 3600 * 44100 * 2  # 12 h mono 44.1kHz/16-bit render cap

Flags = int | Iterable[int]


def _flag_set(flags: Flags | None) -> set[int] | None:
    if flags is None:
        return None
    return {flags} if isinstance(flags, int) else set(flags)


def read_alloc(f, mbr_base, flags: Flags | None = None):
    """Non-zero 64B records from the alloc table. flags: None, int, or iterable."""
    want = _flag_set(flags)
    f.seek(mbr_base + ALLOC_OFF)
    blk = f.read(ALLOC_LEN)
    recs = []
    for i in range(len(blk) // 64):
        r = blk[i * 64 : (i + 1) * 64]
        if not any(r):
            continue
        a, fl, prev, nxt = struct.unpack(">4I", r[:16])
        if want is not None and fl not in want:
            continue
        recs.append(
            {"slot": i, "A": i * 64, "flag": fl, "prev": prev, "next": nxt, "raw": r}
        )
    return recs


def extent_pairs(rec):
    """Six (sector_id, file_offset) pairs from +0x10."""
    words = struct.unpack(">16I", rec["raw"])
    return [(words[4 + k * 2], words[5 + k * 2]) for k in range(6)]


def collect_chains(recs, flags: Flags = EXT_FLAG):
    """Walk extent chains from heads (prev not in A-set).

    flags: int or iterable of extent flags. Records of other flags are ignored;
    anything left over is emitted as a single-record chain.
    """
    want = _flag_set(flags)
    if want is None:
        want = {EXT_FLAG}
    ext = [r for r in recs if r["flag"] in want]
    by_a = {r["A"]: r for r in ext}
    aset = set(by_a)
    heads = [r for r in ext if r["prev"] not in aset]
    chains, visited = [], set()
    for h in heads:
        chain, cur = [], h
        while cur is not None and cur["A"] not in visited:
            if cur["flag"] not in want:
                break
            visited.add(cur["A"])
            chain.append(cur)
            cur = by_a.get(cur["next"])
        if chain:
            chains.append(chain)
    for r in ext:  # orphans (cycle safety)
        if r["A"] not in visited:
            visited.add(r["A"])
            chains.append([r])
    return chains


def render_chain(f, mtr_base, mtr_end, pairs, max_bytes=MAX_PCM_BYTES):
    """pairs sorted by file_offset; read whole 0x18000 cluster per sector.

    Stops at max_bytes (guard against a corrupt alloc table repeating sectors).
    """
    pairs = sorted(pairs, key=lambda p: p[1])
    out = bytearray()
    for sector, _boff in pairs:
        if len(out) >= max_bytes:
            break
        abs_off = mtr_base + sector * SECTOR_BYTES
        # sector 0 is the MTR header cluster, never audio (also catches the
        # zero-valued padding slots of an extent record)
        if sector == 0 or abs_off + SECTOR_BYTES > mtr_end or abs_off < mtr_base:
            continue
        f.seek(abs_off)
        out += f.read(SECTOR_BYTES)
    return bytes(out[:max_bytes])
