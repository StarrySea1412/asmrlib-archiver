from __future__ import annotations

import json
import re
from time import monotonic
from urllib.parse import quote, urlencode, urlparse, urlunsplit

from .components import (
    action_btn,
    action_row,
    button,
    cover_card,
    cover_grid,
    crumb,
    detail_backdrop,
    detail_hero,
    detail_poster,
    empty_state,
    page_header,
    pill,
    pills,
    section_block,
    status_chip,
)
from .util import _h, _tag_slug


class LivePages:
    """Live browse/explore pages (no DB write)."""

    def _explore_tag_slugs(self) -> list[str]:
        """Tag slugs from config.tag_seeds + already-archived tags, de-duplicated."""
        slugs: list[str] = []
        seen: set[str] = set()
        for raw in self.config.tag_seeds:
            slug = _tag_slug(str(raw))
            if slug and slug not in seen:
                seen.add(slug)
                slugs.append(slug)
        for row in self.db.list_tags():
            slug = _tag_slug(row["tag_url"])
            if slug and slug not in seen:
                seen.add(slug)
                slugs.append(slug)
        return slugs

    def _build_tag_page_url(self, slug: str, page: int = 1) -> str:
        base = f"https://asmrlib.com/tags/{quote(slug, safe='')}"
        if page <= 1:
            return base
        return f"{base}?page={page}"

    def _assert_explore_url(self, url: str) -> str:
        """Only allow same-domain /tags/ or /posts/ URLs for live explore."""
        value = (url or "").strip()
        if not value:
            raise ValueError("empty_url")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid_url")
        host = parsed.hostname.lower().rstrip(".")
        allowed = {d.lower().rstrip(".") for d in self.config.allowed_domains}
        if host not in allowed and not any(
            self.config.allow_subdomains and host.endswith(f".{d}") for d in allowed
        ):
            raise ValueError(f"host_not_allowed: {host}")
        path = parsed.path or "/"
        if not (path.startswith("/tags/") or path.startswith("/posts/")):
            raise ValueError("path_not_allowed")
        # Drop fragment; keep query for pagination.
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))

    def _assert_browse_url(self, url: str) -> str:
        """Only allow same-domain homepage URLs: / or /?page=N."""
        value = (url or "").strip()
        if not value:
            raise ValueError("empty_url")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid_url")
        host = parsed.hostname.lower().rstrip(".")
        allowed = {d.lower().rstrip(".") for d in self.config.allowed_domains}
        if host not in allowed and not any(
            self.config.allow_subdomains and host.endswith(f".{d}") for d in allowed
        ):
            raise ValueError(f"host_not_allowed: {host}")
        path = parsed.path or "/"
        if path not in {"/", ""}:
            raise ValueError("path_not_allowed")
        # Keep only page= query if present.
        from urllib.parse import parse_qsl, urlencode

        kept = []
        for key, val in parse_qsl(parsed.query, keep_blank_values=False):
            if key == "page" and str(val).isdigit() and int(val) >= 1:
                kept.append(("page", str(int(val))))
        query = urlencode(kept)
        return urlunsplit((parsed.scheme, parsed.netloc, "/", query, ""))

    def _build_browse_url(self, page: int = 1) -> str:
        domain = (self.config.allowed_domains or ["asmrlib.com"])[0]
        if page <= 1:
            return f"https://{domain}/"
        return f"https://{domain}/?page={page}"

    def _fetch_live(self, url: str):
        """Fetch a live asmrlib page under the same guards as the crawler."""
        from ..guards import UrlGuard
        from ..http_client import SafeHttpClient
        from ..parser import AsmrlibParser
        from ..robots import RobotsPolicy

        guard = UrlGuard(
            allowed_domains=self.config.allowed_domains,
            allow_subdomains=self.config.allow_subdomains,
            allowed_media_domains=self.config.download.allowed_media_domains,
            allow_external_media=self.config.download.allow_external_media,
            ad_keywords=self.config.browser.block_url_keywords,
        )
        parser = AsmrlibParser(guard, self.config.download.allowed_extensions)
        client = SafeHttpClient(guard, self.config.crawler)
        try:
            if self.config.crawler.obey_robots:
                robots = RobotsPolicy(
                    self.config.crawler.user_agent,
                    self.config.crawler.timeout_seconds,
                    self.config.crawler.robots_fail_closed,
                )
                if not robots.can_fetch(url):
                    raise RuntimeError(f"robots_disallow: {url}")
            fetch_result = client.fetch_page(url)
            return parser, fetch_result
        finally:
            client.close()

    @staticmethod
    def _query_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
        return str((query.get(key) or [default])[0]).strip()

    def _live_feed_endpoint(
        self,
        *,
        scope: str,
        page: int,
        raw_url: str = "",
        tag: str = "",
        limit: int | None = None,
    ) -> str:
        params: dict[str, str | int] = {"scope": scope, "page": page}
        if raw_url:
            params["url"] = raw_url
        if tag:
            params["tag"] = tag
        if limit is not None:
            params["limit"] = limit
        return f"/api/live-feed?{urlencode(params)}"

    def _render_live_feed_shell(
        self,
        *,
        title: str,
        endpoint: str,
        subtitle: str,
        back_href: str = "/",
    ) -> str:
        body = [
            crumb(("ASMR 收藏馆", back_href), (title, None)),
            page_header(title, subtitle=subtitle),
            section_block(
                "内容",
                (
                    f"<div class='live-feed' data-live-feed='{_h(endpoint)}'>"
                    "<div data-live-feed-content aria-live='polite' aria-busy='true'></div>"
                    "</div>"
                ),
            ),
        ]
        return self._page(title, body)

    def _render_browse(self, query: dict[str, list[str]]) -> str:
        """Return a non-blocking shell; live cards arrive from /api/live-feed."""
        raw_url = self._query_value(query, "url")
        try:
            page_n = max(1, int(self._query_value(query, "page", "1")))
        except ValueError:
            page_n = 1
        try:
            page_url = self._assert_browse_url(
                raw_url or self._build_browse_url(page_n)
            )
        except ValueError as exc:
            return self._page(
                "站点最新",
                [
                    empty_state(
                        "无法浏览",
                        _h(str(exc)),
                        f"<p>{button('返回发现', href='/browse')}</p>",
                        kicker="LIVE",
                    )
                ],
            )
        endpoint = self._live_feed_endpoint(
            scope="browse", page=page_n, raw_url=page_url
        )
        return self._render_live_feed_shell(
            title="站点最新",
            endpoint=endpoint,
            subtitle="页面已就绪，正在载入 asmrlib 最新内容。",
        )

    # Fresh results are reused for a few minutes so page loads never hammer
    # the remote site.  When the remote fetch fails, the last successful
    # payload is still served (stale) instead of showing a manual retry.
    _LIVE_FEED_TTL_SECONDS = 180.0
    _LIVE_FEED_STALE_MAX_SECONDS = 3600.0

    def _live_feed_cached(self, query: dict[str, list[str]]) -> dict:
        cache: dict[str, tuple[float, dict]] = getattr(
            self, "_live_feed_cache", {}
        )
        if not hasattr(self, "_live_feed_cache"):
            self._live_feed_cache = cache
        key = urlencode(sorted((k, v) for k, values in query.items() for v in values))
        now = monotonic()
        cached = cache.get(key)
        if cached and now - cached[0] < self._LIVE_FEED_TTL_SECONDS:
            return cached[1]
        payload = self._live_feed_payload(query)
        if payload.get("ok"):
            cache[key] = (now, payload)
            return payload
        if cached and now - cached[0] < self._LIVE_FEED_STALE_MAX_SECONDS:
            stale = dict(cached[1])
            stale["stale"] = True
            return stale
        return payload

    def _live_feed_payload(self, query: dict[str, list[str]]) -> dict:
        """Fetch and render a validated live-feed fragment for internal use."""

        scope = self._query_value(query, "scope", "browse").lower()
        raw_url = self._query_value(query, "url")
        slug = self._query_value(query, "tag")
        try:
            page_n = max(1, int(self._query_value(query, "page", "1")))
        except ValueError:
            page_n = 1
        try:
            limit = max(1, min(50, int(self._query_value(query, "limit", "30"))))
        except ValueError:
            limit = 30

        try:
            if scope in {"browse", "home"}:
                page_url = self._assert_browse_url(
                    raw_url or self._build_browse_url(page_n)
                )
                parser, fetch_result = self._fetch_live(page_url)
                parsed = parser.parse_site_page(page_url, fetch_result.text)
                posts = list(parsed.posts or [])[:limit]
                html = self._browse_post_cards(posts)
                current_page = int(parsed.page_number or page_n)

                seed_tags = self._auto_archive_tag_slugs()
                post_urls: list[str] = []
                post_tag_map: dict[str, set[str]] = {}
                for post in posts:
                    url = str(getattr(post, "source_url", "") or "")
                    if not url:
                        continue
                    post_urls.append(url)
                    post_tag_map[url] = {
                        str(tag).strip().lower()
                        for tag in (getattr(post, "tags", None) or [])
                        if str(tag).strip()
                    }
                self._enqueue_live_posts(
                    post_urls,
                    auto_crawl=True,
                    require_tags=seed_tags,
                    post_tags=post_tag_map,
                )
                # The pager fetches these as API endpoints; returning page
                # URLs here made the frontend parse HTML as JSON and show
                # the error panel right after a successful load.
                next_href = self._live_feed_endpoint(
                    scope="browse",
                    page=current_page + 1,
                    raw_url=parsed.next_page_url or "",
                )
                prev_href = (
                    self._live_feed_endpoint(scope="browse", page=current_page - 1)
                    if current_page > 1
                    else ""
                )
            elif scope == "tag":
                if raw_url:
                    page_url = self._assert_explore_url(raw_url)
                    slug = _tag_slug(page_url) or slug
                else:
                    if not re.fullmatch(r"[A-Za-z0-9_\-\.%]+", slug):
                        raise ValueError("invalid_tag_slug")
                    page_url = self._assert_explore_url(
                        self._build_tag_page_url(slug, page_n)
                    )
                parser, fetch_result = self._fetch_live(page_url)
                parsed = parser.parse_tag_page(page_url, fetch_result.text)
                post_urls = list(parsed.post_urls or [])[:limit]
                html = self._explore_post_cards(post_urls)
                current_page = page_n
                seed_tags = self._auto_archive_tag_slugs()
                slug_l = slug.strip().lower()
                if slug_l in seed_tags:
                    self._enqueue_live_posts(
                        post_urls,
                        auto_crawl=True,
                        require_tags=seed_tags,
                        post_tags={url: {slug_l} for url in post_urls},
                    )
                next_href = self._live_feed_endpoint(
                    scope="tag",
                    page=current_page + 1,
                    raw_url=parsed.next_page_url or "",
                    tag=slug,
                )
                prev_href = (
                    self._live_feed_endpoint(
                        scope="tag", page=current_page - 1, tag=slug
                    )
                    if current_page > 1
                    else ""
                )
            else:
                raise ValueError("invalid_scope")
        except Exception as exc:  # noqa: BLE001 - returned as local API state
            return {
                "ok": False,
                "html": "",
                "error": f"{type(exc).__name__}: {exc}",
                "page": page_n,
                "next": "",
                "previous": "",
            }

        return {
            "ok": True,
            "html": html,
            "error": "",
            "page": current_page,
            "count": len(posts) if scope in {"browse", "home"} else len(post_urls),
            "next": next_href,
            "previous": prev_href,
        }

    def _browse_post_cards(self, posts) -> str:
        cards: list[str] = []
        for index, post in enumerate(posts):
            url = post.source_url
            row = self.db.get_item(url)
            title = post.title or (row["title"] if row is not None else url)
            if row is not None and row["status"] in {"archived", "crawled"}:
                href = f"/post/{quote(url, safe='')}"
                badges: list[str | tuple[str, str]] = [("已归档", "ok")]
                cover_url = str(row["cover"] or "") or post.cover
                meta = str(row["published_at"] or post.published_at or row["status"])
            else:
                href = f"/explore/post?u={quote(url, safe='')}"
                badges = ["未归档"]
                cover_url = post.cover
                meta = str(post.published_at or "点开预览 · 系统浏览器打开")
            extra = ""
            if post.tags:
                shown = " · ".join(post.tags[:3])
                extra = f"<span class='cover-tags muted'>{_h(shown)}</span>"
            cards.append(
                cover_card(
                    href=href,
                    title=str(title),
                    meta=meta,
                    cover_url=str(cover_url or ""),
                    plate_css=self._plate_gradient(index),
                    badges=badges,
                    initial=(str(title)[:1] or "?").upper(),
                    extra_meta=extra,
                )
            )
        return cover_grid(cards, empty="这一页没有帖子。")

    def _render_explore(self, query: dict[str, list[str]]) -> str:
        """Render the tag index immediately and tag results as an async shell."""
        slug = self._query_value(query, "tag")
        raw_url = self._query_value(query, "url")
        if not slug and not raw_url:
            return self._render_explore_index(query)
        try:
            page_n = max(1, int(self._query_value(query, "page", "1")))
        except ValueError:
            page_n = 1
        try:
            if raw_url:
                page_url = self._assert_explore_url(raw_url)
                slug = _tag_slug(page_url) or slug
            else:
                if not re.fullmatch(r"[A-Za-z0-9_\-\.%]+", slug):
                    raise ValueError("invalid_tag_slug")
                page_url = self._assert_explore_url(
                    self._build_tag_page_url(slug, page_n)
                )
        except ValueError as exc:
            return self._page(
                "标签发现",
                [
                    empty_state(
                        "无法浏览",
                        _h(str(exc)),
                        f"<p>{button('返回标签', href='/explore')}</p>",
                        kicker="EXPLORE",
                    )
                ],
            )
        endpoint = self._live_feed_endpoint(
            scope="tag", page=page_n, raw_url=page_url, tag=slug
        )
        return self._render_live_feed_shell(
            title=f"#{slug or 'tag'}",
            endpoint=endpoint,
            subtitle="页面已就绪，正在载入标签内容。",
            back_href="/explore",
        )

    def _render_explore_index(self, query: dict[str, list[str]]) -> str:
        slugs = self._explore_tag_slugs()
        body = [
            crumb(("收藏馆", "/"), ("发现", "/browse"), ("按标签", None)),
            page_header(
                "按标签发现",
                subtitle=(
                    "自动归档只跟 config.tag_seeds 走（如 #yoonying）；"
                    "其它标签可预览但不入库。"
                ),
                trailing=button("最新投稿", href="/browse", secondary=True, size="sm"),
            ),
            "<div class='explore-grid'>"
            "<a class='explore-card explore-card-home' href='/browse'>"
            "<span class='explore-kicker'>SITE</span>"
            "<strong>asmrlib 首页</strong>"
            "<span class='muted'>最新投稿 · 仅 seed 标签入库</span></a>",
        ]
        if slugs:
            for s in slugs:
                body.append(
                    f"<a class='explore-card' href='/explore?tag={quote(s)}'>"
                    f"<span class='explore-kicker'>TAG</span>"
                    f"<strong>#{_h(s)}</strong>"
                    f"<span class='muted'>开始浏览</span></a>"
                )
        body.append("</div>")
        if not slugs:
            body.append(
                empty_state(
                    "还没有标签",
                    "配置 <code>tag_seeds</code> 或先归档一些帖子，标签会自动出现。",
                    f"<p>{button('去站点预览', href='/browse')}</p>",
                    kicker="EXPLORE",
                )
            )
        return self._page("标签浏览", body)

    def _explore_post_cards(self, post_urls: list[str]) -> str:
        cards: list[str] = []
        for index, url in enumerate(post_urls):
            row = self.db.get_item(url)
            if row is not None:
                title = row["title"] or url
                href = f"/post/{quote(url, safe='')}"
                cover_url = str(row["cover"] or "")
                badges: list[str | tuple[str, str]] = [("已归档", "ok")]
                meta = str(row["published_at"] or row["status"])
                initial = (str(title)[:1] or "?").upper()
            else:
                title = url.rsplit("/", 1)[-1][:16] + "…"
                href = f"/explore/post?u={quote(url, safe='')}"
                cover_url = ""
                badges = ["未归档"]
                meta = "点开只读预览"
                initial = "?"
            cards.append(
                cover_card(
                    href=href,
                    title=str(title),
                    meta=meta,
                    cover_url=cover_url,
                    plate_css=self._plate_gradient(index),
                    badges=badges,
                    initial=initial,
                )
            )
        return cover_grid(cards, empty="这一页没有帖子。")

    def _render_explore_post(self, query: dict[str, list[str]]) -> str:
        raw = (query.get("u") or [""])[0].strip()
        try:
            source_url = self._assert_explore_url(raw)
            if "/posts/" not in urlparse(source_url).path:
                raise ValueError("not_a_post_url")
        except ValueError as exc:
            return self._page(
                "预览",
                [
                    empty_state(
                        "无法预览",
                        _h(str(exc)),
                        f"<p>{button('返回', href='/explore')}</p>",
                        kicker="PREVIEW",
                    )
                ],
            )

        # Prefer local archive if already present.
        existing = self.db.get_item(source_url)
        if existing is not None and existing["status"] in {"archived", "crawled"}:
            # Redirect-style: just render the local detail.
            return self._render_detail(source_url)

        try:
            parser, fetch_result = self._fetch_live(source_url)
            parsed = parser.parse(source_url, fetch_result.text)
        except Exception as exc:  # noqa: BLE001
            return self._page(
                "预览",
                [
                    empty_state(
                        "抓取失败",
                        f"{_h(type(exc).__name__)}: {_h(exc)}<br>"
                        f"<code>{_h(source_url)}</code>",
                        f"<p>{button('返回', href='/explore')}</p>",
                        kicker="PREVIEW",
                    )
                ],
            )

        title = parsed.title or source_url
        if parsed.cover:
            poster_inner = (
                f"<div class='detail-poster-img' "
                f"style='background-image: url({_h(parsed.cover)})'></div>"
            )
            backdrop = detail_backdrop(
                f"background-image: url({_h(parsed.cover)})"
            )
        else:
            plate_grad = self._plate_gradient(1)
            poster_inner = (
                f"<div class='detail-poster-plate' "
                f"style='background: {plate_grad}'>"
                f"<span class='detail-poster-initial'>"
                f"{_h((title[:1] or '?').upper())}</span></div>"
            )
            backdrop = detail_backdrop(f"background: {plate_grad}")

        meta_bits: list[str] = [pill("状态", "只读预览", status=True)]
        if parsed.author:
            meta_bits.append(pill("作者", str(parsed.author)))
        if parsed.published_at:
            meta_bits.append(pill("发布", str(parsed.published_at)))

        tags_html = ""
        if parsed.tags:
            tag_chips = ["<div class='chips detail-tags'>"]
            for tag in parsed.tags:
                tag_chips.append(
                    f"<a class='chip' href='/explore?tag={quote(tag)}'>{_h(tag)}</a>"
                )
            tag_chips.append("</div>")
            tags_html = "".join(tag_chips)

        body = [
            crumb(("收藏馆", "/"), ("实时浏览", "/explore"), ("预览", None)),
            detail_hero(
                backdrop=backdrop,
                poster=detail_poster(poster_inner),
                kicker="实时预览 · 不入库",
                title=str(title),
                meta_html=pills(meta_bits),
                actions_html=action_row(
                    [
                        # Direct sandboxed play: the desktop bridge resolves
                        # the post page to its bare player URL, so no
                        # landing-page hop is needed.
                        action_btn(
                            "安全播放",
                            variant="online",
                            onclick=f"return openDesktopOnline({json.dumps(source_url)})",
                        )
                    ],
                    hero=True,
                ),
                has_cover=bool(parsed.cover),
                extra_main=tags_html,
            ),
            section_block(
                "播放",
                "",
                trailing=status_chip("未归档"),
                extra_class="action-panel",
                hint=(
                    "桌面版“安全播放”会解析出裸播放器页并在受控窗口中打开"
                    "（只显示视频，拦截广告/弹窗）。"
                ),
            ),
        ]

        if parsed.media:
            cards: list[str] = []
            for media in parsed.media:
                host = urlparse(media.media_url).netloc or "player"
                cards.append(
                    "<article class='ref-card'>"
                    "<div class='ref-card-main'>"
                    f"<strong class='ref-label'>{_h(media.label or media.kind)}</strong>"
                    f"<span class='ref-host'>{_h(host)}</span>"
                    f"<span class='muted ref-meta'>{_h(media.kind)} · "
                    f"{_h(media.status)}</span>"
                    "</div>"
                    "<div class='ref-card-actions'>"
                    + action_btn(
                        "安全播放",
                        variant="online",
                        small=True,
                        onclick=f"return openDesktopOnline({json.dumps(media.media_url)})",
                        icon="",
                    )
                    + "</div></article>"
                )
            body.append(
                section_block(
                    "在线来源",
                    f"<div class='ref-stack'>{''.join(cards)}</div>",
                    trailing=(
                        f"<span class='muted section-count'>{len(parsed.media)}</span>"
                    ),
                )
            )

        return self._page(title, body)

    # --------------------------------------------------------------- cards

