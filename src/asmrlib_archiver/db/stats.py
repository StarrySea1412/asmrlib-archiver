from __future__ import annotations

from .base import DbConnection


class StatsRepo(DbConnection):
    """跨表统计与未决计数，供 CLI status / run 使用。"""

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in self.conn.execute("SELECT status, COUNT(*) AS count FROM items GROUP BY status"):
            result[f"items.{row['status']}"] = int(row["count"])
        for row in self.conn.execute(
            "SELECT status, COUNT(*) AS count FROM media_candidates GROUP BY status"
        ):
            result[f"media.{row['status']}"] = int(row["count"])
        for row in self.conn.execute(
            "SELECT status, COUNT(*) AS count FROM tag_pages GROUP BY status"
        ):
            result[f"tag_pages.{row['status']}"] = int(row["count"])
        result["tags.total"] = int(
            self.conn.execute("SELECT COUNT(DISTINCT tag_url) FROM tag_pages").fetchone()[0]
        )
        result["tag_items.total"] = int(
            self.conn.execute("SELECT COUNT(*) FROM tag_items").fetchone()[0]
        )
        return result

    def unresolved_counts(self) -> dict[str, int]:
        queries = {
            "tag_pages": (
                "SELECT COUNT(*) FROM tag_pages "
                "WHERE status IN ('pending', 'error', 'blocked', 'incomplete')"
            ),
            "items": (
                "SELECT COUNT(*) FROM items "
                "WHERE status IN ('pending', 'error', 'blocked')"
            ),
            "media": (
                "SELECT COUNT(*) FROM media_candidates "
                "WHERE status IN ('pending', 'download_error')"
            ),
        }
        return {
            bucket: int(self.conn.execute(query).fetchone()[0])
            for bucket, query in queries.items()
        }
