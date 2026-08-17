from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from .config import CrawlerConfig, DownloadConfig
from .guards import BlockedRedirect, UrlGuard


@dataclass(frozen=True)
class FetchResult:
    url: str
    content: bytes
    content_type: str
    history: list[str]

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class SafeHttpClient:
    def __init__(self, guard: UrlGuard, crawler: CrawlerConfig) -> None:
        self.guard = guard
        self.crawler = crawler
        self.client = httpx.Client(
            headers={"User-Agent": crawler.user_agent},
            timeout=crawler.timeout_seconds,
            follow_redirects=False,
        )
        self._last_request_at = 0.0

    def close(self) -> None:
        self.client.close()

    def fetch_page(self, url: str) -> FetchResult:
        target = self.guard.assert_page_allowed(url)
        attempts = self.crawler.retries + 1
        last_error: Exception | None = None
        for _attempt in range(attempts):
            try:
                return self._get_with_redirect_guard(
                    target,
                    policy=self.crawler.redirect_policy,
                    max_redirects=self.crawler.max_redirects,
                    media=False,
                )
            except (httpx.HTTPError, BlockedRedirect) as exc:
                last_error = exc
        raise RuntimeError(f"fetch_failed: {last_error}")

    def _get_with_redirect_guard(
        self,
        url: str,
        *,
        policy: str,
        max_redirects: int,
        media: bool,
    ) -> FetchResult:
        current = url
        history: list[str] = []
        for _ in range(max_redirects + 1):
            self._respect_delay()
            with self.client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        raise BlockedRedirect(f"redirect_without_location: {current}")
                    next_url = self.guard.assert_redirect_allowed(
                        current,
                        location,
                        media=media,
                        policy=policy,
                    )
                    history.append(f"{current} -> {next_url}")
                    current = next_url
                    continue
                response.raise_for_status()
                content = self._read_limited(response)
                return FetchResult(
                    url=str(response.url),
                    content=content,
                    content_type=response.headers.get("content-type", ""),
                    history=history,
                )
        raise BlockedRedirect(f"too_many_redirects: {url}")

    def _read_limited(self, response: httpx.Response) -> bytes:
        limit = self.crawler.max_response_bytes
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise httpx.HTTPError("page_response_too_large")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit:
                raise httpx.HTTPError("page_response_too_large")
            chunks.append(chunk)
        return b"".join(chunks)

    def _respect_delay(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait_for = self.crawler.delay_seconds - elapsed
        if wait_for > 0:
            time.sleep(wait_for)
        self._last_request_at = time.monotonic()


class SafeDownloadClient:
    def __init__(self, guard: UrlGuard, download: DownloadConfig, user_agent: str) -> None:
        self.guard = guard
        self.download = download
        self.client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=download.timeout_seconds,
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()
