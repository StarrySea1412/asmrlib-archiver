"""解析接缝：DOM 契约也是显式登记的扩展点。

这里用一个 DOM 形态与 asmrlib **完全不同**的合成第二站点来证明三件事：

(a) 登记后，``SiteParser`` 会把该站的 HTML 解析成正确的 ``ParsedPage`` /
    ``ParsedTagPage``，而 ``parser.py`` 一行都不用改。
(b) 同一段 HTML 在默认（只含 asmrlib）登记表下**不会被认领** —— 会响亮失败，
    而不是被 asmrlib 的选择器静默解析成半截结果。
(c) asmrlib 自己的 HTML 仍被 ``AsmrlibSite`` 逐例正确解析（回归）。
"""

from __future__ import annotations

import re
import unittest
from urllib.parse import parse_qs, urlparse, urlsplit, urlunparse, urlunsplit

from bs4 import BeautifulSoup

from asmrlib_archiver.errors import BlockedUrl, GuardError
from asmrlib_archiver.guards import UrlGuard
from asmrlib_archiver.models import ParsedPage, ParsedTagPage
from asmrlib_archiver.parser import AsmrlibParser, ParseError, SiteParser
from asmrlib_archiver.parsing import (
    append_media_candidate,
    canonical_post_url,
    clean_text,
    collect_servers,
    content_root,
    detail_excerpt,
    extract_author,
    extract_cover,
    extract_published_at,
    extract_title,
)
from asmrlib_archiver.sites import AsmrlibSite, SiteRegistry, default_registry

ASMRLIB_POST = "https://asmrlib.com/posts/77badb9b69d7bfbcc2fc3369e2d3b5ae"
EPISODE_POST = "https://episodes.test/w/1001"

# asmrlib 的详情页：媒体在 #players / #downloads，标签是 /tags/<slug>。
ASMRLIB_DETAIL_HTML = """
<html><head><title>fallback</title></head><body><main id="main">
  <h1>Rainy Night</h1><time datetime="2026-05-02"></time>
  <div id="players">
    <button data-server="byse" data-url="https://mirror.episodes.test/embed/#t=1"
            data-type="iframe">BI</button>
  </div>
  <div id="downloads"><a href="/media/ep1.mp3">Audio</a></div>
  <a href="/tags/rain">rain</a>
  <header>Related</header>
  <a href="/tags/unrelated">unrelated</a>
</main></body></html>
"""

# 合成第二站点：媒体在 .episodes li a[data-src] / .mirrors button[data-embed]，
# 正文容器是 #episode，标签在 .topics a[href]，列表在 .card-list。
EPISODE_DETAIL_HTML = """
<html><head>
  <meta name="author" content="Dana">
  <meta property="og:image" content="https://cdn.episodes.test/cover.jpg">
</head><body>
<section id="episode">
  <h1>Rainy Night</h1>
  <time datetime="2026-05-02"></time>
  <ul class="topics"><li><a href="/topic/rain">rain</a></li>
    <li><a href="/topic/sleep">sleep</a></li></ul>
  <ul class="episodes">
    <li><a data-src="/audio/ep1.mp3">Episode 1</a></li>
    <li><a data-src="/audio/ep2.mp3">Episode 2</a></li>
  </ul>
  <div class="mirrors">
    <button data-embed="https://mirror.episodes.test/embed/#t=1">Mirror A</button>
  </div>
</section>
</body></html>
"""

EPISODE_LISTING_HTML = """
<html><body><main id="episode-list"><h1>rain</h1>
  <ul class="card-list">
    <li class="card"><a class="card" href="/w/1001"><h2>First</h2></a></li>
    <li class="card"><a class="card" href="/w/1002"><h2>Second</h2></a></li>
    <li class="card"><a class="card" href="https://evil.example/w/1003"><h2>external</h2></a></li>
  </ul>
</main>
<a rel="next" href="/topic/rain?p=3">next</a>
<a rel="next" href="/topic/rain?p=9">bad jump</a>
</body></html>
"""


