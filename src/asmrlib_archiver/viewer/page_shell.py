"""Full-page HTML shell (nav, head, loading overlay) for the viewer.

Lives outside ``app.py`` so the LibraryPages/LivePages/PlaybackPages mixins
can render through the same chrome without an import cycle.
"""

from __future__ import annotations

from .ui_assets import UI_VERSION
from .util import _h

_NAV_ICONS = {
    # 16-grid line icons: 1.6 stroke, round caps, shared geometry.
    "home": (
        '<path d="M2.9 8.1 8 3.7l5.1 4.4"/>'
        '<path d="M4.5 7.4v4.4a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1V7.4"/>'
        '<path d="M6.8 12.8V9.9h2.4v2.9"/>'
    ),
    "discover": (
        '<circle cx="8" cy="8" r="5.7"/>'
        '<path d="M10.5 5.5 9.1 9.1 5.5 10.5 6.9 6.9z"/>'
    ),
    "library": (
        '<path d="M8 2.8 13.2 5.6 8 8.4 2.8 5.6z"/>'
        '<path d="M2.8 8.5 8 11.3l5.2-2.8"/>'
        '<path d="M2.8 11.1 8 13.9l5.2-2.8"/>'
    ),
    "local": (
        '<path d="M2.7 10.8V8.9a5.3 5.3 0 0 1 10.6 0v1.9"/>'
        '<rect x="2.4" y="9.6" width="2.8" height="3.8" rx="1.2"/>'
        '<rect x="10.8" y="9.6" width="2.8" height="3.8" rx="1.2"/>'
    ),
}


def _nav_link(href: str, nav: str, label: str) -> str:
    icon = _NAV_ICONS.get(nav, "")
    return (
        f'<a href="{href}" data-nav="{nav}">'
        f'<svg class="nav-ico" viewBox="0 0 16 16" width="18" height="18" '
        f'fill="none" stroke="currentColor" stroke-width="1.6" '
        f'stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true">{icon}</svg>'
        f"<span class=\"nav-label\">{label}</span></a>"
    )


def _page(title: str, body: list[str], *, mini: bool = False) -> str:
    if mini:
        return f"""<!doctype html>
<html lang="zh-CN" class="mini-html">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#07090d">
<title>{_h(title)} | ASMR 收藏馆</title>
<link rel="stylesheet" href="/ui/app.css?v={UI_VERSION}">
</head>
<body class="mini-body">
<main class="mini-wrap">
{"".join(body)}
</main>
<script defer src="/ui/app.js?v={UI_VERSION}"></script>
</body>
</html>
"""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#07090d">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="收藏馆">
<title>{_h(title)} | ASMR 收藏馆</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" type="image/svg+xml" href="/ui/icon.svg">
<link rel="apple-touch-icon" href="/ui/icon.svg">
<link rel="stylesheet" href="/ui/app.css?v={UI_VERSION}">
</head>
<body>
<div id="nav-loading" class="nav-loading" aria-hidden="true" role="status">
  <div class="nav-loading-bar" id="nav-loading-bar"></div>
  <div class="nav-loading-panel">
    <span class="nav-loading-spin" aria-hidden="true"></span>
    <span id="nav-loading-text">加载中…</span>
  </div>
</div>
<header class="top">
  <a class="brand" href="/">
    <span class="brand-mark" aria-hidden="true">
      <svg class="brand-ico" viewBox="0 0 16 16" width="14" height="14" fill="none">
        <rect x="1.5" y="6" width="2.2" height="4" rx="1" fill="currentColor"/>
        <rect x="4.9" y="3.5" width="2.2" height="9" rx="1" fill="currentColor"/>
        <rect x="8.3" y="5" width="2.2" height="6" rx="1" fill="currentColor"/>
        <rect x="11.7" y="2.5" width="2.2" height="11" rx="1" fill="currentColor"/>
      </svg>
    </span>
    ASMR <span>收藏馆</span>
  </a>
  <nav id="top-nav" aria-label="主导航">
    {_nav_link("/", "home", "首页")}
    {_nav_link("/browse", "discover", "发现")}
    {_nav_link("/posts", "library", "收藏")}
    {_nav_link("/recordings", "local", "本地")}
  </nav>
</header>
<main class="wrap" id="page-top">
{"".join(body)}
</main>
<script defer src="/ui/app.js?v={UI_VERSION}"></script>
</body>
</html>
"""
