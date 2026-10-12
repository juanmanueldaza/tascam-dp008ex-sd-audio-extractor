"""Benchmark: wall time + peak RSS + stdlib read/seek counts per command (B0).

Methodology (see BENCHMARK.md): read/seek counts are a STDLIB counting
proxy — tallies on the buffered file object open_device returns, NOT
literal syscalls (spec ambiguity B0-4). RSS is the getrusage peak of the
process that ran the command: on POSIX each command runs in a forked
child (parent stays lean), so RSS is per-command; without fork (Windows)
it falls back to in-process and RSS is cumulative — see BENCHMARK.md.
Carve runs with --limit 200: a full 4GiB sweep at Python speed is not a
useful baseline. The image is bench/gen_image.py output (sparse multi-GB).

Usage: python bench/bench.py [--image PATH] [--rebuild] [--out-dir DIR]
"""

import argparse
import json
import os
import resource
import sys
import tempfile
import time
from pathlib import Path

import gen_image

from tascam_dp008ex_sd_audio_extractor import cli as cli_mod

_current = {}


class CountingReader:
    """Read/seek tally on top of the real buffered file object."""

    def __init__(self, fb):
        self._fb = fb
        self.reads = 0
        self.read_bytes = 0
        self.seeks = 0

    def read(self, n=-1):
        self.reads += 1
        d = self._fb.read(n)
        self.read_bytes += len(d)
        return d

    def seek(self, *a):
        self.seeks += 1
        return self._fb.seek(*a)

    def __enter__(self):
        self._fb.__enter__()
        return self

    def __exit__(self, *a):
        return self._fb.__exit__(*a)

    def __getattr__(self, name):
        return getattr(self._fb, name)


_real_open = cli_mod.open_device


def _counting_open(path, mode="rb"):
    ctr = CountingReader(_real_open(path, mode))
    _current["ctr"] = ctr
    return ctr


def _rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def measure(cmd, img, out_dir):
    full = [c.replace("{img}", img).replace("{out}", out_dir) for c in cmd]
    _current.clear()
    cli_mod.open_device = _counting_open
    try:
        t0 = time.monotonic()
        rc = cli_mod.main(full)
        wall = time.monotonic() - t0
    finally:
        cli_mod.open_device = _real_open
    ctr = _current.get("ctr")
    return {
        "cmd": " ".join(full),
        "exit": rc,
        "wall_s": round(wall, 2),
        "rss_peak_mb": round(_rss_mb(), 1),
        "reads": ctr.reads if ctr else -1,
        "read_mb": round(ctr.read_bytes / 2**20, 1) if ctr else -1,
        "seeks": ctr.seeks if ctr else -1,
    }


def measure_isolated(cmd, img, out_dir, log_path):
    """Run one command in a forked child (POSIX): per-command peak RSS.

    Falls back to in-process measurement where fork is unavailable; then
    rss_peak_mb is cumulative and BENCHMARK.md says so.
    """
    if not hasattr(os, "fork"):
        row = measure(cmd, img, out_dir)
        row["rss_note"] = "cumulative (no fork)"
        return row
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        try:
            os.close(r)
            with open(log_path, "w") as log:
                os.dup2(log.fileno(), 1)
                os.dup2(log.fileno(), 2)
                row = measure(cmd, img, out_dir)
            with os.fdopen(w, "w") as pf:
                pf.write(json.dumps(row))
        finally:
            os._exit(0)
    os.close(w)
    with os.fdopen(r) as pf:
        row = json.loads(pf.read())
    os.waitpid(pid, 0)
    return row


COMMANDS = [
    ["list", "{img}"],
    ["extract-all", "{img}", "--out-dir", "{out}/m"],
    ["stems", "{img}", "--out-dir", "{out}/s"],
    ["carve", "{img}", "--stride", "16", "--limit", "200"],
    ["verify", "{img}"],
]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bench")
    ap.add_argument("--image", default="/tmp/dp008ex-bench.img")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--out-dir", default="")
    a = ap.parse_args(argv)
    img = a.image
    if a.rebuild or not Path(img).exists():
        print("generating benchmark image (streaming, bounded RAM)...", flush=True)
        _, info = gen_image.gen_image(img)
        print("image: " + ", ".join(f"{k}={v}" for k, v in info.items()), flush=True)
    out_dir = a.out_dir or tempfile.mkdtemp(prefix="dp008ex-bench-")
    logs = Path(out_dir) / "logs"
    logs.mkdir(exist_ok=True)
    rows = []
    for c in COMMANDS:
        slug = c[0].replace("-", "_")
        rows.append(measure_isolated(c, img, out_dir, str(logs / f"{slug}.log")))
    print(
        f"{'command':28} {'exit':>4} {'wall_s':>7} {'rssMB':>8} {'reads':>7} {'readMB':>8} {'seeks':>6}"
    )
    for r in rows:
        print(
            f"{r['cmd']:28} {r['exit']:>4} {r['wall_s']:>7} {r['rss_peak_mb']:>8} "
            f"{r['reads']:>7} {r['read_mb']:>8} {r['seeks']:>6}"
        )
    print(f"per-command logs in {logs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
