from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
