"""LLM prompts (single source of truth, mirrored in docs/PROMPTS.md).

Owner: pipeline agent. The keyword-suggestion prompt below is used by the web layer.
"""

from __future__ import annotations

from typing import Any

PROMPT_VERSION = "0"

KEYWORDS_SYSTEM = (
    "Tu aides à configurer une veille d'actualités. À partir du nom et de la description "
    "d'un thème, propose entre 5 et 8 mots-clés de recherche courts (1 à 4 mots), utiles "
    "pour trouver des actualités récentes, en mélangeant français et anglais quand c'est "
    "pertinent. Pas de doublons, pas de phrases. Réponds uniquement en JSON."
)

KEYWORDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "keywords": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 5,
            "maxItems": 8,
        }
    },
    "required": ["keywords"],
}


def build_keywords_user(name: str, description: str) -> str:
    return f"Nom du thème : {name}\nDescription : {description or '(aucune)'}"
