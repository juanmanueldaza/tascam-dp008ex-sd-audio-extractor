"""Skip reporting: named, counted data-loss records + strict exit codes.

After A2 there is no silent loss: every drop/truncate/cap/OOB/short-read
emits a Skip. Strict mode is default-on; --allow-incomplete opts out.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum


class Kind(str, Enum):
    """Loss taxonomy. Every Skip carries one of these (AC-9 audit anchor)."""

    TRUNCATION = "truncation"
    CAP = "cap"
    OOB = "oob"
    SHORT_READ = "short-read"
    ODD_BYTE = "odd-byte"
    OMISSION = "omission"
    UNPAIRED = "unpaired"


@dataclass(frozen=True, slots=True)
class Skip:
    """One reported data-loss event: which site, what kind, human detail."""

    site: str
    kind: Kind
    detail: str


@dataclass(frozen=True, slots=True)
class RenderResult:
    """Renderer output: audio bytes plus every loss encountered producing them."""

    pcm: bytes
    skips: tuple[Skip, ...] = ()
    coverage: float | None = None


def summarize(skips: Sequence[Skip], total: int, unit: str) -> str:
    """One-line strict summary: '<n> of <total> <unit> incomplete (<n> skips)'."""
    n = len(skips)
    return f"{n} of {total} {unit} incomplete ({n} skips)"


def strict_exit(skips: Sequence[Skip], allow_incomplete: bool) -> int:
    """Non-zero when anything was skipped and the user did not opt out."""
    return 0 if (not skips or allow_incomplete) else 1
