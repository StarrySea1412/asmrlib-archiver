from __future__ import annotations

import contextlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from asmrlib_archiver.media_save import save_media
from asmrlib_archiver.online_shield import (
    GuardedWebViewPlayer,
    ShieldPolicy,
    attach_webview_shield,
    build_page_bootstrap,
)
from tests.test_online_shield import _FakeArgs, _FakeCore


class _FakeMessageArgs:
    def __init__(self, payload: dict) -> None:
        self.WebMessageAsJson = json.dumps(payload)


class CinemaToolbarSniffTests(unittest.TestCase):
    def test_bootstrap_contains_cinema_toolbar_and_sniffer(self) -> None:
        script = build_page_bootstrap(ShieldPolicy())
        for marker in (
            "asmrlib-shield-bar",
            "__asmrlibShieldMedia",
            "__asmrlibShieldCinema",
            "__asmrlibShieldToast",
            "__asmrlibShieldTick",
            "requestPictureInPicture",
            "asmrlib-save",
            "m3u8",
            "data-asmrlib-cinema-player",
        ):
            self.assertIn(marker, script)

    def test_sniffs_media_urls_and_dedupes(self) -> None:
        core = _FakeCore()
        binding = attach_webview_shield(target=core, policy=ShieldPolicy())
        request = _FakeArgs("https://cdn.example/video/index.m3u8", "media")
        binding._on_resource(core, request)
        binding._on_resource(core, _FakeArgs("https://cdn.example/video/index.m3u8", "media"))
        binding._on_resource(core, _FakeArgs("https://cdn.example/page.html", "document"))
        self.assertEqual(binding.sniffed_media, ["https://cdn.example/video/index.m3u8"])
        binding.detach()

    def test_save_message_starts_background_worker(self) -> None:
        core = _FakeCore()
        binding = attach_webview_shield(target=core, policy=ShieldPolicy(), save_dir=Path("tmp-saves"))
        seen: list[str] = []
        done = threading.Event()

        def fake_worker(url: str) -> None:
            seen.append(url)
            done.set()

        binding._save_worker = fake_worker  # type: ignore[method-assign]
        binding._on_web_message(core, _FakeMessageArgs({"type": "asmrlib-save", "url": "https://cdn.example/a.mp4"}))
        self.assertTrue(done.wait(5), "save worker was never started")
        self.assertEqual(seen, ["https://cdn.example/a.mp4"])
        self.assertEqual(binding.sniffed_media[0], "https://cdn.example/a.mp4")
        binding.detach()

    def test_save_message_falls_back_to_latest_sniff(self) -> None:
        core = _FakeCore()
        binding = attach_webview_shield(target=core, policy=ShieldPolicy())
        seen: list[str] = []
        done = threading.Event()

        def fake_worker(url: str) -> None:
            seen.append(url)
            done.set()

        binding._save_worker = fake_worker  # type: ignore[method-assign]
        binding.record_sniff("https://cdn.example/index.m3u8")
        binding._on_web_message(core, _FakeMessageArgs({"type": "asmrlib-save", "url": ""}))
        self.assertTrue(done.wait(5))
        self.assertEqual(seen, ["https://cdn.example/index.m3u8"])
        binding.detach()

    def test_save_message_without_media_reports_no_media(self) -> None:
        core = _FakeCore()
        binding = attach_webview_shield(target=core, policy=ShieldPolicy())
        binding._on_web_message(core, _FakeMessageArgs({"type": "asmrlib-save", "url": ""}))
        self.assertEqual(binding.sniffed_media, [])
        self.assertTrue(all(not task.get("started") for task in binding.save_results().values()))
        binding.detach()

    def test_player_accepts_save_dir(self) -> None:
        player = GuardedWebViewPlayer(save_dir=Path("x"))
        self.assertEqual(player.save_dir, Path("x"))
        binding = attach_webview_shield(target=_FakeCore(), policy=ShieldPolicy(), save_dir=Path("y"))
        self.assertEqual(binding.save_dir, Path("y"))


class _FakeResponse:
    pass


class FakeTransport(httpx.BaseTransport):
    """httpx mock transport serving a small plain HLS playlist."""

    def __init__(self, segments: dict[str, bytes], playlist: str, playlist_url: str) -> None:
        self.segments = segments
        self.playlist = playlist
        self.playlist_url = playlist_url

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == self.playlist_url:
            return httpx.Response(200, text=self.playlist)
        body = self.segments.get(url)
        if body is None:
            return httpx.Response(404, text="missing")
        return httpx.Response(200, content=body)


class RouteTransport(httpx.BaseTransport):
    """Offline transport serving an explicit ``url -> response`` map."""

    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        response = self.routes.get(url)
        if response is None:
            return httpx.Response(404, text="missing", request=request)
        return response


