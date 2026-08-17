"""Small, dependency-free guard for an online player hosted by WebView2.

The project used to put this behaviour in the Playwright sandbox.  The desktop
application now uses the WebView2 instance supplied by pywebview instead.  This
module intentionally keeps the policy layer independent from pywebview so URL
decisions and the generated document-start script can be tested on any host.

``attach_webview_shield`` accepts either a pywebview ``Window`` or a native
``CoreWebView2``/``WebView2`` object.  All pywebview imports are lazy; importing
this module never requires a GUI runtime.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


DEFAULT_PLAYER_DOMAINS = (
    "asmrlib.com",
    "bysetayico.com",
    "v.upn.one",
    "upn.one",
    "abyssplayer.com",
    "q8y5z.com",
)

DEFAULT_ASSET_DOMAINS = (
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

# Keep this list broad enough to cover the ad hosts historically seen in the
# embedded BI/UP players.  Host matching always uses a label boundary.
DEFAULT_BLOCKED_HOSTS = (
    "doubleclick.net",
    "googlesyndication.com",
    "google-analytics.com",
    "googletagmanager.com",
    "googleadservices.com",
    "adservice.google.com",
    "amazon-adsystem.com",
    "adsterra.com",
    "exoclick.com",
    "popads.net",
    "propellerads.com",
    "histats.com",
    "wpadmngr.com",
    "sead.pages.dev",
    "downrightfootball.com",
    "dtscout.com",
    "llvpn.com",
    "rtmark.net",
    "luugy.com",
    "bit.ly",
    "goo.gl",
    "t.co",
    "tinyurl.com",
)

DEFAULT_BLOCK_URL_KEYWORDS = (
    "doubleclick",
    "googlesyndication",
    "google-analytics",
    "googletagmanager",
    "googleadservices",
    "adservice",
    "adsystem",
    "adserver",
    "adsterra",
    "exoclick",
    "popads",
    "propeller",
    "tracking",
    "telemetry",
    "histats",
    "wpadmngr",
    "downrightfootball",
    "dtscout",
    "llvpn",
    "rtmark",
    "luugy",
    "sead.pages",
    "popunder",
    "clickunder",
    "outbrain",
    "taboola",
)

_MEDIA_CONTEXTS = frozenset(
    {"media", "fetch", "xhr", "websocket", "eventsource", "manifest", "other"}
)
_DOCUMENT_CONTEXTS = frozenset({"document", "iframe", "frame", "subframe"})
_SCRIPT_CONTEXTS = frozenset({"script", "stylesheet", "font", "image"})
_SHIELD_LOADING_HTML = """<!doctype html>
<meta charset="utf-8">
<style>
html,body{height:100%;margin:0;background:#07090d;color:#aab5c5;font:14px/1.5 system-ui,sans-serif}
body{display:grid;place-items:center}.wait{text-align:center}.mark{color:#c4b5fd;font-weight:700;letter-spacing:.12em}
</style><div class="wait"><div class="mark">ASMR 收藏馆</div><div>正在准备安全播放器…</div></div>
"""


class ShieldUrlError(ValueError):
    """Raised when a player URL cannot be safely opened."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_host(host: str | None) -> str:
    """Return a lower-case IDNA host without a trailing dot."""

    value = str(host or "").strip().rstrip(".").lower()
    if not value:
        return ""
    # IPv6 literals are already ASCII and must not be IDNA encoded.
    if ":" in value:
        return value.strip("[]")
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ShieldUrlError("invalid_host", "host is not valid IDNA") from exc


def host_matches(host: str | None, domains: Iterable[str], *, subdomains: bool = True) -> bool:
    """Match a host against domains without allowing suffix look-alikes."""

    value = normalize_host(host)
    if not value:
        return False
    for candidate in domains:
        domain = normalize_host(candidate)
        if value == domain or (subdomains and value.endswith("." + domain)):
            return True
    return False


def url_host(url: str | None) -> str:
    """Extract a normalized hostname, returning an empty string on malformed input."""

    try:
        return normalize_host(urlsplit(str(url or "")).hostname)
    except (ShieldUrlError, ValueError, UnicodeError):
        return ""


def normalize_url(url: str, *, base_url: str | None = None) -> str:
    """Normalize an HTTP(S) URL and reject parser-confusion primitives."""

    raw = str(url or "").strip()
    if base_url and raw:
        # URL joining is deliberately avoided here: player references are
        # expected to be absolute, and accepting ``//other-host`` is unsafe.
        if raw.startswith("/"):
            base = normalize_url(base_url)
            parts = urlsplit(base)
            raw = urlunsplit((parts.scheme, parts.netloc, raw, "", ""))
    if not raw:
        raise ShieldUrlError("missing_url", "missing URL")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in raw):
        raise ShieldUrlError("invalid_url", "URL contains whitespace or control characters")
    if "\\" in raw:
        raise ShieldUrlError("invalid_url", "URL contains a backslash")
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        host = normalize_host(parsed.hostname)
        port = parsed.port
    except (ShieldUrlError, ValueError, UnicodeError) as exc:
        if isinstance(exc, ShieldUrlError):
            raise
        raise ShieldUrlError("invalid_url", "malformed URL") from exc
    if scheme not in {"http", "https"} or not host:
        raise ShieldUrlError("invalid_url", "only HTTP(S) URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise ShieldUrlError("invalid_url", "URL user information is not allowed")
    if port is not None and not 1 <= port <= 65535:
        raise ShieldUrlError("invalid_url", "invalid URL port")
    # Normalize default ports and preserve query/fragment tokens used by UP.
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parsed.path, parsed.query, parsed.fragment))


