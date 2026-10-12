"""FAT32 partition-0 export harvest (read-only).

Reads the FAT32 volume inside partition 0 of an already-open whole-device
or image file object (never a mount, never a partition node). Copies
WAVE/*.WAV master exports verbatim plus BACKUP/ files opaque.

Every loss emits a named Skip (F1-F7), reusing report.Kind; no new Kind.
"""

import os
import struct
from dataclasses import dataclass
from pathlib import Path

from .report import Kind, Skip

FAT32_TYPES = (0x0B, 0x0C)  # 0x0C FAT32-LBA parses identically (HYPOTHESIS)
EOC_MIN = 0x0FFFFFF8
FAT_BAD = 0x0FFFFFF7
FAT_MASK = 0x0FFFFFFF
MAX_EXPORT_BYTES = 1 << 32  # FAT32 fileSize is u32: the format ceiling
MAX_DIR_ENTRIES = 400  # mirrors scan SC4 400-match cap
MAX_DIR_DEPTH = 16  # mirrors scan SC1 16-hit cap; walker descends <= 2
VALID_BPS = (512, 1024, 2048, 4096)
_SAFE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~-")


@dataclass(frozen=True, slots=True)
class Export:
    """One export candidate: sanitized name, declared size, first cluster."""

    name: str
    size: int
    first_cluster: int


def parse_bpb(f, base, end):
    """Parse the boot sector; None when empty, ValueError when corrupt.

    All-zero (or absent) boot sector = no filesystem ever written: silent
    (the default synthetic builder leaves 64 zero partition sectors).
    Nonzero-but-invalid = corrupt FAT32.
    """
    n = min(512, max(0, end - base))
    f.seek(base)
    boot = f.read(n)
    if len(boot) == 0 or not any(boot):
        return None
    if len(boot) < 512:
        raise ValueError(f"short boot sector: {len(boot)} of 512 bytes")
    bps = struct.unpack_from("<H", boot, 11)[0]
    spc = boot[13]
    res = struct.unpack_from("<H", boot, 14)[0]
    nfats = boot[16]
    spfat = struct.unpack_from("<I", boot, 36)[0]
    root = struct.unpack_from("<I", boot, 44)[0]
    if bps not in VALID_BPS:
        raise ValueError(f"bad bytes-per-sector: {bps}")
    if spc == 0 or spc & (spc - 1):
        raise ValueError(f"bad sectors-per-cluster: {spc}")
    if nfats == 0:
        raise ValueError("no FATs")
    fat_start = base + res * bps
    data_start = base + (res + nfats * spfat) * bps
    cluster_size = spc * bps
    max_clusters = (end - data_start) // cluster_size
    if data_start >= end or max_clusters < 3:
        raise ValueError("data region outside partition")
    if not 2 <= root < max_clusters + 2:
        raise ValueError(f"root cluster out of range: {root}")
    return {
        "bytes_per_sector": bps,
        "sectors_per_cluster": spc,
        "fat_start": fat_start,
        "data_start": data_start,
        "cluster_size": cluster_size,
        "max_clusters": max_clusters,
        "root_cluster": root,
    }


def _fat_entry(f, bpb, cluster):
    """Raw masked FAT value for a cluster index (bounds-checked by caller)."""
    f.seek(bpb["fat_start"] + cluster * 4)
    raw = f.read(4)
    if len(raw) < 4:
        return None
    return struct.unpack("<I", raw)[0] & FAT_MASK


def _follow_clusters(f, base, end, bpb, first, skips, what):
    """Follow a cluster chain; None on corruption (F3/F4 emitted).

    A corrupt chain writes NOTHING (length/order untrustworthy); the skip
    records the loss and strict mode fails. Distinct from F1 short-reads,
    where a byte prefix is still recoverable.
    """
    del base, end  # geometry comes from bpb, not the partition bounds
    chain, seen = [], set()
    cur = first
    while True:
        if cur < 2 or cur >= bpb["max_clusters"] + 2:
            skips.append(
                Skip(
                    site="F3",
                    kind=Kind.OOB,
                    detail=f"{what}: cluster {cur} outside data region",
                )
            )
            return None
        if cur in seen:
            skips.append(
                Skip(
                    site="F4",
                    kind=Kind.OOB,
                    detail=f"{what}: cluster chain revisits {cur}",
                )
            )
            return None
        seen.add(cur)
        chain.append(cur)
        val = _fat_entry(f, bpb, cur)
        if val is None or val == FAT_BAD or val < 2:
            skips.append(
                Skip(
                    site="F3",
                    kind=Kind.OOB,
                    detail=f"{what}: FAT entry for cluster {cur} unreadable",
                )
            )
            return None
        if val >= EOC_MIN:
            return chain
        cur = val


