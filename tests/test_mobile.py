"""Mobile deliverables: network-check endpoint, PWA manifest, APK blocklist.

The APK replays desktop shield decisions from ``android/app/src/main/assets/
blocklist.json`` — the sync between that asset and the Python security tables
is a hard guarantee, so its test fails whenever the two drift apart.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from asmrlib_archiver.config import AppConfig
from asmrlib_archiver.mobile_blocklist import write_blocklist
from asmrlib_archiver.online_shield import ShieldPolicy, export_mobile_blocklist
from asmrlib_archiver.security_lists import BLOCK_URL_KEYWORDS, BLOCKED_HOSTS
from asmrlib_archiver.viewer import ArchiveViewer
from asmrlib_archiver.viewer.ui_assets import app_manifest


class MobileNetworkCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config = AppConfig(
            config_path=root / "config.yaml",
            output_dir=root / "data",
            database_path=root / "data" / "archive.sqlite3",
        )
        self.viewer = ArchiveViewer(self.config)

    def tearDown(self) -> None:
        self.viewer.close()
        self.temp_dir.cleanup()

    def test_payload_shape_and_local_client(self) -> None:
        # Probe result shape (direct call).
        def fake(host: str, port: int, timeout: float = 2.5):
            return True, 42, ""

        def fake_tls(host: str, port: int = 443, timeout: float = 2.5):
            return True, 88, ""

        with mock.patch.object(
            ArchiveViewer, "_tcp_rtt_ms", staticmethod(fake)
        ), mock.patch.object(ArchiveViewer, "_tls_rtt_ms", staticmethod(fake_tls)):
            probed = self.viewer._probe_upstream()
        self.assertTrue(probed["ok"])
        self.assertTrue(probed["tls_ok"])
        # The banner weighs the site's TLS state separately from a player's,
        # so the payload has to name which host is the site.
        self.assertEqual(probed["site_host"], "asmrlib.com")
        # Once the handshake lands, rtt_ms reports the TLS round trip — the
        # honest "usable" latency — so 88 wins over the bare connect's 42.
        self.assertEqual(probed["best_rtt_ms"], 88)
        for entry in probed["upstream"]:
            for key in ("host", "ok", "rtt_ms", "error", "tls_ok", "tls_error"):
                self.assertIn(key, entry)
        # Endpoint payload: cold cache answers instantly with pending=True,
        # client fields are always attached.
        payload = self.viewer._network_check_payload("127.0.0.1")
        self.assertTrue(payload["pending"])
        self.assertTrue(payload["client_is_local"])
        self.assertEqual(payload["client"], "127.0.0.1")

    def test_tls_broken_network_is_reported_as_such(self) -> None:
        """Ports answer, the TLS handshake dies — the classic proxy case.

        Regression: with a TCP-connect-only probe this network looked
        perfectly healthy while every fetch failed with
        ``SSL: UNEXPECTED_EOF_WHILE_READING``, so the banner stayed silent
        and the user got a cryptic crawl error instead.
        """

        def fake(host: str, port: int, timeout: float = 2.5):
            return True, 40, ""

        def fake_tls(host: str, port: int = 443, timeout: float = 2.5):
            return False, 0, "SSLError"

        with mock.patch.object(
            ArchiveViewer, "_tcp_rtt_ms", staticmethod(fake)
        ), mock.patch.object(ArchiveViewer, "_tls_rtt_ms", staticmethod(fake_tls)):
            payload = self.viewer._probe_upstream()
        self.assertTrue(payload["ok"])  # TCP says reachable…
        self.assertFalse(payload["tls_ok"])  # …but HTTPS is not usable
        self.assertEqual(payload["site_host"], "asmrlib.com")
        for entry in payload["upstream"]:
            self.assertTrue(entry["ok"])
            self.assertFalse(entry["tls_ok"])
            self.assertEqual(entry["tls_error"], "SSLError")

    def test_best_rtt_ignores_hosts_whose_tls_failed(self) -> None:
        """A fast connect to a TLS-broken host is not a usable upstream."""

        def fake(host: str, port: int, timeout: float = 2.5):
            # Site: fast connect. Players: slower but actually usable.
            return True, 40 if host == "asmrlib.com" else 300, ""

        def fake_tls(host: str, port: int = 443, timeout: float = 2.5):
            if host == "asmrlib.com":
                return False, 0, "ConnectionResetError"
            return True, 420, ""

        with mock.patch.object(
            ArchiveViewer, "_tcp_rtt_ms", staticmethod(fake)
        ), mock.patch.object(ArchiveViewer, "_tls_rtt_ms", staticmethod(fake_tls)):
            payload = self.viewer._probe_upstream()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["tls_ok"])
        # 40ms came from the host that cannot actually be used — 420 wins.
        self.assertEqual(payload["best_rtt_ms"], 420)
        site = next(e for e in payload["upstream"] if e["host"] == "asmrlib.com")
        self.assertTrue(site["ok"])
        self.assertFalse(site["tls_ok"])

    def test_tls_is_skipped_when_tcp_already_failed(self) -> None:
        calls = []

        def fake(host: str, port: int, timeout: float = 2.5):
            return False, 0, "TimeoutError"

        def fake_tls(host: str, port: int = 443, timeout: float = 2.5):
            calls.append(host)
            return True, 10, ""

        with mock.patch.object(
            ArchiveViewer, "_tcp_rtt_ms", staticmethod(fake)
        ), mock.patch.object(ArchiveViewer, "_tls_rtt_ms", staticmethod(fake_tls)):
            payload = self.viewer._probe_upstream()
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["tls_ok"])
        self.assertEqual(calls, [])  # no point handshaking a dead port

    def test_probe_hosts_lead_with_site_domain(self) -> None:
        hosts = self.viewer._net_probe_hosts()
        self.assertTrue(hosts)
        self.assertEqual(hosts[0], "asmrlib.com")
        self.assertLessEqual(len(hosts), 5)

    def test_upstream_failure_is_reported_not_raised(self) -> None:
        def fail(host: str, port: int, timeout: float = 2.5):
            raise OSError("no route")

        with mock.patch.object(
            ArchiveViewer, "_tcp_rtt_ms", staticmethod(fail)
        ), mock.patch.object(ArchiveViewer, "_tls_rtt_ms", staticmethod(fail)):
            payload = self.viewer._probe_upstream()
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["tls_ok"])
        self.assertTrue(all(not entry["ok"] for entry in payload["upstream"]))

    def test_background_refresh_serves_stale_then_fresh(self) -> None:
        calls = []

        def fake(host: str, port: int, timeout: float = 2.5):
            calls.append(host)
            return True, 10, ""

        def fake_tls(host: str, port: int = 443, timeout: float = 2.5):
            return True, 12, ""

        with mock.patch.object(
            ArchiveViewer, "_tcp_rtt_ms", staticmethod(fake)
        ), mock.patch.object(ArchiveViewer, "_tls_rtt_ms", staticmethod(fake_tls)):
            first = self.viewer._network_check_payload("127.0.0.1")
            self.assertTrue(first["pending"])  # instant, no blocking probe
            self.viewer._net_refresh_worker()  # what the thread runs
            second = self.viewer._network_check_payload("127.0.0.1")
        self.assertFalse(second.get("pending"))
        self.assertTrue(second["ok"])
        # The endpoint's own background kick plus the manual worker run means
        # each host was probed once or twice — never more (single-flight).
        self.assertLessEqual(len(calls), len(second["upstream"]) * 2)

    def test_page_head_carries_mobile_bits(self) -> None:
        html = self.viewer._render_home({})
        self.assertIn("viewport-fit=cover", html)
        self.assertIn('rel="manifest"', html)
        self.assertIn("nav-ico", html)  # bottom-tab icons
        self.assertIn("nav-label", html)
        self.assertIn('data-nav="local"', html)


class MobileManifestTests(unittest.TestCase):
    def test_manifest_is_installable(self) -> None:
        manifest = app_manifest()
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertTrue(manifest["icons"])
        self.assertEqual(manifest["icons"][0]["src"], "/ui/icon.svg")


class MobileReleaseVersionTests(unittest.TestCase):
    """The versionName is a release decision, not build debris.

    History that makes this pin necessary: an iteration of bug fixes was
    once shipped as an invented "v1.6/v1.7/v1.8" ladder (and the artifacts
    to match), all of which had to be deleted; the policy that came out of
    it was versionName moves only when the user names the release, and the
    artifact is named for it.  1.8 is that: the Eara redesign release the
    user asked for by name.  A sed through build.gradle must never move it
    silently again - versionCode, on the other hand, MUST climb every build
    (Android refuses an overwrite-install of a lower code), so only its
    direction is pinned, never its value.
    """

    def test_version_name_is_the_named_release(self) -> None:
        gradle = (
            Path(__file__).resolve().parents[1]
            / "android" / "app" / "build.gradle"
        ).read_text(encoding="utf-8")
        self.assertIn('versionName "1.8"', gradle)

    def test_version_code_climbs_never_descends(self) -> None:
        """versionCode must climb every build (Android refuses an
        overwrite-install of a lower code), so only its direction is pinned,
        never its value - 21 is the floor this release line reached."""
        gradle = (
            Path(__file__).resolve().parents[1]
            / "android" / "app" / "build.gradle"
        ).read_text(encoding="utf-8")
        match = re.search(r"versionCode\s+(\d+)", gradle)
        self.assertIsNotNone(match, "build.gradle declares no versionCode")
        self.assertGreaterEqual(int(match.group(1)), 21)


class MobileBlocklistTests(unittest.TestCase):
    def test_export_matches_python_tables(self) -> None:
        payload = export_mobile_blocklist()
        self.assertEqual(payload["version"], 1)
        self.assertEqual(
            payload["blocked_hosts"], sorted(set(BLOCKED_HOSTS))
        )
        self.assertEqual(
            payload["block_url_keywords"], sorted(set(BLOCK_URL_KEYWORDS))
        )
        # Player allow-list must not leak into the block tables.
        for host in payload["player_domains"]:
            self.assertNotIn(host, payload["blocked_hosts"])

    def test_policy_from_config_is_honored(self) -> None:
        policy = ShieldPolicy(
            blocked_hosts=("ads.example.com",),
            block_url_keywords=("adserver",),
            allowed_domains=("asmrlib.com",),
        )
        payload = export_mobile_blocklist(policy)
        self.assertEqual(payload["blocked_hosts"], ["ads.example.com"])
        self.assertEqual(payload["block_url_keywords"], ["adserver"])

    def test_written_asset_is_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "blocklist.json"
            path = write_blocklist(target)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("blocked_hosts", data)
        self.assertIn("allow_subdomains", data)

    def test_committed_apk_asset_is_in_sync(self) -> None:
        repo_asset = (
            Path(__file__).resolve().parents[1]
            / "android"
            / "app"
            / "src"
            / "main"
            / "assets"
            / "blocklist.json"
        )
        if not repo_asset.is_file():  # pragma: no cover - packaging guard
            self.fail(
                "android/app/src/main/assets/blocklist.json is missing; "
                "run: python -m asmrlib_archiver.mobile_blocklist"
            )
        on_disk = json.loads(repo_asset.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, export_mobile_blocklist())


class MainActivityDirectOnlineTests(unittest.TestCase):
    """The gate's 线上模式: bundled 发现页 (our UI, live site data), no PC."""

    def setUp(self) -> None:
        self.java_dir = (
            Path(__file__).resolve().parents[1]
            / "android"
            / "app"
            / "src"
            / "main"
            / "java"
            / "com"
            / "asmrlib"
            / "archiver"
        )

    def _read(self, name: str) -> str:
        source = self.java_dir / name
        if not source.is_file():  # pragma: no cover - packaging guard
            self.fail(f"{name} is missing from the APK shell sources")
        return source.read_text(encoding="utf-8")

    def test_app_opens_the_bundled_discover_page_directly(self) -> None:
        text = self._read("MainActivity.java")
        # There is no server gate any more: the APK is standalone and the
        # first thing the user sees is the bundled 发现页, never a
        # "which PC are we talking to" form.
        self.assertNotIn("showGate", text)
        self.assertNotIn("电脑收藏馆", text)
        self.assertNotIn("app://pc-library", text)
        self.assertIn("openDiscover", text)
        self.assertIn("file:///android_asset/discover.html", text)
        # The bridge is bound once per WebView lifetime: removing it for
        # remote pages would orphan restored history pages (back-nav breaks).
        self.assertIn("discoverBridgeActive", text)
        self.assertNotIn("removeJavascriptInterface", text)

    def test_apk_supports_real_video_fullscreen_and_loading_screen(self) -> None:
        text = self._read("MainActivity.java")
        # A bare WebView ignores the HTML5 fullscreen button; the host must
        # accept the custom video view through WebChromeClient.
        for marker in (
            "setWebChromeClient(new MediaChromeClient())",
            "onShowCustomView",
            "onHideCustomView",
            "SCREEN_ORIENTATION_SENSOR_LANDSCAPE",
            "SYSTEM_UI_FLAG_IMMERSIVE_STICKY",
            "if (customView != null)",
            "exitFullscreen()",
            # Native DownloadManager only receives a URL observed by the
            # shielded WebView; there is intentionally no JS save(url) bridge.
            "WebResourceResponse blocked = shield.intercept(view, request)",
            "return blocked;",
            "captureDirectMedia(uri.toString())",
            "DownloadManager.Request",
            "isDirectDownloadUrl",
            "setDestinationInExternalPublicDir",
            # This block used to assert the literal comment "HLS/DASH need a
            # segment-aware task, not DownloadManager". That comment described a
            # refusal — honest, but a hard capability hole, since HLS is the
            # site's main playback format and handing an .m3u8 to
            # DownloadManager saves only the playlist. The segment-aware task
            # now exists, so the refusal is deliberately gone and .m3u8 URLs
            # are routed to the in-app job instead. Behaviour is proven by
            # execution in tests/test_hls_downloader_java.py.
            "isHlsUrl",
            "HlsDownloader.download",
        ):
            self.assertIn(marker, text)
        # The refusal has to come *before* the URL is offered to the downloader,
        # or an advert with an .mp4 suffix makes the save action appear. The
        # early return used to be a one-liner; it now also records the frame it
        # explained, so the ordering is what gets pinned rather than the shape.
        self.assertLess(
            text.index("return blocked;"),
            text.index("captureDirectMedia(uri.toString())"),
        )
        # The loading surface must be added after setContentView (otherwise
        # setContentView removes it immediately) and driven by actual progress.
        self.assertIn("attachLoadingChrome();", text)
        self.assertIn("onProgressChanged", text)
        self.assertIn("progressBar.setProgress(newProgress)", text)
        # The renderer warm-up can emit a late 100% callback.  The shell
        # must keep the real navigation's splash up for a minimum interval
        # and reject stale completion callbacks by navigation generation.
        self.assertIn("MIN_SPLASH_MS = 420L", text)
        self.assertIn("splashGeneration", text)
        self.assertIn("SystemClock.elapsedRealtime()", text)
        self.assertIn("正在连接收藏馆", text)
        self.assertLess(
            text.index("setContentView(webView);", text.index("webView = new WebView")),
            text.index("attachLoadingChrome();", text.index("webView = new WebView")),
        )

    def test_discover_bridge_parses_site_listings(self) -> None:
        text = self._read("DiscoverBridge.java")
        self.assertIn("@JavascriptInterface", text)
        self.assertIn("Jsoup.connect", text)
        # Card rules mirror parser.py; the raw site stays available as the
        # page's fallback when parsing breaks on a redesign.
        self.assertIn("https://asmrlib.com", text)
        self.assertIn('/tags/', text)
        self.assertIn("page=", text)

    def test_setup_webview_never_recreates_the_shell(self) -> None:
        # The gate that used to swap the content view is gone, so the only
        # remaining rule is that setupWebView() must reuse the existing
        # WebView — creating a second one would load the feed off-screen and
        # drop the discover → play history.
        text = self._read("MainActivity.java")
        setup = text[text.index("private void setupWebView()"):]
        setup = setup[: setup.index("// ------------------------------------------------------- loading screen")]
        self.assertIn("setContentView(webView);", setup)
        self.assertLess(
            setup.index("if (webView != null) {"),
            setup.index("setContentView(webView);"),
        )


class MobileDownloadCenterTests(unittest.TestCase):
    """The APK's 下载中心: a real, observable DownloadManager task list.

    The old flow was fire-and-forget — a Toast and then nothing.  These
    assertions pin the contract that makes the save path verifiable: persisted
    task records, real DownloadManager state, a pushed completion signal, a
    retry path, a framework FileProvider for 打开, and failure reasons that are
    never rendered as success.
    """

    def setUp(self) -> None:
        self.java_dir = (
            Path(__file__).resolve().parents[1]
            / "android"
            / "app"
            / "src"
            / "main"
            / "java"
            / "com"
            / "asmrlib"
            / "archiver"
        )
        self.manifest = (
            Path(__file__).resolve().parents[1]
            / "android"
            / "app"
            / "src"
            / "main"
            / "AndroidManifest.xml"
        )

    def _read(self, name: str) -> str:
        source = self.java_dir / name
        if not source.is_file():  # pragma: no cover - packaging guard
            self.fail(f"{name} is missing from the APK shell sources")
        return source.read_text(encoding="utf-8")

    def test_tasks_are_persisted_in_shared_preferences(self) -> None:
        text = self._read("MainActivity.java")
        # One JSON object per task, written with the framework JSONArray so no
        # new dependency is required.
        self.assertIn("KEY_DOWNLOADS = \"download_tasks\"", text)
        self.assertIn("import org.json.JSONArray;", text)
        self.assertIn("new JSONArray(raw)", text)
        self.assertIn("arr.put(downloadTasks.get(i))", text)
        self.assertIn("putString(KEY_DOWNLOADS", text)
        # The record carries the fields the panel needs to be honest about.
        for field in (
            "\"downloadId\"",
            "\"url\"",
            "\"pageUrl\"",
            "\"fileName\"",
            "\"state\"",
            "\"createdAt\"",
            "\"bytesTotal\"",
            "\"bytesSoFar\"",
        ):
            self.assertIn(field, text)

    def test_enqueue_captures_the_real_download_id(self) -> None:
        text = self._read("MainActivity.java")
        # The enqueue result is what makes a task observable; dropping it is
        # exactly the fake-success bug this replaced.
        self.assertIn("long downloadId = enqueueDownload(url, pageUrl, fileName);", text)
        self.assertIn("downloadTasks.add(newTaskRecord(downloadId", text)
        self.assertNotIn("已加入系统下载任务", text)

    def test_state_comes_from_a_real_downloadmanager_query(self) -> None:
        text = self._read("MainActivity.java")
        for marker in (
            "new DownloadManager.Query().setFilterById(id)",
            "manager.query(query)",
            "DownloadManager.COLUMN_STATUS",
            "DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR",
            "DownloadManager.COLUMN_TOTAL_SIZE_BYTES",
            "DownloadManager.COLUMN_REASON",
            "DownloadManager.COLUMN_LOCAL_URI",
            "cursor.moveToFirst()",
        ):
            self.assertIn(marker, text)
        # A record whose row is gone must be dropped, not left claiming life.
        self.assertIn("downloadTasks.remove(i);", text)

    def test_completion_is_pushed_by_a_registered_receiver(self) -> None:
        text = self._read("MainActivity.java")
        self.assertIn("DownloadManager.ACTION_DOWNLOAD_COMPLETE", text)
        self.assertIn("DownloadManager.EXTRA_DOWNLOAD_ID", text)
        self.assertIn("extends BroadcastReceiver", text)
        # API 33+ requires an explicit export flag for a context-registered
        # receiver, and the receiver must always be torn down with the activity.
        self.assertIn("Build.VERSION.SDK_INT >= 33", text)
        self.assertIn("registerReceiver(receiver, filter, Context.RECEIVER_EXPORTED)", text)
        self.assertIn("unregisterReceiver(downloadReceiver)", text)
        self.assertIn("registerDownloadReceiver();", text)
        self.assertIn("unregisterDownloadReceiver();", text)

    def test_retry_reenqueues_the_stored_url_and_replaces_the_record(self) -> None:
        text = self._read("MainActivity.java")
        self.assertIn("private void retryDownload(JSONObject task)", text)
        self.assertIn("isDirectDownloadUrl(url)", text)
        self.assertIn("downloadTasks.remove(task);", text)
        # 重试 is offered only for a failed task.
        self.assertIn("if (STATE_FAILED.equals(state)) {", text)
        self.assertIn("\"重试\"", text)
        self.assertIn("\"打开\"", text)
        self.assertIn("\"删除\"", text)
        self.assertIn("manager.remove(id)", text)

    def test_open_uses_a_fileprovider_instead_of_a_file_uri(self) -> None:
        text = self._read("MainActivity.java")
        self.assertIn("DownloadFileProvider.uriForFile(parsed)", text)
        self.assertIn("Intent.FLAG_GRANT_READ_URI_PERMISSION", text)
        provider = self._read("DownloadFileProvider.java")
        self.assertIn("extends ContentProvider", provider)
        self.assertIn("ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY)",
                      provider)
        self.assertIn("com.asmrlib.archiver.fileprovider", provider)
        # The manifest must declare it or every 打开 throws FileUriExposed.
        manifest = self.manifest.read_text(encoding="utf-8")
        self.assertIn("android:name=\".DownloadFileProvider\"", manifest)
        self.assertIn("android:authorities=\"com.asmrlib.archiver.fileprovider\"", manifest)
        self.assertIn("android:grantUriPermissions=\"true\"", manifest)

    def test_failure_reasons_are_mapped_and_never_shown_as_success(self) -> None:
        text = self._read("MainActivity.java")
        for reason in ("网络错误", "文件写入失败", "空间不足", "HTTP 错误码 "):
            self.assertIn(reason, text)
        self.assertIn("private static String reasonText(int reason)", text)
        self.assertIn("return \"失败 · \" + reasonText(", text)
        self.assertIn("DownloadManager.ERROR_INSUFFICIENT_SPACE", text)
        self.assertIn("DownloadManager.ERROR_HTTP_DATA_ERROR", text)

    def test_an_unreadable_store_is_never_written_back_as_empty(self) -> None:
        # The old loadDownloadTasks() cleared the list inside its catch block,
        # and the next poll saved that empty list — one bad read silently
        # destroyed every record. Parsing has to succeed before the in-memory
        # list is replaced, and the unreadable string is quarantined instead of
        # overwritten.
        text = self._read("MainActivity.java")
        method = text[text.index("private void loadDownloadTasks()"):]
        method = method[:method.index("private void saveDownloadTasks()")]
        self.assertNotIn("download task store corrupt, starting empty", method)
        self.assertGreater(
            method.index("downloadTasks.clear()"),
            method.index("new JSONArray(raw)"),
        )
        self.assertIn('putString(KEY_DOWNLOADS + ".bad", raw)', method)

    def test_row_actions_resolve_the_task_by_id_not_by_captured_object(self) -> None:
        # A row is rebuilt every 800ms, so the JSONObject a button was rendered
        # from may already be gone. Every action resolves the stable id against
        # the live list first and does nothing if the record has left it.
        text = self._read("MainActivity.java")
        self.assertIn("private JSONObject findDownloadTask(String taskId, long downloadId)", text)
        row = text[text.index("private View buildDownloadRow("):]
        row = row[:row.index("private Button rowButton(")]
        for action in ("openDownloadedFile(live)", "retryDownload(live)",
                       "renameDownload(live)", "removeDownloadRecord(live)",
                       "deleteDownload(live)"):
            self.assertIn(action, row)
        self.assertEqual(row.count("findDownloadTask(taskId, downloadId)"), 5)

    def test_delete_is_confirmed_and_never_acts_on_a_vanished_record(self) -> None:
        # 删除 is irreversible and can be gigabytes: it must resolve the record
        # against the live list, ask the user, and re-resolve after the dialog
        # (the dialog does not stop the poll).
        text = self._read("MainActivity.java")
        self.assertIn("private void deleteDownload(JSONObject task)", text)
        self.assertIn("private void confirmDelete(final JSONObject task)", text)
        # The product modal (productDialog): the platform AlertDialog's muted
        # pair is retired — the delete pill wears the red→rose gradient, and
        # 取消 sits beside it in its own ghost style.
        self.assertIn('setText("删除下载")', text)
        self.assertNotIn("setNegativeButton(", text)
        self.assertNotIn("setPositiveButton(", text)
        self.assertIn("private Dialog productDialog(View content)", text)
        self.assertIn("performDelete(String taskId, long downloadId)", text)
        delete = text[text.index("private void deleteDownload(JSONObject task)"):]
        self.assertLess(
            delete.index("findDownloadTask("),
            delete.index("confirmDelete(target)"),
        )

    def test_panel_is_native_and_polls_only_while_visible(self) -> None:
        text = self._read("MainActivity.java")
        # A native overlay in the decor chrome — the entry point never lives
        # inside the WebView.
        self.assertIn("private void attachDownloadEntry()", text)
        self.assertIn("\"下载中心\"", text)
        self.assertIn("decor.addView(root", text)
        self.assertIn("POLL_INTERVAL_MS = 800L", text)
        self.assertIn("mainHandler.postDelayed(this, POLL_INTERVAL_MS)", text)
        self.assertIn("startPolling();", text)
        self.assertIn("stopPolling();", text)
        # Polling must stop on hide and on onPause.
        self.assertIn("protected void onPause()", text)
        self.assertLess(
            text.index("protected void onPause()"),
            text.index("stopPolling();", text.index("protected void onPause()")),
        )

    def test_restart_recovery_replays_persisted_tasks(self) -> None:
        text = self._read("MainActivity.java")
        self.assertIn("loadDownloadTasks();", text)
        self.assertIn("registerDownloadReceiver();", text)
        self.assertIn("refreshDownloadStates();", text)
        # In onCreate the stored tasks are loaded first, then replayed against
        # DownloadManager so running tasks come back with real progress.
        on_create = text.index("protected void onCreate(")
        self.assertLess(
            text.index("loadDownloadTasks();", on_create),
            text.index("refreshDownloadStates();", on_create),
        )

    def test_ad_shield_and_bridge_surfaces_are_untouched(self) -> None:
        text = self._read("MainActivity.java")
        # Do not regress the shield gate, the one-time bridge binding, the
        # fullscreen host or the loading splash.
        self.assertIn("WebResourceResponse blocked = shield.intercept(view, request)", text)
        self.assertIn("captureDirectMedia(uri.toString())", text)
        self.assertIn("discoverBridgeActive", text)
        self.assertNotIn("removeJavascriptInterface", text)
        self.assertIn("onShowCustomView", text)
        self.assertIn("onHideCustomView", text)
        self.assertIn("attachLoadingChrome();", text)
        # The HLS refusal that used to be pinned here is replaced by a real
        # segment-aware downloader; DownloadManager stays direct-only.
        self.assertIn("isHlsUrl", text)
        self.assertIn("HlsDownloader", text)
        self.assertIn("if (customView != null)", text)


