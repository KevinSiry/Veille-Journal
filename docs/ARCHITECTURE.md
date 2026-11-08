# Architecture

One code base, one Docker image, two processes (`web`, `worker`) sharing one SQLite file
(WAL mode), plus `ollama` and `searxng` containers on an internal network.

```
browser ──▶ web (FastAPI, Jinja2, HTMX) ──▶ SQLite (WAL + FTS5) ◀── worker (veille worker / run)
                     │ keyword suggestion only                         │
                     └──────────────▶ Ollama ◀─────────────────────────┤ summaries + embeddings
                                       SearXNG ◀───────────────────────┤ news search (JSON API)
                                       RSS feeds / article pages ◀─────┘ (SSRF-guarded HTTP)
```

## Packages

| Package | Role |
|---|---|
| `veille.contracts` | Frozen dataclasses (`Theme`, `Source`, `RawItem`, `Item`, `Entry`, `ArticleDraft`, `RunStats`…) and Protocols (`Store`, `Collector`, `ContentExtractor`, `LLM`, `Clock`). The only integration surface between packages. |
| `veille.config` | `Settings` (pydantic-settings): every environment variable, documented in `.env.example`. |
| `veille.db` | `SqliteStore` (plain `sqlite3`, explicit `BEGIN IMMEDIATE` transactions), numbered SQL migrations applied on open (`schema_migrations`), FTS5 table synced by triggers. |
| `veille.collect` | `RssCollector` (feedparser), `SearxngCollector`, `TrafilaturaExtractor`, `canonicalize`, `net_safety` (SSRF guard + per-host rate limit + DNS-pinned transport). |
| `veille.llm` | `OllamaClient` (`/api/chat` with JSON schema `format`, `think: false`; `/api/embed`), one call in flight per process; `FakeLLM` for tests. |
| `veille.pipeline` | `prepare` → `relevance` (stage 1 keywords / pinned source, stage 2 embeddings) → `dedupe` (title-embedding clusters, top N) → `summarize` (1 call per cluster, 1 retry) → `render` (deterministic Markdown, links only from the DB) → `run` (per-theme isolation, run lock, stats). |
| `veille.web` | Pages, auth (argon2id + signed session cookie), CSRF, security headers, login rate limit. Pico.css and HTMX are vendored in `static/`. |
| `veille.cli`, `veille.wiring`, `veille.scheduler`, `veille.doctor` | Composition root and commands. |

## Key decisions

- **The LLM never writes URLs.** Entries reference items by canonical URL; `render.py`
  builds the links from `entry_sources`, and escapes every text coming from the LLM or a feed.
- **Idempotence.** `UNIQUE(theme_id, day)`: a second run the same day skips existing
  articles; `--force` replaces them and reconsiders the items fetched that day
  (`known_canonical_urls(before=start of day)`).
- **Run lock** = a `runs` row in status `running` whose heartbeat (refreshed before each
  theme and each cluster) is younger than 30 minutes (`start_run`, `heartbeat_run`), so the
  CLI and the scheduler can never run concurrently, even across containers.
- **Outages are not quiet days.** A theme where every collector that had work failed, or
  where the model failed on every selected cluster, ends in `error` and no article is saved
  (the next run retries it).
- **Extraction only for the top N.** Collection and filtering use titles and snippets;
  trafilatura is only called for the primary item of the selected clusters (≤ 8 per theme).
- **Item statuses.** `used` = linked from an entry; `selected` = in a rendered cluster but
  not linked (same source already linked); `dropped` = everything else. Old items keep
  their row (URL dedupe) but their `content_text` is blanked after 30 days.
- **Timezone.** Timestamps stored as UTC ISO-8601; the `day` key and every displayed date
  use `TIMEZONE` (Europe/Paris).
- **CPU budget.** Sequential themes, one LLM call at a time (`OLLAMA_NUM_PARALLEL=1`), the
  worker runs with `nice 10`; the empty-day path makes zero chat calls.

## Library choices

- `feedparser` (RSS/Atom), `trafilatura` (article text), `httpx` + `httpcore` (HTTP; the
  SSRF guard uses a custom httpcore network backend to connect only to the vetted IP).
- `argon2-cffi` (password hash), Starlette `SessionMiddleware` / `itsdangerous` (signed
  cookie), `markdown-it-py` with HTML disabled (article rendering).
- `argparse` for the CLI (no extra dependency for six commands).
- `tzdata` so `zoneinfo` works on Windows.

## Deviations from the brief

- `Collector.collect(theme, sources)` receives the theme's RSS sources explicitly instead of
  `collect(theme)`: collectors stay free of database access.
- The two few-shot examples live inside the system prompt: the `LLM` protocol takes one
  system and one user message.
- Over-long summaries are trimmed at a word boundary (with "…") instead of being dropped.

## Known debt

(Filled in after the review phase, see below.)
