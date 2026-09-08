from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  source_url TEXT PRIMARY KEY,
  title TEXT DEFAULT '',
  author TEXT DEFAULT '',
  published_at TEXT DEFAULT '',
  cover TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  html_path TEXT DEFAULT '',
  metadata_path TEXT DEFAULT '',
  error TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_url TEXT NOT NULL,
  media_url TEXT NOT NULL,
  label TEXT DEFAULT '',
  kind TEXT DEFAULT 'unknown',
  status TEXT NOT NULL DEFAULT 'pending',
  file_path TEXT DEFAULT '',
  error TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(source_url, media_url),
  FOREIGN KEY(source_url) REFERENCES items(source_url)
);

CREATE TABLE IF NOT EXISTS tag_pages (
  tag_url TEXT NOT NULL,
  page_url TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  content_hash TEXT DEFAULT '',
  next_page_url TEXT DEFAULT '',
  discovered_count INTEGER NOT NULL DEFAULT 0,
  html_path TEXT DEFAULT '',
  error TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tag_url, page_url)
);

CREATE TABLE IF NOT EXISTS tag_items (
  tag_url TEXT NOT NULL,
  source_url TEXT NOT NULL,
  discovered_from TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY(tag_url, source_url),
  FOREIGN KEY(source_url) REFERENCES items(source_url) ON DELETE CASCADE,
  FOREIGN KEY(tag_url, discovered_from)
    REFERENCES tag_pages(tag_url, page_url) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_media_status ON media_candidates(status);
CREATE INDEX IF NOT EXISTS idx_tag_pages_status ON tag_pages(status);
CREATE INDEX IF NOT EXISTS idx_tag_pages_tag_status ON tag_pages(tag_url, status);
CREATE INDEX IF NOT EXISTS idx_tag_pages_content_hash
  ON tag_pages(tag_url, content_hash);
CREATE INDEX IF NOT EXISTS idx_tag_items_source_url ON tag_items(source_url);
"""


@dataclass(frozen=True)
class TagPageCompletion:
    items_added: int = 0
    memberships_added: int = 0
    next_page_added: bool = False
    duplicate_of: str = ""


class DbConnection:
    """共享 sqlite3.Connection 的基类。

    所有 repo 子类通过 ``self.conn`` 访问连接。连接由 ArchiveDb 在
    构造时创建并注入，repo 自身不负责连接生命周期。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def open(path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        # This single connection is shared across the HTTP server's request
        # threads and any background crawl/discovery thread the viewer spins
        # up (ArchiveViewer._kick_background_crawl /
        # start_background_tag_sync). Under the default rollback-journal
        # mode a writer holds an exclusive lock for its whole transaction,
        # so a multi-second background crawl write could make a concurrent
        # page read fail with "database is locked". WAL lets readers and one
        # writer proceed concurrently; busy_timeout makes SQLite retry for a
        # while instead of failing immediately when two writers do collide;
        # the connect-level timeout backs that up for the very first lock
        # acquisition before busy_timeout even takes effect.
        conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        # NORMAL is safe under WAL (only fsyncs at checkpoints) and avoids an
        # fsync on every commit, which matters given the per-row commits in
        # ItemRepo/MediaRepo's update paths.
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self._migrate_items_cover()
        self.conn.commit()

    def _migrate_items_cover(self) -> None:
        """Add the cover column to legacy items tables.

        CREATE TABLE IF NOT EXISTS won't add columns to an existing table, so
        existing databases need a one-shot ALTER. Idempotent: silently skips if
        the column already exists.
        """
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(items)")
        }
        if "cover" not in columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN cover TEXT DEFAULT ''")