@dataclass(frozen=True)
class ShieldPolicy:
    """Allow/block policy shared by Python event handlers and injected JS."""

    player_domains: tuple[str, ...] | list[str] = DEFAULT_PLAYER_DOMAINS
    asset_domains: tuple[str, ...] | list[str] = DEFAULT_ASSET_DOMAINS
    allowed_domains: tuple[str, ...] | list[str] = ()
    blocked_hosts: tuple[str, ...] | list[str] = DEFAULT_BLOCKED_HOSTS
    block_url_keywords: tuple[str, ...] | list[str] = DEFAULT_BLOCK_URL_KEYWORDS
    allow_subdomains: bool = True
    allow_external_media: bool = True

    def __post_init__(self) -> None:
        def clean(values: Iterable[str]) -> tuple[str, ...]:
            result: list[str] = []
            for item in values or ():
                try:
                    value = normalize_host(str(item))
                except ShieldUrlError:
                    continue
                if value and value not in result:
                    result.append(value)
            return tuple(result)

        def clean_tokens(values: Iterable[str]) -> tuple[str, ...]:
            result: list[str] = []
            for item in values or ():
                value = str(item).strip().lower()
                if value and value not in result:
                    result.append(value)
            return tuple(result)

        object.__setattr__(self, "player_domains", clean(self.player_domains))
        object.__setattr__(self, "asset_domains", clean(self.asset_domains))
        object.__setattr__(self, "allowed_domains", clean(self.allowed_domains))
        object.__setattr__(self, "blocked_hosts", clean(self.blocked_hosts))
        object.__setattr__(self, "block_url_keywords", clean_tokens(self.block_url_keywords))

    @classmethod
    def from_config(cls, config: Any | None) -> "ShieldPolicy":
        """Build a policy from ``AppConfig`` or a small mapping."""

        if isinstance(config, cls):
            return config
        if isinstance(config, Mapping):
            browser = config.get("browser") or {}
            return cls(
                player_domains=config.get("player_domains", DEFAULT_PLAYER_DOMAINS),
                asset_domains=config.get("asset_domains", DEFAULT_ASSET_DOMAINS),
                allowed_domains=config.get("allowed_domains", ()),
                blocked_hosts=config.get("blocked_hosts", DEFAULT_BLOCKED_HOSTS),
                block_url_keywords=config.get(
                    "block_url_keywords", browser.get("block_url_keywords", DEFAULT_BLOCK_URL_KEYWORDS)
                ),
                allow_subdomains=bool(config.get("allow_subdomains", True)),
                allow_external_media=bool(config.get("allow_external_media", True)),
            )
        browser = getattr(config, "browser", None)
        return cls(
            player_domains=getattr(config, "player_domains", DEFAULT_PLAYER_DOMAINS),
            asset_domains=getattr(config, "asset_domains", DEFAULT_ASSET_DOMAINS),
            allowed_domains=getattr(config, "allowed_domains", ()) or (),
            blocked_hosts=getattr(config, "blocked_hosts", DEFAULT_BLOCKED_HOSTS),
            block_url_keywords=getattr(browser, "block_url_keywords", DEFAULT_BLOCK_URL_KEYWORDS),
            allow_subdomains=bool(getattr(config, "allow_subdomains", True)),
            # Download policy is intentionally stricter than playback policy.
            # Remote players commonly fetch a manifest or media segment from
            # a short-lived CDN host, so do not inherit
            # ``download.allow_external_media`` here. Known advertising hosts
            # and all unapproved external scripts remain blocked below.
            allow_external_media=bool(getattr(config, "online_allow_external_media", True)),
        )

    def is_player_host(self, host: str | None) -> bool:
        return host_matches(host, self.player_domains, subdomains=self.allow_subdomains)

    def is_asset_host(self, host: str | None) -> bool:
        return host_matches(host, self.asset_domains, subdomains=True)

    def is_ad_host(self, host: str | None) -> bool:
        value = normalize_host(host)
        if not value or self.is_player_host(value) or self.is_asset_host(value):
            return False
        return host_matches(value, self.blocked_hosts, subdomains=True)

    def is_ad_url(self, url: str | None) -> bool:
        """Return true for known ad/tracker requests, with host boundaries."""

        raw = str(url or "")
        try:
            parsed = urlsplit(raw)
            host = normalize_host(parsed.hostname)
        except (ShieldUrlError, ValueError, UnicodeError):
            return True
        if not host:
            return True
        if self.is_player_host(host) or self.is_asset_host(host):
            return False
        if self.is_ad_host(host):
            return True
        lowered = raw.lower()
        for token in self.block_url_keywords:
            if not token:
                continue
            if "." in token and "/" not in token and host_matches(host, (token,), subdomains=True):
                return True
            # Bare configured words are matched as URL path/query labels, not
            # arbitrary substrings (``/tags/affiliate`` must remain usable).
            if re.search(r"(?:^|[/?#._=&-])" + re.escape(token) + r"(?:$|[/?#._=&-])", lowered):
                return True
        return False

    def normalize(self, url: str, *, base_url: str | None = None) -> str:
        return normalize_url(url, base_url=base_url)

    def navigation_decision(
        self, url: str, *, current_url: str | None = None, frame: bool = False
    ) -> "ShieldDecision":
        try:
            normalized = self.normalize(url, base_url=current_url)
        except ShieldUrlError as exc:
            return ShieldDecision(False, str(url or ""), exc.code)
        host = url_host(normalized)
        if self.is_ad_url(normalized):
            return ShieldDecision(False, normalized, "blocked_ad_url")
        allowed = self.is_player_host(host)
        if frame:
            allowed = allowed or self.is_asset_host(host) or host_matches(
                host, self.allowed_domains, subdomains=self.allow_subdomains
            )
        else:
            allowed = allowed or host_matches(
                host, self.allowed_domains, subdomains=self.allow_subdomains
            )
        if not allowed:
            return ShieldDecision(False, normalized, "blocked_cross_origin_navigation")
        return ShieldDecision(True, normalized, "allowed")

    def request_decision(
        self,
        url: str,
        *,
        resource_type: str = "other",
        current_url: str | None = None,
    ) -> "ShieldDecision":
        context = _context_name(resource_type)
        if context in _DOCUMENT_CONTEXTS:
            return self.navigation_decision(
                url, current_url=current_url, frame=context != "document"
            )
        try:
            normalized = self.normalize(url, base_url=current_url)
        except ShieldUrlError as exc:
            return ShieldDecision(False, str(url or ""), exc.code)
        if self.is_ad_url(normalized):
            return ShieldDecision(False, normalized, "blocked_ad_url")
        host = url_host(normalized)
        if self.is_player_host(host) or self.is_asset_host(host):
            return ShieldDecision(True, normalized, "allowed_player_asset")
        if host_matches(host, self.allowed_domains, subdomains=self.allow_subdomains):
            return ShieldDecision(True, normalized, "allowed_origin")
        # Media endpoints are often on a short-lived CDN host.  Keep that
        # compatibility while still blocking known ad/tracker hosts.
        if self.allow_external_media and context in _MEDIA_CONTEXTS:
            return ShieldDecision(True, normalized, "allowed_external_media")
        if context in _SCRIPT_CONTEXTS:
            return ShieldDecision(False, normalized, "blocked_external_subresource")
        return ShieldDecision(False, normalized, "blocked_external_request")

    def should_block_request(
        self, url: str, *, resource_type: str = "other", current_url: str | None = None
    ) -> bool:
        return not self.request_decision(
            url, resource_type=resource_type, current_url=current_url
        ).allowed