def _read_dir(f, base, end, bpb, first_cluster, skips, depth=0):
    """Raw (name11, attr, cluster, size) entries of one directory."""
    if depth > MAX_DIR_DEPTH:
        skips.append(
            Skip(site="F5", kind=Kind.CAP, detail="directory nesting over cap")
        )
        return []
    chain = _follow_clusters(f, base, end, bpb, first_cluster, skips, what="directory")
    if chain is None:
        return []
    entries = []
    for c in chain:
        f.seek(bpb["data_start"] + (c - 2) * bpb["cluster_size"])
        raw = f.read(bpb["cluster_size"])
        if len(raw) < bpb["cluster_size"]:
            skips.append(
                Skip(
                    site="F1",
                    kind=Kind.SHORT_READ,
                    detail=(
                        f"directory cluster {c}: got {len(raw)} "
                        f"of {bpb['cluster_size']} bytes"
                    ),
                )
            )
            break
        for i in range(0, len(raw), 32):
            rec = raw[i : i + 32]
            if rec[0] == 0x00:
                return entries  # end-of-table marker
            if rec[0] == 0xE5:
                continue  # deleted entry: intentional, not loss
            attr = rec[11]
            if attr == 0x0F or attr & 0x08:
                continue  # LFN fragment (short name parsed) / volume label
            cluster = (struct.unpack_from("<H", rec, 20)[0] << 16) | struct.unpack_from(
                "<H", rec, 26
            )[0]
            size = struct.unpack_from("<I", rec, 28)[0]
            entries.append((rec[:11], attr, cluster, size))
            if len(entries) >= MAX_DIR_ENTRIES:
                skips.append(
                    Skip(
                        site="F5",
                        kind=Kind.CAP,
                        detail="directory capped at 400 entries",
                    )
                )
                return entries
    return entries


def _decode_83(name11):
    """Decode 11-byte 8.3 name to 'STEM.EXT' (cp437-intolerant bytes survive)."""
    stem = name11[:8].decode("ascii", errors="replace").rstrip()
    ext = name11[8:11].decode("ascii", errors="replace").rstrip()
    return stem + ("." + ext if ext else "")


def _sanitize(name, first_cluster):
    """Return (safe_name, changed). Safe chars only; fallback never escapes.

    Comparison is against the ORIGINAL decoded name: even pure case
    normalization (exotic/lowercase raw bytes) is a rename worth reporting.
    """
    upper = name.upper()
    safe = "".join(c if c in _SAFE_CHARS else "_" for c in upper)
    if safe in ("", ".", "..") or "/" in safe or "\\" in safe:
        return f"export_{first_cluster:08x}", True
    return safe, safe != name


def enumerate_exports(f, base, end, bpb, skips):
    """WAVE/*.WAV (verbatim candidates) + BACKUP/* (opaque) as Exports.

    Names are sanitized here so Export always holds a safe basename; F7
    fires per renamed file. UTILITY/ and System Volume Information are
    never descended (documented scope exclusion, not loss).
    """
    root = _read_dir(f, base, end, bpb, bpb["root_cluster"], skips)
    wanted = {}
    for name11, attr, cluster, _ in root:
        if not attr & 0x10:
            continue
        stem = name11[:8].decode("ascii", errors="replace").rstrip()
        if stem in ("WAVE", "BACKUP"):
            wanted[stem] = cluster
    exports = []
    for dirname in ("WAVE", "BACKUP"):
        if dirname not in wanted:
            continue
        for name11, attr, cluster, size in _read_dir(
            f, base, end, bpb, wanted[dirname], skips, depth=1
        ):
            if attr & 0x10:
                continue  # no recursion into subdirectories
            decoded = _decode_83(name11)
            if dirname == "WAVE" and not decoded.upper().endswith(".WAV"):
                continue
            safe, changed = _sanitize(decoded, cluster)
            if changed:
                skips.append(
                    Skip(
                        site="F7",
                        kind=Kind.OMISSION,
                        detail=f"hostile name {decoded!r} sanitized to {safe!r}",
                    )
                )
            exports.append(Export(name=safe, size=size, first_cluster=cluster))
    return exports


