"""SQLite persistence so repeated runs accumulate articles without duplicates."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Article

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id          TEXT PRIMARY KEY,          -- sha1 of normalised URL
    url         TEXT NOT NULL,
    title       TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    outlet      TEXT NOT NULL,
    published   TEXT,                      -- ISO 8601 UTC, may be NULL
    first_seen  TEXT NOT NULL,             -- ISO 8601 UTC, when we first fetched it
    kind        TEXT NOT NULL DEFAULT 'news'  -- news | govuk | written_statement
);
CREATE TABLE IF NOT EXISTS article_departments (
    article_id  TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    department  TEXT NOT NULL,
    score       INTEGER NOT NULL,
    PRIMARY KEY (article_id, department)
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published);
CREATE INDEX IF NOT EXISTS idx_ad_department ON article_departments(department);
"""


class Store:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring databases created by older versions up to the current schema."""
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(articles)")}
        if "kind" not in columns:
            self.conn.execute("ALTER TABLE articles ADD COLUMN kind TEXT NOT NULL DEFAULT 'news'")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def save(self, articles: list[Article]) -> int:
        """Insert new articles and (re)write their department tags.

        Existing articles keep their original first_seen time; their department
        tags are refreshed, so re-running after tuning keywords takes effect.
        Returns the number of articles that were new.
        """
        now = datetime.now(timezone.utc).isoformat()
        new = 0
        with self.conn:  # single transaction
            for art in articles:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO articles (id, url, title, summary, outlet, published, first_seen, kind)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (art.id, art.url, art.title, art.summary, art.outlet,
                     art.published.isoformat() if art.published else None, now, art.kind),
                )
                new += cur.rowcount
                self.conn.execute("DELETE FROM article_departments WHERE article_id = ?", (art.id,))
                self.conn.executemany(
                    "INSERT INTO article_departments (article_id, department, score) VALUES (?, ?, ?)",
                    [(art.id, slug, score) for slug, score in art.departments.items()],
                )
        return new

    def recent(self, hours: int = 24, department: str | None = None) -> list[Article]:
        """Articles published (or, if undated, first seen) in the last `hours`.

        Sorted newest first, each with its full department mapping.
        """
        since_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
        since, since_day = since_dt.isoformat(), since_dt.date().isoformat()
        # Written statements only carry a date (stored as midnight), so they are
        # compared by day: one made on the window's first day is included.
        sql = """
            SELECT a.id, a.url, a.title, a.summary, a.outlet, a.published, a.kind,
                   ad.department, ad.score
            FROM articles a
            JOIN article_departments ad ON ad.article_id = a.id
            WHERE CASE WHEN a.kind = 'written_statement' AND a.published IS NOT NULL
                       THEN substr(a.published, 1, 10) >= ?
                       ELSE COALESCE(a.published, a.first_seen) >= ? END
        """
        params: list = [since_day, since]
        if department:
            sql += " AND a.id IN (SELECT article_id FROM article_departments WHERE department = ?)"
            params.append(department)
        sql += " ORDER BY COALESCE(a.published, a.first_seen) DESC"

        by_id: dict[str, Article] = {}
        for _id, url, title, summary, outlet, published, kind, dept, score in self.conn.execute(sql, params):
            art = by_id.get(_id)
            if art is None:
                art = by_id[_id] = Article(
                    url=url, title=title, outlet=outlet, summary=summary,
                    published=datetime.fromisoformat(published) if published else None,
                    kind=kind,
                )
            art.departments[dept] = score
        return list(by_id.values())

    def news_articles(self) -> list[Article]:
        """Every stored news article (not official items), for re-classification."""
        rows = self.conn.execute(
            "SELECT url, title, summary, outlet, published FROM articles WHERE kind = 'news'")
        return [Article(url=u, title=t, summary=sm, outlet=o,
                        published=datetime.fromisoformat(p) if p else None)
                for u, t, sm, o, p in rows]

    def prune(self, days: int) -> int:
        """Delete articles first seen more than `days` ago. Returns rows removed."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.conn:
            return self.conn.execute("DELETE FROM articles WHERE first_seen < ?", (cutoff,)).rowcount
