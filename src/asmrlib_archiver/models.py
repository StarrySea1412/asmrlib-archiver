from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class MediaCandidate:
    source_url: str
    media_url: str
    label: str = ""
    kind: str = "unknown"
    status: str = "pending"
    error: str = ""


@dataclass(frozen=True)
class ParsedPage:
    source_url: str
    title: str = ""
    author: str = ""
    published_at: str = ""
    tags: list[str] = field(default_factory=list)
    servers: list[str] = field(default_factory=list)
    media: list[MediaCandidate] = field(default_factory=list)
    external_links: list[str] = field(default_factory=list)
    text_excerpt: str = ""
    cover: str = ""


@dataclass(frozen=True)
class ParsedTagPage:
    source_url: str
    title: str = ""
    post_urls: list[str] = field(default_factory=list)
    next_page_url: str = ""
    text_excerpt: str = ""

    @property
    def next_pages(self) -> list[str]:
        return [self.next_page_url] if self.next_page_url else []


@dataclass(frozen=True)
class SitePostCard:
    """One post tile from the asmrlib homepage / listing grid (live browse only)."""

    source_url: str
    title: str = ""
    cover: str = ""
    published_at: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedSitePage:
    """asmrlib.com homepage (or ?page=N) listing — not a tag page."""

    source_url: str
    title: str = ""
    posts: list[SitePostCard] = field(default_factory=list)
    next_page_url: str = ""
    prev_page_url: str = ""
    page_number: int = 1
