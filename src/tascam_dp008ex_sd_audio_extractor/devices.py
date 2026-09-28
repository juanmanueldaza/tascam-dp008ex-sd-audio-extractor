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


def parse_diskutil_plist(blob):
    """(size_bytes, removable) from `diskutil info -plist` output.

    Split out from the subprocess call so the macOS interpretation can be tested
    on any platform; a Mac is the only place the OS actually runs it.
    """
    import plistlib
    from xml.parsers.expat import ExpatError

    try:
        info = plistlib.loads(blob)
    except (ExpatError, ValueError, TypeError):
        return (None, None)
    if not isinstance(info, dict):
        return (None, None)
    size = info.get("TotalSize")
    removable = info.get("RemovableMedia")
    if removable is None:
        # Whole disks report Ejectable rather than RemovableMedia.
        removable = info.get("Ejectable")
    return (
        int(size) if isinstance(size, int) and size > 0 else None,
        bool(removable) if isinstance(removable, bool) else None,
    )


def _darwin_disk_info(path):
    """(size_bytes, removable) for a /dev/diskN node, or (None, None).

    macOS has no /sys and device nodes report size 0, so diskutil is the
    reliable source. It ships with the OS; subprocess and plistlib are stdlib,
    so the package still has zero third-party dependencies.
    """
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
    except (OSError, subprocess.SubprocessError):
        return (None, None)
    return parse_diskutil_plist(done.stdout)


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


class AlignedReader:
    """Byte-exact reader for raw devices, with sector-aligned I/O.

    Windows rejects a read that does not start on a sector boundary when the
    handle is a raw ``\\\\.\\PhysicalDriveN``, and the MBR partition table is read
    64 bytes at 0x1BE, so an ordinary buffered open dies with OSError partway
    through probing a card. Every read is therefore issued from the sector that
    contains it and the result sliced down. Buffering is bypassed so no hidden
    unaligned prefetch can reintroduce the same problem.

    Behaves like a binary file object for the parts this package uses: seek,
    read, tell, close, and the context-manager protocol.
    """

    def __init__(self, path, size=None):
        self._fd: int = os.open(str(path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        self._closed = False
        try:
            self._pos = 0
            if size is None:
                try:
                    size = os.lseek(self._fd, 0, os.SEEK_END)
                except OSError:
                    # Windows rejects SEEK_END on a raw disk handle (Errno 22).
                    # device_size() asks the OS instead, via DeviceIoControl on
                    # Windows and sysfs or a plain seek elsewhere.
                    size = device_size(path)
            self._size: int | None = int(size) if size else None
        except OSError:
            os.close(self._fd)
            self._closed = True
            raise

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=os.SEEK_SET):
        if self._closed:
            raise ValueError("I/O operation on closed file")
        if whence == os.SEEK_SET:
            self._pos = offset
        elif whence == os.SEEK_CUR:
            self._pos += offset
        elif whence == os.SEEK_END:
            if self._size is None:
                raise ValueError("device size is unknown, cannot seek from the end")
            self._pos = self._size + offset
        else:
            raise ValueError(f"invalid whence {whence!r}")
        return self._pos

    def read(self, size=-1):
        if self._closed:
            raise ValueError("I/O operation on closed file")
        if size is None or size < 0:
            if self._size is None:
                raise ValueError("device size is unknown, cannot read to the end")
            size = max(0, self._size - self._pos)
        if size == 0 or (self._size is not None and self._pos >= self._size):
            return b""
        sector = 512
        start = self._pos
        skip = start % sector
        want = skip + size
        want = -(-want // sector) * sector  # round up to a whole sector
        os.lseek(self._fd, start - skip, os.SEEK_SET)
        buf = os.read(self._fd, want)
        if len(buf) <= skip:
            if self._size is not None:
                self._pos = self._size
            else:
                self._pos += max(0, size)
            return b""
        out = buf[skip : skip + size]
        # Advance by what the caller was given, not by the sector-rounded
        # amount actually pulled from the device: after read(64) from offset 0 a
        # file object must report position 64, not 512.
        self._pos = start + len(out)
        return out

    def close(self):
        if not self._closed:
            os.close(self._fd)
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def open_device(path, mode="rb"):
    """Open an image or a raw device for reading, aligned where it matters.

    Plain files get a normal buffered open. Raw Windows physical drives get
    AlignedReader, because that is the only way a 64-byte read at 0x1BE works.
    """
    if "r" in mode and "+" not in mode and WIN_WHOLE.match(str(path)):
        return AlignedReader(path)
    return open(path, mode)


def privilege_hint():
    """What this platform actually requires to read a raw device."""
    if os.name == "nt":
        return "needs Administrator"
    return "needs root or the disk group"


# --- Windows -----------------------------------------------------------
# ctypes.windll / ctypes.wintypes only exist on Windows, so they are reached
# through getattr(): the module has to import (and type-check) on Linux/macOS.
# No third-party dependency, no subprocess.

_GENERIC_READ = 0x80000000
_SHARE_READ_WRITE = 0x00000003
_OPEN_EXISTING = 3
_IOCTL_DISK_GET_LENGTH_INFO = 0x7405C
_IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
# STORAGE_PROPERTY_QUERY { DWORD PropertyId; DWORD QueryType; UCHAR Extra[1]; } is
# 12 bytes once padded. Sending only the two DWORDs makes Windows reject the
# query with ERROR_BAD_LENGTH, which is how the removable flag silently came
# back unknown.
_STORAGE_PROPERTY_QUERY_LEN = 12
# STORAGE_DEVICE_DESCRIPTOR field offsets: Version@0, Size@4, DeviceType@8,
# DeviceTypeModifier@9, RemovableMedia@10. Reading @8 would report the bus type
# as if it were the removable flag.
_STOR_DEV_REMOVABLE = 10
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
        # PropertyId=StorageDeviceProperty (0), QueryType=0. The struct is 12
        # bytes once padded; sending only the two DWORDs gets the query rejected.
        query = struct.pack("<II4x", 0, 0)
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
            removable = bool(desc.raw[_STOR_DEV_REMOVABLE])
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
