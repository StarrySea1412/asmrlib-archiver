"""Shared HTML UI primitives for the local archive viewer.

These keep page modules from re-stringifying the same crumb / button / section
markup. Pure functions only — no DB, no request state.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from urllib.parse import quote

from .util import _h


# --------------------------------------------------------------------------- crumbs

def crumb(*parts: tuple[str, str | None]) -> str:
    """Breadcrumb. Each part is ``(label, href|None)``; last is usually current."""
    bits: list[str] = ["<p class='crumb'>"]
    for i, (label, href) in enumerate(parts):
        if i:
            bits.append("<span class='crumb-sep'>/</span>")
        if href:
            bits.append(f"<a href='{_h(href)}'>{_h(label)}</a>")
        else:
            bits.append(f"<span class='crumb-current'>{_h(label)}</span>")
    bits.append("</p>")
    return "".join(bits)


# --------------------------------------------------------------------------- pills / chips

def pill(key: str, value: str, *, href: str | None = None, status: bool = False) -> str:
    cls = "pill pill-status" if status else "pill"
    inner = (
        f"<span class='pill-k'>{_h(key)}</span>"
        f"<span class='pill-v'>{_h(value)}</span>"
    )
    if href:
        return f"<a class='{cls}' href='{_h(href)}'>{inner}</a>"
    return f"<span class='{cls}'>{inner}</span>"


def pills(items: Iterable[str]) -> str:
    joined = "".join(items)
    if not joined:
        return ""
    return f"<div class='detail-pills'>{joined}</div>"


def status_chip(text: str, *, ok: bool = False) -> str:
    cls = "status-chip status-ok" if ok else "status-chip"
    return f"<span class='{cls}'>{_h(text)}</span>"


def chip(label: str, *, href: str | None = None, count: int | None = None) -> str:
    count_html = f" <em>{int(count)}</em>" if count is not None else ""
    inner = f"{_h(label)}{count_html}"
    if href:
        return f"<a class='chip' href='{_h(href)}'>{inner}</a>"
    return f"<span class='chip'>{inner}</span>"


def chips(items: Iterable[str]) -> str:
    joined = "".join(items)
    if not joined:
        return ""
    return f"<div class='chips'>{joined}</div>"


# --------------------------------------------------------------------------- buttons / actions

_ACTION_ICONS = {
    "online": "\u25b6",
    "local": "\u25b6",
    "mini": "\u2197",
    "ghost": "\u2192",
    "danger": "\u00d7",
}
_ACTION_VARIANTS = frozenset(_ACTION_ICONS)


def action_btn(
    label: str,
    *,
    variant: str = "ghost",
    href: str | None = None,
    onclick: str | None = None,
    icon: str | None = None,
    size: str = "md",
    small: bool = False,
    extra_class: str = "",
    target_blank: bool = False,
) -> str:
    """Render one of the lightweight viewer's supported action buttons.

    ``variant`` is one of ``online``, ``local``, ``mini``, ``ghost`` or
    ``danger``.  ``small=True`` is an alias for ``size='sm'``.
    """
    if small:
        size = "sm"
    if variant not in _ACTION_VARIANTS:
        raise ValueError(f"unsupported action variant: {variant}")
    classes = ["action-btn", f"action-{variant}"]
    if size == "sm":
        classes.append("action-sm")
    elif size == "lg":
        classes.append("action-lg")
    if extra_class:
        classes.append(extra_class)
    cls = " ".join(classes)
    ico = icon if icon is not None else _ACTION_ICONS.get(variant, "")
    # Compact rows keep labels only; hero/md can show icons.
    show_icon = bool(ico) and size != "sm"
    label_html = (
        f"<span class='action-ico'>{_h(ico)}</span><span>{_h(label)}</span>"
        if show_icon
        else _h(label)
    )
    if href is not None:
        blank = " target='_blank' rel='noopener'" if target_blank else ""
        on = f" onclick=\"{_h(onclick)}\"" if onclick else ""
        return f"<a class='{cls}' href='{_h(href)}'{blank}{on}>{label_html}</a>"
    # Must HTML-escape: callers pass json.dumps(...) which injects double quotes.
    # Without escaping, onclick="...("url")..." truncates the attribute and the
    # click handler becomes dead JS (hero + external action buttons all no-op).
    on = f" onclick=\"{_h(onclick)}\"" if onclick else ""
    return f"<button type='button' class='{cls}'{on}>{label_html}</button>"


def action_row(buttons: Sequence[str], *, hero: bool = False) -> str:
    cls = "detail-hero-actions" if hero else "action-row"
    return f"<div class='{cls}'>{''.join(buttons)}</div>"


def button(
    label: str,
    *,
    href: str | None = None,
    onclick: str | None = None,
    secondary: bool = False,
    size: str = "md",
    type_: str = "button",
) -> str:
    """Generic form / nav button (not the colored action-btn family)."""
    classes = ["button"]
    if secondary:
        classes.append("button-secondary")
    if size == "sm":
        classes.append("button-sm")
    elif size == "lg":
        classes.append("button-lg")
    cls = " ".join(classes)
    if href is not None:
        return f"<a class='{cls}' href='{_h(href)}'>{_h(label)}</a>"
    on = f" onclick=\"{_h(onclick)}\"" if onclick else ""
    return f"<button type='{type_}' class='{cls}'{on}>{_h(label)}</button>"


# --------------------------------------------------------------------------- sections

def section_head(title: str, trailing: str = "") -> str:
    trail = trailing or ""
    return (
        "<div class='section-head'>"
        f"<h2 class='section-title'>{_h(title)}</h2>"
        f"{trail}"
        "</div>"
    )


def section_block(
    title: str,
    body_html: str | Sequence[str],
    *,
    trailing: str = "",
    extra_class: str = "",
    hint: str = "",
) -> str:
    """Standard content panel: head + optional hint + body."""
    classes = ["section-block"]
    if extra_class:
        classes.append(extra_class)
    body = body_html if isinstance(body_html, str) else "".join(body_html)
    hint_html = f"<p class='action-hint muted'>{hint}</p>" if hint else ""
    return (
        f"<section class='{' '.join(classes)}'>"
        f"{section_head(title, trailing)}"
        f"{hint_html}"
        f"{body}"
        "</section>"
    )


# --------------------------------------------------------------------------- cinema primitives

def _plate_index(title: str) -> int:
    """Return a stable, cheap palette index for cover-less items."""
    return sum(ord(ch) for ch in (title or "?")) % 8


def cinema_hero(
    *,
    title: str,
    href: str,
    cover_url: str = "",
    meta: str = "",
    kicker: str = "",
    action_html: str = "",
    initial: str = "",
) -> str:
    """Full-width home hero with a real image and deterministic fallback.

    The image is decorative because the heading and CTA provide the accessible
    name. ``data-cover-img`` lets the shared UI script swap a failed image for
    the matching static plate without an inline event handler.
    """
    title_text = title or "ASMR 收藏馆"
    idx = _plate_index(title_text)
    if cover_url:
        media = (
            "<img class='cinema-hero-img' data-cover-img "
            f"src='{_h(cover_url)}' alt='' loading='eager' "
            "fetchpriority='high' decoding='async'>"
            f"<div class='cinema-hero-fallback plate-{idx}' aria-hidden='true' hidden>"
            f"{_h(initial or title_text[:1] or '?')}</div>"
        )
    else:
        media = (
            f"<div class='cinema-hero-fallback plate-{idx}' aria-hidden='true'>"
            f"{_h(initial or title_text[:1] or '?')}</div>"
        )
    meta_html = f"<p class='cinema-hero-meta'>{_h(meta)}</p>" if meta else ""
    kicker_html = f"<p class='cinema-hero-kicker'>{_h(kicker)}</p>" if kicker else ""
    return (
        "<section class='cinema-hero' data-cinema-hero>"
        f"<div class='cinema-hero-media plate-{idx}'>"
        f"{media}<div class='cinema-hero-scrim' aria-hidden='true'></div></div>"
        "<div class='cinema-hero-copy'>"
        f"{kicker_html}<h1 class='cinema-hero-title'>{_h(title_text)}</h1>"
        f"{meta_html}"
        f"<div class='cinema-hero-actions'>{action_html}"
        f"<a class='cinema-hero-detail' href='{_h(href)}'>查看详情</a></div>"
        "</div></section>"
    )


def cover_rail(
    cards: Sequence[str],
    *,
    label: str = "内容轨道",
    empty: str = "",
) -> str:
    """Accessible horizontal rail used by the cinema home and related items."""
    if not cards:
        return f"<p class='muted empty-inline'>{_h(empty)}</p>" if empty else ""
    return (
        "<div class='cinema-rail-shell'>"
        "<button type='button' class='cinema-rail-arrow is-prev' "
        "data-rail-dir='prev' aria-label='向左滚动' disabled>‹</button>"
        f"<div class='cinema-rail' data-cinema-rail tabindex='0' "
        f"role='region' aria-label='{_h(label)}'>"
        f"{''.join(cards)}</div>"
        "<button type='button' class='cinema-rail-arrow is-next' "
        "data-rail-dir='next' aria-label='向右滚动'>›</button>"
        "</div>"
    )


# --------------------------------------------------------------------------- detail hero

def detail_poster(inner_html: str) -> str:
    return (
        f"<div class='detail-poster'>{inner_html}"
        "<div class='detail-poster-glow' aria-hidden='true'></div></div>"
    )


def detail_backdrop(style_attr: str) -> str:
    """``style_attr`` is either ``background-image: url(...)`` or ``background: ...``."""
    return (
        f"<div class='detail-hero-bg' aria-hidden='true' style='{style_attr}'></div>"
        "<div class='detail-hero-bg-fade' aria-hidden='true'></div>"
    )


def detail_hero(
    *,
    backdrop: str,
    poster: str,
    kicker: str,
    title: str,
    meta_html: str = "",
    actions_html: str = "",
    has_cover: bool = False,
    extra_main: str = "",
) -> str:
    """Detail page hero: cover wash + poster + title/meta/actions.

    Tags / forms intentionally stay OUT of the hero — they go in the meta strip
    below so the primary play actions stay scannable.
    """
    cls = "detail-hero has-cover" if has_cover else "detail-hero"
    return (
        f"<section class='{cls}'>"
        f"{backdrop}"
        f"{poster}"
        "<div class='detail-hero-main'>"
        f"<div class='detail-kicker'>{_h(kicker)}</div>"
        f"<h1 class='detail-title'>{_h(title)}</h1>"
        f"{meta_html}"
        f"{extra_main}"
        f"{actions_html}"
        "</div></section>"
    )


def meta_strip(body_html: str) -> str:
    """Quiet band under the hero for tags / annotate form."""
    if not body_html:
        return ""
    return f"<section class='meta-strip'>{body_html}</section>"


# --------------------------------------------------------------------------- pagination

def pagination(
    base: str,
    page: int,
    total: int,
    page_size: int,
    *,
    q: str = "",
    tag: str = "",
    name: str = "",
) -> str:
    pages = max(1, (total + page_size - 1) // page_size)
    if pages <= 1:
        return ""

    def href(target: int) -> str:
        qs: list[str] = []
        if q:
            qs.append(f"q={quote(q)}")
        if tag:
            qs.append(f"tag={quote(tag)}")
        if name:
            qs.append(f"name={quote(name)}")
        qs.append(f"page={target}")
        return f"{base}?{'&'.join(qs)}#page-top"

    parts = ["<nav class='pager' aria-label='分页'>"]
    if page > 1:
        parts.append(
            f"<a class='pager-btn' href='{_h(href(page - 1))}'>"
            f"<span class='pager-ico' aria-hidden='true'>‹</span>上一页</a>"
        )
    else:
        parts.append(
            "<span class='pager-btn is-disabled' aria-disabled='true'>"
            "<span class='pager-ico' aria-hidden='true'>‹</span>上一页</span>"
        )

    parts.append("<label class='pager-jump'>")
    parts.append("<span class='pager-jump-k'>跳到</span>")
    # Navigation + loading overlay handled by _LOADING_JS on .pager-select change.
    parts.append("<select class='pager-select' aria-label='跳到页码'>")
    for p in range(1, pages + 1):
        sel = " selected" if p == page else ""
        if p == page:
            label = f"第 {p} / {pages} 页"
        elif p == page + 1:
            label = f"下一页 · 第 {p} 页"
        else:
            label = f"第 {p} 页"
        parts.append(
            f"<option value='{_h(href(p))}'{sel}>{_h(label)}</option>"
        )
    parts.append("</select></label>")

    if page < pages:
        parts.append(
            f"<a class='pager-btn pager-next' href='{_h(href(page + 1))}'>"
            f"下一页<span class='pager-ico' aria-hidden='true'>›</span></a>"
        )
    parts.append(
        "<a class='pager-top' href='#page-top' title='回到顶部'>"
        "<span class='pager-ico' aria-hidden='true'>⌃</span>顶部</a>"
    )
    parts.append("</nav>")
    return "".join(parts)


# --------------------------------------------------------------------------- page chrome / cards

def page_header(
    title: str,
    *,
    subtitle: str = "",
    trailing: str = "",
) -> str:
    """List/author page title row with optional muted subtitle + trailing link."""
    sub = f"<p class='muted page-sub'>{subtitle}</p>" if subtitle else ""
    trail = f"<div class='page-header-trail'>{trailing}</div>" if trailing else ""
    return (
        f"<header class='page-header'>"
        f"<div class='page-header-main'>"
        f"<h1 class='page-title'>{_h(title)}</h1>"
        f"{sub}"
        f"</div>"
        f"{trail}"
        f"</header>"
    )


def cover_card(
    *,
    href: str,
    title: str,
    meta: str = "",
    cover_url: str = "",
    plate_css: str = "",
    badges: Sequence[str | tuple[str, str]] = (),
    initial: str = "",
    extra_meta: str = "",
    meta_escaped: bool = False,
    loading: str = "lazy",
) -> str:
    """One card in the cover grid (list / home / related / live).

    ``badges`` entries are either a label string, or ``(label, kind)`` where
    ``kind`` is a cover-badge modifier (e.g. ``"ok"`` → ``cover-badge-ok``).
    ``extra_meta`` is raw HTML (already escaped) inserted above the meta line.
    When ``meta_escaped`` is True, ``meta`` is treated as safe HTML too.
    """
    badge_bits: list[str] = []
    for b in badges:
        if isinstance(b, tuple):
            text, kind = b
            cls = f"cover-badge cover-badge-{kind}" if kind else "cover-badge"
            badge_bits.append(f"<span class='{cls}'>{_h(text)}</span>")
        else:
            badge_bits.append(f"<span class='cover-badge'>{_h(b)}</span>")
    badge_html = "".join(badge_bits)
    letter = initial or (title[:1] if title else "?")
    idx = _plate_index(title)
    if cover_url:
        # Keep the title as the accessible link name; the image is decorative.
        # A delegated error handler in assets.py swaps it for the static plate.
        thumb = (
            "<div class='cover-thumb'>"
            f"<img class='cover-thumb-img' data-cover-img src='{_h(cover_url)}' "
            "alt='' width='640' height='360' "
            f"loading='{_h(loading if loading in {'lazy', 'eager'} else 'lazy')}' "
            "decoding='async'>"
            f"<div class='cover-thumb-fallback plate-{idx}' aria-hidden='true'>"
            f"<span class='cover-initial'>{_h(letter)}</span></div>"
            f"{badge_html}"
        )
    else:
        thumb = (
            f"<div class='cover-thumb cover-thumb-plate plate-{idx}'>"
            f"<span class='cover-initial'>{_h(letter)}</span>{badge_html}"
        )
    thumb += "<div class='cover-scrim' aria-hidden='true'></div>"
    if meta:
        meta_body = meta if meta_escaped else _h(meta)
        meta_html = f"<span class='cover-meta muted'>{meta_body}</span>"
    else:
        meta_html = ""
    # Overlay metadata on the image so every card keeps one stable 16:9 box.
    return (
        f"<a class='cover-card' href='{_h(href)}' aria-label='{_h(title)}'>"
        f"{thumb}"
        f"<span class='cover-info'>"
        f"<strong class='cover-title'>{_h(title)}</strong>"
        f"{extra_meta}"
        f"{meta_html}"
        f"</span></div></a>"
    )


def cover_grid(cards: Sequence[str], *, empty: str = "没有帖子。") -> str:
    if not cards:
        return f"<p class='muted empty-inline'>{_h(empty)}</p>"
    return f"<div class='cover-grid'>{''.join(cards)}</div>"


def live_pager(
    *,
    page: int,
    prev_href: str | None,
    next_href: str,
    option_hrefs: Sequence[tuple[int, str]],
) -> str:
    """Pager for live browse/explore (unknown total page count).

    ``option_hrefs`` is ``[(page_num, href), ...]`` for the jump dropdown.
    """
    parts = ["<nav class='pager' aria-label='分页'>"]
    if prev_href:
        parts.append(
            f"<a class='pager-btn' href='{_h(prev_href)}'>"
            f"<span class='pager-ico' aria-hidden='true'>‹</span>上一页</a>"
        )
    else:
        parts.append(
            "<span class='pager-btn is-disabled' aria-disabled='true'>"
            "<span class='pager-ico' aria-hidden='true'>‹</span>上一页</span>"
        )
    parts.append("<label class='pager-jump'>")
    parts.append("<span class='pager-jump-k'>跳到</span>")
    # Navigation + loading overlay handled by _LOADING_JS on .pager-select change.
    parts.append("<select class='pager-select' aria-label='跳到页码'>")
    for p, href in option_hrefs:
        sel = " selected" if p == page else ""
        if p == page:
            label = f"第 {p} 页"
        elif p == page + 1:
            label = f"下一页 · 第 {p} 页"
        else:
            label = f"第 {p} 页"
        parts.append(
            f"<option value='{_h(href)}'{sel}>{_h(label)}</option>"
        )
    parts.append("</select></label>")
    parts.append(
        f"<a class='pager-btn pager-next' href='{_h(next_href)}'>"
        f"下一页<span class='pager-ico' aria-hidden='true'>›</span></a>"
    )
    parts.append(
        "<a class='pager-top' href='#page-top' title='回到顶部'>"
        "<span class='pager-ico' aria-hidden='true'>⌃</span>顶部</a>"
    )
    parts.append("</nav>")
    return "".join(parts)


# --------------------------------------------------------------------------- empty

def empty_state(
    title: str,
    message: str,
    action_html: str = "",
    *,
    kicker: str = "",
) -> str:
    kick = (
        f"<p class='empty-kicker'>{_h(kicker)}</p>" if kicker else ""
    )
    # ``message`` may contain intentional <code> snippets — caller escapes text.
    return (
        "<section class='empty-state'>"
        f"{kick}"
        f"<h1>{_h(title)}</h1>"
        f"<p class='muted empty-msg'>{message}</p>"
        f"{action_html}"
        "</section>"
    )


__all__ = [
    "action_btn",
    "action_row",
    "button",
    "chip",
    "chips",
    "cover_card",
    "cover_rail",
    "cover_grid",
    "cinema_hero",
    "crumb",
    "detail_backdrop",
    "detail_hero",
    "detail_poster",
    "empty_state",
    "live_pager",
    "meta_strip",
    "page_header",
    "pagination",
    "pill",
    "pills",
    "section_block",
    "section_head",
    "status_chip",
]
