"""Small URL policy shared by the browser landing page and desktop bridge.

This module deliberately has no browser automation dependency.  It only
normalizes and allowlists URLs before a user explicitly opens them in the
system browser.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


DEFAULT_PLAYER_DOMAINS = (
    "asmrlib.com",
    "bysetayico.com",
    "v.upn.one",
    "upn.one",
    "abyssplayer.com",
    "q8y5z.com",
)

DEFAULT_BLOCKED_HOSTS = (
    "doubleclick.net",
    "googlesyndication.com",
    "google-analytics.com",
    "googletagmanager.com",
    "adservice.google.com",
    "adsterra.com",
    "exoclick.com",
    "popads.net",
    "propellerads.com",
    "histats.com",
    "wpadmngr.com",
    "downrightfootball.com",
    "dtscout.com",
    "llvpn.com",
    "rtmark.net",
    "luugy.com",
    "sead.pages.dev",
)


class PlayerUrlError(ValueError):
    """A stable, user-facing URL validation error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _normalize_host(host: str) -> str:
    value = (host or "").strip().rstrip(".").lower()
    if not value:
        return ""
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise PlayerUrlError("invalid_url", "invalid host") from exc


def _host_matches(host: str, domain: str, allow_subdomains: bool = True) -> bool:
    return host == domain or (allow_subdomains and host.endswith(f".{domain}"))


class ExternalUrlPolicy:
    """Validate URLs that may be handed to the operating-system browser."""

    def __init__(
        self,
        *,
        allowed_domains: list[str] | tuple[str, ...] | None = None,
        allow_subdomains: bool = False,
        blocked_keywords: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        configured = allowed_domains or ("asmrlib.com",)
        self.allowed_domains = tuple(
            _normalize_host(str(value)) for value in configured if str(value).strip()
        )
        self.allow_subdomains = bool(allow_subdomains)
        self.player_domains = tuple(_normalize_host(value) for value in DEFAULT_PLAYER_DOMAINS)
        blocked = {_normalize_host(value) for value in DEFAULT_BLOCKED_HOSTS}
        for value in blocked_keywords or ():
            candidate = str(value).strip().lower().strip(".")
            # Config values such as ``doubleclick`` are intentionally only
            # promoted to host rules when they look like a hostname.  Matching
            # arbitrary path/query text would reject valid post URLs.
            if "." in candidate and "/" not in candidate and " " not in candidate:
                try:
                    blocked.add(_normalize_host(candidate))
                except PlayerUrlError:
                    continue
        self.blocked_hosts = frozenset(value for value in blocked if value)

    def normalize(self, value: str) -> str:
        raw = value.strip() if isinstance(value, str) else ""
        if not raw:
            raise PlayerUrlError("missing_url", "missing url")
        if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in raw):
            raise PlayerUrlError("invalid_url", "url contains whitespace or control characters")
        # Backslashes are accepted by some URL parsers as path separators and
        # can make a displayed host differ from the browser's final host.
        if "\\" in raw:
            raise PlayerUrlError("invalid_url", "url contains a backslash")
        try:
            parsed = urlsplit(raw)
            scheme = parsed.scheme.lower()
            host = _normalize_host(parsed.hostname or "")
            port = parsed.port
        except (PlayerUrlError, ValueError) as exc:
            if isinstance(exc, PlayerUrlError):
                raise
            raise PlayerUrlError("invalid_url", "malformed url") from exc
        if scheme not in {"http", "https"} or not host:
            raise PlayerUrlError("invalid_url", "only http(s) URLs are supported")
        if parsed.username is not None or parsed.password is not None:
            raise PlayerUrlError("invalid_url", "user information is not allowed")
        if port is not None and not (1 <= port <= 65535):
            raise PlayerUrlError("invalid_url", "invalid port")
        host_for_netloc = f"[{host}]" if ":" in host else host
        netloc = host_for_netloc if port is None else f"{host_for_netloc}:{port}"
        # Keep query and fragment because player URLs commonly carry a clip
        # token in either location.  The browser bridge still receives this
        # normalized value, never the unvalidated input.
        return urlunsplit((scheme, netloc, parsed.path, parsed.query, parsed.fragment))

    def validate(self, value: str) -> str:
        normalized = self.normalize(value)
        host = _normalize_host(urlsplit(normalized).hostname or "")
        if any(_host_matches(host, blocked) for blocked in self.blocked_hosts):
            raise PlayerUrlError("blocked_host", "this host is blocked")
        if any(_host_matches(host, domain) for domain in self.player_domains):
            return normalized
        if any(_host_matches(host, domain, self.allow_subdomains) for domain in self.allowed_domains):
            return normalized
        raise PlayerUrlError("host_not_allowed", "host is not on the external player allowlist")


def policy_from_config(config) -> ExternalUrlPolicy:
    """Build a policy without requiring a particular config class."""

    browser = getattr(config, "browser", None)
    return ExternalUrlPolicy(
        allowed_domains=list(getattr(config, "allowed_domains", None) or ("asmrlib.com",)),
        allow_subdomains=bool(getattr(config, "allow_subdomains", False)),
        blocked_keywords=list(getattr(browser, "block_url_keywords", None) or ()),
    )

