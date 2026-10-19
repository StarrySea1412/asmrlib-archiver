from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

from asmrlib_archiver.archive_html import is_safe_archive_html, render_detail_archive
from asmrlib_archiver.config import AppConfig
from asmrlib_archiver.db import ArchiveDb
from asmrlib_archiver.local_audit import audit_local_media
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
        # Without the desktop bridge (phone browser / APK), playback stays
        # in-app via the sandboxed /play page, not a system-browser hand-off.
        self.assertIn("'/play?url=' + encodeURIComponent(target)", APP_JS)
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

    def test_cover_proxy_only_accepts_hosts_the_archive_uses(self) -> None:
        """The proxy must not be an open relay into the LAN.

        asmrlib.com is a configured domain (allowed), example.org is not in
        the config and not referenced by any stored cover, so it is refused.
        """
        viewer = self.viewer
        self.assertIn("asmrlib.com", viewer._cover_allow_hosts())
        self.assertNotIn("example.org", viewer._cover_allow_hosts())
        for bad in [
            "http://example.org/a.jpg",
            "file:///etc/passwd",
            "http://127.0.0.1:8765/",
            "not-a-url",
        ]:
            with self.subTest(url=bad):
                status = {}

                class _Handler:
                    def send_response(self, code):
                        status["code"] = code

                    def send_header(self, *_a, **_k):
                        pass

                    def end_headers(self):
                        pass

                    @property
                    def wfile(self):
                        return _Sink()

                class _Sink:
                    def write(self, _b):
                        pass

                viewer._send_cover(_Handler(), bad)
                self.assertNotEqual(status.get("code"), 200)

    def test_cover_proxy_caches_to_disk_and_is_immutable(self) -> None:
        """A fetched cover lands on disk once and is served immutable after."""
        viewer = self.viewer
        url = "https://asmrlib.com/media/cover.jpg"
        target = (
            self.data
            / "covers"
            / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".jpg")
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xff\xd8\xff\xe0jpeg-bytes")

        seen: dict[str, object] = {}

        class _Handler:
            def send_response(self, code):
                seen["code"] = code

            def send_header(self, key, value):
                seen[str(key).lower()] = value

            def end_headers(self):
                pass

            @property
            def wfile(self):
                return _Sink()

        class _Sink:
            def write(self, data):
                seen["body"] = data

        viewer._send_cover(_Handler(), url)
        self.assertEqual(seen["code"], 200)
        self.assertEqual(seen["body"], b"\xff\xd8\xff\xe0jpeg-bytes")
        # immutable: the filename is a hash of the URL, so a hit cannot go stale
        self.assertIn("immutable", str(seen.get("cache-control", "")))
        self.assertTrue(target.is_file())

    def test_media_save_allowlist_uses_recorded_hosts_but_rejects_loopback(self) -> None:
        # The save endpoint must accept real player hosts already referenced by
        # the archive, but never private/loopback targets (SSRF surface).
        self.assertIn("bysetayico.com", self.viewer._media_allow_hosts())
        for host in ("127.0.0.1", "localhost", "::1", "evil.example.com"):
            self.assertNotIn(host, self.viewer._media_allow_hosts())

    def test_media_save_rejects_player_html_as_fake_success(self) -> None:
        # A player/embed URL responds with HTML. The old save worker wrote that
        # 1.6 KB page to a .bin and called it success — it was not a video.
        # Mock the HTTP response so this regression test is deterministic.
        from unittest.mock import patch

        from asmrlib_archiver.media_save import save_media

        class _Response:
            headers = {"content-type": "text/html; charset=utf-8"}
            is_redirect = False
            url = "https://bysetayico.com/e/embed-not-media"

            def raise_for_status(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def stream(self, method, url, **kwargs):
                # The real client receives follow_redirects/timeout here; the
                # save path resolves redirects itself so the double just serves
                # the final response.
                return _Response()

        with patch("asmrlib_archiver.media_save.httpx.Client", _Client):
            result = save_media(
                "https://bysetayico.com/e/embed-not-media",
                save_dir=self.data / "videos",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "not_direct_media")
        self.assertFalse(any((self.data / "videos").glob("*.bin")))

    def test_cover_service_worker_receives_the_real_cover_hosts(self) -> None:
        """The SW used to get only configured domains, i.e. it cached nothing.

        Every cover in this archive comes from a CDN that list does not name,
        so the worker silently did nothing on every page load.
        """
        from asmrlib_archiver.viewer.ui_assets import cover_service_worker

        body = cover_service_worker(
            sorted(self.viewer._cover_allow_hosts())
        ).body.decode("utf-8")
        self.assertIn("asmrlib.com", body)

    def test_home_and_detail_render_cover_image(self) -> None:
        import inspect

        from asmrlib_archiver import viewer as viewer_module

        home = self.viewer._render_home({})
        # Card thumbnail on the home grid (cover-thumb). Covers are routed
        # through the local /cover proxy so they are fetched once for every
        # client instead of once per page load per browser — see local_cover().
        self.assertIn("cover-thumb", home)
        self.assertIn(
            "/cover?u=https%3A%2F%2Fasmrlib.com%2Fmedia%2Fcover.jpg", home
        )
        detail = self.viewer._render_detail(self.source)
        # Detail page shows cover as a dedicated poster panel.
        self.assertIn("detail-poster-img", detail)
        self.assertIn(
            "background-image: url(/cover?u=https%3A%2F%2Fasmrlib.com"
            "%2Fmedia%2Fcover.jpg)",
            detail,
        )
        # Viewer CSP (DEFAULT_CSP constant, sent as HTTP header by _send_html)
        # must permit remote https images so the cover can load in the browser.
        self.assertIn("img-src 'self' data: https:", viewer_module.DEFAULT_CSP)
        send_html_source = inspect.getsource(viewer_module.ArchiveViewer._send_html)
        self.assertIn("Content-Security-Policy", send_html_source)
        self.assertIn("DEFAULT_CSP", send_html_source)

    def test_csp_lets_the_browser_load_the_web_app_manifest(self) -> None:
        # With `default-src 'none'` and no `manifest-src`, the browser refuses
        # the manifest outright, so "add to home screen" silently loses the app
        # name, icon and standalone display — the route returning 200 is not
        # enough. Found by loading the page in a real browser, not by the tests.
        from asmrlib_archiver import viewer as viewer_module
        for name in ("DEFAULT_CSP", "TOOL_CSP"):
            csp = getattr(viewer_module, name)
            self.assertIn("manifest-src 'self'", csp, name)

    def test_archive_pages_dont_attach_the_network_banner(self) -> None:
        # The viewer serves local content on /, /posts, /post/..., /author,
        # /recordings; painting a red "network unsuitable" banner over the
        # cinema hero there misleads the user. The banner must only attach
        # on online pages — found when a real desktop browser showed it on
        # every archive page.
        from asmrlib_archiver.viewer.ui_assets import APP_JS

        # Slice out the band module's init() — the only one that mentions
        # `online` and ends with run(false). The app module's init() runs
        # currentNav / initFeeds / registerCoverCache and must always run.
        last_init = APP_JS.rindex("function init()")
        band_init = APP_JS[last_init:]
        self.assertIn("online", band_init)
        self.assertIn("if (!online) return", band_init)
        self.assertIn("run(false)", band_init)
        # Local paths must not appear as online triggers; otherwise the
        # banner would be painted over the archive pages it must skip.
        for local in ("/posts", "/post/", "/author", "/recordings"):
            self.assertNotIn(f"'{local}'", band_init)
        # And the net-banner runtime must still ship so live pages can show it.
        self.assertIn("__asmrlibNetBannerPreview", APP_JS)

    def test_first_six_covers_paint_eager_above_the_fold(self) -> None:
        # cover_grid defaults to lazy; without a hint the first viewport's
        # right-hand cards sat blank until the user scrolled. The fix is in
        # _post_cards (not cover_card itself, so explicit overrides still
        # work for future callsites). Seed seven items so the boundary
        # between eager and lazy is exercised in both halves.
        import re

        for i in range(7):
            url = f"https://asmrlib.com/posts/aaaaaaaaaaaa000000000000000000000{i}"
            self.viewer.db.add_seed(url)
            self.viewer.db.update_item_result(
                url,
                title=f"Sample {i}",
                published_at="2026-07-01",
                cover=f"https://asmrlib.com/media/cover-{i}.jpg",
                status="archived",
                html_path=str(self.data / "html" / f"sample-{i}.html"),
            )
        html = self.viewer._render_list({})
        # Match each cover-thumb's loading value with a regex that anchors
        # on the cover-thumb div boundary — naive string scans misread
        # class='cover-grid' or grids that lack a matching loading=' for
        # some reason.
        per_card = re.findall(
            r"class='cover-thumb[^']*'[\s\S]*?loading='([a-z]+)'",
            html,
        )
        self.assertEqual(per_card[:6], ["eager"] * 6)
        self.assertGreaterEqual(len(per_card), 7)
        self.assertEqual(per_card[6], "lazy")

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

    # -------------------------------------------------- local media integrity

    def test_recordings_page_keeps_a_deleted_file_visible_as_missing(self) -> None:
        """The defect under test: a deleted file used to make its row vanish.

        The page presented itself as the user's library while silently
        omitting every broken entry, so the row stayed in the database with no
        way to see or reach it. It must now be rendered, honestly labelled, and
        offer only the two actions that can actually help.
        """
        media = self.data / "videos" / "sample.mp3"
        self.assertTrue(media.is_file())
        media.unlink()

        html = self.viewer._render_recordings({})
        self.assertIn("文件丢失", html)
        self.assertIn("Audio", html)
        self.assertIn("sample.mp3", html)
        self.assertIn("重新关联", html)
        self.assertIn("移除记录", html)
        self.assertIn("1 个文件丢失", html)
        # Nothing may be offered that pretends the file is playable.
        self.assertNotIn("action-local", html)
        self.assertNotIn("action-mini", html)
        self.assertNotIn("/media/videos/sample.mp3", html)
        self.assertNotIn("本地可用", html)

    def test_recordings_page_still_plays_a_healthy_file(self) -> None:
        html = self.viewer._render_recordings({})
        self.assertIn("/media/videos/sample.mp3", html)
        self.assertIn("播放", html)
        self.assertIn("小窗", html)
        self.assertIn("重命名", html)
        self.assertIn("删除", html)
        self.assertIn("本地可用", html)
        self.assertNotIn("文件丢失", html)

    def test_storage_view_reports_orphans_without_a_bulk_cleanup(self) -> None:
        stray = self.data / "videos" / "stray.mp3"
        stray.write_bytes(b"s" * 4096)

        html = self.viewer._render_storage({})
        self.assertIn("孤儿文件", html)
        self.assertIn("videos/stray.mp3", html)
        self.assertIn("可重建缓存", html)
        self.assertIn("自有文件", html)
        self.assertIn("存储占用", html)
        # The healthy recording is accounted for, not called an orphan.
        self.assertNotIn("videos/sample.mp3", html)
        # Explicit-only: the scan wires no destructive control at all, and
        # there is deliberately no "clean everything" affordance.
        self.assertNotIn('onclick="return deleteMedia', html)
        self.assertNotIn('onclick="return forgetMedia', html)
        for forbidden in ("清理", "清空", "全部删除", "一键"):
            self.assertNotIn(forbidden, html, forbidden)
        self.assertTrue(stray.is_file())


class LocalMediaIntegrityRouteTests(unittest.TestCase):
    """Route-level behaviour for the missing / orphan repairs.

    These run against the real HTTP server because the acceptance criteria are
    about what an endpoint does to the database *and* to the disk at the same
    time — a rendered page alone cannot prove the file survived.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data = root / "data"
        self.data.mkdir()
        (self.data / "videos").mkdir()
        self.config = AppConfig(
            config_path=root / "config.yaml",
            output_dir=self.data,
            database_path=self.data / "archive.sqlite3",
        )
        self.viewer = ArchiveViewer(self.config)
        self.source = "https://asmrlib.com/posts/22222222222222222222222222222222"
        self.viewer.db.add_seed(self.source)
        self.viewer.db.update_item_result(
            self.source,
            title="Route sample",
            published_at="2026-07-01",
            cover="",
            status="archived",
            html_path=str(self.data / "html" / "sample.html"),
        )
        self.media_path = self.data / "videos" / "route.mp3"
        self.media_path.write_bytes(b"ID3route")
        self.media_id = self.viewer.db.attach_local_media(
            self.source,
            file_path=str(self.media_path),
            label="Route",
            media_url="u-route",
        )
        self.server = self.viewer.create_http_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.viewer.close()
        self.temp_dir.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        conn.close()
        return result

    def row(self):
        return self.viewer.db.conn.execute(
            "SELECT file_path, status FROM media_candidates WHERE id = ?",
            (self.media_id,),
        ).fetchone()

    def count_rows(self) -> int:
        return int(
            self.viewer.db.conn.execute(
                "SELECT COUNT(*) FROM media_candidates"
            ).fetchone()[0]
        )

    def test_forget_drops_the_row_and_leaves_the_file_on_disk(self) -> None:
        self.assertTrue(self.media_path.is_file())
        status, _, payload = self.request(
            "POST", "/recordings/forget", {"id": self.media_id}
        )
        self.assertEqual(status, 200)
        body = json.loads(payload)
        self.assertTrue(body["ok"])
        self.assertTrue(body["row_removed"])
        # It must not claim a file removal: nothing was deleted.
        self.assertNotIn("file_removed", body)
        self.assertEqual(self.count_rows(), 0)
        self.assertIsNone(self.row())
        self.assertTrue(self.media_path.is_file())
        self.assertEqual(self.media_path.read_bytes(), b"ID3route")

    def test_forget_also_works_when_the_file_is_already_gone(self) -> None:
        self.media_path.unlink()
        status, _, payload = self.request(
            "POST", "/recordings/forget", {"id": self.media_id}
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["ok"])
        self.assertEqual(self.count_rows(), 0)
        self.assertFalse(self.media_path.exists())

    def test_relink_points_a_missing_record_at_a_real_file(self) -> None:
        self.media_path.unlink()
        replacement = self.data / "videos" / "renamed.mp3"
        replacement.write_bytes(b"ID3renamed")
        self.assertIn("文件丢失", self.viewer._render_recordings({}))

        status, _, payload = self.request(
            "POST",
            "/recordings/relink",
            {"id": self.media_id, "path": "videos/renamed.mp3"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["ok"])
        row = self.row()
        self.assertEqual(Path(row["file_path"]), replacement.resolve())
        self.assertEqual(row["status"], "downloaded")

        # Repaired for real: the record is servable and the file is no longer
        # an unreferenced orphan.
        report = audit_local_media(self.viewer.db, self.viewer.root)
        self.assertEqual(report.missing, ())
        self.assertEqual(report.orphans, ())
        html = self.viewer._render_recordings({})
        self.assertNotIn("文件丢失", html)
        self.assertIn("/media/videos/renamed.mp3", html)
        self.assertTrue(replacement.is_file())

    def test_relink_refuses_a_path_outside_the_managed_root(self) -> None:
        self.media_path.unlink()
        outside = Path(self.temp_dir.name) / "outside.mp3"
        outside.write_bytes(b"ID3outside")
        status, _, payload = self.request(
            "POST",
            "/recordings/relink",
            {"id": self.media_id, "path": "../outside.mp3"},
        )
        self.assertEqual(status, 200)
        body = json.loads(payload)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "库内找不到这个文件")
        # The record stays broken rather than pointing at an unservable path.
        self.assertEqual(str(self.row()["file_path"]), str(self.media_path))

    def test_delete_still_removes_a_healthy_file(self) -> None:
        status, _, payload = self.request(
            "POST", "/recordings/delete", {"id": self.media_id}
        )
        self.assertEqual(status, 200)
        body = json.loads(payload)
        self.assertTrue(body["ok"])
        self.assertTrue(body["file_removed"])
        self.assertEqual(self.count_rows(), 0)
        self.assertFalse(self.media_path.exists())

    def test_storage_route_serves_the_read_only_audit(self) -> None:
        stray = self.data / "videos" / "stray.mp3"
        stray.write_bytes(b"s" * 2048)
        # Break the record so the page has both halves of the audit to show:
        # a missing record and the orphan that can be attached to it.
        self.media_path.unlink()
        status, headers, payload = self.request("GET", "/storage")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        html = payload.decode("utf-8")
        self.assertIn("文件丢失", html)
        self.assertIn("孤儿文件", html)
        self.assertIn("videos/stray.mp3", html)
        self.assertIn("关联到记录", html)
        self.assertIn("可重建缓存", html)
        self.assertIn("自有文件", html)
        # A plain GET scans and reports; it never repairs or deletes.
        self.assertTrue(stray.is_file())
        self.assertEqual(self.count_rows(), 1)


if __name__ == "__main__":
    unittest.main()
