"""CLI: tascam-dp008ex-sd-audio-extractor list|carve|extract-all|stems|verify <image>."""

import argparse
import collections
import hashlib
import os
import struct
import sys

from .carve import CLUSTER, POOL_OFF, sweep
from .extract import (
    HALF,
    SECTOR_BYTES,
    collect_chains,
    extent_pairs,
    read_alloc,
    render_chain,
)
from .header import parse_header
from .mbr import mtr_bounds, parse_mbr
from .scan import find_bfs_roots, scan_alloc_table, scan_song_slots
from .stems import FRAG, FRAG_SAMPLES, RAW_SECTOR, STEM_FLAGS, render_stem
from .wavio import RATE, pcm_stats, write_wav

# song assignment by timeline top (samples): short<=0x9D0000, A<=0x6F00000, else B
SONG_SHORT_TOP = 0x9D0000
SONG_A_TOP = 0x6F00000


def song_name(top):
    if top <= SONG_SHORT_TOP:
        return "short"
    return "A" if top <= SONG_A_TOP else "B"


def _s16(buf, off):
    return struct.unpack_from("<h", buf, off)[0]


def cmd_list(img_path):
    with open(img_path, "rb") as f:
        mbr = parse_mbr(f)
        print(
            f"total: {mbr['total_bytes']} bytes ({mbr['total_bytes'] / 1e9:.3f} GB, {mbr['total_sectors']} sectors) sig_ok={mbr['sig_ok']}"
        )
        for e in mbr["entries"]:
            print(
                f"p{e['index']}: type={e['type']:#04x} lba={e['lba_start']} count={e['sector_count']} bytes={e['sector_count'] * 512}"
            )
        base, end = mtr_bounds(mbr)
        print(
            f"MTR_BASE={base} ({base:#x}) sector={base // 512} size={(end - base) / 1e9:.3f} GB"
        )
        hdr = parse_header(f, base)
        print(
            f"header magic={hdr['magic']:#010x} sector_size={hdr['sector_size']} reserved={hdr['reserved_sectors']}s/{hdr['reserved_bytes']}B"
        )
        print(
            f"self_ptr={hdr['self_ptr']:#010x} (hdr sector {hdr['hdr_sector']}) match={hdr['self_ptr'] == hdr['hdr_sector']}"
        )
        print(
            f"mtr_start_ptr={hdr['mtr_start_ptr']:#010x} (mtr sector {hdr['mtr_sector']}) match={hdr['mtr_start_ptr'] == hdr['mtr_sector']}"
        )
        print(
            f"bitmap_ptr={hdr['bitmap_ptr']:#010x} expect_mtr+10={hdr['mtr_sector'] + 10:#010x} match={hdr['bitmap_ptr'] == hdr['mtr_sector'] + 10}"
        )
        print(
            f"bfs_root_ptr_cand(+0x68)={hdr['bfs_root_ptr_cand']:#010x} backup_root_cand(+0x74)={hdr['backup_root_cand']:#010x}"
        )
        f.seek(hdr["bfs_root_ptr_cand"] * 512)
        probe = f.read(128)
        print(
            f"bfs_ptr_probe @sector {hdr['bfs_root_ptr_cand']}: {probe[0:32].hex()}... BFS ROOT in sector: {b'BFS ROOT' in f.read(512 - 128) or b'BFS ROOT' in probe}"
        )
        roots = find_bfs_roots(f, base)
        print(f"BFS ROOT hits: {len(roots)}")
        for h in roots[:8]:
            print(
                f"  {h['abs_hex']} sector={h['abs_sector']} sec_off={h['sector_off']}"
            )
        slots = scan_song_slots(f, base)
        print(f"song slots found: {len(slots)}")
        for s in slots[:20]:
            print(f"  {s['abs_hex']} S{s['s_idx']}/{s['song']}")
        if len(slots) > 20:
            print(f"  ... +{len(slots) - 20} more")
        at = scan_alloc_table(f, base)
        print(
            f"alloc: {at['records']} records; masters={at['song_masters']} inodes={at['audio_inodes']} ext5={at['extent_first']} ext6={at['extent_second']}"
        )
        for fl, c in sorted(at["histogram"].items()):
            print(f"  flag {fl:#010x}: {c}")


