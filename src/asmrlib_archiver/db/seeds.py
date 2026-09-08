from __future__ import annotations

from collections.abc import Iterable

from ..models import utc_now
from .base import DbConnection


class SeedRepo(DbConnection):
    """种子入库：posts 直接作为 items 待抓，tags 入 tag_pages 待发现。"""

    def add_seed(self, url: str) -> bool:
        now = utc_now()
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO items(source_url, status, created_at, updated_at)
            VALUES (?, 'pending', ?, ?)
            """,
            (url, now, now),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def add_seeds(self, urls: Iterable[str]) -> int:
        count = 0
        for url in urls:
            if self.add_seed(url):
                count += 1
        return count

    def add_tag_seed(self, tag_url: str) -> bool:
        """Enqueue the first page of a tag without resetting existing progress."""
        now = utc_now()
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO tag_pages(
              tag_url, page_url, status, created_at, updated_at
            )
            VALUES (?, ?, 'pending', ?, ?)
            """,
            (tag_url, tag_url, now, now),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def add_tag_seeds(self, tag_urls: Iterable[str]) -> int:
        now = utc_now()
        count = 0
        with self.conn:
            for tag_url in dict.fromkeys(tag_urls):
                cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO tag_pages(
                      tag_url, page_url, status, created_at, updated_at
                    )
                    VALUES (?, ?, 'pending', ?, ?)
                    """,
                    (tag_url, tag_url, now, now),
                )
                count += int(cur.rowcount > 0)
        return count
