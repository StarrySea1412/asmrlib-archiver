from __future__ import annotations

from collections.abc import Iterable

from ..models import utc_now
from ..source_health import PlaybackOutcome, SourceHealth, health_key
from .base import DbConnection


class SourceHealthRepo(DbConnection):
    """source_health / source_preference：播放线路的实测健康与手动偏好。

    与 ``media_candidates`` 的 health_score 不同，这里记录的是**播放尝试**
    的结果，且以 URL 哈希为键，因此帖子里换一个显示名不会丢失历史。
    """

    def get_source_health(
        self, urls: Iterable[str]
    ) -> dict[str, SourceHealth]:
        """Load health records for ``urls``, keyed by ``health_key(url)``.

        Missing URLs are simply absent from the result; callers treat an
        absent record as "no history", which scores as neutral.
        """

        keys = list(dict.fromkeys(health_key(url) for url in urls if str(url or "").strip()))
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        rows = self.conn.execute(
            f"SELECT * FROM source_health WHERE source_key IN ({placeholders})",
            keys,
        )
        return {str(row["source_key"]): SourceHealth.from_row(row) for row in rows}

    def record_playback_outcome(
        self,
        url: str,
        outcome: PlaybackOutcome,
        latency_ms: int = -1,
        error: str = "",
    ) -> None:
        """Persist one real playback attempt.

        Only ever called once an attempt actually ran: ``PLAYING`` means the
        player document loaded and playback began, ``FAILED`` means it did
        not.  Selecting a source is not an outcome and records nothing.

        A failure also bumps ``consecutive_failures``, which is what puts the
        source into cooldown so the next attempt does not start there again.
        """

        raw = str(url or "").strip()
        if not raw:
            return
        key = health_key(raw)
        now = utc_now()
        with self.conn:
            # Two statements instead of one UPSERT: the counters have to be
            # expressed relative to the existing row, which is far clearer as
            # an UPDATE than as a pile of CASE expressions in the conflict
            # clause.
            self.conn.execute(
                """
                INSERT OR IGNORE INTO source_health(source_key, source_url, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, raw, now),
            )
            if outcome is PlaybackOutcome.PLAYING:
                self.conn.execute(
                    """
                    UPDATE source_health
                    SET success_count = success_count + 1,
                        consecutive_failures = 0,
                        last_success_at = ?,
                        -- Clearing the failure stamp is what lifts the "do not
                        -- start here" demotion: ordering treats a *recent*
                        -- failure as the reason to skip a source, and a source
                        -- that just played is demonstrably not that. The
                        -- lifetime failure_count is kept as history.
                        last_failure_at = '',
                        latency_ms = CASE WHEN ? >= 0 THEN ? ELSE latency_ms END,
                        last_error = '',
                        updated_at = ?
                    WHERE source_key = ?
                    """,
                    (now, int(latency_ms), int(latency_ms), now, key),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE source_health
                    SET failure_count = failure_count + 1,
                        consecutive_failures = consecutive_failures + 1,
                        last_failure_at = ?,
                        last_error = ?,
                        updated_at = ?
                    WHERE source_key = ?
                    """,
                    (now, str(error)[:300], now, key),
                )

    def get_source_preference(self, item_url: str) -> str:
        """The source URL the user manually chose for this post ('' if none)."""

        raw = str(item_url or "").strip()
        if not raw:
            return ""
        row = self.conn.execute(
            """
            SELECT source_url FROM source_preference
            WHERE item_url = ?
            ORDER BY chosen_at DESC
            LIMIT 1
            """,
            (raw,),
        ).fetchone()
        return str(row["source_url"] or "") if row is not None else ""

    def set_source_preference(self, item_url: str, source_url: str) -> None:
        """Remember a manual choice for one post, replacing any previous one."""

        item = str(item_url or "").strip()
        source = str(source_url or "").strip()
        if not item or not source:
            return
        now = utc_now()
        with self.conn:
            # One preferred source per post: a second choice replaces the
            # first rather than leaving two rows for the ordering to argue over.
            self.conn.execute(
                "DELETE FROM source_preference WHERE item_url = ?", (item,)
            )
            self.conn.execute(
                """
                INSERT INTO source_preference(item_url, source_key, source_url, chosen_at)
                VALUES (?, ?, ?, ?)
                """,
                (item, health_key(source), source, now),
            )

    def clear_source_preference(self, item_url: str) -> None:
        """Drop the manual choice so the computed score decides again."""

        item = str(item_url or "").strip()
        if not item:
            return
        with self.conn:
            self.conn.execute(
                "DELETE FROM source_preference WHERE item_url = ?", (item,)
            )
