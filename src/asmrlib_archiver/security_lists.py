"""Single source of truth for the ad/tracker and player-network host tables.

guards.py (crawler/downloader), online_shield.py (desktop WebView2 online
player), and viewer/player_guard.py (system-browser hand-off) each need to
know which hosts are the legitimate ASMRLIB player/asset network and which
are known ad/tracker/link-shortener domains. Before this module existed,
each of the three carried its own copy of these tables and they had already
drifted out of sync -- different ad-host counts, entries present in one
file but missing from another. Edit these lists ONLY here; the three
modules import from here so their existing public names keep working.

PLAYER_DOMAINS / ASSET_DOMAINS / BLOCKED_HOSTS / BLOCK_URL_KEYWORDS /
AD_DOMAIN_ALIASES are Python-level security defaults, not user
configuration. BLOCK_URL_KEYWORDS here is only the fallback used when a
policy is built without a config source (e.g. a bare ``ShieldPolicy()`` in
tests); real deployments get this list from config.yaml's
``browser.block_url_keywords`` instead (a separate user-facing schema
default defined in config.py).

SOURCE_RANKING is the other shared table: the site's server-preference
order, read by both ``player_resolver._rank`` (Python) and the injected
bootstrap JS in ``online_shield``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Domains that host the actual ASMRLIB site and its known embedded players.
PLAYER_DOMAINS: tuple[str, ...] = (
    "asmrlib.com",
    "bysetayico.com",
    # Byse (bysetayico) serves its real player from these sibling hosts:
    # /l3y/<id> player page, /segh/<id> player shell. Without them the BI
    # embed dies at its second iframe hop and shows a 404.
    "f7hyg4q.org",
    "v.upn.one",
    "upn.one",
    "abyssplayer.com",
    "q8y5z.com",
)

# Poster/thumbnail and player-runtime asset hosts used by the embedded
# players. Never treated as navigable pages, only as allowed subresources.
ASSET_DOMAINS: tuple[str, ...] = (
    "img-place.com",
    "imgbox.com",
    "images2.imgbox.com",
    "videothumbs.me",
    "i1.wp.com",
    "i0.wp.com",
    "i2.wp.com",
    "jwplayer.com",
    "jwpcdn.com",
    "cdn.jwplayer.com",
    "ssl.p.jwpcdn.com",
    "cloudflareinsights.com",
    "imasdk.googleapis.com",
    # abyssplayer's runtime asset CDN (player-v2 bundles / danmu assets) —
    # same class as jwpcdn for jwplayer. Without it the AB player cannot
    # boot and shows its anti-adblock wall on every platform.
    "iamcdn.net",
    # asmr.one second source: cover images live on the API host itself and
    # the work pages on the site host — both subresources of the discover
    # page, neither a player, so the asset table is where they belong.
    # The -100/-200/-300 hosts are the mirror family the reference player
    # ships as one switchable site (Eara's default is 200): covers and
    # stream hosts ride the same domains, and matching is exact-or-subdomain,
    # so the bare domain covers api.* and the media subdomains.
    "asmr.one",
    "api.asmr.one",
    "asmr-100.com",
    "asmr-200.com",
    "asmr-300.com",
)

# Known ad/tracker/link-shortener hosts. Host matching always uses a label
# boundary (exact host or subdomain), never a path/query substring match --
# that would incorrectly block legitimate URLs such as /tags/affiliate.
BLOCKED_HOSTS: tuple[str, ...] = (
    "adservice.google.com",
    "adsterra.com",
    "amazon-adsystem.com",
    "bit.ly",
    "doubleclick.net",
    "downrightfootball.com",
    "dtscout.com",
    "exoclick.com",
    "goo.gl",
    "google-analytics.com",
    "googleadservices.com",
    "googlesyndication.com",
    "googletagmanager.com",
    "histats.com",
    "llvpn.com",
    "luugy.com",
    "popads.net",
    "propellerads.com",
    "rtmark.net",
    "sead.pages.dev",
    "t.co",
    "tinyurl.com",
    "wpadmngr.com",
)

# Bare keywords (no dot) that, combined with a label-boundary regex at
# match time, also catch ad/tracker hosts/paths not covered by
# BLOCKED_HOSTS above, matched against the full URL (path/query included),
# not just the hostname.
BLOCK_URL_KEYWORDS: tuple[str, ...] = (
    "adservice",
    "adserver",
    "adsterra",
    "adsystem",
    "clickunder",
    "doubleclick",
    "downrightfootball",
    "dtscout",
    "exoclick",
    "google-analytics",
    "googleadservices",
    "googlesyndication",
    "googletagmanager",
    "histats",
    "llvpn",
    "luugy",
    "outbrain",
    "popads",
    "popunder",
    "propeller",
    "rtmark",
    "sead.pages",
    "taboola",
    "telemetry",
    "tracking",
    "wpadmngr",
)

AD_DOMAIN_ALIASES: dict[str, tuple[str, ...]] = {
    "adservice": ("adservice.google.com", "googleadservices.com"),
    "adsystem": ("amazon-adsystem.com",),
    "adsterra": ("adsterra.com",),
    "doubleclick": ("doubleclick.net",),
    "exoclick": ("exoclick.com",),
    "google-analytics": ("google-analytics.com",),
    "googlesyndication": ("googlesyndication.com",),
    "googletagmanager": ("googletagmanager.com",),
    "popads": ("popads.net",),
    "propeller": ("propellerads.com",),
}


@dataclass(frozen=True)
class SourceRanking:
    """One tier of the site's player/server preference order.

    The same tier is described three ways because its two consumers see
    different signals on the page:

    * ``label`` -- the short button text (``BI``/``UP``/``AB``).  Both the
      parsed post page (``player_resolver``) and the live DOM (the injected
      JS) can read it.
    * ``url_markers`` -- lowercase substrings of the embed URL.  This is the
      only signal ``player_resolver`` has besides the label.
    * ``server_ids`` -- the ``data-server`` attribute values.  DOM-only:
      ``player_resolver`` never sees attributes, so its ranking must not
      consult them.

    A tier matches when *any* of its signals matches; ties fall through to
    the next tier, and anything unmatched gets ``SOURCE_RANKING_DEFAULT``.
    """

    rank: int
    label: str
    url_markers: tuple[str, ...]
    server_ids: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        """JSON-ready form consumed by the injected JS."""

        return {
            "rank": self.rank,
            "label": self.label,
            "markers": list(self.url_markers),
            "servers": list(self.server_ids),
        }


# Server-preference order of the site's player buttons.  Before this table
# existed the order lived twice -- as ``player_resolver._rank`` and as a
# hard-coded ``rank`` closure inside online_shield's injected JS -- so adding
# a player meant editing both, and forgetting one silently made the native
# resolver and the in-page auto-picker choose different servers.  Both
# consumers read this table now; edit the order ONLY here.
SOURCE_RANKING: tuple[SourceRanking, ...] = (
    SourceRanking(rank=0, label="BI", url_markers=("bysetayico",), server_ids=("byse",)),
    SourceRanking(rank=1, label="UP", url_markers=("upn.one",), server_ids=("upnshare",)),
    SourceRanking(rank=2, label="AB", url_markers=("abyss",), server_ids=("abyss",)),
)

# Rank assigned to a server that matches no tier; keeps unknown players last.
SOURCE_RANKING_DEFAULT = 9