class MobileHlsDownloadTests(unittest.TestCase):
    """The APK's real HLS save path.

    DownloadManager cannot save an HLS stream — handed an .m3u8 it stores the
    playlist text and reports success.  ``HlsDownloader`` is the segment-aware,
    resumable task the old refusal comment promised, and these assertions pin
    the wiring that makes the phone actually use it.  The behaviour itself is
    proven by execution, not by these markers: see
    ``tests/test_hls_downloader_java.py``, which compiles the real class with
    javac and runs it against a live HLS tree.
    """

    def setUp(self) -> None:
        self.java_dir = (
            Path(__file__).resolve().parents[1]
            / "android"
            / "app"
            / "src"
            / "main"
            / "java"
            / "com"
            / "asmrlib"
            / "archiver"
        )

    def _read(self, name: str) -> str:
        source = self.java_dir / name
        if not source.is_file():  # pragma: no cover - packaging guard
            self.fail(f"{name} is missing from the APK shell sources")
        return source.read_text(encoding="utf-8")

    def test_downloader_has_no_android_dependency(self) -> None:
        # This constraint is what lets the JVM test execute the real class
        # instead of merely reading it. Losing it would silently turn the only
        # end-to-end proof of this feature back into a source inspection.
        source = self._read("HlsDownloader.java")
        self.assertNotIn("import android.", source)
        self.assertNotIn("import org.json", source)
        self.assertNotIn("android.util.Log", source)
        for marker in (
            "import java.io.",
            "import java.net.HttpURLConnection;",
            "javax.crypto.Cipher",
        ):
            self.assertIn(marker, source)

    def test_refusals_are_named_rather_than_generic(self) -> None:
        source = self._read("HlsDownloader.java")
        # A live playlist saved offline is a truncated file presented as a
        # complete one; SAMPLE-AES decrypted as AES-128 is a corrupt file
        # presented as a good one. Each has to be refused by name.
        self.assertIn("EXT-X-ENDLIST", source)
        self.assertIn("直播", source)
        self.assertIn("EXT-X-SESSION-KEY", source)
        self.assertIn("SAMPLE-AES", source)
        self.assertIn("AES-128", source)
        self.assertIn("返回了 HTML 而非媒体数据", source)
        # 403/410 is an expired link, not a flaky connection.
        self.assertIn("媒体链接已失效或被拒绝", source)

    def test_redirects_are_resolved_by_hand(self) -> None:
        source = self._read("HlsDownloader.java")
        # An auto-followed redirect is an access-control bypass: the policy
        # only ever saw the declared URL, never the hop it landed on.
        self.assertIn("setInstanceFollowRedirects(false)", source)
        self.assertIn("MAX_REDIRECTS", source)
        self.assertIn("重定向目标未通过安全策略", source)

    def test_segments_are_cached_for_resume_and_decrypted_in_place(self) -> None:
        source = self._read("HlsDownloader.java")
        self.assertIn(".parts", source)
        self.assertIn('"seg-" + index + ".part"', source)
        self.assertIn("AES/CBC/NoPadding", source)
        # Success is only claimed once the bytes are actually on disk.
        self.assertIn("dest.length() <= 0L", source)

    def test_activity_routes_m3u8_to_the_in_app_job(self) -> None:
        text = self._read("MainActivity.java")
        self.assertIn("private static boolean isHlsUrl(String raw)", text)
        self.assertIn("verifiedHlsUrl", text)
        self.assertIn("HlsDownloader.download", text)
        # 保存媒体 must dispatch rather than hand the manifest to
        # DownloadManager, which would save only the playlist.
        self.assertIn("if (isHlsUrl(verifiedHlsUrl)) {", text)
        self.assertIn("startHlsDownload(verifiedHlsUrl", text)
        # A navigation must forget the previous page's manifest.
        self.assertIn("verifiedHlsUrl = \"\";", text)

    def test_hls_records_carry_a_kind_and_survive_a_refresh(self) -> None:
        text = self._read("MainActivity.java")
        for marker in (
            'o.put("kind", KIND_HLS);',
            'o.put("kind", KIND_DIRECT);',
            "newHlsTaskRecord",
            'o.put("taskId", taskId);',
            'o.put("localPath", localPath);',
            'o.put("segmentsDone", 0);',
            'o.put("segmentsTotal", 0);',
            'o.put("error", "");',
            "private static boolean isHlsTask(JSONObject task)",
        ):
            self.assertIn(marker, text)
        # A record written before HLS existed has no "kind"; defaulting it to
        # "direct" is what keeps an already-populated phone working.
        self.assertIn(
            'if (!KIND_HLS.equals(o.optString("kind", ""))) o.put("kind", KIND_DIRECT);',
            text,
        )
        # refreshDownloadStates drops every record whose DownloadManager id has
        # no cursor row. An HLS task has no such row by design, so without this
        # guard the first refresh deletes every HLS task — the single most
        # likely way to break this feature.
        refresh = text.index("private void refreshDownloadStates()")
        guard = text.index("if (isHlsTask(task)) continue;", refresh)
        self.assertLess(guard, text.index("new DownloadManager.Query()", refresh))

    def test_hls_output_is_app_private_mpeg_ts(self) -> None:
        text = self._read("MainActivity.java")
        # Package-private so DownloadsActivity can reuse the same offline
        # directory without duplicating the literal. Accept either
        # declaration — the field is final either way.
        self.assertRegex(
            text,
            r"static final String OFFLINE_DIR\s*=\s*\"offline\";",
        )
        self.assertIn("new File(base, OFFLINE_DIR)", text)
        self.assertIn("getExternalFilesDir(null)", text)
        # The output is concatenated MPEG-TS, so both the extension and the
        # reported MIME type have to say so — calling it .mp4 would be a lie
        # about the container.
        self.assertIn("return uniqueDest(offlineDir(), stem, \".ts\");", text)
        self.assertIn('if (n.endsWith(".ts")) return "video/mp2t";', text)

    def test_hls_output_name_comes_from_the_title_not_a_timestamp(self) -> None:
        text = self._read("MainActivity.java")
        self.assertIn("private File hlsDestination(String url)", text)
        self.assertIn("String stem = suggestedStem(url);", text)
        # Uniqueness is a visible " (2)" suffix, not an opaque stamp. A stamp
        # made resume unreachable: the parts cache is keyed by destination
        # path, so a new stamp meant a new directory and a restart from
        # segment 0 with the previous run's segments orphaned.
        self.assertIn("private static File uniqueDest(File dir, String stem, String ext)", text)
        self.assertIn('candidate = new File(dir, stem + " (" + n + ")" + ext);', text)
        # A .parts/.part sibling counts as taken — reusing a name whose
        # segments are still on disk would resume a different download from
        # stale bytes.
        self.assertIn('new File(dir, stem + ext + ".parts").exists()', text)
        self.assertIn('new File(dir, stem + ext + ".part").exists()', text)
        self.assertNotIn(
            'return stem + "-" + System.currentTimeMillis() + ".ts";', text
        )

    def test_hls_task_actions_can_open_cancel_and_delete(self) -> None:
        text = self._read("MainActivity.java")
        self.assertIn("private void startHlsJob(final JSONObject task)", text)
        # The registry is process-wide now, not a MainActivity field: the
        # download centre is a different Activity and has to be able to stop a
        # worker this one started. Asserted on the shared holder rather than on
        # a `Map<String, HlsJob> hlsJobs` field, which no longer exists — the
        # semantics (a running job can be reached and cancelled by id) are
        # unchanged, only where the map lives.
        self.assertIn("HlsJobs.put(taskId, job)", text)
        self.assertIn("HlsJobs.cancel(taskId)", text)
        registry = self._read("HlsJobs.java")
        self.assertIn("job.cancelled = true;", registry)
        self.assertIn("volatile boolean cancelled;", registry)
        self.assertIn("public static void cancel(String taskId)", registry)
        # Progress is posted back to the UI thread rather than touched from the
        # worker.
        self.assertIn("mainHandler.post(new Runnable()", text)
        self.assertIn(
            'return "已完成 " + task.optInt("segmentsDone", 0) + " / "', text
        )
        # 打开 serves the app-private file through the provider; 删除 clears
        # the resume directory so a later save cannot reuse stale segments.
        self.assertIn("DownloadFileProvider.uriForFile(file)", text)
        self.assertIn('deleteTree(new File(localPath + ".parts"))', text)

    def test_file_provider_serves_app_private_offline_files(self) -> None:
        provider = self._read("DownloadFileProvider.java")
        self.assertIn("public static Uri uriForFile(File file)", provider)
        self.assertIn('private static final String OFFLINE = "offline";', provider)
        self.assertIn("getExternalFilesDir(null)", provider)
        # Only a bare file name is ever served, never a traversal.
        self.assertIn("safeName(", provider)


class MobileLostArtifactTests(unittest.TestCase):
    """The download centre's 文件丢失 state: a finished task whose bytes are gone.

    The row used to read 已完成 forever because ``stateText`` only re-read the
    stored ``state`` field.  ``openDownloadedFile`` did notice the missing file,
    but it could only raise a Toast, so the row kept claiming a save that no
    longer existed.  These assertions pin the contract that the state is decided
    by the filesystem for both task kinds, that a lost row withholds 打开, and
    that dropping the record is an explicit, record-only action.
    """

    def setUp(self) -> None:
        self.java_dir = (
            Path(__file__).resolve().parents[1]
            / "android"
            / "app"
            / "src"
            / "main"
            / "java"
            / "com"
            / "asmrlib"
            / "archiver"
        )

    def _read(self, name: str) -> str:
        source = self.java_dir / name
        if not source.is_file():  # pragma: no cover - packaging guard
            self.fail(f"{name} is missing from the APK shell sources")
        return source.read_text(encoding="utf-8")

    def test_lost_state_exists_and_is_labelled(self) -> None:
        text = self._read("MainActivity.java")
        self.assertIn('private static final String STATE_LOST = "lost";', text)
        self.assertIn('if (STATE_LOST.equals(state)) return "文件丢失";', text)
        # It is a failure the user can act on, so it is drawn in the failure
        # colour rather than the success green.
        self.assertIn(
            "else if (STATE_FAILED.equals(state) || STATE_LOST.equals(state)) {", text
        )

    def test_lost_is_decided_by_the_filesystem_not_the_stored_state(self) -> None:
        text = self._read("MainActivity.java")
        self.assertIn("private boolean applyArtifactCheck(JSONObject task)", text)
        self.assertIn("private boolean artifactExists(JSONObject task)", text)
        # An in-app HLS save is a plain app-private file: the file itself is the
        # only evidence accepted, and it has to be non-empty.
        self.assertIn('String localPath = task.optString("localPath", "");', text)
        self.assertIn("return file.isFile() && file.length() > 0L;", text)
        # A direct task resolves its local path from the stored uri, and falls
        # back to the system's own handle for the row — both have to open.
        self.assertIn("uriHasBytes(Uri.parse(localUri))", text)
        self.assertIn(
            'manager.getUriForDownloadedFile(task.optLong("downloadId", -1L))', text
        )
        self.assertIn('"file".equals(uri.getScheme())', text)
        self.assertIn('openAssetFileDescriptor(uri, "r")', text)
        # Only a finished task can be lost; a running one has no artifact yet
        # and a failed one already carries its real reason.
        apply_start = text.index("private boolean applyArtifactCheck(JSONObject task)")
        apply_end = text.index("private boolean artifactExists(JSONObject task)", apply_start)
        self.assertIn(
            "if (!STATE_SUCCESS.equals(state) && !STATE_LOST.equals(state)) return false;",
            text[apply_start:apply_end],
        )
        # The check runs after the cursor pass: otherwise a DownloadManager row
        # still reporting STATUS_SUCCESSFUL would overwrite 文件丢失 back to
        # 已完成 on the very next poll.
        refresh = text.index("private void refreshDownloadStates()")
        self.assertLess(
            text.index('task.put("state", stateForStatus(status));', refresh),
            text.index("applyArtifactCheck(downloadTasks.get(i))", refresh),
        )
        self.assertLess(
            text.index("applyArtifactCheck(downloadTasks.get(i))", refresh),
            text.index("if (changed) saveDownloadTasks();", refresh),
        )
        # Both kinds are covered: the HLS guard that keeps the record alive is
        # still in place, so the pass above is what decides an HLS task.
        self.assertLess(
            text.index("if (isHlsTask(task)) continue;", refresh),
            text.index("new DownloadManager.Query()", refresh),
        )

    def test_a_lost_record_is_not_silently_dropped(self) -> None:
        text = self._read("MainActivity.java")
        # The state is flipped, never deleted: a file the user removed is still
        # a task the user created, and only the user decides it goes away.
        apply_start = text.index("private boolean applyArtifactCheck(JSONObject task)")
        apply_end = text.index("private boolean artifactExists(JSONObject task)", apply_start)
        self.assertNotIn("downloadTasks.remove", text[apply_start:apply_end])
        self.assertIn("private void removeDownloadRecord(JSONObject task)", text)
        self.assertIn('"移除记录"', text)

    def test_lost_rows_withhold_open_and_offer_a_record_only_removal(self) -> None:
        text = self._read("MainActivity.java")
        row = text.index("private View buildDownloadRow(final JSONObject task)")
        row_end = text.index("private Button rowButton(", row)
        block = text[row:row_end]
        # 打开 stays gated on success alone, so a lost row cannot offer it.
        self.assertIn("if (STATE_SUCCESS.equals(state)) {", block)
        self.assertIn("if (STATE_LOST.equals(state)) {", block)
        self.assertLess(block.index("if (STATE_LOST.equals(state)) {"),
                        block.index('"移除记录"'))
        # The lost branch returns before 删除 is added, so the two actions can
        # never appear together.
        self.assertLess(block.index('"移除记录"'), block.index('rowButton("删除"'))
        # 移除记录 drops the record only — no bytes, no resume material, and it
        # does not reach into DownloadManager.
        rem = text.index("private void removeDownloadRecord(JSONObject task)")
        rem_end = text.index("private void deleteDownload(JSONObject task)", rem)
        body = text[rem:rem_end]
        self.assertIn("downloadTasks.remove(task);", body)
        self.assertIn("saveDownloadTasks();", body)
        self.assertIn("renderDownloadPanel();", body)
        for forbidden in ("deleteTree", "manager.remove", ".delete()"):
            self.assertNotIn(forbidden, body)

    def test_header_reports_the_real_offline_size(self) -> None:
        text = self._read("MainActivity.java")
        self.assertIn("private TextView downloadStorage;", text)
        self.assertIn('"离线目录 · " + formatBytes(offlineBytes())', text)
        self.assertIn("private long offlineBytes()", text)
        self.assertIn("return treeBytes(offlineDir());", text)
        self.assertIn("private static long treeBytes(File file)", text)
        # The walk recurses, which is what makes a save still in flight count as
        # the partial bytes it has already written under <dest>.parts and in
        # <dest>.part instead of zero or the size it has not reached.
        self.assertIn("File[] children = file.listFiles();", text)
        self.assertIn("total += treeBytes(child);", text)
        offline = text.index("The offline directory's real size right now")
        self.assertIn(".parts", text[offline:text.index("private static long treeBytes", offline)])
        # Built into the header once, then refreshed on every render — the poll
        # included — so the figure cannot go stale while the panel is open.
        build = text.index("private void buildDownloadPanelIfNeeded()")
        self.assertIn("downloadStorage = new TextView(this);",
                      text[build:text.index("downloadScroll = new ScrollView(this);", build)])
        render = text.index("private void renderDownloadPanel()")
        self.assertLess(
            text.index("downloadStorage.setText(", render),
            text.index("downloadList.removeAllViews();", render),
        )


class MobileDiscoverAssetTests(unittest.TestCase):
    """The bundled 发现页 must match its Python generator (blocklist rule)."""

    def test_committed_apk_asset_is_in_sync(self) -> None:
        from asmrlib_archiver.mobile_discover import (
            ASSET_RELPATH,
            MARKER,
            build_discover_html,
        )

        asset = Path(__file__).resolve().parents[1] / ASSET_RELPATH
        if not asset.is_file():  # pragma: no cover - packaging guard
            self.fail(
                "android discover.html asset is missing; run: "
                "python -m asmrlib_archiver.mobile_discover"
            )
        self.assertEqual(asset.read_text(encoding="utf-8"), build_discover_html())
        html = build_discover_html()
        self.assertIn(MARKER, html)
        self.assertIn("connect-src 'none'", html)
        self.assertIn("__asmrlibDiscover", html)
        # The grid's only data source is a live site, so the page exposes a
        # synthetic-payload hook for visual QA (same pattern as the play page's
        # __asmrlibNetBannerPreview); pin it so a refactor cannot drop it.
        self.assertIn("__asmrlibDiscoverPreview", html)
        # The viewer's phone query reserves 64px under the page for a 4-tab dock
        # this page does not have — the neutralising class removes the dead band.
        self.assertIn("<body class='discover-page'>", html)
        # The page/record count is chrome now: it lives in the section header's
        # pill, and #ds-status is reserved for transient messages.
        self.assertIn("id='ds-count'", html)
        self.assertIn("id='ds-sec'", html)
        # A failed 下一页 must not wipe the cards already on screen, so
        # request() threads a keep-the-grid flag into showError(); pin it as a
        # tripwire against a refactor quietly restoring the old blanking.
        self.assertIn("keep: !!append || !!isRefresh", html)
        self.assertIn("req.keep", html)
        # Loading cards must preserve the same shell as real cards; otherwise
        # the caption area jumps and becomes a detached sliver on narrow phones.
        self.assertIn("#ds-grid > .live-skeleton-grid", html)
        self.assertIn("grid-column: 1 / -1", html)
        # #ds-grid is itself the real-card grid. The skeleton wrapper must
        # span it and both grids must derive columns from the same responsive
        # minmax contract, never from separate hard-coded breakpoints. The
        # contract moved into the shared skin with the rest of the tokens, so
        # the name is --card-min now rather than a page-local duplicate.
        self.assertIn("--card-min: clamp(148px, 43vw, 208px)", html)
        # The phone wall is a staggered masonry (the reference drives its list
        # with StaggeredGridCells.Adaptive): the real grid is CSS columns,
        # while the loading block keeps the old minmax contract and spans
        # every column so the skeleton still predicts full-width rows.
        self.assertIn(".cover-grid {\n  /* A staggered wall", html)
        self.assertIn("columns: 2;", html)
        self.assertIn("column-span: all;", html)
        self.assertGreaterEqual(
            html.count("repeat(auto-fit, minmax(min(100%, var(--card-min)), 1fr))"),
            1,
        )
        self.assertNotIn("@media (max-width: 340px)", html)
        self.assertIn("for (var i = 0; i < 8; i++)", html)
        self.assertIn("statusEl.className = 'ds-status is-loading';", html)
        self.assertIn("AUTO_RETRY_DELAYS = [900, 1800, 3600]", html)
        self.assertIn("网络波动，正在自动重试…", html)
        self.assertIn("request(url, !!append, false, true, keepGrid)", html)
        # The APK is standalone: the PC-library route was cut, so no link and
        # no app://pc-library handler may survive anywhere in the bundles.
        self.assertNotIn("app://pc-library", html)
        self.assertNotIn("电脑收藏馆", html)

    def test_the_generated_pages_scripts_parse_as_javascript(self) -> None:
        """The page scripts live inside Python strings, so nothing else parses them.

        A Python ``#`` comment written where a JS ``//`` belongs is a one-line
        slip that the generator, every string assertion in this file and the APK
        build all accept - and the page then dies on a SyntaxError and sits on
        its splash forever.  That is exactly what shipped once, and it took a
        device to notice.  ``node --check`` is the only thing here that reads
        the script *as JavaScript*.
        """
        from asmrlib_archiver.mobile_discover import build_discover_html, build_play_html

        node = shutil.which("node")
        if not node:  # pragma: no cover - depends on the machine
            self.skipTest("node is not on PATH")
        for build in (build_discover_html, build_play_html):
            html = build()
            blocks = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.DOTALL)
            self.assertTrue(blocks, f"{build.__name__} produced no inline script")
            checked = 0
            for index, (attrs, body) in enumerate(blocks):
                declared = re.search(r"type\s*=\s*['\"]([^'\"]+)['\"]", attrs)
                if declared and "javascript" not in declared.group(1).lower():
                    # A data island (the page's own JSON config), not code.
                    continue
                checked += 1
                for number, line in enumerate(body.splitlines(), start=1):
                    self.assertFalse(
                        line.lstrip().startswith("#"),
                        f"{build.__name__} script #{index} line {number} starts with "
                        f"'#', which is a Python comment inside a JS body: {line.strip()}",
                    )
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".js", delete=False, encoding="utf-8"
                ) as handle:
                    handle.write(body)
                    scratch = Path(handle.name)
                try:
                    proc = subprocess.run(
                        [node, "--check", str(scratch)], capture_output=True, text=True
                    )
                finally:
                    scratch.unlink(missing_ok=True)
                self.assertEqual(
                    proc.returncode, 0,
                    f"{build.__name__} script #{index} is not valid JavaScript:\n"
                    + proc.stderr,
                )
            self.assertGreater(checked, 0, f"{build.__name__} has no executable script")

    def test_pull_to_refresh_is_a_drawn_arc_not_a_rotating_glyph(self) -> None:
        """The pull indicator is a drawn arc whose fill IS the progress.

        It used to be a 24px disc holding a text glyph: the pull rotated that
        glyph by an inline transform at a rate tied to the drag distance, and
        the busy state handed the same property to a CSS keyframe — so the two
        fought on release and the pull gave no sense of progress at all. Pin
        the drawn arc, the fill wiring, and the drag-gated settle so a refactor
        cannot quietly put the glyph back.
        """
        from asmrlib_archiver.mobile_discover import build_discover_html

        html = build_discover_html()
        # Drawn, and inlined: the CSP is connect-src 'none', so every icon on
        # this page is SVG in the markup — nothing may be fetched.
        self.assertIn("<svg class='ds-ptr-ring' viewBox='0 0 36 36'", html)
        self.assertIn("class='ds-ptr-track'", html)
        self.assertIn("id='ds-ptr-arc'", html)
        self.assertIn("stroke-linecap='round'", html)
        # dasharray == the circumference of r=14 (2*pi*14), which is what makes
        # dashoffset a 0..1 progress read-out rather than a decoration
        self.assertIn("stroke-dasharray='87.96'", html)
        self.assertIn("PTR_ARC_LEN = 87.96", html)
        # The accent is the app's own accent, taken from the shared skin's
        # token rather than a literal at the use site — which is what keeps the
        # web surfaces and the native chrome the same colour. The token's value
        # is asserted here and cross-checked against the Java literals by
        # test_the_web_and_native_accents_are_the_same_colour.
        self.assertIn("--accent: #7c9cff;", html)
        self.assertIn(".ds-ptr-arc { stroke: var(--accent); }", html)
        self.assertIn(".ds-ptr-track { stroke: var(--sep-strong);", html)
        # ... and the text glyph and its inline rotation are gone
        self.assertNotIn("ds-ptr-ico", html)
        self.assertNotIn("&#8595;", html)
        self.assertNotIn("firstElementChild.style.transform", html)

        # The pull drives the arc; rotation is reserved for the busy chain,
        # and it lives on the SVG root so it can never meet the arc's own
        # rotate(-90) attribute on one element.
        self.assertIn("ptrArcEl.style.strokeDashoffset = String(PTR_ARC_LEN * (1 - t));", html)
        self.assertIn(".ds-ptr.busy .ds-ptr-ring { animation: ds-ptr-spin", html)
        self.assertIn("transform='rotate(-90 18 18)'", html)

        # Settle transitions exist only with the finger up: the drag class is
        # what keeps the content tracking the touch, and its removal is what
        # turns the below-threshold snap-back into a glide.
        self.assertIn("mainEl.classList.add('is-dragging')", html)
        self.assertIn("mainEl.classList.remove('is-dragging')", html)
        self.assertIn(
            "main.wrap:not(.is-dragging) { transition: transform .26s cubic-bezier(.22,.61,.36,1); }",
            html,
        )
        self.assertIn("main.wrap:not(.is-dragging) .ds-ptr { transition:", html)
        # a cancelled touch never fires touchend — without the shared release
        # handler the page would keep .is-dragging and never settle again
        self.assertIn("mainEl.addEventListener('touchcancel', releasePull, { passive: true });", html)

        # Crossing the threshold is its own visible state ("release now").
        self.assertIn("ptrEl.classList.toggle('armed', armed)", html)
        self.assertIn(".ds-ptr.armed .ds-ptr-track", html)

        # Reduced motion drops the spin but NOT the fill: the arc is
        # information, the rotation is decoration.
        self.assertIn(
            "@media (prefers-reduced-motion: reduce) {\n"
            "  main.wrap:not(.is-dragging), main.wrap:not(.is-dragging) .ds-ptr { transition: none; }",
            html,
        )
        self.assertIn(".ds-ptr.busy .ds-ptr-ring { animation: none; }", html)

        # The refresh semantics this redesign must not disturb: the whole
        # loaded chain is re-requested, a failed refresh still ends the
        # spinner, the listeners stay passive, and the indicator never eats a
        # tap.
        self.assertIn("request(pages[0], false, true);", html)
        self.assertIn("if (state.refreshing) endRefresh();", html)
        self.assertIn("PTR_THRESHOLD = 72, PTR_MAX = 120", html)
        # Every listener that can fire during a gesture is passive: the four on
        # the scroll container, plus the nav bar's scroll listener, which only
        # reads scrollY and toggles one class through rAF. A non-passive
        # listener here is what makes a WebView feel like it is fighting the
        # finger, so the count is a tripwire rather than bookkeeping.
        self.assertEqual(html.count("{ passive: true }"), 5)
        self.assertIn("window.addEventListener('scroll', function () {", html)
        self.assertIn("pointer-events: none; opacity: 0;", html)

    def test_tablet_gets_its_own_grid_contract(self) -> None:
        """A phone grid does not become a tablet grid on its own.

        The auto-fit contract above is width-agnostic, so at 1280px it simply
        resolves more columns at the phone's 216px ceiling and the captions
        stay at phone sizes — edge to edge. Pin the two tablet ranges that add
        a cap and a bigger card, split by ORIENTATION because 1024x1366
        portrait and 1280x800 landscape are different problems.
        """
        from asmrlib_archiver.mobile_discover import build_discover_html

        html = build_discover_html()
        self.assertIn("@media (min-width: 768px) and (orientation: portrait)", html)
        self.assertIn("@media (min-width: 768px) and (orientation: landscape)", html)
        # portrait: a centred reading column of 3 staggered columns
        self.assertIn("--card-min: 240px", html)
        self.assertIn("max-width: 920px", html)
        # landscape: a wider cap and a fourth column; the wall keeps its
        # square covers on every width, so the short viewport buys back a
        # ROW, not a flatter card
        self.assertIn("--card-min: 252px", html)
        self.assertIn("max-width: 1180px", html)
        self.assertIn(".cover-grid { columns: 3; column-gap: var(--sp-4); }", html)
        self.assertIn(".cover-grid { columns: 4; column-gap: var(--sp-4); }", html)
        # typography has to scale with the room, not stay at the phone's size
        self.assertGreaterEqual(
            html.count(".cover-title { font-size: var(--fs-headline); }"), 2
        )
        # the tag rail bleeds to the wrap's own padding, which is not the
        # phone's 16px once the tablet range pads and caps the wrap
        self.assertIn(
            ".chips { margin: 0 calc(var(--sp-6) * -1) var(--sp-4);", html
        )
        # the loading grid has to move with the real grid or the skeleton
        # stops predicting the card it stands in for
        self.assertIn("#ds-grid > .live-skeleton-grid { gap: var(--sp-5); }", html)
        self.assertIn("#ds-grid > .live-skeleton-grid { gap: var(--sp-4); }", html)

    def test_tablet_rules_cannot_reach_the_phone(self) -> None:
        """Every media query that can move the layout must start at >=768px.

        The tablet pass is meant to be additive: a phone has to render exactly
        as it did before. Asserting it structurally beats re-pinning each phone
        value, because it also catches a tablet rule added later under a query
        that happens to overlap the phone range.
        """
        import re

        from asmrlib_archiver.mobile_discover import build_discover_html

        html = build_discover_html()
        for cond in re.findall(r"@media([^{]+)\{", html):
            if "orientation" not in cond and "min-width" not in cond:
                continue
            floors = [int(n) for n in re.findall(r"min-width:\s*(\d+)px", cond)]
            self.assertTrue(floors, f"layout query without a min-width floor: {cond}")
            self.assertGreaterEqual(min(floors), 768, cond)

    def test_first_paint_splash_fades_once_and_never_returns(self) -> None:
        """A centred brand mark + sweeping accent ring covers the first paint.

        The splash exists for one purpose: to make the cold start look
        intentional. The fade is a one-shot — pull-to-refresh / pagination /
        chain refills must never re-show it, which the JS enforces with a
        class toggle that only ever adds ``is-gone`` and never removes it.
        """
        from asmrlib_archiver.mobile_discover import build_discover_html

        html = build_discover_html()
        self.splash_test(html)

    def test_header_refresh_button_is_a_fallback_for_pull_to_refresh(self) -> None:
        """A button in the header is the always-available refresh.

        WebView's native scroll sometimes swallows the pull gesture before
        our touchmove listener sees it, so the gesture-driven arc never
        appears. A button in the header is the always-available
        alternative — it calls the same startRefresh() the gesture would
        have, and the gesture path is allowed to fail without the user
        losing the ability to refresh.
        """
        from asmrlib_archiver.mobile_discover import build_discover_html

        html = build_discover_html()
        self.assertIn("<button type='button' class='icon-btn ds-refresh'", html)
        self.assertIn("id='ds-refresh-btn'", html)
        self.assertIn("aria-label='刷新内容'", html)
        # The icon-only button takes its shape from the shared .icon-btn and its
        # busy state from .is-busy — without that the icon never spins, so a
        # long-running refresh would look frozen.
        self.assertIn(".icon-btn.is-busy svg", html)
        self.assertIn(".icon-btn:active", html)
        # JS wires the click to the same startRefresh() the gesture path
        # uses, not a parallel implementation that could drift. The
        # re-entrant guard inside startRefresh() keeps a double tap from
        # queuing two refreshes.
        self.assertIn("var refreshBtn = document.getElementById('ds-refresh-btn');", html)
        self.assertIn("if (refreshBtn) refreshBtn.classList.add('is-busy');", html)
        self.assertIn("if (refreshBtn) refreshBtn.classList.remove('is-busy');", html)
        self.assertIn("startRefresh();", html)

    def splash_test(self, html: str) -> None:
        # Markup: a ring SVG + the brand glyph + label + sub-label, all inside
        # one container. Inline SVG is required — the page CSP is
        # connect-src 'none' and nothing may be fetched.
        self.assertIn("<div class='ds-splash' id='ds-splash'", html)
        self.assertIn("class='ds-splash-ring'", html)
        self.assertIn("class='ds-splash-arc'", html)
        # The arc length (2 * pi * 42 = 263.89) is the same constant the JS
        # expects to walk — mismatching it would make the sweep stutter.
        self.assertIn("stroke-dasharray='263.89'", html)
        self.assertIn("transform='rotate(-90 48 48)'", html)
        self.assertIn("<span class='ds-splash-glyph'", html)
        self.assertIn("ds-splash-label", html)
        self.assertIn("正在发现内容", html)
        self.assertIn("ds-splash-sub", html)

        # CSS: the fade is .42s and the layer only ever takes the is-gone
        # class — its own visibility:hidden lock fires after the opacity
        # transition so the screen reader / focus tree does not see it.
        self.assertIn(".ds-splash.is-gone", html)
        self.assertIn("opacity: 0", html)
        self.assertIn("visibility: hidden", html)
        # Reduced motion drops the spin but keeps the fade (just shorter),
        # so an animation-averse user still sees the brand mark settle out.
        self.assertIn(
            "@media (prefers-reduced-motion: reduce)", html
        )
        self.assertIn(".ds-splash-ring", html)
        self.assertIn("animation: none", html)

        # JS: the element reference is taken once and used in two places —
        # first successful render and the unrecoverable error path. A
        # wrong-shape guard (``classList.contains('is-gone')``) prevents
        # later renders from re-showing the splash.
        self.assertIn(
            "var splashEl = document.getElementById('ds-splash');", html
        )
        self.assertIn("splashEl.classList.add('is-gone');", html)
        self.assertIn(
            "if (splashEl && !splashEl.classList.contains('is-gone'))", html
        )
        # Two add sites — one in render(), one in showError() — so the
        # splash cannot leak past either the happy path or the error panel.
        self.assertGreaterEqual(
            html.count("splashEl.classList.add('is-gone');"), 2
        )