@dataclass(frozen=True)
class ShieldDecision:
    allowed: bool
    url: str
    reason: str = ""


@dataclass
class ShieldStats:
    """Counters useful for diagnostics and tests."""

    blocked_popups: int = 0
    blocked_ads: int = 0
    blocked_nav: int = 0
    allowed_requests: int = 0
    autoplay_clicks: int = 0
    server_clicks: int = 0
    captcha_clicks: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


# The script is deliberately plain ES5-ish JavaScript so it works in the
# WebView2 versions shipped by older Windows installations as well.  The
# policy JSON is inserted by ``build_page_bootstrap`` and is never executable.
_PAGE_BOOTSTRAP_TEMPLATE = r"""
(() => {
  // Installed with CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync.
  if (window.__asmrlibOnlineShield) return;
  window.__asmrlibOnlineShield = true;
  const policy = __POLICY_JSON__;
  const state = window.__asmrlibShieldState = {
    server: '', captcha: false, userSound: false, lastPlay: 0,
  };
  const hostOf = (value) => {
    try { return new URL(String(value || ''), location.href).hostname.toLowerCase().replace(/\.$/, ''); }
    catch (e) { return ''; }
  };
  const inDomains = (host, list) => {
    if (!host) return false;
    return (list || []).some((d) => host === d || host.endsWith('.' + d));
  };
  const playerHost = (host) => inDomains(host, policy.player_domains);
  const assetHost = (host) => inDomains(host, policy.asset_domains);
  const adUrl = (value) => {
    const raw = String(value || '').toLowerCase();
    const host = hostOf(raw);
    if (!host || playerHost(host) || assetHost(host)) return false;
    if (inDomains(host, policy.blocked_hosts)) return true;
    return (policy.block_url_keywords || []).some((token) => {
      const escaped = String(token).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      return new RegExp('(?:^|[/?#._=&-])' + escaped + '(?:$|[/?#._=&-])', 'i').test(raw);
    });
  };
  const playerUrl = (value) => {
    const host = hostOf(value);
    return playerHost(host);
  };
  const allowedUrl = (value) => {
    const host = hostOf(value);
    if (!host || adUrl(value)) return false;
    return playerHost(host) || assetHost(host) || inDomains(host, policy.allowed_domains);
  };
  const kill = (node) => {
    if (!node || node === document.body || node === document.documentElement) return;
    try { node.remove(); }
    catch (e) { try { node.style.setProperty('display', 'none', 'important'); } catch (_) {} }
  };
  const scrub = (root) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('a[href]').forEach((a) => {
      const href = a.getAttribute('href') || a.href || '';
      if (adUrl(href)) kill(a.closest('div,section,aside,p,li') || a);
    });
    root.querySelectorAll('iframe').forEach((frame) => {
      const src = frame.getAttribute('src') || frame.src || '';
      if (!src || src === 'about:blank') return;
      if (!playerUrl(src)) { kill(frame); return; }
      frame.setAttribute('data-asmrlib-player', '1');
      frame.style.setProperty('display', 'block', 'important');
      frame.style.setProperty('width', '100%', 'important');
      frame.style.setProperty('min-height', '560px', 'important');
      frame.style.setProperty('height', 'min(72vh, 760px)', 'important');
      frame.style.setProperty('border', '0', 'important');
      frame.style.setProperty('background', '#000', 'important');
    });
  };
  const blockNavigation = (event) => {
    try {
      const anchor = event.target && event.target.closest ? event.target.closest('a[href]') : null;
      if (!anchor) return;
      const href = anchor.getAttribute('href') || '';
      if (!href || href[0] === '#' || /^javascript:/i.test(href)) return;
      if (!allowedUrl(href)) {
        event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation();
      } else if (anchor.target === '_blank' || anchor.target === 'blank') {
        anchor.removeAttribute('target');
      }
    } catch (_) {}
  };
  document.addEventListener('click', blockNavigation, true);
  try {
    const originalOpen = window.open;
    const blockedOpen = function () { return null; };
    window.open = blockedOpen;
    Object.defineProperty(window, 'open', { configurable: false, writable: false, value: blockedOpen });
  } catch (_) {}
  try {
    ['assign', 'replace'].forEach((name) => {
      const original = location[name];
      if (typeof original !== 'function') return;
      location[name] = function (value) {
        if (allowedUrl(value)) return original.call(location, value);
      };
    });
  } catch (_) {}
  const pickServer = () => {
    if (state.server) return false;
    const buttons = Array.from(document.querySelectorAll('button[data-url],a[data-url]'));
    const rank = (el) => {
      const server = (el.getAttribute('data-server') || '').toLowerCase();
      const url = (el.getAttribute('data-url') || '').toLowerCase();
      const text = (el.textContent || '').trim().toUpperCase();
      if (server === 'byse' || text === 'BI' || url.indexOf('bysetayico') >= 0) return 0;
      if (server === 'upnshare' || text === 'UP' || url.indexOf('upn.one') >= 0) return 1;
      if (server === 'abyss' || text === 'AB' || url.indexOf('abyss') >= 0) return 2;
      return 9;
    };
    buttons.sort((a, b) => rank(a) - rank(b));
    const best = buttons.find((el) => playerUrl(el.getAttribute('data-url') || ''));
    if (!best) return false;
    try { best.click(); state.server = (best.textContent || '').trim() || 'player'; return true; }
    catch (_) { return false; }
  };
  const play = () => {
    const now = Date.now();
    if (now - state.lastPlay < 1800) return false;
    state.lastPlay = now;
    let acted = false;
    const gate = document.querySelector('.captcha-gate__play, .captcha-gate button, [data-captcha] button');
    if (gate && !state.captcha) { try { gate.click(); state.captcha = true; acted = true; } catch (_) {} }
    document.querySelectorAll('video,audio').forEach((media) => {
      try {
        media.controls = true;
        if (!state.userSound) media.muted = true;
        const p = media.play();
        if (p && p.then) p.then(() => setTimeout(() => {
          if (!state.userSound && !media.paused) { try { media.muted = false; if (!media.volume) media.volume = 1; } catch (_) {} }
        }, 400)).catch(() => {});
        acted = true;
      } catch (_) {}
    });
    document.querySelectorAll('.jw-icon-display,.jw-display-icon-container,.vjs-big-play-button,.plyr__control--overlaid,#player-button-container,#player-button,button[aria-label*="play" i]').forEach((button) => {
      try { button.click(); acted = true; } catch (_) {}
    });
    return acted;
  };
  document.addEventListener('volumechange', (event) => {
    if (event.target && /^(VIDEO|AUDIO)$/.test(event.target.tagName)) state.userSound = true;
  }, true);
  const boot = () => { scrub(document); pickServer(); play(); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true });
  else boot();
  try { new MutationObserver(() => { scrub(document); pickServer(); play(); }).observe(document.documentElement, { childList: true, subtree: true }); } catch (_) {}
  let attempts = 0;
  const timer = setInterval(() => { attempts += 1; scrub(document); pickServer(); play(); if (attempts > 60) clearInterval(timer); }, 600);
})();
"""

