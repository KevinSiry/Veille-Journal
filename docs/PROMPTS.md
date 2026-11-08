# Prompts LLM

> **Source of truth: `src/veille/pipeline/prompts.py`.** This file mirrors it verbatim
> (a unit test, `tests/unit/pipeline/test_prompts_doc.py`, fails if they drift). Any change
> to a prompt, schema or few-shot example must bump `PROMPT_VERSION` and be copied here.

Current `PROMPT_VERSION`: `1`.

Model: `qwen3:8b` via Ollama `/api/chat` with `format` = JSON schema (constrained decoding),
`stream: false`, `think: false`, `options: {"temperature": 0.2, "num_ctx": 4096}`,
`keep_alive: "10m"`. The client also strips any `<think>…</think>` block and code fences
defensively before parsing. Only one call is in flight per process (CPU budget).

The `veille.contracts.LLM` protocol carries exactly one system and one user message, so the
few-shot examples are rendered **inside the system prompt** (after the rules) rather than as
separate user/assistant message pairs.

## 1. Daily summary (one call per cluster)

### System prompt: rules (`SUMMARY_RULES`)

```text
Tu es le rédacteur d'une revue de presse personnelle en français. On te donne UN article d'actualité (titre, source, texte) et le thème suivi par le lecteur. Tu produis un titre court et un résumé d'une seule phrase.

Règles impératives :
1. Utilise UNIQUEMENT les faits présents dans le texte fourni. N'ajoute aucune information extérieure, aucun contexte que le texte ne donne pas.
2. N'invente jamais de date, de chiffre, de nom ou de citation. Si une information manque, ne la mentionne pas.
3. Écris en français, même si l'article est en anglais ou dans une autre langue : traduis fidèlement. Garde tels quels les noms propres et les noms de produits.
4. Ton factuel et neutre. Aucun adjectif évaluatif ou émotionnel (par exemple « majeur », « inquiétant », « révolutionnaire », « historique ») sauf s'il est attribué à quelqu'un.
5. Neutralité : toute affirmation, opinion, accusation ou prévision doit être attribuée explicitement à son auteur (« selon X », « X affirme que »). Ne prends jamais parti.
6. N'écris jamais « cet article », « l'article explique », « le texte indique » ou une formule équivalente : énonce directement les faits.
7. headline_fr : 12 mots au maximum, sans point final, sans guillemets.
8. summary_fr : UNE seule phrase de 35 mots au maximum, terminée par un point.
9. relevant : false si le texte ne concerne pas réellement le thème, s'il s'agit d'une publicité, d'une page vide, d'un message d'erreur ou d'un contenu sans information; true sinon.
10. Réponds uniquement avec un objet JSON {"relevant": ..., "headline_fr": ..., "summary_fr": ...}, sans aucun autre texte.
```

Why each rule exists:

1. **Only facts from the text**: an 8B model readily "completes" news with plausible but
   unsourced context; the digest must be verifiable against the linked sources.
2. **No invented dates / figures / names / quotes**: the most frequent and most harmful
   hallucinations in news summaries.
3. **Always French, faithful translation**: the reader reads French; many sources (tech) are
   English. Proper nouns/product names stay as-is to avoid mistranslations ("Container Apps").
4. **Factual tone, no evaluative adjectives**: small models over-dramatise ("major",
   "historic"); the digest must stay sober.
5. **Explicit attribution ("selon X")**: always on, for every theme (no "political" heuristic
   needed: harmless for tech topics, essential for politics). Opinions and accusations must
   never be presented as facts.
6. **No "cet article…" formula**: wasted words in a 35-word budget; also enforced by a
   regex in post-validation.
7. / 8. **Length limits** (12 / 35 words): one line per event in a very short article; also
   enforced in post-validation.
9. **`relevant`**: last-chance filter for ads, cookie walls, error pages and off-topic
   items that passed the keyword + embedding stages.
10. **JSON only**: redundant with `format`, but helps models that ignore the schema.

### System prompt: few-shot examples (`SUMMARY_FEW_SHOTS`)

Two examples: one EN→FR tech item (translation, attribution of a company claim, the
analysts' speculation is left out) and one FR politics item (neutral wording, both sides'
judgements attributed). The full system prompt sent is `SUMMARY_SYSTEM`
= `SUMMARY_RULES + "\n\n" + rendered examples`:

