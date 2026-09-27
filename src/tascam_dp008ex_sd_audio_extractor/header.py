"""MTR main header at MTR_BASE+0x800 (128B, big-endian u32). Read-only."""

import struct

HEADER_OFF = 0x800
HEADER_LEN = 128


def parse_header(f, mtr_base):
    f.seek(mtr_base + HEADER_OFF)
    raw = f.read(HEADER_LEN)
    if len(raw) < HEADER_LEN:
        raise ValueError(
            f"truncated image: MTR header needs {HEADER_LEN} bytes at "
            f"{mtr_base + HEADER_OFF:#x}, got {len(raw)}"
        )
    words = struct.unpack(">32I", raw)
    return {
        "magic": words[0],
        "sector_size": words[9],  # +0x24
        "reserved_sectors": words[11],  # +0x2C
        "reserved_bytes": words[12],  # +0x30
        "hdr_sector": (mtr_base + HEADER_OFF) // 512,
        "mtr_sector": mtr_base // 512,
        # DP-008EX observed: self-ptr at +0x08, mtr start +0x38, bitmap +0x3C,
        # BFS ROOT ptr at +0x68 (shifted +8 vs DP-03's +0x60), backup root +0x74
        "self_ptr": words[2],
        "mtr_start_ptr": words[14],
        "bitmap_ptr": words[15],
        "bfs_root_ptr_cand": words[26],  # +0x68
        "backup_root_cand": words[29],  # +0x74
    }
