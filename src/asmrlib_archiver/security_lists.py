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
"""

from __future__ import annotations

# Domains that host the actual ASMRLIB site and its known embedded players.
PLAYER_DOMAINS: tuple[str, ...] = (
    "asmrlib.com",
    "bysetayico.com",
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


