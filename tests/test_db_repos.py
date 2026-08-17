from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from asmrlib_archiver.db.base import DbConnection
from asmrlib_archiver.db.items import ItemRepo
from asmrlib_archiver.db.media import MediaRepo
from asmrlib_archiver.db.seeds import SeedRepo
from asmrlib_archiver.db.stats import StatsRepo
from asmrlib_archiver.db.tag_pages import TagPageRepo


class DbRepoStandaloneTests(unittest.TestCase):
    """验证各 repo 不依赖 ArchiveDb facade 即可独立协作。

    拆分的核心价值就是 repo 可以单独实例化、单独测试。如果只能通过
    ArchiveDb 用，那就是伪装的 God Class。
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.conn = DbConnection.open(Path(self.temp_dir.name) / "archive.sqlite3")
        DbConnection(self.conn).init_schema()
        # 各 repo 共享同一个 conn，模拟 ArchiveDb 内部结构
        self.seeds = SeedRepo(self.conn)
        self.tag_pages = TagPageRepo(self.conn)
        self.items = ItemRepo(self.conn)
        self.media = MediaRepo(self.conn)
        self.stats = StatsRepo(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def test_repos_collaborate_without_archive_db_facade(self) -> None:
        tag_url = "https://asmrlib.com/tags/yoonying"
        post_url = "https://asmrlib.com/posts/11111111111111111111111111111111"

        self.assertTrue(self.seeds.add_tag_seed(tag_url))
        self.assertFalse(self.seeds.add_tag_seed(tag_url))

        completion = self.tag_pages.complete_tag_page(
            tag_url,
            tag_url,
            post_urls=[post_url],
            content_hash="h1",
        )
        self.assertEqual(completion.items_added, 1)
        # add_seed 在 complete_tag_page 之后应幂等返回 False
        self.assertFalse(self.seeds.add_seed(post_url))

        self.items.update_item_result(post_url, title="Hello", status="archived")
        row = self.items.get_item(post_url)
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "Hello")

        counts = self.stats.counts()
        self.assertEqual(counts["items.archived"], 1)
        self.assertEqual(counts["tag_pages.complete"], 1)
        self.assertEqual(counts["tags.total"], 1)

    def test_find_items_by_title_or_url_returns_empty_for_blank_needle(self) -> None:
        self.assertEqual(self.items.find_items_by_title_or_url(""), [])
        self.assertEqual(self.items.find_items_by_title_or_url("   "), [])

    def test_list_archive_items_rejects_negative_limit(self) -> None:
        with self.assertRaises(ValueError):
            self.items.list_archive_items(limit=-1)

    def test_attach_local_media_rejects_unknown_source_url(self) -> None:
        with self.assertRaises(ValueError):
            self.media.attach_local_media(
                "https://asmrlib.com/posts/ffffffffffffffffffffffffffffffff",
                file_path=__file__,
            )

    def test_attach_local_media_rejects_missing_file(self) -> None:
        post_url = "https://asmrlib.com/posts/11111111111111111111111111111111"
        self.seeds.add_seed(post_url)
        with self.assertRaises(FileNotFoundError):
            self.media.attach_local_media(post_url, file_path="/no/such/file.mp3")

    def test_counts_on_empty_database_returns_expected_buckets(self) -> None:
        counts = self.stats.counts()
        # 空 db 仍应返回 tag_items.total / tags.total 等键，值为 0
        self.assertEqual(counts["tags.total"], 0)
        self.assertEqual(counts["tag_items.total"], 0)
        self.assertEqual(self.stats.unresolved_counts(), {"tag_pages": 0, "items": 0, "media": 0})

    def test_list_playable_media_rejects_non_positive_limit(self) -> None:
        with self.assertRaises(ValueError):
            self.media.list_playable_media(limit=0)

    def test_media_counts_for_sources_batches_card_status(self) -> None:
        first = "https://asmrlib.com/posts/11111111111111111111111111111111"
        second = "https://asmrlib.com/posts/22222222222222222222222222222222"
        self.seeds.add_seed(first)
        self.seeds.add_seed(second)
        self.conn.execute(
            """
            INSERT INTO media_candidates(
              source_url, media_url, status, file_path, created_at, updated_at
            ) VALUES (?, ?, 'downloaded', ?, 't', 't')
            """,
            (first, "file://local/a.mp3", __file__),
        )
        self.conn.execute(
            """
            INSERT INTO media_candidates(
              source_url, media_url, status, file_path, created_at, updated_at
            ) VALUES (?, ?, 'reference', '', 't', 't')
            """,
            (first, "https://bysetayico.com/e/example"),
        )
        self.conn.commit()

        counts = self.media.media_counts_for_sources([first, second, first])

        self.assertEqual(
            counts[first], {"local": 1, "references": 1, "playable": 1}
        )
        self.assertEqual(
            counts[second], {"local": 0, "references": 0, "playable": 0}
        )


if __name__ == "__main__":
    unittest.main()
