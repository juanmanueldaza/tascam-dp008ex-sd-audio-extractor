"""MBR parsing (read-only)."""

import struct

SECTOR = 512
MBR_SIG = b"\x55\xaa"


def parse_mbr(f):
    """Parse MBR from open binary file object. Returns dict with entries + total_sectors."""
    f.seek(0, 2)
    total_bytes = f.tell()
    f.seek(0x1BE)
    raw = f.read(64)
    if len(raw) < 64:
        raise ValueError(
            f"truncated image: MBR partition table needs 64 bytes, got {len(raw)}"
        )
    f.seek(0x1FE)
    sig = f.read(2)
    entries = []
    for i in range(4):
        e = raw[i * 16 : (i + 1) * 16]
        boot, _, ptype, _, lba, count = struct.unpack("<B3sB3sII", e)
        entries.append(
            {
                "index": i,
                "boot": boot,
                "type": ptype,
                "lba_start": lba,
                "sector_count": count,
            }
        )
    return {
        "total_bytes": total_bytes,
        "total_sectors": total_bytes // SECTOR,
        "entries": entries,
        "sig_ok": sig == MBR_SIG,
        "sig": sig.hex(),
    }


def nonempty_partitions(mbr):
    """Partition entries the card actually declares (type or length set)."""
    return [e for e in mbr["entries"] if e["type"] or e["sector_count"]]


def mtr_bounds(mbr):
    """MTR = undeclared raw region after partition 0. Returns (base_byte, end_byte)."""
    p0 = mbr["entries"][0]
    base_sector = p0["lba_start"] + p0["sector_count"]
    base = base_sector * SECTOR
    return base, mbr["total_bytes"]


def extra_partition_warning(mbr):
    """Warn when the layout is not the single-partition shape a DP-008EX uses.

    mtr_bounds() only looks at entry 0, so audio inside a second partition would
    be skipped without a word. A real DP-008EX card always has exactly one, which
    makes a second entry worth flagging rather than silently ignoring.
    """
    extra = nonempty_partitions(mbr)[1:]
    if not extra:
        return None
    where = ", ".join(f"type 0x{e['type']:02x} at LBA {e['lba_start']}" for e in extra)
    return (
        f"this card declares {len(extra) + 1} partitions ({where}); only the first "
        "is used to locate the MTR, so audio inside the others would be missed. "
        "A DP-008EX card normally has exactly one."
    )
