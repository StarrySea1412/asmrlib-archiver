from __future__ import annotations

import logging

from ..downloader import SafeDownloader
from .base import ServiceBase

logger = logging.getLogger(__name__)


class DownloadService(ServiceBase):
    """下载 media_candidates 里 pending 的直链媒体到 data/videos/。"""

    def run(self, limit: int = 20, retry_errors: bool = False) -> tuple[int, int]:
        if limit <= 0:
            raise ValueError("Download limit must be positive")
        if not self.ctx.config.download.enabled:
            return 0, 0
        ok = 0
        failed = 0
        rows = self.ctx.db.list_media_for_download(limit, retry_errors=retry_errors)
        total = len(rows)
        self._report(f"Download queue: {total} media file(s).")
        downloader = SafeDownloader(self.ctx.config, self.ctx.guard, self.ctx.storage)
        try:
            for index, row in enumerate(rows, start=1):
                media_url = row["media_url"]
                self._report(f"[media {index}/{total}] Downloading {media_url}")
                try:
                    path = downloader.download(media_url, row["title"] or row["label"])
                    self.ctx.db.update_media_result(
                        row["id"], status="downloaded", file_path=str(path)
                    )
                    ok += 1
                    self._report(f"[media {index}/{total}] Saved {path.name}")
                except Exception as exc:
                    self._log_download_failure(media_url, exc)
                    self.ctx.db.update_media_result(
                        row["id"],
                        status="download_error",
                        error=self._safe_error(exc),
                    )
                    failed += 1
                    self._report(f"[media {index}/{total}] Failed")
        finally:
            downloader.close()
        return ok, failed

    def _log_download_failure(self, media_url: str, exc: Exception) -> None:
        """Distinguish network/HTTP fallout from code bugs in logs.

        HTTP status / blocked / redirect / content-type / size guard failures
        are expected transient or policy outcomes; log them at WARNING. Any
        other exception (AttributeError, KeyError, …) is a code bug and gets
        ERROR + traceback so it isn't hidden behind routine download misses.
        """
        from ..guards import BlockedUrl

        message = str(exc).lower()
        is_expected = isinstance(exc, BlockedUrl) or any(
            marker in message
            for marker in (
                "blocked",
                "timeout",
                "timed out",
                "connectionerror",
                "connection error",
                "too many redirects",
                "redirect",
                "unexpected_",
                "content_type",
                "max_file",
                "too_large",
                "http",
                "404",
                "403",
                "5",
            )
        )
        if is_expected:
            logger.warning(
                "download failure (network/policy) url=%s exc=%s: %s",
                media_url,
                type(exc).__name__,
                exc,
            )
        else:
            logger.exception(
                "download failure (unexpected) url=%s exc=%s: %s",
                media_url,
                type(exc).__name__,
                exc,
            )
