"""LLM prompts: the single source of truth, mirrored verbatim in docs/PROMPTS.md.

Owner: pipeline agent. ``KEYWORDS_SYSTEM``, ``KEYWORDS_SCHEMA`` and ``build_keywords_user``
are imported by the web layer (keyword suggestion): keep their names and signatures.

Bump ``PROMPT_VERSION`` whenever a prompt, a schema or a few-shot example changes.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

PROMPT_VERSION = "1"

# ------------------------------------------------------------------ keywords

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


# ------------------------------------------------------------------- summary

SUMMARY_MAX_HEADLINE_WORDS = 12
SUMMARY_MAX_WORDS = 35
SUMMARY_MAX_CONTENT_CHARS = 3000
SUMMARY_MAX_OTHER_TITLES = 3

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "headline_fr": {"type": "string"},
        "summary_fr": {"type": "string"},
    },
    "required": ["relevant", "headline_fr", "summary_fr"],
}

SUMMARY_RULES = """Tu es le rédacteur d'une revue de presse personnelle en français. On te donne \
UN article d'actualité (titre, source, texte) et le thème suivi par le lecteur. Tu produis \
un titre court et un résumé d'une seule phrase.

Règles impératives :
1. Utilise UNIQUEMENT les faits présents dans le texte fourni. N'ajoute aucune information \
extérieure, aucun contexte que le texte ne donne pas.
2. N'invente jamais de date, de chiffre, de nom ou de citation. Si une information manque, \
ne la mentionne pas.
3. Écris en français, même si l'article est en anglais ou dans une autre langue : traduis \
fidèlement. Garde tels quels les noms propres et les noms de produits.
4. Ton factuel et neutre. Aucun adjectif évaluatif ou émotionnel (par exemple « majeur », \
« inquiétant », « révolutionnaire », « historique ») sauf s'il est attribué à quelqu'un.
5. Neutralité : toute affirmation, opinion, accusation ou prévision doit être attribuée \
explicitement à son auteur (« selon X », « X affirme que »). Ne prends jamais parti.
6. N'écris jamais « cet article », « l'article explique », « le texte indique » ou une \
formule équivalente : énonce directement les faits.
7. headline_fr : 12 mots au maximum, sans point final, sans guillemets.
8. summary_fr : UNE seule phrase de 35 mots au maximum, terminée par un point.
9. relevant : false si le texte ne concerne pas réellement le thème, s'il s'agit d'une \
publicité, d'une page vide, d'un message d'erreur ou d'un contenu sans information; \
true sinon.
10. Réponds uniquement avec un objet JSON {"relevant": ..., "headline_fr": ..., \
"summary_fr": ...}, sans aucun autre texte."""


def build_summary_user(
    *,
    theme_name: str,
    theme_description: str,
    title: str,
    source_name: str,
    content: str,
    other_titles: Sequence[str] = (),
) -> str:
    """User prompt for one cluster. ``content`` is truncated to 3000 characters."""
    lines = [
        f"Thème : {theme_name}",
        f"Description du thème : {theme_description or '(aucune)'}",
        f"Titre : {title}",
        f"Source : {source_name or '(inconnue)'}",
    ]
    others = [t for t in other_titles if t.strip()][:SUMMARY_MAX_OTHER_TITLES]
    if others:
        lines.append("Autres titres sur le même sujet : " + " | ".join(others))
    body = content.strip()[:SUMMARY_MAX_CONTENT_CHARS] or "(texte indisponible, utilise le titre)"
    lines.append(f"Texte :\n{body}")
    return "\n".join(lines)


# Few-shot examples as (user prompt, expected JSON answer) pairs. The LLM contract only
# carries one system and one user message, so they are rendered inside SUMMARY_SYSTEM.
SUMMARY_FEW_SHOTS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        build_summary_user(
            theme_name="Azure Cloud",
            theme_description="Actualités de la plateforme cloud Microsoft Azure",
            title="Microsoft announces general availability of Azure Container Apps GPU support",
            source_name="The Register",
            content=(
                "Microsoft said on Tuesday that serverless GPUs in Azure Container Apps are "
                "now generally available in West US 3 and Sweden Central. The company says "
                "customers pay only for the seconds a GPU is used. Analysts quoted by the "
                "publication expect strong demand from AI start-ups."
            ),
        ),
        {
            "relevant": True,
            "headline_fr": "Azure Container Apps : les GPU serverless en disponibilité générale",
            "summary_fr": (
                "Microsoft rend disponibles les GPU serverless d'Azure Container Apps dans "
                "les régions West US 3 et Sweden Central, facturés à la seconde d'utilisation "
                "selon l'entreprise."
            ),
        },
    ),
    (
        build_summary_user(
            theme_name="Politique française",
            theme_description="Vie politique nationale, Parlement et gouvernement",
            title="Budget : le Sénat adopte la première partie du projet de loi de finances",
            source_name="Public Sénat",
            content=(
                "Les sénateurs ont adopté mardi soir la partie recettes du projet de loi de "
                "finances par 190 voix contre 109. La majorité sénatoriale salue un texte "
                "« responsable », tandis que l'opposition dénonce « un budget d'austérité ». "
                "Le texte doit encore être examiné en commission mixte paritaire."
            ),
        ),
        {
            "relevant": True,
            "headline_fr": "Le Sénat adopte la partie recettes du budget",
            "summary_fr": (
                "Le Sénat a adopté la partie recettes du projet de loi de finances par 190 "
                "voix contre 109, un texte jugé responsable par la majorité sénatoriale et "
                "d'austérité selon l'opposition."
            ),
        },
    ),
)


def _render_few_shots() -> str:
    parts = []
    for index, (user, answer) in enumerate(SUMMARY_FEW_SHOTS, start=1):
        parts.append(
            f"Exemple {index}\nEntrée :\n{user}\nRéponse :\n"
            f"{json.dumps(answer, ensure_ascii=False)}"
        )
    return "\n\n".join(parts)


SUMMARY_SYSTEM = f"{SUMMARY_RULES}\n\n{_render_few_shots()}"
