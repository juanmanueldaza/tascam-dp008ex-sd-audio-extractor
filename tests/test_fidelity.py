"""Sample-exact fidelity: known sine+impulse signals via extract-all and stems.

Ground-truth harness (A1). The builder paints with helpers' signal functions
and these tests assert with them, so any deviation is a renderer bug. The
full-buffer exact compare is the authority for position/amplitude/phase
(AC-2). The verify boundary-smoothness metric does not apply to impulse
trains and is intentionally not run here.
"""

import struct
import wave

import pytest
from helpers import (
    CLUSTER,
    FRAG,
    MASTER_SAMPLES,
    STEM_BOFFS,
    STEM_SAMPLES,
    build_image,
    image_bytes,
    impulse_offsets,
    master_sine_impulse_pcm,
    stem_fragment_payload,
)

from tascam_dp008ex_sd_audio_extractor.cli import main
from tascam_dp008ex_sd_audio_extractor.stems import FRAG_SAMPLES

SEED = 0
HALF = CLUSTER // 2
IMPULSE = 16000


@pytest.fixture
def si_card(tmp_path):
    return build_image(tmp_path / "si.img", signal="sine_impulse", seed=SEED)


def read_data(p):
    with wave.open(str(p)) as w:
        return w.getnframes(), w.getnchannels(), w.readframes(w.getnframes())


def test_seed_changes_payload():
    assert image_bytes(signal="sine_impulse", seed=0) != image_bytes(
        signal="sine_impulse", seed=7
    )
    assert image_bytes() == image_bytes(signal="ramp")


def test_extract_all_sample_exact(si_card, tmp_path):
    d = tmp_path / "m"
    assert main(["extract-all", str(si_card), "--out-dir", str(d)]) == 0
    wavs = list(d.glob("*.wav"))
    assert len(wavs) == 1
    nframes, nchannels, data = read_data(wavs[0])
    assert nchannels == 2  # shared fixture carries a stereo pair (A4)
    assert nframes == MASTER_SAMPLES
    # stereo frames deinterleave to the two mono renders byte-for-byte
    samples = struct.unpack("<" + "h" * (len(data) // 2), data)
    expected = master_sine_impulse_pcm(SEED)
    exp = struct.unpack("<" + "h" * (len(expected) // 2), expected)
    assert list(samples[0::2]) == list(exp)
    assert list(samples[1::2]) == list(exp)


def test_stems_sample_exact(si_card, tmp_path):
    d = tmp_path / "s"
    assert main(["stems", str(si_card), "--out-dir", str(d)]) == 0
    wavs = list(d.glob("*.wav"))
    assert len(wavs) == 1
    nframes, nchannels, data = read_data(wavs[0])
    assert nchannels == 1
    assert nframes == STEM_SAMPLES
    expected = bytearray(STEM_SAMPLES * 2)
    expected[0:FRAG] = stem_fragment_payload(0, SEED)
    p = STEM_BOFFS[1] * 2
    expected[p : p + FRAG] = stem_fragment_payload(1, SEED)
    assert data == bytes(expected)


def test_impulse_offsets_mid_cluster():
    master_offs = impulse_offsets(MASTER_SAMPLES, SEED)
    assert len(master_offs) > 10  # guard against a vacuous pass
    frag_offs = impulse_offsets(FRAG_SAMPLES, SEED)
    assert len(frag_offs) >= 1  # guard against a vacuous pass
    stem_offs = [b + r for b in STEM_BOFFS for r in frag_offs]
    for o in (*master_offs, *stem_offs):
        assert o % HALF != 0, f"impulse at cluster boundary: {o:#x}"


def test_impulse_polarity_and_single_sample_width():
    o0, o1 = impulse_offsets(MASTER_SAMPLES, SEED)[:2]
    pcm = master_sine_impulse_pcm(SEED)
    assert struct.unpack_from("<h", pcm, o0 * 2)[0] == IMPULSE
    assert struct.unpack_from("<h", pcm, o1 * 2)[0] == -IMPULSE
    # single-sample spikes: immediate neighbours are ordinary sine samples
    for o in (o0, o1):
        for n in (o - 1, o + 1):
            v = struct.unpack_from("<h", pcm, n * 2)[0]
            assert abs(v) != IMPULSE