class EpisodeSite:
    """第二个视频源：URL 语法是 /w/<数字> 与 /topic/<slug>?p=N，DOM 契约也不同。"""

    name = "episodes"
    domains = ("episodes.test",)

    _POST_PATH_RE = re.compile(r"^/w/([0-9]+)/?$")
    _TOPIC_PATH_RE = re.compile(r"^/topic/([^/]+)/?$")
    _PAGE_QUERY_RE = re.compile(r"^p=([1-9][0-9]*)$")

    # --- URL 语法 ---------------------------------------------------------

    def canonical_post_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.params or parsed.query:
            raise BlockedUrl(f"invalid_post_url_query: {url}")
        match = self._POST_PATH_RE.fullmatch(parsed.path)
        if not match:
            return None
        return urlunparse(
            parsed._replace(path=f"/w/{match.group(1)}", params="", query="", fragment="")
        )

    def canonical_tag_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.params or parsed.query:
            raise BlockedUrl(f"invalid_tag_url_query: {url}")
        match = self._TOPIC_PATH_RE.fullmatch(parsed.path)
        if not match:
            return None
        return urlunparse(
            parsed._replace(path=f"/topic/{match.group(1)}", params="", query="", fragment="")
        )

    def canonical_tag_page_url(self, url: str, tag_url: str) -> str | None:
        parsed = urlparse(url)
        if not parsed.query:
            return tag_url
        match = self._PAGE_QUERY_RE.fullmatch(parsed.query)
        if not match:
            return None
        page_number = int(match.group(1))
        return tag_url if page_number == 1 else f"{tag_url}?p={page_number}"

    def build_post_url(self, post_id: str) -> str:
        return f"https://episodes.test/w/{post_id}"

    def build_tag_url(self, slug: str) -> str:
        return f"https://episodes.test/topic/{slug}"

    # --- DOM 契约 ---------------------------------------------------------

    def parse_post(self, url, html, *, guard, allowed_extensions):
        soup = BeautifulSoup(html, "html.parser")
        content = content_root(soup, ("#episode", "section"))
        media = []
        seen: set[str] = set()
        for node in content.select(".episodes li a[data-src]"):
            append_media_candidate(
                media,
                seen,
                guard,
                url,
                str(node.get("data-src", "")),
                clean_text(node.get_text(" ", strip=True)),
                "download",
            )
        for node in content.select(".mirrors button[data-embed]"):
            append_media_candidate(
                media,
                seen,
                guard,
                url,
                str(node.get("data-embed", "")),
                clean_text(node.get_text(" ", strip=True)),
                "embed",
                preserve_fragment=True,
            )
        title = extract_title(content, soup)
        author = extract_author(soup)
        published_at = extract_published_at(content)
        tags = [
            clean_text(link.get_text(" ", strip=True))
            for link in content.select(".topics a[href]")
        ]
        servers = collect_servers(media)
        return ParsedPage(
            source_url=url,
            title=title,
            author=author,
            published_at=published_at,
            tags=tags,
            servers=servers,
            media=media,
            external_links=[],
            text_excerpt=detail_excerpt(title, author, published_at, tags, servers),
            cover=extract_cover(url, soup, content, guard),
        )

    def parse_listing(self, page_url, html, *, guard, allowed_extensions):
        parts = urlsplit(page_url)
        tag_url = guard.canonical_tag_url(
            urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        )
        source = guard.canonical_tag_page_url(page_url, tag_url)
        soup = BeautifulSoup(html, "html.parser")
        content = content_root(soup, ("#episode-list", "main"))
        post_urls: list[str] = []
        seen: set[str] = set()
        for link in content.select("a.card[href]"):
            candidate = canonical_post_url(guard, source, str(link.get("href", "")))
            if candidate and candidate not in seen:
                seen.add(candidate)
                post_urls.append(candidate)
        next_page_url = ""
        current_page = self._page_number(source)
        for link in soup.select("a[rel='next'][href]"):
            try:
                candidate = guard.canonical_tag_page_url(str(link.get("href", "")), tag_url)
            except GuardError:
                continue
            if self._page_number(candidate) == current_page + 1:
                next_page_url = candidate
                break
        return ParsedTagPage(
            source_url=source,
            title=extract_title(content, soup),
            post_urls=post_urls,
            next_page_url=next_page_url,
            text_excerpt=extract_title(content, soup)[:1200],
        )

    def parse_site_page(self, url, html, *, guard, allowed_extensions):
        # 这个站没有"首页卡片墙"形态：明确表示解析不了。
        return None

    def _page_number(self, url: str) -> int:
        values = parse_qs(urlsplit(url).query).get("p", [])
        return int(values[0]) if len(values) == 1 and values[0].isdecimal() else 1


