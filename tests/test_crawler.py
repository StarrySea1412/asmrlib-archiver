from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from asmrlib_archiver.archive_html import is_safe_archive_html
from asmrlib_archiver.config import AppConfig, CrawlerConfig, DiscoveryConfig
from asmrlib_archiver.crawler import Archiver
from asmrlib_archiver.http_client import FetchResult


TAG_URL = "https://asmrlib.com/tags/yoonying"
POST_ONE = "https://asmrlib.com/posts/11111111111111111111111111111111"
POST_TWO = "https://asmrlib.com/posts/22222222222222222222222222222222"


class FakeHttpClient:
    pages: dict[str, str] = {}
    final_urls: dict[str, str] = {}

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def close(self) -> None:
        pass

    def fetch_page(self, url: str) -> FetchResult:
        html = self.pages[url]
        return FetchResult(
            url=self.final_urls.get(url, url),
            content=html.encode("utf-8"),
            content_type="text/html; charset=utf-8",
            history=[],
        )


class CrawlerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeHttpClient.final_urls = {}
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config = AppConfig(
            config_path=root / "config.yaml",
            tag_seeds=[TAG_URL],
            output_dir=root / "data",
            database_path=root / "data" / "archive.sqlite3",
            crawler=CrawlerConfig(obey_robots=False, delay_seconds=0, retries=0),
            discovery=DiscoveryConfig(
                max_pages_per_tag=10,
                max_posts_per_tag=20,
                page_batch_size=2,
            ),
        )
        self.progress_messages: list[str] = []
        self.archiver = Archiver(self.config, progress=self.progress_messages.append)
        self.archiver.init()

    def tearDown(self) -> None:
        self.archiver.close()
        self.temp_dir.cleanup()

    def test_discover_then_crawl_writes_only_safe_archives(self) -> None:
        page_two = f"{TAG_URL}?page=2"
        FakeHttpClient.pages = {
            TAG_URL: f"""
                <main><h1>Yoonying</h1><a href="{POST_ONE}">one</a></main>
                <script src="https://js.wpadmngr.com/static/adManager.js"></script>
                <a rel="next" href="?page=2">next</a>
            """,
            page_two: f"""
                <main><h1>Yoonying page 2</h1>
                <a href="{POST_ONE}">duplicate</a><a href="{POST_TWO}">two</a></main>
                <a href="https://sead.pages.dev/xingchen">promotion</a>
            """,
        }
        self.assertEqual(self.archiver.add_config_seeds(), 1)

        with patch("asmrlib_archiver.services.discovery.SafeHttpClient", FakeHttpClient), patch("asmrlib_archiver.services.crawl.SafeHttpClient", FakeHttpClient):
            discovered = self.archiver.discover()

        self.assertEqual(discovered.pages_ok, 2)
        self.assertEqual(discovered.pages_failed, 0)
        self.assertEqual(discovered.posts_added, 2)
        self.assertEqual(discovered.incomplete, 0)
        self.assertEqual(len(self.archiver.db.list_tag_items(TAG_URL)), 2)
        tag_progress = [
            message
            for message in self.progress_messages
            if message.startswith("[tag ") and " Fetching " in message
        ]
        self.assertEqual(len(tag_progress), 2)
        self.assertIn("[tag 1/", tag_progress[0])
        self.assertIn(TAG_URL, tag_progress[0])
        self.assertIn("[tag 2/", tag_progress[1])
        self.assertIn(page_two, tag_progress[1])

        for html_path in self.config.output_dir.joinpath("html").glob("*.html"):
            html = html_path.read_text(encoding="utf-8")
            self.assertTrue(is_safe_archive_html(html))
            self.assertNotIn("wpadmngr", html.lower())
            self.assertNotIn("sead.pages.dev", html.lower())

        FakeHttpClient.pages = {
            POST_ONE: """
                <main><h1>First post</h1><a href="/tags/yoonying">yoonying</a></main>
                <script src="https://s10.histats.com/js15_as.js"></script>
            """,
            POST_TWO: """
                <main><h1>Second post</h1><a href="/tags/yoonying">yoonying</a></main>
                <div><a href="https://bit.ly/fulise">promotion</a></div>
            """,
        }
        with patch("asmrlib_archiver.services.discovery.SafeHttpClient", FakeHttpClient), patch("asmrlib_archiver.services.crawl.SafeHttpClient", FakeHttpClient):
            crawled, failed = self.archiver.crawl(limit=20)

        self.assertEqual((crawled, failed), (2, 0))
        post_progress = [
            message
            for message in self.progress_messages
            if message.startswith("[post ") and " Fetching " in message
        ]
        self.assertEqual(len(post_progress), 2)
        self.assertIn("[post 1/2]", post_progress[0])
        self.assertIn(POST_ONE, post_progress[0])
        self.assertIn("[post 2/2]", post_progress[1])
        self.assertIn(POST_TWO, post_progress[1])
        counts = self.archiver.status()
        self.assertEqual(counts["items.archived"], 2)
        self.assertEqual(counts["tag_items.total"], 2)
        for html_path in self.config.output_dir.joinpath("html").glob("*.html"):
            self.assertTrue(is_safe_archive_html(html_path.read_text(encoding="utf-8")))

        with patch("asmrlib_archiver.services.discovery.SafeHttpClient", FakeHttpClient), patch("asmrlib_archiver.services.crawl.SafeHttpClient", FakeHttpClient):
            second_run = self.archiver.discover()
        self.assertEqual(second_run.pages_ok, 0)
        self.assertEqual(second_run.posts_added, 0)

    def test_page_limit_is_reported_as_incomplete_without_enqueuing_more(self) -> None:
        limited_config = AppConfig(
            config_path=self.config.config_path,
            tag_seeds=[TAG_URL],
            output_dir=self.config.output_dir / "limited",
            database_path=self.config.output_dir / "limited" / "archive.sqlite3",
            crawler=CrawlerConfig(obey_robots=False, delay_seconds=0, retries=0),
            discovery=DiscoveryConfig(
                max_pages_per_tag=1,
                max_posts_per_tag=20,
                page_batch_size=1,
            ),
        )
        limited = Archiver(limited_config)
        limited.init()
        limited.add_config_seeds()
        FakeHttpClient.pages = {
            TAG_URL: f"""
                <main><h1>Yoonying</h1><a href="{POST_ONE}">one</a></main>
                <a rel="next" href="?page=2">next</a>
            """
        }
        try:
            with patch("asmrlib_archiver.services.discovery.SafeHttpClient", FakeHttpClient), patch("asmrlib_archiver.services.crawl.SafeHttpClient", FakeHttpClient):
                result = limited.discover()
            self.assertEqual(result.pages_ok, 1)
            self.assertEqual(result.incomplete, 1)
            self.assertEqual(limited.db.count_tag_pages(TAG_URL), 1)
            row = limited.db.conn.execute(
                "SELECT status, next_page_url FROM tag_pages"
            ).fetchone()
            self.assertEqual(row["status"], "incomplete")
            self.assertEqual(row["next_page_url"], f"{TAG_URL}?page=2")
        finally:
            limited.close()

    def test_empty_or_redirected_tag_pages_fail_without_leaking_urls(self) -> None:
        self.archiver.add_config_seeds()
        FakeHttpClient.pages = {TAG_URL: "<html><title>Challenge</title></html>"}
        with patch("asmrlib_archiver.services.discovery.SafeHttpClient", FakeHttpClient), patch("asmrlib_archiver.services.crawl.SafeHttpClient", FakeHttpClient):
            empty = self.archiver.discover()
        self.assertEqual(empty.pages_failed, 1)
        row = self.archiver.db.conn.execute("SELECT status, error FROM tag_pages").fetchone()
        self.assertEqual(row["status"], "error")
        self.assertNotIn("http", row["error"])

        FakeHttpClient.pages = {
            TAG_URL: f"<main><h1>Yoonying</h1><a href='{POST_ONE}'>one</a></main>"
        }
        FakeHttpClient.final_urls = {TAG_URL: "https://asmrlib.com/tags/other"}
        with patch("asmrlib_archiver.services.discovery.SafeHttpClient", FakeHttpClient), patch("asmrlib_archiver.services.crawl.SafeHttpClient", FakeHttpClient):
            redirected = self.archiver.discover(retry_errors=True)
        self.assertEqual(redirected.pages_failed, 1)
        row = self.archiver.db.conn.execute("SELECT status, error FROM tag_pages").fetchone()
        self.assertEqual(row["status"], "error")
        self.assertIn("redirect_mismatch", row["error"])
        self.assertNotIn("http", row["error"])

    def test_retry_errors_processes_more_than_one_batch_once_each(self) -> None:
        tags = [f"https://asmrlib.com/tags/retry-{index}" for index in range(3)]
        for index, tag_url in enumerate(tags):
            self.archiver.db.add_tag_seed(tag_url)
            self.archiver.db.mark_tag_page_failed(
                tag_url,
                tag_url,
                error="previous_error",
            )
            post_id = f"{index + 10:032x}"
            FakeHttpClient.pages[tag_url] = (
                f"<main><h1>Retry</h1><a href='/posts/{post_id}'>post</a></main>"
            )

        with patch("asmrlib_archiver.services.discovery.SafeHttpClient", FakeHttpClient), patch("asmrlib_archiver.services.crawl.SafeHttpClient", FakeHttpClient):
            result = self.archiver.discover(limit=3, retry_errors=True)

        self.assertEqual(result.pages_ok, 3)
        self.assertEqual(result.pages_failed, 0)
        self.assertEqual(result.posts_added, 3)
        self.assertEqual(self.archiver.unresolved()["tag_pages"], 0)


if __name__ == "__main__":
    unittest.main()
