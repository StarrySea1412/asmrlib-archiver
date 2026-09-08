from __future__ import annotations

import unittest

import httpx

import asmrlib_archiver.player_resolver as pr
from asmrlib_archiver.player_resolver import (
    PlayerResolveError,
    _is_player_host,
    _rank,
    resolve_player_url,
)

_POST_HTML = """
<html><body><div id="players">
  <button data-url="https://abyssplayer.com/e/xyz" data-type="iframe" data-server="abyss">AB</button>
  <button data-url="https://bysetayico.com/e/abc123" data-type="iframe" data-server="byse">BI</button>
  <button data-url="https://upn.one/embed/9x8y" data-type="iframe" data-server="upnshare">UP</button>
</div>
<h1 class="post-title">Test post</h1></body></html>
"""

_EMPTY_HTML = "<html><body><p>no players here</p></body></html>"


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _FakeTransport(httpx.BaseTransport):
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        page = self.pages.get(str(request.url))
        if page is None:
            return httpx.Response(404, text="missing")
        return httpx.Response(200, text=page)


class PlayerResolverTests(unittest.TestCase):
    def _resolve_with(self, pages: dict[str, str], url: str):
        transport = _FakeTransport(pages)
        original_get = pr.httpx.get

        def fake_get(target, **kwargs):
            # Reuse a client-bound request through the fake transport.
            request = httpx.Request("GET", target)
            response = transport.handle_request(request)
            if response.status_code != 200:
                response.raise_for_status()
            return _FakeResponse(response.text)

        pr.httpx.get = fake_get  # type: ignore[assignment]
        try:
            return resolve_player_url(url)
        finally:
            pr.httpx.get = original_get  # type: ignore[assignment]

    def test_direct_player_url_short_circuits(self) -> None:
        resolved = resolve_player_url("https://bysetayico.com/e/abc")
        self.assertTrue(resolved.is_direct)
        self.assertEqual(resolved.player_url, "https://bysetayico.com/e/abc")

    def test_resolves_best_embed_and_alternatives(self) -> None:
        post = "https://asmrlib.com/posts/example"
        resolved = self._resolve_with({post: _POST_HTML}, post)
        self.assertFalse(resolved.is_direct)
        self.assertEqual(resolved.player_url, "https://bysetayico.com/e/abc123")
        self.assertIn("https://upn.one/embed/9x8y", resolved.alternatives)
        self.assertIn("https://abyssplayer.com/e/xyz", resolved.alternatives)

    def test_page_without_player_raises(self) -> None:
        post = "https://asmrlib.com/posts/empty"
        with self.assertRaises(PlayerResolveError) as ctx:
            self._resolve_with({post: _EMPTY_HTML}, post)
        self.assertEqual(ctx.exception.code, "no_player_found")

    def test_non_site_page_is_rejected(self) -> None:
        from asmrlib_archiver.guards import BlockedUrl

        with self.assertRaises((PlayerResolveError, BlockedUrl)):
            self._resolve_with({}, "https://evil.com/posts/x")

    def test_host_classification(self) -> None:
        self.assertTrue(_is_player_host("https://bysetayico.com/e/abc"))
        self.assertTrue(_is_player_host("https://v.upn.one/x"))
        self.assertFalse(_is_player_host("https://asmrlib.com/posts/x"))
        self.assertFalse(_is_player_host("https://doubleclick.net/x"))

    def test_rank_prefers_byse_then_up_then_abyss(self) -> None:
        self.assertEqual(_rank("BI", "https://bysetayico.com/e/1"), 0)
        self.assertEqual(_rank("UP", "https://upn.one/e/1"), 1)
        self.assertEqual(_rank("AB", "https://abyssplayer.com/e/1"), 2)
        self.assertEqual(_rank("?", "https://other.example/e/1"), 9)


if __name__ == "__main__":
    unittest.main()
