"""Resolve a post page URL to its bare player URL for the sandbox window.

The user-facing promise of the guarded player window is a *pure video
page*: no site header, no related-posts grid, no Telegram banners, no
advertiser iframes.  The post page at asmrlib.com/posts/<slug> embeds its
actual player behind server buttons (``#players [data-url]`` pointing at
bysetayico / upn.one / abyss ...).  This module fetches the post page once
with the same guards the crawler uses and returns the best player embed
URL plus all alternates, so ``DesktopApi.open_online_player`` can load the
player document itself instead of the whole site page.

Fetching is read-only and best-effort: on any failure the caller falls
back to the original URL, which the online shield already renders
safe-ish (ads stripped, popups killed, cinema mode applied).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .security_lists import PLAYER_DOMAINS


@dataclass(frozen=True)
class ResolvedPlayer:
    """The bare player URL and its alternates for one post page."""

    player_url: str
    source_url: str
    alternatives: tuple[str, ...] = ()

    @property
    def is_direct(self) -> bool:
        """True when the input URL was already a bare player URL."""

        return self.player_url == self.source_url


def _is_player_host(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    for domain in PLAYER_DOMAINS:
        if domain == "asmrlib.com":
            continue
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _rank(label: str, url: str) -> int:
    """Prefer the same server order the in-page auto-picker uses."""

    lowered_label = label.strip().upper()
    lowered_url = url.lower()
    if "bysetayico" in lowered_url or lowered_label == "BI":
        return 0
    if "upn.one" in lowered_url or lowered_label == "UP":
        return 1
    if "abyss" in lowered_url or lowered_label == "AB":
        return 2
    return 9


def resolve_player_url(url: str, *, timeout_seconds: float = 12.0) -> ResolvedPlayer:
    """Fetch a post page and extract the bare player embed URLs.

    Raises ``PlayerResolveError`` so callers can distinguish "already a
    player URL" from "resolution failed, fall back to the page".
    """

    try:
        urlsplit(url)
    except ValueError as exc:
        raise PlayerResolveError("invalid_url", str(exc)) from exc
    if _is_player_host(url):
        # Already a bare player link (e.g. from the "在线来源" picker).
        return ResolvedPlayer(player_url=url, source_url=url)

    from .guards import UrlGuard
    from .parser import AsmrlibParser

    guard = UrlGuard(
        allowed_domains=("asmrlib.com",),
        allow_subdomains=True,
    )
    # The page must still belong to the site domain; the player embeds it
    # references are extracted afterwards from the parsed media candidates.
    guard.assert_page_allowed(url)

    parser = AsmrlibParser(guard, allowed_extensions=())
    try:
        response = httpx.get(
            url,
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "ASMRLIBPersonalArchiver/0.1 (+local personal archive)"},
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - caller decides fallback
        raise PlayerResolveError("fetch_failed", str(exc)) from exc

    try:
        parsed = parser.parse(url, response.text)
    except Exception as exc:  # noqa: BLE001
        raise PlayerResolveError("parse_failed", str(exc)) from exc

    embeds: list[tuple[int, str, str]] = []
    for media in parsed.media or []:
        media_url = str(getattr(media, "media_url", "") or "")
        if not media_url:
            continue
        kind = str(getattr(media, "kind", "") or "")
        label = str(getattr(media, "label", "") or "")
        if kind in {"embed", "link", "media"} and _is_player_host(media_url):
            embeds.append((_rank(label, media_url), media_url, label))
    if not embeds:
        raise PlayerResolveError("no_player_found", "page exposes no player embed")

    embeds.sort(key=lambda item: (item[0], item[1]))
    best = embeds[0][1]
    alternatives = tuple(u for _, u, _ in embeds[1:])
    return ResolvedPlayer(player_url=best, source_url=url, alternatives=alternatives)


class PlayerResolveError(RuntimeError):
    """Raised when a post page cannot be resolved to a player URL."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
