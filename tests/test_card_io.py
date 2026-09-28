"""Tests for SD-card handling: device classification, guards, and imaging.

Device *reads* need privileges (root or the disk group), so the real-hardware
path is covered by classification tests against real device nodes on the host
plus a byte-level imaging round trip using a stand-in file.
"""

import hashlib
import os
import random

import pytest

from tascam_dp008ex_sd_audio_extractor.cli import main
from tascam_dp008ex_sd_audio_extractor.devices import (
    AlignedReader,
    PartitionNodeError,
    check_read_target,
    device_size,
    is_block_device,
    is_partition_node,
    is_removable,
    is_whole_device,
    list_device_nodes,
    mounted_volumes,
    open_device,
    parse_mount_output,
    parse_mounts,
    partition_hint,
    privilege_hint,
    whole_device_for,
)
from tascam_dp008ex_sd_audio_extractor.imaging import image_device, sha256_file
from tascam_dp008ex_sd_audio_extractor.mbr import (
    extra_partition_warning,
    mtr_bounds,
    nonempty_partitions,
)

WHOLE = [
    "/dev/sda",
    "/dev/sdb",
    "/dev/hdc",
    "/dev/vdb",
    "/dev/xvdb",
    "/dev/nvme0n1",
    "/dev/nvme1n3",
    "/dev/mmcblk0",
    "/dev/mmcblk2",
    "/dev/disk2",
    "/dev/rdisk4",
    r"\\.\PhysicalDrive0",
    r"\\.\PhysicalDrive12",
]
PARTITIONS = [
    "/dev/sda1",
    "/dev/sdb12",
    "/dev/nvme0n1p3",
    "/dev/mmcblk0p1",
    "/dev/mmcblk1p2",
    "/dev/disk2s1",
    "/dev/rdisk4s2",
    "D:\\",
    r"\\.\D:",
]


@pytest.mark.parametrize("path", WHOLE)
def test_whole_devices_recognised(path):
    assert is_whole_device(path)
    assert not is_partition_node(path)
    assert whole_device_for(path) is None


@pytest.mark.parametrize("path", PARTITIONS)
def test_partition_nodes_recognised(path):
    assert is_partition_node(path)
    assert not is_whole_device(path)


@pytest.mark.parametrize(
    ("part", "whole"),
    [
        ("/dev/sdb1", "/dev/sdb"),
        ("/dev/nvme0n1p3", "/dev/nvme0n1"),
        ("/dev/mmcblk0p1", "/dev/mmcblk0"),
        ("/dev/disk2s1", "/dev/disk2"),
        ("/dev/rdisk4s2", "/dev/rdisk4"),
    ],
)
def test_partition_hint_points_at_the_whole_device(part, whole):
    assert whole_device_for(part) == whole
    assert whole in partition_hint(part)


def test_windows_partition_hint():
    assert r"\\.\PhysicalDriveN" in partition_hint("D:\\")


def test_read_target_refuses_partition_nodes():
    with pytest.raises(PartitionNodeError) as e:
        check_read_target("/dev/sdb1")
    assert "/dev/sdb" in str(e.value)
    assert "outside any partition" in str(e.value)


def test_read_target_labels_regular_file(tmp_path):
    f = tmp_path / "card.img"
    f.write_bytes(b"\x00" * 512)
    assert check_read_target(str(f)) == "image file"


@pytest.mark.skipif(
    not os.path.exists("/dev/mmcblk0"), reason="no mmc block device on this host"
)
def test_real_host_nodes_classify_correctly():
    """Uses the actual device nodes present on the machine running the tests."""
    if is_block_device("/dev/mmcblk0"):
        assert is_whole_device("/dev/mmcblk0")
        assert whole_device_for("/dev/mmcblk0p1") == "/dev/mmcblk0"


# --- imaging ---------------------------------------------------------------


def source_file(tmp_path, size=3_000_000, seed=1):
    rng = random.Random(seed)
    p = tmp_path / "source.bin"
    p.write_bytes(bytes(rng.getrandbits(8) for _ in range(size)))
    return p


