from __future__ import annotations

import unittest
from pathlib import Path

from asmrlib_archiver.archive_html import (
    ARCHIVE_MARKER,
    is_safe_archive_html,
    render_archive_html,
)
from asmrlib_archiver.guards import UrlGuard
from asmrlib_archiver.parser import AsmrlibParser

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = (
    ROOT
    / "tests"
    / "fixtures"
    / "ad_laden_detail.html"
)
SOURCE_URL = "https://asmrlib.com/posts/77badb9b69d7bfbcc2fc3369e2d3b5ae"


class ParserTests(unittest.TestCase):
    def setUp(self) -> None:
        guard = UrlGuard(
            ["asmrlib.com"],
            allowed_media_domains=["bysetayico.com", "v.upn.one"],
            allow_external_media=True,
        )
        self.parser = AsmrlibParser(guard, [".mp4", ".mp3", ".m3u8"])

    def test_tag_page_discovers_only_exact_same_origin_posts_and_pagination(self) -> None:
        html = """
        <html><body><main><h1>Yoonying</h1>
          <a href="/posts/0123456789abcdef0123456789abcdef">valid</a>
          <a href="/posts/fedcba9876543210fedcba9876543210?utm_source=ad">tracking</a>
          <a href="/posts/not-a-post">invalid</a>
          <a href="https://evil.example/posts/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">external</a>
          <a href="https://sead.pages.dev/xingchen">promotion</a>
          <a href="/tags/yoonying?page=9">unmarked page link</a>
        </main>
        <a rel="next" href="/tags/yoonying?page=2">next</a>
        <nav aria-label="Pagination Navigation">
          <a href="/tags/yoonying?page=3">3</a>
          <a href="/tags/other?page=4">other tag</a>
          <a href="https://evil.example/tags/yoonying?page=5">external page</a>
          <a href="/tags/yoonying?utm_source=ad&page=6">tracking page</a>
        </nav></body></html>
        """
        parsed = self.parser.parse_tag_page("https://asmrlib.com/tags/yoonying", html)

        self.assertEqual(
            parsed.post_urls,
            ["https://asmrlib.com/posts/0123456789abcdef0123456789abcdef"],
        )
        self.assertEqual(
            parsed.next_page_url,
            "https://asmrlib.com/tags/yoonying?page=2",
        )
        self.assertEqual(parsed.next_pages, [parsed.next_page_url])
        archive = render_archive_html(parsed)
        self.assertTrue(is_safe_archive_html(archive))
        self.assertNotRegex(archive.lower(), r"\b(?:href|src)\s*=")

    def test_tag_page_next_page_moves_forward_from_current_page(self) -> None:
        html = """
        <main><h1>Yoonying page 2</h1></main>
        <nav class="pagination">
          <a href="?page=1">previous</a>
          <a href="?page=2">current</a>
          <a href="?page=4">later</a>
          <a href="?page=3">next</a>
        </nav>
        """
        parsed = self.parser.parse_tag_page(
            "https://asmrlib.com/tags/yoonying?page=2", html
        )

        self.assertEqual(
            parsed.next_page_url,
            "https://asmrlib.com/tags/yoonying?page=3",
        )

    def test_detail_uses_positive_content_and_media_regions(self) -> None:
        html = """
        <html><head><title>fallback title</title></head><body><main id="main">
          <h1>Yoonying &amp; ASMR</h1><time datetime="2026-07-12"></time>
          <div><a href="https://sead.pages.dev/xingchen">monthly promotion</a></div>
          <div id="players">
            <button data-server="byse" data-url="https://bysetayico.com/e/abc"
                    data-type="iframe">BI</button>
            <button data-server="upnshare" data-url="https://v.upn.one/#iupmtw"
                    data-type="iframe">UP</button>
            <button data-url="https://js.wpadmngr.com/fake" data-type="iframe">
              promoted player
            </button>
          </div>
          <div id="downloads"><a href="/media/file.mp3">Audio</a></div>
          <div id="downloads"><a href="https://unknown.example/ad.mp3">Unknown</a></div>
          <a href="/tags/ASMR">ASMR</a><a href="/tags/yoonying">yoonying</a>
          <header>Related</header>
          <a href="/tags/unrelated">unrelated</a>
          <a href="https://evil.example/fake.mp3">not in media region</a>
        </main></body></html>
        """
        parsed = self.parser.parse(SOURCE_URL, html)

        self.assertEqual(parsed.tags, ["ASMR", "yoonying"])
        self.assertEqual(parsed.servers, ["BI", "UP"])
        self.assertEqual(parsed.external_links, [])
        self.assertNotIn("promotion", parsed.text_excerpt.lower())
        self.assertNotIn("promoted player", parsed.text_excerpt.lower())
        self.assertEqual(len(parsed.media), 3)
        self.assertIn("https://v.upn.one/#iupmtw", [item.media_url for item in parsed.media])
        self.assertNotIn("https://evil.example/fake.mp3", [item.media_url for item in parsed.media])
        self.assertNotIn(
            "https://unknown.example/ad.mp3",
            [item.media_url for item in parsed.media],
        )
        embed_statuses = {
            item.media_url: item.status
            for item in parsed.media
            if item.kind == "embed"
        }
        self.assertEqual(embed_statuses["https://bysetayico.com/e/abc"], "reference")
        self.assertEqual(embed_statuses["https://v.upn.one/#iupmtw"], "reference")

    def test_embed_players_are_kept_as_references_without_external_download(self) -> None:
        """Even with allow_external_media=false, record iframe players for the library."""
        guard = UrlGuard(["asmrlib.com"], allow_external_media=False)
        parser = AsmrlibParser(guard, [".mp4", ".mp3"])
        html = """
        <main id="main">
          <h1>Yoonying</h1>
          <div id="players">
            <button data-url="https://bysetayico.com/e/abc" data-type="iframe">BI</button>
            <button data-url="https://v.upn.one/#clip" data-type="iframe">UP</button>
          </div>
          <div id="downloads"><a href="/media/file.mp3">Audio</a></div>
          <a href="/tags/yoonying">yoonying</a>
        </main>
        """
        parsed = parser.parse(SOURCE_URL, html)
        self.assertEqual(parsed.servers, ["BI", "UP"])
        by_kind = {item.kind: item for item in parsed.media}
        embeds = [item for item in parsed.media if item.kind == "embed"]
        self.assertEqual(len(embeds), 2)
        self.assertTrue(all(item.status == "reference" for item in embeds))
        self.assertEqual(by_kind["download"].status, "pending")
        self.assertEqual(
            by_kind["download"].media_url,
            "https://asmrlib.com/media/file.mp3",
        )

    def test_cover_prefers_jsonld_thumbnail_over_related_post_first_img(self) -> None:
        """asmrlib in-content <img> is a related-post strip; JSON-LD thumbnail
        is the per-post cover and must win."""
        html = """
        <html><head><title>fallback title</title></head><body><main id="main">
          <h1>Yoonying &amp; ASMR</h1>
          <script type="application/ld+json">
            {"@type": "VideoObject", "name": "abc",
             "thumbnailUrl": "https://videothumbs.me/n5ce857wj65u.jpg"}
          </script>
          <img src="https://videothumbs.me/ommyxm97fsfm.jpg">
          <img src="https://videothumbs.me/related1.jpg">
        </main></body></html>
        """
        parsed = self.parser.parse(SOURCE_URL, html)
        self.assertEqual(parsed.cover, "https://videothumbs.me/n5ce857wj65u.jpg")

    def test_cover_falls_back_to_first_img_when_no_jsonld(self) -> None:
        html = """
        <html><head><title>t</title></head><body><main><h1>t</h1>
          <img src="https://videothumbs.me/onlyone.jpg">
        </main></body></html>
        """
        parsed = self.parser.parse(SOURCE_URL, html)
        self.assertEqual(parsed.cover, "https://videothumbs.me/onlyone.jpg")

    def test_cover_ignores_ad_image_hosts(self) -> None:
        html = """
        <html><head><title>t</title></head><body><main><h1>t</h1>
          <img src="https://doubleclick.net/static/ad_banner.jpg">
        </main></body></html>
        """
        parsed = self.parser.parse(SOURCE_URL, html)
        self.assertEqual(parsed.cover, "")

    def test_invalid_rel_next_cannot_skip_sequential_pagination(self) -> None:
        html = """
        <main><h1>Yoonying</h1></main>
        <a rel="next" href="?page=100">bad jump</a>
        <nav class="pagination"><a href="?page=2">2</a></nav>
        """
        parsed = self.parser.parse_tag_page("https://asmrlib.com/tags/yoonying", html)
        self.assertEqual(parsed.next_page_url, "https://asmrlib.com/tags/yoonying?page=2")

    def test_archive_html_has_strict_static_contract(self) -> None:
        parsed = self.parser.parse(
            SOURCE_URL,
            """
            <main><h1>Yoonying</h1>
              <a href="https://sead.pages.dev/xingchen">monthly promotion</a>
              <a href="/tags/yoonying">yoonying</a>
            </main>
            <script src="https://js.wpadmngr.com/static/adManager.js"></script>
            """,
        )
        archive = render_archive_html(parsed)

        self.assertTrue(is_safe_archive_html(archive))
        self.assertIn(ARCHIVE_MARKER, archive)
        self.assertIn("Yoonying", archive)
        self.assertIn("yoonying", archive)
        self.assertNotIn("sead.pages.dev", archive)
        self.assertNotIn("monthly promotion", archive)
        self.assertNotIn("wpadmngr", archive)
        tampered = archive.replace("</body>", "<script></script></body>")
        self.assertFalse(is_safe_archive_html(tampered))
        self.assertFalse(
            is_safe_archive_html(archive.replace("<main>", '<main onload="alert(1)">'))
        )
        self.assertFalse(
            is_safe_archive_html(archive.replace("<main>", '<main><img src="https://ad.test/x">'))
        )

    def test_captured_page_regression_removes_ads_without_losing_content(self) -> None:
        parsed = self.parser.parse(SOURCE_URL, SNAPSHOT.read_text(encoding="utf-8"))
        archive = render_archive_html(parsed)
        lowered = archive.lower()

        self.assertTrue(is_safe_archive_html(archive))
        self.assertIn("Yoonying", parsed.title)
        self.assertIn("yoonying", parsed.tags)
        for forbidden in [
            "downrightfootball",
            "wpadmngr",
            "histats",
            "sead.pages.dev/xingchen",
            "bit.ly/fulise",
            "monthly promotion",
            "promotion 9.9",
        ]:
            self.assertNotIn(forbidden.lower(), lowered)

    def test_parse_site_page_extracts_cards_and_pagination(self) -> None:
        html = """
        <html><body><main id="main" class="pt-4">
          <div class="grid gap-3 grid-cols-2">
            <div>
              <div class="relative">
                <a href="/posts/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">
                  <img alt="First post" src="https://videothumbs.me/one.jpg"/>
                </a>
                <time datetime="2026-07-26">2026-07-26</time>
              </div>
              <div class="my-1">
                <a href="/posts/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">
                  <h2>First post title</h2>
                </a>
              </div>
              <div>
                <a href="/tags/ASMR">ASMR</a>
                <a href="/tags/yoonying">yoonying</a>
              </div>
            </div>
            <div>
              <div class="relative">
                <a href="/posts/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb">
                  <img alt="Second" src="https://videothumbs.me/two.jpg"/>
                </a>
                <time datetime="2026-07-25">2026-07-25</time>
              </div>
              <a href="/posts/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"><h2>Second</h2></a>
              <a href="/tags/Record">Record</a>
            </div>
            <div>
              <a href="https://evil.example/posts/cccccccccccccccccccccccccccccccc">
                <h2>external</h2>
              </a>
            </div>
          </div>
        </main>
        <nav class="relative z-0 inline-flex">
          <a href="https://asmrlib.com?page=2">2</a>
          <a href="https://asmrlib.com?page=3">3</a>
        </nav>
        </body></html>
        """
        parsed = self.parser.parse_site_page("https://asmrlib.com/", html)
        self.assertEqual(parsed.page_number, 1)
        self.assertEqual(len(parsed.posts), 2)
        self.assertEqual(
            parsed.posts[0].source_url,
            "https://asmrlib.com/posts/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        self.assertEqual(parsed.posts[0].title, "First post title")
        self.assertEqual(parsed.posts[0].cover, "https://videothumbs.me/one.jpg")
        self.assertEqual(parsed.posts[0].published_at, "2026-07-26")
        self.assertEqual(parsed.posts[0].tags, ["ASMR", "yoonying"])
        self.assertEqual(parsed.next_page_url, "https://asmrlib.com/?page=2")
        self.assertEqual(parsed.prev_page_url, "")

        page2 = self.parser.parse_site_page(
            "https://asmrlib.com/?page=2",
            html.replace("?page=2", "?page=1").replace("?page=3", "?page=3"),
        )
        # page=2 source should see page=3 as next when both links present;
        # rebuild a minimal page-2 fixture:
        html_p2 = """
        <main id="main"><div class="grid">
          <div><a href="/posts/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"><h2>Only</h2>
            <img src="https://videothumbs.me/one.jpg"/></a>
            <time datetime="2026-07-01">2026-07-01</time>
          </div>
        </div></main>
        <a href="https://asmrlib.com/">1</a>
        <a href="https://asmrlib.com?page=3">3</a>
        """
        page2 = self.parser.parse_site_page("https://asmrlib.com/?page=2", html_p2)
        self.assertEqual(page2.page_number, 2)
        self.assertEqual(page2.prev_page_url, "https://asmrlib.com/")
        self.assertEqual(page2.next_page_url, "https://asmrlib.com/?page=3")
        self.assertEqual(len(page2.posts), 1)


if __name__ == "__main__":
    unittest.main()
