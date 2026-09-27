"""Pool carver: sweep BFS clusters, flag PCM-structured ones. Read-only on image."""

import math
import struct

CLUSTER = 0x18000  # 96 KiB, from MTR header reserved field
POOL_OFF = 0x390000  # audio pool start relative to MTR_BASE (DP-03 confirmed)


def cluster_stats(raw):
    """Stats for one 96KiB cluster. raw must be CLUSTER bytes."""
    n = len(raw) // 2
    if not any(raw):
        return {
            "label": "zeros",
            "rms": 0,
            "peak": 0,
            "silent_frac": 1.0,
            "zc": 0,
        }
    s = struct.unpack("<" + "h" * n, raw)
    rms = math.sqrt(sum(x * x for x in s) / n)
    peak = max(abs(x) for x in s)
    silent = sum(1 for x in s if abs(x) < 50) / n
    zc = sum(1 for a, b in zip(s, s[1:], strict=False) if (a < 0) != (b < 0))
    # Real DP-03 audio observed: rms low-hundreds, silent 0.8-0.9, peak few thousand,
    # smooth steps. Uninitialized pool here: rms ~19k, silent ~0.001, peak 32768.
    if rms < 30 and silent > 0.98:
        label = "near-silent"
    elif 50 <= rms <= 12000 and silent >= 0.30 and peak < 30000:
        label = "pcm-like"
    elif silent >= 0.30 and rms < 12000:
        label = "pcm-possible"
    else:
        label = "noise"
    return {
        "label": label,
        "rms": int(rms),
        "peak": peak,
        "silent_frac": round(silent, 3),
        "zc": zc,
    }


def sweep(f, mtr_base, mtr_end, stride=16, limit=0):
    """Yield (idx, abs_off, stats) for every stride-th cluster. limit caps count (0=all)."""
    pool_start = mtr_base + POOL_OFF
    total_clusters = (mtr_end - pool_start) // CLUSTER
    done = 0
    idx = 0
    while idx < total_clusters:
        off = pool_start + idx * CLUSTER
        f.seek(off)
        raw = f.read(CLUSTER)
        if len(raw) < CLUSTER:
            break
        st = cluster_stats(raw)
        yield idx, off, st
        done += 1
        if limit and done >= limit:
            break
        idx += stride
