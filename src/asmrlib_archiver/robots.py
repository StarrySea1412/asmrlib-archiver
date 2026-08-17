from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx


@dataclass
class RobotsCacheEntry:
    parser: RobotFileParser
    fetched: bool


class RobotsPolicy:
    def __init__(self, user_agent: str, timeout_seconds: int, fail_closed: bool = False) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.fail_closed = fail_closed
        self._cache: dict[str, RobotsCacheEntry] = {}

    def can_fetch(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        entry = self._cache.get(origin)
        if entry is None:
            entry = self._fetch(origin)
            self._cache[origin] = entry
        if not entry.fetched and self.fail_closed:
            return False
        return entry.parser.can_fetch(self.user_agent, url)

    def _fetch(self, origin: str) -> RobotsCacheEntry:
        robots_url = f"{origin}/robots.txt"
        parser = RobotFileParser(robots_url)
        try:
            response = httpx.get(robots_url, timeout=self.timeout_seconds, follow_redirects=False)
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
                return RobotsCacheEntry(parser=parser, fetched=True)
        except httpx.HTTPError:
            pass
        parser.parse([])
        return RobotsCacheEntry(parser=parser, fetched=False)

