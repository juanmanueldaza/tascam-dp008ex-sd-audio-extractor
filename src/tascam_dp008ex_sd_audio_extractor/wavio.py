"""WAV output and PCM statistics for rendered audio (single source)."""

import math
import os
import struct
import wave

RATE = 44100


def write_wav(path, pcm, rate=RATE):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if len(pcm) % 2:
        pcm = pcm[:-1]
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def pcm_stats(buf):
    """(rms, peak) of a mono s16le buffer. iter_unpack avoids a huge int tuple."""
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