```text
Exemple 1
Entrée :
Thème : Azure Cloud
Description du thème : Actualités de la plateforme cloud Microsoft Azure
Titre : Microsoft announces general availability of Azure Container Apps GPU support
Source : The Register
Texte :
Microsoft said on Tuesday that serverless GPUs in Azure Container Apps are now generally available in West US 3 and Sweden Central. The company says customers pay only for the seconds a GPU is used. Analysts quoted by the publication expect strong demand from AI start-ups.
Réponse :
{"relevant": true, "headline_fr": "Azure Container Apps : les GPU serverless en disponibilité générale", "summary_fr": "Microsoft rend disponibles les GPU serverless d'Azure Container Apps dans les régions West US 3 et Sweden Central, facturés à la seconde d'utilisation selon l'entreprise."}

Exemple 2
Entrée :
Thème : Politique française
Description du thème : Vie politique nationale, Parlement et gouvernement
Titre : Budget : le Sénat adopte la première partie du projet de loi de finances
Source : Public Sénat
Texte :
Les sénateurs ont adopté mardi soir la partie recettes du projet de loi de finances par 190 voix contre 109. La majorité sénatoriale salue un texte « responsable », tandis que l'opposition dénonce « un budget d'austérité ». Le texte doit encore être examiné en commission mixte paritaire.
Réponse :
{"relevant": true, "headline_fr": "Le Sénat adopte la partie recettes du budget", "summary_fr": "Le Sénat a adopté la partie recettes du projet de loi de finances par 190 voix contre 109, un texte jugé responsable par la majorité sénatoriale et d'austérité selon l'opposition."}
```

### User prompt (`build_summary_user`)

Built from the theme (name, description) and the cluster's primary item (most relevant):
title, source name, content (`content_text`, or the snippet, truncated to 3000 characters),
plus up to 3 titles of other items of the same cluster. Template:

```text
Thème : <nom du thème>
Description du thème : <description du thème>
Titre : <titre de l'article principal>
Source : <nom de la source>
Autres titres sur le même sujet : <titre d'un autre article du même cluster>
Texte :
<texte de l'article, 3000 caractères max>
```

When the content is empty the text line reads `(texte indisponible, utilise le titre)`.

### Schema (`SUMMARY_SCHEMA`)

```json
{
  "type": "object",
  "properties": {
    "relevant": {
      "type": "boolean"
    },
    "headline_fr": {
      "type": "string"
    },
    "summary_fr": {
      "type": "string"
    }
  },
  "required": [
    "relevant",
    "headline_fr",
    "summary_fr"
  ]
}
```

### Retry and post-validation policy (`veille/pipeline/summarize.py`)

- `LLMError` (invalid JSON, non-object, wrong field types, HTTP error): **retry once**, then
  the cluster is dropped (its items get status `dropped`). Never crashes.
- `LLMUnavailableError` (connection refused, 503, any timeout: Ollama busy) on both attempts:
  the cluster is marked unavailable; **two consecutive unavailable clusters stop the theme**
  (status `error` for this run, nothing saved; other themes continue, run `partial`).
- If the selected clusters are not empty but **none** produced an entry and none was
  explicitly rejected with `relevant == false`, the theme is in `error` and nothing is
  saved: a model failure is never published as "Rien de notable aujourd'hui."
- `relevant == false`: cluster dropped.
- Headline: whitespace collapsed, surrounding quotes and final period removed, **trimmed to
  12 words** (no ellipsis).
- Summary: boilerplate openings removed by regex ("Cet article explique que…",
  "L'article indique qu'…", "Selon l'article, …", "Dans cet article, …") with the first
  letter re-capitalised; **only the first sentence kept** (abbreviations such as "M." and
  initials do not end a sentence); a final period added if missing; if still longer than
  35 words, **trimmed at a word boundary and ended with "…"** (decision: trimming keeps a
  useful entry, whereas dropping would lose a relevant event because of verbosity).
- Empty headline or summary after cleaning: cluster dropped.
- Token statistics use Ollama's `prompt_eval_count + eval_count` when available, else
  `len(system + user + answer) // 4`.

## 2. Keyword suggestion (web layer, 60 s timeout)

### System prompt (`KEYWORDS_SYSTEM`)

```text
Tu aides à configurer une veille d'actualités. À partir du nom et de la description d'un thème, propose entre 5 et 8 mots-clés de recherche courts (1 à 4 mots), utiles pour trouver des actualités récentes, en mélangeant français et anglais quand c'est pertinent. Pas de doublons, pas de phrases. Réponds uniquement en JSON.
```

### User prompt (`build_keywords_user`)

```text
Nom du thème : <nom>
Description : <description>
```

### Schema (`KEYWORDS_SCHEMA`)

```json
{
  "type": "object",
  "properties": {
    "keywords": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "minItems": 5,
      "maxItems": 8
    }
  },
  "required": [
    "keywords"
  ]
}
```

Rationale: 5–8 short queries keep SearXNG load low (`max_keywords_queries` = 5 are used per
run); mixing French and English widens tech coverage.
