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

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from .security_lists import PLAYER_DOMAINS, SOURCE_RANKING, SOURCE_RANKING_DEFAULT
from .source_health import RankedSource, SourceHealth, rank_sources


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
    """Prefer the same server order the in-page auto-picker uses.

    This is a *static* preference read off the label/URL — it says which
    server the site lists first, never whether that server works today.  It
    is kept only as the tie-break for :func:`rank_sources`; the primary
    ordering is the measured health record (see ``source_health``).

    The order itself lives in ``security_lists.SOURCE_RANKING``, which the
    injected shield JS renders from as well, so the two can no longer drift.
    Only the label and the URL are consulted here: the ``data-server``
    attribute that the JS also matches on is not available to a fetched,
    parsed page.
    """

    lowered_label = label.strip().upper()
    lowered_url = url.lower()
    for tier in SOURCE_RANKING:
        if lowered_label == tier.label or any(
            marker in lowered_url for marker in tier.url_markers
        ):
            return tier.rank
    return SOURCE_RANKING_DEFAULT


def resolve_player_candidates(
    url: str, *, timeout_seconds: float = 12.0
) -> list[tuple[str, str]]:
    """Return ``[(label, url)]`` for every player embed on a post page.

    The order is the static server preference — callers that know the
    measured health of each source should pass this through
    ``source_health.rank_sources`` before showing or opening anything.

    Raises ``PlayerResolveError`` when the page exposes no player at all.
    """

    try:
        urlsplit(url)
    except ValueError as exc:
        raise PlayerResolveError("invalid_url", str(exc)) from exc
    if _is_player_host(url):
        # Already a bare player link (e.g. from the "在线来源" picker).
        return [(urlsplit(url).hostname or "player", url)]

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
    return [(label, media_url) for _, media_url, label in embeds]


def resolve_player_sources(
    url: str,
    *,
    health: Mapping[str, SourceHealth] | None = None,
    preferred_url: str | None = None,
    now: datetime | None = None,
    timeout_seconds: float = 12.0,
) -> list[RankedSource]:
    """Resolve a post page to its candidates, ordered by measured health.

    ``health`` is the persisted per-source record (keyed by
    ``source_health.health_key``) and ``preferred_url`` the user's manual
    choice for this post, which outranks the score.  Sources with no history
    rank neutral, so a brand-new source is tried rather than buried.
    """

    return rank_sources(
        resolve_player_candidates(url, timeout_seconds=timeout_seconds),
        health=health,
        preferred_url=preferred_url,
        static_rank=_rank,
        now=now,
    )


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
        return ResolvedPlayer(player_url=url, source_url=url)

    candidates = resolve_player_candidates(url, timeout_seconds=timeout_seconds)
    best = candidates[0][1]
    alternatives = tuple(candidate_url for _, candidate_url in candidates[1:])
    return ResolvedPlayer(player_url=best, source_url=url, alternatives=alternatives)



class PlayerResolveError(RuntimeError):
    """Raised when a post page cannot be resolved to a player URL."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