class MobilePlayAssetTests(unittest.TestCase):
    """The bundled 播放页 must match its Python generator (same rule as 发现页)."""

    def test_committed_apk_asset_is_in_sync(self) -> None:
        from asmrlib_archiver.mobile_discover import (
            PLAY_ASSET_RELPATH,
            PLAY_MARKER,
            build_play_html,
        )

        asset = Path(__file__).resolve().parents[1] / PLAY_ASSET_RELPATH
        if not asset.is_file():  # pragma: no cover - packaging guard
            self.fail(
                "android play.html asset is missing; run: "
                "python -m asmrlib_archiver.mobile_discover"
            )
        self.assertEqual(asset.read_text(encoding="utf-8"), build_play_html())
        html = build_play_html()
        self.assertIn(PLAY_MARKER, html)
        self.assertIn("connect-src 'none'", html)
        self.assertIn("__asmrlibDiscover", html)
        # The play page rides the shared phone padding of the 4-tab dock it
        # does not have — the neutralising class is what removes the dead
        # band under the source list, so pin it.
        self.assertIn("<body class='play-page'>", html)

    def test_wide_tablet_puts_the_hero_beside_the_source_list(self) -> None:
        """The phone layout stacks a 16/9 hero above the source rows.

        Both are full width, so on a tablet the footer's hint line runs to
        1120px (unreadable) and the hero grows taller than the list it is
        introducing. Pin the two tablet ranges: narrow tablets keep the stack
        but stop it growing, wide ones split the hero into its own column.
        """
        from asmrlib_archiver.mobile_discover import build_play_html

        html = build_play_html()
        self.assertIn("@media (min-width: 768px) and (max-width: 899px)", html)
        self.assertIn("@media (min-width: 900px)", html)
        self.assertIn("@media (min-width: 900px) and (orientation: landscape)", html)
        # The split is gated on the hero, never on #pl-dock: the glass layer's
        # .play-dock sets display:flex, which beats the UA [hidden] rule, so
        # #pl-dock is never display:none — keying the columns on it would
        # leave an empty left column through the whole resolve.
        self.assertIn(".play-page .wrap:has(> #pl-hero:not([hidden]))", html)
        self.assertIn(
            "display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);",
            html,
        )
        self.assertIn("grid-column: 1; grid-row: 1 / -1; margin: 0;", html)
        self.assertIn("grid-column: 2; grid-row: 1;", html)
        # neither the footer nor the grouped rows may stretch to 1280px
        self.assertIn("max-width: 1120px", html)
        self.assertIn("max-width: 760px", html)
        # the dock's own side padding would double up on the grid gap
        self.assertIn(
            "padding: 0 0 calc(var(--sp-1) + env(safe-area-inset-bottom));", html
        )
        # the rows and their type scale up with the room
        self.assertIn(".play-enter { padding: var(--sp-3) var(--sp-4); contain-intrinsic-size: auto 68px; }", html)
        self.assertIn(".play-enter strong { font-size: var(--fs-headline); }", html)
        self.assertIn(".pl-hero-title { font-size: 23px; }", html)
        self.assertIn(".play-hint { font-size: var(--fs-subhead); }", html)

    def test_phone_play_block_is_untouched_by_the_tablet_pass(self) -> None:
        """The only phone-specific block on this page must read exactly as it
        did: same query, same two declarations, same ordering."""
        from asmrlib_archiver.mobile_discover import build_play_html

        html = build_play_html()
        self.assertIn(
            "@media (max-width: 767px) {\n"
            "  .play-page { padding-bottom: env(safe-area-inset-bottom); }\n"
            "  .play-page .wrap { padding-top: .7rem;\n"
            "    padding-bottom: calc(1.4rem + env(safe-area-inset-bottom)); }\n"
            "}",
            html,
        )

    def test_tablet_rules_cannot_reach_the_phone(self) -> None:
        """Every media query that can move the layout must start at >=768px.

        Same structural guard as the discover page: it catches a tablet rule
        added under a query that happens to overlap the phone range.
        """
        import re

        from asmrlib_archiver.mobile_discover import build_play_html

        html = build_play_html()
        for cond in re.findall(r"@media([^{]+)\{", html):
            if "orientation" not in cond and "min-width" not in cond:
                continue
            floors = [int(n) for n in re.findall(r"min-width:\s*(\d+)px", cond)]
            self.assertTrue(floors, f"layout query without a min-width floor: {cond}")
            self.assertGreaterEqual(min(floors), 768, cond)


