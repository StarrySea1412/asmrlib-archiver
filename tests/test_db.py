from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from asmrlib_archiver.db import ArchiveDb
from asmrlib_archiver.models import MediaCandidate


class ArchiveDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = ArchiveDb(Path(self.temp_dir.name) / "archive.sqlite3")
        self.db.init()

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_foreign_keys_are_enabled(self) -> None:
        enabled = self.db.conn.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(enabled, 1)

    def test_tag_seed_queue_is_idempotent_and_counted(self) -> None:
        tag_url = "https://asmrlib.com/tags/yoonying"

        self.assertTrue(self.db.add_tag_seed(tag_url))
        self.assertFalse(self.db.add_tag_seed(tag_url))
        self.assertEqual(self.db.count_tag_pages(tag_url), 1)

        queued = self.db.list_tag_pages_for_discovery(limit=10)
        self.assertEqual([row["page_url"] for row in queued], [tag_url])
        self.assertEqual(self.db.counts()["tag_pages.pending"], 1)
        self.assertEqual(self.db.counts()["tags.total"], 1)

    def test_complete_tag_page_atomically_adds_posts_memberships_and_next_page(self) -> None:
        tag_url = "https://asmrlib.com/tags/yoonying"
        next_url = f"{tag_url}?page=2"
        first_post = "https://asmrlib.com/posts/11111111111111111111111111111111"
        second_post = "https://asmrlib.com/posts/22222222222222222222222222222222"
        self.db.add_tag_seed(tag_url)

        result = self.db.complete_tag_page(
            tag_url,
            tag_url,
            post_urls=[first_post, first_post, second_post],
            next_page_url=next_url,
            content_hash="hash-page-1",
            html_path="data/html/tag-1.html",
        )

        self.assertEqual(result.items_added, 2)
        self.assertEqual(result.memberships_added, 2)
        self.assertTrue(result.next_page_added)
        self.assertEqual(result.duplicate_of, "")
        self.assertEqual(
            [row["source_url"] for row in self.db.list_archive_items()],
            [first_post, second_post],
        )
        self.assertEqual(len(self.db.list_tag_items(tag_url)), 2)
        self.assertEqual(self.db.count_tag_pages(tag_url), 2)
        self.assertTrue(self.db.has_tag_content_hash(tag_url, "hash-page-1"))
        self.assertFalse(
            self.db.has_tag_content_hash(
                tag_url,
                "hash-page-1",
                exclude_page_url=tag_url,
            )
        )

        pages = {
            row["page_url"]: row
            for row in self.db.conn.execute("SELECT * FROM tag_pages WHERE tag_url = ?", (tag_url,))
        }
        self.assertEqual(pages[tag_url]["status"], "complete")
        self.assertEqual(pages[tag_url]["discovered_count"], 2)
        self.assertEqual(pages[next_url]["status"], "pending")

        repeated = self.db.complete_tag_page(
            tag_url,
            tag_url,
            post_urls=[first_post, second_post],
            next_page_url=next_url,
            content_hash="hash-page-1",
        )
        self.assertEqual(repeated.items_added, 0)
        self.assertEqual(repeated.memberships_added, 0)
        self.assertFalse(repeated.next_page_added)

    def test_incomplete_tag_page_is_persisted_without_losing_next_page(self) -> None:
        tag_url = "https://asmrlib.com/tags/yoonying"
        next_url = f"{tag_url}?page=2"
        self.db.add_tag_seed(tag_url)

        result = self.db.complete_tag_page(
            tag_url,
            tag_url,
            post_urls=[],
            next_page_url=next_url,
            status="incomplete",
        )

        self.assertTrue(result.next_page_added)
        current = self.db.conn.execute(
            "SELECT * FROM tag_pages WHERE tag_url = ? AND page_url = ?",
            (tag_url, tag_url),
        ).fetchone()
        self.assertEqual(current["status"], "incomplete")
        self.assertEqual(current["next_page_url"], next_url)
        queued = self.db.list_tag_pages_for_discovery(limit=10)
        self.assertEqual([row["page_url"] for row in queued], [next_url])
        self.assertEqual(self.db.counts()["tag_pages.incomplete"], 1)

        with self.assertRaises(ValueError):
            self.db.complete_tag_page(
                tag_url,
                tag_url,
                post_urls=[],
                status="not-a-status",
            )

    def test_duplicate_page_content_stops_pagination(self) -> None:
        tag_url = "https://asmrlib.com/tags/yoonying"
        second_page = f"{tag_url}?page=2"
        third_page = f"{tag_url}?page=3"
        first_post = "https://asmrlib.com/posts/11111111111111111111111111111111"
        ignored_post = "https://asmrlib.com/posts/33333333333333333333333333333333"
        self.db.add_tag_seed(tag_url)
        self.db.complete_tag_page(
            tag_url,
            tag_url,
            post_urls=[first_post],
            next_page_url=second_page,
            content_hash="same-content",
        )

        result = self.db.complete_tag_page(
            tag_url,
            second_page,
            post_urls=[ignored_post],
            next_page_url=third_page,
            content_hash="same-content",
        )

        self.assertEqual(result.duplicate_of, tag_url)
        self.assertEqual([row["source_url"] for row in self.db.list_archive_items()], [first_post])
        self.assertEqual(self.db.count_tag_pages(tag_url), 2)
        duplicate = self.db.conn.execute(
            "SELECT * FROM tag_pages WHERE tag_url = ? AND page_url = ?",
            (tag_url, second_page),
        ).fetchone()
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertIn(tag_url, duplicate["error"])

    def test_posts_are_global_but_memberships_and_hashes_are_scoped_per_tag(self) -> None:
        first_tag = "https://asmrlib.com/tags/yoonying"
        second_tag = "https://asmrlib.com/tags/asmr"
        shared_post = "https://asmrlib.com/posts/11111111111111111111111111111111"
        self.db.add_tag_seeds([first_tag, second_tag])

        first = self.db.complete_tag_page(
            first_tag,
            first_tag,
            post_urls=[shared_post],
            content_hash="shared-hash",
        )
        second = self.db.complete_tag_page(
            second_tag,
            second_tag,
            post_urls=[shared_post],
            content_hash="shared-hash",
        )

        self.assertEqual(first.items_added, 1)
        self.assertEqual(second.items_added, 0)
        self.assertEqual(first.memberships_added, 1)
        self.assertEqual(second.memberships_added, 1)
        self.assertEqual(len(self.db.list_archive_items()), 1)
        self.assertEqual(len(self.db.list_tag_items(first_tag)), 1)
        self.assertEqual(len(self.db.list_tag_items(second_tag)), 1)
        self.assertEqual(second.duplicate_of, "")

    def test_tag_failure_status_and_retry_filter(self) -> None:
        error_tag = "https://asmrlib.com/tags/error"
        blocked_tag = "https://asmrlib.com/tags/blocked"
        self.db.add_tag_seeds([error_tag, blocked_tag, error_tag])

        self.db.mark_tag_page_failed(error_tag, error_tag, error="timeout")
        self.db.mark_tag_page_failed(
            blocked_tag,
            blocked_tag,
            status="blocked",
            error="robots_disallow",
        )

        self.assertEqual(self.db.list_tag_pages_for_discovery(limit=10), [])
        retry = self.db.list_tag_pages_for_discovery(limit=10, retry_errors=True)
        self.assertEqual([row["tag_url"] for row in retry], [error_tag])
        with self.assertRaises(ValueError):
            self.db.mark_tag_page_failed(error_tag, error_tag, status="complete", error="bad")

    def test_unresolved_counts_include_historical_failures_and_pending_work(self) -> None:
        tag_url = "https://asmrlib.com/tags/yoonying"
        post_url = "https://asmrlib.com/posts/11111111111111111111111111111111"
        self.db.add_tag_seed(tag_url)
        self.db.add_seed(post_url)

        self.assertEqual(
            self.db.unresolved_counts(),
            {"tag_pages": 1, "items": 1, "media": 0},
        )
        self.db.mark_tag_page_failed(tag_url, tag_url, status="blocked", error="blocked")
        self.db.update_item_result(post_url, status="error", error="failed")
        self.assertEqual(
            self.db.unresolved_counts(),
            {"tag_pages": 1, "items": 1, "media": 0},
        )

    def test_complete_tag_page_rolls_back_all_writes_on_failure(self) -> None:
        tag_url = "https://asmrlib.com/tags/yoonying"
        first_post = "https://asmrlib.com/posts/11111111111111111111111111111111"
        rejected_post = "https://asmrlib.com/posts/ffffffffffffffffffffffffffffffff"
        self.db.add_tag_seed(tag_url)
        self.db.conn.execute(
            f"""
            CREATE TRIGGER reject_test_post
            BEFORE INSERT ON tag_items
            WHEN NEW.source_url = '{rejected_post}'
            BEGIN
              SELECT RAISE(ABORT, 'test rejection');
            END;
            """
        )
        self.db.conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.complete_tag_page(
                tag_url,
                tag_url,
                post_urls=[first_post, rejected_post],
                next_page_url=f"{tag_url}?page=2",
                content_hash="hash-page-1",
            )

        self.assertEqual(self.db.list_archive_items(), [])
        self.assertEqual(self.db.count_tag_pages(tag_url), 1)
        page = self.db.list_tag_pages_for_discovery(limit=1)[0]
        self.assertEqual(page["status"], "pending")

    def test_replace_media_candidates_removes_stale_rows_and_updates_status(self) -> None:
        source_url = "https://asmrlib.com/posts/11111111111111111111111111111111"
        kept_url = "https://asmrlib.com/media/kept.mp4"
        stale_url = "https://asmrlib.com/media/stale.mp4"
        self.db.add_seed(source_url)
        self.db.replace_media_candidates(
            source_url,
            [
                MediaCandidate(source_url, kept_url, label="old", status="pending"),
                MediaCandidate(source_url, stale_url, status="pending"),
            ],
        )

        self.db.replace_media_candidates(
            source_url,
            [MediaCandidate(source_url, kept_url, label="new", status="blocked", error="ad")],
        )

        rows = list(
            self.db.conn.execute(
                "SELECT * FROM media_candidates WHERE source_url = ?",
                (source_url,),
            )
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["media_url"], kept_url)
        self.assertEqual(rows[0]["label"], "new")
        self.assertEqual(rows[0]["status"], "blocked")
        self.assertEqual(rows[0]["error"], "ad")

        self.db.replace_media_candidates(source_url, [])
        remaining = self.db.conn.execute(
            "SELECT COUNT(*) FROM media_candidates WHERE source_url = ?",
            (source_url,),
        ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_replace_media_preserves_downloaded_files(self) -> None:
        source_url = "https://asmrlib.com/posts/11111111111111111111111111111111"
        media_url = "https://asmrlib.com/media/kept.mp4"
        self.db.add_seed(source_url)
        self.db.replace_media_candidates(
            source_url,
            [MediaCandidate(source_url, media_url, label="old", status="pending")],
        )
        media_id = self.db.conn.execute(
            "SELECT id FROM media_candidates WHERE media_url = ?",
            (media_url,),
        ).fetchone()["id"]
        self.db.update_media_result(
            media_id,
            status="downloaded",
            file_path="D:/archive/videos/kept.mp4",
        )

        self.db.replace_media_candidates(
            source_url,
            [MediaCandidate(source_url, media_url, label="refresh", status="pending")],
        )
        row = self.db.list_media_for_item(source_url)[0]
        self.assertEqual(row["status"], "downloaded")
        self.assertEqual(row["file_path"], "D:/archive/videos/kept.mp4")
        self.assertEqual(row["label"], "refresh")

    def test_list_media_for_download_skips_embed_references(self) -> None:
        source_url = "https://asmrlib.com/posts/11111111111111111111111111111111"
        self.db.add_seed(source_url)
        self.db.replace_media_candidates(
            source_url,
            [
                MediaCandidate(
                    source_url,
                    "https://bysetayico.com/e/abc",
                    label="BI",
                    kind="embed",
                    status="reference",
                ),
                MediaCandidate(
                    source_url,
                    "https://asmrlib.com/media/file.mp3",
                    label="Audio",
                    kind="download",
                    status="pending",
                ),
            ],
        )
        rows = self.db.list_media_for_download(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["media_url"], "https://asmrlib.com/media/file.mp3")

    def test_search_items_and_tags(self) -> None:
        tag_url = "https://asmrlib.com/tags/yoonying"
        first = "https://asmrlib.com/posts/11111111111111111111111111111111"
        second = "https://asmrlib.com/posts/22222222222222222222222222222222"
        self.db.add_tag_seed(tag_url)
        self.db.complete_tag_page(
            tag_url,
            tag_url,
            post_urls=[first, second],
            content_hash="hash-1",
        )
        self.db.update_item_result(
            first,
            title="Winter dream",
            published_at="2026-01-01",
            status="archived",
        )
        self.db.update_item_result(
            second,
            title="Summer night",
            published_at="2026-07-01",
            status="archived",
        )
        rows = self.db.search_items(query="Summer", limit=10)
        self.assertEqual([row["source_url"] for row in rows], [second])
        tagged = self.db.search_items(tag="yoonying", limit=10)
        self.assertEqual(len(tagged), 2)
        self.assertEqual(self.db.count_items(tag="yoonying"), 2)
        self.assertEqual(self.db.list_tags()[0]["post_count"], 2)

    def test_update_item_result_persists_cover(self) -> None:
        source_url = "https://asmrlib.com/posts/11111111111111111111111111111111"
        self.db.add_seed(source_url)
        self.db.update_item_result(
            source_url,
            title="Covered post",
            cover="https://asmrlib.com/media/cover.jpg",
            status="archived",
        )
        row = self.db.get_item(source_url)
        self.assertEqual(row["cover"], "https://asmrlib.com/media/cover.jpg")

    def test_legacy_items_table_gets_cover_column_via_migration(self) -> None:
        import sqlite3

        self.db.close()
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        # Build a pre-cover schema (no cover column) and populate one row.
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            """
            CREATE TABLE items (
              source_url TEXT PRIMARY KEY,
              title TEXT DEFAULT '',
              author TEXT DEFAULT '',
              published_at TEXT DEFAULT '',
              status TEXT NOT NULL DEFAULT 'pending',
              html_path TEXT DEFAULT '',
              metadata_path TEXT DEFAULT '',
              error TEXT DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO items(source_url, status, created_at, updated_at) VALUES (?, 'archived', 't', 't')",
            ("https://asmrlib.com/posts/11111111111111111111111111111111",),
        )
        conn.commit()
        conn.close()

        # init() must add the column without losing existing rows.
        db = ArchiveDb(legacy_path)
        db.init()
        try:
            columns = {str(r["name"]) for r in db.conn.execute("PRAGMA table_info(items)")}
            self.assertIn("cover", columns)
            row = db.get_item("https://asmrlib.com/posts/11111111111111111111111111111111")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "archived")
            self.assertEqual(row["cover"], "")  # migrated default
        finally:
            db.close()



if __name__ == "__main__":
    unittest.main()
