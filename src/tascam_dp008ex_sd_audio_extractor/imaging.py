"""Read-only card imaging: copy a whole device to an image file, then verify it.

Deliberately read-only with respect to the source: the device is only ever
opened "rb". The guards exist because the two easy mistakes are destructive or
silently useless -- writing the image onto the card being read, or imaging a
partition node (which yields an empty-looking card because the songs live in the
undeclared MTR region).
"""

import hashlib
import os
import time

from .devices import (
    PartitionNodeError,
    device_size,
    is_block_device,
    is_partition_node,
    partition_hint,
)

CHUNK = 8 << 20


def sha256_file(path, chunk=CHUNK, on_progress=None):
    """(digest, bytes_read) for a file or device, streamed."""
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
            done += len(block)
            if on_progress:
                on_progress(done)
    return h.hexdigest(), done


def _same_target(src, dst):
    """True if src and dst resolve to the same underlying object."""
    try:
        s, d = os.stat(src), os.stat(dst)
    except OSError:
        return False
    # st_rdev only exists on POSIX; on Windows device identity is not exposed.
    s_rdev = getattr(s, "st_rdev", None)
    d_rdev = getattr(d, "st_rdev", None)
    if s_rdev and s_rdev == d_rdev:
        return True
    return s.st_dev == d.st_dev and s.st_ino == d.st_ino


def _check_source(src):
    if is_partition_node(src):
        raise PartitionNodeError(
            f"{src} is a partition, not a whole device. The DP-008EX stores the "
            f"multitrack audio outside any partition, so the image would look "
            f"empty. {partition_hint(src)}"
        )
    if not os.path.exists(src):
        raise FileNotFoundError(f"no such device or image: {src}")
    if not (is_block_device(src) or os.path.isfile(src)):
        raise ValueError(
            f"{src} is neither a block device nor a regular file; pass a whole "
            f"device (e.g. /dev/sdb) or a .img file"
        )


def image_device(
    src,
    dst,
    *,
    chunk=CHUNK,
    verify=True,
    resume=False,
    overwrite=False,
    on_progress=None,
):
    """Copy `src` (whole device) to `dst` (image file), then verify by re-reading.

    Returns a summary dict. Raises on any guard violation; the source is never
    opened for writing.
    """
    _check_source(src)
    if _same_target(src, dst):
        raise ValueError(f"refusing to image {src} onto itself ({dst})")
    if os.path.exists(dst) and not os.path.isfile(dst):
        # Covers block devices, char devices, fifos and directories: the image
        # must land in a regular file.
        raise ValueError(
            f"refusing to write the image to {dst}: not a regular file "
            f"(device node or directory?)"
        )
    parent = os.path.dirname(os.path.abspath(dst))
    if not os.path.isdir(parent):
        raise FileNotFoundError(f"output directory does not exist: {parent}")
    if is_block_device(src):
        # Writing the image onto the card we are reading would corrupt it.
        try:
            if os.stat(parent).st_dev == os.stat(src).st_dev:
                raise ValueError(
                    f"refusing to write {dst} onto the same device as {src}; "
                    f"that would overwrite the card you are trying to recover"
                )
        except OSError:
            pass
    if os.path.exists(dst):
        if resume:
            pass
        elif overwrite:
            os.remove(dst)
        else:
            raise FileExistsError(
                f"{dst} already exists; pass --overwrite to replace it or "
                f"--resume to continue an interrupted image"
            )

    total = device_size(src)
    start = 0
    resuming = resume and os.path.exists(dst)
    if resuming:
        start = os.path.getsize(dst)
        if total and start > total:
            raise ValueError(
                f"{dst} is larger ({start} bytes) than the source ({total} bytes); "
                f"cannot resume"
            )

    started = time.monotonic()
    written = start
    digest = hashlib.sha256()
    with open(src, "rb") as r, open(dst, "r+b" if resuming else "wb") as w:
        if resuming:
            # Hash the existing prefix so the reported digest covers the whole file.
            with open(dst, "rb") as pre:
                left = start
                while left > 0:
                    block = pre.read(min(chunk, left))
                    if not block:
                        break
                    digest.update(block)
                    left -= len(block)
            r.seek(start)
            w.seek(start)
        elif total:
            # Preallocate: sparse-capable filesystems then write sparsely, and a
            # truncated image becomes obvious.
            try:
                w.truncate(total)
            except OSError:
                pass
        while True:
            block = r.read(chunk)
            if not block:
                break
            w.write(block)
            digest.update(block)
            written += len(block)
            if on_progress:
                on_progress(written, total)
        w.flush()
        os.fsync(w.fileno())

    summary = {
        "source": str(src),
        "image": str(dst),
        "bytes": written,
        "size": total,
        "sha256": digest.hexdigest(),
        "seconds": time.monotonic() - started,
        "verified": None,
    }
    if verify:
        got, read_bytes = sha256_file(dst, chunk)
        summary["verified"] = got == summary["sha256"]
        summary["verify_bytes"] = read_bytes
        if not summary["verified"]:
            summary["verify_digest"] = got
    return summary
