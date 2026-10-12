"""WAV output and PCM statistics for rendered audio (single source)."""

import math
import os
import struct
import wave

from .report import Kind, Skip

RATE = 44100


def write_wav(path, pcm, rate=RATE, nchannels=1):
    """Write s16le WAV; return skips (W1 when an odd byte is dropped).

    WAV frames are whole 16-bit samples, so a trailing odd byte cannot be
    stored: it is dropped AND reported, never silent.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    skips = []
    if len(pcm) % 2:
        skips.append(
            Skip(
                site="W1",
                kind=Kind.ODD_BYTE,
                detail=(
                    f"dropped trailing byte of {len(pcm)}-byte pcm "
                    "(WAV needs whole samples)"
                ),
            )
        )
        pcm = pcm[:-1]
    with wave.open(path, "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return tuple(skips)


def pcm_stats(buf):
    """(rms, peak) of a mono s16le buffer. iter_unpack avoids a huge int tuple.

    The even-length mask is display-sampling (callers pass bounded slices for
    RMS display only, never audio data): struct.iter_unpack raises on odd
    input, so the mask is load-bearing, not redundant. Documented, not silent.
    """
    total = 0
    peak = 0
    n = 0
    for (v,) in struct.iter_unpack("<h", buf[: len(buf) & ~1]):
        total += v * v
        a = -v if v < 0 else v
        if a > peak:
            peak = a
        n += 1
    if not n:
        return 0.0, 0
    return math.sqrt(total / n), peak
