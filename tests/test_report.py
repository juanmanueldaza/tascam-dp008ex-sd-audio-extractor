"""Unit tests for the Skip/RenderResult core (T3 done-condition)."""

import dataclasses

import pytest

from tascam_dp008ex_sd_audio_extractor.report import (
    Kind,
    RenderResult,
    Skip,
    strict_exit,
    summarize,
)


def test_skip_is_frozen():
    s = Skip(site="E1", kind=Kind.SHORT_READ, detail="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.detail = "y"


def test_render_result_is_frozen_with_sane_defaults():
    r = RenderResult(pcm=b"\x00\x01")
    assert r.skips == ()
    assert r.coverage is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.pcm = b""


def test_summarize_shape():
    skips = (
        Skip(site="E1", kind=Kind.SHORT_READ, detail="a"),
        Skip(site="E2", kind=Kind.CAP, detail="b"),
    )
    out = summarize(skips, 12, "chains")
    assert out == "2 of 12 chains incomplete (2 skips)"
    assert summarize((), 12, "chains") == "0 of 12 chains incomplete (0 skips)"


def test_strict_exit_matrix():
    skips = (Skip(site="E1", kind=Kind.SHORT_READ, detail="a"),)
    assert strict_exit((), False) == 0
    assert strict_exit((), True) == 0
    assert strict_exit(skips, True) == 0
    assert strict_exit(skips, False) == 1
