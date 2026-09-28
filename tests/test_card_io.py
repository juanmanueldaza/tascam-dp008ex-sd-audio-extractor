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
    PartitionNodeError,
    check_read_target,
    is_block_device,
    is_partition_node,
    is_whole_device,
    partition_hint,
    whole_device_for,
)
from tascam_dp008ex_sd_audio_extractor.imaging import image_device, sha256_file

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


def test_cli_refuses_partition_node(tmp_path, capsys):
    assert main(["verify", "/dev/sdb1"]) == 2
    err = capsys.readouterr().err
    assert "PartitionNodeError" in err
    assert "/dev/sdb" in err


def test_cli_image_missing_out_is_a_usage_error(capsys):
    with pytest.raises(SystemExit):
        main(["image", "/dev/sdb"])
