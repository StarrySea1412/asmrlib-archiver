from __future__ import annotations

import sqlite3

from ..models import utc_now
from .base import DbConnection


class ItemRepo(DbConnection):
    """items 表的查询与状态更新。"""

    def list_archive_items(self, limit: int | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM items ORDER BY created_at ASC, source_url ASC"
        if limit is None:
            return list(self.conn.execute(query))
        if limit < 0:
            raise ValueError("limit must not be negative")
        return list(self.conn.execute(f"{query} LIMIT ?", (limit,)))

    def list_items_for_crawl(
        self,
        limit: int,
        retry_errors: bool = False,
        *,
        refresh: bool = False,
    ) -> list[sqlite3.Row]:
        statuses = ["pending"]
        if retry_errors:
            statuses.extend(["error", "blocked", "no_media_found"])
        if refresh:
            statuses.extend(["archived", "crawled"])
        # Preserve order while de-duplicating when refresh + retry overlap.
        statuses = list(dict.fromkeys(statuses))
        placeholders = ",".join("?" for _ in statuses)
        return list(
            self.conn.execute(
                f"""
                SELECT * FROM items
                WHERE status IN ({placeholders})
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (*statuses, limit),
            )
        )

    def get_item(self, source_url: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM items WHERE source_url = ?",
            (source_url,),
        ).fetchone()

    def search_items(
        self,
        *,
        query: str = "",
        tag: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        if limit <= 0 or offset < 0:
            raise ValueError("limit must be positive and offset must not be negative")
        clauses: list[str] = ["1=1"]
        params: list[object] = []
        needle = query.strip()
        if needle:
            clauses.append(
                "(items.title LIKE ? OR items.source_url LIKE ? OR items.author LIKE ?)"
            )
            like = f"%{needle}%"
            params.extend([like, like, like])
        tag_value = tag.strip()
        join_sql = ""
        if tag_value:
            join_sql = "JOIN tag_items ON tag_items.source_url = items.source_url"
            clauses.append("(tag_items.tag_url = ? OR tag_items.tag_url LIKE ?)")
            params.extend([tag_value, f"%/tags/{tag_value}"])
        where_sql = " AND ".join(clauses)
        params.extend([limit, offset])
        return list(
            self.conn.execute(
                f"""
                SELECT DISTINCT items.*
                FROM items
                {join_sql}
                WHERE {where_sql}
                ORDER BY
                  CASE WHEN items.published_at = '' THEN 1 ELSE 0 END,
                  items.published_at DESC,
                  items.updated_at DESC,
                  items.source_url ASC
                LIMIT ? OFFSET ?
                """,
                params,
            )
        )

    def count_items(self, *, query: str = "", tag: str = "") -> int:
        clauses: list[str] = ["1=1"]
        params: list[object] = []
        needle = query.strip()
        if needle:
            clauses.append(
                "(items.title LIKE ? OR items.source_url LIKE ? OR items.author LIKE ?)"
            )
            like = f"%{needle}%"
            params.extend([like, like, like])
        tag_value = tag.strip()
        join_sql = ""
        if tag_value:
            join_sql = "JOIN tag_items ON tag_items.source_url = items.source_url"
            clauses.append("(tag_items.tag_url = ? OR tag_items.tag_url LIKE ?)")
            params.extend([tag_value, f"%/tags/{tag_value}"])
        where_sql = " AND ".join(clauses)
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS count FROM (
              SELECT DISTINCT items.source_url
              FROM items
              {join_sql}
              WHERE {where_sql}
            )
            """,
            params,
        ).fetchone()
        return int(row["count"])

    def update_item_result(
        self,
        source_url: str,
        *,
        title: str = "",
        author: str = "",
        published_at: str = "",
        cover: str = "",
        status: str,
        html_path: str = "",
        metadata_path: str = "",
        error: str = "",
    ) -> None:
        self.conn.execute(
            """
            UPDATE items
            SET title = ?, author = ?, published_at = ?, cover = ?, status = ?,
                html_path = ?, metadata_path = ?, error = ?, updated_at = ?
            WHERE source_url = ?
            """,
            (
                title,
                author,
                published_at,
                cover,
                status,
                html_path,
                metadata_path,
                error,
                utc_now(),
                source_url,
            ),
        )
        self.conn.commit()

    def find_items_by_title_or_url(self, needle: str, limit: int = 20) -> list[sqlite3.Row]:
        value = needle.strip()
        if not value:
            return []
        if limit <= 0:
            raise ValueError("limit must be positive")
        like = f"%{value}%"
        return list(
            self.conn.execute(
                """
                SELECT * FROM items
                WHERE source_url = ?
                   OR source_url LIKE ?
                   OR title LIKE ?
                ORDER BY
                  CASE WHEN source_url = ? THEN 0 ELSE 1 END,
                  updated_at DESC,
                  source_url ASC
                LIMIT ?
                """,
                (value, like, like, value, limit),
            )
        )

    def list_items_without_cover(self, limit: int) -> list[sqlite3.Row]:
        """Items that have never had a cover extracted, oldest first."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        return list(
            self.conn.execute(
                """
                SELECT * FROM items
                WHERE cover IS NULL OR cover = ''
                ORDER BY created_at ASC, source_url ASC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def update_item_cover(self, source_url: str, cover: str) -> None:
        """Backfill only the cover column (covers-only crawl path)."""
        self.conn.execute(
            "UPDATE items SET cover = ?, updated_at = ? WHERE source_url = ?",
            (cover, utc_now(), source_url),
        )
        self.conn.commit()

    def list_authors(self, limit: int = 30) -> list[sqlite3.Row]:
        """Authors ranked by archived post count (non-empty author only)."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        return list(
            self.conn.execute(
                """
                SELECT author, COUNT(*) AS post_count
                FROM items
                WHERE author IS NOT NULL AND TRIM(author) <> ''
                GROUP BY author
                ORDER BY post_count DESC, author ASC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def list_items_by_author(
        self,
        author: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        if limit <= 0 or offset < 0:
            raise ValueError("limit must be positive and offset must not be negative")
        needle = author.strip()
        if not needle:
            return []
        return list(
            self.conn.execute(
                """
                SELECT * FROM items
                WHERE author = ? OR author LIKE ?
                ORDER BY
                  CASE WHEN published_at = '' THEN 1 ELSE 0 END,
                  published_at DESC,
                  updated_at DESC,
                  source_url ASC
                LIMIT ? OFFSET ?
                """,
                (needle, f"%{needle}%", limit, offset),
            )
        )

    def count_items_by_author(self, author: str) -> int:
        needle = author.strip()
        if not needle:
            return 0
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS count FROM items
            WHERE author = ? OR author LIKE ?
            """,
            (needle, f"%{needle}%"),
        ).fetchone()
        return int(row["count"])

    def list_related_items(self, source_url: str, limit: int = 12) -> list[sqlite3.Row]:
        """Other archived posts that share at least one tag with ``source_url``."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        return list(
            self.conn.execute(
                """
                SELECT items.*, COUNT(DISTINCT shared.tag_url) AS shared_tags
                FROM tag_items AS mine
                JOIN tag_items AS shared
                  ON shared.tag_url = mine.tag_url
                 AND shared.source_url <> mine.source_url
                JOIN items ON items.source_url = shared.source_url
                WHERE mine.source_url = ?
                GROUP BY items.source_url
                ORDER BY shared_tags DESC,
                  CASE WHEN items.published_at = '' THEN 1 ELSE 0 END,
                  items.published_at DESC,
                  items.updated_at DESC
                LIMIT ?
                """,
                (source_url, limit),
            )
        )
