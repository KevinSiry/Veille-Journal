-- 001_init: initial schema. Timestamps are UTC ISO-8601 strings; `day` is YYYY-MM-DD (Europe/Paris).

CREATE TABLE themes (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    description   TEXT NOT NULL DEFAULT '',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE sources (
    id        INTEGER PRIMARY KEY,
    theme_id  INTEGER NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL DEFAULT 'rss' CHECK (kind IN ('rss')),
    url       TEXT NOT NULL,
    label     TEXT NOT NULL DEFAULT '',
    active    INTEGER NOT NULL DEFAULT 1,
    UNIQUE (theme_id, url)
);

CREATE TABLE items (
    id            INTEGER PRIMARY KEY,
    theme_id      INTEGER NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title         TEXT NOT NULL,
    published_at  TEXT,
    fetched_at    TEXT NOT NULL,
    source_name   TEXT NOT NULL DEFAULT '',
    origin        TEXT NOT NULL CHECK (origin IN ('rss', 'searxng')),
    content_text  TEXT NOT NULL DEFAULT '',
    content_hash  TEXT NOT NULL DEFAULT '',
    embedding     BLOB,
    relevance     REAL,
    status        TEXT NOT NULL DEFAULT 'new'
                  CHECK (status IN ('new', 'dropped', 'selected', 'used')),
    UNIQUE (theme_id, canonical_url)
);
CREATE INDEX idx_items_theme_fetched ON items (theme_id, fetched_at);

CREATE TABLE articles (
    id           INTEGER PRIMARY KEY,
    theme_id     INTEGER NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
    day          TEXT NOT NULL,
    title        TEXT NOT NULL,
    body_md      TEXT NOT NULL,
    model        TEXT NOT NULL DEFAULT '',
    generated_at TEXT NOT NULL,
    entry_count  INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL CHECK (status IN ('ok', 'empty', 'error')),
    UNIQUE (theme_id, day)
);
CREATE INDEX idx_articles_day ON articles (day);
CREATE INDEX idx_articles_theme_day ON articles (theme_id, day);

CREATE TABLE article_entries (
    id          INTEGER PRIMARY KEY,
    article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    headline_fr TEXT NOT NULL,
    summary_fr  TEXT NOT NULL
);
CREATE INDEX idx_article_entries_article ON article_entries (article_id);

CREATE TABLE entry_sources (
    entry_id INTEGER NOT NULL REFERENCES article_entries(id) ON DELETE CASCADE,
    item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    url      TEXT NOT NULL,
    PRIMARY KEY (entry_id, item_id)
);

CREATE TABLE runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    trigger     TEXT NOT NULL CHECK (trigger IN ('cron', 'cli')),
    status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'error')),
    stats_json  TEXT NOT NULL DEFAULT '{}',
    error       TEXT
);

CREATE VIRTUAL TABLE articles_fts USING fts5 (
    title,
    body_md,
    content = 'articles',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts (rowid, title, body_md) VALUES (new.id, new.title, new.body_md);
END;

CREATE TRIGGER articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts (articles_fts, rowid, title, body_md)
    VALUES ('delete', old.id, old.title, old.body_md);
END;

CREATE TRIGGER articles_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts (articles_fts, rowid, title, body_md)
    VALUES ('delete', old.id, old.title, old.body_md);
    INSERT INTO articles_fts (rowid, title, body_md) VALUES (new.id, new.title, new.body_md);
END;
