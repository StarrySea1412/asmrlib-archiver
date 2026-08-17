from __future__ import annotations

import unittest
from pathlib import Path

from asmrlib_archiver.config import AppConfig
from asmrlib_archiver.online_shield import (
    GuardedWebViewPlayer,
    ShieldPolicy,
    ShieldStats,
    _resolve_native_targets,
    attach_webview_shield,
    build_page_bootstrap,
)


class _FakeArgs:
    def __init__(self, url: str = "", context: str = "script") -> None:
        self.Uri = url
        self.Request = type("Request", (), {"Uri": url, "ResourceContext": context})()
        self.Cancel = False
        self.Handled = False
        self.Response = None


class _FakeCore:
    def __init__(self, script_task=None) -> None:
        self.NavigationStarting = []
        self.FrameNavigationStarting = []
        self.NewWindowRequested = []
        self.WebResourceRequested = []
        self.scripts: list[str] = []
        self.script_task = script_task

    def AddScriptToExecuteOnDocumentCreatedAsync(self, script: str):
        self.scripts.append(script)
        return self.script_task

    def AddWebResourceRequestedFilter(self, *_args):
        return None


class _FakeControl:
    """WinForms-like wrapper used to verify UI-thread marshaling."""

    def __init__(self, core: _FakeCore) -> None:
        self.CoreWebView2 = core
        self.NavigationStarting = []
        self.FrameNavigationStarting = []
        self.CoreWebView2InitializationCompleted = []
        self.InvokeRequired = True
        self.invoke_calls = 0

    def Invoke(self, callback):
        self.invoke_calls += 1
        return callback()


class _FakeWindow:
    def __init__(self, control: _FakeControl) -> None:
        self.gui = type("Gui", (), {"webview": control})()
        # This deliberately looks like a competing proxy.  The real control
        # must win when both paths are present.
        self.native = type("Proxy", (), {"InvokeRequired": True})()


class _FakeLifecycleEvent(list):
    def wait(self, _timeout=None):
        return True


class _UninitializedWindow:
    def __init__(self) -> None:
        self.events = type(
            "Events",
            (),
            {
                "shown": _FakeLifecycleEvent(),
                "loaded": _FakeLifecycleEvent(),
            },
        )()
        self.loaded_urls: list[str] = []
        self.destroyed = False

    def load_url(self, url: str) -> None:
        self.loaded_urls.append(url)

    def destroy(self) -> None:
        self.destroyed = True


class _FakeWebViewModule:
    def __init__(self, window) -> None:
        self.window = window

    def create_window(self, *_args, **_kwargs):
        return self.window


class OnlineShieldTests(unittest.TestCase):
    def test_policy_allows_player_and_blocks_ad_or_cross_origin(self) -> None:
        policy = ShieldPolicy()
        self.assertTrue(policy.navigation_decision("https://bysetayico.com/e/abc").allowed)
        self.assertTrue(policy.request_decision("https://q8y5z.com/api/videos/x/embed/playback", resource_type="media").allowed)
        self.assertFalse(policy.request_decision("https://doubleclick.net/ad.js", resource_type="script").allowed)
        self.assertFalse(policy.navigation_decision("https://evil.example/popup").allowed)

    def test_app_config_download_policy_does_not_block_online_cdn_media(self) -> None:
        config = AppConfig(config_path=Path("config.yaml"))
        policy = ShieldPolicy.from_config(config)
        self.assertTrue(policy.allow_external_media)
        self.assertTrue(
            policy.request_decision(
                "https://short-lived-cdn.example/segment-1.m4s",
                resource_type="media",
            ).allowed
        )
        self.assertFalse(
            policy.request_decision(
                "https://short-lived-cdn.example/player.js",
                resource_type="script",
            ).allowed
        )

    def test_page_bootstrap_contains_interception_and_autoplay_hooks(self) -> None:
        script = build_page_bootstrap(ShieldPolicy())
        for marker in (
            "window.open",
            "MutationObserver",
            "captcha-gate__play",
            "video,audio",
            "bysetayico.com",
            "blocked_hosts",
        ):
            self.assertIn(marker, script)

    def test_native_binding_blocks_popup_navigation_and_ad_request(self) -> None:
        core = _FakeCore()
        binding = attach_webview_shield(target=core, policy=ShieldPolicy())
        self.assertTrue(binding.installed)
        self.assertTrue(core.scripts)

        popup = _FakeArgs("https://ad.example/popup")
        binding._on_new_window(core, popup)
        self.assertTrue(popup.Handled)

        nav = _FakeArgs("https://evil.example/redirect")
        binding._on_navigation(core, nav)
        self.assertTrue(nav.Cancel)

        request = _FakeArgs("https://doubleclick.net/ad.js", "script")
        binding._on_resource(core, request)
        self.assertTrue(request.Cancel or request.Response is not None)
        self.assertGreaterEqual(binding.stats.blocked_ads, 1)
        binding.detach()

    def test_direct_core_resolution_and_stats_shape(self) -> None:
        core = _FakeCore()
        self.assertEqual(_resolve_native_targets(core), (core, None))
        self.assertEqual(set(ShieldStats().as_dict()), {
            "blocked_popups", "blocked_ads", "blocked_nav", "allowed_requests",
            "autoplay_clicks", "server_clicks", "captcha_clicks",
        })
        self.assertTrue(hasattr(GuardedWebViewPlayer(), "open"))

    def test_install_marshals_through_real_webview_control(self) -> None:
        core = _FakeCore()
        control = _FakeControl(core)
        binding = attach_webview_shield(window=_FakeWindow(control), policy=ShieldPolicy())
        self.assertTrue(binding.installed)
        self.assertEqual(control.invoke_calls, 1)

    def test_player_never_loads_remote_url_before_shield_is_ready(self) -> None:
        window = _UninitializedWindow()
        player = GuardedWebViewPlayer(webview_module=_FakeWebViewModule(window))
        result = player.open("https://bysetayico.com/e/abc")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "open_failed")
        self.assertEqual(window.loaded_urls, [])
        self.assertTrue(window.destroyed)


if __name__ == "__main__":
    unittest.main()
