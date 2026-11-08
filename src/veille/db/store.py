"""SQLite implementation of :class:`veille.contracts.Store`."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
from array import array
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import Any

from veille.contracts import (
    HIGHLIGHT_END,
    HIGHLIGHT_START,
    Article,
    ArticleDraft,
    ArticleStatus,
    Clock,
    Item,
    Run,
    RunStats,
    RunStatus,
    RunTrigger,
    SearchHit,
    Source,
    Theme,
    ThemeConflictError,
)

logger = logging.getLogger(__name__)

RUN_LOCK_TTL = timedelta(hours=3)
# Stays well below SQLite's default SQLITE_MAX_VARIABLE_NUMBER (32766).
_IN_CHUNK = 500

_ARTICLE_COLUMNS = (
    "a.id, a.theme_id, t.name AS theme_name, a.day, a.title, a.body_md, a.model, "
    "a.generated_at, a.entry_count, a.status"
)


# ------------------------------------------------------------------ conversions


def to_db_datetime(value: datetime) -> str:
    """Aware datetime -> fixed-width UTC ISO-8601 string (sortable as text)."""
    if value.tzinfo is None:
        raise ValueError("naive datetime not allowed, use an aware UTC datetime")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def from_db_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def pack_embedding(vector: Sequence[float] | None) -> bytes | None:
    """float32 little-endian blob, or None."""
    if vector is None:
        return None
    packed = array("f", vector)
    if sys.byteorder == "big":  # pragma: no cover - little-endian hosts only in CI
        packed.byteswap()
    return packed.tobytes()


def unpack_embedding(blob: bytes | None) -> tuple[float, ...] | None:
    if blob is None:
        return None
    packed = array("f")
    packed.frombytes(blob)
    if sys.byteorder == "big":  # pragma: no cover
        packed.byteswap()
    return tuple(packed)


def fts_query(user_input: str) -> str | None:
    """Turn free user input into a safe FTS5 MATCH expression (implicit AND).

    Each whitespace-separated token is stripped of double quotes and wrapped in
    double quotes; a trailing ``*`` becomes a prefix query. Tokens without any
    letter or digit are dropped. Returns None when nothing searchable is left.
    """
    terms: list[str] = []
    for raw in user_input.split():
        token = raw.replace('"', "")
        prefix = token.endswith("*")
        token = token.rstrip("*")
        if not any(ch.isalnum() for ch in token):
            continue
        terms.append(f'"{token}"' + ("*" if prefix else ""))
    return " ".join(terms) if terms else None


def _theme(row: sqlite3.Row) -> Theme:
    return Theme(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        keywords=tuple(json.loads(row["keywords_json"])),
        active=bool(row["active"]),
        created_at=from_db_datetime(row["created_at"]),
    )


def _source(row: sqlite3.Row) -> Source:
    return Source(
        id=row["id"],
        theme_id=row["theme_id"],
        url=row["url"],
        label=row["label"],
        active=bool(row["active"]),
        kind=row["kind"],
    )


def _article(row: sqlite3.Row) -> Article:
    return Article(
        id=row["id"],
        theme_id=row["theme_id"],
        theme_name=row["theme_name"],
        day=date.fromisoformat(row["day"]),
        title=row["title"],
        body_md=row["body_md"],
        model=row["model"],
        generated_at=from_db_datetime(row["generated_at"]),
        entry_count=row["entry_count"],
        status=ArticleStatus(row["status"]),
    )


def _run(row: sqlite3.Row) -> Run:
    finished = row["finished_at"]
    return Run(
        id=row["id"],
        started_at=from_db_datetime(row["started_at"]),
        finished_at=from_db_datetime(finished) if finished is not None else None,
        trigger=RunTrigger(row["trigger"]),
        status=RunStatus(row["status"]),
        stats=json.loads(row["stats_json"]),
        error=row["error"],
    )


def _keywords_json(keywords: Sequence[str]) -> str:
    return json.dumps(list(keywords), ensure_ascii=False)


# ------------------------------------------------------------------------ store


class _UTCClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SqliteStore:
    """Store backed by one SQLite connection (already migrated).

    The connection must be in autocommit mode (see :func:`veille.db.connection.connect`).
    A re-entrant lock serialises access so the store can be shared between threads.
    """

    def __init__(self, conn: sqlite3.Connection, *, clock: Clock | None = None) -> None:
        self._conn = conn
        self._clock: Clock = clock if clock is not None else _UTCClock()
        self._lock = threading.RLock()

    # ----------------------------------------------------------------- plumbing

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Write transaction (BEGIN IMMEDIATE), rolled back on any exception."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
            return row

    # ------------------------------------------------------------------- themes

    def list_themes(self, *, active_only: bool = False) -> list[Theme]:
        where = "WHERE active = 1 " if active_only else ""
        return [_theme(r) for r in self._all(f"SELECT * FROM themes {where}ORDER BY name")]  # noqa: S608

    def get_theme(self, theme_id: int) -> Theme | None:
        row = self._one("SELECT * FROM themes WHERE id = ?", (theme_id,))
        return _theme(row) if row is not None else None

    def create_theme(self, name: str, description: str, keywords: Sequence[str]) -> Theme:
        created = to_db_datetime(self._clock.now())
        try:
            with self._tx() as conn:
                cur = conn.execute(
                    "INSERT INTO themes (name, description, keywords_json, active, created_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (name, description, _keywords_json(keywords), created),
                )
                theme_id = cur.lastrowid
        except sqlite3.IntegrityError as exc:
            raise ThemeConflictError(f"Un thème nommé « {name} » existe déjà.") from exc
        assert theme_id is not None
        theme = self.get_theme(theme_id)
        assert theme is not None
        return theme

    def update_theme(
        self,
        theme_id: int,
        *,
        name: str,
        description: str,
        keywords: Sequence[str],
        active: bool,
    ) -> Theme:
        """Raises ThemeConflictError on duplicate name, KeyError if the theme is unknown."""
        try:
            with self._tx() as conn:
                cur = conn.execute(
                    "UPDATE themes SET name = ?, description = ?, keywords_json = ?, active = ? "
                    "WHERE id = ?",
                    (name, description, _keywords_json(keywords), int(active), theme_id),
                )
                updated = cur.rowcount
        except sqlite3.IntegrityError as exc:
            raise ThemeConflictError(f"Un thème nommé « {name} » existe déjà.") from exc
        theme = self.get_theme(theme_id)
        if updated == 0 or theme is None:
            raise KeyError(theme_id)
        return theme

    def delete_theme(self, theme_id: int) -> None:
        # ON DELETE CASCADE removes sources, items, articles (FTS via trigger), entries.
        with self._tx() as conn:
            conn.execute("DELETE FROM themes WHERE id = ?", (theme_id,))

    # ------------------------------------------------------------------ sources

    def list_sources(self, theme_id: int, *, active_only: bool = False) -> list[Source]:
        sql = "SELECT * FROM sources WHERE theme_id = ?"
        if active_only:
            sql += " AND active = 1"
        return [_source(r) for r in self._all(sql + " ORDER BY id", (theme_id,))]

    def add_source(self, theme_id: int, url: str, label: str) -> Source:
        """Raises ValueError for a duplicate URL in the theme or an unknown theme."""
        try:
            with self._tx() as conn:
                cur = conn.execute(
                    "INSERT INTO sources (theme_id, kind, url, label, active) "
                    "VALUES (?, 'rss', ?, ?, 1)",
                    (theme_id, url, label),
                )
                source_id = cur.lastrowid
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Source impossible à ajouter : {url}") from exc
        row = self._one("SELECT * FROM sources WHERE id = ?", (source_id,))
        assert row is not None
        return _source(row)

    def delete_source(self, source_id: int) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))

    # -------------------------------------------------------------------- items

    def known_canonical_urls(
        self, theme_id: int, urls: Iterable[str], *, before: datetime | None = None
    ) -> set[str]:
        wanted = list(dict.fromkeys(urls))
        known: set[str] = set()
        for start in range(0, len(wanted), _IN_CHUNK):
            chunk = wanted[start : start + _IN_CHUNK]
            marks = ",".join("?" * len(chunk))
            sql = (
                f"SELECT canonical_url FROM items WHERE theme_id = ? AND canonical_url IN ({marks})"  # noqa: S608
            )
            params: list[Any] = [theme_id, *chunk]
            if before is not None:
                sql += " AND fetched_at < ?"
                params.append(to_db_datetime(before))
            known.update(r[0] for r in self._all(sql, params))
        return known

    def purge_old_content(self, older_than: datetime) -> int:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE items SET content_text = '' WHERE fetched_at < ? AND content_text != ''",
                (to_db_datetime(older_than),),
            )
            return cur.rowcount

    def _upsert_item(self, conn: sqlite3.Connection, theme_id: int, item: Item) -> int:
        # fetched_at keeps the first-seen value so `before` filters stay meaningful.
        row = conn.execute(
            """
            INSERT INTO items (theme_id, url, canonical_url, title, published_at, fetched_at,
                               source_name, origin, content_text, content_hash, embedding,
                               relevance, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (theme_id, canonical_url) DO UPDATE SET
                url = excluded.url,
                title = excluded.title,
                published_at = excluded.published_at,
                source_name = excluded.source_name,
                origin = excluded.origin,
                content_text = excluded.content_text,
                content_hash = excluded.content_hash,
                embedding = COALESCE(excluded.embedding, items.embedding),
                relevance = excluded.relevance,
                status = excluded.status
            RETURNING id
            """,
            (
                theme_id,
                item.url,
                item.canonical_url,
                item.title,
                to_db_datetime(item.published_at) if item.published_at else None,
                to_db_datetime(item.fetched_at),
                item.source_name,
                item.origin.value,
                item.content_text,
                item.content_hash,
                pack_embedding(item.embedding),
                item.relevance,
                item.status.value,
            ),
        ).fetchone()
        item_id: int = row[0]
        return item_id

    # ----------------------------------------------------------------- articles

    def get_article(self, theme_id: int, day: date) -> Article | None:
        row = self._one(
            f"SELECT {_ARTICLE_COLUMNS} FROM articles a JOIN themes t ON t.id = a.theme_id "  # noqa: S608
            "WHERE a.theme_id = ? AND a.day = ?",
            (theme_id, day.isoformat()),
        )
        return _article(row) if row is not None else None

    def save_article(self, draft: ArticleDraft, items: Sequence[Item], *, force: bool) -> bool:
        """See :meth:`veille.contracts.Store.save_article`.

        Raises ValueError (nothing written) if a SourceLink references an item that
        is not in ``items``.
        """
        known = {item.canonical_url for item in items}
        for entry in draft.entries:
            for link in entry.source_links:
                if link.item_canonical_url not in known:
                    raise ValueError(
                        f"SourceLink references unknown item {link.item_canonical_url}"
                    )

        day = draft.day.isoformat()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT id FROM articles WHERE theme_id = ? AND day = ?",
                (draft.theme_id, day),
            ).fetchone()
            if existing is not None and not force:
                return False

            item_ids = {
                item.canonical_url: self._upsert_item(conn, draft.theme_id, item) for item in items
            }

            values = (
                draft.title,
                draft.body_md,
                draft.model,
                to_db_datetime(draft.generated_at),
                draft.entry_count,
                draft.status.value,
            )
            if existing is not None:
                article_id: int = existing[0]
                conn.execute("DELETE FROM article_entries WHERE article_id = ?", (article_id,))
                # UPDATE (not REPLACE) keeps the id and fires the FTS update trigger.
                conn.execute(
                    "UPDATE articles SET title = ?, body_md = ?, model = ?, generated_at = ?, "
                    "entry_count = ?, status = ? WHERE id = ?",
                    (*values, article_id),
                )
            else:
                article_id = conn.execute(
                    "INSERT INTO articles (theme_id, day, title, body_md, model, generated_at, "
                    "entry_count, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                    (draft.theme_id, day, *values),
                ).fetchone()[0]

            for position, entry in enumerate(draft.entries):
                entry_id = conn.execute(
                    "INSERT INTO article_entries (article_id, position, headline_fr, summary_fr) "
                    "VALUES (?, ?, ?, ?) RETURNING id",
                    (article_id, position, entry.headline_fr, entry.summary_fr),
                ).fetchone()[0]
                conn.executemany(
                    "INSERT OR IGNORE INTO entry_sources (entry_id, item_id, name, url) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (entry_id, item_ids[link.item_canonical_url], link.name, link.url)
                        for link in entry.source_links
                    ],
                )
        logger.info(
            "Saved article theme=%s day=%s entries=%s force=%s",
            draft.theme_id,
            day,
            draft.entry_count,
            force,
        )
        return True

    def list_articles_for_day(self, day: date) -> list[Article]:
        return self.list_articles(day=day)

    def list_articles(
        self, *, theme_id: int | None = None, day: date | None = None
    ) -> list[Article]:
        """Newest day first, then theme name."""
        clauses: list[str] = []
        params: list[Any] = []
        if theme_id is not None:
            clauses.append("a.theme_id = ?")
            params.append(theme_id)
        if day is not None:
            clauses.append("a.day = ?")
            params.append(day.isoformat())
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = self._all(
            f"SELECT {_ARTICLE_COLUMNS} FROM articles a JOIN themes t ON t.id = a.theme_id "  # noqa: S608
            f"{where}ORDER BY a.day DESC, t.name",
            params,
        )
        return [_article(r) for r in rows]

    def list_days(
        self, *, theme_id: int | None = None, limit: int = 30, offset: int = 0
    ) -> list[date]:
        where, params = ("WHERE theme_id = ? ", [theme_id]) if theme_id is not None else ("", [])
        rows = self._all(
            f"SELECT DISTINCT day FROM articles {where}ORDER BY day DESC LIMIT ? OFFSET ?",  # noqa: S608
            [*params, limit, offset],
        )
        return [date.fromisoformat(r[0]) for r in rows]

    def count_days(self, *, theme_id: int | None = None) -> int:
        where, params = ("WHERE theme_id = ?", [theme_id]) if theme_id is not None else ("", [])
        row = self._one(f"SELECT COUNT(DISTINCT day) FROM articles {where}", params)  # noqa: S608
        assert row is not None
        count: int = row[0]
        return count

    def adjacent_days(self, day: date) -> tuple[date | None, date | None]:
        key = day.isoformat()
        row = self._one(
            "SELECT (SELECT MAX(day) FROM articles WHERE day < ?), "
            "(SELECT MIN(day) FROM articles WHERE day > ?)",
            (key, key),
        )
        assert row is not None
        prev, nxt = row[0], row[1]
        return (
            date.fromisoformat(prev) if prev is not None else None,
            date.fromisoformat(nxt) if nxt is not None else None,
        )

    # ------------------------------------------------------------------- search

    def search(
        self, query: str, *, theme_id: int | None = None, limit: int = 20, offset: int = 0
    ) -> list[SearchHit]:
        match = fts_query(query)
        if match is None:
            return []
        sql = (
            f"SELECT {_ARTICLE_COLUMNS}, "  # noqa: S608
            "snippet(articles_fts, 1, ?, ?, '…', 16) AS snip, bm25(articles_fts) AS rank "
            "FROM articles_fts JOIN articles a ON a.id = articles_fts.rowid "
            "JOIN themes t ON t.id = a.theme_id WHERE articles_fts MATCH ?"
        )
        params: list[Any] = [HIGHLIGHT_START, HIGHLIGHT_END, match]
        if theme_id is not None:
            sql += " AND a.theme_id = ?"
            params.append(theme_id)
        sql += " ORDER BY rank, a.day DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        try:
            rows = self._all(sql, params)
        except sqlite3.OperationalError:
            logger.warning("FTS query failed for %r", query, exc_info=True)
            return []
        return [SearchHit(article=_article(r), snippet=r["snip"], rank=r["rank"]) for r in rows]

    def count_search(self, query: str, *, theme_id: int | None = None) -> int:
        match = fts_query(query)
        if match is None:
            return 0
        sql = (
            "SELECT COUNT(*) FROM articles_fts JOIN articles a ON a.id = articles_fts.rowid "
            "WHERE articles_fts MATCH ?"
        )
        params: list[Any] = [match]
        if theme_id is not None:
            sql += " AND a.theme_id = ?"
            params.append(theme_id)
        try:
            row = self._one(sql, params)
        except sqlite3.OperationalError:
            logger.warning("FTS count failed for %r", query, exc_info=True)
            return 0
        assert row is not None
        count: int = row[0]
        return count

    # --------------------------------------------------------------------- runs

    def start_run(self, trigger: RunTrigger, now: datetime) -> int | None:
        """Lock: None if a 'running' run started less than 3 h ago.

        Older 'running' runs are considered abandoned and marked 'error'.
        """
        threshold = to_db_datetime(now - RUN_LOCK_TTL)
        with self._tx() as conn:
            busy = conn.execute(
                "SELECT id FROM runs WHERE status = 'running' AND started_at > ? LIMIT 1",
                (threshold,),
            ).fetchone()
            if busy is not None:
                logger.info("Run %s still running, lock not acquired", busy[0])
                return None
            conn.execute(
                "UPDATE runs SET status = 'error', error = 'abandoned (stale lock)' "
                "WHERE status = 'running'"
            )
            run_id: int = conn.execute(
                "INSERT INTO runs (started_at, trigger, status) VALUES (?, ?, 'running') "
                "RETURNING id",
                (to_db_datetime(now), trigger.value),
            ).fetchone()[0]
        return run_id

    def finish_run(
        self,
        run_id: int,
        *,
        status: RunStatus,
        stats: RunStats,
        finished_at: datetime,
        error: str | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, stats_json = ?, finished_at = ?, error = ? "
                "WHERE id = ?",
                (
                    status.value,
                    json.dumps(stats.to_json_dict(), ensure_ascii=False),
                    to_db_datetime(finished_at),
                    error,
                    run_id,
                ),
            )

    def last_run(self) -> Run | None:
        runs = self.list_runs(limit=1)
        return runs[0] if runs else None

    def list_runs(self, limit: int = 20) -> list[Run]:
        rows = self._all("SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,))
        return [_run(r) for r in rows]

    # -------------------------------------------------------------------- misc

    def ping(self) -> bool:
        try:
            row = self._one("SELECT 1")
        except sqlite3.Error:
            return False
        return row is not None and row[0] == 1

    def close(self) -> None:
        with self._lock:
            self._conn.close()
