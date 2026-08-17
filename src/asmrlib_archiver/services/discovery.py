from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..archive_html import render_tag_archive
from ..guards import BlockedUrl
from ..http_client import SafeHttpClient
from .base import ServiceBase


@dataclass
class DiscoveryStats:
    pages_ok: int = 0
    pages_failed: int = 0
    posts_added: int = 0
    duplicate_pages: int = 0
    incomplete: int = 0


class DiscoveryService(ServiceBase):
    """从配置的 /tags/<slug> 页面发现帖子，写入 items + tag_items。"""

    def run(
        self,
        limit: int | None = None,
        retry_errors: bool = False,
    ) -> DiscoveryStats:
        stats = DiscoveryStats()
        if not self.ctx.config.discovery.enabled:
            return stats

        tag_count = max(1, self.ctx.db.counts().get("tags.total", 0))
        budget = limit or self.ctx.config.discovery.max_pages_per_tag * tag_count
        if budget <= 0:
            raise ValueError("Discovery limit must be positive")

        client = SafeHttpClient(self.ctx.guard, self.ctx.config.crawler)
        attempted: set[str] = set()
        processed = 0
        try:
            while stats.pages_ok + stats.pages_failed < budget:
                remaining = budget - stats.pages_ok - stats.pages_failed
                batch_limit = min(self.ctx.config.discovery.page_batch_size, remaining)
                rows = self.ctx.db.list_tag_pages_for_discovery(
                    batch_limit,
                    retry_errors=retry_errors,
                    exclude_page_urls=attempted,
                )
                if not rows:
                    break
                for row in rows:
                    processed += 1
                    attempted.add(row["page_url"])
                    self._report(
                        f"[tag {processed}/{budget} max] Fetching {row['page_url']}"
                    )
                    self._discover_tag_page(client, row, stats)
                    self._report(
                        f"[tag {processed}/{budget} max] Finished "
                        f"ok={stats.pages_ok} failed={stats.pages_failed}"
                    )
        finally:
            client.close()

        counts = self.ctx.db.counts()
        unresolved_pages = counts.get("tag_pages.pending", 0) + counts.get(
            "tag_pages.incomplete", 0
        )
        stats.incomplete = max(stats.incomplete, unresolved_pages)
        return stats

    def _discover_tag_page(self, client: SafeHttpClient, row, stats: DiscoveryStats) -> None:
        tag_url = row["tag_url"]
        page_url = row["page_url"]
        try:
            current_posts = len(self.ctx.db.list_tag_items(tag_url))
            page_count = self.ctx.db.count_tag_pages(tag_url)
            limits_reached = (
                current_posts >= self.ctx.config.discovery.max_posts_per_tag
                or page_count > self.ctx.config.discovery.max_pages_per_tag
            )
            if limits_reached:
                self.ctx.db.complete_tag_page(
                    tag_url,
                    page_url,
                    post_urls=[],
                    next_page_url=row["next_page_url"],
                    status="incomplete",
                    enqueue_next=False,
                )
                stats.incomplete += 1
                stats.pages_ok += 1
                return

            if self.ctx.config.crawler.obey_robots and not self.ctx.robots.can_fetch(page_url):
                raise RuntimeError(f"robots_disallow: {page_url}")

            fetch_result = client.fetch_page(page_url)
            self._validate_html_response(fetch_result.content_type, len(fetch_result.content))
            try:
                effective_url = self.ctx.guard.canonical_tag_page_url(fetch_result.url, tag_url)
            except BlockedUrl as exc:
                raise RuntimeError("tag_redirect_mismatch") from exc
            if effective_url != page_url:
                raise RuntimeError("tag_redirect_mismatch")
            parsed = self.ctx.parser.parse_tag_page(page_url, fetch_result.text)
            if not parsed.post_urls:
                raise RuntimeError("tag_layout_error:no_posts")
            archive_html = render_tag_archive(parsed)
            self._assert_safe_archive(archive_html)
            html_path = self.ctx.storage.html_path(page_url)
            self.ctx.storage.write_text(html_path, archive_html)

            post_urls = parsed.post_urls
            truncated = (
                current_posts + len(post_urls) > self.ctx.config.discovery.max_posts_per_tag
            )
            if parsed.next_page_url and page_count >= self.ctx.config.discovery.max_pages_per_tag:
                truncated = True
            status = "incomplete" if truncated else "complete"
            fingerprint = "\n".join([*post_urls, parsed.next_page_url]).encode("utf-8")
            completion = self.ctx.db.complete_tag_page(
                tag_url,
                page_url,
                post_urls=post_urls,
                next_page_url=parsed.next_page_url,
                content_hash=hashlib.sha256(fingerprint).hexdigest(),
                html_path=str(html_path),
                status=status,
                enqueue_next=not truncated,
            )
            stats.pages_ok += 1
            stats.posts_added += completion.items_added
            stats.duplicate_pages += int(bool(completion.duplicate_of))
            stats.incomplete += int(truncated)
        except Exception as exc:
            blocked = isinstance(exc, BlockedUrl) or "blocked" in str(exc).lower()
            status = "blocked" if blocked else "error"
            self.ctx.db.mark_tag_page_failed(
                tag_url,
                page_url,
                status=status,
                error=self._safe_error(exc),
            )
            stats.pages_failed += 1