class MobilePlaybackFallbackTests(unittest.TestCase):
    """打开 must play what was saved, without a third-party player installed.

    The in-app HLS saver writes concatenated MPEG-TS and the download center
    reported it as ``video/mp2t``.  Nothing on a bare emulator resolves that
    type, so ``ACTION_VIEW`` threw ``ActivityNotFoundException`` and 打开 on a
    已完成 row did nothing at all.  These assertions pin the replacement: a
    bundled framework-only player, and a resolve-before-start guard so an
    unresolved intent can never throw again.

    The decode capability itself is not asserted here — it is a platform
    property, proven by execution on the emulator (``dumpsys media.extractor``
    registers ``MPEG2-PS/TS Extractor ... supports: m2p m2ts mts ts``) and by
    playing a real artifact.
    """

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.java_dir = (
            self.root / "android" / "app" / "src" / "main" / "java" / "com" / "asmrlib" / "archiver"
        )
        self.manifest = (
            self.root / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
        )

    def _read(self, name: str) -> str:
        source = self.java_dir / name
        if not source.is_file():  # pragma: no cover - packaging guard
            self.fail(f"{name} is missing from the APK shell sources")
        return source.read_text(encoding="utf-8")

    def test_a_bundled_player_exists_and_uses_framework_media_only(self) -> None:
        source = self._read("PlayerActivity.java")
        # No AndroidX (android.useAndroidX=false), so the player has to be
        # built from framework classes — not ExoPlayer, not a bundled JS player.
        self.assertNotIn("import androidx.", source)
        self.assertNotIn("import android.support.", source)
        for marker in (
            "extends Activity",
            # The surface is owned directly. VideoView was dropped on purpose:
            # it welds its MediaPlayer to the surface (so a rotation, which the
            # manifest absorbs, killed playback) and it drives MediaController,
            # a floating popup that re-measures the video surface on every
            # show/hide. Re-introducing it would silently undo both fixes.
            "SurfaceView",
            "SurfaceHolder.Callback",
            "new MediaPlayer()",
            "setDataSource",
            "setOnErrorListener",
            "setOnPreparedListener",
            "prepareAsync",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("new VideoView(", source)
        self.assertNotIn("import android.widget.VideoView", source)
        self.assertNotIn("new MediaController(", source)
        self.assertNotIn("import android.widget.MediaController", source)

    def test_the_player_transport_bar_is_hand_drawn(self) -> None:
        source = self._read("PlayerActivity.java")
        # The stock SeekBar/ProgressBar/MediaController arrive in the platform
        # theme, which is the one palette in the app that came from a different
        # product. The rail, the knob, the play/pause disc and the busy ring are
        # all drawn from the view's own bounds instead — no drawable XML per
        # density bucket, and nothing that can drift from the accent gradient.
        for marker in (
            "class PlayerSeekBar extends View",
            "class PlayPauseButton extends View",
            "class LoadingRing extends View",
            "OnSeekListener",
            "drawRoundRect",
            "drawArc",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("import android.widget.SeekBar", source)
        # Nothing leans on a themed widget's own progress drawable.
        self.assertNotIn("setProgressDrawable", source)
        self.assertNotIn("setThumb", source)
        # Geometry is a fraction of the bounds the view is handed, never a
        # captured px — the same rule the download-centre glyph follows.
        # (The seek bar and the play button each reduce their bounds once;
        # the glyph uses `s`.)
        self.assertIn("float size = Math.min(w, h);", source)
        self.assertIn("float s = Math.min(w, h);", source)

    def test_playback_failure_is_named_never_dressed_up_as_working(self) -> None:
        source = self._read("PlayerActivity.java")
        # Same honesty rule the download path already follows: a file the
        # device cannot decode is reported as such, with the real cause.
        self.assertIn("MEDIA_ERROR_UNSUPPORTED", source)
        self.assertIn("MEDIA_ERROR_MALFORMED", source)
        self.assertIn("这台设备没有能解码该文件的组件", source)
        # The framework's own error dialog would hide the reason behind a
        # generic message, so the listener must consume the error.
        self.assertIn("return true; // handled", source)

    def test_the_player_fills_the_bars_with_the_works_own_art(self) -> None:
        """The letterbox bars carry the blurred poster, not dead black.

        The reference player paints its whole page from the artwork; this
        player's stage used to fall back to bare black wherever the video
        did not fill it. The backdrop is a poster smeared into ambient
        light: it must sit UNDER the surface (added first, so z-order puts
        the picture in front), start hidden, and only ever appear once a
        real frame exists - a fake floor of colour before decode would be
        exactly the dressed-up-not-working lie the failure copy forbids.
        """
        source = (self.java_dir / "PlayerActivity.java").read_text(encoding="utf-8")
        # Field declared, view built before the surface (z-order), hidden.
        self.assertIn("private ImageView backdrop;", source)
        self.assertIn("backdrop = new ImageView(this);", source)
        self.assertLess(
            source.index("backdrop = new ImageView(this);"),
            source.index("surface = new SurfaceView(this);"),
            "the backdrop must be added before the surface to sit under it",
        )
        self.assertIn("backdrop.setScaleType(ImageView.ScaleType.CENTER_CROP);", source)
        self.assertIn("backdrop.setVisibility(View.GONE);", source)
        # The stage's own black stays: the backdrop takes over, it does not
        # replace the pre-decode answer.
        self.assertIn("videoFrame.setBackgroundColor(Color.BLACK);", source)

        # One frame drives both the poster and the floor, on the UI thread.
        publish = source[source.index("private void publishFrame("):]
        publish = publish[: publish.index("private static Bitmap blurredBackdrop")]
        self.assertIn("final Bitmap floor = blurredBackdrop(poster);", publish)
        self.assertIn("backdrop.setColorFilter(0x66000000,", publish)
        self.assertIn("android.graphics.PorterDuff.Mode.SRC_OVER", publish)
        self.assertIn("backdrop.setImageBitmap(floor);", publish)
        self.assertIn("backdrop.setVisibility(View.VISIBLE);", publish)

        # The smear is two bilinear passes, not a convolution: crush to a
        # thumb, stretch back. The factors are the contract - small enough
        # to be ambient, large enough to never compete with the decoder.
        blur = source[source.index("private static Bitmap blurredBackdrop"):]
        self.assertIn("Math.max(1, w / 16), Math.max(1, h / 16), true", blur)
        self.assertIn("Math.max(1, w / 5), Math.max(1, h / 5), true", blur)

    def test_a_playback_failure_retries_before_it_is_reported(self) -> None:
        source = self._read("PlayerActivity.java")
        # Reporting the error and stopping treated a stumble at the head of a
        # stream as permanent, and the only cure was to back out and tap again.
        self.assertIn("MAX_AUTO_RETRIES = 3", source)
        self.assertIn("RETRY_COUNTDOWN_SECONDS = 5", source)
        # The listener must still consume the error — but it must hand it to a
        # method that can recover, not straight to a static panel.
        listener = source[source.index("public boolean onError(MediaPlayer p"):]
        listener = listener[: listener.index("}\n        });")]
        self.assertIn("handlePlaybackError(what, extra)", listener)
        self.assertIn("return true; // handled", listener)
        # Nothing in the error path may tear the player down: a retry against a
        # released player is not a retry.
        self.assertNotIn("releasePlayer", listener)

        retry = source[source.index("private void runAutoRetry()"):]
        retry = retry[: retry.index("private void cancelAutoRetry()")]
        # reset() is the legal way out of the Error state (Idle/Initialized, not
        # End), so the retry is a re-prepare rather than a rebuild.
        for call in ("mp.reset()", "mp.setDataSource(this, mediaUri)",
                     "setVideoScalingMode", "mp.prepareAsync()"):
            self.assertIn(call, retry)
        self.assertLess(retry.index("mp.reset()"), retry.index("mp.setDataSource"))
        self.assertLess(retry.index("mp.setDataSource"), retry.index("mp.prepareAsync()"))

    def test_the_retry_budget_is_bounded_and_its_countdown_is_cancellable(self) -> None:
        source = self._read("PlayerActivity.java")
        handler = source[source.index("private void handlePlaybackError(int what"):]
        handler = handler[: handler.index("private static boolean isTransient")]
        # First failure arms the budget, every later one spends it; exhausted
        # means the panel finally reports the failure. Unbounded retrying would
        # hide a genuinely broken file behind a spinner forever.
        self.assertIn("autoRetryRemaining = MAX_AUTO_RETRIES", handler)
        self.assertIn("autoRetryRemaining--", handler)
        self.assertIn("showFinalError()", handler)
        # The countdown is a Handler, so releasePlayer can cancel it; a Timer
        # would outlive the activity and fire into a dead player.
        countdown = source[source.index("private void scheduleAutoRetry()"):]
        countdown = countdown[: countdown.index("private void runAutoRetry()")]
        self.assertIn("ui.postDelayed(", countdown)
        self.assertNotIn("java.util.Timer", source)
        self.assertNotIn("new Timer(", source)
        release = source[source.index("private void releasePlayer()"):]
        release = release[: release.index("private void togglePlay()")]
        # Teardown has to reach it: a countdown that survives releasePlayer
        # would call prepareAsync on a released player.
        self.assertIn("cancelAutoRetry();", release)

    def test_a_failure_a_retry_cannot_fix_is_reported_immediately(self) -> None:
        source = self._read("PlayerActivity.java")
        # A codec the device lacks will still be missing after five seconds, and
        # a corrupt container cannot un-corrupt: queueing three attempts for
        # those is fifteen seconds of delay in front of the honest message.
        self.assertIn("private static boolean isTransient(int extra)", source)
        transient = source[source.index("private static boolean isTransient(int extra)"):]
        transient = transient[: transient.index("private void scheduleAutoRetry()")]
        self.assertIn("case MediaPlayer.MEDIA_ERROR_UNSUPPORTED:", transient)
        self.assertIn("case MediaPlayer.MEDIA_ERROR_MALFORMED:", transient)
        self.assertIn("return false;", transient)
        handler = source[source.index("private void handlePlaybackError(int what"):]
        handler = handler[: handler.index("private static boolean isTransient")]
        self.assertIn("if (!extractorAccepted && !isTransient(extra))", handler)
        self.assertLess(handler.index("if (!extractorAccepted && !isTransient(extra))"),
                        handler.index("autoRetryRemaining = MAX_AUTO_RETRIES"))

    def test_a_break_mid_stream_is_delivery_not_a_codec_verdict(self) -> None:
        """The same -1010 means two different things depending on when it lands.

        Before onPrepared it is the honest "this device cannot decode this".
        After minutes of playback it is a truncated CDN segment wearing that
        badge — on-device, the seg-12 timeout of a stream that had already
        played twelve segments surfaced as (1,-1010), got the terminal
        "容器或编码不受支持" panel and no retry. The extractor's acceptance is
        the discriminator, the position is the recovery, and the sticky flag
        keeps a failing retry chain from un-reading the same fact.
        """
        source = self._read("PlayerActivity.java")
        handler = source[source.index("private void handlePlaybackError(int what"):]
        handler = handler[: handler.index("private static boolean isTransient")]
        # The state is read BEFORE prepared is cleared: after, every failure
        # would look like a prepare-time one.
        self.assertIn("boolean extractorAccepted = prepared || errorWasMidPlay;",
                      handler)
        self.assertLess(handler.index("boolean extractorAccepted"),
                        handler.index("prepared = false"))
        # A mid-play failure retries and resumes where it broke rather than
        # replaying the whole file from zero.
        self.assertIn("if (extractorAccepted && playedTo > 0) pendingSeekMs = playedTo;",
                      handler)
        # Sticky through the retry chain — a failed re-prepare runs this same
        # handler with prepared already false — and reset only when a fresh
        # source opens.
        self.assertIn("errorWasMidPlay = extractorAccepted;", handler)
        open_ = source[source.index("private void open()"):]
        open_ = open_[: open_.index("MediaPlayer mp = new MediaPlayer();")]
        self.assertIn("errorWasMidPlay = false;", open_)
        # The exhausted panel of a mid-play stream names the delivery, not
        # the codec — "不受支持" would contradict what the user just watched.
        describe = source[source.index("private String describe(int what, int extra)"):]
        describe = describe[: describe.index("private void showError(")]
        self.assertIn("errorWasMidPlay && !remoteUrl.isEmpty()", describe)
        self.assertIn("播放中途断流", describe)

    def test_the_retry_panel_counts_down_instead_of_just_saying_loading(self) -> None:
        source = self._read("PlayerActivity.java")
        # A static "加载中" that sits there for five seconds is
        # indistinguishable from a hang; the number is the proof that the shell
        # is still trying.
        self.assertIn("private TextView retryCountdownLabel;", source)
        panel = source[source.index("private void showPanel(String message"):]
        panel = panel[: panel.index("private void openExternally()")]
        self.assertIn("retryCountdownLabel.setVisibility(countingDown ? View.VISIBLE : View.GONE)",
                      panel)
        # The countdown label is a live number, updated on every tick.
        countdown = source[source.index("private void scheduleAutoRetry()"):]
        countdown = countdown[: countdown.index("private void runAutoRetry()")]
        self.assertIn("retryCountdownLabel.setText(String.valueOf(autoRetrySecondsLeft))",
                      countdown)
        # And the final panel reports what was actually tried.
        final = source[source.index("private void showFinalError()"):]
        final = final[: final.index("private void showPanel(")]
        self.assertIn("已自动重试 ", final)

    def test_open_resolves_a_handler_before_starting(self) -> None:
        text = self._read("MainActivity.java")
        self.assertIn("private boolean hasExternalViewer(Uri uri, String mime)", text)
        self.assertIn("PackageManager.MATCH_DEFAULT_ONLY", text)
        # The guard has to run *before* the start, otherwise it is decoration:
        # an unresolved ACTION_VIEW throws instead of failing softly.
        start = text.index("private void openDownloadedFile(JSONObject task)")
        end = text.index("private boolean hasExternalViewer(", start)
        body = text[start:end]
        self.assertIn("hasExternalViewer(uri, mime)", body)
        self.assertLess(
            body.index("hasExternalViewer(uri, mime)"),
            body.index("startActivity(view)"),
        )

    def test_open_falls_back_to_the_in_app_player(self) -> None:
        text = self._read("MainActivity.java")
        self.assertIn("private boolean startInAppPlayer(Uri uri, String fileName)", text)
        self.assertIn("PlayerActivity.EXTRA_URI", text)
        self.assertIn("PlayerActivity.EXTRA_TITLE", text)
        start = text.index("private void openDownloadedFile(JSONObject task)")
        end = text.index("private boolean hasExternalViewer(", start)
        body = text[start:end]
        # An HLS task is concatenated MPEG-TS: no external handler claims
        # video/mp2t, so it goes straight to the bundled player rather than
        # hoping the device has one.
        self.assertIn("if (isHlsTask(task) || !hasExternalViewer(uri, mime))", body)
        self.assertIn("if (startInAppPlayer(uri, fileName)) return;", body)
        # And the external path's own throw falls back too, so 打开 can never
        # end in a crash.
        self.assertIn("if (!startInAppPlayer(uri, fileName))", body)

    def test_player_is_internal_and_claims_no_global_media_type(self) -> None:
        manifest = self.manifest.read_text(encoding="utf-8")
        self.assertIn('android:name=".PlayerActivity"', manifest)
        # Not exported and with no intent filter: the app must not become a
        # system-wide handler for a type it does not own.  Slicing the element
        # keeps the assertion about PlayerActivity rather than the whole file.
        start = manifest.index('android:name=".PlayerActivity"')
        element = manifest[start : manifest.index("/>", start)]
        self.assertIn('android:exported="false"', element)
        self.assertNotIn("intent-filter", element)
        # A rotation or a split-screen resize must not restart the stream.
        self.assertIn("screenLayout", element)
        self.assertIn("smallestScreenSize", element)


class MobileTabletLayoutTests(unittest.TestCase):
    """The native chrome must adapt to a tablet, not just to a phone.

    Every number below used to be a literal at its use site (a 112x44 chip, a
    360dp list, a MATCH_PARENT card).  On a 1280dp tablet that stretched the
    download card edge to edge and stranded the floating buttons in a corner.
    """

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.java_dir = (
            self.root / "android" / "app" / "src" / "main" / "java" / "com" / "asmrlib" / "archiver"
        )
        self.manifest = (
            self.root / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
        )

    def _activity(self) -> str:
        return (self.java_dir / "MainActivity.java").read_text(encoding="utf-8")

    def test_one_helper_decides_what_a_tablet_is(self) -> None:
        text = self._activity()
        self.assertIn("private boolean isTablet()", text)
        # smallestScreenWidthDp (the Configuration field), not the
        # smallestScreenSize configChanges value — they are different things
        # and only the former is a compile-time symbol.
        self.assertIn("smallestScreenWidthDp >= TABLET_MIN_WIDTH_DP", text)
        self.assertIn("private static final int TABLET_MIN_WIDTH_DP = 600;", text)
        self.assertIn("private static final int CONTENT_MAX_WIDTH_DP = 560;", text)

    def test_content_width_is_capped_and_centred(self) -> None:
        text = self._activity()
        self.assertIn("private int contentMaxWidth()", text)
        # The card used to be MATCH_PARENT with 14dp margins, which on a
        # 1280dp tablet meant the card and every task row ran edge to edge.
        self.assertIn("private FrameLayout.LayoutParams downloadCardLayout()", text)
        card = text[text.index("private FrameLayout.LayoutParams downloadCardLayout()"):]
        card = card[: card.index("private LinearLayout.LayoutParams downloadScrollLayout()")]
        self.assertIn("contentMaxWidth()", card)
        self.assertIn("Gravity.CENTER_HORIZONTAL", card)
        self.assertNotIn("MATCH_PARENT", card)
        # The splash is capped from the same helper rather than carrying its
        # own literal (the gate form that used to share it is gone with the
        # PC-library flow).
        self.assertIn("splashBoxLayout()", text)

    def test_task_list_height_follows_the_window(self) -> None:
        text = self._activity()
        self.assertIn("private int downloadListHeight()", text)
        self.assertIn("LIST_MAX_HEIGHT_DP", text)
        self.assertIn("LIST_MIN_HEIGHT_DP", text)
        # The old fixed 360dp left most of a landscape tablet empty.
        scroll = text[text.index("private LinearLayout.LayoutParams downloadScrollLayout()"):]
        scroll = scroll[: scroll.index("private void renderDownloadPanel()")]
        self.assertIn("downloadListHeight()", scroll)
        self.assertNotIn("dp(360)", scroll)

    def test_there_is_exactly_one_floating_control(self) -> None:
        text = self._activity()
        # The shell used to float two chips: the download-centre entry stacked
        # above a 保存媒体 action. That action is meaningless until the shield
        # has sniffed a media URL while the entry is useful all session, so the
        # pair spent a permanent strip of the page on a mostly-dead label. There
        # is now one floating control and the action lives in the card it opens.
        self.assertNotIn("floatingActionLayout", text)
        self.assertNotIn("attachDownloadAction", text)
        self.assertNotIn("styleFloatingChip", text)
        self.assertIn("private FrameLayout.LayoutParams downloadEntryLayout()", text)
        # The icon is now sized from entrySize() rather than the shared chip
        # height: a 40dp circle reads as a single affordance, not a stranded
        # floating button. The dragging layer above uses entryGravity +
        # entryDragX/Y to track the finger.
        self.assertIn("private int entrySize()", text)
        self.assertIn("return isTablet() ? dp(48) : dp(40);", text)
        self.assertIn("private int entryGravity", text)
        self.assertIn("private int entryDragX, entryDragY", text)
        entry = text[text.index("private FrameLayout.LayoutParams downloadEntryLayout()"):]
        entry = entry[: entry.index("private void toggleDownloadPanel()")]
        # The default pin is still bottom-right; edge inset is parameterised so
        # a tablet gets a more breathing-room margin than a phone.
        self.assertIn("entryGravity", entry)
        self.assertIn("int edge = isTablet() ? dp(24) : dp(14);", entry)
        # Drag offsets layer on top of the inset so the icon never leaves the
        # viewport; clampInt() guards the move path.
        self.assertIn("left += entryDragX", entry)
        self.assertIn("top += entryDragY", entry)
        self.assertIn("private static int clampInt", text)
        # Long-press re-pins the icon to its corner: that affordance exists
        # because a drag without a way back would strand the icon. All four
        # gestures (tap / double-tap / long-press / drag) share one
        # GestureDetector so a drag is never mistaken for a long-press - the
        # View-level setOnLongClickListener cannot be layered under the same
        # onTouch without firing twice.
        self.assertIn("onLongPress", text)
        self.assertIn("已归位到右下角", text)
        # Drag offsets are written to BOTH margins, mirrored: FrameLayout
        # honours only the margin on the side the gravity pins to, so writing
        # left/top alone left the icon frozen in the default bottom-right pin
        # until the release snap flipped the gravity.
        self.assertIn("right -= entryDragX", entry)
        self.assertIn("bottom -= entryDragY", entry)

    def test_the_single_fab_expands_both_actions(self) -> None:
        """One floating icon, two actions behind it — never two floating icons.

        Splitting 保存 and 下载中心 into separate floating buttons was rejected:
        two 40dp circles read as duplicate chrome and neither was labelled, so
        the user still could not tell which one saved. The merged control keeps
        one icon. It was a three-cell stack for a while — the middle row
        (后台播放) was removed again when 后台播放 itself was replaced by the
        automatic keep-alive service, so the pill is back to its two siblings:
        下载中心 | 保存当前视频, one hairline divider, one shared radius.
        """
        text = self._activity()
        # No second floating button of any kind.
        self.assertNotIn("downloadCenterEntry", text)
        self.assertNotIn("attachDownloadCenterEntry", text)
        self.assertIn("private void toggleFabMenu()", text)
        self.assertIn("private boolean fabMenuOpen", text)
        menu = text[text.index("private void expandFabMenu()"):]
        menu = menu[: menu.index("static String formatBytes")]
        # Tap-outside and Back both dismiss: a sheet that traps the user is
        # worse than one that opens by accident.
        self.assertIn("collapseFabMenu()", menu)
        self.assertIn("fabMenu.bringToFront()", menu)
        self.assertIn("fabScrim", menu)
        # Both actions are reachable from the pill, and the save row is
        # greyed — not hidden — when nothing has been sniffed yet. The save
        # row is 保存视频, not 保存当前视频: a third cell (应用内播放) shares
        # the pill now, and a fixed-width label would squeeze it into a
        # one-character-per-line vertical column — observed live.
        self.assertIn('"下载中心"', text)
        self.assertIn('"保存视频"', text)
        self.assertNotIn('"后台播放"', text)
        self.assertIn("private void refreshFabMenuState()", text)
        self.assertIn("hasSavableMedia()", text)
        # The entry's own label names its own action. It used to be overwritten
        # with 保存当前视频 — a leftover from when this control *was* the save
        # chip — so a screen reader announced "save" for a tap that opens a menu.
        self.assertIn('chip.setContentDescription(ready ? "打开下载菜单（有可保存的视频）" : "打开下载菜单")', text)
        self.assertNotIn('chip.setContentDescription(ready ? "保存当前视频"', text)
        # Fullscreen and navigation both collapse it: a menu left open over a
        # fullscreen video is the "挡住全屏" regression all over again.
        self.assertIn("if (fabMenuOpen) {", text)
        self.assertIn("collapseFabMenu();\n        panelWasVisibleBeforeFullscreen", text)

    def test_a_saved_video_says_so_before_the_tap(self) -> None:
        """同一个视频判断不出已下载 — one title piled up three gigabyte copies.

        The dedupe keyed on the exact media URL, and these players hand out
        signed, per-session manifests: the same video browsed tomorrow is a
        different URL, the check never fired, and uniqueDest quietly named
        the copies (1) and (2). The match now falls through URL → play-page
        address → remembered title vs saved stem, the save row and the panel
        button both relabel to 已保存 before the tap, and the tap opens the
        download centre instead of writing copy two.
        """
        text = self._activity()
        self.assertIn("private JSONObject findTaskForMedia(String url, String pageUrl, String title)", text)
        self.assertIn("private JSONObject findSavedTaskForCurrent()", text)
        # The layered keys, strongest last.
        matcher = text[text.index("private JSONObject findTaskForMedia"):]
        matcher = matcher[: matcher.index("/** The record for the video on screen")]
        self.assertIn("findTaskFor(url)", matcher)
        self.assertIn('pageUrl.equals(task.optString("pageUrl", ""))', matcher)
        self.assertIn("sanitizeStem(name).toLowerCase(java.util.Locale.US)", matcher)
        # Only a finished save counts as 已保存 — a failed one stays retryable.
        self.assertIn('STATE_SUCCESS.equals(task.optString("state", ""))', text)
        # Both save surfaces relabel.
        self.assertIn('fabSaveItem.setText(saved ? "已保存 · 在下载中心" : "保存视频")', text)
        self.assertIn('downloadAction.setText("已保存 · 在下载中心")', text)
        # And the tap lands on the saved file, not on a duplicate.
        absorb = text[text.index("private boolean absorbIntoExistingTask(String url)"):]
        absorb = absorb[: absorb.index("private void enqueueVerifiedDownload()")]
        self.assertIn("findTaskForMedia(url, pageUrl, title)", absorb)
        self.assertIn("该视频已保存过，已打开下载中心", absorb)
        self.assertIn("launchDownloadsActivity();", absorb)

    def test_every_hand_drawn_button_answers_a_press(self) -> None:
        """点击了根本没有效果 — a GradientDrawable has no pressed state.

        Every custom pill took a tap with total silence, which reads as a
        dead control. One wrapper (PressFeedback) turns each plate into a
        RippleDrawable — the platform's touch answer over the exact look
        each button already wears — and every button background site in all
        three activities goes through it.
        """
        wrapper = (self.java_dir / "PressFeedback.java").read_text(encoding="utf-8")
        self.assertIn("new RippleDrawable(ColorStateList.valueOf(RIPPLE), plate, null)", wrapper)
        for name in ("MainActivity.java", "DownloadsActivity.java", "PlayerActivity.java"):
            source = (self.java_dir / name).read_text(encoding="utf-8")
            self.assertIn("PressFeedback.on(", source, name)
        # Spot-check the surfaces the user actually taps: the FAB cells, the
        # download rows, the player transport, the dialog buttons.
        main = self._activity()
        self.assertIn("cell.setBackground(PressFeedback.on(pillCellBackground()));", main)
        self.assertIn("downloadAction.setBackground(PressFeedback.on(primaryActionBackground(ready)));", main)
        downloads = (self.java_dir / "DownloadsActivity.java").read_text(encoding="utf-8")
        self.assertIn("b.setBackground(PressFeedback.on(gd));", downloads)
        player = (self.java_dir / "PlayerActivity.java").read_text(encoding="utf-8")
        self.assertIn("b.setBackground(PressFeedback.on(iconPlate()));", player)
        self.assertIn("rewind.setBackground(PressFeedback.on(iconPlate()));", player)

    def test_screen_off_online_playback_is_a_keep_alive_not_a_handoff(self) -> None:
        """息屏播放 for the online player: the WebView keeps playing.

        The previous attempt handed the sniffed stream to PlaybackService and
        paused the WebView — the track restarted from zero, which the user
        rejected outright. The online player lives in a cross-origin iframe:
        its position is unreadable and its stream URL is not reopenable, so
        nothing outside can take the playback over. What ends audio when the
        screen goes dark is the process being frozen and the CPU dozing —
        so WebAudioService holds exactly those two: a mediaPlayback foreground
        promotion and a partial wake lock, and the WebView is never paused.
        """
        text = self._activity()
        # The handover moment: leaving our play page for an allowed external
        # player starts the keep-alive — main frame only, and only from the
        # play page (a random link out of some other page starts nothing).
        handover = text[text.index("learnPlayerUrl(url);\n            boolean blocked"):]
        handover = handover[: handover.index("return blocked;")]
        self.assertIn("isOurPlayPage(currentFrameUrl)", handover)
        self.assertIn("WebAudioService.start(MainActivity.this, t)", handover)
        self.assertIn("request.isForMainFrame()", handover)
        # Back on a bundled page, the keep-alive has no page left to keep
        # alive: stopped in onPageStarted, where file:// returns land.
        pagestart = text[text.index("public void onPageStarted("):]
        pagestart = pagestart[: pagestart.index("public boolean shouldOverrideUrlLoading(")]
        self.assertIn('WebAudioService.stop(MainActivity.this)', pagestart)
        self.assertIn('url.startsWith("file:")', pagestart)
        # And when the activity itself is gone, the notification must not
        # survive it: the stop lives in onDestroy.
        destroy = text[text.index("protected void onDestroy()"):]
        destroy = destroy[: destroy.index("// -------------------------------------------------------- tablet adaptation")]
        self.assertIn("WebAudioService.stop(this)", destroy)
        # The WebView is never paused for background audio — that call is
        # exactly what froze the picture after the first lock screen.
        self.assertNotIn("webView.onPause()", text)

        svc = (self.java_dir / "WebAudioService.java").read_text(encoding="utf-8")
        # A keep-alive, not a player: no media objects, only the two
        # resources the WebView needs to keep decoding.
        self.assertNotIn("MediaPlayer", svc)
        self.assertNotIn("SurfaceView", svc)
        self.assertIn("PowerManager.PARTIAL_WAKE_LOCK", svc)
        self.assertIn('"asmrlib:web-audio"', svc)
        self.assertIn("ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK", svc)
        # The notification names what is happening and can stop it.
        self.assertIn("停止", svc)
        self.assertIn("ACTION_STOP", svc)
        self.assertIn("回到应用查看画面", svc)

    def test_the_external_reveal_waits_for_the_page_to_settle(self) -> None:
        """白屏闪了一下, final round: the curtain lied about readiness.

        An external player page reports progress 100% while its document is
        still an unpainted white body — the site's own CSS has not applied
        yet. Lifting the loading curtain on that moment is the flash the user
        saw twice. The reveal for an external page now waits for the load
        event plus a settle; our own asset pages keep the snappy
        progress-based reveal, and a page that never finishes is released by
        a hard cap instead of holding the curtain hostage.
        """
        text = self._activity()
        self.assertIn("EXTERNAL_REVEAL_SETTLE_MS = 600L", text)
        self.assertIn("SPLASH_HARD_CAP_MS = 8000L", text)
        # Progress no longer lifts the curtain for an external page — the
        # bar still moves, the reveal does not ride on it.
        progress = text[text.index("public void onProgressChanged("):]
        progress = progress[: progress.index("private final class ShieldWebViewClient")]
        self.assertIn("if (external) {", progress)
        self.assertIn("return;", progress[: progress.index("mainHandler.postDelayed", progress.index("if (external) {"))])
        # The load event is the honest moment, with the settle attached.
        finished = text[text.index("public void onPageFinished(WebView view, String url) {"):]
        finished = finished[: finished.index("public void onReceivedError(")]
        self.assertIn('url.startsWith("http")', finished)
        self.assertIn("EXTERNAL_REVEAL_SETTLE_MS", finished)
        # The hard cap is armed on every real page start, generation-checked
        # so it can neither lift a newer curtain nor be cancelled by one.
        started = text[text.index("public void onPageStarted("):]
        started = started[: started.index("public boolean shouldOverrideUrlLoading(")]
        self.assertIn("SPLASH_HARD_CAP_MS", started)
        self.assertIn("final int generation = pageGeneration;", started)

    def test_the_save_action_lives_inside_the_download_card(self) -> None:
        text = self._activity()
        # One action button, built into the panel card — not added to the decor.
        self.assertIn("private Button buildDownloadAction()", text)
        self.assertIn("downloadAction = buildDownloadAction();", text)
        self.assertIn("card.addView(downloadAction, actionLp);", text)
        # Its enabled/label state is derived from the captured URLs, and both
        # the icon highlight and the button read the same predicate so they can
        # never disagree about whether a save is possible.
        self.assertIn("private boolean hasSavableMedia()", text)
        self.assertIn("private void refreshDownloadActionState()", text)
        state = text[text.index("private void refreshDownloadActionState()"):]
        state = state[: state.index("private android.graphics.drawable.GradientDrawable "
                                     "primaryActionBackground")]
        self.assertIn("isHlsUrl(verifiedHlsUrl)", state)
        self.assertIn("isDirectDownloadUrl(verifiedDownloadUrl)", state)
        self.assertIn("downloadAction.setEnabled(ready)", state)
        # The card is taller now, so the list height has to reserve the button
        # or the action is pushed past the bottom edge of the window.
        reserved = text[text.index("private int downloadListHeight()"):]
        reserved = reserved[: reserved.index("private int floatingChipHeight()")]
        self.assertIn("dp(46)", reserved)

    def test_resize_is_absorbed_and_the_chrome_reapplied(self) -> None:
        text = self._activity()
        # Because the manifest now absorbs screenLayout/smallestScreenSize/
        # density, the Activity is no longer recreated on those changes — so
        # the chrome it sized for the old window has to be re-applied by hand.
        self.assertIn("public void onConfigurationChanged(Configuration newConfig)", text)
        handler = text[text.index("public void onConfigurationChanged(Configuration newConfig)"):]
        handler = handler[: handler.index("protected void onDestroy()")]
        for marker in (
            "downloadEntryLayout()",
            "splashBoxLayout()",
            # Built-once views keep px computed for the old density, so the
            # entry is restyled and the card is rebuilt rather than patched.
            "styleDownloadEntry(downloadEntry, hasSavableMedia())",
            "removeView(downloadPanel)",
            "openDownloadPanel()",
        ):
            self.assertIn(marker, handler)
        # The action button is a child of the discarded card. Without dropping
        # the reference, rebuilding the panel re-adds a view that still has a
        # parent and throws "child already has a parent".
        self.assertIn("downloadAction = null;", handler)

    def test_density_derived_text_metrics_are_reapplied(self) -> None:
        text = self._activity()
        # setTextSize resolves sp to px immediately, and a Button's own
        # metrics are resolved the same way. Once a density change no longer
        # recreates the Activity, an already-inflated view keeps the old px —
        # observed on the emulator at 240dpi after a resize from 480dpi. The
        # icon entry is restyled from the current metrics at every state
        # change, not just at creation.
        self.assertIn("private void styleDownloadEntry(Button chip, boolean ready)", text)
        # Creation, a fresh sniff, a navigation, and a configuration change.
        self.assertIn("styleDownloadEntry(button, false);", text)
        self.assertIn("styleDownloadEntry(downloadEntry, true);", text)
        self.assertIn("styleDownloadEntry(downloadEntry, false);", text)
        self.assertIn("styleDownloadEntry(downloadEntry, hasSavableMedia());", text)

    def test_manifest_absorbs_split_screen_and_fold_resizes(self) -> None:
        manifest = self.manifest.read_text(encoding="utf-8")
        # Entering split-screen, dragging a freeform window or unfolding a
        # foldable changes these; unlisted, they recreate the Activity, which
        # kills the in-flight HLS worker threads and drops the WebView state.
        for marker in (
            "screenLayout",
            "smallestScreenSize",
            "density",
            'android:resizeableActivity="true"',
        ):
            self.assertIn(marker, manifest)
        # Still listed: rotation and the soft keyboard.
        self.assertIn("orientation", manifest)
        self.assertIn("keyboardHidden", manifest)
        # Declared on both the application and the activity.
        self.assertGreaterEqual(manifest.count("android:resizeableActivity="), 2)
        self.assertGreaterEqual(manifest.count("smallestScreenSize"), 2)


class MobileFullscreenChromeTests(unittest.TestCase):
    """The download-centre entry must not bleed over a fullscreen video.

    The entry lives in the decor FrameLayout, which is also where
    ``onShowCustomView`` parks the video. Without explicit hiding the
    44dp icon stays on top of the picture, and a panel that was open
    before the gesture survives into the immersive view — both reported
    as "下载的button影响我全屏了".
    """

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.text = (self.root / "android" / "app" / "src" / "main" / "java"
                     / "com" / "asmrlib" / "archiver" / "MainActivity.java"
                     ).read_text(encoding="utf-8")

    def test_fields_exist(self) -> None:
        self.assertIn("private boolean panelWasVisibleBeforeFullscreen;", self.text)
        self.assertIn("private boolean entryWasVisibleBeforeFullscreen;", self.text)

    def test_enter_fullscreen_hides_the_floating_entry(self) -> None:
        # The state we need to restore is captured before we hide the chrome.
        self.assertRegex(
            self.text,
            r"entryWasVisibleBeforeFullscreen\s*=\s*downloadEntry\.getVisibility\(\)\s*==\s*View\.VISIBLE",
        )
        self.assertIn("downloadEntry.setVisibility(View.GONE);", self.text)
        # An open panel must not survive the fullscreen gesture: it sits in
        # the same decor and would float above the video.
        self.assertRegex(
            self.text,
            r"if\s*\(\s*panelVisible\s*\)\s*closeDownloadPanel\(\)",
        )

    def test_exit_fullscreen_restores_the_floating_entry(self) -> None:
        # Only restore if it was visible in the first place — a layout pass
        # before any page loaded must not conjure the button out of thin air.
        self.assertRegex(
            self.text,
            r"if\s*\(\s*downloadEntry\s*!=\s*null\s*&&\s*entryWasVisibleBeforeFullscreen\s*\)\s*\{[^}]*downloadEntry\.setVisibility\(View\.VISIBLE\)",
        )
        self.assertRegex(
            self.text,
            r"if\s*\(\s*panelWasVisibleBeforeFullscreen\s*\)\s*openDownloadPanel\(\)",
        )
        # The "was visible" latches are cleared every time so a second
        # fullscreen gesture cannot leak the first one's state.
        self.assertIn("entryWasVisibleBeforeFullscreen = false;", self.text)
        self.assertIn("panelWasVisibleBeforeFullscreen = false;", self.text)


class MobileDownloadsActivityTests(unittest.TestCase):
    """The download centre lives in its own Activity.

    Putting it in MainActivity's decor meant the floating entry button and
    the in-app panel could overlap the WebView's inline fullscreen video
    (the "下载按钮挡住全屏" complaint). A dedicated Activity moves the
    whole thing into a separate window, and the entry button launches it
    with an Intent rather than toggling a panel.
    """

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.java_dir = (
            self.root / "android" / "app" / "src" / "main" / "java" / "com" / "asmrlib" / "archiver"
        )
        self.manifest = (self.root / "android" / "app" / "src" / "main"
                         / "AndroidManifest.xml").read_text(encoding="utf-8")
        self.main_text = (self.java_dir / "MainActivity.java").read_text(encoding="utf-8")
        self.dl_text = (self.java_dir / "DownloadsActivity.java").read_text(encoding="utf-8")
        self.player_text = (self.java_dir / "PlayerActivity.java").read_text(encoding="utf-8")

    def test_activity_is_declared_in_the_manifest(self) -> None:
        # Single instance of the activity, single declaration. Two copies
        # would split the user across two windows the system has to keep in
        # sync.
        self.assertIn(
            'android:name=".DownloadsActivity"',
            self.manifest,
        )
        self.assertEqual(
            self.manifest.count('android:name=".DownloadsActivity"'), 1
        )

    def test_activity_owns_its_own_task_list_not_main_activitys(self) -> None:
        # The store key is the same on both sides so a change in one shows
        # up in the other on next load — but the in-memory list lives in
        # this Activity, not MainActivity. Sharing the list would make
        # MainActivity's poll fight this Activity's render every 800ms.
        self.assertIn('private final List<JSONObject> downloadTasks', self.dl_text)
        # Same store key, two different lists.
        self.assertIn('KEY_DOWNLOADS = "download_tasks"', self.dl_text)
        self.assertIn('KEY_DOWNLOADS = "download_tasks"', self.main_text)
        # But the in-memory list class field is only declared in this file
        # — MainActivity keeps its own (the panel still uses it).
        self.assertIn(
            'private final List<JSONObject> downloadTasks', self.dl_text
        )

    def test_open_action_launches_player_activity(self) -> None:
        # 打开 must hand the saved file to PlayerActivity with both the URI
        # and the title. The title is what the player will render in its
        # top bar; without it, the bar shows the file extension instead.
        self.assertIn("new Intent(this, PlayerActivity.class)", self.dl_text)
        self.assertIn("PlayerActivity.EXTRA_URI", self.dl_text)
        self.assertIn("PlayerActivity.EXTRA_TITLE", self.dl_text)
        self.assertIn("FLAG_GRANT_READ_URI_PERMISSION", self.dl_text)
        # The player shows the file's facts and offers 重命名 / 删除, so the id
        # and the measured size travel with the intent — without them the
        # player can only offer actions that do nothing.
        self.assertIn("PlayerActivity.EXTRA_TASK_ID", self.dl_text)
        self.assertIn("PlayerActivity.EXTRA_BYTES", self.dl_text)
        self.assertIn("PlayerActivity.EXTRA_STATE", self.dl_text)
        # And the record actions come back here rather than being implemented
        # a second time inside the player.
        self.assertIn("startActivityForResult(open, REQUEST_PLAYER)", self.dl_text)
        self.assertIn("PlayerActivity.RESULT_ACTION", self.dl_text)

    def test_player_page_adapts_to_portrait_and_landscape(self) -> None:
        """The saved-file page has to work on its side, not just upright.

        The page used to be one full-bleed surface with a title bar, so a
        rotation kept the same arrangement and the picture was letterboxed
        beside a status bar — the "播放页一点没有改" complaint. Portrait is a
        stage plus a facts column; landscape is the whole window, immersive.
        """
        source = self.player_text
        self.assertIn("private void applyOrientationLayout()", source)
        layout = source[source.index("private void applyOrientationLayout()"):]
        layout = layout[: layout.index("private int stageHeightPortrait()")]
        self.assertIn("ORIENTATION_LANDSCAPE", layout)
        # The column is gone in landscape: a squeezed half-column is worse
        # than none, and the video should own the window.
        self.assertIn("infoScroll.setVisibility(landscape ? View.GONE : View.VISIBLE)", layout)
        self.assertIn("enterImmersive()", layout)
        # Stage height follows the real aspect ratio, not a hard-coded 16:9.
        self.assertIn("private int stageHeightPortrait()", source)
        self.assertIn("metaHeight / (float) metaWidth", source)
        # Rotation is absorbed by the manifest, so the layout has to be redone
        # by hand — otherwise nothing reacts to the new orientation.
        cfg = source[source.index("public void onConfigurationChanged"):]
        cfg = cfg[: cfg.index("// -------------------------------------------------------------- utilities")]
        self.assertIn("applyOrientationLayout()", cfg)

    def test_player_page_shows_the_files_own_facts(self) -> None:
        """Facts come from the file, off the main thread.

        MediaMetadataRetriever on a gigabyte TS is not main-thread work, and
        a page that opens on a black rectangle while it waits is the thing
        that made this page look unfinished.
        """
        source = self.player_text
        self.assertIn("MediaMetadataRetriever", source)
        self.assertIn("METADATA_KEY_VIDEO_WIDTH", source)
        self.assertIn("METADATA_KEY_DURATION", source)
        self.assertIn("getFrameAtTime(", source)
        self.assertIn('"asmrlib-meta"', source)
        # A poster over the surface until the first frame lands, and the same
        # frame as the thumbnail in the detail card.
        self.assertIn("cover.setImageBitmap(poster)", source)
        self.assertIn("thumb.setImageBitmap(poster)", source)
        self.assertIn("cover.setVisibility(View.GONE)", source)
        # Presented as a table, not as pills: a third pill squeezed the row
        # until its own label wrapped (measured 186px vs 96px).
        self.assertIn("private View metaRow(String label, String value, boolean divider)", source)
        self.assertNotIn('chip("大小"', source)
        # Rich table: duration, resolution, size, container, bitrate, saved
        # time, where it lives. A page that shows a bare file name reads as a
        # placeholder, not as the user's own library.
        for row in ("时长", "分辨率", "文件大小", "容器格式", "码率", "保存时间", "存储位置"):
            self.assertIn('metaRow("' + row + '"', source)
        self.assertIn("METADATA_KEY_BITRATE", source)
        self.assertIn("EXTRA_SAVED_AT", source)

    def test_player_action_row_is_one_size_one_gap(self) -> None:
        """重命名 / 删除 / 其他应用打开 are three identical plates.

        They used to be icon+label Buttons, and a Button drags the platform's
        own text size in with it: 重命名 wrapped onto two lines and 其他应用打开
        lost its last line outright. Measured on device the three cells came out
        103 / 132 / 95 px tall on one shared baseline, which reads as broken no
        matter how carefully the margins are chosen. Icons carry no text
        metrics, so equal cells are equal by construction — and every geometry
        decision lives in one helper rather than three call sites.
        """
        source = self.player_text
        self.assertIn(
            "private void addAction(LinearLayout row, int glyph, String description,",
            source)
        self.assertEqual(source.count("addAction(actions, PlayerGlyph."), 3)
        for description in ('"重命名"', '"删除文件"', '"其他应用打开"'):
            self.assertIn(description, source)
        # The words are for TalkBack; the eye gets the drawing.
        self.assertIn("b.setContentDescription(description)", source)
        self.assertIn('new PlayerGlyph(glyph, dp(20), "", Color.parseColor(tint))', source)
        # One cell shape, one gap, defined once.
        self.assertIn("new LinearLayout.LayoutParams(0, dp(44), 1f)", source)
        self.assertEqual(source.count("lp.rightMargin = dp(8);"), 1)
        # No text button survives in the action row.
        self.assertNotIn("setCompoundDrawablesWithIntrinsicBounds", source)

    def test_download_row_buttons_share_the_width_instead_of_asking_for_it(
            self) -> None:
        """The download centre's pills are weighted, not wrapped.

        Same root cause as the player action row, one file over: a Button
        carries the platform's text metrics, so a WRAP_CONTENT pill in a
        three-cell row asks for more width than the row has. On a 360dp phone
        it fits and nobody notices; on a 320dp one at font scale 1.3 both cards
        rendered 「删」 sitting on top of a clipped 「除」. Measured before the
        fix, the narrow config produced two pills of 170px and dropped the
        third off the row entirely; after, three of 140px with 46px gaps.

        Equal weights cannot overflow, because the row *is* the card's width.
        """
        text = self.dl_text
        start = text.index("private Button rowButton(String label, View.OnClickListener onClick)")
        end = text.index("// ---------------------------------------------------------- store", start)
        body = text[start:end]
        # A third of a small card cannot afford stylePill's 18dp gutters.
        self.assertIn("b.setPadding(dp(6), 0, dp(6), 0);", body)
        self.assertIn("b.setSingleLine(true);", body)
        # Equal share of the row, one height, one gap — and the platform's
        # minimums zeroed so they cannot reintroduce a floor the weight ignores.
        self.assertIn("new LinearLayout.LayoutParams(0, dp(40), 1f)", body)
        self.assertIn("lp.setMargins(dp(4), 0, dp(4), 0);", body)
        for zeroed in ("b.setMinWidth(0);", "b.setMinimumWidth(0);",
                       "b.setMinHeight(0);", "b.setMinimumHeight(0);"):
            self.assertIn(zeroed, body)
        # The wrapped width is what broke it, so pin that it is gone.
        self.assertNotIn("WRAP_CONTENT, dp(40)", body)

    def test_download_row_keeps_labels_that_fit_a_third_of_the_row(self) -> None:
        """移除, not 移除记录 — the full phrase belongs in the label.

        Four characters do not fit a third of a 320dp card at font scale 1.3,
        and the row ellipsises rather than wraps now, so the long form would
        have become 「移…」. The accessibility label costs no width, so the
        phrase lives there.
        """
        text = self.dl_text
        row = text.index("private View buildDownloadRow(")
        block = text[row:text.index("private Button rowButton(", row)]
        self.assertIn('rowButton("移除"', block)
        self.assertNotIn('"移除记录"', block)
        self.assertIn('remove.setContentDescription("移除这条记录");', block)

    def test_player_picks_a_frame_that_is_not_black(self) -> None:
        """getFrameAtTime takes microseconds; METADATA_KEY_DURATION reports ms.

        Mixing the two put every sample inside the first second, which on a
        concatenated TS is a fade from black — so the card thumbnail rendered as
        a solid black rectangle. The samples are spread across the file, the
        first bright one wins, and the facts must not wait on any of it: the
        table is published before the frame hunt, because deep sync seeks into a
        949 MB file take seconds and a half-empty table looks like a failure.
        """
        source = self.player_text
        self.assertIn("durationMs / 10L * 1000L", source)
        self.assertIn("durationMs / 2L * 1000L", source)
        self.assertIn("OPTION_CLOSEST_SYNC", source)
        self.assertIn("private static float luminance(Bitmap b)", source)
        self.assertIn("luminance(f) > 24f", source)
        self.assertLess(source.index("publishFacts(w, h, dur, bitrate)"),
                        source.index("pickFrame(r, dur)"))
        # A stream never frame-hunts: its picture is the network cover, and
        # deep sync-seeks over the proxy would stall the stream's head.
        self.assertIn("frame = remote ? null : pickFrame(r, dur)", source)
        self.assertIn("if (!remote) publishFrame(frame);", source)

    def test_the_player_remembers_where_the_user_stopped(self) -> None:
        """A 22-minute file must not restart at 0:00 every single time.

        The decision itself — resume, start over, or forget — is pure Java in
        ``PlaybackResume`` and is *executed* by
        ``tests/test_playback_resume_java.py``. What this pins is the wiring,
        because every hook here is somewhere the feature silently stops
        working: a position written after the player was released, a handover
        that forgets to save, or a finished file that keeps its record and
        drops the user into the closing seconds.
        """
        source = self.player_text
        # One record per source, keyed on the task id so a rename cannot
        # orphan it — 重命名 rewrites the URI and leaves the task id alone.
        # A remote stream has no task id, so its key is the remote URL (a
        # signed URL changes per session, which makes a spent stream record
        # harmless: it keys to nothing on the next open).
        self.assertIn(
            'resumeKey = PlaybackResume.keyFor(taskId,',
            source)
        self.assertIn('!remoteUrl.isEmpty() ? remoteUrl', source)
        self.assertIn("PlaybackResume.startPosition(saved[0], saved[1])", source)
        # Saved on the way out, and again on the handover to the service — and
        # the handover must save *before* releasePlayer(), which clears
        # `prepared` and would make the save a no-op.
        self.assertEqual(source.count("saveResume();"), 2)
        self.assertIn("saveResume();\n        releasePlayer();", source)
        self.assertLess(
            source.index("saveResume();\n        releasePlayer();"),
            source.index("PlaybackService.start(this, mediaUri.toString()"))
        # Spent on completion, so the next open starts at the beginning.
        self.assertLess(source.index("syncProgress();", source.index("onCompletion")),
                        source.index("clearResume();"))
        # And it says so: opening at 5:00 with no word reads as a broken file.
        self.assertIn('"已续播到 " + formatTime((int) resumeAnnounceMs)', source)
        # The same preferences file the download centre writes, and a store
        # that is never rewritten from an unreadable read. The store IO lives
        # in ResumeStore, shared with PlaybackService — two implementations
        # of the same JSON would drift apart the first time one side evolved.
        self.assertIn('private static final String PREFS = "asmrlib";', source)
        self.assertIn("ResumeStore.read(getSharedPreferences(PREFS, MODE_PRIVATE)",
                      source)
        store = (self.java_dir / "ResumeStore.java").read_text(encoding="utf-8")
        self.assertIn("private static JSONObject readStore(SharedPreferences prefs)",
                      store)
        self.assertIn('prefs.edit().putString(KEY + ".bad", raw).apply();', store)
        self.assertIn("PlaybackResume.keysToDrop(keys", store)

    def test_screen_off_playback_is_audio_only_and_service_backed(self) -> None:
        """息屏播放 is not a flag on the video player — it cannot be.

        Turning the screen off destroys the SurfaceView's surface, so a video
        MediaPlayer stops and no permission changes that. What the platform
        does support is audio-only playback that outlives the activity, and
        that needs three things together: a partial wake lock (CPU awake,
        screen off), a mediaPlayback foreground service (process alive), and a
        MediaSession (controllable from the lock screen).
        """
        svc = (self.java_dir / "PlaybackService.java").read_text(encoding="utf-8")
        # Audio only: the service must never take a surface.
        self.assertNotIn("setDisplay(", svc)
        self.assertNotIn("new SurfaceView(", svc)
        self.assertNotIn("SurfaceHolder", svc)
        self.assertIn("setWakeMode(this, PowerManager.PARTIAL_WAKE_LOCK)", svc)
        self.assertIn("ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK", svc)
        self.assertIn("new MediaSession(this", svc)
        self.assertIn("session.setActive(true)", svc)
        # Focus and the headphone-unplug pause: a media app that ignores both
        # talks over the user's calls and keeps playing into the room.
        self.assertIn("requestAudioFocus", svc)
        self.assertIn("ACTION_AUDIO_BECOMING_NOISY", svc)

        manifest = self.manifest
        self.assertIn('android:name=".PlaybackService"', manifest)
        self.assertIn('android:foregroundServiceType="mediaPlayback"', manifest)
        for permission in ("WAKE_LOCK", "FOREGROUND_SERVICE",
                           "FOREGROUND_SERVICE_MEDIA_PLAYBACK"):
            self.assertIn("android.permission." + permission, manifest)

        # The activity hands over instead of playing twice.
        player = self.player_text
        self.assertIn("private void startSleepPlayback()", player)
        handover = player[player.index("private void startSleepPlayback()"):]
        handover = handover[: handover.index("private View buildBackgroundBar()")]
        # Release first: two MediaPlayers on one file would fight.
        self.assertLess(
            handover.index("releasePlayer();"),
            handover.index("PlaybackService.start("),
        )
        self.assertIn("finish();", handover)
        self.assertIn("PlaybackService.isRunning()", player)
        self.assertIn("回到画面播放", player)
        self.assertIn("static final int MOON = 10;", player)

    def test_sleep_playback_keeps_walking_the_queue_and_the_place(self) -> None:
        """息屏连播: the screen going dark is where ASMR listening lives.

        A queue handed to the page but not to the service meant continuous
        playback stopped at the first track the moment the user locked the
        phone — precisely the form this app exists for. Three pins: the
        handover carries the queue (plus the resume key, so the service
        writes into the record the page already owns instead of forking a
        second one); the service steps forward on completion, skipping a
        dead entry rather than stranding the rest; and the service writes
        the place on a cadence, because the tick's ten-second staleness is
        the difference between "resumed where I was" and "resumed an hour
        before I fell asleep".
        """
        svc = (self.java_dir / "PlaybackService.java").read_text(encoding="utf-8")
        player = self.player_text
        # The handover carries queue, index and the page's own resume key.
        self.assertIn(
            "PlaybackService.start(this, mediaUri.toString(), mediaTitle, position,\n"
            "                resumeKey,",
            player)
        for marker in (
            'private static final String EXTRA_QUEUE = "queue";',
            'private static final String EXTRA_RESUME_KEY = "resume_key";',
            "private boolean advanceQueue()",
            'String kind = entry.optString("k", "direct").trim();',
            'proxied = StreamProxy.register(url, referer);',
            "resumeKey = PlaybackResume.keyFor(\"\", url);",
        ):
            self.assertIn(marker, svc)
        # Completion: spend the place, then walk — a finished track must not
        # resume into its closing seconds, and the last track stops as before.
        completion = svc[svc.index("public void onCompletion(MediaPlayer mp)"):]
        completion = completion[: completion.index("private boolean advanceQueue()")]
        self.assertIn("clearResume();", completion)
        self.assertIn("if (advanceQueue()) return;", completion)
        # One proxy reference, held once: a queue advance re-enters
        # startPlayback with the reference already held.
        self.assertIn("if (holdingProxy && !ours)", svc)
        self.assertIn("if (ours && !holdingProxy)", svc)
        # The cadence: 10 ticks, then write; and a pause writes immediately.
        self.assertIn("if (++resumeCountdown >= 10)", svc)
        self.assertIn("saveResume();\n        publishState();", svc)
        # The lock screen's scrubber divides by the published duration, so
        # prepare must publish more than the placeholder zero.
        self.assertIn("updateSessionMetadata();", svc)
        self.assertIn("METADATA_KEY_DURATION, duration", svc)
        # The notification tap target reopens the CURRENT track with its
        # queue, so taking the screen back does not drop 连播.
        self.assertIn("EXTRA_REMOTE_QUEUE, queue.toString()", svc)
        self.assertIn("EXTRA_REMOTE_INDEX, queueIndex", svc)

    def test_the_queue_walk_balances_its_proxy_reference(self) -> None:
        """One register, one release — across the whole queue.

        register() acquires a reference and only onDestroy releases one, so
        a page that advanced through ten tracks without letting go pinned
        the loopback server open for the rest of the process. Every route
        into another track — completion, 下一轨, the queue panel — now walks
        through jumpQueue, which drops its reference before taking the next,
        skips a dead entry instead of letting it end the walk, and refuses a
        bad one without half-taking it.
        """
        player = self.player_text
        # The walk is one section (switchQueue/stepQueue/previousQueue/
        # nextQueue/jumpQueue); take it as a block so the assertions stay
        # bound to the code that owns them.
        walk = player[player.index("queue walk"):]
        walk = walk[: walk.index("private void scheduleAutoRetry()")]
        # Release before the next acquire, and only when one is held.
        self.assertIn("if (!proxiedUrl.isEmpty()) {", walk)
        self.assertIn("StreamProxy.release();", walk)
        self.assertLess(walk.index("StreamProxy.release();"),
                        walk.index("StreamProxy.register(url, referer);"))
        # A refused registration skips to the following entry: the step asks
        # for the next index in the direction it was travelling.
        self.assertIn("if (jumpQueue(next)) {", walk)
        self.assertIn("next += delta", walk)
        # The per-entry referer the bridge computed rides along.
        self.assertIn('String referer = entry.optString("r", "").trim();', walk)
        # And the video page's header follows the track it is actually
        # playing — a bar naming track one over track four reads as a bug.
        self.assertIn("if (headerTitleView != null) headerTitleView.setText(label);",
                      walk)
        # Completion shares that walk rather than carrying its own copy —
        # the copy is how the two routes drifted apart before.
        advance = player[player.index("private boolean advanceQueue()"):]
        advance = advance[: advance.index("private void open()")]
        self.assertIn("return stepQueue(1);", advance)

    def test_queue_entries_carry_the_referer_of_their_own_host(self) -> None:
        """A queue can span CDNs; one page identity cannot serve them all.

        refererFor's cross-origin rule applied to the tapped track alone:
        every later track inherited it, and a track on another host got a
        Referer it has no reason to accept. The bridge computes the rule per
        entry before the queue leaves for the player — unless the entry pins
        its own Referer ("rr"), which is the embed sniffer's observed header
        and outranks any heuristic.
        """
        bridge = (self.java_dir / "DiscoverBridge.java").read_text(encoding="utf-8")
        self.assertIn('entry.put("r", entryPinned.isEmpty()', bridge)
        self.assertIn('? refererFor(entry.optString("u", "").trim(), referer)', bridge)
        self.assertIn('entry.optString("rr", "").trim()', bridge)
        self.assertIn("if (entry == null) continue;", bridge)
        # The tapped track's pinned Referer reaches the player intent too,
        # not just the queue entries that survive a later jump.
        self.assertIn('firstPinned.isEmpty() ? refererFor(target, referer) : firstPinned',
                      bridge)

    def test_player_page_can_make_the_picture_bigger(self) -> None:
        """A 16:9 file on a 9:16 phone is 607px tall no matter the padding.

        "Bigger" therefore has to be a real choice in the UI, not a layout
        tweak: 全屏 rotates to an immersive landscape, and 填充 opts into
        crop-to-fill for the user who would rather lose the sides than live
        with letterboxing.
        """
        source = self.player_text
        self.assertIn("private void setLandscapeFullscreen(boolean on)", source)
        self.assertIn("SCREEN_ORIENTATION_SENSOR_LANDSCAPE", source)
        self.assertIn("private void setFillScreen(boolean fill)", source)
        self.assertIn("VIDEO_SCALING_MODE_SCALE_TO_FIT_WITH_CROPPING", source)
        self.assertIn("private int scalingMode()", source)
        # Fill also has to grow the stage, or the crop has nowhere to go.
        stage = source[source.index("private int stageHeightPortrait()"):]
        stage = stage[: stage.index("private int statusBarInset()")]
        self.assertIn("if (fillScreen)", stage)
        # Both controls are in the top bar: the transport row is already
        # three thumb-sized targets wide.
        # The controls are icons, not words: four Chinese labels in a row is
        # a settings list, not a transport bar. The words survive as
        # contentDescription so TalkBack still names every control.
        self.assertIn("private static final class PlayerGlyph extends Drawable", source)
        for glyph in ("EXPAND", "COLLAPSE", "FILL", "FIT", "REWIND", "FORWARD", "BACK"):
            self.assertIn("static final int " + glyph + " =", source)
        self.assertIn("private ImageButton iconButton(int glyph, int sizeDp, String description)", source)
        self.assertNotIn('fullscreenButton.setText(', source)
        self.assertIn("setIcon(fullscreenButton, fsGlyph, fsDesc)", source)
        # 全屏 lives on an overlay chip pinned to the video itself (Eara's
        # fullscreen chip), swapped with the same glyph+description pair —
        # a duplicate in the transport row would be three thumb-sized
        # targets wide for the same action.
        self.assertIn("fullscreenButton = chipButton(PlayerGlyph.EXPAND", source)
        # A swapped glyph without a swapped description would be an unlabelled
        # button for anyone using TalkBack.
        self.assertIn('"横屏全屏播放"', source)
        self.assertIn('"退出横屏全屏"', source)
        self.assertIn('"后退 10 秒"', source)
        # And the button must not keep offering 全屏 on a screen that is
        # already landscape because the user turned the device.
        self.assertIn('if (!landscape) landscapeRequested = false;', source)

    def test_fullscreen_letterboxes_by_the_real_aspect(self) -> None:
        """全屏又是被拉伸的: the decoder fills whatever rectangle it is given.

        The stage in landscape is the whole window, the SurfaceView was
        MATCH_PARENT inside it, and the decoder fills its surface — a 16:9
        file on a 20:9 phone window was stretched wide. setVideoScalingMode
        cannot be trusted on a plain SurfaceView, so the shell fits the
        surface (and the poster with it) to the video's real shape itself:
        letterbox bars are the stage's own black, 填充 keeps the stretch the
        user asked for, and an unknown aspect falls back until the retriever
        answers.
        """
        source = self.player_text
        self.assertIn("private void applyStageAspect()", source)
        self.assertIn("videoFrame.setBackgroundColor(Color.BLACK);", source)
        # The refit is armed on the stage's own resize: the manifest absorbs
        # rotation, so a layout change is the only event left to react to.
        self.assertIn("videoFrame.addOnLayoutChangeListener", source)
        self.assertIn("if (right - left == oldRight - oldLeft", source)
        fit = source[source.index("private void applyStageAspect()"):]
        fit = fit[: fit.index("private int scalingMode()")]
        # The decision reads the video's dimensions, not an assumption.
        self.assertIn("metaWidth <= 0 || metaHeight <= 0", fit)
        self.assertIn("fillScreen || metaWidth <= 0", fit)
        # Both surface and poster follow the same rectangle, so the poster
        # never sits on a different shape than the picture underneath it.
        self.assertEqual(fit.count("Gravity.CENTER"), 2)
        self.assertIn("surface.setLayoutParams", fit)
        self.assertIn("cover.setLayoutParams", fit)
        # Applied when the stage's shape changes, not only once at build.
        self.assertIn("applyStageAspect();\n    }", source)

    def test_the_player_scrubs_with_a_horizontal_drag(self) -> None:
        """滑动快进 — the gesture every serious player ships, and the user
        asked for by name.

        The controls root used to own only a click: tap toggles, and a
        horizontal drag did nothing. One gesture detector now owns both
        motions so they can never misfire for each other: a horizontal drag
        shows a centred bubble (快进/快退 + the target time) and commits a
        clamped seek on release; a confirmed tap still toggles the bars.
        """
        source = self.player_text
        self.assertIn("new GestureDetector(this,", source)
        self.assertIn("controls.setOnTouchListener(new View.OnTouchListener()", source)
        self.assertIn("onSingleTapConfirmed", source)
        self.assertIn("private void showScrubBubble(int seconds)", source)
        self.assertIn("private void commitScrub(int seconds)", source)
        scrub = source[source.index("private void commitScrub(int seconds)"):]
        scrub = scrub[: scrub.index("private ImageButton iconButton(")]
        self.assertIn("player.seekTo(target)", scrub)
        # The bubble names the direction and the destination, and exists
        # only mid-gesture.
        self.assertIn('"快进 "', source)
        self.assertIn('"快退 "', source)
        self.assertIn("scrubBubble.setVisibility(View.VISIBLE)", source)
        # Commit on release only, and a tap (zero delta) seeks nowhere.
        self.assertIn("if (scrubbing) {", source)
        self.assertIn("if (player == null || !prepared || seconds == 0) return;", source)

    def test_the_fill_and_fullscreen_icons_do_not_read_as_the_same(self) -> None:
        """顶栏的两个斜向箭头图标被点名重画.

        FILL was two diagonal arrows sitting beside EXPAND's four — a
        subset of the same shape, unreadable at 18dp. Both are now width
        glyphs: a wide frame crossed by a horizontal double-headed arrow,
        open-sided for 填充 and caged for 恢复.
        """
        source = self.player_text
        glyph = source[source.index("case FILL:\n                case FIT: {"):]
        glyph = glyph[: glyph.index("case REWIND:")]
        # Width, not diagonals: the frame strokes and the arrow heads run
        # horizontally through the centre.
        self.assertIn("canvas.drawLine(ax0, cy, ax1, cy, paint)", glyph)
        self.assertIn("head(canvas, ax0 + s * 0.12f, cy, ax0, cy, s)", glyph)
        self.assertNotIn("arrow(canvas,", glyph)
        # And 全屏 keeps its four corners next to it.
        expand = source[source.index("case EXPAND:"):]
        expand = expand[: expand.index("case FILL:")]
        self.assertEqual(expand.count("arrow(canvas,"), 4)

    def test_the_sleep_notification_returns_to_its_own_file(self) -> None:
        """Tapping the 息屏播放 notification used to open a blank player.

        The service's tap target carried no media: the player opened, found
        no URI, and told the user 找不到要播放的文件 — from a notification
        promising the opposite. The intent now carries the playing file and
        its title; and a media URI the decoder could never open (an opaque
        file: string) is rejected at parse time with the value logged,
        instead of a native 「uri param can not be null」 that names nothing.
        """
        svc = (self.java_dir / "PlaybackService.java").read_text(encoding="utf-8")
        self.assertIn('open.putExtra(PlayerActivity.EXTRA_URI, playingUri)', svc)
        self.assertIn("private String playingUri = \"\"", svc)
        player = self.player_text
        parse = player[player.index("private static Uri parseUri(Intent intent)"):]
        parse = parse[: parse.index("// ----------------------------------"
                                   "-------------------------------- chrome")]
        self.assertIn("if (parsed.getPath() == null) {", parse)
        # The failure that still happens names the URI it failed on.
        self.assertIn('showError("无法读取文件：" + e.getMessage()', player)
        self.assertIn(' + "\\n（" + mediaUri + "）"', player)

    def test_rename_dialog_appears_for_saved_and_failed_hls_tasks(self) -> None:
        # Same policy as MainActivity's panel: rename is offered for an
        # HLS task that is not running and not lost. Direct tasks get open
        # and delete only — their bytes belong to DownloadManager, so the
        # rename path (which moves the file) cannot own them.
        self.assertIn("private void renameDownload(final JSONObject task)", self.dl_text)
        self.assertIn(
            "if (isHls && !STATE_RUNNING.equals(state) && !STATE_LOST.equals(state))",
            self.dl_text,
        )

    def test_store_is_written_the_same_way_as_main_activity(self) -> None:
        # Both activities own the same `download_tasks` key, so both have to
        # emit the same bytes. This one used List.toString(), which separates
        # records with ", " and stringifies each one through a different path
        # than JSONArray — a value the two paths disagree on leaves a string
        # neither side can read back.
        self.assertIn("arr.put(downloadTasks.get(i))", self.dl_text)
        self.assertIn("arr.toString()", self.dl_text)
        self.assertNotIn("downloadTasks.toString()", self.dl_text)

    def test_an_unreadable_store_is_never_written_back_as_empty(self) -> None:
        # Clearing the list in the catch block let the next poll save an empty
        # list, which cost the user every record. The list is only replaced
        # after a successful parse, and the bad string is quarantined.
        method = self.dl_text[self.dl_text.index("private void loadDownloadTasks()"):]
        method = method[:method.index("private void saveDownloadTasks()")]
        self.assertNotIn("download task store corrupt, starting empty", method)
        self.assertGreater(
            method.index("downloadTasks.clear()"),
            method.index("new JSONArray(raw)"),
        )
        self.assertIn('putString(KEY_DOWNLOADS + ".bad", raw)', method)

    def test_row_actions_resolve_the_task_by_id_not_by_captured_object(self) -> None:
        # Same 800ms rebuild as MainActivity's panel: a button must re-resolve
        # its record from the live list instead of trusting the object it was
        # rendered from.
        self.assertIn("private JSONObject findDownloadTask(String taskId, long downloadId)",
                      self.dl_text)
        row = self.dl_text[self.dl_text.index("private View buildDownloadRow("):]
        row = row[:row.index("private JSONObject findDownloadTask(")]
        for action in ("openDownloadedFile(live)", "renameDownload(live)",
                       "removeDownloadRecord(live)", "deleteDownload(live)",
                       "retryDownload(live)", "exportAsMp4(live)"):
            self.assertIn(action, row)
        self.assertEqual(row.count("findDownloadTask(taskId, downloadId)"), 6)

    def test_a_failed_row_gets_a_retry_button(self) -> None:
        """Retry used to exist only in MainActivity's panel.

        The docstring here once said it "requires the in-app worker" and stayed
        there — but HlsJobs is process-wide and HlsDownloader is a plain class,
        so the download centre can own the retry for the records it renders.
        Without it, a failed row's only path back was to leave the page, tap
        through the floating menu, and find the same row in a smaller panel.
        """
        row = self.dl_text[self.dl_text.index("private View buildDownloadRow("):]
        row = row[:row.index("private JSONObject findDownloadTask(")]
        # FAILED (either kind) re-enqueues; a lost HLS task may still have its
        # .parts cache to resume from. A lost direct file's bytes are gone
        # wholesale — it keeps 移除 and gets no retry.
        self.assertIn('rowButton("重试"', row)
        retry = row[row.index('rowButton("重试"'):]
        self.assertIn("STATE_FAILED.equals(state)", row[: row.index('rowButton("重试"')])
        self.assertIn("STATE_LOST.equals(state) && isHls", row[: row.index('rowButton("重试"')])
        self.assertIn("retryDownload(live)", retry)
        # And the worker runs under the same ad-host policy: a retry can
        # never become the way an advert gets fetched.
        self.assertIn("ShieldRules.fromAssets(getAssets(), \"\")", self.dl_text)
        self.assertIn("HlsDownloader.download(", self.dl_text)
        self.assertIn("HlsJobs.put(taskId, job)", self.dl_text)

    def test_the_progress_bar_exists_only_while_running(self) -> None:
        """A finished row's bar sat at its last fraction, a failed one froze
        mid-bar — both read as a download that is still going."""
        row = self.dl_text[self.dl_text.index("private View buildDownloadRow("):]
        row = row[:row.index("private JSONObject findDownloadTask(")]
        self.assertIn("if (STATE_RUNNING.equals(state)) {", row)
        bar = row[row.index("ProgressBar bar = new ProgressBar"):]
        bar = bar[: bar.index("LinearLayout actions = new LinearLayout")]
        # The bar's block is inside the running guard: the guard opens before
        # the bar is built and closes before the actions row.
        self.assertLess(row.index("if (STATE_RUNNING.equals(state)) {"),
                        row.index("ProgressBar bar = new ProgressBar"))

    def test_the_poll_reloads_the_shared_store(self) -> None:
        """The frozen-progress-bar report, fixed at the seam.

        The HLS worker writes its segment progress into prefs from whichever
        Activity is in front, and this page's refreshDownloadStates()
        deliberately skips HLS rows (no DownloadManager row to query). A poll
        that only refreshed and re-rendered sat on a stale copy all download.
        """
        poll = self.dl_text[self.dl_text.index("private final Runnable pollRunnable"):]
        poll = poll[: poll.index("protected void onCreate(")]
        self.assertIn("loadDownloadTasks();", poll)
        self.assertIn("refreshDownloadStates();", poll)
        self.assertLess(
            poll.index("loadDownloadTasks();"),
            poll.index("refreshDownloadStates();"),
        )

    def test_the_progress_rides_a_notification(self) -> None:
        """A download the user left might as well not report progress in-app.

        The user locks the screen precisely because a gigabyte takes a while;
        the notification shade is the only surface still visible. HlsNotify
        covers what the system cannot see — the in-app HLS worker; direct
        DownloadManager saves already carry the system's own.
        """
        notify = self.root / "android" / "app" / "src" / "main" / "java" / "com" / "asmrlib" / "archiver" / "HlsNotify.java"
        self.assertTrue(notify.is_file(), "HlsNotify.java is missing")
        text = notify.read_text(encoding="utf-8")
        self.assertIn("static void progress(", text)
        self.assertIn("static void finished(", text)
        self.assertIn("setOngoing(true)", text)
        self.assertIn("setProgress(total > 0 ? total : 1, done, total <= 0)", text)
        # The finished notification replaces the ongoing one under the same id.
        self.assertEqual(text.count("id(taskId)"), 2)
        # Both activities feed it: MainActivity's worker and a retry started
        # from the download centre.
        self.assertIn("HlsNotify.progress(this, taskId,", self.main_text)
        self.assertIn("HlsNotify.progress(this, taskId,", self.dl_text)
        self.assertIn("HlsNotify.finished(this, taskId, fileName, success)",
                      self.main_text)
        self.assertIn("HlsNotify.finished(DownloadsActivity.this, taskId,", self.dl_text)
        # Throttled: at most one notification a second per task.
        self.assertIn("MIN_INTERVAL_MS = 1000L", text)

    def test_an_orphaned_hls_task_recovers_on_reopen(self) -> None:
        """A worker whose Activity died mid-download left its record running
        in the store forever — a bar frozen at 40% that nothing could finish.
        The registry is process-wide, so a running task absent from it is
        finished or dead, and the file on disk decides which."""
        self.assertIn("private void recoverOrphanedHlsTasks()", self.dl_text)
        recover = self.dl_text[self.dl_text.index("private void recoverOrphanedHlsTasks()"):]
        recover = recover[: recover.index("// ---------------------------------------------------------- chrome")]
        self.assertIn("HlsJobs.get(taskId) != null) continue;", recover)
        self.assertIn("artifactExists(task)", recover)
        self.assertIn('"下载中断，可重试"', recover)
        # onResume is the one moment the page re-reads the store anyway.
        onResume = self.dl_text[self.dl_text.index("protected void onResume()"):]
        onResume = onResume[: onResume.index("protected void onPause()")]
        self.assertIn("recoverOrphanedHlsTasks();", onResume)

    def test_a_saved_ts_exports_to_mp4_for_old_rows_too(self) -> None:
        """导出为mp4到本地 — and the videos downloaded before it landed get it.

        Nothing else on a bare phone opens the app's MPEG-TS artifact: the
        remux is a container change via MediaExtractor → MediaMuxer, no
        codec, no quality loss. Eligibility is decided by the FILE on disk,
        not the record's kind field, because videos saved by older app
        versions have records with no kind at all — and a content:// localUri
        from DownloadManager still resolves through the record's own
        fileName against the public Downloads directory it was enqueued to.
        """
        text = self.dl_text
        self.assertIn('rowButton("导出"', text)
        self.assertIn("private void exportAsMp4(final JSONObject task)", text)
        # The file decides, and the decision needs an existing .ts.
        self.assertIn("private static File exportableFile(JSONObject task)", text)
        exportable = text[text.index("private static File exportableFile(JSONObject task)"):]
        exportable = exportable[: exportable.index("/**\n     * 导出")]
        self.assertIn("STATE_SUCCESS.equals(task.optString(\"state\", \"\"))", exportable)
        self.assertIn('endsWith(".ts")', exportable)
        # Old records: the content:// fallback looks up the record's own
        # fileName in the public Downloads directory the direct saver wrote
        # into — the path it carried is unavailable but the file is not lost.
        artifact = text[text.index("private static File artifactFile(JSONObject task)"):]
        artifact = artifact[: artifact.index("private static File exportableFile")]
        self.assertIn("DIRECTORY_DOWNLOADS", artifact)
        self.assertIn('task.optString("fileName", "")', artifact)
        # Remux, not re-encode: the platform's TS extractor + MP4 muxer.
        ts = (self.java_dir / "TsToMp4.java").read_text(encoding="utf-8")
        self.assertIn("new MediaMuxer(destination, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)", ts)
        self.assertIn("MediaExtractor.SAMPLE_FLAG_SYNC", ts)
        self.assertIn("MediaCodec.BUFFER_FLAG_KEY_FRAME", ts)
        self.assertIn("muxer.writeSampleData(", ts)
        self.assertNotIn("MediaCodec.createDecoderByType", ts)
        # A half-written MP4 is worse than none: the partial is deleted.
        self.assertIn("partial.delete()", ts)
        # The original stays; the export is a second artifact.
        self.assertNotIn("src.delete()", text)

    def test_the_export_lands_where_file_managers_can_see_it(self) -> None:
        """导出到本地却找不到 MP4 — the old destination was invisible.

        The first version remuxed into the app-private offline directory,
        which 文件管理 on Android 11+ cannot browse: the export succeeded and
        the user saw nothing, no notification either. Now the copy goes
        through MediaStore into public Download/ (un-pended on success, the
        partial row deleted on failure), and the shade carries a progress
        notification and an outcome — with the same bar riding the row.
        """
        text = self.dl_text
        self.assertIn("MediaStore.Downloads.EXTERNAL_CONTENT_URI", text)
        self.assertIn('values.put(MediaStore.MediaColumns.RELATIVE_PATH,', text)
        self.assertIn("IS_PENDING", text)
        self.assertIn("getContentResolver().delete(pendingRow, null, null)", text)
        # Progress: row bar + notification, one exporter.
        self.assertIn("HlsNotify.exportProgress(", text)
        self.assertIn("HlsNotify.exportFinished(", text)
        self.assertIn("exportProgress.put(key, pct)", text)
        self.assertIn("正在导出 MP4 · ", text)
        self.assertIn("TsToMp4.Progress", text)
        notify = (self.java_dir / "HlsNotify.java").read_text(encoding="utf-8")
        self.assertIn("正在导出 MP4", notify)
        self.assertIn("MP4 已导出到 Download/", notify)
        self.assertIn("EXPORT_ID_BASE = 55000", notify)

    def test_the_download_center_keeps_whole_page_actions_on_top(self) -> None:
        """打开文件位置 and 清空数据 belong 最最最顶部, not buried.

        The user exported, opened 文件管理, and could not find the MP4 — the
        answer (where the files land) had no button; and the bulk sweep had
        no entry at all. Both now sit in a row directly under the header,
        above the list: 位置 opens the system Downloads view (the directory
        MediaStore exports land in), 清空 reuses 删除's own per-task
        machinery — worker cancel, .parts cleanup, DownloadManager remove —
        behind the same product modal 删除 uses.
        """
        text = self.dl_text
        start = text.index("private void buildChrome()")
        chrome = text[start:text.index("storageLabel.setText(", start)]
        # Order in the header: title stack, then the two actions — before
        # any row is built.
        self.assertIn('openLocation.setText("打开文件位置")', chrome)
        self.assertIn('clearAll.setText("清空数据")', chrome)
        self.assertLess(chrome.index('clearAll.setText("清空数据")'),
                        chrome.index("listContainer = new LinearLayout"))
        # The sweep is 删除, applied to every record — not a weaker copy.
        sweep = text[text.index("private void performClearAll()"):]
        sweep = sweep[: sweep.index("// ---------------------------------"
                                   "------------------------- retry")]
        self.assertIn("performDelete(task.optString(\"taskId\", \"\")", sweep)
        self.assertIn("confirmClearAll()", text)
        self.assertIn("ACTION_VIEW_DOWNLOADS", text)

    def test_delete_cancels_the_running_worker_before_deleting_bytes(self) -> None:
        # The worker lives in the process (HlsJobs), not in this Activity.
        # Without the cancel it kept fetching segments and rewrote the .parts
        # directory the user had just deleted — the row reappeared by itself.
        delete = self.dl_text[self.dl_text.index("private void performDelete("):]
        delete = delete[:delete.index("private void removeDownloadRecord(")]
        self.assertIn("HlsJobs.cancel(taskId)", delete)
        self.assertLess(
            delete.index("HlsJobs.cancel(taskId)"),
            delete.index('deleteTree(new File(localPath + ".parts"))'),
        )
        # And it asks first: the deletion is irreversible and can be gigabytes.
        self.assertIn('setText("删除下载")', self.dl_text)
        # Custom view, not the stock two-button dialog: 取消 and 删除 must not
        # wear the same muted tint, and 删除 gets the red→rose pill. The shell
        # is the product modal (productDialog): a dim scrim, one glass card —
        # the platform AlertDialog's own grey chrome is what the 「modal乱改」
        # complaint was about.
        self.assertNotIn('setNegativeButton(', self.dl_text)
        self.assertNotIn('setPositiveButton(', self.dl_text)
        self.assertIn("private Dialog productDialog(View content)", self.dl_text)
        self.assertIn("new GlassDrawable(dp(24))", self.dl_text)
        self.assertIn("Theme.scrim(DownloadsActivity.this, 70)", self.dl_text)
        dialog = self.dl_text[self.dl_text.index("private void confirmDelete("):]
        dialog = dialog[: dialog.index("private static long taskFileBytes(")]
        self.assertIn('cancelBtn.setText("取消")', dialog)
        self.assertIn('deleteBtn.setText("删除")', dialog)
        # 取消 and 删除 must not wear the same muted tint: 删除 gets the
        # red-to-rose pill, and both stops now come from the palette rather
        # than from a literal, so the destructive tint has exactly one
        # definition.  The rose stop used to be a Color.parseColor("#dc6fbf")
        # in this very method - the last hard-coded colour in the chrome.
        self.assertIn("Theme.danger(DownloadsActivity.this)", dialog)
        self.assertIn("Theme.DANGER_ALT", dialog)
        self.assertIn("Theme.ON_DANGER", dialog)
        self.assertNotIn("Color.parseColor", dialog)
        self.assertNotIn("Color.WHITE", dialog)
        # 取消 follows the theme too: a pinned SURFACE_2 was a black pill on a
        # white page in light mode.
        self.assertIn("cancelBg.setColor(Theme.surface2(DownloadsActivity.this))", dialog)
        onDelete = dialog[dialog.index("deleteBtn.setOnClickListener"):]
        self.assertLess(
            onDelete.index("performDelete(taskId, downloadId)"),
            onDelete.index("dialog.dismiss()"),
        )

    def test_delete_clears_hls_parts_and_direct_download_manager_row(self) -> None:
        # The two paths have to be different: HLS lives in app-private
        # storage (file + .parts + .part siblings), direct lives in
        # DownloadManager. A single deleteTree() call would not revoke the
        # DownloadManager row, leaving the user with a phantom download
        # notification.
        self.assertIn("deleteTree(new File(localPath + \".parts\"))", self.dl_text)
        self.assertIn("new File(localPath + \".part\").delete()", self.dl_text)
        self.assertIn("mgr.remove(id)", self.dl_text)

    def test_main_activity_entry_button_launches_the_activity(self) -> None:
        # The entry button used to call toggleDownloadPanel(); the panel
        # path is still here for the in-app flows (gate, status), but the
        # button must not reopen it.
        self.assertIn("launchDownloadsActivity();", self.main_text)
        self.assertIn("new Intent(this, DownloadsActivity.class)", self.main_text)
        # And the button handler no longer references the panel toggle.
        # The two methods must not appear inside the same onClick block.
        handler = re.search(
            r"button\.setOnClickListener\(.*?\}\s*\}\s*\)",
            self.main_text,
            re.DOTALL,
        )
        self.assertIsNotNone(handler)
        self.assertNotIn("toggleDownloadPanel()", handler.group(0))


class MobileThemeCoherenceTests(unittest.TestCase):
    """One palette, three languages, and a seam that fails when they drift.

    The app paints itself from three places: the bundled pages (CSS custom
    properties in ``mobile_skin``), the native chrome (``Theme.java``), and the
    Android resources (``colors.xml`` / ``themes.xml``).  Each is the right home
    for its own half - but "the accent" and "the page background" are properties
    of the *app*, not of either half, so the halves have to agree.

    They had already stopped agreeing: three accent blues (#5c7ee6 for the
    shell's ring, #7aa2ff for the download centre and the refusal page, #8fb3ff
    in the launcher icon) and three near-blacks (#07090d splash, #0a0d14 window,
    #0a0b0f page).  Each difference is small enough to read as a mistake rather
    than a choice, and a screen full of near-misses is most of what makes a UI
    look unfinished.  These tests are the seam.
    """

    ROOT = Path(__file__).resolve().parents[1]
    JAVA = ROOT / "android/app/src/main/java/com/asmrlib/archiver"
    RES = ROOT / "android/app/src/main/res"

    #: The values that used to be the palette, per language.  Any of them
    #: reappearing means a call site was written from memory instead of read
    #: from Theme.
    STALE_NATIVE = (
        "5c7ee6",
        "8066cb",
        "7aa2ff",
        "a78bfa",
        "0a0d14",
        "07090d",
        "070910",
        "eef2f9",
        "e8ecf6",
        "8b95ad",
        "8fa0b8",
        "93a3b8",
        "111726",
        "0d1322",
        "080b11",
    )

    @staticmethod
    def _strip_comments(css: str) -> str:
        """Drop ``/* ... */`` before parsing.

        The token block explains itself in comments that name the very hex
        values the tokens hold, so a naive ``--name: value`` scan reads the
        prose as declarations.
        """
        return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)

    @staticmethod
    def _braced(text: str, start: int) -> str:
        """The body of the block whose opening brace is at or after ``start``."""
        open_at = text.index("{", start)
        depth = 0
        for i in range(open_at, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[open_at + 1 : i]
        raise AssertionError("unbalanced braces")

    @classmethod
    def _skin_tokens(cls) -> tuple[dict[str, str], dict[str, str]]:
        """``(dark, light)`` custom properties, keyed by token name."""
        from asmrlib_archiver.mobile_skin import SKIN

        marker = "@media (prefers-color-scheme: light)"
        if SKIN.count(marker) != 1:
            raise AssertionError(
                f"expected exactly one light block, found {SKIN.count(marker)}"
            )
        dark_src, light_src = SKIN.split(marker, 1)

        def tokens(block: str) -> dict[str, str]:
            stripped = cls._strip_comments(block)
            body = cls._braced(stripped, stripped.index(":root"))
            return {
                name: value.strip()
                for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body)
            }

        return tokens(dark_src), tokens(light_src)

    @classmethod
    def _theme_java(cls) -> dict[str, str]:
        """Every ``Theme`` constant, as lowercase CSS-ready text.

        Ints come back as ``#rrggbb`` (alpha dropped, since every one of them is
        opaque) and Strings as written, so an int and the CSS literal it backs
        can be compared to each other and to the web token.
        """
        text = (cls.JAVA / "Theme.java").read_text(encoding="utf-8")
        found = {
            name: "#" + raw[2:].lower()
            for name, raw in re.findall(
                r"public static final int ([A-Z0-9_]+)\s*=\s*0x([0-9A-Fa-f]{8});", text
            )
        }
        found.update(
            re.findall(
                r'public static final String ([A-Z0-9_]+)\s*=\s*"([^"]+)";', text
            )
        )
        return found

    @classmethod
    def _res_colour(cls, folder: str, name: str) -> str:
        text = (cls.RES / folder / "colors.xml").read_text(encoding="utf-8")
        match = re.search(r'<color name="' + name + r'">\s*([^<\s]+)\s*</color>', text)
        if match is None:
            raise AssertionError(f"{folder}/colors.xml has no <color name='{name}'>")
        return match.group(1)

    # ------------------------------------------------------------- the seam

    def test_the_web_and_native_accents_are_the_same_colour(self) -> None:
        dark, light = self._skin_tokens()
        java = self._theme_java()

        # The int and its own CSS literal have to agree first - otherwise the
        # page the shield serves and the ring the shell draws are two colours
        # from one file.
        for token in ("BG", "ACCENT", "ACCENT_ALT", "TEXT", "TEXT_2", "DANGER"):
            self.assertIn(token + "_CSS", java, f"{token} has no CSS literal")
            self.assertEqual(
                java[token], java[token + "_CSS"],
                f"Theme.{token} and Theme.{token}_CSS disagree",
            )

        # Then the web tokens against them.
        pairs = (
            ("--bg", "BG"),
            ("--accent", "ACCENT"),
            ("--accent-strong", "ACCENT_ALT"),
            ("--text", "TEXT"),
            ("--text-2", "TEXT_2"),
            ("--danger", "DANGER"),
        )
        for web_name, java_name in pairs:
            self.assertEqual(
                dark[web_name], java[java_name],
                f"dark {web_name} is {dark[web_name]}, Theme.{java_name} is {java[java_name]}",
            )

        # The product pins dark in both system modes: the skin's light block
        # must repeat the dark values verbatim (WebViews that resolve the
        # media query still land on the same material), and its former light
        # palette must not survive anywhere in Theme.
        for web_name, _java_name in pairs:
            self.assertEqual(
                light[web_name], dark[web_name],
                f"light {web_name} is {light[web_name]}, dark is {dark[web_name]}",
            )
        for retired in ("BG_LIGHT", "ACCENT_LIGHT", "ACCENT_ALT_LIGHT", "TEXT_LIGHT",
                        "TEXT_2_LIGHT", "SCRIM_LIGHT", "SEP_LIGHT", "FILL_LIGHT"):
            self.assertNotIn(retired, java,
                             f"the retired light constant {retired} is still in Theme")

        # And the web token against its own CSS literal in Theme, so the refusal
        # page the shield builds cannot be a shade off the page it interrupts.
        self.assertEqual(dark["--bg"], java["BG_CSS"])

    def test_the_window_matches_the_page_background_in_both_themes(self) -> None:
        """The window, the bars and the page are one colour per theme.

        They were three different near-blacks, which is visible as a step on
        every cold start: the window paints, then the page paints over it.  The
        dark value lives in ``values-night`` so Android picks it from the same
        system signal the pages' media query resolves against.
        """
        dark, light = self._skin_tokens()
        self.assertEqual(self._res_colour("values-night", "app_bg"), dark["--bg"])
        self.assertEqual(self._res_colour("values", "app_bg"), light["--bg"])
        # The launcher plate is deliberately *not* theme-aware: an icon that
        # changes colour with the system theme is a different brand per phone.
        self.assertEqual(self._res_colour("values", "ic_launcher_background"), dark["--bg"])

    def test_the_system_bars_are_readable_in_both_themes(self) -> None:
        """Bars painted app_bg need icons that contrast with app_bg.

        The product pins dark in both system modes, so both resource folders
        must paint the bars with the same app_bg and both must ask for light
        (white) bar icons: on a dark bar the clock and the gesture pill are
        dark-on-dark and vanish if the flag is missing, and a folder that
        drifts from the other one resurrects the two-colour cold start.
        """
        light = (self.RES / "values" / "themes.xml").read_text(encoding="utf-8")
        night = (self.RES / "values-night" / "themes.xml").read_text(encoding="utf-8")
        for name, text in (("values", light), ("values-night", night)):
            for attr in ("windowBackground", "statusBarColor", "navigationBarColor"):
                self.assertIn(
                    f'name="android:{attr}">@color/app_bg<', text,
                    f"{name}/themes.xml does not paint android:{attr} with app_bg",
                )
            self.assertIn('name="android:windowLightStatusBar">false<', text)
            self.assertIn('name="android:windowLightNavigationBar">false<', text)
        # values-night is kept as a statement of intent, not a second palette:
        # its <style> block must repeat the default folder's exactly (the
        # explanatory comments may differ; the compiled values may not).
        style = re.compile(r"<style\b.*?</style>", re.S)
        self.assertEqual(style.search(light).group(0),
                         style.search(night).group(0),
                         "values-night/themes.xml drifted from values/themes.xml")

    def test_the_theme_color_meta_follows_the_page_background(self) -> None:
        """``theme-color`` is the status bar's tint while the WebView is up.

        The product pins dark, so one unconditional tag equal to --bg is the
        whole contract. A media-tagged second tag is the retired light theme's
        shape: it would tint the bar on the system's signal while the page
        underneath ignores that signal, and the two would disagree.
        """
        from asmrlib_archiver.mobile_skin import THEME_COLOR_META

        dark, light = self._skin_tokens()
        self.assertEqual(
            THEME_COLOR_META,
            f'<meta name="theme-color" content="{dark["--bg"]}">\n',
        )
        self.assertNotIn("media=", THEME_COLOR_META)

    def test_no_resource_comment_contains_a_double_hyphen(self) -> None:
        """XML forbids ``--`` inside a comment, and aapt says so only at build time.

        That is a slow way to learn it, and it happened twice in one sitting:
        a comment explaining that ``app_bg`` mirrors the web's ``--bg`` token
        broke ``mergeDebugResources``.  A unit test turns a failed APK build
        into a named failure with a line number.
        """
        for path in sorted((self.RES).rglob("*.xml")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                body = line.replace("<!--", "").replace("-->", "")
                if "--" in body:
                    self.fail(
                        f"{path.relative_to(self.ROOT)}:{number} has '--' inside a "
                        f"comment, which aapt rejects: {line.strip()}"
                    )

    def test_the_old_palette_is_gone_from_the_native_chrome(self) -> None:
        for name in ("MainActivity.java", "DownloadsActivity.java",
                     "PlayerActivity.java", "ShieldRules.java"):
            text = (self.JAVA / name).read_text(encoding="utf-8").lower()
            for value in self.STALE_NATIVE:
                self.assertNotIn(
                    value, text,
                    f"{name} still spells out #{value}; it belongs in Theme",
                )

    def test_the_native_alpha_tints_are_derived_not_repeated(self) -> None:
        """The chrome is full of accent/surface colours at partial alpha.

        They used to be written as three numbers per call site - ``Color.argb(
        120, 122, 162, 255)`` and friends - which means changing the accent
        leaves a dozen stale triples behind, each one a near-miss.  The helpers
        derive from the constant, so they cannot drift; assert the derivation is
        the real one and not a second literal wearing its name.
        """
        text = (self.JAVA / "Theme.java").read_text(encoding="utf-8")
        self.assertIn("private static int withAlpha(int colour, int alpha)", text)
        for helper, base in (
            ("bg", "BG"),
            ("surface", "SURFACE"),
            ("text", "TEXT"),
            ("text2", "TEXT_2"),
            ("accent", "ACCENT"),
            ("onAccent", "ON_ACCENT"),
            ("fill", "FILL"),
        ):
            body = re.search(
                r"public static int " + helper + r"\(Context context\)\s*\{(.*?)\}",
                text,
                re.DOTALL,
            )
            self.assertIsNotNone(body, f"Theme has no {helper}(Context)")
            self.assertIn(
                base, body.group(1), f"{helper}(Context) does not pick a {base} half"
            )
            # Every one of them has to answer with the pinned dark constant.
            # A helper that still consults isNight() is a retired light half
            # speaking through a dead branch, and a helper that derives its
            # own copy of the colour is the stale-triple bug all over again.
            self.assertNotIn(
                "isNight(", body.group(1),
                f"{helper}(Context) still branches per theme; the light half "
                f"is retired",
            )
            alpha = re.search(
                r"public static int " + helper + r"\(Context context, int alpha\)"
                r"\s*\{(.*?)\}",
                text,
                re.DOTALL,
            )
            self.assertIsNotNone(alpha, f"Theme has no {helper}(Context, int)")
            self.assertIn(
                "withAlpha(", alpha.group(1),
                f"{helper}(Context, int) does not derive through withAlpha",
            )

    def test_the_pinned_dark_accessors_stay_inside_the_player(self) -> None:
        """A dark-only accessor is a light-mode bug with a friendly name.

        ``bgAlpha(210)``, ``surfaceAlpha(190)`` and friends answered from the
        dark constants no matter what the system theme was, and nine call sites
        in the shell - the download menu card, its scrim, the progress ring's
        track - took them at their word.  Light mode drew a black card over a
        white page.

        The player's chrome legitimately pins dark (a video surface is the one
        place where a bright frame should win), so the family still exists -
        but it is named ``dark*``, takes no Context, and may only be reached
        from ``PlayerActivity``.  Anything else reaching for it is the bug.
        """
        text = (self.JAVA / "Theme.java").read_text(encoding="utf-8")
        for helper in ("darkBg", "darkSurface", "darkText", "darkAccent"):
            self.assertIn(f"public static int {helper}(int alpha)", text)
            # No Context: the signature itself says "this does not follow the
            # system", so a caller cannot reach for it by accident and believe
            # it is theme-aware.
            self.assertNotIn(f"public static int {helper}(Context", text)

        for path in sorted(self.JAVA.glob("*.java")):
            if path.name in ("Theme.java", "PlayerActivity.java"):
                continue
            body = path.read_text(encoding="utf-8")
            for helper in ("darkBg", "darkSurface", "darkText", "darkAccent"):
                self.assertNotIn(
                    f"Theme.{helper}(", body,
                    f"{path.name} reaches for Theme.{helper}(), which ignores the "
                    f"system theme; use the Context-taking accessor instead",
                )

    def test_no_call_site_spells_out_a_translucent_white(self) -> None:
        """``Color.argb(n, 255, 255, 255)`` is a light-mode no-op.

        A white wash at 6% over a dark page is a visible lift; the same wash
        over a light page is nothing at all, so every quiet button, menu row
        and card outline drawn that way simply vanished in light mode.  The
        theme-following ``Theme.fill`` / ``Theme.sep`` exist for exactly this.
        """
        for name in ("MainActivity.java", "DownloadsActivity.java"):
            body = (self.JAVA / name).read_text(encoding="utf-8")
            stray = re.findall(r"Color\.argb\(\s*\d+\s*,\s*255\s*,\s*255\s*,\s*255\s*\)", body)
            self.assertEqual(
                stray, [],
                f"{name} still writes {stray} - use Theme.fill()/Theme.sep(), "
                f"which pick a colour per theme",
            )

    def test_the_fab_scrim_is_built_from_the_scrim_not_the_page(self) -> None:
        """An overlay whose whole job is to darken cannot be the page colour.

        ``Theme.bg(ctx, 70)`` is right on a dark page - more near-black *is*
        less light - and a no-op on a light one, where it lays a 27% wash of
        near-white over near-white.  ``Theme.scrim`` picks a darkening agent per
        theme; this is the wiring half, and the compiled-class half lives in
        ``test_a_scrim_darkens_the_page_it_covers``.
        """
        body = (self.JAVA / "MainActivity.java").read_text(encoding="utf-8")
        match = re.search(r"fabScrim\.setBackgroundColor\((.*)\);", body)
        self.assertIsNotNone(match, "the FAB scrim no longer sets its own colour")
        colour = match.group(1)
        self.assertIn(
            "Theme.scrim(", colour,
            f"the FAB scrim is painted {colour.strip()} - a scrim has to come "
            f"from Theme.scrim(), which knows which theme it is in",
        )

    def test_every_token_a_rule_uses_is_actually_defined(self) -> None:
        """A missing custom property fails silently - the declaration is dropped.

        Not hypothetical: ``--accent-strong`` reached the light block and the
        brand mark's ``linear-gradient(var(--accent), var(--accent-strong))``
        before it existed in the dark block, which left the dark theme's logo
        with no background at all.  Nothing errored, and no test noticed.

        So the check is two-sided: every referenced token must be defined
        somewhere, *and* every token the light block overrides must have a
        dark-block counterpart.  The second half is the one that catches the
        real bug - ``--accent-strong`` was defined only inside the light
        override, so a file-wide "is it defined?" search would have passed it.
        Component-local tokens (``.plate-N`` sets ``--tint``) are defined in the
        stylesheet body, not in ``:root``, so the search is file-wide.
        """
        from asmrlib_archiver.mobile_skin import SKIN

        dark, light = self._skin_tokens()
        stripped = self._strip_comments(SKIN)
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", stripped))
        # Both forms count as a reference.  ``var(--i, 0)`` still has to resolve
        # to a real token: a fallback is what an element with no rank gets, not
        # a licence to reference a name the stylesheet never declares.
        used = set(re.findall(r"var\((--[a-z0-9-]+)", stripped))
        self.assertTrue(used, "no var() references found - the parse is wrong")
        self.assertEqual(
            sorted(used - defined), [],
            "these tokens are referenced but never defined anywhere",
        )
        # Every light override must name a token that exists in the dark set;
        # a typo there would define a phantom token and silently do nothing.
        self.assertEqual(
            sorted(set(light) - set(dark)), [],
            "the light block overrides tokens the dark block never defines",
        )
        # And the light block must cover the colours, or light mode inherits
        # dark ones and the page goes half-and-half.
        for token in ("--bg", "--surface", "--text", "--text-2", "--accent",
                      "--accent-strong", "--danger", "--sep"):
            self.assertIn(token, light, f"light mode does not redefine {token}")


class MobileWebViewBaselineTests(unittest.TestCase):
    """The pages ship to a WebView that is older than the web.

    The shell renders the bundled pages in the *system* WebView, which on the
    Android 14 test image reports ``Chrome/113.0.5672.136``.  That is not the
    browser anyone develops against, and the failure mode is the nastiest one
    there is: an unrecognised declaration is dropped silently, so the CSS looks
    right, the tests that read the CSS pass, and the only symptom is on the
    phone.  ``text-wrap: pretty`` sat in ``.err-sub`` doing nothing at all for
    exactly that reason - it lands in Chrome 117 - and the two-character orphan
    it was written to prevent kept happening.

    So: anything newer than the shipped baseline has to sit behind an
    ``@supports`` guard, which costs nothing today and starts working the day
    the system WebView updates itself.
    """

    #: Properties and at-rules measured as *absent* from the shipped WebView.
    #: Each entry is the substring to look for.  Kept to what was actually
    #: probed in the emulator - guessing at a version table is how a baseline
    #: test turns into a liability.
    POST_BASELINE = (
        "text-wrap: pretty",
        "text-wrap: balance",
        "text-box-trim",
        "text-box-edge",
        "interpolate-size",
        "@starting-style",
        "field-sizing",
        "light-dark(",
        "@scope",
        "anchor-name",
        "position-anchor",
        ":user-valid",
        ":user-invalid",
    )

    #: Post-baseline features that are allowed to stand unguarded because a
    #: working fallback sits next to them, mapped to the fallback that has to
    #: still be there.  ``scrollbar-width: none`` is the case: Chrome 113 has
    #: no idea what it is, but ``::-webkit-scrollbar { display: none }`` next
    #: to it has been doing the job since long before that.
    POST_BASELINE_WITH_FALLBACK = {
        "scrollbar-width": "::-webkit-scrollbar",
    }

    @staticmethod
    def _guarded_css() -> list[tuple[str, str]]:
        from asmrlib_archiver.mobile_discover import _EXTRA_CSS, _PLAY_EXTRA_CSS

        return [("discover", _EXTRA_CSS), ("play", _PLAY_EXTRA_CSS)]

    @classmethod
    def _unguarded(cls, css: str, feature: str) -> list[str]:
        """Declarations mentioning ``feature`` that no ``@supports`` covers.

        Walks the braces rather than the lines: a guard is a *block*, so the
        question "is this declaration inside one?" cannot be answered by
        looking at the line above it.  Comments are stripped first because the
        skin explains these features in prose that names them.
        """
        text = MobileThemeCoherenceTests._strip_comments(css)
        stack: list[bool] = []
        pending = ""
        found: list[str] = []
        for char in text:
            if char == "{":
                head = pending.strip()
                stack.append(head.startswith("@supports") and feature in head)
                pending = ""
            elif char == "}":
                if stack:
                    stack.pop()
                pending = ""
            elif char == ";":
                if feature in pending and not any(stack):
                    found.append(pending.strip())
                pending = ""
            else:
                pending += char
        if feature in pending and not any(stack):
            found.append(pending.strip())
        return found

    def test_no_page_css_relies_on_a_post_113_feature_unguarded(self) -> None:
        for name, css in self._guarded_css():
            for feature in self.POST_BASELINE:
                unguarded = self._unguarded(css, feature)
                self.assertEqual(
                    unguarded, [],
                    f"{name}.html uses {feature!r} outside an @supports guard, "
                    f"and the shipped WebView (Chrome 113) drops it silently: "
                    f"{unguarded}",
                )
            for feature, fallback in self.POST_BASELINE_WITH_FALLBACK.items():
                for decl in self._unguarded(css, feature):
                    self.assertIn(
                        fallback, css,
                        f"{name}.html uses {feature!r} ({decl}) with no fallback "
                        f"for Chrome 113 - expected {fallback!r} beside it",
                    )

    def test_the_guard_the_pages_do_carry_is_a_real_one(self) -> None:
        """The scan above is only worth anything if it can see a guard.

        ``text-wrap: pretty`` is the live case: it is written once, behind
        ``@supports``, and the block has to actually contain the declaration
        for the negative test above to mean anything.  Without this, deleting
        the guarded block outright would leave the suite green.
        """
        from asmrlib_archiver.mobile_discover import _EXTRA_CSS

        guarded = re.search(
            r"@supports\s*\(text-wrap:\s*pretty\)\s*\{(.*?)\}", _EXTRA_CSS, re.DOTALL
        )
        self.assertIsNotNone(
            guarded, "the @supports (text-wrap: pretty) guard is gone from the skin"
        )
        self.assertIn("text-wrap: pretty", guarded.group(1))
        # And the unguarded scan would notice it if the guard were removed:
        # re-run it against the same text with the guard's head deleted.
        broken = _EXTRA_CSS.replace("@supports (text-wrap: pretty)", "@media screen and")
        self.assertTrue(
            self._unguarded(broken, "text-wrap: pretty"),
            "the scan cannot see an unguarded text-wrap: pretty - it is not "
            "actually testing anything",
        )


class MobileHitTargetTests(unittest.TestCase):
    """Two audit findings that only a ruler on the device could see.

    Both are the kind of thing that looks fine in a screenshot and is wrong in
    the hand: a filter pill that is a third smaller than a finger, and a bar
    that is translucent without a blur, so the cards scrolling under it ghost
    their titles straight through.
    """

    @classmethod
    def _block(cls, css: str, selector: str) -> str:
        """The declaration body of ``selector``, verbatim."""
        at = css.index(selector + " {")
        return MobileThemeCoherenceTests._braced(css, at)

    def test_the_filter_pill_is_tappable_even_though_it_is_small(self) -> None:
        """A 34px pill needs a 44px hit box, and the rail has to leave room.

        The visual size is deliberate - a rail of 44px lozenges reads chunky -
        so the target is grown with an invisible ``::after`` instead.  That only
        works if the scroller's own padding-block is at least as deep as the
        expander, or ``overflow-y: hidden`` clips it back down to 34px.
        """
        from asmrlib_archiver.mobile_skin import SKIN

        chip = self._block(SKIN, ".chip")
        self.assertIn("height: 34px", chip)
        self.assertIn("position: relative", chip)

        after = self._block(SKIN, ".chip::after")
        self.assertIn("position: absolute", after)
        self.assertIn("content: ''", after)
        inset = re.search(r"inset:\s*(-?\d+)px\s+0", after)
        self.assertIsNotNone(inset, f".chip::after has no vertical inset: {after!r}")
        grow = int(inset.group(1)) * -1
        self.assertGreaterEqual(
            34 + 2 * grow, 44,
            f"the pill is 34px and the expander adds {grow}px a side, which is "
            f"{34 + 2 * grow}px - under the 44px minimum",
        )

        rail = self._block(SKIN, ".chips")
        pad = re.search(
            r"padding:\s*([^\s;]+)\s+var\(--inset\)\s+([^\s;]+)", rail
        )
        self.assertIsNotNone(pad, f".chips padding is not the expected shape: {rail!r}")
        tokens = MobileThemeCoherenceTests._skin_tokens()[0]

        def pixels(value: str) -> int:
            name = re.fullmatch(r"var\((--[a-z0-9-]+)\)", value.strip())
            if name is None:
                return int(value.strip().removesuffix("px"))
            return int(tokens[name.group(1)].removesuffix("px"))

        self.assertGreaterEqual(
            min(pixels(pad.group(1)), pixels(pad.group(2))), grow,
            "the rail's padding-block is shallower than the chip's hit "
            "expander, so overflow-y: hidden clips the extra pixels away",
        )

    def test_the_nav_bar_cannot_ghost_what_scrolls_under_it(self) -> None:
        """Translucent is only allowed *under* a blur that actually answers.

        A translucent, blur-free bar lets the card titles under it show through
        at 10%, which reads as a rendering fault rather than as depth - the
        lesson that once banned ``backdrop-filter`` outright. The Liquid Glass
        pass reintroduced exactly one blur surface (the nav), on the contract
        below: an opaque base is always painted; the translucent glass and the
        blur ride only inside an ``@supports`` guard whose rule repeats the
        opaque base as its bottom layer; and
        ``prefers-reduced-transparency`` drops the whole thing.
        """
        dark, light = MobileThemeCoherenceTests._skin_tokens()
        for theme, tokens in (("dark", dark), ("light", light)):
            value = tokens["--nav-bg"]
            self.assertRegex(
                value, r"^#[0-9a-f]{6}$",
                f"the {theme} nav base is {value}; an opaque base is what "
                f"stands between the content and the bar when blur is "
                f"unavailable or dropped",
            )
        from asmrlib_archiver.mobile_skin import SKIN

        clean = MobileThemeCoherenceTests._strip_comments(SKIN)
        # The blur exists in exactly one guarded rule, and that rule still
        # paints the opaque base beneath the translucent glass.
        self.assertEqual(
            clean.count("@supports ((backdrop-filter"), 1,
            "the nav's backdrop-filter must live in one @supports guard",
        )
        self.assertIn(
            "background: var(--glass-sheen), var(--glass-nav), var(--nav-bg);",
            clean,
            "the guarded glass nav dropped its opaque bottom layer",
        )
        self.assertIn(
            "background: var(--glass-sheen), var(--nav-bg);",
            clean,
            "the unguarded .nav must paint the opaque base",
        )
        # Nothing else in the skin may blur: the blanket ban was earned on a
        # dozen live-blur layers, and one surface is the whole budget.
        self.assertEqual(
            clean.count("backdrop-filter: blur(22px)"), 2,
            "blur is only for the nav's -webkit- and standard pair",
        )


class MobileErrorCopyTests(unittest.TestCase):
    """Error hints are sized to one line per ``<br>``, because Chrome 113 can't
    balance CJK wrapping.

    The failure the user actually saw: on the refusal panel the hint broke
    between 「访」 and 「问」 and left 「问。」 centred on a line of its own.  The
    fix that was written for it - ``text-wrap: pretty`` - is a Chrome 117
    feature, so the shipped WebView dropped the declaration and the orphan
    stayed.  Until the system WebView updates, the only thing that reliably
    prevents an orphan is copy that fits.

    Measured in the emulator rather than guessed: ``.err-sub`` is 304px wide on
    a 360dp phone and one CJK glyph is 13.5px at ``--fs-subhead``, so a line
    holds ``floor(304 / 13.5) = 22`` glyphs.  The budget below keeps one glyph
    of slack.
    """

    #: Glyphs that fit on one line, minus one for rounding.
    LINE_BUDGET = 21

    @staticmethod
    def _width(text: str) -> float:
        """The text's width in CJK glyphs.

        Full-width characters and punctuation are 1, Latin letters and digits
        about half, a space about a quarter.  That is the metric that decides
        whether a line fits, so it is the metric the budget is written in.
        """
        total = 0.0
        for char in text:
            if char == " ":
                total += 0.25
            elif ord(char) < 0x2E80:
                total += 0.5
            else:
                total += 1.0
        return total

    @classmethod
    def _hints(cls) -> list[tuple[str, str]]:
        """``(page, segment)`` for every ``<br>``-separated error hint."""
        from asmrlib_archiver.mobile_discover import (
            build_discover_html,
            build_play_html,
        )

        out: list[tuple[str, str]] = []
        for page, html in (
            ("discover", build_discover_html()),
            ("play", build_play_html()),
        ):
            # The panel is built by JS, so the markup only exists as a string
            # literal in the page's <script>.  Rejoin the `' + '` concatenation
            # the generator uses before measuring, or the quotes and the plus
            # signs get counted as glyphs.
            for raw in re.findall(
                r'<p class="err-sub">(.*?)</p>', html, re.DOTALL
            ):
                body = re.sub(r"'\s*\+\s*'", "", raw)
                # The hand-written hint is the paragraph with the <br> in it.
                # The other .err-sub is the raw exception text, which is
                # whatever the bridge or the network produced and cannot be
                # budgeted.
                if "<br>" not in body:
                    continue
                for segment in body.split("<br>"):
                    text = re.sub(r"<[^>]+>", "", segment).strip()
                    if text:
                        out.append((page, text))
        self_checked = [s for _, s in out]
        if len(self_checked) < 4:
            raise AssertionError(
                f"expected a hint per <br> on both pages, found {self_checked}"
            )
        return out

    def test_every_error_hint_fits_on_one_line(self) -> None:
        for page, segment in self._hints():
            width = self._width(segment)
            self.assertLessEqual(
                width, self.LINE_BUDGET,
                f"the {page} page's error hint is {width:.2f} glyphs wide and "
                f"the line holds {self.LINE_BUDGET} - it will wrap, and Chrome "
                f"113 will leave the tail centred on its own line: {segment!r}",
            )

    def test_the_budget_would_catch_the_orphan_that_started_this(self) -> None:
        """The exact string that produced 「问。」 must fail the check above.

        Without this the test is just "some numbers are small" - a budget that
        never rejects anything is not a budget.
        """
        orphan = "该站点在国内网络通常需要代理（梯子）才能访问。"
        self.assertGreater(self._width(orphan), self.LINE_BUDGET)
        # And it is not a near-miss: it overflowed by more than a glyph.
        self.assertGreaterEqual(self._width(orphan) - self.LINE_BUDGET, 1.0)


class MobileRetryVisibilityTests(unittest.TestCase):
    """A retry budget must not be used to hide the first-load error.

    The bug: ``showError`` asked ``scheduleAutoRetry`` first and returned early
    if it said yes, so "the budget has not run out yet" was treated as "do not
    show the error".  On a phone with no network at all that meant four 20s
    bridge timeouts - about 86 seconds of spinner - before the page would admit
    anything.  The budget is there to avoid flashing an error *over content the
    user already has*; with an empty screen there is nothing to protect.

    This is a behavioural test, not a source scan: the three functions are
    pulled out of the generated page and actually executed against a stub DOM,
    because the interesting part is the order of side effects and a string
    assertion cannot see order.  ``errEl.innerHTML`` writes are counted, which
    is how the second half of the fix - a background retry must not make the
    panel strobe - is pinned.
    """

    ROOT = Path(__file__).resolve().parents[1]

    #: The stub DOM.  Only what showError/scheduleAutoRetry actually touch.
    HARNESS = """
function tracked() {
  var o = { _html: '', hidden: false, className: '', textContent: '',
            disabled: false, writes: 0,
            classList: { _s: {}, contains: function (c) { return !!this._s[c]; },
                         add: function (c) { this._s[c] = 1; } } };
  Object.defineProperty(o, 'innerHTML', {
    get: function () { return o._html; },
    set: function (v) { o._html = v; o.writes += 1; },
  });
  return o;
}
var statusEl = tracked(), errEl = tracked(), grid = tracked();
var tagsEl = tracked(), secEl = tracked(), pagerEl = tracked(),
    srcEl = tracked(), countEl = tracked(), splashEl = tracked();
var prevBtn = tracked(), nextBtn = tracked();
grid.cards = 0;
grid.querySelector = function () { return grid.cards > 0 ? { tagName: 'A' } : null; };
var state = { url: 'https://asmrlib.com/', chain: null, refreshing: false, pages: [] };
var pending = {}, seq = 0;
var autoRetryCount = 0, autoRetryTimer = 0, retryUrl = '';
var AUTO_RETRY_DELAYS = [900, 1800, 3600];
var scheduled = [], skeletonCalls = 0, scrollCalls = 0, bridgeCalls = [];
function setTimeout(fn, delay) { scheduled.push({ fn: fn, delay: delay }); return scheduled.length; }
function clearTimeout() {}
function skeleton() { skeletonCalls += 1; }
function endRefresh() {}
function esc(s) { return String(s == null ? '' : s); }
var SITE = 'https://asmrlib.com';
// request() names the source it is switching to; the real isOneUrl() is a
// one-liner over this same prefix.
var ASMRONE_SEARCH_PREFIX = 'asmrone://search/';
function isOneUrl(url) { return String(url || '').indexOf(ASMRONE_SEARCH_PREFIX) === 0; }
// The real request() is pulled in below and driven for real - an earlier
// version of this harness stubbed request() out and then asserted that the
// stub had left errEl alone, which could never fail no matter what the page
// did.  These are its actual dependencies.
var window = {
  __asmrlibDiscover: {
    feed: function (u, id) { bridgeCalls.push(['feed', u, id]); },
    refresh: function (p, id) { bridgeCalls.push(['refresh', p, id]); },
  },
  scrollTo: function () { scrollCalls += 1; },
};
function fireTimers(maxDelay) {
  scheduled.slice().forEach(function (t) { if (t.delay <= maxDelay) t.fn(); });
}

function reset(opts) {
  statusEl.className = ''; statusEl.textContent = '';
  // The real markup ships the panel with the `hidden` attribute, so "panelUp"
  // has to map to hidden=false.  Getting this backwards made the first version
  // of this harness assert the opposite of what it claimed: showError saw a
  // visible panel on a cold start and took the "already up, do not redraw"
  // early return, so the three tests below went red against correct code.
  errEl.hidden = !opts.panelUp; errEl._html = ''; errEl.writes = 0;
  grid.cards = opts.cards; grid._html = ''; grid.writes = 0;
  autoRetryCount = 0; autoRetryTimer = 0; retryUrl = '';
  scheduled.length = 0; skeletonCalls = 0; scrollCalls = 0; bridgeCalls.length = 0;
  pending = {}; seq = 0;
  state.chain = null; state.refreshing = false;
}

function failure(opts) {
  reset(opts);
  showError('boom', 'https://asmrlib.com/', !!opts.keepGrid, !!opts.retryable, false);
  return {
    panelShown: errEl.hidden === false,
    panelWrites: errEl.writes,
    status: statusEl.textContent,
    scheduled: scheduled.length,
    retriesInFlight: pendingCount(),
  };
}

function pendingCount() { var n = 0, k; for (k in pending) n += 1; return n; }

function directSchedule(opts) {
  reset(opts);
  var before = { status: statusEl.textContent, gridWrites: grid.writes,
                 panelWrites: errEl.writes, skeletonCalls: skeletonCalls };
  var scheduledNow = scheduleAutoRetry('boom', 'https://asmrlib.com/', false, false);
  return {
    scheduledNow: scheduledNow,
    drewStatus: statusEl.textContent !== before.status,
    drewGrid: grid.writes !== before.gridWrites,
    drewPanel: errEl.writes !== before.panelWrites,
    drewSkeleton: skeletonCalls !== before.skeletonCalls,
  };
}

function retryIsSilent(opts) {
  // The real request(), on the way into a background retry, with the panel up.
  reset(opts);
  errEl.hidden = false;
  request('https://asmrlib.com/', false, false, true, false);
  var onTheWayIn = {
    panelStayedUp: errEl.hidden === false,
    drewStatus: statusEl.textContent !== '',
    drewSkeleton: skeletonCalls > 0,
    bridgeCalls: bridgeCalls.length,
  };
  // ...and once the 250ms hold fires, which is where a user-initiated load
  // draws its skeleton.  A retry must not.
  fireTimers(250);
  var afterHold = {
    panelStayedUp: errEl.hidden === false,
    drewStatus: statusEl.textContent !== '',
    drewSkeleton: skeletonCalls > 0,
  };
  // Contrast, so that "draws nothing" cannot be satisfied by a page that has
  // simply stopped drawing: the same hold for a user-initiated load does draw.
  reset(opts);
  errEl.hidden = false;
  request('https://asmrlib.com/', false, false, false, false);
  var userPanelHidden = errEl.hidden === true;
  fireTimers(250);
  return {
    panelStayedUp: onTheWayIn.panelStayedUp && afterHold.panelStayedUp,
    drewNothing: !onTheWayIn.drewStatus && !onTheWayIn.drewSkeleton
      && !afterHold.drewStatus && !afterHold.drewSkeleton,
    bridgeCalls: onTheWayIn.bridgeCalls,
    userPanelHidden: userPanelHidden,
    userDrewSkeleton: skeletonCalls > 0,
  };
}

console.log(JSON.stringify({
  coldStart: failure({ cards: 0, panelUp: false, keepGrid: false, retryable: true }),
  cardsOnScreen: failure({ cards: 12, panelUp: false, keepGrid: true, retryable: true }),
  retryWhilePanelUp: failure({ cards: 0, panelUp: true, keepGrid: false, retryable: true }),
  budgetExhausted: failure({ cards: 0, panelUp: false, keepGrid: false, retryable: false }),
  scheduleDraws: directSchedule({ cards: 0 }),
  silentRetry: retryIsSilent({ cards: 0 }),
}, null, 2));
"""

    @classmethod
    def _run(cls) -> dict:
        from asmrlib_archiver.mobile_discover import build_discover_html

        page = build_discover_html()
        script = re.search(r"<script>(.*?)</script>", page, re.DOTALL)
        if script is None:
            raise AssertionError("the discover page has no inline <script>")
        source = script.group(1)
        body = "\n".join(
            MobileMotionTests._function_source(source, name)
            for name in ("clearAutoRetry", "scheduleAutoRetry", "showError",
                         "setSwitchDim", "proxyRow", "nextId", "request")
        )
        probe = body + "\n" + cls.HARNESS
        with tempfile.TemporaryDirectory(prefix="retry-") as tmp:
            path = Path(tmp) / "probe.js"
            path.write_text(probe, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(path)], capture_output=True, text=True,
                encoding="utf-8", check=False
            )
        if proc.returncode != 0:
            raise AssertionError(f"the probe failed: {proc.stderr.strip()[:600]}")
        return json.loads(proc.stdout)

    def test_a_cold_start_failure_shows_the_panel_immediately(self) -> None:
        """Nothing on screen: say it now, and keep retrying behind the panel."""
        result = self._run()["coldStart"]
        self.assertTrue(result["panelShown"], "the first-load failure showed no error")
        self.assertEqual(result["panelWrites"], 1)
        self.assertEqual(result["status"], "", "the panel is the message; not a status line")
        self.assertEqual(
            result["scheduled"], 1,
            "the panel went up but no retry was scheduled - a flap would now be "
            "permanent until the user taps 重试",
        )

    def test_cards_on_screen_are_kept_and_the_panel_stays_away(self) -> None:
        """The other half, unchanged: never cover content the user already has."""
        result = self._run()["cardsOnScreen"]
        self.assertFalse(result["panelShown"], "an incremental failure covered the grid")
        self.assertEqual(result["panelWrites"], 0)
        self.assertEqual(result["status"], "网络波动，正在自动重试…")
        self.assertEqual(result["scheduled"], 1)

    def test_a_retry_failure_does_not_redraw_the_panel(self) -> None:
        """Each attempt reports a different exception text; redrawing strobed."""
        result = self._run()["retryWhilePanelUp"]
        self.assertTrue(result["panelShown"])
        self.assertEqual(
            result["panelWrites"], 0,
            "the panel was rewritten while it was already up, which restarts its "
            "entrance animation every retry",
        )
        self.assertEqual(result["scheduled"], 1)

    def test_an_exhausted_budget_still_shows_the_panel(self) -> None:
        result = self._run()["budgetExhausted"]
        self.assertTrue(result["panelShown"])
        self.assertEqual(result["scheduled"], 0)
        self.assertEqual(result["status"], "")

    def test_scheduling_a_retry_draws_nothing(self) -> None:
        """The scheduling half must stay free of side effects.

        It used to draw the skeleton and the "网络波动" line itself, which is
        what let the first-load path sit on a skeleton for the whole budget.
        """
        result = self._run()["scheduleDraws"]
        self.assertTrue(result["scheduledNow"])
        for drew in ("drewStatus", "drewGrid", "drewPanel", "drewSkeleton"):
            self.assertFalse(result[drew], f"scheduleAutoRetry {drew}")

    def test_a_background_retry_leaves_the_panel_up_on_the_way_in(self) -> None:
        """The retry must not touch a pixel - that is what made the panel strobe.

        The first version of this test stubbed ``request`` out and then asserted
        the stub had not changed ``errEl``, so it passed no matter what the page
        did.  It now drives the real ``request`` through the real bridge stub.
        """
        result = self._run()["silentRetry"]
        self.assertTrue(result["panelStayedUp"], "a silent retry cleared the panel")
        self.assertTrue(result["drewNothing"], "a silent retry repainted the screen")
        self.assertEqual(result["bridgeCalls"], 1, "the retry never reached the bridge")
        self.assertTrue(
            result["userPanelHidden"],
            "a user-initiated load no longer clears the panel, so the retry's "
            "silence is indistinguishable from a page that stopped responding",
        )
        self.assertTrue(result["userDrewSkeleton"])


class MobileMotionTests(unittest.TestCase):
    """Motion is part of the design, so it is pinned like the palette is.

    A UI with correct colours and no motion still reads as a mock-up: content
    that is simply *there* the instant you tap gives no sense that the tap did
    anything.  These tests hold the two halves of that - arrival and press -
    and, just as importantly, hold the trade Reduce Motion is supposed to make.
    """

    ROOT = Path(__file__).resolve().parents[1]

    @classmethod
    def _pages(cls) -> dict[str, str]:
        from asmrlib_archiver.mobile_discover import build_discover_html, build_play_html

        return {"discover": build_discover_html(), "play": build_play_html()}

    @staticmethod
    def _strip_root_blocks(css: str) -> str:
        """The stylesheet with every ``:root { ... }`` body removed.

        The token blocks are the one place a literal colour is allowed, so
        removing them is how "is this colour a token or a stray copy?" becomes
        answerable.
        """
        out = []
        i = 0
        while True:
            at = css.find(":root", i)
            if at < 0:
                out.append(css[i:])
                break
            out.append(css[i:at])
            brace = css.index("{", at)
            depth = 0
            for j in range(brace, len(css)):
                if css[j] == "{":
                    depth += 1
                elif css[j] == "}":
                    depth -= 1
                    if depth == 0:
                        i = j + 1
                        break
            else:  # pragma: no cover - malformed css
                raise AssertionError("unbalanced braces in the stylesheet")
        return "".join(out)

    # ------------------------------------------------------------- arrival

    def test_content_arrives_instead_of_appearing(self) -> None:
        """Cards and source rows cascade in; they do not blink into place.

        ``backwards`` is the load-bearing part of the declaration.  Without it
        a card is fully painted during its own delay and then snaps back to
        the start of the animation - which is worse than no animation at all,
        and is the kind of thing that looks like a rendering bug.
        """
        from asmrlib_archiver.mobile_skin import SKIN

        self.assertIn("@keyframes skin-rise", SKIN)
        # The arrival rule is pinned whole, including the two parts that are
        # easy to get subtly wrong: `backwards` (or the card flashes at its
        # final position before animating) and the capped delay.
        self.assertIn(
            ".cover-card {\n"
            "  animation: skin-rise var(--dur-enter) var(--ease) backwards;\n"
            "  animation-delay: calc(min(var(--i, 0), 9) * var(--stagger));\n"
            "}",
            SKIN,
        )
        rows = SKIN[SKIN.index(".play-enter {\n  animation:"):]
        rows = rows[: rows.index("}")]
        self.assertIn("animation: skin-rise", rows)
        self.assertIn("var(--i, 0)", rows)

        # The markup has to supply the rank, or every card waits zero and the
        # cascade silently does not happen.
        self.assertIn("' style=\"--i:' + (i || 0) + '\"'", self._pages()["discover"])
        # The play row wraps the anchor in a .play-row div that carries the
        # cascade rank; the anchor inside no longer holds a style of its own.
        self.assertIn("' style=\"--i:' + i + '\">'", self._pages()["play"])

    def test_the_cascade_is_capped(self) -> None:
        """Page 5 of a feed must not take a second to appear.

        The delay is ``min(rank, 9)``, so the tenth card onwards arrives with
        the tenth.  An uncapped cascade looks great on the first screen and
        terrible on the fifth.
        """
        from asmrlib_archiver.mobile_skin import SKIN

        self.assertEqual(SKIN.count("min(var(--i, 0), 9)"), 1)
        self.assertEqual(SKIN.count("min(var(--i, 0), 7)"), 1)
        for token in ("--stagger:", "--dur-enter:", "--dur-press:", "--enter-y:"):
            self.assertIn(token, SKIN, f"the motion scale has no {token}")

    # ------------------------------------------------- the Eara redesign

    def test_the_discover_card_is_a_cover_not_a_box(self) -> None:
        """The card is the cover itself, set straight on a staggered wall.

        Read from the reference implementation's source, not from a
        screenshot: AlbumGridItem lays a SQUARE cover (``aspectRatio(1f)``),
        12dp-rounded, floating on a depth shadow, with a 16/22 SemiBold
        three-line title above a left/right meta row (circle in the
        secondary voice, RJ number in the accent) and a flowing #tag line;
        the whole list is a staggered grid, so cards pack tight instead of
        aligning in rows.  The glass rectangle behind every cover read as
        one grey slab repeated, and row-aligned thumbnails read as a list,
        not a wall.
        """
        from asmrlib_archiver.mobile_skin import SKIN

        # Square cover, 12dp-rounded (--r-md), floating on a shadow pool.
        self.assertIn("--thumb-ratio: 1 / 1;", SKIN)
        self.assertIn("/* AlbumCoverDepthShadow", SKIN)
        self.assertIn("box-shadow: 0 12px 28px -10px rgba(0,0,0,.55);", SKIN)
        start = SKIN.index(".cover-card {")
        block = SKIN[start : SKIN.index("}", start)]
        # No card surface: no border, no background, no box shadow - and no
        # splitting across masonry columns.
        self.assertNotIn("border:", block)
        self.assertNotIn("background:", block)
        self.assertNotIn("box-shadow:", block)
        self.assertIn("break-inside: avoid;", block)
        self.assertIn("margin-bottom: var(--sp-4);", block)
        thumb = SKIN[SKIN.index(".cover-thumb {"):]
        thumb = thumb[: thumb.index("}")]
        self.assertIn("border-radius: var(--r-md);", thumb)
        # The wall is masonry: two columns on the phone, skeleton spanning.
        self.assertIn(".cover-grid {", SKIN)
        self.assertIn("columns: 2;", SKIN)
        self.assertIn("column-gap: var(--sp-3);", SKIN)
        self.assertIn("column-span: all;", SKIN)
        # The title carries the reference's own metrics: 16/22 SemiBold,
        # three lines.
        self.assertIn("font-size: var(--fs-headline);", SKIN)
        self.assertIn("-webkit-line-clamp: 3;", SKIN)
        # The meta row: a globe glyph and the source host in the accent -
        # the reference's prominent-identity voice; the date lives on the
        # cover as this page's play-count analogue.
        self.assertIn(".cover-meta-row {", SKIN)
        self.assertIn("color: var(--accent);", SKIN)
        self.assertIn("white-space: nowrap;", SKIN)
        self.assertIn(".cover-src {", SKIN)
        # Tags under the title as bare #words in the secondary colour.
        self.assertIn(".cover-tags {", SKIN)
        self.assertIn("-webkit-line-clamp: 2;", SKIN)
        self.assertIn(".cover-tag {", SKIN)
        # The duration flag rides the cover's foot.
        self.assertIn("right: var(--sp-2);\n  bottom: var(--sp-2);", SKIN)
        self.assertIn(".cover-flag svg", SKIN)
        # The old chrome is gone from the skin and both bundled pages.
        self.assertNotIn("cover-badges", SKIN)
        for page in self._pages().values():
            self.assertNotIn("cover-badge", page)
            self.assertNotIn("cover-meta ", page)
        discover = self._pages()["discover"]
        self.assertIn('class="cover-flag"', discover)
        self.assertIn('class="cover-tag">#', discover)
        self.assertIn('class="cover-meta-row"', discover)
        self.assertIn('class="cover-src"', discover)

    def test_the_play_hero_bleeds_to_the_phone_edges(self) -> None:
        """The hero is the artwork owning the page, not a boxed thumbnail.

        From the reference detail screen's own source: a full-bleed
        fixed-height artwork block (280-360dp) whose foot melts into the
        page through a colour ramp, with the title riding the melt. The
        tablet ranges pull it back to a boxed 16/9 hero because a centred
        column has no edge to bleed to and a ramp cannot melt inside a box.
        """
        play = self._pages()["play"]
        self.assertIn(
            ".pl-hero { position: relative; margin: 0 calc(var(--inset) * -1) var(--sp-2);",
            play,
        )
        self.assertIn("height: clamp(250px, 66vw, 380px);", play)
        # The melt: the scrim ends in the page colour, not in a shadow.
        self.assertIn("rgba(10,11,15,.58) 66%, var(--bg) 100%);", play)
        self.assertIn(".pl-hero-title { font-size: 23px;", play)
        # The tablet passes restore the boxed hero and its 16/9 stamp.
        self.assertIn(
            ".pl-hero { margin: 0 0 var(--sp-5); height: auto; border-radius: var(--r-xl); }",
            play,
        )
        self.assertEqual(
            play.count(".pl-hero-img { position: static; width: 100%; height: auto; aspect-ratio: 16 / 9; }"),
            2,
        )
        # A card can arrive with no cover at all: the plate variant keeps the
        # same full-bleed silhouette, and its text stays in theme colours so
        # the page reads in light mode too.
        self.assertIn(".pl-hero.no-cover { background: linear-gradient(150deg, var(--accent-soft), var(--surface) 62%); }", play)
        self.assertIn(".pl-hero.no-cover .pl-hero-body { position: static;", play)
        self.assertIn(".pl-hero.no-cover .pl-hero-title { color: var(--text); text-shadow: none; }", play)

    @staticmethod
    def _function_source(script: str, name: str) -> str:
        """The source of a top-level ``function name(...) { ... }``.

        Brace-matched rather than regexed, because the bodies contain braces
        inside template strings and object literals.
        """
        at = script.index(f"function {name}(")
        brace = script.index("{", at)
        depth = 0
        for j in range(brace, len(script)):
            if script[j] == "{":
                depth += 1
            elif script[j] == "}":
                depth -= 1
                if depth == 0:
                    return script[at : j + 1]
        raise AssertionError(f"unbalanced braces in {name}()")

    def test_each_card_carries_its_own_rank(self) -> None:
        """A card's rank is its position - so call the function and look.

        ``card(c, i)`` ran its badge loop as ``for (var i = 0; ...)``.  ``var``
        is function-scoped, so that redeclared the *parameter*: after the loop
        ``i`` held the number of badges, not the card's index.  Every card on
        the page got the same rank and the cascade silently flattened into
        "everything arrives at once" - while both halves of the mechanism were
        still visibly in place.  ``--i:`` was in the markup and
        ``calc(min(var(--i, 0), 9) ...)`` was in the stylesheet, so the string
        assertions above passed on a page with no stagger in it at all.

        Nothing short of calling the generator's own function can tell those
        two states apart, which is why this test runs the real ``card()``.
        """
        from asmrlib_archiver.mobile_discover import build_discover_html

        node = shutil.which("node")
        if not node:  # pragma: no cover - depends on the machine
            self.skipTest("node is not on PATH")

        script = build_discover_html()
        helpers = "\n".join(
            self._function_source(script, name)
            for name in ("esc", "plateIdx", "initial", "card")
        )
        probe = (
            helpers
            + "\nvar out = [0, 3, 7, 11].map(function (n) {\n"
            # Rank 3 carries three badges: with the parameter shadowed, that is
            # exactly the card whose rank would come out as '3' by accident.
            "  var tags = n === 3 ? ['ASMR', 'R18', 'HARI'] : ['ASMR'];\n"
            "  var html = card({ u: 'u' + n, t: 't' + n, c: '', d: '', g: tags }, n);\n"
            "  return /--i:(\\d+)/.exec(html)[1];\n"
            "});\n"
            "console.log(JSON.stringify(out));\n"
        )
        proc = subprocess.run(
            [node, "-e", probe], capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout), ["0", "3", "7", "11"],
            "card() does not hand each card its own rank, so the entrance "
            "cascade collapses to a single simultaneous arrival",
        )

    def test_the_card_speaks_the_works_identity(self) -> None:
        """The identity row is generated, so call the generator and look.

        The host line is derived from the card's URL in JavaScript
        (``new URL(u).hostname`` with ``www.`` stripped); no string assertion
        on the bundled page can tell a correct derivation from a broken one -
        a regex over a template proves only that the template exists.  The
        probe runs the real ``card()`` against a URL with a ``www.`` prefix
        and six tags, and checks three things at once: the host is derived
        and stripped, the tag flow is capped at five (an unbounded wall of
        #words would outrun the two-line clamp and stretch the masonry),
        and the date still lands on the cover's flag rather than the info
        column - the flag is this page's play-count analogue, and a date
        shown twice (cover and row) is a layout bug, not a style.
        """
        from asmrlib_archiver.mobile_discover import build_discover_html

        node = shutil.which("node")
        if not node:  # pragma: no cover - depends on the machine
            self.skipTest("node is not on PATH")

        script = build_discover_html()
        helpers = "\n".join(
            self._function_source(script, name)
            for name in ("esc", "plateIdx", "initial", "card")
        )
        probe = (
            helpers
            + "\nvar html = card({\n"
            "  u: 'https://www.example.com/watch?v=1',\n"
            "  t: '标题', c: 'https://img.example.com/x.jpg',\n"
            "  d: '2026-09-19',\n"
            "  g: ['ASMR', 'R18', 'Tag3', 'Tag4', 'Tag5', 'Tag6'],\n"
            "}, 2);\n"
            "console.log(JSON.stringify({\n"
            "  src: /class=\"cover-src\">([^<]*)</.exec(html)[1],\n"
            "  tags: (html.match(/class=\"cover-tag\"/g) || []).length,\n"
            "  flag: /class=\"cover-flag\"/.test(html),\n"
            "  metaRow: /class=\"cover-meta-row\"/.test(html),\n"
            "}));\n"
        )
        proc = subprocess.run(
            [node, "-e", probe], capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["src"], "example.com")
        self.assertEqual(out["tags"], 5)
        self.assertTrue(out["flag"])
        self.assertTrue(out["metaRow"])

    # -------------------------------------------- the in-app playback pass

    @staticmethod
    def _java_source(name: str) -> str:
        """One APK shell source file, for the native-side pins."""
        path = (Path(__file__).resolve().parents[1]
                / "android" / "app" / "src" / "main" / "java"
                / "com" / "asmrlib" / "archiver" / name)
        if not path.is_file():
            raise AssertionError(f"{name} is missing from the APK shell sources")
        return path.read_text(encoding="utf-8")

    def test_video_sources_are_classified_not_dropped(self) -> None:
        """The resolver keeps .m3u8 sources and labels every source's kind.

        Dropping HLS sources was the honest answer when the only thing a
        source could do was navigate: a manifest is not a page.  The in-app
        player changed that — a manifest is exactly what StreamProxy wants —
        so now every source carries its kind and the play page decides what
        to offer.  The Referer rides along, because a player host answers
        only requests that carry the page it was resolved from.
        """
        bridge = self._java_source("DiscoverBridge.java")
        # HLS kept, not dropped.
        self.assertNotIn('endsWith(".m3u8")) continue;', bridge)
        for marker in ('source.put("kind", kind)', 'source.put("referer",'):
            self.assertIn(marker, bridge)
        # The three kinds, and only hls/direct may launch the player.
        self.assertIn('"hls".equals(k) && !"direct".equals(k)', bridge)
        self.assertIn("public void playDirect(", bridge)
        self.assertIn("PlayerActivity.EXTRA_REMOTE_URL", bridge)
        # Opus is a direct audio format the device can decode (API 29+);
        # every format gate names it or a .opus work plays nowhere.
        self.assertIn('".ogg", ".opus", ".wav"', bridge)
        player = self._java_source("PlayerActivity.java")
        self.assertIn('n.endsWith(".opus")', player)
        self.assertIn('if (n.endsWith(".opus")) return "Opus";', player)
        self.assertIn('if (n.endsWith(".opus")) return "audio/ogg";', player)

    def test_the_play_page_offers_in_app_playback_for_media_sources(self) -> None:
        """Every source row plays in the app — embeds resolve on tap.

        A direct/hls URL is bytes, and a WebView that lands on those throws
        the page away for Chromium's own player — the source list, the title
        and every way back are gone. That is what tapping an asmr.one track
        used to do, and it read as "the new source does not respond": the row
        is the play button. An embed URL is a player *page*, but jumping to it
        would make that one row the single holdout against the unified
        player — so the row resolves on tap (bridge resolveEmbed, a shielded
        hidden WebView sniff) and rewinds into the plain external link only
        when the resolve fails.
        """
        play = self._pages()["play"]
        for marker in (
            "var media = s.kind === 'hls' || s.kind === 'direct';",
            "data-action=\"play-inapp\"",
            "data-action=\"embed-play\"",
            "b.resolveEmbed(",
            "playDirect(",
            "stopPropagation()",
        ):
            self.assertIn(marker, play)
        # One row style for both kinds: the in-app div, never a link at rest.
        self.assertIn("var rowOpen = '<div class=\"play-enter is-inapp'", play)
        self.assertNotIn("'<a class=\"play-enter'", play)
        self.assertIn("'\" data-inapp=\"1\"'", play)
        self.assertIn("' data-embed=\"1\" data-src-index=\"'", play)
        # The embed row must carry its page URL too — the whole row is the
        # tap target, and a body tap reads data-url off the row (a missing
        # one reached the bridge as an empty string and degraded the row).
        self.assertIn("' data-url=\"' + esc(s.url) + '\"')", play)
        self.assertIn("'.play-enter[data-inapp]'", play)
        # The row handler steps aside for anything with an action of its own,
        # or the pill's tap would run the queue launch twice.
        self.assertIn("if (ev.target.closest('[data-action]')) return;", play)
        # The row markup must not address any URL with an href: one navigation
        # is all it takes for the WebView to leave the page. The degrade path
        # is the only place an anchor comes back, and it links the player
        # *page* (the embed URL), never the media bytes.
        branch = play[play.index("var buttons = data.sources.map"):]
        branch = branch[: branch.index("}).join('');")]
        self.assertNotIn("href=", branch)
        self.assertIn("function degradeEmbedRow(row)", play)
        # The resolved stream enters the player through the same queue path
        # as a native row, carrying the sniffer's observed Referer pinned.
        self.assertIn("b.playDirectQueue(JSON.stringify(track), 0,", play)
        self.assertIn("track[0].rr = res.referer;", play)

    def test_embed_rows_resolve_through_a_shielded_hidden_webview(self) -> None:
        """The unified player needs the real stream behind a player page.

        Parsing the page or its scripts breaks on every host redesign;
        watching what the running player fetches does not. So resolveEmbed
        loads the embed in a never-attached WebView with the ad shield on
        every request, classifies what survives, and hands back the media
        URL with the Referer the page itself sent. The user-gesture playback
        gate stays ON — a hidden page that found its voice would be the
        worst kind of bug in an ASMR app.
        """
        bridge = self._java_source("DiscoverBridge.java")
        self.assertIn(
            "public String resolveEmbed(final String embedUrl, final String pageUrl,",
            bridge,
        )
        self.assertIn('enqueue("embed", embedUrl, requestId, pageUrl)', bridge)
        # Same traffic rules as the visible shell: ads are blocked before a
        # URL can be classified, and navigation passes the shield too.
        self.assertIn("shield.intercept(view, request)", bridge)
        self.assertIn("shield.shouldBlockNavigation(url)", bridge)
        # JS must run (the player fetches its own manifest), and both real
        # hosts gate the stream behind a play gesture — proven live — so the
        # WebView gesture gate is opened and the page's own play button is
        # nudged. The price is autoplay in a hidden page, so the sniffer must
        # ship the mute patch (forced on every play() + re-sweeps); a hidden
        # page that found its voice would be the worst bug an ASMR app could
        # ship.
        self.assertIn("st.setJavaScriptEnabled(true)", bridge)
        self.assertIn("st.setMediaPlaybackRequiresUserGesture(false)", bridge)
        self.assertIn("P.play=function(){try{this.muted=true", bridge)
        self.assertIn(
            "private void muteAndNudge(WebView view)", bridge)
        self.assertIn("main.postDelayed(nudge, 2_500L)", bridge)
        # Cross-origin iframes ignore synthetic clicks — only a real input
        # event carries user activation, and the embed player's whole surface
        # is its play button. So the hidden WebView is attached with a real
        # size (off-screen, transparent) and the nudge dispatches a touch at
        # its center; the moment a media URL is classified the player is
        # paused, so its speaker time ends before the first sample.
        self.assertIn("private void tapCenter(WebView view)", bridge)
        self.assertIn("view.dispatchTouchEvent(down)", bridge)
        self.assertIn("if (w != null) w.onPause();", bridge)
        self.assertIn("content.addView(wv)", bridge)
        # The embed host is a shell that iframes the real player (bysetayico
        # /e/{id} → rotated host /{route}/{id}, seen live): the interceptor
        # admits that one handover through the shield's own embedded-player
        # rule before the shield answers, or the iframe renders the refusal
        # page and no byte ever flows. trustEmbeddedPlayerUrl had no
        # production caller until this sniffer wired it.
        self.assertIn("shield.trustEmbeddedPlayerUrl(embedUrl, u)", bridge)
        self.assertIn("if (!request.isForMainFrame()) {", bridge)
        # Bounded by construction: a latch with a timeout, and the WebView is
        # always destroyed afterwards.
        self.assertIn("done.await(", bridge)
        self.assertIn("EMBED_SNIFF_TIMEOUT_MS", bridge)
        self.assertIn("wv.destroy()", bridge)
        # A sniffer watching a hidden page must not mistake local traffic or
        # an advert for the stream.
        self.assertIn("static boolean isPublicHttpUrl(String raw)", bridge)
        self.assertIn('shield.isAdUrl(target)', bridge)
        # Refusals are loud, not silent degrades.
        self.assertIn('Log.i(TAG, "embed resolve refused: " + target)', bridge)
        # The observed Referer is the ground truth; the cache stays short
        # because sniffed CDN URLs often carry signed, expiring tokens.
        self.assertIn("observedReferer(request, embedUrl)", bridge)
        self.assertIn("EMBED_TTL_MS = 5 * 60_000L", bridge)

    def test_a_tracks_name_is_not_wrapped_in_its_folder_path(self) -> None:
        """The label names the track; the path travels as its own field.

        Every consumer renders the label as the name of what is playing — the
        now-playing title, the queue row, the notification — and the path in
        that slot crowded the name out: the play page's rows wrapped to five
        lines on a work whose folders read 「06: 特典 · 03: 闹钟音频 · 01_mp3」,
        and the queue row ellipsised away the one word that differed.
        """
        src = self._java_source("AsmrOneSource.java")
        self.assertIn('s.put("label", label);', src)
        self.assertIn('if (!folder.isEmpty()) s.put("group", folder);', src)
        self.assertNotIn('if (!folder.isEmpty()) label = folder + " · " + label;', src)

    def test_a_folder_is_shown_only_where_the_name_is_ambiguous(self) -> None:
        """Two tracks named 「01_あさ」 need their folders; eighty do not.

        A work's folder names repeat on row after row, so the path is
        boilerplate in the common case and the only disambiguator in the rare
        one.  The page counts the names and prints the deepest folder — the
        segment that differs — only where the row would otherwise read twice.
        """
        play = self._pages()["play"]
        for marker in (
            "var nameCount = {};",
            "nameCount[n] = (nameCount[n] || 0) + 1;",
            "var tail = nameCount[name] > 1 ? (parts[parts.length - 1] || '') : '';",
            "(tail ? ' · ' + esc(tail) : '')",
        ):
            self.assertIn(marker, play)

    def test_the_play_page_names_the_source_it_is_showing(self) -> None:
        """The two sources are not interchangeable, so the page says which.

        An asmr.one work's rows play in the app; the site's rows open a page.
        A user who cannot tell which world they are in reads the second
        behaviour as the first one broken — which is what "the new source
        does not respond" turned out to mean.  The hero carries the origin,
        with the mirror family folded into one name.
        """
        play = self._pages()["play"]
        for marker in (
            "function originLabel(url)",
            "'<span class=\"pl-hero-src\">' + esc(originLabel(entry.u))",
            "if (/^asmr(-(100|200|300))?\\.(one|com)$/.test(host)) return 'ASMR.one';",
            "if (/asmrlib/.test(host)) return '本站';",
            ".pl-hero-src { align-self: flex-start;",
        ):
            self.assertIn(marker, play)

    def test_a_long_name_cannot_stretch_a_row(self) -> None:
        """One line, ellipsised: a name is not allowed to become a paragraph.

        The label was a path AND unclamped, so both ends needed the fix — the
        data split (above) and a clamp that holds whatever the data says, or
        the next long string grows the row again.
        """
        from asmrlib_archiver.mobile_skin import SKIN

        self.assertIn(
            ".src-name {\n"
            "  flex: 0 1 auto;\n"
            "  min-width: 0;\n"
            "  overflow: hidden;\n"
            "  text-overflow: ellipsis;\n"
            "  white-space: nowrap;\n"
            "}",
            SKIN,
        )
        # The meta line wraps for the same reason (a CDN host alone is wider
        # than the row) and gets the same treatment.
        self.assertIn(
            ".src-meta {\n"
            "  min-width: 0;\n"
            "  overflow: hidden;\n"
            "  text-overflow: ellipsis;\n"
            "  white-space: nowrap;\n"
            "}",
            SKIN,
        )

    def test_the_artwork_is_sized_from_what_the_page_needs(self) -> None:
        """The cover's height budget is measured, not a constant.

        ``heightPixels - dp(300)`` cannot know how many rows the page has:
        when the utility row arrived under the transport, a 640dp screen was
        left with the identity title squeezed into a 33px band and the meta
        line at zero — a LinearLayout clips the overflow, it does not
        ellipsise it, so the one line that says what is playing was
        unreadable.  The identity lines are now measured unconstrained and
        the artwork takes what is left.
        """
        player = self._java_source("PlayerActivity.java")
        self.assertNotIn("int byHeight = h - dp(300);", player)
        self.assertIn("private void fitAudioCover(View body)", player)
        self.assertIn("addOnLayoutChangeListener", player)
        self.assertIn("View[] lines = {identityTitle, identityMeta};", player)
        self.assertIn("line.measure(widthSpec, freeSpec)", player)
        self.assertIn("View.MeasureSpec.UNSPECIFIED", player)

    def test_multi_track_works_reach_the_player_as_one_queue(self) -> None:
        """Eara's continuous playback, end to end in three pins.

        An asmr.one resolve carries dozens of tracks; playing one and
        stopping would strand the rest.  The page builds the playable
        subset into a queue (buttons carry only an index — a full JSON
        copy per row would bloat the DOM), the bridge validates, caps and
        forwards it, and the player walks forward on completion instead
        of idling.
        """
        play = self._pages()["play"]
        for marker in (
            "playDirectQueue(",
            "playQueue.push({ u: s.url, l: s.label",
            'data-qi="',
        ):
            self.assertIn(marker, play)
        bridge = self._java_source("DiscoverBridge.java")
        for marker in (
            "public void playDirectQueue(",
            "private static final int QUEUE_CAP = 200;",
            "EXTRA_REMOTE_QUEUE",
        ):
            self.assertIn(marker, bridge)
        player = self._java_source("PlayerActivity.java")
        for marker in (
            "EXTRA_REMOTE_QUEUE",
            "private boolean advanceQueue()",
            "resumeKey = PlaybackResume.keyFor(taskId, url)",
            "if (advanceQueue()) {",
        ):
            self.assertIn(marker, player)

    def test_asmrone_ships_eards_mirror_family_and_video_tracks(self) -> None:
        """The reference player's second source is one API on four hosts.

        a single unreachable domain stranded the whole source. The mirror is
        a page-visible chip row (线路) that persists per device, the virtual
        search URL carries it as a tail segment, and the resolve lands on the
        same mirror the card came from. Works also ship mp4 extras: video
        leaves are collected alongside audio — the reference player treats
        both as playable media, so the page offers them all in one queue.
        """
        src = self._java_source("AsmrOneSource.java")
        for marker in (
            '"https://api.asmr-100.com/api/"',
            '"https://api.asmr-200.com/api/"',
            '"https://api.asmr-300.com/api/"',
            "static String apiFor(String site)",
            "apiFor(siteFromUrl(url))",
            'card.put("u", "https://" + siteHost(site) + "/work/"',
            '!"video".equals(type)',  # video leaves are collected next to audio
        ):
            self.assertIn(marker, src)
        bridge = self._java_source("DiscoverBridge.java")
        self.assertIn('target.startsWith(ASMRONE_SCHEME)', bridge)
        self.assertIn('AsmrOneSource.workId(target)', bridge)
        # The page exposes the switch and rides it on the virtual URL.
        disc = self._pages()["discover"]
        for marker in (
            "class='ds-lines is-off'",
            "data-site='200'",
            'function oneUrl(',
            "'ds-asmrone-site'",
            "/(one|100|200|300))?/",
        ):
            self.assertIn(marker, disc)
        # The mirrors' covers and streams must pass the ad shield, or the
        # cards render imageless and the player black-holes.
        import json
        from pathlib import Path
        blocklist = json.loads(
            (Path(__file__).resolve().parents[1]
             / "android" / "app" / "src" / "main" / "assets"
             / "blocklist.json").read_text(encoding="utf-8"))
        for domain in ("asmr-100.com", "asmr-200.com", "asmr-300.com"):
            self.assertIn(domain, blocklist["asset_domains"])

    def test_switching_sources_keeps_the_feed_and_moves_it(self) -> None:
        """A source switch is a transition, not a navigation.

        Tapping the other pill used to blank the feed for a skeleton 250ms
        later and teleport the viewport to the top — two discontinuities for
        one tap.  The cards now stay on screen and dim while the other source
        resolves, and the page glides to the top instead of arriving there in
        one frame.
        """
        disc = self._pages()["discover"]
        for marker in (
            # The switch names itself, so request() can tell it apart from a
            # plain navigation and leave the skeleton alone.
            "state.switching = true;",
            "} else if (state.switching) {",
            "'正在切换到 ' + (isOneUrl(url) ? 'ASMR.one' : '本站') + '…'",
            "setSwitchDim(true);",
            "glideToTop();",
            "var wasSwitching = !!state.switching;",
        ):
            self.assertIn(marker, disc)
        # Both endpoints clear it — the render that replaces the feed and the
        # error that replaces it with a panel — or the next listing renders
        # under a dim nobody took off.
        self.assertEqual(disc.count("state.switching = false;"), 2)
        # The switch's own glide replaces the snap, which would cut it short.
        self.assertIn("if (!state.chain && !wasSwitching) window.scrollTo(0, 0);", disc)
        # And the glide obeys Reduce Motion like the pagination one does.
        self.assertIn("? 'auto' : 'smooth'", disc)

    def test_the_source_chrome_collapses_instead_of_vanishing(self) -> None:
        """The search field and the mirror row belong to one source only.

        Showing or hiding them with ``display: none`` moved the whole feed by
        94px in a single frame.  A max-height collapse makes it a movement,
        and ``visibility`` flips at the end of it so the input leaves the tab
        order with the box rather than a frame early.
        """
        from asmrlib_archiver.mobile_skin import SKIN

        self.assertIn(".ds-search.is-off, .ds-lines.is-off {", SKIN)
        self.assertIn("max-height: 0;", SKIN)
        self.assertIn("visibility: hidden;", SKIN)
        self.assertNotIn(".ds-search[hidden]", SKIN)
        self.assertNotIn(".ds-lines[hidden]", SKIN)
        # The markup ships them already collapsed, so the first paint has no
        # state to animate out of.
        disc = self._pages()["discover"]
        self.assertIn("class='ds-search is-off'", disc)
        self.assertIn("class='ds-lines is-off'", disc)

    def test_the_tabs_report_their_selection(self) -> None:
        """The pills moved without the roles.

        Only the class was toggled, so the tabs have read "本站 selected" to a
        screen reader since the second source landed.
        """
        disc = self._pages()["discover"]
        self.assertIn("tabSite.setAttribute('aria-selected', String(!one));", disc)
        self.assertIn("tabOne.setAttribute('aria-selected', String(one));", disc)

    def test_the_audio_player_is_earas_now_playing(self) -> None:
        """The audio page is the Classic now-playing port.

        Read from the reference implementation's source: a square artwork
        (28dp-rounded, clamped at 360dp) floating on the artwork-derived
        backdrop with no card box; identity under it at 18/20 bold with one
        13sp meta line; a 72dp play button whose corner morphs (36↔24dp on
        the reference) as the playing state; a header that carries collapse
        on the left.  A video stage would be wrong here twice: a 9:16 stage
        clamp and a facts column are video furniture.
        """
        source = self._java_source("PlayerActivity.java")
        for marker in (
            "private void buildAudioChrome(",
            "audioCoverSide()",                      # the 360dp clamp
            "coverPlate.setCornerRadius(dp(28));",   # the reference's radius
            "identityTitle",
            "identityMeta",
            "new PlayPauseButton(this, dp(72));",    # the reference's size
            "size * 0.30f : size * 0.44f",           # the corner morph
            "applyArtworkBackdrop",
            "artworkHue",
            "backdropTint",
            "backdropScrim",
            "enterStagger(",
        ):
            self.assertIn(marker, source)
        # The backdrop tint is clamped into the reference's pastel band
        # (S 0.24–0.58, L/V 0.42–0.56) — the raw mean of a cover is usually
        # near-black and would vanish.
        self.assertIn("Math.max(0.24f, Math.min(0.58f, hsv[1]))", source)
        self.assertIn("Math.max(0.42f, Math.min(0.56f, hsv[2]))", source)
        # The video stage stays for video: audio mode is a branch, not a
        # replacement.
        self.assertIn("if (audioMode) {", source)

    def test_the_stream_proxy_is_referenced_by_both_players(self) -> None:
        """Streams are registered once and held by whoever is playing.

        The activity registers and holds a reference; the sleep-handoff
        service takes its own and releases it in onDestroy.  The idle timer
        between the two references is what keeps the 息屏 handoff from
        cutting the audio mid-word.
        """
        player = self._java_source("PlayerActivity.java")
        service = self._java_source("PlaybackService.java")
        self.assertIn("StreamProxy.register(remoteUrl, remoteReferer)", player)
        self.assertIn("StreamProxy.release()", player)
        self.assertIn("StreamProxy.retain()", service)
        self.assertIn("StreamProxy.release()", service)
        self.assertIn("StreamProxy.isOurs(playingUri)", service)
        self.assertIn("holdingProxy", service)

    # --------------------------------------------------------------- press

    def test_press_in_is_quicker_than_the_release(self) -> None:
        """The finger wants the change immediately; the spring back is watchable.

        Every tappable declares its own transition, so the shared rule only
        overrides the *duration* - and only while the press is held.
        """
        from asmrlib_archiver.mobile_skin import SKIN

        at = SKIN.index("transition-duration: var(--dur-press)")
        # The selector list sits *before* the declaration, so walk back to the
        # end of the previous rule rather than forward to the next brace.
        block = SKIN[SKIN.rindex("}", 0, at) + 1 : at]
        for selector in (".icon-btn:active", ".chip:active", ".cover-card:active",
                         ".button:active", ".glass-chip:active", ".play-enter:active"):
            self.assertIn(selector, block, f"{selector} has no press-in duration")

    # ------------------------------------------------------- reduce motion

    def test_reduce_motion_drops_travel_but_keeps_feedback(self) -> None:
        """The trade iOS's Reduce Motion makes: no movement, still a response.

        A blanket ``* { animation: none; transition: none }`` is wrong here for
        two separate reasons.  It kills the press states, which leaves a UI
        that does not react to touch at all; and it kills the pull-to-refresh
        arc fill, which is a transition carrying information - the pull
        distance - and which the page contracts pin as staying.
        """
        from asmrlib_archiver.mobile_skin import SKIN

        block = SKIN[SKIN.index("@media (prefers-reduced-motion: reduce)"):]
        self.assertIn(".cover-card, .err-hero, .pl-hero, .play-enter { animation: none; }", block)
        self.assertIn("transform: none;", block)
        # Feedback survives as a colour/opacity change.
        self.assertIn(".button:active { opacity: .7; }", block)
        # The card's box is gone, so its travel-free press answer is the dim.
        self.assertIn(".cover-card:active { opacity: .7; }", block)
        # And it is still targeted, not a blanket override.
        self.assertNotIn("* {", block)
        self.assertNotIn("transition: none", block)

    def test_the_scroll_glide_respects_reduce_motion(self) -> None:
        """A smooth glide is a large fast movement across the viewport.

        That is precisely what the setting exists to avoid, so the pagination
        glide has to become a jump.  ``scroll-behavior: smooth`` in CSS would
        have been the obvious fix and the wrong one: it cannot be opted out of
        from the call site.
        """
        html = self._pages()["discover"]
        self.assertIn("prefers-reduced-motion: reduce", html)
        self.assertIn("? 'auto' : 'smooth'", html)
        self.assertIn("scrollIntoView({ behavior: glide, block: 'start' })", html)
        self.assertNotIn("scrollIntoView({ behavior: 'smooth'", html)

    # ------------------------------------------------------ no stray colour

    def test_no_page_hardcodes_a_palette_colour_outside_the_token_block(self) -> None:
        """The token block is the only place a literal colour may live.

        This is not hypothetical either: the splash arc was stroked with an SVG
        ``linearGradient`` whose two stops were written out as literals, so it
        kept the dark theme's periwinkle in light mode while every other accent
        on the page had already deepened.  One stray copy is all it takes to
        reintroduce the near-miss the palette exists to remove.
        """
        from asmrlib_archiver.mobile_skin import SKIN

        marker = "@media (prefers-color-scheme: light)"
        dark_src, light_src = SKIN.split(marker, 1)
        palette = set()
        for block in (dark_src, light_src):
            stripped = re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)
            palette |= {
                value.lower()
                for value in re.findall(
                    r"--[a-z0-9-]+\s*:\s*(#[0-9a-fA-F]{6})\s*;", stripped
                )
            }
        self.assertTrue(palette, "no hex token values found - the parse is wrong")
        for name, html in self._pages().items():
            # The theme-color metas are exempt, and only because a different
            # test pins them equal to the bg token in both schemes - they have
            # to be literals, since the platform reads them before any CSS.
            # Comments are stripped too: prose that names a colour is not a
            # declaration of one, and this file's own comments do exactly that.
            body = re.sub(r"/\*.*?\*/", "", self._strip_root_blocks(html), flags=re.DOTALL)
            body = re.sub(r'<meta name="theme-color"[^>]*>', "", body)
            for value in sorted(palette):
                self.assertNotIn(
                    value, body,
                    f"{name} hardcodes {value} outside a token block; it will not "
                    f"follow the theme",
                )
            # The SVG-gradient shape of the same bug: a presentation attribute
            # cannot resolve var(), so a gradient stop has to be a literal.
            self.assertNotIn("stop-color='#", body)


if __name__ == "__main__":
    unittest.main()
