"""Block-device discovery and classification (read-only).

Why this exists: the DP-008EX keeps multitrack audio in the *undeclared* region
after partition 0, so only the **whole device** contains the songs. A partition
node (/dev/sdb1, /dev/disk2s1, D:\\) exposes the FAT32 volume and nothing else --
reading one looks like an empty card. Every command here exists to make the
user pick the right node.
"""

import os
import re
import stat

# Whole-device nodes. These carry the MTR region.
WHOLE_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"^/dev/sd[a-z]+$",
        r"^/dev/hd[a-z]+$",
        r"^/dev/vd[a-z]+$",
        r"^/dev/xvd[a-z]+$",
        r"^/dev/nvme\d+n\d+$",
        r"^/dev/mmcblk\d+$",
        r"^/dev/disk\d+$",  # macOS
        r"^/dev/rdisk\d+$",  # macOS raw (faster, unbuffered)
    )
)
# Partition nodes. Reading one of these silently misses every song.
PARTITION_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"^/dev/sd[a-z]+\d+$",
        r"^/dev/hd[a-z]+\d+$",
        r"^/dev/vd[a-z]+\d+$",
        r"^/dev/xvd[a-z]+\d+$",
        r"^/dev/nvme\d+n\d+p\d+$",
        r"^/dev/mmcblk\d+p\d+$",
        r"^/dev/disk\d+s\d+$",
        r"^/dev/rdisk\d+s\d+$",
    )
)
WIN_WHOLE = re.compile(r"^\\\\\.\\PhysicalDrive\d+$", re.IGNORECASE)
WIN_PARTITION = re.compile(r"^(\\\\.\\)?[A-Za-z]:(\\|/)?$")


class PartitionNodeError(ValueError):
    """Raised when the user points at a partition instead of the whole device."""


def is_block_device(path):
    """True for a block device node. Always False on platforms without one."""
    isblk = getattr(stat, "S_ISBLK", None)
    if isblk is None:
        return bool(WIN_WHOLE.match(str(path)))
    try:
        return isblk(os.stat(path).st_mode)
    except OSError:
        return False


def is_whole_device(path):
    p = str(path)
    if WIN_WHOLE.match(p):
        return True
    return any(rx.match(p) for rx in WHOLE_PATTERNS)


def is_partition_node(path):
    p = str(path)
    if WIN_PARTITION.match(p):
        return True
    return any(rx.match(p) for rx in PARTITION_PATTERNS)


def whole_device_for(path):
    """The whole-device node corresponding to a partition node, or None."""
    p = str(path)
    if not is_partition_node(p):
        return None
    if WIN_PARTITION.match(p):
        return r"\\.\PhysicalDriveN"
    for pattern in (r"p[0-9]+$", r"s[0-9]+$", r"[0-9]+$"):
        whole = re.sub(pattern, "", p)
        if whole != p and is_whole_device(whole):
            return whole
    return None


def partition_hint(path):
    """Actionable guidance for a partition node.

    Written without backslashes inside f-string expressions: that is a 3.12+
    syntax feature and this package supports 3.10.
    """
    whole = whole_device_for(path)
    if whole is None:
        return "point at the whole disk, not a partition"
    return f"try {whole} instead"


def check_read_target(path):
    """Validate a path that will be read. Returns a short label for banners.

    Raises PartitionNodeError with guidance if a partition node was given.
    """
    p = str(path)
    if is_partition_node(p):
        raise PartitionNodeError(
            f"{p} is a partition, not a whole device. The DP-008EX stores the "
            f"multitrack audio outside any partition, so this would read as an "
            f"empty card. {partition_hint(p)}"
        )
    return "block device" if is_block_device(p) else "image file"


def _sysfs(name, attr):
    """Read a /sys/class/block attribute, or None when unavailable."""
    try:
        with open(f"/sys/class/block/{name}/{attr}") as f:
            return f.read().strip()
    except OSError:
        return None


def device_size(path):
    """Size in bytes, or None when it cannot be determined without privileges."""
    p = str(path)
    name = os.path.basename(p)
    if os.path.isdir("/sys/class/block"):
        raw = _sysfs(name, "size")
        if raw and raw.isdigit():
            return int(raw) * 512
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
        return size or None
    except OSError:
        return None


def is_removable(path):
    """True/False from sysfs, else None (unknown on macOS/Windows)."""
    name = os.path.basename(str(path))
    if not os.path.isdir("/sys/class/block"):
        return None
    raw = _sysfs(name, "removable")
    if raw is None:
        return None
    return raw == "1"


def can_read(path):
    try:
        with open(path, "rb") as f:
            f.read(512)
        return True
    except OSError:
        return False


def list_device_nodes():
    """Candidate whole-device nodes on this platform (stdlib only, no subprocess)."""
    import glob

    found = set()
    for pattern in ("/dev/sd?", "/dev/hd?", "/dev/vd?", "/dev/nvme?n?", "/dev/mmcblk?"):
        found.update(glob.glob(pattern))
    if os.path.isdir("/dev"):
        found.update(glob.glob("/dev/disk[0-9]*"))
        found.update(glob.glob("/dev/rdisk[0-9]*"))
    return sorted(p for p in found if is_whole_device(p))


def parse_mounts(text):
    """Parse /proc/mounts (or /proc/self/mounts) into (device, mountpoint, fstype)."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mountpoint, fstype = parts[0], parts[1], parts[2]
        if device.startswith("/dev/") and not device.startswith("/dev/loop"):
            out.append((device, mountpoint.replace("\\040", " "), fstype.lower()))
    return out


def mounted_volumes(path="/proc/self/mounts", only=None):
    """Mounted volumes that sit on a candidate device.

    A mounted DP-008EX card looks empty, which is the trap: the songs are not on
    the filesystem. This surfaces the mount and the whole device that actually
    holds them. `only` filters by filesystem type (e.g. {"vfat", "exfat"}).
    """
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return []
    found = []
    for device, mountpoint, fstype in parse_mounts(text):
        if only and fstype not in only:
            continue
        whole = whole_device_for(device) or (
            device if is_whole_device(device) else None
        )
        if whole is None:
            continue
        label = os.path.basename(mountpoint.rstrip("/")) or mountpoint
        found.append(
            {
                "partition": device,
                "whole": whole,
                "mountpoint": mountpoint,
                "fstype": fstype,
                "label": label,
                "looks_like_dp008ex": "dp-008" in label.lower()
                or "dp008" in label.lower(),
            }
        )
    return found