# Public aliases retained for callers/tests that used the old private symbol.
_PAGE_BOOTSTRAP = _PAGE_BOOTSTRAP_TEMPLATE


def build_page_bootstrap(policy: ShieldPolicy | Mapping[str, Any] | Any | None = None) -> str:
    """Render the document-start guard script for ``policy``."""

    resolved = ShieldPolicy.from_config(policy)
    payload = {
        "player_domains": list(resolved.player_domains),
        "asset_domains": list(resolved.asset_domains),
        "allowed_domains": list(resolved.allowed_domains),
        "blocked_hosts": list(resolved.blocked_hosts),
        "block_url_keywords": list(resolved.block_url_keywords),
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return _PAGE_BOOTSTRAP_TEMPLATE.replace("__POLICY_JSON__", encoded)


page_bootstrap = build_page_bootstrap
make_page_bootstrap = build_page_bootstrap


def _context_name(value: Any) -> str:
    text = str(value or "").lower()
    # .NET enum string forms are commonly ``CoreWebView2WebResourceContext.Script``.
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    text = text.replace("corewebview2webresourcecontext.", "")
    aliases = {
        "subdocument": "iframe",
        "document": "document",
        "iframe": "iframe",
        "stylesheet": "stylesheet",
        "script": "script",
        "image": "image",
        "font": "font",
        "media": "media",
        "xhr": "xhr",
        "fetch": "fetch",
        "websocket": "websocket",
        "eventsource": "eventsource",
    }
    return aliases.get(text, text)


def _get_nested(obj: Any, *paths: str, default: Any = None) -> Any:
    for path in paths:
        value = obj
        try:
            for piece in path.split("."):
                value = getattr(value, piece)
            if value is not None:
                return value
        except Exception:
            continue
    return default


def _set_flag(obj: Any, name: str, value: Any) -> bool:
    try:
        setattr(obj, name, value)
        return True
    except Exception:
        return False


def _event_add(owner: Any, name: str, handler: Callable[..., Any]) -> Callable[[], None] | None:
    """Subscribe to a Python list/event or a pythonnet event."""

    event = getattr(owner, name, None)
    if event is None:
        return None
    try:
        if hasattr(event, "append"):
            event.append(handler)

            def remove_list() -> None:
                try:
                    event.remove(handler)
                except (ValueError, AttributeError):
                    pass

            return remove_list
        event += handler

        def remove_dotnet() -> None:
            try:
                event -= handler
            except Exception:
                pass

        return remove_dotnet
    except Exception:
        return None


def _resolve_native_targets(window: Any, target: Any = None) -> tuple[Any, Any]:
    """Return ``(core_webview2, webview2_control)`` from common pywebview shapes."""

    roots = [target, window]
    seen: set[int] = set()
    control = None
    core = None
    for root in roots:
        if root is None or id(root) in seen:
            continue
        seen.add(id(root))
        if callable(_get_nested(root, "AddScriptToExecuteOnDocumentCreatedAsync")):
            # A caller may pass CoreWebView2 itself rather than the pywebview
            # control that owns it.
            return root, None
        if _get_nested(root, "CoreWebView2") is not None:
            control = root
            core = _get_nested(root, "CoreWebView2")
            break
        if _get_nested(root, "core_webview2") is not None:
            control = root
            core = _get_nested(root, "core_webview2")
            break
        # pywebview's Windows backend exposes window.gui.webview.
        for path in (
            "gui.webview",
            "_gui.webview",
            "native.browser.webview",
            "native.webview",
            "browser.webview",
            "webview",
        ):
            candidate = _get_nested(root, path)
            if candidate is None:
                continue
            nested_core = _get_nested(candidate, "CoreWebView2", "core_webview2")
            if nested_core is not None:
                return nested_core, candidate
            # It may be the CoreWebView2 object itself.
            if _get_nested(candidate, "AddScriptToExecuteOnDocumentCreatedAsync") is not None:
                return candidate, None
    return core, control


class ShieldBinding:
    """Installed event handlers and script for one native WebView instance."""

    def __init__(self, window: Any, target: Any, policy: ShieldPolicy) -> None:
        self.window = window
        self.target = target
        self.policy = policy
        self.stats = ShieldStats()
        self.script = build_page_bootstrap(policy)
        self.core = None
        self.control = None
        self.installed = False
        self.pending = True
        self._removers: list[Callable[[], None]] = []
        self._script_task = None
        self._current_url = ""

    def install(self) -> bool:
        core, control = _resolve_native_targets(self.window, self.target)
        self.core, self.control = core, control
        if core is None:
            self._defer_to_window_events()
            return False
        if self.installed:
            return True
        # CoreWebView2 methods and event subscriptions are UI-thread APIs.
        # JS bridge calls arrive on a worker thread in pywebview, so marshal
        # the installation through the native WinForms control when needed.
        # ``Window.native`` is an optional proxy and is ``None`` on the
        # stock EdgeChromium backend.  Prefer the actual WinForms WebView2
        # control returned by target resolution: its Invoke/InvokeRequired
        # pair is the one that owns the CoreWebView2 apartment.
        native = self.control or _get_nested(self.window, "native")
        if native is not None and bool(getattr(native, "InvokeRequired", False)):
            invoke = getattr(native, "Invoke", None)
            if callable(invoke):
                try:
                    # WinForms expects a .NET Delegate.  ``Action`` is the
                    # appropriate no-result delegate here; keep a plain
                    # callable fallback for test doubles and non-pythonnet
                    # backends where ``System`` is not importable yet.
                    try:
                        from System import Action

                        def run_on_ui_thread() -> None:
                            self._install_now()

                        callback = Action(run_on_ui_thread)
                    except Exception:
                        callback = self._install_now
                    result = invoke(callback)
                    # Control.Invoke commonly returns ``None`` for an Action.
                    return bool(result) or self.installed
                except Exception:
                    pass
        return self._install_now()

    def _install_now(self) -> bool:
        core = self.core
        control = self.control
        if core is None:
            self._defer_to_window_events()
            return False
        if self.installed:
            return True
        self.pending = False
        self._filter_requests(core)
        self._inject_document_script(core)
        # pywebview's default handler may navigate popups in the same window
        # (or hand them to the system browser). Remove it so the shield's
        # explicit deny policy is the only NewWindowRequested behavior.
        popup_owners = (
            _get_nested(self.window, "native.browser"),
            _get_nested(self.window, "native"),
            _get_nested(self.window, "gui"),
        )
        event = getattr(core, "NewWindowRequested", None)
        if event is not None:
            for owner in popup_owners:
                default_popup = _get_nested(owner, "on_new_window_request")
                if default_popup is None:
                    continue
                try:
                    event -= default_popup
                except Exception:
                    pass
        # NavigationStarting is exposed by the WebView2 control; frame
        # navigation is exposed by CoreWebView2 on newer runtimes.
        if control is not None:
            self._subscribe(control, "NavigationStarting", self._on_navigation)
            self._subscribe(control, "FrameNavigationStarting", self._on_frame_navigation)
            self._subscribe(control, "CoreWebView2InitializationCompleted", self._on_initialized)
        self._subscribe(core, "NavigationStarting", self._on_navigation)
        self._subscribe(core, "FrameNavigationStarting", self._on_frame_navigation)
        self._subscribe(core, "NewWindowRequested", self._on_new_window)
        self._subscribe(core, "WebResourceRequested", self._on_resource)
        self.installed = True
        return True

    def _subscribe(self, owner: Any, name: str, handler: Callable[..., Any]) -> None:
        remover = _event_add(owner, name, handler)
        if remover is not None:
            self._removers.append(remover)

    def _defer_to_window_events(self) -> None:
        events = _get_nested(self.window, "events")
        if events is None:
            return
        for name in ("shown", "loaded"):
            remover = _event_add(events, name, lambda *args: self.install())
            if remover is not None:
                self._removers.append(remover)
        # The initialization event can be reached through a private GUI object
        # before CoreWebView2 itself is available.
        gui = _get_nested(self.window, "gui", "_gui", "native")
        if gui is not None:
            for owner in (gui, _get_nested(gui, "webview")):
                if owner is not None:
                    self._subscribe(owner, "CoreWebView2InitializationCompleted", self._on_initialized)

    def _on_initialized(self, *_args: Any) -> None:
        self.install()

    def _filter_requests(self, core: Any) -> None:
        add_filter = getattr(core, "AddWebResourceRequestedFilter", None)
        if not callable(add_filter):
            return
        try:
            # Importing the enum is optional, which keeps fake/test objects and
            # non-Windows imports working.
            from webview.platforms.edgechromium import CoreWebView2WebResourceContext

            add_filter("*", CoreWebView2WebResourceContext.All)
        except Exception:
            try:
                add_filter("*")
            except Exception:
                pass

    def _inject_document_script(self, core: Any) -> None:
        add_script = getattr(core, "AddScriptToExecuteOnDocumentCreatedAsync", None)
        if callable(add_script):
            try:
                self._script_task = add_script(self.script)
            except Exception:
                self._script_task = None

    def wait_until_ready(self, timeout_seconds: float = 5.0) -> bool:
        """Wait for document-start script registration off the WebView UI thread."""

        if not self.installed:
            return False
        task = self._script_task
        if task is None:
            # Test doubles and older adapters may register synchronously and
            # return no task object.
            return True
        wait = getattr(task, "Wait", None)
        if callable(wait):
            try:
                completed = wait(max(0, int(timeout_seconds * 1000)))
            except Exception:
                return False
            if completed is False:
                return False
        try:
            if bool(getattr(task, "IsFaulted", False)) or bool(
                getattr(task, "IsCanceled", False)
            ):
                return False
        except Exception:
            return False
        return self.installed

    @staticmethod
    def _request_url(args: Any) -> str:
        value = _get_nested(args, "Request.Uri", "Uri", "uri", "Source", default="")
        return str(value or "")

    @staticmethod
    def _resource_type(args: Any) -> str:
        value = _get_nested(args, "ResourceContext", "resource_context", "Context", default="other")
        return _context_name(value)

    def _on_resource(self, _sender: Any, args: Any) -> None:
        url = self._request_url(args)
        context = self._resource_type(args)
        decision = self.policy.request_decision(
            url, resource_type=context, current_url=self._current_url or None
        )
        if decision.allowed:
            self.stats.allowed_requests += 1
            return
        self.stats.blocked_ads += decision.reason == "blocked_ad_url"
        self.stats.blocked_nav += decision.reason.startswith("blocked_") and context in _DOCUMENT_CONTEXTS
        self._block_resource(args)

    def _block_resource(self, args: Any) -> None:
        # WebResourceRequested has no Cancel property in WebView2.  A 403
        # response is the supported way to stop a request; test doubles often
        # expose Cancel, so use it as a fallback.
        response = None
        environment = _get_nested(self.core, "Environment")
        creator = _get_nested(environment, "CreateWebResourceResponse")
        if not callable(creator):
            creator = _get_nested(self.core, "CreateWebResourceResponse")
        if callable(creator):
            try:
                response = creator(None, 403, "Blocked by ASMR shield", "Content-Type: text/plain")
            except Exception:
                response = None
        if response is not None and _set_flag(args, "Response", response):
            return
        _set_flag(args, "Cancel", True)

    def _on_navigation(self, _sender: Any, args: Any) -> None:
        url = str(_get_nested(args, "Uri", "uri", default="") or "")
        decision = self.policy.navigation_decision(url, current_url=self._current_url or None)
        if decision.allowed:
            self._current_url = decision.url
            return
        self.stats.blocked_nav += 1
        _set_flag(args, "Cancel", True)

    def _on_frame_navigation(self, _sender: Any, args: Any) -> None:
        url = str(_get_nested(args, "Uri", "uri", default="") or "")
        decision = self.policy.navigation_decision(
            url, current_url=self._current_url or None, frame=True
        )
        if decision.allowed:
            return
        self.stats.blocked_nav += 1
        _set_flag(args, "Cancel", True)

    def _on_new_window(self, _sender: Any, args: Any) -> None:
        self.stats.blocked_popups += 1
        _set_flag(args, "Handled", True)
        # Some WebView2 versions use lower-case Python properties in wrappers.
        _set_flag(args, "handled", True)

    def detach(self) -> None:
        for remover in reversed(self._removers):
            try:
                remover()
            except Exception:
                pass
        self._removers.clear()
        self.installed = False


def attach_webview_shield(
    window: Any = None,
    target: Any = None,
    policy: ShieldPolicy | Mapping[str, Any] | Any | None = None,
) -> ShieldBinding:
    """Attach request/navigation/popup guards and document-start automation.

    ``target`` may be a native ``CoreWebView2`` or ``WebView2`` control.  When
    omitted, common pywebview paths are inspected.  If initialization has not
    happened yet the returned binding hooks the window's lifecycle events and
    installs itself as soon as CoreWebView2 becomes available.
    """

    binding = ShieldBinding(window, target, ShieldPolicy.from_config(policy))
    binding.install()
    return binding


class GuardedWebViewPlayer:
    """Reusable pywebview online player with the native shield attached."""

    def __init__(
        self,
        policy: ShieldPolicy | Mapping[str, Any] | Any | None = None,
        *,
        title: str = "ASMR 收藏馆 · 在线播放器",
        width: int = 1100,
        height: int = 760,
        on_top: bool = False,
        webview_module: Any | None = None,
    ) -> None:
        self.policy = ShieldPolicy.from_config(policy)
        self.title = title
        self.width = width
        self.height = height
        self.on_top = on_top
        self._webview_module = webview_module
        self.window: Any = None
        self.binding: ShieldBinding | None = None
        self._lock = threading.RLock()

    @property
    def stats(self) -> ShieldStats:
        return self.binding.stats if self.binding is not None else ShieldStats()

    def _module(self) -> Any:
        if self._webview_module is None:
            import webview  # lazy optional dependency

            self._webview_module = webview
        return self._webview_module

    def open(self, url: str) -> dict[str, Any]:
        """Open/reuse the guarded window and return a stable result mapping."""

        try:
            target = self.policy.normalize(url)
        except ShieldUrlError as exc:
            return {"ok": False, "code": exc.code, "error": str(exc)}
        decision = self.policy.navigation_decision(target)
        if not decision.allowed:
            return {"ok": False, "code": decision.reason, "error": decision.reason, "url": target}
        with self._lock:
            if self.window is not None:
                try:
                    if self.binding is None or not self.binding.installed:
                        self.binding = attach_webview_shield(self.window, policy=self.policy)
                    if not self.binding.wait_until_ready():
                        raise RuntimeError("WebView2 shield did not become ready")
                    self.window.load_url(target)
                    for method in ("show", "restore"):
                        callback = getattr(self.window, method, None)
                        if callable(callback):
                            try:
                                callback()
                            except Exception:
                                pass
                    return {"ok": True, "reused": True, "mode": "guarded-webview", "url": target}
                except Exception:
                    if self.binding is not None:
                        self.binding.detach()
                    self.window = None
                    self.binding = None
            created_window = None
            try:
                webview = self._module()
                created_window = webview.create_window(
                    self.title,
                    html=_SHIELD_LOADING_HTML,
                    width=self.width,
                    height=self.height,
                    min_size=(640, 420),
                    on_top=self.on_top,
                    resizable=True,
                    background_color="#07090d",
                )
                self.window = created_window
                # Wait for the local bootstrap document to initialize the
                # native CoreWebView2 object before loading any remote URL.
                events = _get_nested(self.window, "events")
                loaded = getattr(events, "loaded", None) if events is not None else None
                waiter = getattr(loaded, "wait", None)
                if callable(waiter):
                    waiter(5)
                self.binding = attach_webview_shield(self.window, policy=self.policy)
                if not self.binding.wait_until_ready():
                    raise RuntimeError("WebView2 shield did not become ready")
                self.window.load_url(target)
                self._watch_closed(self.window)
                return {"ok": True, "reused": False, "mode": "guarded-webview", "url": target}
            except Exception as exc:
                if self.binding is not None:
                    self.binding.detach()
                self.window = None
                self.binding = None
                if created_window is not None:
                    callback = getattr(created_window, "destroy", None) or getattr(
                        created_window, "close", None
                    )
                    if callable(callback):
                        try:
                            callback()
                        except Exception:
                            pass
                return {"ok": False, "code": "open_failed", "error": str(exc), "url": target}

    def _watch_closed(self, window: Any) -> None:
        events = _get_nested(window, "events")
        if events is None:
            return

        def clear(*_args: Any) -> None:
            with self._lock:
                if self.window is window:
                    self.window = None
                    if self.binding is not None:
                        self.binding.detach()
                    self.binding = None

        _event_add(events, "closed", clear)

    def close(self) -> None:
        with self._lock:
            binding, window = self.binding, self.window
            self.binding = None
            self.window = None
            if binding is not None:
                binding.detach()
        if window is not None:
            callback = getattr(window, "destroy", None) or getattr(window, "close", None)
            if callable(callback):
                try:
                    callback()
                except Exception:
                    pass


# Naming used by a few integrations while the old sandbox was being removed.
OnlineShieldWindow = GuardedWebViewPlayer


__all__ = [
    "DEFAULT_ASSET_DOMAINS",
    "DEFAULT_BLOCKED_HOSTS",
    "DEFAULT_BLOCK_URL_KEYWORDS",
    "DEFAULT_PLAYER_DOMAINS",
    "GuardedWebViewPlayer",
    "OnlineShieldWindow",
    "ShieldBinding",
    "ShieldDecision",
    "ShieldPolicy",
    "ShieldStats",
    "ShieldUrlError",
    "attach_webview_shield",
    "build_page_bootstrap",
    "host_matches",
    "make_page_bootstrap",
    "normalize_host",
    "normalize_url",
    "page_bootstrap",
    "url_host",
    "_PAGE_BOOTSTRAP",
]
