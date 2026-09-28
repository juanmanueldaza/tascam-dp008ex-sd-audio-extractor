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


def _darwin_disk_info(path):
    """(size_bytes, removable) from `diskutil info -plist`, or (None, None).

    macOS has no /sys and device nodes report size 0, so diskutil is the
    reliable source. It ships with the OS; subprocess and plistlib are stdlib,
    so the package still has zero third-party dependencies.
    """
    import plistlib
    import subprocess
    import sys

    if sys.platform != "darwin":
        return (None, None)
    try:
        done = subprocess.run(
            ["diskutil", "info", "-plist", str(path)],
            capture_output=True,
            timeout=15,
            check=False,
        )
        info = plistlib.loads(done.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return (None, None)
    if not isinstance(info, dict):
        return (None, None)
    size = info.get("TotalSize")
    removable = info.get("RemovableMedia")
    if removable is None:
        removable = info.get("Ejectable")
    return (
        int(size) if isinstance(size, int) and size > 0 else None,
        bool(removable) if isinstance(removable, bool) else None,
    )


def device_size(path):
    """Size in bytes, or None when it cannot be determined without privileges."""
    p = str(path)
    if WIN_WHOLE.match(p):
        return _win_device_info(p)[0]
    size, _ = _darwin_disk_info(p)
    if size is not None:
        return size
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
    """True/False from the platform, else None (unknown)."""
    p = str(path)
    if WIN_WHOLE.match(p):
        return _win_device_info(p)[1]
    _, removable = _darwin_disk_info(p)
    if removable is not None:
        return removable
    name = os.path.basename(p)
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


# --- Windows -----------------------------------------------------------
# ctypes.windll / ctypes.wintypes only exist on Windows, so they are reached
# through getattr(): the module has to import (and type-check) on Linux/macOS.
# No third-party dependency, no subprocess.

_GENERIC_READ = 0x80000000
_SHARE_READ_WRITE = 0x00000003
_OPEN_EXISTING = 3
_IOCTL_DISK_GET_LENGTH_INFO = 0x7405C
_IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
_STORAGE_PROPERTY_QUERY = 0
_DRIVE_REMOVABLE = 2
_MAX_PHYSICAL_DRIVES = 32


def _kernel32():
    import ctypes

    dll = getattr(ctypes, "WinDLL", None)
    if dll is None:  # not Windows
        return None
    k32 = dll("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = ctypes.c_int
    k32.DeviceIoControl.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
    ]
    k32.DeviceIoControl.restype = ctypes.c_int
    k32.GetLogicalDrives.restype = ctypes.c_ulong
    k32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    k32.GetDriveTypeW.restype = ctypes.c_ulong
    return k32


def _win_open(k32, path):
    import ctypes

    handle = k32.CreateFileW(
        path,
        _GENERIC_READ,
        _SHARE_READ_WRITE,
        None,
        _OPEN_EXISTING,
        0,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle is None or handle == invalid:
        return None
    return handle


def _win_device_info(path):
    """(size_bytes, removable) for a \\\\.\\PhysicalDriveN node, or (None, None)."""
    if os.name != "nt":
        return (None, None)
    k32 = _kernel32()
    if k32 is None:
        return (None, None)
    import ctypes
    import struct

    handle = _win_open(k32, str(path))
    if handle is None:
        return (None, None)
    try:
        size = None
        buf = ctypes.create_string_buffer(8)
        returned = ctypes.c_ulong(0)
        if k32.DeviceIoControl(
            handle,
            _IOCTL_DISK_GET_LENGTH_INFO,
            None,
            0,
            buf,
            8,
            ctypes.byref(returned),
            None,
        ):
            size = struct.unpack("<Q", buf.raw[:8])[0] or None

        removable = None
        # STORAGE_PROPERTY_QUERY { DWORD PropertyId; DWORD QueryType; BYTE Extra[1]; }
        query = struct.pack("<II", _STORAGE_PROPERTY_QUERY, 0)
        desc = ctypes.create_string_buffer(256)
        if k32.DeviceIoControl(
            handle,
            _IOCTL_STORAGE_QUERY_PROPERTY,
            query,
            len(query),
            desc,
            256,
            ctypes.byref(returned),
            None,
        ):
            # STORAGE_DEVICE_DESCRIPTOR: ... DeviceType@6, RemovableMedia@8
            removable = bool(desc.raw[8])
        return (size, removable)
    finally:
        k32.CloseHandle(handle)


def _win_physical_drives():
    """Every \\\\.\\PhysicalDriveN the OS lets us open, in numeric order."""
    if os.name != "nt":
        return []
    k32 = _kernel32()
    if k32 is None:
        return []
    out = []
    for n in range(_MAX_PHYSICAL_DRIVES):
        node = rf"\\.\PhysicalDrive{n}"
        handle = _win_open(k32, node)
        if handle is not None:
            k32.CloseHandle(handle)
            out.append(node)
    return out


def _win_removable_drives():
    """Drive letters Windows reports as removable, e.g. ['E:']."""
    if os.name != "nt":
        return []
    k32 = _kernel32()
    if k32 is None:
        return []
    mask = k32.GetLogicalDrives()
    out = []
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(ord('A') + i)}:\\"
            if k32.GetDriveTypeW(root) == _DRIVE_REMOVABLE:
                out.append(f"{chr(ord('A') + i)}:")
    return out


def list_device_nodes():
    """Candidate whole-device nodes on this platform.

    Linux/macOS: glob /dev. Windows: ask the OS which \\\\.\\PhysicalDriveN nodes
    exist (there is no /dev, and inventing names would be a guess). Either way,
    every node returned is filtered through is_whole_device().
    """
    import glob

    found = set()
    for pattern in ("/dev/sd?", "/dev/hd?", "/dev/vd?", "/dev/nvme?n?", "/dev/mmcblk?"):
        found.update(glob.glob(pattern))
    if os.path.isdir("/dev"):
        found.update(glob.glob("/dev/disk[0-9]*"))
        found.update(glob.glob("/dev/rdisk[0-9]*"))
    found.update(_win_physical_drives())
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


def parse_mount_output(text):
    """Parse BSD/macOS `mount` output into (device, mountpoint, fstype).

    /dev/disk2s1 on /Volumes/DP-008EX (msdos, local, nodev, nosuid)
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("/dev/") or " on " not in line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        fstype = ""
        if "(" in line:
            fstype = line[line.rfind("(") + 1 :].split(",")[0].split(")")[0]
        out.append((parts[0], parts[2], fstype.lower()))
    return out


def _mount_table():
    """(device, mountpoint, fstype) triples from whatever this OS provides."""
    if os.path.isfile("/proc/self/mounts"):
        try:
            with open("/proc/self/mounts") as f:
                return parse_mounts(f.read())
        except OSError:
            pass
    if os.name == "nt":
        return []
    import subprocess

    try:
        done = subprocess.run(["mount"], capture_output=True, timeout=15, check=False)
        return parse_mount_output(done.stdout.decode("utf-8", "replace"))
    except (OSError, subprocess.SubprocessError):
        return []


def mounted_volumes(path=None, only=None):
    """Mounted volumes that sit on a candidate device.

    A mounted DP-008EX card looks empty, which is the trap: the songs are not on
    the filesystem. This surfaces the mount and the whole device that actually
    holds them. `only` filters by filesystem type (e.g. {"vfat", "exfat"}).
    `path` reads a specific /proc/mounts file instead of the platform default.
    """
    if path is not None:
        try:
            with open(path) as f:
                table = parse_mounts(f.read())
        except OSError:
            return []
    else:
        table = _mount_table()
    volumes = []
    for device, mountpoint, fstype in table:
        if only and fstype not in only:
            continue
        whole = whole_device_for(device) or (
            device if is_whole_device(device) else None
        )
        if whole is None:
            continue
        label = os.path.basename(mountpoint.rstrip("/")) or mountpoint
        volumes.append(
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
    # Windows: a removable drive letter cannot be mapped to its physical drive
    # without walking the device stack, so report it without guessing.
    for letter in _win_removable_drives():
        label = letter.rstrip(":")
        volumes.append(
            {
                "partition": f"{label}:\\",
                "whole": "see the physical drives listed above",
                "mountpoint": f"{label}:\\",
                "fstype": "removable",
                "label": label,
                "looks_like_dp008ex": False,
            }
        )
    return volumes
