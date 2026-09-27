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


def mtr_bounds(mbr):
    """MTR = undeclared raw region after partition 0. Returns (base_byte, end_byte)."""
    p0 = mbr["entries"][0]
    base_sector = p0["lba_start"] + p0["sector_count"]
    base = base_sector * SECTOR
    return base, mbr["total_bytes"]
