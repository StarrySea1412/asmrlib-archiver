from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from ..models import utc_now
from .base import DbConnection, TagPageCompletion


class TagPageRepo(DbConnection):
    """tag_pages + tag_items 表的访问与状态机。"""

    def list_tag_pages_for_discovery(
        self,
        limit: int,
        retry_errors: bool = False,
        exclude_page_urls: Iterable[str] = (),
    ) -> list[sqlite3.Row]:
        if limit < 0:
            raise ValueError("limit must not be negative")
        statuses = ["pending"]
        if retry_errors:
            statuses.extend(["error", "incomplete"])
        placeholders = ",".join("?" for _ in statuses)
        excluded = list(dict.fromkeys(exclude_page_urls))
        exclude_clause = ""
        parameters: list[object] = list(statuses)
        if excluded:
            excluded_placeholders = ",".join("?" for _ in excluded)
            exclude_clause = f"AND page_url NOT IN ({excluded_placeholders})"
            parameters.extend(excluded)
        parameters.append(limit)
        return list(
            self.conn.execute(
                f"""
                SELECT * FROM tag_pages
                WHERE status IN ({placeholders})
                  {exclude_clause}
                ORDER BY created_at ASC, rowid ASC
                LIMIT ?
                """,
                parameters,
            )
        )

    def complete_tag_page(
        self,
        tag_url: str,
        page_url: str,
        *,
        post_urls: Iterable[str],
        next_page_url: str = "",
        content_hash: str = "",
        html_path: str = "",
        status: str = "complete",
        enqueue_next: bool = True,
    ) -> TagPageCompletion:
        """Atomically archive discoveries, enqueue the next page, and finish this page."""
        if status not in {"complete", "incomplete"}:
            raise ValueError(f"Invalid tag page completion status: {status}")
        unique_posts = list(dict.fromkeys(url.strip() for url in post_urls if url.strip()))
        next_page_url = next_page_url.strip()
        content_hash = content_hash.strip()
        now = utc_now()

        with self.conn:
            current = self.conn.execute(
                """
                SELECT page_url FROM tag_pages
                WHERE tag_url = ? AND page_url = ?
                """,
                (tag_url, page_url),
            ).fetchone()
            if current is None:
                raise ValueError(f"Unknown tag page: {tag_url} -> {page_url}")

            if content_hash:
                duplicate = self.conn.execute(
                    """
                    SELECT page_url FROM tag_pages
                    WHERE tag_url = ? AND page_url <> ? AND content_hash = ?
                    ORDER BY created_at ASC, rowid ASC
                    LIMIT 1
                    """,
                    (tag_url, page_url, content_hash),
                ).fetchone()
                if duplicate is not None:
                    duplicate_of = str(duplicate["page_url"])
                    self.conn.execute(
                        """
                        UPDATE tag_pages
                        SET status = 'duplicate', content_hash = ?, next_page_url = '',
                            discovered_count = 0, html_path = ?, error = ?, updated_at = ?
                        WHERE tag_url = ? AND page_url = ?
                        """,
                        (
                            content_hash,
                            html_path,
                            f"duplicate_content:{duplicate_of}",
                            now,
                            tag_url,
                            page_url,
                        ),
                    )
                    return TagPageCompletion(duplicate_of=duplicate_of)

            items_added = 0
            memberships_added = 0
            for source_url in unique_posts:
                item_cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO items(source_url, status, created_at, updated_at)
                    VALUES (?, 'pending', ?, ?)
                    """,
                    (source_url, now, now),
                )
                items_added += int(item_cur.rowcount > 0)

                membership_cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO tag_items(
                      tag_url, source_url, discovered_from, first_seen_at, last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (tag_url, source_url, page_url, now, now),
                )
                membership_added = membership_cur.rowcount > 0
                memberships_added += int(membership_added)
                if not membership_added:
                    self.conn.execute(
                        """
                        UPDATE tag_items
                        SET last_seen_at = ?
                        WHERE tag_url = ? AND source_url = ?
                        """,
                        (now, tag_url, source_url),
                    )

            next_page_added = False
            if enqueue_next and next_page_url and next_page_url != page_url:
                next_cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO tag_pages(
                      tag_url, page_url, status, created_at, updated_at
                    )
                    VALUES (?, ?, 'pending', ?, ?)
                    """,
                    (tag_url, next_page_url, now, now),
                )
                next_page_added = next_cur.rowcount > 0

            self.conn.execute(
                """
                UPDATE tag_pages
                SET status = ?, content_hash = ?, next_page_url = ?,
                    discovered_count = ?, html_path = ?, error = '', updated_at = ?
                WHERE tag_url = ? AND page_url = ?
                """,
                (
                    status,
                    content_hash,
                    next_page_url,
                    len(unique_posts),
                    html_path,
                    now,
                    tag_url,
                    page_url,
                ),
            )

        return TagPageCompletion(
            items_added=items_added,
            memberships_added=memberships_added,
            next_page_added=next_page_added,
        )

    def mark_tag_page_failed(
        self,
        tag_url: str,
        page_url: str,
        *,
        status: str = "error",
        error: str,
    ) -> None:
        if status not in {"error", "blocked"}:
            raise ValueError(f"Invalid tag page failure status: {status}")
        cur = self.conn.execute(
            """
            UPDATE tag_pages
            SET status = ?, error = ?, updated_at = ?
            WHERE tag_url = ? AND page_url = ?
            """,
            (status, error, utc_now(), tag_url, page_url),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"Unknown tag page: {tag_url} -> {page_url}")

    def count_tag_pages(self, tag_url: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM tag_pages WHERE tag_url = ?",
            (tag_url,),
        ).fetchone()
        return int(row["count"])

    def has_tag_content_hash(
        self,
        tag_url: str,
        content_hash: str,
        *,
        exclude_page_url: str = "",
    ) -> bool:
        if not content_hash:
            return False
        if exclude_page_url:
            row = self.conn.execute(
                """
                SELECT 1 FROM tag_pages
                WHERE tag_url = ? AND content_hash = ? AND page_url <> ?
                LIMIT 1
                """,
                (tag_url, content_hash, exclude_page_url),
            ).fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT 1 FROM tag_pages
                WHERE tag_url = ? AND content_hash = ?
                LIMIT 1
                """,
                (tag_url, content_hash),
            ).fetchone()
        return row is not None

    def list_tag_pages(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM tag_pages ORDER BY created_at ASC, rowid ASC"
            )
        )

    def list_tag_items(self, tag_url: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT tag_items.*, items.title, items.status
                FROM tag_items
                JOIN items ON items.source_url = tag_items.source_url
                WHERE tag_items.tag_url = ?
                ORDER BY tag_items.first_seen_at ASC, tag_items.source_url ASC
                """,
                (tag_url,),
            )
        )

    def list_tags(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT tag_url, COUNT(*) AS post_count
                FROM tag_items
                GROUP BY tag_url
                ORDER BY post_count DESC, tag_url ASC
                """
            )
        )

    def add_user_tag(self, source_url: str, label: str) -> str:
        """Attach a user-authored tag to a post (``user-tag://<slug>``).

        Inserts the tag_pages stub the FK requires, then the membership row.
        Returns the tag_url used.
        """
        slug = _user_tag_slug(label)
        if not slug:
            raise ValueError("empty_tag_label")
        tag_url = f"user-tag://{slug}"
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO tag_pages(
                  tag_url, page_url, status, discovered_count, created_at, updated_at
                ) VALUES (?, 'user-added', 'reference', 0, ?, ?)
                """,
                (tag_url, now, now),
            )
            self.conn.execute(
                """
                INSERT OR IGNORE INTO tag_items(
                  tag_url, source_url, discovered_from, first_seen_at, last_seen_at
                ) VALUES (?, ?, 'user-added', ?, ?)
                """,
                (tag_url, source_url, now, now),
            )
        return tag_url

    def remove_user_tag(self, source_url: str, tag_url: str) -> bool:
        """Remove a user tag membership from a post. Returns True if removed."""
        if not tag_url.startswith("user-tag://"):
            return False
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM tag_items WHERE source_url = ? AND tag_url = ?",
                (source_url, tag_url),
            )
        return cur.rowcount > 0


def _user_tag_slug(label: str) -> str:
    """Sanitize a free-text tag into a URL-safe slug (keeps CJK)."""
    import re as _re

    value = (label or "").strip()
    if not value:
        return ""
    value = _re.sub(r"[\x00-\x1f<>:\"/\\|?*#]", "", value)
    value = _re.sub(r"\s+", " ", value).strip(" .")
    return value[:40].rstrip(" .")
