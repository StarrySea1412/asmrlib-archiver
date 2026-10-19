"""asmrlib.com 的站点契约：URL 语法 + DOM 解析。

这个类逐条复刻 ``UrlGuard`` / ``AsmrlibParser`` 改造前内联的站点知识：

- **URL 语法**：32 位十六进制帖子 ID、``/tags/<slug>`` 标签路径、``?page=N``
  分页、第 1 页折叠、18 位页码上限、``quote(slug, safe='-._~')`` 转义、
  post_id 小写化、params/query 非空即非法。
- **DOM 契约**：``#players`` / ``#downloads`` 里的媒体候选与 ``data-server`` /
  ``data-type`` 约定、``Related`` / ``Tags`` 小节边界、首页 ``.grid`` 卡片墙、
  首页 ``?page=N`` 分页。

站点无关的读取规则（标题/作者/时间/封面降级链、媒体准入判定）在
``parsing.py``，这里只放选择器与站点语法。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from urllib.parse import parse_qs, quote, urlparse, urlsplit, urlunparse, urlunsplit

from bs4 import BeautifulSoup, Tag

from ..errors import BlockedUrl, GuardError
from ..models import MediaCandidate, ParsedPage, ParsedSitePage, ParsedTagPage, SitePostCard
from ..parsing import (
    absolute_http_url,
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
    looks_like_media,
    resolve_extensions,
    same_origin,
)
from .base import canonical_path_segment

_TAG_PATH_RE = re.compile(r"^/tags/([^/]+)/?$")
_POST_PATH_RE = re.compile(r"^/posts/([0-9a-fA-F]{32})/?$")
_TAG_PAGE_QUERY_RE = re.compile(r"^page=([1-9][0-9]*)$")
_MAX_PAGE_DIGITS = 18

# 正文里出现这些小节标题就停止收集标签：后面是"相关帖子"之类，不是本帖标签。
SECTION_BOUNDARIES = {"related", "tags"}

# ``data-type`` 里这些取值表示"这是一个媒体/播放器引用"。
MEDIA_DATA_TYPES = {"audio", "file", "hls", "iframe", "media", "video"}


class AsmrlibSite:
    name = "asmrlib"
    domains = ("asmrlib.com",)

    # --- URL 语法 ---------------------------------------------------------

    def canonical_post_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.params or parsed.query:
            raise BlockedUrl(f"invalid_post_url_query: {url}")
        match = _POST_PATH_RE.fullmatch(parsed.path)
        if not match:
            return None
        post_id = match.group(1).lower()
        return urlunparse(
            parsed._replace(path=f"/posts/{post_id}", params="", query="", fragment="")
        )

    def canonical_tag_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.params or parsed.query:
            raise BlockedUrl(f"invalid_tag_url_query: {url}")
        match = _TAG_PATH_RE.fullmatch(parsed.path)
        if not match:
            return None
        slug = canonical_path_segment(match.group(1), "tag slug")
        path = f"/tags/{quote(slug, safe='-._~')}"
        return urlunparse(parsed._replace(path=path, params="", query="", fragment=""))

    def canonical_tag_page_url(self, url: str, tag_url: str) -> str | None:
        parsed = urlparse(url)
        if not parsed.query:
            return tag_url
        match = _TAG_PAGE_QUERY_RE.fullmatch(parsed.query)
        if not match:
            return None
        page_text = match.group(1)
        if len(page_text) > _MAX_PAGE_DIGITS:
            return None
        page_number = int(page_text)
        if page_number == 1:
            return tag_url
        return f"{tag_url}?page={page_number}"

    def build_post_url(self, post_id: str) -> str:
        return f"{self._base_url()}/posts/{post_id.lower()}"

    def build_tag_url(self, slug: str) -> str:
        return f"{self._base_url()}/tags/{quote(slug, safe='-._~')}"

    def _base_url(self) -> str:
        return f"https://{self.domains[0]}"

    # --- DOM 契约 ---------------------------------------------------------

    def parse_post(
        self,
        url: str,
        html: str,
        *,
        guard,
        allowed_extensions: Sequence[str],
    ) -> ParsedPage | None:
        extensions = resolve_extensions(allowed_extensions)
        soup = BeautifulSoup(html, "html.parser")
        content = content_root(soup)
        title = extract_title(content, soup)
        tags = self._tags(guard, url, content)
        media = self._media_candidates(guard, extensions, url, content)
        servers = collect_servers(media)
        published_at = extract_published_at(content)
        author = extract_author(soup)
        cover = extract_cover(url, soup, content, guard)
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
            cover=cover,
        )

    def parse_listing(
        self,
        page_url: str,
        html: str,
        *,
        guard,
        allowed_extensions: Sequence[str],
    ) -> ParsedTagPage | None:
        parts = urlsplit(page_url)
        tag_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        tag_url = guard.canonical_tag_url(tag_url)
        source = guard.canonical_tag_page_url(page_url, tag_url)

        soup = BeautifulSoup(html, "html.parser")
        content = content_root(soup)
        title = extract_title(content, soup)
        post_urls: list[str] = []
        seen_posts: set[str] = set()
        for link in content.select("a[href]"):
            candidate = canonical_post_url(guard, source, str(link.get("href", "")))
            if candidate and candidate not in seen_posts:
                seen_posts.add(candidate)
                post_urls.append(candidate)

        next_page_url = self._next_page_url(guard, tag_url, source, soup)

        return ParsedTagPage(
            source_url=source,
            title=title,
            post_urls=post_urls,
            next_page_url=next_page_url,
            text_excerpt=title[:1200],
        )

    def parse_site_page(
        self,
        url: str,
        html: str,
        *,
        guard,
        allowed_extensions: Sequence[str],
    ) -> ParsedSitePage | None:
        """解析 asmrlib.com 首页 / ``?page=N`` 的卡片墙。

        抽出帖子卡片（url、标题、封面、日期、标签）与相邻的上一页/下一页链接。
        只给 live /browse 浏览器用 —— 不写归档库。
        """
        source = absolute_http_url(url, url, False) or url
        soup = BeautifulSoup(html, "html.parser")
        content = content_root(soup)
        title = extract_title(content, soup) or "ASMRLIB"
        posts = self._site_post_cards(guard, source, content)
        page_number = self._page_number(source)
        next_page_url, prev_page_url = self._site_pagination(guard, source, soup, page_number)
        return ParsedSitePage(
            source_url=source,
            title=title,
            posts=posts,
            next_page_url=next_page_url,
            prev_page_url=prev_page_url,
            page_number=page_number,
        )

    # --- 正文标签 ---------------------------------------------------------

    def _tags(self, guard, source_url: str, content: Tag) -> list[str]:
        tags: list[str] = []
        for node in content.descendants:
            if not isinstance(node, Tag):
                continue
            if self._is_section_boundary(node):
                break
            if node.name != "a" or not node.get("href"):
                continue
            try:
                candidate = guard.canonical_tag_url(str(node["href"]), source_url)
            except GuardError:
                continue
            if not same_origin(source_url, candidate):
                continue
            value = clean_text(node.get_text(" ", strip=True))
            if value and value not in tags:
                tags.append(value)
        return tags

    def _is_section_boundary(self, node: Tag) -> bool:
        if node.name not in {"h2", "h3", "h4", "header"}:
            return False
        label = clean_text(node.get_text(" ", strip=True)).lower().rstrip(":")
        return label in SECTION_BOUNDARIES

    # --- 媒体候选 ---------------------------------------------------------

    def _media_candidates(
        self,
        guard,
        allowed_extensions: tuple[str, ...],
        source_url: str,
        content: Tag,
    ) -> list[MediaCandidate]:
        candidates: list[MediaCandidate] = []
        seen: set[str] = set()

        for selector, attr, kind in [
            ("video[src]", "src", "video"),
            ("audio[src]", "src", "audio"),
            ("video source[src]", "src", "source"),
            ("audio source[src]", "src", "source"),
            ("#players a[href]", "href", "link"),
            ("#downloads a[href]", "href", "download"),
        ]:
            for node in content.select(selector):
                raw_url = str(node.get(attr, ""))
                if not raw_url or (
                    node.name == "a" and not looks_like_media(raw_url, allowed_extensions)
                ):
                    continue
                label = node.get_text(" ", strip=True) or str(node.get("title", "")) or kind
                append_media_candidate(
                    candidates, seen, guard, source_url, raw_url, clean_text(label), kind
                )

        for node in content.select("#players [data-url], #downloads [data-url]"):
            raw_url = str(node.get("data-url", ""))
            data_type = str(node.get("data-type", "")).lower().strip()
            if not raw_url or (
                data_type not in MEDIA_DATA_TYPES
                and not looks_like_media(raw_url, allowed_extensions)
            ):
                continue
            kind = "embed" if data_type == "iframe" else (data_type or "media")
            label = clean_text(node.get_text(" ", strip=True))
            if not label:
                label = clean_text(str(node.get("data-server", ""))) or kind
            append_media_candidate(
                candidates,
                seen,
                guard,
                source_url,
                raw_url,
                label,
                kind,
                preserve_fragment=kind == "embed",
            )
        return candidates

    # --- 首页卡片墙 -------------------------------------------------------

    def _site_post_cards(self, guard, source_url: str, content: Tag) -> list[SitePostCard]:
        cards: list[SitePostCard] = []
        seen: set[str] = set()
        # 首页是一堵裸 <div> 的 CSS grid；grid 没了（改版）就回落到遍历所有
        # 帖子链接。
        roots: list[Tag] = []
        grid = content.select_one(".grid")
        if grid is not None:
            roots = [child for child in grid.find_all(recursive=False) if isinstance(child, Tag)]
        if not roots:
            # 每个唯一帖子链接的最近块级祖先算一个合成卡片。
            for link in content.select('a[href*="/posts/"]'):
                block = link
                for parent in link.parents:
                    if not isinstance(parent, Tag):
                        continue
                    if parent.name in {"div", "article", "li", "section"}:
                        block = parent
                        break
                if block not in roots:
                    roots.append(block)

        for root in roots:
            post_url = ""
            title = ""
            cover = ""
            published_at = ""
            tags: list[str] = []
            for link in root.select("a[href]"):
                href = str(link.get("href", ""))
                candidate = canonical_post_url(guard, source_url, href)
                if candidate:
                    if not post_url:
                        post_url = candidate
                    # 显式标题优先于 img alt / 裸链接文字。
                    heading = link.select_one("h1, h2, h3")
                    if heading:
                        title = clean_text(heading.get_text(" ", strip=True))
                    elif not title:
                        alt_title = clean_text(link.get_text(" ", strip=True))
                        if alt_title:
                            title = alt_title
                try:
                    tag_url = guard.canonical_tag_url(href, source_url)
                except GuardError:
                    tag_url = ""
                if tag_url and same_origin(source_url, tag_url):
                    label = clean_text(link.get_text(" ", strip=True))
                    if label and label not in tags:
                        tags.append(label)

            if not title:
                heading = root.select_one("h1, h2, h3")
                if heading is not None:
                    title = clean_text(heading.get_text(" ", strip=True))

            img = root.select_one("img[src], img[data-src]")
            if img is not None:
                raw = str(img.get("src") or img.get("data-src") or "").strip()
                if raw:
                    cover = absolute_http_url(source_url, raw, False) or ""
                    if not title:
                        title = clean_text(str(img.get("alt", "")))

            time_node = root.select_one("time[datetime], time")
            if time_node is not None:
                published_at = str(
                    time_node.get("datetime") or time_node.get_text(" ", strip=True) or ""
                ).strip()

            if not post_url or post_url in seen:
                continue
            seen.add(post_url)
            cards.append(
                SitePostCard(
                    source_url=post_url,
                    title=title or post_url.rsplit("/", 1)[-1],
                    cover=cover if cover and not guard.is_ad_url(cover) else "",
                    published_at=published_at,
                    tags=tags,
                )
            )
        return cards

    def _site_pagination(
        self,
        guard,
        source_url: str,
        soup: BeautifulSoup,
        current_page: int,
    ) -> tuple[str, str]:
        """返回首页 ``?page=N`` 分页的 (next_page_url, prev_page_url)。"""
        next_url = ""
        prev_url = ""
        for link in soup.select("a[href]"):
            raw = str(link.get("href", "")).strip()
            if not raw:
                continue
            absolute = absolute_http_url(source_url, raw, False)
            if not absolute or not same_origin(source_url, absolute):
                continue
            parts = urlsplit(absolute)
            # 只有首页：path 是 / 或空，page= 在查询串里。
            if (parts.path or "/") not in {"/", ""}:
                continue
            page_n = self._page_number(absolute)
            # 归一成裸 https://asmrlib.com/ 或 ?page=N
            if page_n <= 1:
                candidate = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
            else:
                candidate = urlunsplit(
                    (parts.scheme, parts.netloc, "/", f"page={page_n}", "")
                )
            if page_n == current_page + 1 and not next_url:
                next_url = candidate
            elif page_n == current_page - 1 and current_page > 1 and not prev_url:
                prev_url = candidate
        return next_url, prev_url

    # --- 标签分页 ---------------------------------------------------------

    def _pagination_links(self, soup: BeautifulSoup) -> Iterator[Tag]:
        seen: set[int] = set()
        for link in soup.select("a[href]"):
            rel = link.get("rel", [])
            rel_values = [rel] if isinstance(rel, str) else list(rel)
            explicit_next = any(str(value).lower() == "next" for value in rel_values)
            if explicit_next or self._inside_pagination(link):
                identity = id(link)
                if identity not in seen:
                    seen.add(identity)
                    yield link

    def _inside_pagination(self, link: Tag) -> bool:
        for parent in link.parents:
            if not isinstance(parent, Tag):
                continue
            markers = [str(item).lower() for item in parent.get("class", [])]
            markers.append(str(parent.get("id", "")).lower())
            aria_label = str(parent.get("aria-label", "")).lower()
            if any(self._is_pagination_token(marker) for marker in markers):
                return True
            if re.search(r"\bpagination\b", aria_label):
                return True
        return False

    def _is_pagination_token(self, value: str) -> bool:
        return bool(re.search(r"(?:^|[-_])pagination(?:$|[-_])", value))

    def _canonical_pagination_url(
        self,
        guard,
        tag_url: str,
        source_url: str,
        raw_url: str,
    ) -> str | None:
        try:
            candidate = guard.canonical_tag_page_url(raw_url, tag_url)
        except GuardError:
            return None
        if not same_origin(source_url, candidate):
            return None
        return candidate

    def _next_page_url(
        self,
        guard,
        tag_url: str,
        source_url: str,
        soup: BeautifulSoup,
    ) -> str:
        current_page = self._page_number(source_url)
        rel_next: list[tuple[int, str]] = []
        pagination: list[tuple[int, str]] = []
        for link in self._pagination_links(soup):
            candidate = self._canonical_pagination_url(
                guard, tag_url, source_url, str(link.get("href", ""))
            )
            if not candidate:
                continue
            page_number = self._page_number(candidate)
            if page_number != current_page + 1:
                continue
            pair = (page_number, candidate)
            rel = link.get("rel", [])
            rel_values = [rel] if isinstance(rel, str) else list(rel)
            if any(str(value).lower() == "next" for value in rel_values):
                rel_next.append(pair)
            else:
                pagination.append(pair)
        choices = rel_next or pagination
        return min(choices, default=(0, ""), key=lambda item: item[0])[1]

    def _page_number(self, url: str) -> int:
        values = parse_qs(urlsplit(url).query).get("page", [])
        return int(values[0]) if len(values) == 1 and values[0].isdecimal() else 1
