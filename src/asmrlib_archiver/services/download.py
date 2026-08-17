from __future__ import annotations

from ..downloader import SafeDownloader
from .base import ServiceBase


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
                self._report(f"[media {index}/{total}] Downloading {row['media_url']}")
                try:
                    path = downloader.download(row["media_url"], row["title"] or row["label"])
                    self.ctx.db.update_media_result(
                        row["id"], status="downloaded", file_path=str(path)
                    )
                    ok += 1
                    self._report(f"[media {index}/{total}] Saved {path.name}")
                except Exception as exc:
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
