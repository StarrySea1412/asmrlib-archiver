from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from asmrlib_archiver.config import AppConfig
from asmrlib_archiver.viewer import ArchiveViewer
from asmrlib_archiver.viewer.player_guard import ExternalUrlPolicy, PlayerUrlError
from desktop_main import DesktopApi, _safe_local_path


class LightRuntimeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        data = root / "data"
        data.mkdir()
        (data / "videos").mkdir()
        (data / "videos" / "sample.mp3").write_bytes(b"ID3sample")
        config = AppConfig(
            config_path=root / "config.yaml",
            output_dir=data,
            database_path=data / "archive.sqlite3",
        )
        self.viewer = ArchiveViewer(config)
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

    def request(self, method: str, path: str, body: bytes = b"") -> tuple[int, dict, bytes]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        conn.close()
        return result

    def test_retired_routes_are_branded_410(self) -> None:
        for path in (
            "/sandbox",
            "/sandbox-status?id=x",
            "/proxy?u=x",
            "/proxy-asset?u=x",
            "/p/example.com/player",
            "/watch-frame?id=x",
            "/watch-stop?id=x",
            "/record/upload?code=x",
            "/assets/legacy.js",
            "/e/legacy",
            "/embed/legacy",
        ):
            with self.subTest(path=path):
                status, headers, payload = self.request("GET", path)
                self.assertEqual(status, 410)
                self.assertIn("text/html", headers.get("Content-Type", ""))
                self.assertIn(b"410 GONE", payload)

        status, _, payload = self.request("POST", "/record/upload", b"{}")
        self.assertEqual(status, 410)
        self.assertIn(b"410 GONE", payload)

    def test_local_media_and_recordings_management_remain_available(self) -> None:
        status, _, payload = self.request("GET", "/media/videos/sample.mp3")
        self.assertEqual(status, 200)
        self.assertEqual(payload, b"ID3sample")

        local_src = quote("/media/videos/sample.mp3", safe="")
        status, _, payload = self.request(
            "GET", f"/watch-local?src={local_src}&title=Sample&mini=1"
        )
        self.assertEqual(status, 200)
        self.assertIn(b"/media/videos/sample.mp3", payload)

        status, _, _ = self.request("GET", "/recordings")
        self.assertEqual(status, 200)
        for path in ("/recordings/delete", "/recordings/rename"):
            status, headers, payload = self.request("POST", path, b"{}")
            self.assertEqual(status, 200)
            self.assertIn("application/json", headers.get("Content-Type", ""))
            self.assertFalse(json.loads(payload)["ok"])

    def test_watch_is_validated_external_browser_landing_page(self) -> None:
        target = "https://bysetayico.com/e/abc?token=1"
        status, _, payload = self.request("GET", f"/watch?url={quote(target, safe='')}")
        html = payload.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("SYSTEM BROWSER", html)
        self.assertIn(target.replace("&", "&amp;"), html)
        landing = html.split("<section class='watch-shell", 1)[1].split("</section>", 1)[0]
        self.assertIn("openDesktopOnline", landing)
        self.assertIn("automatically intercepts popups", landing)
        self.assertNotIn("recordDesktopOnline", landing)

        status, _, payload = self.request(
            "GET", f"/watch?url={quote('https://evil.example/player', safe='')}"
        )
        self.assertEqual(status, 400)
        self.assertIn(b"Unable to open link", payload)


class LightDesktopApiTests(unittest.TestCase):
    def test_desktop_api_exposes_only_light_actions(self) -> None:
        public_methods = {
            name
            for name, value in DesktopApi.__dict__.items()
            if callable(value) and not name.startswith("_")
        }
        self.assertEqual(
            public_methods,
            {"open_external_url", "open_mini_player", "open_online_player"},
        )

    def test_online_player_prefers_guarded_webview(self) -> None:
        from asmrlib_archiver import online_shield

        api = DesktopApi("http://127.0.0.1:8765")
        with patch.object(online_shield, "GuardedWebViewPlayer") as player_cls:
            player_cls.return_value.open.return_value = {
                "ok": True,
                "mode": "guarded-webview",
            }
            result = api.open_online_player("https://bysetayico.com/e/abc")
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "guarded-webview")
        player_cls.return_value.open.assert_called_once()

    def test_online_player_falls_back_to_system_browser(self) -> None:
        from asmrlib_archiver import online_shield

        api = DesktopApi("http://127.0.0.1:8765")
        with patch.object(online_shield, "GuardedWebViewPlayer") as player_cls:
            player_cls.return_value.open.return_value = {
                "ok": False,
                "code": "shield_unavailable",
                "error": "no WebView2",
            }
            with patch("webbrowser.open", return_value=True) as opened:
                result = api.open_online_player("https://bysetayico.com/e/abc")
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "system-browser-fallback")
        opened.assert_called_once_with("https://bysetayico.com/e/abc", new=2)

    def test_external_url_policy_and_system_browser_handoff(self) -> None:
        api = DesktopApi("http://127.0.0.1:8765")
        with patch("webbrowser.open", return_value=True) as opened:
            result = api.open_external_url("HTTPS://BYSETAYICO.COM/e/abc")
        self.assertTrue(result["ok"])
        opened.assert_called_once_with("https://bysetayico.com/e/abc", new=2)

        blocked = api.open_external_url("https://evil.example/watch")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["code"], "host_not_allowed")

    def test_mini_player_rejects_remote_and_unrelated_local_paths(self) -> None:
        api = DesktopApi("http://127.0.0.1:8765")
        for path in (
            "https://bysetayico.com/e/abc",
            "/watch?url=https%3A%2F%2Fbysetayico.com%2Fe%2Fabc",
            "/post/example",
            "/media/../secret.mp3",
        ):
            with self.subTest(path=path):
                result = api.open_mini_player(path)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "invalid_local_path")

        local = "/watch-local?src=%2Fmedia%2Fvideos%2Fsample.mp3&title=Sample&mini=1"
        self.assertEqual(
            _safe_local_path("http://127.0.0.1:8765", local),
            f"http://127.0.0.1:8765{local}",
        )

    def test_player_policy_rejects_credentials_controls_and_ads(self) -> None:
        policy = ExternalUrlPolicy()
        for url in (
            "javascript:alert(1)",
            "https://user:pass@asmrlib.com/posts/x",
            "https://asmrlib.com/line\nbreak",
            "https://doubleclick.net/ad",
        ):
            with self.subTest(url=url), self.assertRaises(PlayerUrlError):
                policy.validate(url)


if __name__ == "__main__":
    unittest.main()