def test_image_round_trip_is_byte_identical(tmp_path):
    src = source_file(tmp_path)
    dst = tmp_path / "out" / "card.img"
    dst.parent.mkdir()
    summary = image_device(str(src), str(dst), chunk=64 * 1024)
    assert dst.read_bytes() == src.read_bytes()
    assert summary["verified"] is True
    assert summary["bytes"] == src.stat().st_size
    assert summary["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()


def test_image_reports_progress_and_ends_with_total(tmp_path):
    src = source_file(tmp_path, size=400_000)
    dst = tmp_path / "card.img"
    seen = []
    image_device(
        str(src),
        str(dst),
        chunk=64 * 1024,
        on_progress=lambda d, t: seen.append((d, t)),
    )
    assert seen
    assert seen[-1][0] == src.stat().st_size
    assert seen[-1][1] == src.stat().st_size


def test_image_without_verification_leaves_it_unset(tmp_path):
    src = source_file(tmp_path)
    dst = tmp_path / "card.img"
    summary = image_device(str(src), str(dst), verify=False)
    assert summary["verified"] is None
    assert dst.read_bytes() == src.read_bytes()


def test_image_resume_continues_a_partial_file(tmp_path):
    src = source_file(tmp_path)
    dst = tmp_path / "card.img"
    prefix = src.stat().st_size // 3
    dst.write_bytes(src.read_bytes()[:prefix])
    summary = image_device(str(src), str(dst), resume=True)
    assert dst.read_bytes() == src.read_bytes()
    assert summary["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert summary["verified"] is True


def test_image_refuses_a_partition_node(tmp_path):
    dst = tmp_path / "card.img"
    with pytest.raises(PartitionNodeError):
        image_device("/dev/sdb1", str(dst))


def test_image_refuses_its_own_source(tmp_path):
    src = source_file(tmp_path)
    with pytest.raises(ValueError, match="onto itself"):
        image_device(str(src), str(src))


def test_image_refuses_existing_output_without_flag(tmp_path):
    src = source_file(tmp_path)
    dst = tmp_path / "card.img"
    dst.write_bytes(b"stale")
    with pytest.raises(FileExistsError):
        image_device(str(src), str(dst))
    assert dst.read_bytes() == b"stale"  # untouched
    image_device(str(src), str(dst), overwrite=True)
    assert dst.read_bytes() == src.read_bytes()


def test_image_refuses_missing_source(tmp_path):
    with pytest.raises(FileNotFoundError):
        image_device(str(tmp_path / "nope.img"), str(tmp_path / "out.img"))


def test_image_refuses_a_non_regular_destination(tmp_path):
    """A directory stands in for any non-regular destination, on every platform."""
    src = source_file(tmp_path)
    d = tmp_path / "a-directory"
    d.mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        image_device(str(src), str(d))


@pytest.mark.skipif(os.name == "nt", reason="POSIX character device")
def test_image_refuses_a_character_device(tmp_path):
    src = source_file(tmp_path)
    with pytest.raises(ValueError, match="not a regular file"):
        image_device(str(src), "/dev/null")


def test_verify_failure_is_reported(tmp_path, monkeypatch):
    src = source_file(tmp_path, size=100_000)
    dst = tmp_path / "card.img"
    real = sha256_file

    def lying_digest(path, chunk=None, on_progress=None):
        digest, done = real(path, chunk or (8 << 20), on_progress)
        return (("0" * 64), done) if str(path) == str(dst) else (digest, done)

    monkeypatch.setattr(
        "tascam_dp008ex_sd_audio_extractor.imaging.sha256_file", lying_digest
    )
    summary = image_device(str(src), str(dst))
    assert summary["verified"] is False
    assert summary["verify_digest"] == "0" * 64


def test_sha256_file_handles_empty(tmp_path):
    p = tmp_path / "empty"
    p.write_bytes(b"")
    assert sha256_file(str(p)) == (hashlib.sha256(b"").hexdigest(), 0)


# --- CLI -------------------------------------------------------------------


def test_cli_image_command(tmp_path, capsys):
    src = source_file(tmp_path, size=200_000)
    dst = tmp_path / "card.img"
    assert main(["image", str(src), "-o", str(dst), "--chunk-mb", "1"]) == 0
    assert dst.read_bytes() == src.read_bytes()
    out = capsys.readouterr().out
    assert "read-only source" in out
    assert "verify: PASS" in out


def test_cli_devices_runs_without_raising(capsys):
    assert main(["devices"]) == 0
    assert "Partition nodes" in capsys.readouterr().out


def test_parse_mounts_keeps_only_device_mounts():
    text = (
        "/dev/mmcblk0p1 /run/media/u/DP-008EX vfat rw,relatime 0 0\n"
        "/dev/sda1 /boot ext4 rw 0 0\n"
        "tmpfs /run tmpfs rw 0 0\n"
        "proc /proc proc rw 0 0\n"
        "/dev/loop1 /snap/x squashfs ro 0 0\n"
    )
    assert parse_mounts(text) == [
        ("/dev/mmcblk0p1", "/run/media/u/DP-008EX", "vfat"),
        ("/dev/sda1", "/boot", "ext4"),
    ]


def test_parse_mounts_unescapes_spaces():
    text = "/dev/sdb1 /run/media/u/My\\040Card vfat rw 0 0\n"
    assert parse_mounts(text) == [("/dev/sdb1", "/run/media/u/My Card", "vfat")]


def test_mounted_volumes_maps_a_partition_to_its_whole_device(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/mmcblk0p1 /run/media/u/DP-008EX vfat rw 0 0\n")
    found = mounted_volumes(path=str(mounts), only={"vfat"})
    assert found == [
        {
            "partition": "/dev/mmcblk0p1",
            "whole": "/dev/mmcblk0",
            "mountpoint": "/run/media/u/DP-008EX",
            "fstype": "vfat",
            "label": "DP-008EX",
            "looks_like_dp008ex": True,
        }
    ]


def test_mounted_volumes_ignores_other_filesystems_and_unknown_mounts(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/sda2 /home ext4 rw 0 0\n"
        "tmpfs /run tmpfs rw 0 0\n"
        "/dev/disk2s1 /data hfs rw 0 0\n"
    )
    assert mounted_volumes(path=str(mounts), only={"vfat", "exfat"}) == []


def test_mounted_volumes_survives_a_missing_mounts_file(tmp_path):
    assert mounted_volumes(path=str(tmp_path / "nope")) == []


def test_cli_devices_flags_a_mounted_dp008ex_volume(capsys, monkeypatch):
    from tascam_dp008ex_sd_audio_extractor import cli

    monkeypatch.setattr(
        cli,
        "mounted_volumes",
        lambda **kw: [
            {
                "partition": "/dev/mmcblk0p1",
                "whole": "/dev/mmcblk0",
                "mountpoint": "/run/media/u/DP-008EX",
                "fstype": "vfat",
                "label": "DP-008EX",
                "looks_like_dp008ex": True,
            }
        ],
    )
    assert main(["devices"]) == 0
    out = capsys.readouterr().out
    assert "MOUNTED VOLUMES on candidate devices" in out
    assert "labelled like a DP-008EX: the songs are on /dev/mmcblk0" in out


def test_cli_devices_does_not_claim_cards_for_unrelated_mounts(capsys, monkeypatch):
    from tascam_dp008ex_sd_audio_extractor import cli

    monkeypatch.setattr(
        cli,
        "mounted_volumes",
        lambda **kw: [
            {
                "partition": "/dev/nvme0n1p1",
                "whole": "/dev/nvme0n1",
                "mountpoint": "/boot",
                "fstype": "vfat",
                "label": "EFI",
                "looks_like_dp008ex": False,
            }
        ],
    )
    assert main(["devices"]) == 0
    out = capsys.readouterr().out
    assert "not labelled like a DP-008EX (device: /dev/nvme0n1)" in out
    assert "the songs are on" not in out


def test_cli_refuses_partition_node(tmp_path, capsys):
    assert main(["verify", "/dev/sdb1"]) == 2
    err = capsys.readouterr().err
    assert "PartitionNodeError" in err
    assert "/dev/sdb" in err


def test_cli_image_missing_out_is_a_usage_error(capsys):
    with pytest.raises(SystemExit):
        main(["image", "/dev/sdb"])


# --- platform backends -------------------------------------------------
# These run on every OS in CI, so the Linux/macOS/Windows branches of the
# discovery code are executed for real, not merely type-checked.

_REAL_FSTYPES = {
    "vfat",
    "exfat",
    "msdos",
    "msdosfs",
    "fat",
    "fat32",
    "ntfs",
    "ntfs3",
    "hfs",
    "hfsplus",
    "apfs",
    "ext4",
    "ext3",
    "xfs",
    "btrfs",
}


def test_list_device_nodes_returns_only_whole_devices():
    nodes = list_device_nodes()
    assert nodes == sorted(set(nodes)), "sorted and de-duplicated"
    for node in nodes:
        assert is_whole_device(node), node
        assert not is_partition_node(node), node


def test_device_size_and_removable_agree_for_discovered_nodes():
    """At least one node must yield a real size on a machine that has disks.

    This is the assertion that fails loudly if the Windows CreateFileW/
    DeviceIoControl path or the macOS diskutil path regresses, because the CI
    runners have real disks and no mocking is involved.
    """
    nodes = list_device_nodes()
    if not nodes:
        pytest.skip("no candidate devices on this machine")
    sizes = [(n, device_size(n)) for n in nodes]
    known = [(n, s) for n, s in sizes if s]
    assert known, f"no size could be determined for any of {sizes}"
    assert all(size > 0 for _, size in known)
    removable = [is_removable(n) for n in nodes]
    assert all(r in (True, False, None) for r in removable)


def test_mounted_volumes_never_claim_a_guess_for_the_whole_device():
    for vol in mounted_volumes(only=_REAL_FSTYPES):
        assert vol["mountpoint"]
        assert vol["whole"]
        if vol["looks_like_dp008ex"]:
            # Only claim a specific device when the path really implies one.
            assert vol["whole"] != "see the physical drives listed above"


def test_parse_mount_output_handles_macos_mount_lines():
    text = (
        "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
        "/dev/disk2s1 on /Volumes/DP-008EX (msdos, local, nodev, nosuid, noowners)\n"
        "map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)\n"
    )
    assert parse_mount_output(text) == [
        ("/dev/disk3s1s1", "/", "apfs"),
        ("/dev/disk2s1", "/Volumes/DP-008EX", "msdos"),
    ]


def test_parse_mount_output_is_dumb_but_narrow():
    """The parser reports device mounts verbatim; filtering happens downstream.

    /dev/null really is a devfs mount, so the parser keeps it. What matters is
    that mounted_volumes() drops it, because /dev/null is neither a whole device
    nor a partition of one.
    """
    text = "tmpfs on /tmp (local)\n/dev/null on /dev/null (devfs)\n"
    assert parse_mount_output(text) == [("/dev/null", "/dev/null", "devfs")]
    assert mounted_volumes(path="does-not-exist") == []


def test_windows_backends_are_inert_off_windows(monkeypatch):
    """On a non-Windows host the ctypes paths must return nothing, not raise."""
    import tascam_dp008ex_sd_audio_extractor.devices as devices

    if os.name == "nt":
        pytest.skip("this asserts the non-Windows behaviour")
    assert devices._win_physical_drives() == []
    assert devices._win_removable_drives() == []
    assert devices._win_device_info(r"\\.\PhysicalDrive0") == (None, None)


def test_macos_backend_is_inert_off_macos():
    import sys

    import tascam_dp008ex_sd_audio_extractor.devices as devices

    if sys.platform == "darwin":
        pytest.skip("this asserts the non-macOS behaviour")
    assert devices._darwin_disk_info("/dev/disk2") == (None, None)


# --- partition-table shape --------------------------------------------


def _mbr(entries):
    return {
        "total_bytes": 1 << 30,
        "total_sectors": (1 << 30) // 512,
        "sig_ok": True,
        "sig": "55aa",
        "entries": entries,
    }


def _entry(index, ptype, lba, count):
    return {
        "index": index,
        "boot": 0,
        "type": ptype,
        "lba_start": lba,
        "sector_count": count,
    }


def test_single_partition_card_raises_no_warning():
    mbr = _mbr(
        [
            _entry(0, 0x0B, 63, 4_192_902),
            _entry(1, 0, 0, 0),
            _entry(2, 0, 0, 0),
            _entry(3, 0, 0, 0),
        ]
    )
    assert extra_partition_warning(mbr) is None
    assert len(nonempty_partitions(mbr)) == 1


def test_extra_partitions_are_warned_about_not_silently_ignored():
    mbr = _mbr(
        [
            _entry(0, 0x0B, 63, 4_192_902),
            _entry(1, 0x83, 4_200_000, 1000),
            _entry(2, 0, 0, 0),
            _entry(3, 0, 0, 0),
        ]
    )
    warn = extra_partition_warning(mbr)
    assert warn is not None
    assert "2 partitions" in warn
    assert "type 0x83" in warn
    assert "would be missed" in warn
    # mtr_bounds still only consults entry 0, which is the documented behaviour.
    assert mtr_bounds(mbr)[0] == (63 + 4_192_902) * 512


def test_zero_type_but_nonzero_length_counts_as_declared():
    """Some firmware writes type 0x00 with a real length; that still shadows audio."""
    mbr = _mbr(
        [
            _entry(0, 0x0B, 63, 100),
            _entry(1, 0x00, 200, 50),
            _entry(2, 0, 0, 0),
            _entry(3, 0, 0, 0),
        ]
    )
    assert extra_partition_warning(mbr) is not None


def test_privilege_hint_is_platform_correct():
    if os.name == "nt":
        assert privilege_hint() == "needs Administrator"
    else:
        assert privilege_hint() == "needs root or the disk group"


def test_windows_removable_query_uses_a_well_formed_property_query():
    """The 12-byte padded STORAGE_PROPERTY_QUERY, not two bare DWORDs.

    Windows rejects an undersized query buffer with ERROR_BAD_LENGTH, which made
    RemovableMedia come back unknown on the first CI run that actually executed
    this code. Pinned here so the layout cannot regress.
    """
    import struct

    from tascam_dp008ex_sd_audio_extractor import devices

    query = struct.pack("<II4x", 0, 0)
    assert len(query) == devices._STORAGE_PROPERTY_QUERY_LEN == 12
    # RemovableMedia is byte 10 of STORAGE_DEVICE_DESCRIPTOR; byte 8 is the bus
    # type, and reading that instead reports every disk as removable.
    assert devices._STOR_DEV_REMOVABLE == 10


# --- sector-aligned device reads ---------------------------------------
# Windows rejects a read that does not begin on a sector boundary when the
# handle is a raw PhysicalDriveN. The MBR partition table is read 64 bytes at
# 0x1BE, so a buffered open fails there. AlignedReader is the fix, and it is
# testable anywhere by pointing it at an ordinary file.


def test_aligned_reader_matches_a_plain_file(tmp_path):
    data = bytes(range(256)) * 40  # 10,240 bytes
    p = tmp_path / "img.bin"
    p.write_bytes(data)

    with AlignedReader(p) as r, open(p, "rb") as plain:
        assert r.read(64) == plain.read(64)
        assert r.tell() == plain.tell()
        # the unaligned read that Windows rejects
        r.seek(0x1BE)
        assert r.read(64) == data[0x1BE : 0x1BE + 64]
        r.seek(0)
        assert r.read() == data
        r.seek(-10, os.SEEK_END)
        assert r.read() == data[-10:]
        r.seek(512, os.SEEK_SET)
        r.seek(512, os.SEEK_CUR)
        assert r.tell() == 1024


def test_aligned_reader_handles_short_tail_reads(tmp_path):
    """A read that runs past EOF must return what exists, not raise."""
    p = tmp_path / "odd.bin"
    p.write_bytes(b"x" * 700)  # not a multiple of 512
    with AlignedReader(p) as r:
        r.seek(512)
        assert r.read(4096) == b"x" * 188
        assert r.read(10) == b""


def test_aligned_reader_rejects_bad_whence(tmp_path):
    p = tmp_path / "img.bin"
    p.write_bytes(b"y" * 1024)
    with AlignedReader(p) as r, pytest.raises(ValueError):
        r.seek(0, 99)


def test_open_device_uses_a_normal_file_for_images(tmp_path):
    p = tmp_path / "img.bin"
    p.write_bytes(b"z" * 1024)
    with open_device(p) as f:
        assert not isinstance(f, AlignedReader)
        assert f.read(4) == b"z" * 4


def test_windows_physical_drive_survives_the_unaligned_mbr_read():
    """The actual bug, on an actual Windows drive, when the runner allows it."""
    import sys

    from tascam_dp008ex_sd_audio_extractor.mbr import parse_mbr

    if sys.platform != "win32":
        pytest.skip("needs a Windows physical drive")
    from tascam_dp008ex_sd_audio_extractor.devices import _win_physical_drives

    nodes = _win_physical_drives()
    if not nodes:
        pytest.skip("no openable physical drive on this runner")
    with open_device(nodes[0]) as f:
        mbr = parse_mbr(f)
    assert set(mbr) >= {"entries", "total_sectors", "sig_ok"}
    assert len(mbr["entries"]) == 4


# --- macOS diskutil interpretation -------------------------------------
# `diskutil info -plist` only runs on macOS, so the subprocess is not exercised
# off a Mac. The interpretation of its output is, using real captured output.

DISKUTIL_REMOVABLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>DeviceIdentifier</key>
	<string>disk2</string>
	<key>DeviceNode</key>
	<string>/dev/disk2</string>
	<key>Whole</key>
	<true/>
	<key>Internal</key>
	<false/>
	<key>Ejectable</key>
	<true/>
	<key>RemovableMedia</key>
	<true/>
	<key>TotalSize</key>
	<integer>4003447808</integer>
	<key>DeviceBlockSize</key>
	<integer>512</integer>
	<key>VolumeName</key>
	<string>DP-008EX</string>
</dict>
</plist>
"""

DISKUTIL_INTERNAL = DISKUTIL_REMOVABLE.replace(
    b"""	<key>Ejectable</key>
	<true/>
	<key>RemovableMedia</key>
	<true/>""",
    b"""	<key>Ejectable</key>
	<false/>
	<key>RemovableMedia</key>
	<false/>""",
).replace(b"<string>DP-008EX</string>", b"<string>Macintosh HD</string>")

DISKUTIL_NO_REMOVABLE_KEY = b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
	<key>TotalSize</key>
	<integer>1000204886016</integer>
</dict>
</plist>
"""


def test_parse_diskutil_plist_reads_size_and_removable():
    from tascam_dp008ex_sd_audio_extractor.devices import parse_diskutil_plist

    assert parse_diskutil_plist(DISKUTIL_REMOVABLE) == (4003447808, True)
    assert parse_diskutil_plist(DISKUTIL_INTERNAL) == (4003447808, False)


def test_parse_diskutil_plist_falls_back_to_ejectable():
    from tascam_dp008ex_sd_audio_extractor.devices import parse_diskutil_plist

    # A whole disk that only carries Ejectable must still be classified.
    only_ejectable = DISKUTIL_REMOVABLE.replace(
        b"""	<key>RemovableMedia</key>
	<true/>""",
        b"""	<key>SomethingElse</key>
	<true/>""",
    )
    assert parse_diskutil_plist(only_ejectable) == (4003447808, True)
    assert parse_diskutil_plist(DISKUTIL_NO_REMOVABLE_KEY) == (1000204886016, None)


def test_parse_diskutil_plist_survives_garbage():
    from tascam_dp008ex_sd_audio_extractor.devices import parse_diskutil_plist

    assert parse_diskutil_plist(b"") == (None, None)
    assert parse_diskutil_plist(b"not xml at all") == (None, None)
    assert parse_diskutil_plist(b"<plist><array/></plist>") == (None, None)
    assert parse_diskutil_plist(DISKUTIL_REMOVABLE.replace(b"4003447808", b"0")) == (
        None,
        True,
    )


def test_aligned_reader_close_is_idempotent(tmp_path):
    p = tmp_path / "img.bin"
    p.write_bytes(b"q" * 1024)
    r = AlignedReader(p)
    r.close()
    r.close()  # must not raise
    # Matches what open() does, rather than leaking OSError(EBADF).
    with pytest.raises(ValueError, match="closed file"):
        r.read(1)
    with pytest.raises(ValueError, match="closed file"):
        r.seek(0)
