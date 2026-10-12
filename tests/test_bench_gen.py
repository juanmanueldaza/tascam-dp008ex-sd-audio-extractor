"""Generator validity: bench/gen_image.py output must verify + render clean."""

import os
import subprocess
import sys
import wave
from pathlib import Path

from tascam_dp008ex_sd_audio_extractor.cli import main

BENCH_GEN = Path(__file__).resolve().parent.parent / "bench" / "gen_image.py"


def _gen(out, size_gb, clusters, seed):
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parent.parent / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH", "")]))
    r = subprocess.run(
        [
            sys.executable,
            str(BENCH_GEN),
            "--out",
            str(out),
            "--size-gb",
            str(size_gb),
            "--clusters",
            str(clusters),
            "--seed",
            str(seed),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, r.stderr


def test_gen_image_verify_and_render(tmp_path):
    img = tmp_path / "bench.img"
    _gen(img, size_gb=8 / 1024, clusters=4, seed=1)
    assert img.stat().st_size == 8 * 2**20
    assert main(["verify", str(img)]) == 0
    d = tmp_path / "m"
    assert main(["extract-all", str(img), "--out-dir", str(d)]) == 0
    wavs = list(d.glob("*.wav"))
    assert len(wavs) == 1
    with wave.open(str(wavs[0])) as w:
        assert w.getnchannels() == 2
    assert main(["stems", str(img), "--out-dir", str(tmp_path / "s")]) == 0
