from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from asmrlib_archiver.archive_html import is_safe_archive_html, render_detail_archive
from asmrlib_archiver.config import AppConfig
from asmrlib_archiver.db import ArchiveDb
from asmrlib_archiver.models import MediaCandidate, ParsedPage
from asmrlib_archiver.viewer import ArchiveViewer


class ViewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data = root / "data"
        self.data.mkdir()
        self.config = AppConfig(
            config_path=root / "config.yaml",
            output_dir=self.data,
            database_path=self.data / "archive.sqlite3",
        )
        self.db = ArchiveDb(self.config.database_path)
        self.db.init()
        self.source = "https://asmrlib.com/posts/11111111111111111111111111111111"
        self.db.add_seed(self.source)
        self.db.update_item_result(
            self.source,
            title="Local sample",
            published_at="2026-07-01",
            cover="https://asmrlib.com/media/cover.jpg",
            status="archived",
            html_path=str(self.data / "html" / "sample.html"),
        )
        (self.data / "html").mkdir()
        (self.data / "videos").mkdir()
        media_path = self.data / "videos" / "sample.mp3"
        media_path.write_bytes(b"ID3fake-audio")
        self.db.replace_media_candidates(
            self.source,
            [
                MediaCandidate(
                    self.source,
                    "https://bysetayico.com/e/abc",
                    label="BI",
                    kind="embed",
                    status="reference",
                ),
                MediaCandidate(
                    self.source,
                    "https://asmrlib.com/media/sample.mp3",
                    label="Audio",
                    kind="download",
                    status="pending",
                ),
            ],
        )
        media_id = self.db.conn.execute(
            "SELECT id FROM media_candidates WHERE kind = 'download'"
        ).fetchone()["id"]
        self.db.update_media_result(
            media_id,
            status="downloaded",
            file_path=str(media_path),
        )
        self.db.close()
        self.viewer = ArchiveViewer(self.config)

    def tearDown(self) -> None:
        self.viewer.close()
        self.temp_dir.cleanup()

    def test_home_and_detail_render_local_player(self) -> None:
        home = self.viewer._render_home({})
        self.assertIn("Local sample", home)
        self.assertIn("1", home)
        self.assertIn("本地已可播", home)
        detail = self.viewer._render_detail(self.source)
        self.assertIn("本地播放器", detail)
        self.assertIn("/media/videos/sample.mp3", detail)
        self.assertIn("bysetayico.com", detail)
        # Remote references use one controlled online action; local media
        # keeps direct playback and the pinned mini player.  The original URL
        # remains available as a quiet escape hatch.
        self.assertIn("在线播放", detail)
        self.assertIn("detail-source-picker", detail)
        self.assertIn("source-open", detail)
        self.assertIn("source-external", detail)
        self.assertIn("openDesktopOnline", detail)
        self.assertIn("openDesktopExternal", detail)
        self.assertNotIn("recordDesktopOnline", detail)
        self.assertNotIn("browser-picker", detail)
        self.assertNotIn("/watch?url=", detail)
        self.assertIn("<strong>Audio</strong>", detail)
        self.assertIn("播放本地", detail)
        self.assertIn("在线来源", detail)
        self.assertIn("media-card", detail)
        self.assertIn("ref-card", detail)
        # One prominent controlled action plus a quiet original-link escape
        # hatch; old sandbox/recording actions stay removed.
        self.assertNotIn("简易嵌入", detail)
        self.assertIn("原链", detail)

    def test_watch_button_prefers_controlled_player(self) -> None:
        detail = self.viewer._render_detail(self.source)
        self.assertIn("在线播放", detail)
        self.assertIn("自动拦截", detail)
        self.assertIn("原链", detail)
        self.assertIn("openDesktopOnline", detail)
        self.assertIn("openDesktopExternal", detail)
        self.assertNotIn("openDesktopOnlineMini", detail)
        self.assertNotIn("录制到本地", detail)
        self.assertNotIn("/watch?url=", detail)

    def test_desktop_online_action_contract(self) -> None:
        from asmrlib_archiver.viewer.ui_assets import APP_JS

        self.assertIn("function openDesktopOnline(url)", APP_JS)
        self.assertIn("_callDesktop('open_online_player', target)", APP_JS)
        self.assertIn("return openDesktopExternal(target)", APP_JS)
        self.assertNotIn("getBrowserChoice", APP_JS)
        self.assertNotIn("openDesktopOnlineMini", APP_JS)
        self.assertNotIn("recordDesktopOnline", APP_JS)

    def test_cover_fallback_retries_photon_origin_before_plate(self) -> None:
        from asmrlib_archiver.viewer.ui_assets import APP_JS

        # Photon proxy misses must retry the origin URL once; the letter
        # plate only shows when the origin also fails.
        self.assertIn("photonOriginUrl", APP_JS)
        self.assertIn("coverOriginTried", APP_JS)
        self.assertLess(
            APP_JS.index("photonOriginUrl"),
            APP_JS.index("dataset.coverFailed = '1'"),
            "origin retry must run before the fallback plate is revealed",
        )

    def test_live_feed_cache_serves_stale_on_failure(self) -> None:
        # Transient remote failures must not surface the manual retry state
        # while a recent payload exists: the stale copy is served instead.
        calls = []

        def fake_payload(query):
            calls.append(dict(query))
            if len(calls) == 1:
                return {"ok": True, "html": "<div>x</div>", "next": "/browse?page=2"}
            return {"ok": False, "html": "", "error": "boom"}

        self.viewer._live_feed_cache = {}
        self.viewer._live_feed_payload = fake_payload  # type: ignore[method-assign]

        query = {"scope": ["browse"], "page": ["1"]}
        first = self.viewer._live_feed_cached(query)
        self.assertTrue(first["ok"])
        self.assertEqual(len(calls), 1)

        # Fresh cache window: no remote call at all.
        again = self.viewer._live_feed_cached(query)
        self.assertTrue(again["ok"])
        self.assertEqual(len(calls), 1)

        # After TTL expiry the remote fetch fails; the stale copy is served
        # with the stale flag instead of the error payload.
        cached_at, cached_payload = self.viewer._live_feed_cache[
            "page=1&scope=browse"
        ]
        self.viewer._live_feed_cache["page=1&scope=browse"] = (
            cached_at - 1800.0,
            cached_payload,
        )
        stale = self.viewer._live_feed_cached(query)
        self.assertTrue(stale["ok"])
        self.assertTrue(stale.get("stale"))
        self.assertEqual(len(calls), 2)

    def test_home_renders_grids_without_horizontal_rails(self) -> None:
        home = self.viewer._render_home({})
        # 最近归档 / 本地已可播 / 站点最新 all render as cover grids now.
        self.assertNotIn("data-live-feed-mode='rail'", home)
        self.assertNotIn("data-cinema-rail", home)

    def test_home_and_detail_render_cover_image(self) -> None:
        import inspect

        from asmrlib_archiver import viewer as viewer_module

        home = self.viewer._render_home({})
        # Card thumbnail on the home grid (cover-thumb, includes remote cover URL).
        self.assertIn("cover-thumb", home)
        self.assertIn("https://asmrlib.com/media/cover.jpg", home)
        detail = self.viewer._render_detail(self.source)
        # Detail page shows cover as a dedicated poster panel.
        self.assertIn("detail-poster-img", detail)
        self.assertIn(
            "background-image: url(https://asmrlib.com/media/cover.jpg)", detail
        )
        # Viewer CSP (DEFAULT_CSP constant, sent as HTTP header by _send_html)
        # must permit remote https images so the cover can load in the browser.
        self.assertIn("img-src 'self' data: https:", viewer_module.DEFAULT_CSP)
        send_html_source = inspect.getsource(viewer_module.ArchiveViewer._send_html)
        self.assertIn("Content-Security-Policy", send_html_source)
        self.assertIn("DEFAULT_CSP", send_html_source)

    def test_detail_omits_cover_when_absent(self) -> None:
        bare_url = "https://asmrlib.com/posts/33333333333333333333333333333333"
        self.viewer.db.conn.execute(
            "INSERT INTO items(source_url, status, cover, created_at, updated_at) "
            "VALUES (?, 'archived', '', 't', 't')",
            (bare_url,),
        )
        self.viewer.db.conn.commit()
        detail = self.viewer._render_detail(bare_url)
        # No cover URL -> gradient plate poster, no remote cover image element.
        self.assertIn("detail-hero", detail)
        self.assertIn("class='detail-poster-plate'", detail)
        self.assertNotIn("class='detail-poster-img'", detail)
        # Cover wash uses inline background-image only when a cover URL exists.
        self.assertNotIn("background-image: url(http", detail)
        self.assertNotIn("background-image: url('http", detail)
        self.assertNotIn('background-image: url("http', detail)

    def test_home_hides_playable_section_when_no_local_files(self) -> None:
        # Drop the downloaded row so home no longer shows the playable strip.
        self.viewer.db.conn.execute(
            "UPDATE media_candidates SET status='reference', file_path='' WHERE kind='download'"
        )
        self.viewer.db.conn.commit()
        home = self.viewer._render_home({})
        self.assertNotIn("本地已可播", home)
        self.assertIn("安全播放", home)
        self.assertIn("桌面自动拦截", home)
        self.assertIn("openDesktopOnline", home)
        # Explore / author nav still present on the redesigned home.
        self.assertIn("/explore", home)
        self.assertIn("发现", home)

    def test_author_and_related_routes(self) -> None:
        self.viewer.db.conn.execute(
            "UPDATE items SET author = ? WHERE source_url = ?",
            ("Sample Author", self.source),
        )
        self.viewer.db.conn.execute(
            """
            INSERT INTO tag_pages(tag_url, page_url, status, created_at, updated_at)
            VALUES (?, ?, 'complete', 't', 't')
            """,
            ("https://asmrlib.com/tags/yoonying", "https://asmrlib.com/tags/yoonying"),
        )
        self.viewer.db.conn.execute(
            """
            INSERT INTO tag_items(tag_url, source_url, discovered_from, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, 't', 't')
            """,
            (
                "https://asmrlib.com/tags/yoonying",
                self.source,
                "https://asmrlib.com/tags/yoonying",
            ),
        )
        related_url = "https://asmrlib.com/posts/22222222222222222222222222222222"
        self.viewer.db.conn.execute(
            """
            INSERT INTO items(source_url, title, author, status, cover, created_at, updated_at)
            VALUES (?, 'Related sample', 'Sample Author', 'archived', '', 't', 't')
            """,
            (related_url,),
        )
        self.viewer.db.conn.execute(
            """
            INSERT INTO tag_items(tag_url, source_url, discovered_from, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, 't', 't')
            """,
            (
                "https://asmrlib.com/tags/yoonying",
                related_url,
                "https://asmrlib.com/tags/yoonying",
            ),
        )
        self.viewer.db.conn.commit()

        authors = self.viewer._render_author({"name": ["Sample Author"]})
        self.assertIn("Sample Author", authors)
        self.assertIn("Local sample", authors)
        self.assertIn("Related sample", authors)

        detail = self.viewer._render_detail(self.source)
        self.assertIn("相关推荐", detail)
        self.assertIn("Related sample", detail)
        self.assertIn(f"/author?name={quote('Sample Author')}", detail)

    def test_explore_rejects_off_domain(self) -> None:
        page = self.viewer._render_explore({"url": ["https://evil.example/tags/x"]})
        self.assertIn("无法浏览", page)

    def test_explore_index_lists_config_tags(self) -> None:
        # Default config has no tag_seeds in AppConfig() — inject one.
        object.__setattr__(
            self.viewer.config,
            "tag_seeds",
            ["https://asmrlib.com/tags/yoonying"],
        )
        page = self.viewer._render_explore({})
        self.assertIn("#yoonying", page)
        self.assertIn("/explore?tag=yoonying", page)
        self.assertIn("/browse", page)

    def test_browse_rejects_off_domain_and_non_home(self) -> None:
        page = self.viewer._render_browse({"url": ["https://evil.example/"]})
        self.assertIn("无法浏览", page)
        page2 = self.viewer._render_browse(
            {"url": ["https://asmrlib.com/tags/yoonying"]}
        )
        self.assertIn("无法浏览", page2)

    def test_browse_url_builder(self) -> None:
        self.assertEqual(self.viewer._build_browse_url(1), "https://asmrlib.com/")
        self.assertEqual(
            self.viewer._build_browse_url(3), "https://asmrlib.com/?page=3"
        )
        self.assertEqual(
            self.viewer._assert_browse_url("https://asmrlib.com/?page=2&utm=x"),
            "https://asmrlib.com/?page=2",
        )

    def test_home_has_site_browse_entry(self) -> None:
        home = self.viewer._render_home({})
        self.assertIn("/browse", home)
        self.assertIn("asmrlib 最新", home)
        # Top nav is 4 tabs, not the old 6-item bar.
        self.assertIn('data-nav="discover"', home)
        self.assertIn('data-nav="library"', home)
        self.assertIn('data-nav="local"', home)
        self.assertNotIn("站点预览", home)
        self.assertNotIn("本地录制", home)

    def test_enqueue_live_posts_seeds_and_skips_archived(self) -> None:
        new_url = "https://asmrlib.com/posts/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        summary = self.viewer._enqueue_live_posts(
            [self.source, new_url, "https://evil.example/posts/x"],
            auto_crawl=False,
        )
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["archived"], 1)
        row = self.viewer.db.get_item(new_url)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending")

    def test_enqueue_live_posts_respects_tag_seeds_filter(self) -> None:
        object.__setattr__(
            self.viewer.config,
            "tag_seeds",
            ["https://asmrlib.com/tags/yoonying"],
        )
        yoonying = "https://asmrlib.com/posts/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        other = "https://asmrlib.com/posts/cccccccccccccccccccccccccccccccc"
        summary = self.viewer._enqueue_live_posts(
            [yoonying, other],
            auto_crawl=False,
            require_tags=self.viewer._auto_archive_tag_slugs(),
            post_tags={
                yoonying: {"yoonying", "asmr"},
                other: {"other-author"},
            },
        )
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["skipped_tag"], 1)
        self.assertIsNotNone(self.viewer.db.get_item(yoonying))
        self.assertIsNone(self.viewer.db.get_item(other))

    def test_startup_tag_sync_skips_without_tag_seeds(self) -> None:
        object.__setattr__(self.viewer.config, "tag_seeds", [])
        # Reset guard so the call is evaluated fresh.
        self.viewer._startup_sync_started = False
        self.assertFalse(self.viewer.start_background_tag_sync(reason="test"))
        home = self.viewer._render_home({})
        # No seeds → no startup banner.
        self.assertNotIn("data-sync-banner='startup'", home)

    def test_home_shows_startup_sync_banner_when_tag_seeds(self) -> None:
        object.__setattr__(
            self.viewer.config,
            "tag_seeds",
            ["https://asmrlib.com/tags/yoonying"],
        )
        home = self.viewer._render_home({})
        self.assertIn("data-sync-banner='startup'", home)
        self.assertIn("#yoonying", home)

    def test_parse_byte_range(self) -> None:
        from asmrlib_archiver.viewer import _parse_byte_range

        self.assertEqual(_parse_byte_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(_parse_byte_range("bytes=50-", 100), (50, 99))
        self.assertEqual(_parse_byte_range("bytes=-10", 100), (90, 99))
        self.assertIsNone(_parse_byte_range("bytes=100-110", 100))

    def test_safe_path_rejects_traversal(self) -> None:
        self.assertIsNone(self.viewer._safe_data_path("../secret.txt"))
        self.assertIsNone(self.viewer._safe_data_path("videos/../../secret.txt"))
        ok = self.viewer._safe_data_path("videos/sample.mp3")
        self.assertIsNotNone(ok)
        assert ok is not None
        self.assertTrue(ok.is_file())

    def test_lazy_cover_falls_back_gracefully_without_network(self) -> None:
        """A failed/nonexistent lazy cover must return '' and cache the miss,
        never raise — so the detail page still renders."""
        bad_url = "https://asmrlib.com/posts/ffffff00000000000000000000000000"
        cover = self.viewer._lazy_cover(bad_url, "")
        self.assertEqual(cover, "")
        # Second call returns cached '' immediately (no re-fetch attempt).
        cover2 = self.viewer._lazy_cover(bad_url, "")
        self.assertEqual(cover2, "")
        self.assertIn(bad_url, self.viewer._cover_cache)

    def test_lazy_cover_uses_stored_cover_without_network(self) -> None:
        cover = self.viewer._lazy_cover(self.source, "https://asmrlib.com/media/cover.jpg")
        self.assertEqual(cover, "https://asmrlib.com/media/cover.jpg")
        # A stored cover short-circuits and is not cached as a miss.
        self.assertNotIn(self.source, self.viewer._cover_cache)

    def test_archive_html_allows_relative_local_media_only(self) -> None:
        page = ParsedPage(
            source_url=self.source,
            title="Local sample",
            tags=["yoonying"],
            media=[
                MediaCandidate(
                    self.source,
                    "https://bysetayico.com/e/abc",
                    label="BI",
                    kind="embed",
                    status="reference",
                )
            ],
        )
        html = render_detail_archive(
            page,
            local_media=[{"label": "Audio", "src": "videos/sample.mp3", "kind": "audio"}],
        )
        self.assertTrue(is_safe_archive_html(html))
        # Archives are served at /file/html/<key>.html, media at
        # /file/videos/<x>, so the src is rewritten to ../videos/<x> to resolve
        # correctly (a bare videos/<x> would 404 under /file/html/...).
        self.assertIn(
            '<audio controls preload="metadata" src="../videos/sample.mp3">', html
        )
        unsafe = html.replace('src="../videos/sample.mp3"', 'src="https://evil.example/x.mp3"')
        self.assertFalse(is_safe_archive_html(unsafe))
        # A deeper traversal that would escape the archive root must also fail.
        traversal = html.replace('src="../videos/sample.mp3"', 'src="../../etc/passwd"')
        self.assertFalse(is_safe_archive_html(traversal))


if __name__ == "__main__":
    unittest.main()
