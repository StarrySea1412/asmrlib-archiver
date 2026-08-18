from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote, unquote_to_bytes, urldefrag, urljoin, urlparse, urlunparse

from .security_lists import AD_DOMAIN_ALIASES, BLOCKED_HOSTS as DEFAULT_AD_DOMAINS

# NOTE: DEFAULT_AD_DOMAINS and AD_DOMAIN_ALIASES are imported above from
# security_lists.py, the single source of truth shared with online_shield.py
# and viewer/player_guard.py. Edit the tables there, not here; existing
# importers of this module keep working unchanged.

_TAG_PATH_RE = re.compile(r"^/tags/([^/]+)/?$")
_POST_PATH_RE = re.compile(r"^/posts/([0-9a-fA-F]{32})/?$")
_TAG_PAGE_QUERY_RE = re.compile(r"^page=([1-9][0-9]*)$")
_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9a-fA-F]{2})")


class GuardError(RuntimeError):
    pass


class BlockedUrl(GuardError):
    pass


class BlockedRedirect(GuardError):
    pass


@dataclass(frozen=True)
class UrlDecision:
    allowed: bool
    url: str
    reason: str = ""


class UrlGuard:
    def __init__(
        self,
        allowed_domains: list[str],
        allow_subdomains: bool = False,
        allowed_media_domains: list[str] | None = None,
        allow_external_media: bool = False,
        ad_keywords: list[str] | None = None,
    ) -> None:
        self.allowed_domains = [self._normalize_host(domain) for domain in allowed_domains]
        self.allow_subdomains = allow_subdomains
        self.allow_external_media = allow_external_media
        self.allowed_media_domains = [
            self._normalize_host(domain) for domain in (allowed_media_domains or [])
        ]

        # Keep the public attribute for callers that expose the configured values, but
        # never match these strings against a path or query. A hostname boundary is
        # required to avoid blocking legitimate URLs such as /tags/affiliate.
        self.ad_keywords = [word.lower().strip() for word in (ad_keywords or []) if word.strip()]
        self.ad_domains = set(DEFAULT_AD_DOMAINS)
        for value in self.ad_keywords:
            self.ad_domains.update(AD_DOMAIN_ALIASES.get(value, ()))
            configured_host = self._configured_ad_host(value)
            if configured_host:
                self.ad_domains.add(configured_host)

    def normalize(self, url: str, base_url: str | None = None) -> str:
        value = url.strip()
        if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value):
            raise BlockedUrl("Blocked URL containing whitespace or control characters")
        if base_url:
            value = urljoin(base_url, value)
        value, _fragment = urldefrag(value)
        try:
            parsed = urlparse(value)
            scheme = parsed.scheme.lower()
            host = self._normalize_host(parsed.hostname or "")
            port = parsed.port
        except (UnicodeError, ValueError) as exc:
            raise BlockedUrl(f"Blocked malformed URL: {value}") from exc

        if scheme not in {"http", "https"}:
            raise BlockedUrl(f"Blocked non-http URL scheme: {scheme or '<empty>'}")
        if not host:
            raise BlockedUrl(f"Blocked URL without host: {value}")
        if parsed.username is not None or parsed.password is not None:
            raise BlockedUrl("Blocked URL containing user information")

        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            port = None
        host_for_netloc = f"[{host}]" if ":" in host else host
        netloc = host_for_netloc if port is None else f"{host_for_netloc}:{port}"
        normalized = parsed._replace(scheme=scheme, netloc=netloc, fragment="")
        return urlunparse(normalized)

    def is_ad_url(self, url: str) -> bool:
        try:
            host = self._normalize_host(urlparse(url).hostname or "")
        except (UnicodeError, ValueError):
            return False
        return any(self._host_matches(host, domain) for domain in self.ad_domains)

    def page_decision(self, url: str, base_url: str | None = None) -> UrlDecision:
        try:
            normalized = self.normalize(url, base_url)
        except BlockedUrl as exc:
            return UrlDecision(False, url, str(exc))
        if self.is_ad_url(normalized):
            return UrlDecision(False, normalized, "blocked_ad_url")
        if not self._host_allowed(urlparse(normalized).hostname or "", self.allowed_domains):
            return UrlDecision(False, normalized, "blocked_cross_domain_page")
        return UrlDecision(True, normalized)

    def media_decision(self, url: str, base_url: str | None = None) -> UrlDecision:
        try:
            normalized = self.normalize(url, base_url)
        except BlockedUrl as exc:
            return UrlDecision(False, url, str(exc))
        if self.is_ad_url(normalized):
            return UrlDecision(False, normalized, "blocked_ad_url")

        host = urlparse(normalized).hostname or ""
        if self._host_allowed(host, self.allowed_domains):
            return UrlDecision(True, normalized)
        if self.allow_external_media and self._host_allowed(host, self.allowed_media_domains):
            return UrlDecision(True, normalized)
        return UrlDecision(False, normalized, "blocked_external_media")

    def assert_page_allowed(self, url: str, base_url: str | None = None) -> str:
        decision = self.page_decision(url, base_url)
        if not decision.allowed:
            raise BlockedUrl(f"{decision.reason}: {decision.url}")
        return decision.url

    def assert_media_allowed(self, url: str, base_url: str | None = None) -> str:
        decision = self.media_decision(url, base_url)
        if not decision.allowed:
            raise BlockedUrl(f"{decision.reason}: {decision.url}")
        return decision.url

    def canonical_tag_url(self, url: str, base_url: str | None = None) -> str:
        normalized = self.assert_page_allowed(url, base_url)
        parsed = urlparse(normalized)
        if parsed.params or parsed.query:
            raise BlockedUrl(f"invalid_tag_url_query: {normalized}")
        match = _TAG_PATH_RE.fullmatch(parsed.path)
        if not match:
            raise BlockedUrl(f"invalid_tag_url_path: {normalized}")
        slug = self._canonical_path_segment(match.group(1), "tag slug")
        path = f"/tags/{quote(slug, safe='-._~')}"
        return urlunparse(parsed._replace(path=path, params="", query="", fragment=""))

    def is_tag_url(self, url: str, base_url: str | None = None) -> bool:
        try:
            self.canonical_tag_url(url, base_url)
        except GuardError:
            return False
        return True

    def canonical_post_url(self, url: str, base_url: str | None = None) -> str:
        normalized = self.assert_page_allowed(url, base_url)
        parsed = urlparse(normalized)
        if parsed.params or parsed.query:
            raise BlockedUrl(f"invalid_post_url_query: {normalized}")
        match = _POST_PATH_RE.fullmatch(parsed.path)
        if not match:
            raise BlockedUrl(f"invalid_post_url_path: {normalized}")
        post_id = match.group(1).lower()
        return urlunparse(
            parsed._replace(path=f"/posts/{post_id}", params="", query="", fragment="")
        )

    def is_post_url(self, url: str, base_url: str | None = None) -> bool:
        try:
            self.canonical_post_url(url, base_url)
        except GuardError:
            return False
        return True

    def canonical_tag_page_url(self, url: str, tag_url: str) -> str:
        canonical_tag = self.canonical_tag_url(tag_url)
        normalized = self.assert_page_allowed(url, canonical_tag)
        parsed = urlparse(normalized)
        tag_parsed = urlparse(canonical_tag)

        candidate_without_query = urlunparse(parsed._replace(query="", fragment=""))
        candidate_tag = self.canonical_tag_url(candidate_without_query)
        candidate_parsed = urlparse(candidate_tag)
        if (
            candidate_parsed.scheme != tag_parsed.scheme
            or candidate_parsed.netloc != tag_parsed.netloc
            or candidate_parsed.path != tag_parsed.path
        ):
            raise BlockedUrl(f"tag_page_scope_mismatch: {normalized}")

        if not parsed.query:
            return canonical_tag
        match = _TAG_PAGE_QUERY_RE.fullmatch(parsed.query)
        if not match:
            raise BlockedUrl(f"invalid_tag_page_query: {normalized}")
        page_text = match.group(1)
        if len(page_text) > 18:
            raise BlockedUrl(f"invalid_tag_page_query: {normalized}")
        page_number = int(page_text)
        if page_number == 1:
            return canonical_tag
        return f"{canonical_tag}?page={page_number}"

    def is_tag_page_url(self, url: str, tag_url: str) -> bool:
        try:
            self.canonical_tag_page_url(url, tag_url)
        except GuardError:
            return False
        return True

    def assert_redirect_allowed(
        self,
        current_url: str,
        location: str,
        *,
        media: bool = False,
        policy: str = "same_domain_only",
    ) -> str:
        if policy == "none":
            raise BlockedRedirect(f"redirect_blocked_by_policy: {current_url} -> {location}")
        normalized_current = self.normalize(current_url)
        next_url = self.normalize(location, normalized_current)
        current = urlparse(normalized_current)
        target = urlparse(next_url)
        current_origin = (current.scheme, current.netloc)
        target_origin = (target.scheme, target.netloc)
        if policy == "same_domain_only" and current_origin != target_origin:
            raise BlockedRedirect(
                f"cross_origin_redirect_blocked: {normalized_current} -> {next_url}"
            )
        if media:
            return self.assert_media_allowed(next_url)
        return self.assert_page_allowed(next_url)

    def _host_allowed(self, host: str, domains: list[str]) -> bool:
        normalized_host = self._normalize_host(host)
        for domain in domains:
            if normalized_host == domain:
                return True
            if self.allow_subdomains and normalized_host.endswith(f".{domain}"):
                return True
        return False

    @staticmethod
    def _host_matches(host: str, domain: str) -> bool:
        return host == domain or host.endswith(f".{domain}")

    @staticmethod
    def _normalize_host(host: str) -> str:
        value = host.strip().rstrip(".").lower()
        if not value:
            return ""
        try:
            return value.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise BlockedUrl(f"Blocked malformed host: {host}") from exc

    @classmethod
    def _configured_ad_host(cls, value: str) -> str | None:
        candidate = value
        if "://" in candidate:
            try:
                candidate = urlparse(candidate).hostname or ""
            except ValueError:
                return None
        candidate = candidate.strip().strip(".")
        if "." not in candidate or "/" in candidate:
            return None
        try:
            return cls._normalize_host(candidate)
        except BlockedUrl:
            return None

    @staticmethod
    def _canonical_path_segment(segment: str, label: str) -> str:
        if _INVALID_PERCENT_ESCAPE_RE.search(segment):
            raise BlockedUrl(f"invalid_{label.replace(' ', '_')}: malformed percent escape")
        try:
            decoded = unquote_to_bytes(segment).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BlockedUrl(f"invalid_{label.replace(' ', '_')}: invalid UTF-8") from exc
        decoded = unicodedata.normalize("NFC", decoded)
        if (
            not decoded
            or decoded in {".", ".."}
            or "/" in decoded
            or "\\" in decoded
            or any(ord(character) <= 0x1F or ord(character) == 0x7F for character in decoded)
        ):
            raise BlockedUrl(f"invalid_{label.replace(' ', '_')}: unsafe path segment")
        return decoded