def both_sites_guard() -> UrlGuard:
    registry = SiteRegistry()
    registry.register(AsmrlibSite())
    registry.register(EpisodeSite())
    return UrlGuard(
        ["asmrlib.com", "episodes.test"],
        allowed_media_domains=["cdn.episodes.test", "mirror.episodes.test"],
        allow_external_media=True,
        registry=registry,
    )


class SyntheticSecondSiteTests(unittest.TestCase):
    """(a) 登记第二个站点后，门面就会把它的 DOM 解析成正确的 ParsedPage。"""

    def setUp(self) -> None:
        self.parser = SiteParser(both_sites_guard(), [".mp3", ".mp4"])

    def test_foreign_dom_is_parsed_into_the_expected_page(self) -> None:
        parsed = self.parser.parse(EPISODE_POST, EPISODE_DETAIL_HTML)

        self.assertEqual(parsed.title, "Rainy Night")
        self.assertEqual(parsed.author, "Dana")
        self.assertEqual(parsed.published_at, "2026-05-02")
        self.assertEqual(parsed.tags, ["rain", "sleep"])
        self.assertEqual(parsed.servers, ["Mirror A"])
        self.assertEqual(parsed.cover, "https://cdn.episodes.test/cover.jpg")

        by_url = {item.media_url: item for item in parsed.media}
        self.assertEqual(
            sorted(by_url),
            [
                "https://episodes.test/audio/ep1.mp3",
                "https://episodes.test/audio/ep2.mp3",
                "https://mirror.episodes.test/embed/#t=1",
            ],
        )
        self.assertEqual(by_url["https://episodes.test/audio/ep1.mp3"].kind, "download")
        self.assertEqual(by_url["https://episodes.test/audio/ep1.mp3"].status, "pending")
        self.assertEqual(by_url["https://mirror.episodes.test/embed/#t=1"].kind, "embed")
        self.assertEqual(by_url["https://mirror.episodes.test/embed/#t=1"].status, "reference")

    def test_foreign_listing_uses_its_own_pagination_syntax(self) -> None:
        parsed = self.parser.parse_tag_page(
            "https://episodes.test/topic/rain?p=2", EPISODE_LISTING_HTML
        )
        self.assertEqual(
            parsed.post_urls,
            ["https://episodes.test/w/1001", "https://episodes.test/w/1002"],
        )
        self.assertEqual(parsed.next_page_url, "https://episodes.test/topic/rain?p=3")

    def test_asmrlib_urls_still_route_to_the_builtin_adapter(self) -> None:
        parsed = self.parser.parse(ASMRLIB_POST, ASMRLIB_DETAIL_HTML)
        self.assertEqual(parsed.title, "Rainy Night")
        self.assertEqual(parsed.tags, ["rain"])
        self.assertEqual(parsed.servers, ["BI"])

    def test_adapter_declining_a_page_shape_fails_loudly(self) -> None:
        # EpisodeSite.parse_site_page 明确返回 None —— 门面必须报错，不能返回空页。
        with self.assertRaises(ParseError) as ctx:
            self.parser.parse_site_page("https://episodes.test/", "<html></html>")
        self.assertIn("no_site_adapter", str(ctx.exception))


