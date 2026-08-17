from __future__ import annotations

import re
from collections.abc import Iterator
from urllib.parse import parse_qs, urljoin, urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from .guards import GuardError, UrlGuard
from .models import MediaCandidate, ParsedPage, ParsedSitePage, ParsedTagPage, SitePostCard


MEDIA_EXTENSIONS = (
    ".mp4",
    ".m4v",
    ".mkv",
    ".webm",
    ".mov",
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".flac",
    ".ogg",
    ".m3u8",
)

SECTION_BOUNDARIES = {"related", "tags"}
MEDIA_DATA_TYPES = {"audio", "file", "hls", "iframe", "media", "video"}


class AsmrlibParser:
    def __init__(self, guard: UrlGuard, allowed_extensions: list[str]) -> None:
        self.guard = guard
        configured = tuple(ext.lower() for ext in allowed_extensions)
        self.allowed_extensions = configured or MEDIA_EXTENSIONS

    def parse(self, source_url: str, html: str) -> ParsedPage:
        soup = BeautifulSoup(html, "html.parser")
        content = self._content_root(soup)
        title = self._title(content, soup)
        tags = self._tags(source_url, content)
        media = self._media_candidates(source_url, content)
        servers = self._servers(media)
        published_at = self._published_at(content)
        author = self._author(soup)
        cover = self._cover(source_url, soup, content)
        text_excerpt = self._detail_excerpt(title, author, published_at, tags, servers)
        return ParsedPage(
            source_url=source_url,
            title=title,
            author=author,
            published_at=published_at,
            tags=tags,
            servers=servers,
            media=media,
            external_links=[],
            text_excerpt=text_excerpt,
            cover=cover,
        )

    def parse_tag_page(self, source_url: str, html: str) -> ParsedTagPage:
        source_parts = urlsplit(source_url)
        tag_url = urlunsplit(
            (source_parts.scheme, source_parts.netloc, source_parts.path, "", "")
        )
        tag_url = self.guard.canonical_tag_url(tag_url)
        source = self.guard.canonical_tag_page_url(source_url, tag_url)

        soup = BeautifulSoup(html, "html.parser")
        content = self._content_root(soup)
        title = self._title(content, soup)
        post_urls: list[str] = []
        seen_posts: set[str] = set()
        for link in content.select("a[href]"):
            candidate = self._canonical_post_url(source, str(link.get("href", "")))
            if candidate and candidate not in seen_posts:
                seen_posts.add(candidate)
                post_urls.append(candidate)

        next_page_url = self._next_page_url(tag_url, source, soup)

        return ParsedTagPage(
            source_url=source,
            title=title,
            post_urls=post_urls,
            next_page_url=next_page_url,
            text_excerpt=title[:1200],
        )

    def parse_site_page(self, source_url: str, html: str) -> ParsedSitePage:
        """Parse asmrlib.com homepage / ?page=N listing grid.

        Extracts post cards (url, title, cover, date, tags) plus sequential
        next/prev page links. Used by the live /browse viewer only — does not
        write to the archive DB.
        """
        source = self._absolute_http_url(source_url, source_url, False) or source_url
        soup = BeautifulSoup(html, "html.parser")
        content = self._content_root(soup)
        title = self._title(content, soup) or "ASMRLIB"
        posts = self._site_post_cards(source, content)
        page_number = self._page_number(source)
        next_page_url, prev_page_url = self._site_pagination(source, soup, page_number)
        return ParsedSitePage(
            source_url=source,
            title=title,
            posts=posts,
            next_page_url=next_page_url,
            prev_page_url=prev_page_url,
            page_number=page_number,
        )

    def _site_post_cards(self, source_url: str, content: Tag) -> list[SitePostCard]:
        cards: list[SitePostCard] = []
        seen: set[str] = set()
        # Homepage uses a CSS grid of bare <div> cards; fall back to walking
        # every post link if the grid is missing (layout change).
        roots: list[Tag] = []
        grid = content.select_one(".grid")
        if grid is not None:
            roots = [child for child in grid.find_all(recursive=False) if isinstance(child, Tag)]
        if not roots:
            # One synthetic root per unique post link's nearest block ancestor.
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
                candidate = self._canonical_post_url(source_url, href)
                if candidate:
                    if not post_url:
                        post_url = candidate
                    # Prefer an explicit heading over img alt / bare link text.
                    heading = link.select_one("h1, h2, h3")
                    if heading:
                        title = self._clean_text(heading.get_text(" ", strip=True))
                    elif not title:
                        alt_title = self._clean_text(link.get_text(" ", strip=True))
                        if alt_title:
                            title = alt_title
                try:
                    tag_url = self.guard.canonical_tag_url(href, source_url)
                except GuardError:
                    tag_url = ""
                if tag_url and self._same_origin(source_url, tag_url):
                    label = self._clean_text(link.get_text(" ", strip=True))
                    if label and label not in tags:
                        tags.append(label)

            if not title:
                heading = root.select_one("h1, h2, h3")
                if heading is not None:
                    title = self._clean_text(heading.get_text(" ", strip=True))

            img = root.select_one("img[src], img[data-src]")
            if img is not None:
                raw = str(img.get("src") or img.get("data-src") or "").strip()
                if raw:
                    cover = self._absolute_http_url(source_url, raw, False) or ""
                    if not title:
                        title = self._clean_text(str(img.get("alt", "")))

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
                    cover=cover if cover and not self.guard.is_ad_url(cover) else "",
                    published_at=published_at,
                    tags=tags,
                )
            )
        return cards

    def _site_pagination(
        self, source_url: str, soup: BeautifulSoup, current_page: int
    ) -> tuple[str, str]:
        """Return (next_page_url, prev_page_url) for homepage ?page=N links."""
        next_url = ""
        prev_url = ""
        for link in soup.select("a[href]"):
            raw = str(link.get("href", "")).strip()
            if not raw:
                continue
            absolute = self._absolute_http_url(source_url, raw, False)
            if not absolute or not self._same_origin(source_url, absolute):
                continue
            parts = urlsplit(absolute)
            # Homepage only: path is / or empty, query carries page=.
            if (parts.path or "/") not in {"/", ""}:
                continue
            page_n = self._page_number(absolute)
            # Normalize to bare https://asmrlib.com/ or ?page=N
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

    def _content_root(self, soup: BeautifulSoup) -> Tag:
        for selector in ("#main", "main", "article"):
            node = soup.select_one(selector)
            if node:
                return node
        return soup.body or soup

    def _title(self, content: Tag, soup: BeautifulSoup) -> str:
        heading = content.select_one("h1")
        if heading:
            value = heading.get_text(" ", strip=True)
            if value:
                return self._clean_text(value)
        for selector in ["h1", "meta[property='og:title']", "title"]:
            node = soup.select_one(selector)
            if not node:
                continue
            if node.name == "meta":
                value = node.get("content", "")
            else:
                value = node.get_text(" ", strip=True)
            if value:
                return self._clean_text(value)
        return ""

    def _author(self, soup: BeautifulSoup) -> str:
        for selector in [
            "meta[name='author']",
            "meta[property='article:author']",
            "[rel='author']",
        ]:
            node = soup.select_one(selector)
            if not node:
                continue
            value = (
                node.get("content", "")
                if node.name == "meta"
                else node.get_text(" ", strip=True)
            )
            if value:
                return value.strip()
        return ""

    def _published_at(self, content: Tag) -> str:
        for selector in [
            "time[datetime]",
            "meta[property='article:published_time']",
            "meta[name='date']",
        ]:
            node = content.select_one(selector)
            if not node:
                continue
            value = node.get("datetime") or node.get("content") or node.get_text(" ", strip=True)
            if value:
                return value.strip()
        text = content.get_text("\n", strip=True)
        match = re.search(r"\b(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})\b", text)
        return match.group(1).replace("/", "-").replace(".", "-") if match else ""

    def _cover(self, source_url: str, soup: BeautifulSoup, content: Tag) -> str:
        """Pick a cover image URL from og:image / JSON-LD / first in-content img.

        Priority: og:image meta, then JSON-LD VideoObject.thumbnailUrl (this is
        the reliable per-post cover on asmrlib, where the first in-content <img>
        is often a *related-post* thumbnail), then first <img> inside main
        content as a fallback.

        Only accept URLs that pass the media guard (same domain or whitelisted
        image hosts, never ad hosts). Keeps the archive ad-free and local-only.
        """
        candidates: list[str] = []
        for selector in [
            "meta[property='og:image']",
            "meta[property='og:image:url']",
            "meta[property='twitter:image']",
            "meta[name='twitter:image']",
        ]:
            node = soup.select_one(selector)
            if node:
                value = str(node.get("content", "")).strip()
                if value:
                    candidates.append(value)

        # JSON-LD VideoObject thumbnailUrl — per-post cover on asmrlib.
        for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = node.string
            if not raw or isinstance(raw, list):
                continue
            value = self._jsonld_thumbnail(raw)
            if value:
                candidates.append(value)

        # First <img> inside main content as a fallback.
        img = content.select_one("img[src]")
        if img:
            value = str(img.get("src", "")).strip()
            if value:
                candidates.append(value)

        for raw in candidates:
            normalized = self._absolute_http_url(source_url, raw, preserve_fragment=False)
            if not normalized or self.guard.is_ad_url(normalized):
                continue
            decision = self.guard.media_decision(normalized, source_url)
            # media_decision allows same-domain always, plus whitelisted image
            # hosts only when allow_external_media is set. Accept either path so
            # a post on asmrlib plus an img-place.com poster both qualify.
            if decision.allowed or self._image_host_allowed(normalized):
                if self._looks_like_image(normalized) or normalized:
                    return normalized
        return ""

    @staticmethod
    def _jsonld_thumbnail(raw: str) -> str:
        """Extract thumbnailUrl from an application/ld+json node, or ''."""
        # Regex fallback is safer than full JSON parsing: page JSON is
        # machine-generated, but a broken script tag elsewhere must not
        # cause the cover to be skipped.
        match = re.search(
            r'"thumbnailUrl"\s*:\s*"([^"]+)"',
            raw,
            re.IGNORECASE,
        )
        if match:
            return match.group(1)
        return ""

    def _image_host_allowed(self, url: str) -> bool:
        """Same-registrable-ish image hosts used for posters/thumbnails."""
        image_hosts = {
            "img-place.com",
            "videothumbs.me",
            "i0.wp.com",
            "i1.wp.com",
            "i2.wp.com",
        }
        try:
            host = self._norm_host(urlparse(url).hostname or "")
        except (UnicodeError, ValueError):
            return False
        return any(host == h or host.endswith(f".{h}") for h in image_hosts)

    def _looks_like_image(self, url: str) -> bool:
        path = urlsplit(url).path.lower()
        return path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"))

    @staticmethod
    def _norm_host(host: str) -> str:
        return host.strip().lower().rstrip(".")

    def _tags(self, source_url: str, content: Tag) -> list[str]:
        tags: list[str] = []
        for node in content.descendants:
            if not isinstance(node, Tag):
                continue
            if self._is_section_boundary(node):
                break
            if node.name != "a" or not node.get("href"):
                continue
            try:
                candidate = self.guard.canonical_tag_url(str(node["href"]), source_url)
            except GuardError:
                continue
            if not self._same_origin(source_url, candidate):
                continue
            value = self._clean_text(node.get_text(" ", strip=True))
            if value and value not in tags:
                tags.append(value)
        return tags

    def _servers(self, media: list[MediaCandidate]) -> list[str]:
        servers: list[str] = []
        for candidate in media:
            if candidate.kind != "embed":
                continue
            label = candidate.label.strip()
            if label and label not in servers:
                servers.append(label[:80])
        return servers

    def _media_candidates(self, source_url: str, content: Tag) -> list[MediaCandidate]:
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
                if not raw_url or (node.name == "a" and not self._looks_like_media(raw_url)):
                    continue
                label = node.get_text(" ", strip=True) or str(node.get("title", "")) or kind
                self._append_media_candidate(
                    candidates, seen, source_url, raw_url, self._clean_text(label), kind
                )

        for node in content.select("#players [data-url], #downloads [data-url]"):
            raw_url = str(node.get("data-url", ""))
            data_type = str(node.get("data-type", "")).lower().strip()
            if not raw_url or (
                data_type not in MEDIA_DATA_TYPES and not self._looks_like_media(raw_url)
            ):
                continue
            kind = "embed" if data_type == "iframe" else (data_type or "media")
            label = self._clean_text(node.get_text(" ", strip=True))
            if not label:
                label = self._clean_text(str(node.get("data-server", ""))) or kind
            self._append_media_candidate(
                candidates,
                seen,
                source_url,
                raw_url,
                label,
                kind,
                preserve_fragment=kind == "embed",
            )
        return candidates

    def _append_media_candidate(
        self,
        candidates: list[MediaCandidate],
        seen: set[str],
        source_url: str,
        raw_url: str,
        label: str,
        kind: str,
        *,
        preserve_fragment: bool = False,
    ) -> None:
        normalized = self._absolute_http_url(source_url, raw_url, preserve_fragment)
        if not normalized or normalized in seen:
            return
        if urlsplit(normalized).path.lower().endswith(".m3u8"):
            return
        if self.guard.is_ad_url(normalized):
            return

        decision = self.guard.media_decision(raw_url, source_url)
        # Third-party iframe players are never direct downloads. Keep them as
        # local library references so posts still show servers and player hosts.
        if kind == "embed":
            status = "reference"
        elif decision.allowed:
            status = "pending"
        else:
            return

        seen.add(normalized)
        candidates.append(
            MediaCandidate(
                source_url=source_url,
                media_url=normalized,
                label=label[:200],
                kind=kind,
                status=status,
                error="" if status == "pending" else "player_reference_not_downloaded",
            )
        )

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

    def _canonical_post_url(self, source_url: str, raw_url: str) -> str | None:
        try:
            candidate = self.guard.canonical_post_url(raw_url, source_url)
        except GuardError:
            return None
        if not self._same_origin(source_url, candidate):
            return None
        return candidate

    def _canonical_pagination_url(
        self,
        tag_url: str,
        source_url: str,
        raw_url: str,
    ) -> str | None:
        try:
            candidate = self.guard.canonical_tag_page_url(raw_url, tag_url)
        except GuardError:
            return None
        if not self._same_origin(source_url, candidate):
            return None
        return candidate

    def _next_page_url(self, tag_url: str, source_url: str, soup: BeautifulSoup) -> str:
        current_page = self._page_number(source_url)
        rel_next: list[tuple[int, str]] = []
        pagination: list[tuple[int, str]] = []
        for link in self._pagination_links(soup):
            candidate = self._canonical_pagination_url(
                tag_url, source_url, str(link.get("href", ""))
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

    def _absolute_http_url(
        self,
        base_url: str,
        raw_url: str,
        preserve_fragment: bool,
    ) -> str | None:
        try:
            parts = urlsplit(urljoin(base_url, raw_url.strip()))
            if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
                return None
            if parts.username or parts.password:
                return None
            port = parts.port
        except (TypeError, ValueError):
            return None

        scheme = parts.scheme.lower()
        host = parts.hostname.lower().rstrip(".")
        default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        netloc = host if port is None or default_port else f"{host}:{port}"
        fragment = parts.fragment if preserve_fragment else ""
        return urlunsplit((scheme, netloc, parts.path or "/", parts.query, fragment))

    def _same_origin(self, left: str, right: str) -> bool:
        left_url = self._absolute_http_url(left, left, False)
        right_url = self._absolute_http_url(right, right, False)
        if not left_url or not right_url:
            return False
        left_parts = urlsplit(left_url)
        right_parts = urlsplit(right_url)
        return (left_parts.scheme, left_parts.netloc) == (right_parts.scheme, right_parts.netloc)

    def _is_section_boundary(self, node: Tag) -> bool:
        if node.name not in {"h2", "h3", "h4", "header"}:
            return False
        label = self._clean_text(node.get_text(" ", strip=True)).lower().rstrip(":")
        return label in SECTION_BOUNDARIES

    def _detail_excerpt(
        self,
        title: str,
        author: str,
        published_at: str,
        tags: list[str],
        servers: list[str],
    ) -> str:
        values = [title, author, published_at]
        values.extend(tags)
        values.extend(servers)
        return "\n".join(value for value in values if value)[:1200]

    def _clean_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    def _looks_like_media(self, url: str) -> bool:
        parsed_path = urlsplit(url).path.lower()
        return parsed_path.endswith(self.allowed_extensions)
