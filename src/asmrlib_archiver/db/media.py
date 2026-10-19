from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from ..models import MediaCandidate, utc_now
from .base import DbConnection


class MediaRepo(DbConnection):
    """media_candidates 表的查询、下载状态与本地文件挂载。"""

    def list_media_for_item(self, source_url: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT * FROM media_candidates
                WHERE source_url = ?
                ORDER BY
                  CASE status
                    WHEN 'downloaded' THEN 0
                    WHEN 'pending' THEN 1
                    WHEN 'reference' THEN 2
                    ELSE 3
                  END,
                  health_score DESC,
                  CASE WHEN last_checked_at <> '' THEN last_checked_at END DESC,
                  id ASC
                """,
                (source_url,),
            )
        )

    def list_media_urls(self) -> list[str]:
        """Every media URL the archive has already recorded.

        Drives the allowlist for the "save to library" endpoint: only hosts
        the archive already references may be fetched, so the endpoint can
        never be pointed at the LAN or an arbitrary internet host.
        """
        rows = self.conn.execute(
            "SELECT media_url FROM media_candidates "
            "WHERE media_url IS NOT NULL AND media_url != ''"
        ).fetchall()
        return [str(row[0]) for row in rows if row[0]]

    def record_media_health(
        self, source_url: str, media_url: str, *, ok: bool
    ) -> None:
        """Persist one validated playback/download result for a candidate.

        A successful direct-media save is strong evidence that this route works
        on the current network. A rejected HTML response or a transport error
        lowers its score, so future desktop source selection tries healthier
        candidates first instead of repeating a known-bad first URL.
        """
        now = utc_now()
        with self.conn:
            if ok:
                self.conn.execute(
                    """
                    UPDATE media_candidates
                    SET success_count = success_count + 1,
                        health_score = MIN(100, health_score * 0.70 + 30),
                        last_checked_at = ?, updated_at = ?
                    WHERE source_url = ? AND media_url = ?
                    """,
                    (now, now, source_url, media_url),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE media_candidates
                    SET failure_count = failure_count + 1,
                        health_score = MAX(-100, health_score * 0.70 - 35),
                        last_checked_at = ?, updated_at = ?
                    WHERE source_url = ? AND media_url = ?
                    """,
                    (now, now, source_url, media_url),
                )

    def media_counts_for_sources(
        self, source_urls: Iterable[str]
    ) -> dict[str, dict[str, int]]:
        """Return local/reference/playable counts for several posts at once.

        Card grids frequently render the same set of posts in multiple rails.
        Keeping this aggregation in SQLite avoids one ``list_media_for_item``
        query per card while preserving the existing media status semantics.
        """
        urls = list(dict.fromkeys(str(url) for url in source_urls if str(url)))
        if not urls:
            return {}
        placeholders = ",".join("?" for _ in urls)
        rows = self.conn.execute(
            f"""
            SELECT source_url,
              SUM(CASE WHEN status = 'downloaded' AND file_path <> '' THEN 1 ELSE 0 END) AS local_count,
              SUM(CASE WHEN status <> 'downloaded' OR file_path = '' THEN 1 ELSE 0 END) AS reference_count,
              SUM(CASE WHEN status = 'downloaded' AND file_path <> '' THEN 1 ELSE 0 END) AS playable_count
            FROM media_candidates
            WHERE source_url IN ({placeholders})
            GROUP BY source_url
            """,
            urls,
        )
        result = {
            url: {"local": 0, "references": 0, "playable": 0} for url in urls
        }
        for row in rows:
            result[str(row["source_url"])] = {
                "local": int(row["local_count"] or 0),
                "references": int(row["reference_count"] or 0),
                "playable": int(row["playable_count"] or 0),
            }
        return result

    def list_playable_media(self, limit: int = 100) -> list[sqlite3.Row]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        return list(
            self.conn.execute(
                """
                SELECT media_candidates.*, items.title
                FROM media_candidates
                JOIN items ON items.source_url = media_candidates.source_url
                WHERE media_candidates.status = 'downloaded'
                  AND media_candidates.file_path <> ''
                ORDER BY media_candidates.updated_at DESC, media_candidates.id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def list_local_media(self) -> list[sqlite3.Row]:
        """All locally-owned media files (recordings + imports), newest first."""
        return list(
            self.conn.execute(
                """
                SELECT media_candidates.*, items.title, items.source_url AS item_url,
                       items.cover AS item_cover
                FROM media_candidates
                JOIN items ON items.source_url = media_candidates.source_url
                WHERE media_candidates.status = 'downloaded'
                  AND media_candidates.file_path <> ''
                ORDER BY media_candidates.updated_at DESC, media_candidates.id DESC
                """
            )
        )

    def delete_media_row(self, media_id: int) -> str:
        """Delete a media_candidates row; returns its stored file_path ('' if none)."""
        row = self.conn.execute(
            "SELECT file_path FROM media_candidates WHERE id = ?", (media_id,)
        ).fetchone()
        if row is None:
            return ""
        with self.conn:
            self.conn.execute("DELETE FROM media_candidates WHERE id = ?", (media_id,))
        return str(row["file_path"] or "")

    def relink_media_file(self, media_id: int, file_path: str) -> bool:
        """Point an existing record at a file that is already on disk.

        The repair path for a record whose file was moved or renamed: the row
        keeps its identity (and its label) while the path is corrected. The
        caller is responsible for the file existing; this only refuses a path
        that is not a regular file so a record can never be "repaired" onto
        nothing. Returns False when the record no longer exists.
        """
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"local_media_missing: {path}")
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE media_candidates
                SET file_path = ?, status = 'downloaded', error = '', updated_at = ?
                WHERE id = ?
                """,
                (str(path), utc_now(), media_id),
            )
        return cursor.rowcount > 0

    def rename_media_label(self, media_id: int, label: str) -> None:
        """Set a human-readable label on a media row (e.g. '第1集 在线录制')."""
        with self.conn:
            self.conn.execute(
                "UPDATE media_candidates SET label = ?, updated_at = ? WHERE id = ?",
                (label[:200], utc_now(), media_id),
            )

    def replace_media_candidates(self, source_url: str, media: Iterable[MediaCandidate]) -> None:
        candidates = {candidate.media_url: candidate for candidate in media}
        now = utc_now()
        with self.conn:
            if candidates:
                placeholders = ",".join("?" for _ in candidates)
                self.conn.execute(
                    f"""
                    DELETE FROM media_candidates
                    WHERE source_url = ? AND media_url NOT IN ({placeholders})
                    """,
                    (source_url, *candidates),
                )
            else:
                self.conn.execute(
                    "DELETE FROM media_candidates WHERE source_url = ?",
                    (source_url,),
                )

            for candidate in candidates.values():
                self.conn.execute(
                    """
                    INSERT INTO media_candidates(
                      source_url, media_url, label, kind, status, error, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_url, media_url) DO UPDATE SET
                      label = excluded.label,
                      kind = excluded.kind,
                      status = CASE
                        WHEN media_candidates.status = 'downloaded'
                          AND media_candidates.file_path <> ''
                        THEN media_candidates.status
                        ELSE excluded.status
                      END,
                      error = CASE
                        WHEN media_candidates.status = 'downloaded'
                          AND media_candidates.file_path <> ''
                        THEN media_candidates.error
                        ELSE excluded.error
                      END,
                      updated_at = excluded.updated_at
                    """,
                    (
                        source_url,
                        candidate.media_url,
                        candidate.label,
                        candidate.kind,
                        candidate.status,
                        candidate.error,
                        now,
                        now,
                    ),
                )

    def list_media_for_download(self, limit: int, retry_errors: bool = False) -> list[sqlite3.Row]:
        statuses = ["pending"]
        if retry_errors:
            statuses.append("download_error")
        placeholders = ",".join("?" for _ in statuses)
        return list(
            self.conn.execute(
                f"""
                SELECT media_candidates.*, items.title
                FROM media_candidates
                JOIN items ON items.source_url = media_candidates.source_url
                WHERE media_candidates.status IN ({placeholders})
                  AND media_candidates.kind NOT IN ('embed', 'iframe')
                ORDER BY media_candidates.created_at ASC
                LIMIT ?
                """,
                (*statuses, limit),
            )
        )

    def update_media_result(
        self,
        media_id: int,
        *,
        status: str,
        file_path: str = "",
        error: str = "",
    ) -> None:
        self.conn.execute(
            """
            UPDATE media_candidates
            SET status = ?, file_path = ?, error = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, file_path, error, utc_now(), media_id),
        )
        self.conn.commit()

    def attach_local_media(
        self,
        source_url: str,
        *,
        file_path: str,
        label: str = "Local file",
        media_url: str = "",
        kind: str = "local",
    ) -> int:
        """Attach an already-owned local file so the library can play it.

        Does not fetch remote content. The file must already exist on disk.
        """
        item = self.conn.execute(
            "SELECT 1 FROM items WHERE source_url = ?",
            (source_url,),
        ).fetchone()
        if item is None:
            raise ValueError(f"unknown_source_url: {source_url}")
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"local_media_missing: {path}")

        now = utc_now()
        synthetic_url = media_url.strip() or f"file://local/{path.name}"
        with self.conn:
            existing = self.conn.execute(
                """
                SELECT id FROM media_candidates
                WHERE source_url = ? AND media_url = ?
                """,
                (source_url, synthetic_url),
            ).fetchone()
            if existing is not None:
                media_id = int(existing["id"])
                self.conn.execute(
                    """
                    UPDATE media_candidates
                    SET label = ?, kind = ?, status = 'downloaded',
                        file_path = ?, error = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (label[:200], kind[:80], str(path), now, media_id),
                )
                return media_id

            cur = self.conn.execute(
                """
                INSERT INTO media_candidates(
                  source_url, media_url, label, kind, status,
                  file_path, error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'downloaded', ?, '', ?, ?)
                """,
                (
                    source_url,
                    synthetic_url,
                    label[:200],
                    kind[:80],
                    str(path),
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)
