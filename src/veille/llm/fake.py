"""Deterministic in-process LLM used by tests and offline development."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from veille.contracts import LLMError
from veille.pipeline.prompts import KEYWORDS_SYSTEM
from veille.pipeline.text import tokens

DEFAULT_DIM = 256

_STOPWORDS_TEXT = (
    "a an and the of to in on for with by at is are was be from as it its this that "
    "le la les un une des de du d l et ou en au aux sur pour par avec dans est sont "
    "ce cette ces qui que qu se sa son ses leur leurs plus"
)
_STOPWORDS = frozenset(_STOPWORDS_TEXT.split())
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _first_words(text: str, n: int) -> str:
    return " ".join(text.split()[:n])


class FakeLLM:
    """Implements ``veille.contracts.LLM`` without any network.

    * ``embed``: hashed bag of normalised words (accent-insensitive, stopwords removed),
      L2-normalised, so texts sharing most words get a high cosine.
    * ``chat_json``: scripted ``responses`` are consumed first (a ``dict`` is returned, a
      ``str`` is parsed as JSON - invalid JSON raises ``LLMError`` like the real client -
      and an ``Exception`` is raised). Then a deterministic answer derived from the prompt.
    """

    def __init__(
        self,
        responses: Sequence[dict[str, Any] | str | Exception] | None = None,
        *,
        model: str = "fake-llm",
        dim: int = DEFAULT_DIM,
        embed_error: Exception | None = None,
    ) -> None:
        self.model = model
        self.dim = dim
        self.responses: list[dict[str, Any] | str | Exception] = list(responses or [])
        self.embed_error = embed_error
        self.calls: list[tuple[str, str, Mapping[str, Any]]] = []
        self.embed_calls: list[list[str]] = []
        self.last_usage: int | None = None

    # ------------------------------------------------------------------- chat

    def chat_json(self, system: str, user: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append((system, user, schema))
        if self.responses:
            scripted = self.responses.pop(0)
            if isinstance(scripted, Exception):
                raise scripted
            if isinstance(scripted, str):
                try:
                    parsed = json.loads(scripted)
                except ValueError as exc:
                    raise LLMError("JSON invalide renvoyé par le modèle") from exc
                if not isinstance(parsed, dict):
                    raise LLMError("Le modèle n'a pas renvoyé un objet JSON")
                return parsed
            return dict(scripted)
        if system == KEYWORDS_SYSTEM:
            return self._keywords(user)
        return self._summary(user)

    @staticmethod
    def _field(user: str, label: str) -> str:
        for line in user.splitlines():
            if line.startswith(f"{label} :"):
                return line.split(":", 1)[1].strip()
        return ""

    def _keywords(self, user: str) -> dict[str, Any]:
        words = [w for w in tokens(self._field(user, "Nom du thème")) if w not in _STOPWORDS]
        words += [w for w in tokens(self._field(user, "Description")) if w not in _STOPWORDS]
        unique = list(dict.fromkeys(words))
        while len(unique) < 5:
            unique.append(f"mot-clé {len(unique) + 1}")
        return {"keywords": unique[:8]}

    def _summary(self, user: str) -> dict[str, Any]:
        title = self._field(user, "Titre")
        _, _, text = user.partition("Texte :")
        text = " ".join(text.split()) or title
        first = _SENTENCE_RE.split(text, maxsplit=1)[0]
        summary = _first_words(first, 35)
        if not summary.endswith((".", "!", "?")):
            summary += "."
        return {"relevant": True, "headline_fr": _first_words(title, 8), "summary_fr": summary}

    # ------------------------------------------------------------------ embed

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        if self.embed_error is not None:
            raise self.embed_error
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in tokens(text):
            if word in _STOPWORDS:
                continue
            digest = hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest()
            vec[int.from_bytes(digest, "big") % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm else vec
