"""Text normalisation helpers shared by the pipeline steps (and the fake LLM)."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence

_NON_WORD_RE = re.compile(r"[^0-9a-z]+")


def fold(text: str) -> str:
    """Casefold and strip accents (NFKD, combining marks removed)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_words(text: str) -> str:
    """Accent-insensitive lowercase text with every non-alphanumeric run turned into a
    single space, padded with spaces so ``f" {word} " in result`` is a word-boundary test."""
    return f" {' '.join(_NON_WORD_RE.sub(' ', fold(text)).split())} "


def tokens(text: str) -> list[str]:
    return normalize_words(text).split()


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
