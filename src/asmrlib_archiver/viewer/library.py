from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote, urlparse

from .assets import PAGE_SIZE
from .components import (
    action_btn,
    action_row,
    button,
    chip,
    chips,
    cinema_hero,
    cover_card,
    cover_grid,
    cover_rail,
    crumb,
    detail_backdrop,
    detail_hero,
    detail_poster,
    empty_state,
    meta_strip,
    page_header,
    pill,
    pills,
    section_block,
    status_chip,
)
from .components import (
    pagination as pagination_html,
)
from .preview import VIDEO_PREVIEW_EXTS
from .util import (
    _h,
    _human_size,
    _human_time,
    _player_tag,
    _tag_slug,
)


class LibraryPages:
    """Local archive pages: home, list, author, detail, recordings, cards."""

    def _lazy_cover(self, source_url: str, current_cover: str) -> str:
        """Return a cover URL for a post, lazily resolving from the source site.

        When the post has no stored cover, fetch the live asmrlib page (once,
        cached in-process) and extract the cover URL, then persist it to the DB
        so future renders are instant. Never blocks the 404 path: on any failure
        or a genuinely cover-less page we return '' and remember it.
        """
        if current_cover:
            return current_cover
        if source_url in self._cover_cache:
            return self._cover_cache[source_url]
        try:
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
                    if not robots.can_fetch(source_url):
                        self._cover_cache[source_url] = ""
                        return ""
                fetch_result = client.fetch_page(source_url)
                parsed = parser.parse(source_url, fetch_result.text)
                resolved = parsed.cover
            finally:
                client.close()
            if resolved:
                self.db.update_item_cover(source_url, resolved)
                self._cover_cache[source_url] = resolved
                return resolved
        except Exception:  # noqa: BLE001 - lazy best-effort, never break the page
            self._cover_cache[source_url] = ""
        self._cover_cache[source_url] = ""
        return ""

    def _plate_gradient(self, index: int) -> str:
        """Deterministic gradient placeholder for posts without covers."""
        palettes = [
            "linear-gradient(135deg, #0f2027 0%, #203a43 50%, #2c5364 100%)",
            "linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%)",
            "linear-gradient(135deg, #2c0e37 0%, #4a1942 50%, #89216b 100%)",
            "linear-gradient(135deg, #0d1b2a 0%, #1b2838 50%, #415a77 100%)",
            "linear-gradient(135deg, #1e1b2e 0%, #2d1f3d 50%, #4a2c5e 100%)",
            "linear-gradient(135deg, #141e30 0%, #243b55 50%, #306080 100%)",
        ]
        return palettes[index % len(palettes)]

    @staticmethod
    def _cover_is_usable(value: object) -> bool:
        """Cheap deterministic cover eligibility check for SSR selection.

        Do not perform a network request while rendering the home page.  The
        browser image error handler provides the final fallback if a stored URL
        has gone stale.
        """
        raw = str(value or "").strip()
        if not raw:
            return False
        try:
            parsed = urlparse(raw)
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    # ------------------------------------------------------------------ pages

    def _render_home(self, query: dict[str, list[str]]) -> str:
        counts = self.db.counts()
        tags = self.db.list_tags()
        recent_source = self.db.search_items(limit=24, offset=0)
        playable = self.db.list_playable_media(limit=12)
        total_posts = sum(v for k, v in counts.items() if k.startswith("items."))
        total_local = int(counts.get("media.downloaded", 0) or 0)
        explore_tags = self._explore_tag_slugs()
        seed_slugs = sorted(self._auto_archive_tag_slugs())

        # Pick the newest stored cover without doing a blocking network fetch.
        # If all covers are absent, use the newest item with a plate fallback.
        hero_row = next(
            (row for row in recent_source if self._cover_is_usable(row["cover"])),
            None,
        )
        if hero_row is None:
            # The first 24 rows can all be cover-less in a large archive.  A
            # narrow indexed query finds the newest stored cover without
            # fetching any remote page or widening the card query.
            cover_rows = self.db.conn.execute(
                """
                SELECT * FROM items
                WHERE cover <> ''
                ORDER BY
                  CASE WHEN published_at = '' THEN 1 ELSE 0 END,
                  published_at DESC, updated_at DESC, source_url ASC
                LIMIT 64
                """
            )
            hero_row = next(
                (row for row in cover_rows if self._cover_is_usable(row["cover"])),
                recent_source[0] if recent_source else None,
            )
        hero_source = str(hero_row["source_url"]) if hero_row is not None else ""
        recent = [
            row for row in recent_source
            if str(row["source_url"]) != hero_source
        ][:18]

        # Resolve a local CTA only from files that are still present on disk.
        local_href = ""
        for media in playable:
            src_url = str(media["source_url"] or "")
            if src_url == hero_source and not local_href:
                path = Path(str(media["file_path"] or ""))
                if path.is_file():
                    try:
                        rel = path.resolve().relative_to(self.root)
                    except ValueError:
                        continue
                    local_src = f"/media/{quote(rel.as_posix())}"
                    local_href = (
                        f"/watch-local?src={quote(local_src, safe='')}&title="
                        f"{quote(str(media['label'] or media['kind'] or '本地播放'), safe='')}"
                    )

        hero_href = (
            f"/post/{quote(hero_source, safe='')}" if hero_source else "/browse"
        )
        if local_href:
            hero_action = action_btn("播放本地", variant="local", href=local_href)
        elif hero_source:
            hero_action = action_btn(
                "安全播放",
                variant="online",
                onclick=f"return openDesktopOnline({json.dumps(hero_source)})",
            )
        else:
            hero_action = action_btn("开始发现", variant="online", href="/browse")
        hero_meta_bits: list[str] = []
        if hero_row is not None and hero_row["published_at"]:
            hero_meta_bits.append(str(hero_row["published_at"]))
        if total_posts:
            hero_meta_bits.append(f"{total_posts} 帖收藏")
        if total_local:
            hero_meta_bits.append(f"{total_local} 个本地媒体")
        hero_title = str(hero_row["title"] or "ASMR 收藏馆") if hero_row is not None else "ASMR 收藏馆"
        hero_cover = str(hero_row["cover"] or "") if hero_row is not None else ""
        hero_kicker = "ASMR 收藏馆 · 最新归档" if hero_row is not None else "ASMR 收藏馆"
        if hero_source and not local_href:
            hero_kicker += " · 桌面自动拦截"

        body = [
            cinema_hero(
                title=hero_title,
                href=hero_href,
                cover_url=hero_cover if self._cover_is_usable(hero_cover) else "",
                meta=" · ".join(hero_meta_bits),
                kicker=hero_kicker,
                action_html=hero_action,
                initial=(hero_title[:1] or "A").upper(),
            ),
            self._search_form(
                query.get("q", [""])[0] if query else "",
                compact=True,
            ),
        ]
        # Startup / tag_seeds sync status sits right under the slim header.
        startup_banner = self._startup_sync_banner_html()
        if startup_banner:
            body.append(startup_banner)

        # Content first: what you came for.  Do not leave an empty rail title
        # in an archive that has not been populated yet.  Grids everywhere:
        # horizontal scroll rails cramped the layout and hid most cards.
        if recent:
            body.append(
                section_block(
                    "最近归档",
                    self._post_cards(recent),
                    trailing="<a class='text-link' href='/posts'>全部 →</a>",
                )
            )

        if playable:
            seen: set[str] = set()
            playable_items = []
            for row in playable:
                src = str(row["source_url"])
                if src in seen:
                    continue
                seen.add(src)
                item = self.db.get_item(src)
                if item is not None:
                    playable_items.append(item)
                if len(playable_items) >= 12:
                    break
            if playable_items:
                body.append(
                    section_block(
                        "本地已可播",
                        self._post_cards(playable_items),
                        trailing=status_chip(str(len(playable_items)), ok=True),
                    )
                )

        # Live discovery is deliberately a non-blocking shell.  The shared
        # app script fills this grid from /api/live-feed after the local HTML
        # has painted; an empty successful response removes the section.
        # Grid, not rail: horizontal scrollbars cramped the home layout.
        endpoint_builder = getattr(self, "_live_feed_endpoint", None)
        live_endpoint = (
            endpoint_builder(scope="home", page=1, limit=8)
            if callable(endpoint_builder)
            else "/api/live-feed?scope=home&page=1&limit=8"
        )
        body.append(
            section_block(
                "站点最新",
                (
                    f"<div class='live-feed' data-live-feed='{_h(live_endpoint)}'>"
                    "<div data-live-feed-content aria-live='polite' aria-busy='true'>"
                    "<div class='live-skeleton-grid' aria-hidden='true'>"
                    + "<span class='live-skeleton-card'></span>" * 8
                    + "</div></div>"
                    "<button type='button' class='button button-secondary live-feed-retry' "
                    "data-live-feed-retry hidden>重试</button></div>"
                ),
                trailing="<a class='text-link' href='/browse'>查看全部 →</a>",
            )
        )

        # Discover tags and authors as a compact, horizontal exploration strip.
        discover_bits = [
            chip("asmrlib 最新", href="/browse"),
        ]
        for slug in (seed_slugs + [s for s in explore_tags if s not in seed_slugs])[:8]:
            discover_bits.append(
                chip(f"#{slug}", href=f"/explore?tag={quote(slug)}")
            )
        if tags:
            for row in tags[:6]:
                slug = _tag_slug(row["tag_url"])
                if slug in seed_slugs:
                    continue
                discover_bits.append(
                    chip(
                        slug,
                        href=f"/posts?tag={quote(slug)}",
                        count=int(row["post_count"]),
                    )
                )
        author_rows = self.db.list_authors(limit=8)
        for author_row in author_rows:
            author = str(author_row["author"] or "").strip()
            if author:
                discover_bits.append(
                    chip(
                        f"作者 · {author}",
                        href=f"/author?name={quote(author)}",
                        count=int(author_row["post_count"] or 0),
                    )
                )
        if discover_bits:
            body.append(
                section_block(
                    "探索",
                    f"<div class='cinema-explore-strip' role='region' aria-label='标签与作者探索'>"
                    f"{chips(discover_bits)}</div>",
                    trailing="<a class='text-link' href='/explore'>更多 →</a>",
                )
            )

        return self._page("收藏馆", body)

    def _render_list(self, query: dict[str, list[str]]) -> str:
        q = (query.get("q") or [""])[0].strip()
        tag = (query.get("tag") or [""])[0].strip()
        try:
            page = max(1, int((query.get("page") or ["1"])[0]))
        except ValueError:
            page = 1
        offset = (page - 1) * PAGE_SIZE
        total = self.db.count_items(query=q, tag=tag)
        rows = self.db.search_items(query=q, tag=tag, limit=PAGE_SIZE, offset=offset)
        title_bits = ["帖子"]
        if tag:
            title_bits.append(f"#{tag}")
        if q:
            title_bits.append(f"“{q}”")
        body = [
            page_header(
                " · ".join(title_bits),
                subtitle=f"共 {total} 条 · 本地收藏库",
                trailing=(
                    "<a class='text-link' href='/author'>作者</a>"
                    "<span class='muted'> · </span>"
                    "<a class='text-link' href='/explore'>标签发现</a>"
                ),
            ),
            self._search_form(q, tag),
            self._post_cards(rows),
            self._pagination("/posts", page, total, q=q, tag=tag),
        ]
        return self._page("收藏", body)

    def _render_author(self, query: dict[str, list[str]]) -> str:
        name = (query.get("name") or [""])[0].strip()
        try:
            page = max(1, int((query.get("page") or ["1"])[0]))
        except ValueError:
            page = 1
        if not name:
            authors = self.db.list_authors(limit=50)
            author_chips = []
            for row in authors:
                author = str(row["author"] or "").strip()
                if not author:
                    continue
                author_chips.append(
                    chip(
                        author,
                        href=f"/author?name={quote(author)}",
                        count=int(row["post_count"]),
                    )
                )
            body = [
                page_header("按作者", subtitle="从已归档帖子里汇总的作者。"),
                chips(author_chips)
                if author_chips
                else "<p class='muted empty-inline'>暂无作者。</p>",
            ]
            return self._page("作者", body)

        offset = (page - 1) * PAGE_SIZE
        total = self.db.count_items_by_author(name)
        rows = self.db.list_items_by_author(name, limit=PAGE_SIZE, offset=offset)
        body = [
            crumb(("收藏馆", "/"), ("作者", "/author"), (name, None)),
            page_header(name, subtitle=f"共 {total} 条"),
            self._post_cards(rows),
            self._pagination("/author", page, total, name=name),
        ]
        return self._page(name, body)

    def _render_detail(self, source_url: str) -> str:
        row = self.db.get_item(source_url)
        if row is None:
            return self._page(
                "未找到",
                [
                    empty_state(
                        "未找到该帖子",
                        "可能尚未归档，或链接不正确。",
                        f"<p>{button('返回首页', href='/')}</p>",
                        kicker="ARCHIVE",
                    )
                ],
            )
        media_rows = self.db.list_media_for_item(source_url)
        tag_rows = list(self.db.conn.execute(
                "SELECT tag_url FROM tag_items WHERE source_url = ? ORDER BY tag_url",
                (source_url,),
            ))
        title = row["title"] or source_url

        cover_url = str(row["cover"] or "")
        if not cover_url:
            cover_url = self._lazy_cover(source_url, "")

        if cover_url:
            poster_inner = (
                f"<img class='detail-poster-img' data-cover-img "
                f"src='{_h(cover_url)}' alt='' loading='eager' decoding='async' "
                f"style='background-image: url({_h(cover_url)})'>"
                "<div class='detail-poster-fallback plate-0' "
                f"aria-hidden='true' hidden>{_h((title[:1] or '?').upper())}</div>"
            )
            backdrop = detail_backdrop(
                f"background-image: url({_h(cover_url)})"
            )
        else:
            plate_grad = self._plate_gradient(0)
            initial = (title[:1] or "?").upper()
            poster_inner = (
                f"<div class='detail-poster-plate' "
                f"style='background: {plate_grad}'>"
                f"<span class='detail-poster-initial'>{_h(initial)}</span></div>"
            )
            backdrop = detail_backdrop(f"background: {plate_grad}")

        meta_bits: list[str] = []
        if row["author"]:
            author = str(row["author"])
            meta_bits.append(
                pill("作者", author, href=f"/author?name={quote(author)}")
            )
        if row["published_at"]:
            meta_bits.append(pill("发布", str(row["published_at"])))
        media_total = len(media_rows)
        if media_total:
            meta_bits.append(pill("媒体", str(media_total)))
        meta_bits.append(pill("状态", str(row["status"]), status=True))

        # Build local media and online source metadata separately.  The hero
        # gets one smart action; source switching lives in one selector below.
        local_blocks: list[str] = []
        primary_actions: list[str] = []
        ref_cards: list[str] = []
        has_local = False
        local_primary: tuple[str, str, str] | None = None
        online_candidates: list[tuple[str, str, str]] = []

        for media in media_rows:
            label = str(media["label"] or media["kind"] or "media")
            media_url = str(media["media_url"] or "")
            if media["status"] == "downloaded" and media["file_path"]:
                path = Path(str(media["file_path"]))
                if path.is_file():
                    try:
                        rel = path.resolve().relative_to(self.root)
                    except ValueError:
                        continue
                    src = f"/media/{quote(rel.as_posix())}"
                    player = _player_tag(path.suffix.lower(), src)
                    ext = path.suffix.lower()
                    is_video = ext in VIDEO_PREVIEW_EXTS
                    kind = "audio" if ext in {
                        ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"
                    } else "video"
                    media_id = int(media["id"])
                    local_mini = (
                        f"/watch-local?src={quote(src, safe='')}&title="
                        f"{quote(label, safe='')}&mini=1"
                    )
                    preview_btn = ""
                    preview_panel = ""
                    if is_video:
                        preview_btn = (
                            " · "
                            "<button type='button' class='text-link text-btn' "
                            f"onclick='return loadMediaPreview({media_id}, false)'>"
                            "预览截帧</button>"
                            " · "
                            "<button type='button' class='text-link text-btn' "
                            f"id='preview-refresh-{media_id}' hidden "
                            f"onclick='return loadMediaPreview({media_id}, true)'>"
                            "刷新时间点</button>"
                        )
                        preview_panel = (
                            f"<div class='preview-panel' id='preview-panel-{media_id}' "
                            "hidden>"
                            "<div class='preview-head'>"
                            "<span class='preview-title'>临时预览 · 3 帧</span>"
                            f"<span class='muted preview-meta' "
                            f"id='preview-meta-{media_id}'></span>"
                            "</div>"
                            f"<div class='preview-grid' id='preview-grid-{media_id}'>"
                            "</div>"
                            "<p class='muted preview-note'>"
                            "截图只在当前页面内存里，不会写入本地磁盘；"
                            "刷新页面即消失。点「刷新时间点」会换一组随机位置重新截取。"
                            "</p></div>"
                        )
                    local_blocks.append(
                        f"<article class='media-card' id='media-card-{media_id}'>"
                        f"<header class='media-card-head'>"
                        f"<span class='media-kind'>{kind}</span>"
                        f"<strong>{_h(label)}</strong>"
                        f"<span class='muted media-file'>{_h(path.name)}"
                        f" · {_human_size(path.stat().st_size)}</span>"
                        f"</header>"
                        f"<div class='media-card-player'>{player}</div>"
                        f"{preview_panel}"
                        f"<footer class='media-card-foot'>"
                        f"<a class='text-link' href='{src}'>打开本地文件</a>"
                        " · "
                        "<button type='button' class='text-link text-btn' "
                        f"onclick='return openMiniPlayer({json.dumps(local_mini)})'>"
                        "小窗</button>"
                        f"{preview_btn}"
                        " · "
                        "<button type='button' class='text-link text-btn danger-link' "
                        f"onclick='return deleteDetailMedia({media_id}, "
                        f"{json.dumps(path.name)})'>"
                        "删除</button>"
                        f"</footer></article>"
                    )
                    if local_primary is None:
                        local_primary = (label, src, local_mini)
                    has_local = True
                    continue

            if media_url.startswith(("http://", "https://")):
                host = urlparse(media_url).netloc or "player"
                if not any(candidate[1] == media_url for candidate in online_candidates):
                    online_candidates.append((label, media_url, host))
                # Online refs are intentionally descriptive; one shared action
                # below opens the selected source in the system browser.
                ref_cards.append(
                    "<article class='ref-card'>"
                    "<div class='ref-card-main'>"
                    f"<strong class='ref-label'>{_h(label)}</strong>"
                    f"<span class='ref-host'>{_h(host)}</span>"
                    f"<span class='muted ref-meta'>"
                    f"{_h(media['kind'])} · {_h(media['status'] or '')}</span>"
                    "</div>"
                    "</article>"
                )

        if local_primary is not None:
            label, local_src, local_mini = local_primary
            primary_actions.append(
                action_btn("播放本地", variant="local", href=local_src)
            )
            primary_actions.append(
                action_btn(
                    "小窗",
                    variant="mini",
                    onclick=f"return openMiniPlayer({json.dumps(local_mini)})",
                )
            )
        else:
            primary_actions.append(
                action_btn(
                    "安全播放" if source_url else "查看详情",
                    variant="online" if source_url else "ghost",
                    href=source_url if not source_url else None,
                    onclick=(
                        f"return openDesktopOnline({json.dumps(source_url)})"
                        if source_url else None
                    ),
                )
            )

        # Tags + annotate form live BELOW the hero (meta strip), not inside it —
        # keeps the play actions scannable and the page less noisy.
        strip_parts: list[str] = []
        if tag_rows:
            strip_parts.append("<div class='chips detail-tags'>")
            for tag_row in tag_rows:
                tag_url = str(tag_row["tag_url"])
                tag = _tag_slug(tag_url)
                if tag_url.startswith("user-tag://"):
                    strip_parts.append(
                        "<span class='chip chip-user'>"
                        f"{_h(tag)}"
                        "<button type='button' class='chip-x' "
                        f"onclick='return removeTag({json.dumps(source_url)}, "
                        f"{json.dumps(tag_url)})'>✕</button></span>"
                    )
                else:
                    strip_parts.append(
                        f"<a class='chip' href='/posts?tag={quote(tag)}'>{_h(tag)}</a>"
                    )
            strip_parts.append("</div>")
        strip_parts.append(
            "<div class='tag-add'>"
            "<input id='tagInput' type='text' placeholder='添加自定义标注…' "
            "maxlength='40'>"
            "<button type='button' class='button button-secondary' "
            f"onclick='return addTag({json.dumps(source_url)})'>添加</button>"
            "</div>"
        )

        body = [
            crumb(("收藏馆", "/"), ("帖子", "/posts"), ("详情", None)),
            detail_hero(
                backdrop=backdrop,
                poster=detail_poster(poster_inner),
                kicker="ASMR · 本地归档",
                title=str(title),
                meta_html=pills(meta_bits),
                actions_html=action_row(primary_actions, hero=True),
                has_cover=bool(cover_url),
            ),
            meta_strip("".join(strip_parts)),
        ]

        # One source selector keeps online actions scannable.  The prominent
        # action uses the controlled desktop player (which can intercept
        # popups/ad redirects); a quiet original-link escape hatch remains for
        # users who explicitly want the exact URL in their system browser.
        source_options: list[tuple[str, str]] = []
        if source_url:
            source_options.append(("原始页面", source_url))
        source_options.extend(
            (label, url) for label, url, _host in online_candidates
            if url != source_url
        )
        if source_options:
            option_html = "".join(
                f"<option value='{_h(url)}'>{_h(label)}</option>"
                for label, url in source_options
            )
            first_source = source_options[0][1]
            source_open = action_btn(
                "在线播放",
                variant="online",
                href=first_source,
                onclick="return openDesktopOnline(this.href)",
                target_blank=True,
                extra_class="source-open",
            )
            source_external = (
                f"<a class='text-link source-external' href='{_h(first_source)}' "
                "target='_blank' rel='noopener noreferrer' "
                "onclick='return openDesktopExternal(this.href)'>原链</a>"
            )
            source_controls = (
                "<div class='detail-source-controls'>"
                "<label class='detail-source-label' for='detail-source'>播放来源</label>"
                f"<select id='detail-source' class='detail-source-picker' "
                f"data-source-picker>{option_html}</select>"
                f"{source_open}"
                f"{source_external}"
                "</div>"
                "<script>"
                "(function(){var s=document.querySelector('[data-source-picker]');"
                "var a=document.querySelector('.source-open');"
                "var e=document.querySelector('.source-external');"
                "if(s)s.addEventListener('change',function(){"
                "if(a)a.href=s.value; if(e)e.href=s.value;});})();"
                "</script>"
            )
            body.append(
                section_block(
                    "在线来源",
                    source_controls + f"<div class='ref-stack'>{''.join(ref_cards)}</div>",
                    trailing=status_chip("自动拦截"),
                    extra_class="detail-panel source-panel",
                    hint=(
                        "桌面版“在线播放”会自动拦截弹窗与广告跳转；"
                        "无桌面桥时回退系统浏览器。需要原始地址可点“原链”。"
                    ),
                )
            )
        elif has_local:
            body.append(
                section_block(
                    "播放方式",
                    "<p class='muted action-hint'>本地媒体可直接播放，也可以使用置顶小窗。</p>",
                    trailing=status_chip("本地可播", ok=True),
                    extra_class="detail-panel env-panel",
                )
            )

        if has_local:
            body.append(
                section_block(
                    "本地播放器",
                    f"<div class='media-stack'>{''.join(local_blocks)}</div>",
                    trailing=(
                        f"<span class='muted section-count'>{len(local_blocks)}</span>"
                    ),
                    extra_class="detail-panel",
                )
            )

        related = self.db.list_related_items(source_url, limit=12)
        if related:
            body.append(
                section_block(
                    "相关推荐",
                    self._post_cards(related),
                    trailing=(
                        f"<span class='muted section-count'>{len(related)}</span>"
                    ),
                    extra_class="detail-panel",
                )
            )

        html_path = row["html_path"]
        if html_path and Path(str(html_path)).is_file():
            try:
                rel = Path(str(html_path)).resolve().relative_to(self.root)
                body.append(
                    "<section class='section-block section-foot detail-panel'>"
                    f"<a class='text-link' href='/file/{quote(rel.as_posix())}'>"
                    "打开静态归档 HTML →</a>"
                    "</section>"
                )
            except ValueError:
                pass
        body.append(
            "<script>"
            "function _releaseAllMedia(){"
            "try{var vs=document.querySelectorAll('.media-card-player video,.media-card-player audio');"
            "for(var i=0;i<vs.length;i++){try{vs[i].pause();}catch(e){}"
            "vs[i].removeAttribute('src');try{vs[i].load();}catch(e){}}}catch(e){}}"
            "function deleteDetailMedia(id, name){"
            "var run=function(){_releaseAllMedia();setTimeout(function(){"
            "fetch('/recordings/delete',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({id:id})}).then(function(r){return r.json();}).then(function(d){"
            "if(d&&d.ok){"
            "if(window.__asmrlibToast)window.__asmrlibToast(d.file_removed?'已删除':(d.file_error||'已从库中移除'));"
            "location.reload();}"
            "else{if(window.__asmrlibToast)window.__asmrlibToast('删除失败');else alert('删除失败');}})"
            ".catch(function(){if(window.__asmrlibToast)window.__asmrlibToast('删除失败');});},220);};"
            "if(window.__asmrlibConfirm){"
            "window.__asmrlibConfirm('删除「'+name+'」？该文件会从磁盘移除。',run);return false;}"
            "if(!confirm('删除「'+name+'」？该文件会从磁盘移除。'))return false;"
            "run();return false;}"
            "function addTag(source){"
            "var label=(document.getElementById('tagInput')||{}).value||'';"
            "label=label.trim();if(!label)return false;"
            "fetch('/tags/add',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({source:source,label:label})}).then(function(r){return r.json();}).then(function(d){"
            "if(d&&d.ok){if(window.__asmrlibToast)window.__asmrlibToast('已添加标注');location.reload();}"
            "else{if(window.__asmrlibToast)window.__asmrlibToast('添加失败');}});"
            "return false;}"
            "function removeTag(source, tag){"
            "var fn=function(){"
            "fetch('/tags/remove',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({source:source,tag:tag})}).then(function(r){return r.json();}).then(function(d){"
            "if(d&&d.ok){location.reload();}else{if(window.__asmrlibToast)window.__asmrlibToast('移除失败');}});};"
            "if(window.__asmrlibConfirm){window.__asmrlibConfirm('移除该标注？',fn);return false;}"
            "if(!confirm('移除该标注？'))return false;fn();return false;}"
            "function _previewSkeleton(grid){"
            "var html='';"
            "for(var i=0;i<3;i++){"
            "html+='<div class=\"preview-cell is-loading\">'"
            "+'<div class=\"preview-skel\"></div>'"
            "+'<div class=\"preview-skel-label\"></div></div>';}"
            "grid.innerHTML=html;}"
            "function _renderPreviewFrames(grid, frames){"
            "var html='';"
            "for(var i=0;i<frames.length;i++){"
            "var f=frames[i];"
            "var src=f.url||'';"
            "html+='<figure class=\"preview-cell\">'"
            "+'<button type=\"button\" class=\"preview-shot\" data-src=\"'+src+'\" '"
            "+'onclick=\"return openPreviewLightbox(this)\">'"
            "+'<img src=\"'+src+'\" alt=\"frame '+f.index+'\" loading=\"lazy\">'"
            "+'<span class=\"preview-time\">'+f.label+'</span></button></figure>';}"
            "grid.innerHTML=html;}"
            "function openPreviewLightbox(btn){"
            "var src=btn.getAttribute('data-src');if(!src)return false;"
            "var host=document.getElementById('preview-lightbox');"
            "if(!host){"
            "host=document.createElement('div');host.id='preview-lightbox';"
            "host.className='preview-lightbox';host.setAttribute('hidden','');"
            "host.innerHTML='<button type=\"button\" class=\"preview-lightbox-x\" "
            "onclick=\"closePreviewLightbox()\">✕</button>"
            "<img alt=\"preview\">';"
            "host.addEventListener('click',function(e){"
            "if(e.target===host)closePreviewLightbox();});"
            "document.body.appendChild(host);}"
            "var img=host.querySelector('img');"
            "img.src=src;"
            "host.removeAttribute('hidden');"
            "document.body.classList.add('has-lightbox');"
            "return false;}"
            "function closePreviewLightbox(){"
            "var host=document.getElementById('preview-lightbox');"
            "if(host)host.setAttribute('hidden','');"
            "document.body.classList.remove('has-lightbox');"
            "return false;}"
            "function loadMediaPreview(id, refresh){"
            "var panel=document.getElementById('preview-panel-'+id);"
            "var grid=document.getElementById('preview-grid-'+id);"
            "var meta=document.getElementById('preview-meta-'+id);"
            "var refreshBtn=document.getElementById('preview-refresh-'+id);"
            "if(!panel||!grid)return false;"
            "panel.hidden=false;"
            "panel.classList.add('is-busy');"
            "_previewSkeleton(grid);"
            "if(meta)meta.textContent=refresh?'正在刷新时间点…':'正在截取 3 帧…';"
            "fetch('/preview/generate',{method:'POST',"
            "headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({id:id,refresh:!!refresh})})"
            ".then(function(r){return r.json();})"
            ".then(function(d){"
            "panel.classList.remove('is-busy');"
            "if(!d||!d.ok){"
            "grid.innerHTML='<p class=\"preview-error\">'"
            "+(d&&d.error?d.error:'预览失败')+'</p>';"
            "if(meta)meta.textContent='';"
            "if(window.__asmrlibToast)window.__asmrlibToast(d&&d.error?d.error:'预览失败');"
            "return;}"
            "_renderPreviewFrames(grid,d.frames||[]);"
            "if(meta){"
            "meta.textContent='时长 '+ (d.duration_label||'')"
            "+' · 仅内存 · 不落盘 · 点图可放大';}"
            "if(refreshBtn)refreshBtn.hidden=false;"
            "if(window.__asmrlibToast){"
            "window.__asmrlibToast(refresh?'已换一组时间点':'已生成 3 帧预览');}"
            "})"
            ".catch(function(){"
            "panel.classList.remove('is-busy');"
            "grid.innerHTML='<p class=\"preview-error\">网络或服务异常</p>';"
            "if(window.__asmrlibToast)window.__asmrlibToast('预览失败');"
            "});"
            "return false;}"
            "var tagInput=document.getElementById('tagInput');"
            "if(tagInput){tagInput.addEventListener('keydown',function(e){"
            "if(e.key==='Enter'){e.preventDefault();var src=document.body.getAttribute('data-source');"
            "if(src)addTag(src);}});}"
            "document.body.setAttribute('data-source','" + source_url.replace("'", "\\'") + "');"
            "document.addEventListener('keydown',function(e){"
            "if(e.key==='Escape')closePreviewLightbox();});"
            "</script>"
        )
        return self._page(title, body)

    # ---------------------------------------------------------- live explore

    def _post_cards(
        self,
        rows,
        *,
        rail: bool = False,
        rail_label: str = "内容轨道",
    ) -> str:
        row_list = list(rows or [])
        source_urls = [str(row["source_url"]) for row in row_list]
        # ArchiveDb now exposes the aggregate query through MediaRepo.  Keep a
        # defensive empty mapping for lightweight fakes used by downstream
        # embedders; production never falls back to per-card SQL.
        count_fn = getattr(self.db, "media_counts_for_sources", None)
        media_counts = count_fn(source_urls) if callable(count_fn) else {}
        cards: list[str] = []
        for index, row in enumerate(row_list):
            title = str(row["title"] or row["source_url"])
            href = f"/post/{quote(row['source_url'], safe='')}"
            counts = media_counts.get(str(row["source_url"]), {})
            local_n = int(counts.get("local", 0) or 0)
            ref_n = int(counts.get("references", 0) or 0)
            badges: list[str | tuple[str, str]] = []
            if local_n:
                badges.append((f"本地 {local_n}", "ok"))
            if ref_n:
                badges.append(f"在线 {ref_n}")
            cards.append(
                cover_card(
                    href=href,
                    title=title,
                    meta=str(row["published_at"] or row["status"] or ""),
                    cover_url=str(row["cover"] or ""),
                    plate_css=self._plate_gradient(index),
                    badges=badges,
                    initial=(title[:1] or "?"),
                )
            )
        if rail:
            return cover_rail(cards, label=rail_label)
        return cover_grid(cards)

    def _search_form(
        self, q: str = "", tag: str = "", *, compact: bool = False
    ) -> str:
        cls = "search search-compact" if compact else "search"
        # Home: one field is enough; full list page keeps title + tag filters.
        if compact and not tag:
            return (
                f"<form class='{cls}' method='get' action='/posts'>"
                f"<input type='search' name='q' value='{_h(q)}' aria-label='搜索收藏' "
                "placeholder='搜索标题 / 作者 / 标签'>"
                "<button type='submit'>搜索</button>"
                "</form>"
            )
        return (
            f"<form class='{cls}' method='get' action='/posts'>"
            f"<input type='search' name='q' value='{_h(q)}' aria-label='搜索标题、网址或作者' "
            "placeholder='搜索标题 / URL / 作者'>"
            f"<input type='text' name='tag' value='{_h(tag)}' aria-label='筛选标签' placeholder='标签'>"
            "<button type='submit'>搜索</button>"
            "</form>"
        )

    # -------------------------------------------------------------- local media

    def _render_recordings(self, query: dict[str, list[str]]) -> str:
        """Local file management page: every recording / import grouped by post,
        with play, rename and delete. Shows disk usage in one place."""
        rows = self.db.list_local_media()
        total_bytes = 0
        groups: dict[str, list] = {}
        for media in rows:
            path = Path(str(media["file_path"] or ""))
            if not path.is_file():
                continue
            total_bytes += path.stat().st_size
            try:
                rel = path.resolve().relative_to(self.root)
            except ValueError:
                continue
            src = f"/media/{quote(rel.as_posix())}"
            label = str(media["label"] or media["kind"] or "本地文件")
            title = str(media["title"] or media["item_url"] or "未知帖子")
            item_url = str(media["item_url"] or "")
            item_cover = str(media["item_cover"] or "")
            size_hr = _human_size(path.stat().st_size)
            mtime = _human_time(media["updated_at"])
            media_id = int(media["id"])
            local_mini = (
                f"/watch-local?src={quote(src, safe='')}&title="
                f"{quote(label, safe='')}&mini=1"
            )
            group = groups.setdefault(item_url, {"title": title, "cover": item_cover, "items": []})
            actions = (
                action_btn("播放", variant="local", size="sm", href=src, icon="")
                + action_btn(
                    "小窗",
                    variant="mini",
                    size="sm",
                    onclick=f"return openMiniPlayer({json.dumps(local_mini)})",
                    icon="",
                )
                + action_btn(
                    "重命名",
                    variant="ghost",
                    size="sm",
                    onclick=(
                        f"return renameMedia({media_id}, {json.dumps(label)})"
                    ),
                    icon="",
                )
                + action_btn(
                    "删除",
                    variant="danger",
                    size="sm",
                    onclick=(
                        f"return deleteMedia({media_id}, {json.dumps(path.name)})"
                    ),
                    icon="",
                )
            )
            group["items"].append(
                "<article class='rec-card'>"
                "<div class='rec-card-main'>"
                f"<strong class='rec-label'>{_h(label)}</strong>"
                f"<span class='muted rec-file'>{_h(path.name)}</span>"
                "</div>"
                "<div class='rec-card-meta'>"
                f"<span class='rec-badge' data-dur='{media_id}'>–:––</span>"
                f"<span class='muted'>{size_hr}</span>"
                f"<span class='muted'>{mtime}</span>"
                "</div>"
                f"<div class='rec-card-actions'>{actions}</div>"
                f"<div class='rec-card-player' data-player='{media_id}'><video "
                f"src='{_h(src)}' preload='metadata' data-dur-src='{media_id}'"
                "></video></div>"
                "</article>"
            )

        title_line = f"共 {len(rows)} 个本地文件"
        if total_bytes:
            title_line += f" · 占用 {_human_size(total_bytes)}"
        group_html: list[str] = []
        if groups:
            for item_url, group in groups.items():
                cover = group["cover"]
                thumb = (
                    f"<div class='rec-group-cover' "
                    f"style='background-image:url({_h(cover)})'></div>"
                    if cover
                    else "<div class='rec-group-cover rec-group-cover-plain'>♥</div>"
                )
                group_html.append(
                    "<section class='rec-group'>"
                    f"<a class='rec-group-head' href='/post/{quote(item_url, safe='')}'>"
                    f"{thumb}"
                    "<div class='rec-group-head-main'>"
                    f"<strong>{_h(group['title'])}</strong>"
                    f"<span class='muted'>{len(group['items'])} 个文件</span>"
                    "</div>"
                    "<span class='rec-group-arrow'>→</span>"
                    "</a>"
                    "<div class='rec-group-list'>"
                    + "".join(group["items"])
                    + "</div></section>"
                )
        else:
            group_html.append(
                empty_state(
                    "还没有本地文件",
                    "在帖子详情页播放本地媒体，或导入已有文件后，会出现在这里。",
                    kicker="RECORDINGS",
                )
            )
        body = [
            crumb(("收藏馆", "/"), ("本地媒体", None)),
            page_header(
                "本地媒体管理",
                subtitle="本地文件按帖子分组；删除会同时移除磁盘文件。",
                trailing=f"<span class='muted section-count'>{_h(title_line)}</span>",
            ),
            section_block(
                "文件列表",
                "".join(group_html),
                extra_class="rec-panel",
            ),
        ]
        # Release the <video> handle before asking the server to delete —
        # Windows refuses unlink() while the page still has the file open.
        body.append(
            "<script>"
            "function _releaseMedia(id){"
            "try{var v=document.querySelector('video[data-dur-src=\"'+id+'\"]');"
            "if(v){try{v.pause();}catch(e){}v.removeAttribute('src');"
            "try{v.load();}catch(e){}}}catch(e){}}"
            "function _doDelete(id){"
            "_releaseMedia(id);"
            "return new Promise(function(res){setTimeout(function(){"
            "fetch('/recordings/delete',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({id:id})}).then(function(r){return r.json();}).then(res)"
            ".catch(function(){res({ok:false});});},220);});}"
            "function deleteMedia(id, name){"
            "var run=function(){_doDelete(id).then(function(d){"
            "if(d&&d.ok){"
            "if(window.__asmrlibToast)window.__asmrlibToast(d.file_removed?'已删除':(d.file_error||'已从库中移除'));"
            "location.reload();}"
            "else{if(window.__asmrlibToast)window.__asmrlibToast('删除失败');else alert('删除失败');}});};"
            "if(window.__asmrlibConfirm){"
            "window.__asmrlibConfirm('删除「'+name+'」？该文件会从磁盘移除。',run);return false;}"
            "if(!confirm('删除「'+name+'」？该文件会从磁盘移除。'))return false;"
            "run();return false;}"
            "function renameMedia(id, cur){"
            "if(window.__asmrlibPrompt){"
            "window.__asmrlibPrompt('重命名：',cur,function(label){"
            "fetch('/recordings/rename',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({id:id,label:label})}).then(function(r){return r.json();}).then(function(d){"
            "if(d&&d.ok){if(window.__asmrlibToast)window.__asmrlibToast('已重命名');location.reload();}"
            "else{if(window.__asmrlibToast)window.__asmrlibToast('重命名失败');}});});"
            "return false;}"
            "var label=prompt('重命名：',cur);"
            "if(label===null||!label.trim())return false;"
            "fetch('/recordings/rename',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({id:id,label:label.trim()})}).then(function(r){return r.json();}).then(function(d){"
            "if(d&&d.ok){location.reload();}else{alert('重命名失败');}});"
            "return false;}"
            "function fmtDur(s){if(!isFinite(s)||s<=0)return '–:––';"
            "s=Math.floor(s);var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;"
            "function p(n){return (n<10?'0':'')+n;}return (h?h+':':'')+p(m)+':'+p(x);}"
            "document.querySelectorAll('video[data-dur-src]').forEach(function(v){"
            "var id=v.getAttribute('data-dur-src');"
            "var badge=document.querySelector('[data-dur=\"'+id+'\"]');"
            "v.addEventListener('loadedmetadata',function(){"
            "if(badge)badge.textContent=fmtDur(v.duration);});"
            "v.addEventListener('error',function(){if(badge)badge.textContent='–';});"
            "});"
            "</script>"
        )
        return self._page("本地媒体管理", body)

    def _pagination(
        self,
        base: str,
        page: int,
        total: int,
        *,
        q: str = "",
        tag: str = "",
        name: str = "",
    ) -> str:
        return pagination_html(
            base, page, total, PAGE_SIZE, q=q, tag=tag, name=name
        )

