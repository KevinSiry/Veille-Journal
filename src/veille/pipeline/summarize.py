"""Step 4: one LLM call per cluster -> one French headline + one-sentence summary.

Policy (documented in docs/PROMPTS.md):
- ``LLMError`` (invalid JSON, wrong shape, HTTP error...) -> retry once, then the cluster
  is dropped (``SummaryOutcome.reason`` starts with ``llm-error``).
- ``LLMUnavailableError`` (Ollama down, busy or timed out) on both attempts -> the cluster
  is dropped with ``unavailable=True``; the caller (``pipeline.run``) aborts the theme
  after two consecutive unavailable clusters, and marks it in error when no cluster
  produced an entry and none was rejected as not relevant.
- ``relevant == false`` -> dropped (``reason == "not-relevant"``).
- Post-validation: boilerplate openings removed, first sentence only, summary trimmed to
  35 words at a word boundary with "…", headline trimmed to 12 words.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from veille.contracts import LLM, Entry, LLMError, LLMUnavailableError, Theme
from veille.pipeline.dedupe import Cluster
from veille.pipeline.prompts import (
    SUMMARY_MAX_HEADLINE_WORDS,
    SUMMARY_MAX_WORDS,
    SUMMARY_SCHEMA,
    SUMMARY_SYSTEM,
    build_summary_user,
)

logger = logging.getLogger(__name__)

_BOILERPLATE_RE = re.compile(
    r"^\s*(?:"
    r"(?:cet|l['’]|le présent)\s*(?:article|texte|billet)\s+"
    r"(?:explique|indique|rapporte|affirme|souligne|annonce|précise|révèle|montre|"
    r"évoque|raconte|relate|détaille|note|signale|rappelle)\s+(?:que\s+|qu['’]\s*)"
    r"|selon\s+(?:cet|l['’])\s*(?:article|texte)\s*,?\s*"
    r"|(?:dans\s+)?(?:cet|l['’])\s*(?:article|texte)\s*,\s*"
    r")",
    re.IGNORECASE,
)
_ABBREVIATIONS = frozenset({"m", "mm", "mme", "mmes", "dr", "st", "pr", "me", "mlle", "etc"})
_SENTENCE_END_RE = re.compile(r"[.!?…]+(?=\s+\S)")
_TRAILING_PUNCT = " ,;:—–-"


@dataclass(frozen=True, slots=True)
class SummaryOutcome:
    entry: Entry | None  # None -> cluster dropped
    llm_calls: int
    tokens: int
    reason: str = ""
    unavailable: bool = False  # both attempts failed with LLMUnavailableError


def strip_boilerplate(text: str) -> str:
    stripped = _BOILERPLATE_RE.sub("", text, count=1).strip()
    if stripped and stripped != text.strip():
        stripped = stripped[0].upper() + stripped[1:]
    return stripped


def first_sentence(text: str) -> str:
    for match in _SENTENCE_END_RE.finditer(text):
        before = text[: match.start()].split()
        last_word = before[-1].rstrip(".") if before else ""
        is_initial = len(last_word) == 1 and last_word.isupper()
        if match.group() == "." and (last_word.casefold() in _ABBREVIATIONS or is_initial):
            continue  # "M. Macron", initials
        return text[: match.end()].strip()
    return text.strip()


def trim_words(text: str, limit: int, *, ellipsis: bool) -> str:
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    cut = " ".join(words[:limit]).rstrip(_TRAILING_PUNCT + ".")
    return f"{cut}…" if ellipsis else cut


def clean_headline(text: str) -> str:
    headline = " ".join(text.split()).strip().strip('"«»“”').strip()
    headline = trim_words(headline, SUMMARY_MAX_HEADLINE_WORDS, ellipsis=False)
    return headline.rstrip(_TRAILING_PUNCT + ".").strip()


def clean_summary(text: str) -> str:
    summary = strip_boilerplate(" ".join(text.split()))
    summary = first_sentence(summary)
    if len(summary.split()) > SUMMARY_MAX_WORDS:
        return trim_words(summary, SUMMARY_MAX_WORDS, ellipsis=True)
    if summary and summary[-1] not in ".!?…":
        summary = summary.rstrip(_TRAILING_PUNCT) + "."
    return summary


def _validate(payload: dict[str, Any]) -> tuple[bool, str, str]:
    relevant = payload.get("relevant")
    headline = payload.get("headline_fr")
    summary = payload.get("summary_fr")
    if not isinstance(relevant, bool) or not isinstance(headline, str):
        raise LLMError("Réponse du modèle non conforme au schéma")
    if not isinstance(summary, str):
        raise LLMError("Réponse du modèle non conforme au schéma")
    return relevant, headline, summary


def _usage(llm: LLM, system: str, user: str, payload: dict[str, Any] | None) -> int:
    last = getattr(llm, "last_usage", None)
    if isinstance(last, int):
        return last
    answer = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
    return (len(system) + len(user) + len(answer)) // 4


def build_cluster_prompt(cluster: Cluster, theme: Theme) -> str:
    primary = cluster.primary
    return build_summary_user(
        theme_name=theme.name,
        theme_description=theme.description,
        title=primary.title,
        source_name=primary.source_name,
        content=primary.content_text or primary.snippet,
        other_titles=[i.title for i in cluster.items[1:] if i.title != primary.title],
    )


def summarize_cluster(cluster: Cluster, theme: Theme, llm: LLM) -> SummaryOutcome:
    user = build_cluster_prompt(cluster, theme)
    calls = tokens = 0
    unavailable = 0
    payload: dict[str, Any] | None = None
    last_error: LLMError | None = None
    for _attempt in range(2):
        calls += 1
        payload = None
        try:
            payload = llm.chat_json(SUMMARY_SYSTEM, user, SUMMARY_SCHEMA)
            tokens += _usage(llm, SUMMARY_SYSTEM, user, payload)
            relevant, headline, summary = _validate(payload)
            break
        except LLMUnavailableError as exc:
            unavailable += 1
            last_error = exc
        except LLMError as exc:
            if payload is None:  # the call itself failed: the prompt was still processed
                tokens += _usage(llm, SUMMARY_SYSTEM, user, None)
            last_error = exc
    else:
        logger.warning("Cluster %r dropped after retry: %s", cluster.primary.title, last_error)
        return SummaryOutcome(
            None, calls, tokens, reason=f"llm-error: {last_error}", unavailable=unavailable == 2
        )

    if not relevant:
        return SummaryOutcome(None, calls, tokens, reason="not-relevant")
    headline, summary = clean_headline(headline), clean_summary(summary)
    if not headline or not summary:
        return SummaryOutcome(None, calls, tokens, reason="empty")
    entry = Entry(headline_fr=headline, summary_fr=summary, source_links=cluster.source_links())
    return SummaryOutcome(entry, calls, tokens)