def _unique_path(out_dir, basename):
    """Collision-proof path: _N suffix, never overwrite."""
    stem, dot, ext = basename.rpartition(".")
    candidate = os.path.join(out_dir, basename)
    n = 1
    while os.path.exists(candidate):
        if dot:
            candidate = os.path.join(out_dir, f"{stem}_{n}.{ext}")
        else:
            candidate = os.path.join(out_dir, f"{basename}_{n}")
        n += 1
    return candidate


def copy_export(f, base, end, bpb, export, out_dir, skips):
    """Stream one export cluster-by-cluster to out_dir; Path or None.

    F2 (cap) and F3/F4 (corrupt chain) write nothing. F1 short-read keeps
    the recoverable prefix on disk. Zero-size files write empty, no skip.
    """
    data_region = end - bpb["data_start"]
    if export.size > min(MAX_EXPORT_BYTES, data_region):
        skips.append(
            Skip(
                site="F2",
                kind=Kind.CAP,
                detail=f"{export.name}: declared {export.size} bytes over cap",
            )
        )
        return None
    if export.size == 0:
        path = _unique_path(out_dir, export.name)
        Path(path).write_bytes(b"")
        return path
    if export.first_cluster < 2:
        skips.append(
            Skip(
                site="F3",
                kind=Kind.OOB,
                detail=f"{export.name}: first cluster {export.first_cluster}",
            )
        )
        return None
    chain = _follow_clusters(
        f, base, end, bpb, export.first_cluster, skips, what=export.name
    )
    if chain is None:
        return None
    path = _unique_path(out_dir, export.name)
    remaining = export.size
    with open(path, "wb") as w:
        for c in chain:
            if remaining <= 0:
                break
            f.seek(bpb["data_start"] + (c - 2) * bpb["cluster_size"])
            chunk = f.read(min(bpb["cluster_size"], remaining))
            if not chunk:
                break
            w.write(chunk)
            remaining -= len(chunk)
    if remaining > 0:
        skips.append(
            Skip(
                site="F1",
                kind=Kind.SHORT_READ,
                detail=(
                    f"{export.name}: got {export.size - remaining} "
                    f"of {export.size} bytes"
                ),
            )
        )
    return path


def harvest_exports(f, mbr, out_dir, skips):
    """Copy WAVE/*.WAV + BACKUP/* from partition 0; returns files written."""
    from .mbr import fat_bounds

    p0 = mbr["entries"][0]
    if p0["type"] == 0 and p0["sector_count"] == 0:
        return 0  # no partition declared: silent, nothing to harvest
    if p0["type"] not in FAT32_TYPES:
        skips.append(
            Skip(
                site="F6",
                kind=Kind.OMISSION,
                detail=f"partition type {p0['type']:#04x} is not FAT32",
            )
        )
        return 0
    base, end = fat_bounds(mbr)
    try:
        bpb = parse_bpb(f, base, end)
    except ValueError as e:
        skips.append(Skip(site="F6", kind=Kind.OMISSION, detail=str(e)))
        return 0
    if bpb is None:
        return 0  # all-zero boot sector: never formatted, silent
    os.makedirs(out_dir, exist_ok=True)
    written = 0
    for export in enumerate_exports(f, base, end, bpb, skips):
        if copy_export(f, base, end, bpb, export, out_dir, skips) is not None:
            written += 1
    return written


def count_wave_exports(f, mbr):
    """Guarded *.WAV count for the devices probe; 0 on any failure."""
    from .mbr import fat_bounds

    try:
        p0 = mbr["entries"][0]
        if p0["type"] not in FAT32_TYPES:
            return 0
        base, end = fat_bounds(mbr)
        bpb = parse_bpb(f, base, end)
        if bpb is None:
            return 0
        probe_skips = []  # probe contract: count or nothing, never report
        return len(
            [
                e
                for e in enumerate_exports(f, base, end, bpb, probe_skips)
                if e.name.upper().endswith(".WAV")
            ]
        )
    except (OSError, ValueError, struct.error):
        return 0