def cmd_carve(img_path, stride=16, limit=0, dump_top=0, out_dir="carve_out"):
    with open(img_path, "rb") as f:
        mbr = parse_mbr(f)
        base, end = mtr_bounds(mbr)
        total_c = (end - (base + POOL_OFF)) // CLUSTER
        print(
            f"pool: off={base + POOL_OFF:#x} clusters={total_c} size={CLUSTER} stride={stride} limit={limit or 'all'}"
        )
        hist = collections.Counter()
        scored = []  # (silent_frac desc, -rms, idx, off, stats)
        n = 0
        for idx, off, st in sweep(f, base, end, stride=stride, limit=limit):
            hist[st["label"]] += 1
            n += 1
            if st["label"] in ("pcm-like", "pcm-possible"):
                scored.append((st["silent_frac"], -st["rms"], idx, off, st))
            if n % 2000 == 0:
                print(f"  ...{n} clusters ({dict(hist)})", flush=True)
        print(f"scanned={n} {dict(hist)}")
        scored.sort(reverse=True)
        print(f"candidates={len(scored)}")
        for _, _, idx, off, st in scored[:20]:
            print(
                f"  cluster={idx} {off:#x} rms={st['rms']} peak={st['peak']} silent={st['silent_frac']} zc={st['zc']} {st['label']}"
            )
        if dump_top and scored:
            for rank, (_, _, idx, off, st) in enumerate(scored[:dump_top]):
                f.seek(off)
                raw = f.read(CLUSTER)
                p = os.path.join(
                    out_dir, f"cand_{rank:02d}_c{idx}_{off:#x}_rms{st['rms']}.wav"
                )
                write_wav(p, raw)
                print(f"  wrote {p}")


def cmd_extract_all(img_path, out_dir="out"):
    with open(img_path, "rb") as f:
        mbr = parse_mbr(f)
        base, end = mtr_bounds(mbr)
        recs = read_alloc(f, base)
        chains = collect_chains(recs)
        print(f"chains={len(chains)}")
        for ci, ch in enumerate(chains):
            pairs = [p for r in ch for p in extent_pairs(r)]
            raw = render_chain(f, base, end, pairs)
            rms, peak = pcm_stats(raw[: 4 * 2**20])
            boffs = sorted(p[1] for p in pairs)
            dur = len(raw) / 2 / RATE
            name = os.path.join(
                out_dir, f"chain{ci}_{len(ch)}recs_{dur / 60:.1f}min_rms{rms:.0f}.wav"
            )
            write_wav(name, raw)
            print(
                f"chain{ci}: recs={len(ch)} pairs={len(pairs)} boff {boffs[0]:#x}..{boffs[-1]:#x} "
                f"{len(raw) / 2**20:.1f}MB {dur / 60:.1f}min rms={rms:.0f} peak={peak} -> {name}"
            )


def cmd_stems(img_path, out_dir="stems_out"):
    with open(img_path, "rb") as f:
        mbr = parse_mbr(f)
        base, end = mtr_bounds(mbr)
        recs = read_alloc(f, base, flags=STEM_FLAGS)
        chains = collect_chains(recs, STEM_FLAGS)
        print(f"stem chains={len(chains)}")
        for ci, ch in enumerate(sorted(chains, key=len, reverse=True)):
            raw, cov = render_stem(f, base, end, ch)
            rms, peak = pcm_stats(raw[: 2 * 2**20])
            frags = [p for r in ch for p in extent_pairs(r) if p[0]]
            song = song_name(max(b for _, b in frags))
            dur = len(raw) / 2 / RATE
            name = os.path.join(
                out_dir,
                f"stem_{song}_c{ci}_{len(ch)}recs_{dur / 60:.1f}min_cov{cov:.2f}_rms{rms:.0f}.wav",
            )
            write_wav(name, raw)
            print(
                f"stem {song} c{ci}: recs={len(ch)} frags={len(frags)} flag={ch[0]['flag']:#x} "
                f"{len(raw) / 2**20:.1f}MB {dur / 60:.1f}min cov={cov:.2f} rms={rms:.0f} peak={peak} -> {name}"
            )