class UnregisteredSiteIsNotClaimedTests(unittest.TestCase):
    """(b) DOM 契约与 URL 语法一样是显式登记的：没登记就响亮失败。"""

    def setUp(self) -> None:
        # guard 放行 episodes.test（域白名单），但登记表里只有 asmrlib。
        self.guard = UrlGuard(
            ["asmrlib.com", "episodes.test"],
            allowed_media_domains=["cdn.episodes.test", "mirror.episodes.test"],
            allow_external_media=True,
        )
        self.parser = SiteParser(self.guard, [".mp3", ".mp4"])

    def test_default_registry_holds_only_asmrlib(self) -> None:
        self.assertEqual([adapter.name for adapter in default_registry().all()], ["asmrlib"])

    def test_unregistered_dom_raises_instead_of_returning_an_empty_page(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            self.parser.parse(EPISODE_POST, EPISODE_DETAIL_HTML)
        self.assertIn("no_site_adapter", str(ctx.exception))
        self.assertIn("post_layout_error", str(ctx.exception))

        with self.assertRaises(ParseError) as ctx:
            self.parser.parse_tag_page("https://episodes.test/topic/rain", EPISODE_LISTING_HTML)
        self.assertIn("no_site_adapter", str(ctx.exception))

        with self.assertRaises(ParseError) as ctx:
            self.parser.parse_site_page("https://episodes.test/", "<html></html>")
        self.assertIn("no_site_adapter", str(ctx.exception))

    def test_asmrlib_selectors_find_nothing_on_the_foreign_dom(self) -> None:
        """asmrlib 的 DOM 契约在这段 HTML 上一无所获 —— 这正是必须按登记路由的原因。"""
        parsed = AsmrlibSite().parse_post(
            EPISODE_POST,
            EPISODE_DETAIL_HTML,
            guard=self.guard,
            allowed_extensions=[".mp3", ".mp4"],
        )
        self.assertEqual(parsed.media, [])
        self.assertEqual(parsed.tags, [])


class AsmrlibDomRegressionTests(unittest.TestCase):
    """(c) asmrlib 的 HTML 仍被 AsmrlibSite 逐例正确解析。"""

    def setUp(self) -> None:
        self.guard = UrlGuard(
            ["asmrlib.com"],
            allowed_media_domains=["cdn.episodes.test", "mirror.episodes.test"],
            allow_external_media=True,
        )

    def test_facade_and_adapter_agree_on_the_builtin_site(self) -> None:
        via_facade = SiteParser(self.guard, [".mp3", ".mp4"]).parse(
            ASMRLIB_POST, ASMRLIB_DETAIL_HTML
        )
        via_adapter = AsmrlibSite().parse_post(
            ASMRLIB_POST,
            ASMRLIB_DETAIL_HTML,
            guard=self.guard,
            allowed_extensions=[".mp3", ".mp4"],
        )
        self.assertEqual(via_facade, via_adapter)
        self.assertEqual(via_facade.title, "Rainy Night")
        self.assertEqual(via_facade.tags, ["rain"])
        self.assertEqual(via_facade.servers, ["BI"])
        self.assertEqual(
            [item.media_url for item in via_facade.media],
            ["https://asmrlib.com/media/ep1.mp3", "https://mirror.episodes.test/embed/#t=1"],
        )

    def test_legacy_class_name_still_constructs_the_facade(self) -> None:
        self.assertIs(AsmrlibParser, SiteParser)
        parsed = AsmrlibParser(self.guard, [".mp3"]).parse(ASMRLIB_POST, ASMRLIB_DETAIL_HTML)
        self.assertEqual(parsed.title, "Rainy Night")


if __name__ == "__main__":
    unittest.main()
