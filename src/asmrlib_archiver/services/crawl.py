from __future__ import annotations

import logging

from ..guards import BlockedUrl
from ..http_client import SafeHttpClient
from .base import ServiceBase

logger = logging.getLogger(__name__)


class CrawlService(ServiceBase):
    """抓取 items 表里 pending 的 post 页面，解析后写安全归档。"""

    def run(
        self,
        limit: int = 20,
        retry_errors: bool = False,
        *,
        refresh: bool = False,
    ) -> tuple[int, int]:
        if limit <= 0:
            raise ValueError("Crawl limit must be positive")
        ok = 0
        failed = 0
        rows = self.ctx.db.list_items_for_crawl(
            limit,
            retry_errors=retry_errors,
            refresh=refresh,
        )
        total = len(rows)
        self._report(f"Crawl queue: {total} post(s).")
        client = SafeHttpClient(self.ctx.guard, self.ctx.config.crawler)
        try:
            for index, row in enumerate(rows, start=1):
                source_url = row["source_url"]
                self._report(f"[post {index}/{total}] Fetching {source_url}")
                try:
                    if (
                        self.ctx.config.crawler.obey_robots
                        and not self.ctx.robots.can_fetch(source_url)
                    ):
                        raise RuntimeError(f"robots_disallow: {source_url}")
                    fetch_result = client.fetch_page(source_url)
                    self._validate_html_response(
                        fetch_result.content_type,
                        len(fetch_result.content),
                    )
                    try:
                        effective_url = self.ctx.guard.canonical_post_url(fetch_result.url)
                    except BlockedUrl as exc:
                        raise RuntimeError("post_redirect_mismatch") from exc
                    if effective_url != source_url:
                        raise RuntimeError("post_redirect_mismatch")
                    parsed = self.ctx.parser.parse(source_url, fetch_result.text)
                    if not parsed.title or not parsed.tags:
                        raise RuntimeError("post_layout_error:missing_title_or_tags")
                    if not parsed.media:
                        # Page parsed fine but carries no media references at
                        # all — a real content state, not a crash. Mark it
                        # no_media_found so retry-errors doesn't loop on it
                        # forever, but surface it separately from generic
                        # errors so a site-wide layout change (which usually
                        # drops title/tags too) stays distinguishable.
                        self.ctx.db.update_item_result(
                            source_url,
                            title=parsed.title,
                            author=parsed.author,
                            published_at=parsed.published_at,
                            cover=parsed.cover,
                            status="no_media_found",
                            error="",
                        )
                        failed += 1
                        self._report(
                            f"[post {index}/{total}] No media found {source_url}"
                        )
                        logger.info(
                            "crawl no_media_found url=%s title=%r", source_url, parsed.title
                        )
                        continue
                    self._save_parse_result(parsed)
                    ok += 1
                    self._report(f"[post {index}/{total}] Archived {source_url}")
                except Exception as exc:
                    self._log_crawl_failure(source_url, exc)
                    self.ctx.db.update_item_result(
                        source_url,
                        status="blocked" if "blocked" in str(exc).lower() else "error",
                        error=self._safe_error(exc),
                    )
                    failed += 1
                    self._report(f"[post {index}/{total}] Failed {source_url}")
        finally:
            client.close()
        return ok, failed

    def _log_crawl_failure(self, source_url: str, exc: Exception) -> None:
        """Distinguish code bugs from site layout changes in logs.

        Layout/policy problems are expected fallout from asmrlib changing its
        page structure; they're WARNING-level. Anything else is an unexpected
        code failure and gets the full ERROR treatment with a traceback so a
        real bug isn't lost under the noise of a site revamp.
        """
        message = str(exc).lower()
        is_layout_or_policy = any(
            marker in message
            for marker in (
                "robots_disallow",
                "post_redirect_mismatch",
                "post_layout_error",
                "missing_page_content_type",
                "unexpected_page_content_type",
                "page_too_large",
                "blocked",
            )
        ) or isinstance(exc, BlockedUrl)
        if is_layout_or_policy:
            logger.warning(
                "crawl failure (site/policy) url=%s exc=%s: %s",
                source_url,
                type(exc).__name__,
                exc,
            )
        else:
            logger.exception(
                "crawl failure (unexpected) url=%s exc=%s: %s",
                source_url,
                type(exc).__name__,
                exc,
            )

    def run_covers_only(self, limit: int = 20) -> tuple[int, int]:
        """Backfill only the cover for archived items that have no cover yet.

        Lightweight: fetches the post page, parses it, extracts the cover URL,
        and updates only the items.cover column. Does not rewrite HTML archives
        and does not touch media_candidates.
        """
        if limit <= 0:
            raise ValueError("Crawl limit must be positive")
        rows = self.ctx.db.list_items_without_cover(limit)
        total = len(rows)
        self._report(f"Covers-only crawl queue: {total} post(s).")
        ok = 0
        failed = 0
        client = SafeHttpClient(self.ctx.guard, self.ctx.config.crawler)
        try:
            for index, row in enumerate(rows, start=1):
                source_url = row["source_url"]
                self._report(f"[cover {index}/{total}] Fetching {source_url}")
                try:
                    if (
                        self.ctx.config.crawler.obey_robots
                        and not self.ctx.robots.can_fetch(source_url)
                    ):
                        raise RuntimeError(f"robots_disallow: {source_url}")
                    fetch_result = client.fetch_page(source_url)
                    self._validate_html_response(
                        fetch_result.content_type,
                        len(fetch_result.content),
                    )
                    effective_url = self.ctx.guard.canonical_post_url(fetch_result.url)
                    if effective_url != source_url:
                        raise RuntimeError("post_redirect_mismatch")
                    parsed = self.ctx.parser.parse(source_url, fetch_result.text)
                    cover = parsed.cover
                    if cover:
                        self.ctx.db.update_item_cover(source_url, cover)
                        ok += 1
                        self._report(
                            f"[cover {index}/{total}] Backfilled {source_url} -> {cover}"
                        )
                    else:
                        self._report(
                            f"[cover {index}/{total}] No cover found {source_url}"
                        )
                        failed += 1
                except Exception as exc:  # noqa: PERF203 - one post at a time
                    self.ctx.db.update_item_cover(source_url, "")
                    failed += 1
                    self._report(
                        f"[cover {index}/{total}] Failed {source_url}: "
                        f"{self._safe_error(exc)}"
                    )
        finally:
            client.close()
        return ok, failed