def cmd_verify(img_path):
    """Cross-checks the render model against the image. Returns exit code."""
    fails = []

    def check(name, cond, detail=""):
        print(
            f"  [{'PASS' if cond else 'FAIL'}] {name}"
            + (f" {detail}" if detail else "")
        )
        if not cond:
            fails.append(name)

    with open(img_path, "rb") as f:
        mbr = parse_mbr(f)
        base, end = mtr_bounds(mbr)
        recs = read_alloc(f, base)
        chains = collect_chains(recs)
        schains = collect_chains(read_alloc(f, base, flags=STEM_FLAGS), STEM_FLAGS)
        schains.sort(key=len, reverse=True)
        if not chains and not schains:
            print("NO SONGS: no master or stem chains in image, nothing to verify")
            return 1

        sizes = set()
        for r in recs:
            if r["flag"] in (0x80030001, 0x80030000):
                w = struct.unpack(">16I", r["raw"])
                check(
                    f"size-field pair slot {r['slot']}", w[11] == w[12], f"{w[11]:#x}"
                )
                sizes.add(w[11])
        sizes = sorted(sizes)
        print(
            "size fields: " + ", ".join(f"{s:#x}={s / RATE / 60:.1f}min" for s in sizes)
        )
        check("size fields found", len(sizes) >= 1, f"n={len(sizes)}")

        # --- masters: boundary smoothness, dups, duration vs size field ---
        print(f"master chains={len(chains)}")
        durations = []
        for ci, ch in enumerate(chains):
            # sector 0 is never audio: the renderers skip it, so verify does too
            pairs = sorted(
                (p for r in ch for p in extent_pairs(r) if p[0]), key=lambda p: p[1]
            )
            blocks = collections.Counter()
            full_j, half_j, base_j = [], [], []
            for k, (cl, _) in enumerate(pairs):
                f.seek(base + cl * SECTOR_BYTES)
                data = f.read(SECTOR_BYTES)
                if len(data) < SECTOR_BYTES:
                    continue
                for b in range(0, len(data), 4096):
                    blocks[hashlib.md5(data[b : b + 4096]).hexdigest()] += 1
                for i in range(0, len(data) - 2, 2048):
                    base_j.append(abs(_s16(data, i + 2) - _s16(data, i)))
                if k + 1 < len(pairs):
                    f.seek(base + pairs[k + 1][0] * SECTOR_BYTES)
                    ndata = f.read(4)
                    if len(ndata) == 4:
                        full_j.append(
                            abs(_s16(ndata, 0) - _s16(data, SECTOR_BYTES - 2))
                        )
                        half_j.append(abs(_s16(ndata, 0) - _s16(data, HALF - 2)))
            bl = sum(base_j) / max(1, len(base_j))
            fj = sum(full_j) / max(1, len(full_j))
            hj = sum(half_j) / max(1, len(half_j))
            dups = sum(n - 1 for n in blocks.values() if n > 1)
            dur = len(pairs) * SECTOR_BYTES / (2 * RATE)
            durations.append(dur)
            if bl > 0:
                # boundary jump should look like the signal's own local step
                smooth = fj / bl < 1.5
                detail = f"ratio={fj / bl:.2f} (half={hj / bl:.2f}, baseline={bl:.1f}"
            else:
                # locally constant signal: no baseline to normalise against, so
                # fall back to an absolute continuity threshold
                smooth = fj < 50
                detail = f"abs_jump={fj:.1f} (half={hj:.1f}, baseline=0"
            check(
                f"master c{ci} full-boundary smooth",
                smooth,
                f"{detail}, pairs={len(pairs)}, {dur / 60:.1f}min, dups={dups})",
            )
            check(f"master c{ci} no dup blocks", dups == 0, f"dups={dups}")
        for d in sorted(set(durations)) if sizes else []:
            near = min(sizes, key=lambda s: abs(s / RATE - d))
            ok = abs(near / RATE - d) / d < 0.01
            check(
                f"master duration {d / 60:.1f}min matches size field",
                ok,
                f"field={near / RATE / 60:.1f}min",
            )

        # --- stems: duration vs size field, dup, coverage, partner adjacency ---
        print(f"stem chains={len(schains)}")
        all_frags = []
        for ci, ch in enumerate(schains):
            frags = [p for r in ch for p in extent_pairs(r) if p[0]]
            all_frags.append(frags)
            top = max(b for _, b in frags) + FRAG_SAMPLES
            dur = top / RATE
            if sizes:
                near = min(sizes, key=lambda s: abs(s / RATE - dur))
                ok = abs(near / RATE - dur) / dur < 0.01
                check(
                    f"stem c{ci} duration matches song",
                    ok,
                    f"{dur / 60:.1f}min vs {near / RATE / 60:.1f}min (pairs={len(frags)})",
                )
            blocks = collections.Counter()
            touched = set()
            for s, b in frags:
                f.seek(base + s * RAW_SECTOR)
                data = f.read(FRAG)
                if len(data) < FRAG:
                    continue
                blocks[hashlib.md5(data).hexdigest()] += 1
                touched.update(range((b * 2) // 4096, (b * 2 + FRAG) // 4096))
            dups = sum(n - 1 for n in blocks.values() if n > 1)
            check(
                f"stem c{ci} fragment dups bounded",
                dups <= max(2, len(frags) // 20),
                f"dups={dups}/{len(frags)} (intentional sector reuse allowed)",
            )
            cov = len(touched) / max(1, (top * 2 + 4095) // 4096)
            check(f"stem c{ci} sparse coverage sane", cov <= 0.25, f"cov={cov:.3f}")
        # packing adjacency: another fragment (any chain, any boff) starts exactly
        # +-0x8000 from this one. Proves extent length <= 0x8000 (overlaps are
        # impossible on disk) and that the allocator packs in 0x8000 units.
        owner = collections.Counter()
        for frags in all_frags:
            for s, _ in frags:
                if s:
                    owner[s] += 1
        adj = tot = 0
        for frags in all_frags:
            for s, _ in frags:
                if not s:
                    continue
                tot += 1
                adj += bool(owner.get(s + 0x40) or owner.get(s - 0x40))
        check(
            "stem packing adjacency at +-0x8000",
            bool(tot) and adj * 10 >= tot * 9,
            f"{adj}/{tot} = {adj / tot:.0%} (expect ~95%; proves length <= 0x8000)"
            if tot
            else "no fragments",
        )

    print(("VERIFY FAIL: " + ", ".join(fails)) if fails else "VERIFY PASS")
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tascam-dp008ex-sd-audio-extractor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("list", help="list MBR/MTR/BFS/songs/masters of an image")
    pl.add_argument("image")
    pc = sub.add_parser("carve", help="sweep pool clusters for PCM-structured audio")
    pc.add_argument("image")
    pc.add_argument("--stride", type=int, default=16)
    pc.add_argument("--limit", type=int, default=0)
    pc.add_argument("--dump-top", type=int, default=0)
    pc.add_argument("--out-dir", default="carve_out")
    px = sub.add_parser("extract-all", help="extract extent chains to mono WAVs")
    px.add_argument("image")
    px.add_argument("--out-dir", default="out")
    pt = sub.add_parser(
        "stems", help="render stem fragments to per-track WAV timelines"
    )
    pt.add_argument("image")
    pt.add_argument("--out-dir", default="stems_out")
    pv = sub.add_parser(
        "verify", help="cross-check render model against the image (no WAV output)"
    )
    pv.add_argument("image")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            cmd_list(a.image)
        elif a.cmd == "carve":
            cmd_carve(
                a.image,
                stride=a.stride,
                limit=a.limit,
                dump_top=a.dump_top,
                out_dir=a.out_dir,
            )
        elif a.cmd == "extract-all":
            cmd_extract_all(a.image, out_dir=a.out_dir)
        elif a.cmd == "stems":
            cmd_stems(a.image, out_dir=a.out_dir)
        elif a.cmd == "verify":
            return cmd_verify(a.image)
    except (OSError, ValueError, MemoryError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