@contextlib.contextmanager
def _routed_client(transport: httpx.BaseTransport):
    """Force every ``httpx.Client`` built by media_save onto ``transport``."""

    import asmrlib_archiver.media_save as ms

    original_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs.pop("transport", None)
        original_init(self, *args, transport=transport, **kwargs)

    ms.httpx.Client.__init__ = patched_init  # type: ignore[method-assign]
    try:
        yield
    finally:
        ms.httpx.Client.__init__ = original_init  # type: ignore[method-assign]


class MediaSaveTests(unittest.TestCase):
    def test_save_plain_hls_playlist(self) -> None:
        playlist_url = "https://cdn.example/v/index.m3u8"
        playlist = "#EXTM3U\n#EXT-X-TARGETDURATION:4\nseg0.ts\nseg1.ts\n"
        segments = {
            "https://cdn.example/v/seg0.ts": b"AAAA",
            "https://cdn.example/v/seg1.ts": b"BBBB",
        }
        transport = FakeTransport(segments, playlist, playlist_url)

        # Patch httpx.Client inside media_save to use the fake transport.
        import asmrlib_archiver.media_save as ms

        original_init = httpx.Client.__init__

        def patched_init(self, *args, **kwargs):
            kwargs.pop("transport", None)
            original_init(self, *args, transport=transport, **kwargs)

        ms.httpx.Client.__init__ = patched_init  # type: ignore[method-assign]
        try:
            result = ms.save_media(playlist_url, save_dir=Path("tmp-hls-test"))
        finally:
            ms.httpx.Client.__init__ = original_init  # type: ignore[method-assign]
        self.assertTrue(result["ok"], result)
        output = Path(str(result["path"]))
        self.assertEqual(output.read_bytes(), b"AAAABBBB")
        output.unlink(missing_ok=True)

    def test_rejects_non_http_urls(self) -> None:
        result = save_media("file:///C:/Windows/system.ini")
        self.assertFalse(result["ok"])

    def test_policy_check_blocks_ad_hosts(self) -> None:
        result = save_media(
            "https://ads.example/video.m3u8",
            allow_media=lambda url: False,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "blocked_by_policy")

    def test_segment_redirect_to_blocked_host_is_refused(self) -> None:
        # A segment on an allowed host answers 302 to a host the policy layer
        # would refuse. With follow_redirects=True the download silently escaped
        # allow_media; the redirect must now be re-checked per hop.
        playlist_url = "https://cdn.example/v/index.m3u8"
        routes = {
            playlist_url: httpx.Response(200, text="#EXTM3U\nseg0.ts\n"),
            "https://cdn.example/v/seg0.ts": httpx.Response(
                302, headers={"location": "https://ads.example/seg0.ts"}
            ),
            "https://ads.example/seg0.ts": httpx.Response(200, content=b"AD"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            with _routed_client(RouteTransport(routes)):
                result = save_media(
                    playlist_url,
                    save_dir=Path(tmp),
                    allow_media=lambda url: (urlsplit(url).hostname or "") == "cdn.example",
                )
            self.assertFalse(result["ok"], result)
            self.assertEqual(list(Path(tmp).glob("*.ts")), [])

    def test_allowed_redirect_is_followed(self) -> None:
        # Real CDNs bounce segments to another allowed host; that must keep working.
        playlist_url = "https://cdn.example/v/index.m3u8"
        routes = {
            playlist_url: httpx.Response(200, text="#EXTM3U\nseg0.ts\nseg1.ts\n"),
            "https://cdn.example/v/seg0.ts": httpx.Response(
                302, headers={"location": "//edge.example/v/seg0.ts"}
            ),
            "https://edge.example/v/seg0.ts": httpx.Response(200, content=b"AAAA"),
            "https://cdn.example/v/seg1.ts": httpx.Response(200, content=b"BBBB"),
        }
        allowed = {"cdn.example", "edge.example"}
        with tempfile.TemporaryDirectory() as tmp:
            with _routed_client(RouteTransport(routes)):
                result = save_media(
                    playlist_url,
                    save_dir=Path(tmp),
                    allow_media=lambda url: (urlsplit(url).hostname or "") in allowed,
                )
            self.assertTrue(result["ok"], result)
            self.assertEqual(Path(str(result["path"])).read_bytes(), b"AAAABBBB")

    def test_segment_returning_html_is_refused(self) -> None:
        # An ad interstitial / error page served with 200 must never be written
        # into the output and reported as a successful save.
        playlist_url = "https://cdn.example/v/index.m3u8"
        html = b"<!DOCTYPE html><html><body>ad</body></html>"
        for content_type in ("text/html; charset=utf-8", "application/octet-stream"):
            with self.subTest(content_type=content_type):
                routes = {
                    playlist_url: httpx.Response(200, text="#EXTM3U\nseg0.ts\n"),
                    "https://cdn.example/v/seg0.ts": httpx.Response(
                        200, content=html, headers={"content-type": content_type}
                    ),
                }
                with tempfile.TemporaryDirectory() as tmp:
                    with _routed_client(RouteTransport(routes)):
                        result = save_media(playlist_url, save_dir=Path(tmp))
                    self.assertFalse(result["ok"], result)
                    self.assertIn("HTML", str(result["error"]))
                    self.assertEqual(list(Path(tmp).glob("*.ts")), [])

    def test_unsupported_encryption_is_refused(self) -> None:
        # METHOD=SAMPLE-AES used to be decrypted as AES-128, producing a corrupt
        # file reported as success. It must fail loudly instead.
        playlist_url = "https://cdn.example/v/index.m3u8"
        for method in ("SAMPLE-AES", "SAMPLE-AES-CTR"):
            with self.subTest(method=method):
                playlist = f'#EXTM3U\n#EXT-X-KEY:METHOD={method},URI="key.bin"\nseg0.ts\n'
                routes = {
                    playlist_url: httpx.Response(200, text=playlist),
                    "https://cdn.example/v/key.bin": httpx.Response(
                        200, content=b"0123456789abcdef"
                    ),
                    "https://cdn.example/v/seg0.ts": httpx.Response(200, content=b"AAAA"),
                }
                with tempfile.TemporaryDirectory() as tmp:
                    with _routed_client(RouteTransport(routes)):
                        result = save_media(playlist_url, save_dir=Path(tmp))
                    self.assertFalse(result["ok"], result)
                    self.assertIn(method, str(result["error"]))
                    self.assertEqual(list(Path(tmp).glob("*.ts")), [])

    def test_session_key_playlist_is_refused(self) -> None:
        playlist_url = "https://cdn.example/v/index.m3u8"
        playlist = '#EXTM3U\n#EXT-X-SESSION-KEY:METHOD=AES-128,URI="key.bin"\nseg0.ts\n'
        routes = {
            playlist_url: httpx.Response(200, text=playlist),
            "https://cdn.example/v/seg0.ts": httpx.Response(200, content=b"AAAA"),
        }
        with tempfile.TemporaryDirectory() as tmp, _routed_client(RouteTransport(routes)):
            result = save_media(playlist_url, save_dir=Path(tmp))
        self.assertFalse(result["ok"], result)
        self.assertIn("加密", str(result["error"]))

    def test_forbidden_segment_reports_http_status(self) -> None:
        # A 403 means the link expired or was refused, not that the network is
        # flaky; the result must carry the HTTP status instead of network_error.
        playlist_url = "https://cdn.example/v/index.m3u8"
        routes = {
            playlist_url: httpx.Response(200, text="#EXTM3U\nseg0.ts\n"),
            "https://cdn.example/v/seg0.ts": httpx.Response(403, text="forbidden"),
        }
        with tempfile.TemporaryDirectory() as tmp, _routed_client(RouteTransport(routes)):
            result = save_media(playlist_url, save_dir=Path(tmp))
        self.assertFalse(result["ok"], result)
        self.assertNotEqual(result["code"], "network_error")
        self.assertEqual(result["code"], "http_status_error")
        self.assertEqual(result["status"], 403)

    def test_direct_file_redirect_to_blocked_host_is_refused(self) -> None:
        # The direct-file branch streams for progress, so it cannot use _get.
        # It must not let httpx auto-follow either: an allowed host answering
        # 302 to a blocked host is the same access-control bypass as in HLS.
        raw = "https://cdn.example/v/clip.mp4"
        routes = {
            raw: httpx.Response(302, headers={"location": "https://ads.example/clip.mp4"}),
            "https://ads.example/clip.mp4": httpx.Response(200, content=b"MP4DATA"),
        }
        transport = RouteTransport(routes)
        with tempfile.TemporaryDirectory() as tmp, _routed_client(transport):
            result = save_media(
                raw,
                save_dir=Path(tmp),
                allow_media=lambda url: "ads.example" not in url,
            )
            self.assertFalse(result["ok"], result)
            self.assertEqual(list(Path(tmp).glob("*")), [])
        # The blocked hop must never even be requested.
        self.assertNotIn("https://ads.example/clip.mp4", transport.requests)

    def test_direct_file_allowed_redirect_still_downloads(self) -> None:
        # Real CDNs redirect constantly; refusing every hop would break saves.
        raw = "https://cdn.example/v/clip.mp4"
        routes = {
            raw: httpx.Response(302, headers={"location": "https://edge.example/clip.mp4"}),
            "https://edge.example/clip.mp4": httpx.Response(
                200, content=b"MP4DATA", headers={"content-type": "video/mp4"}
            ),
        }
        transport = RouteTransport(routes)
        with tempfile.TemporaryDirectory() as tmp, _routed_client(transport):
            result = save_media(raw, save_dir=Path(tmp), allow_media=lambda url: True)
            self.assertTrue(result["ok"], result)
            self.assertEqual(Path(str(result["path"])).read_bytes(), b"MP4DATA")


if __name__ == "__main__":
    unittest.main()
