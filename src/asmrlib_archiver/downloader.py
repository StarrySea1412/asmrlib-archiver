from __future__ import annotations

from pathlib import Path

import httpx

from .config import AppConfig
from .guards import BlockedRedirect, UrlGuard
from .storage import Storage


class DownloadError(RuntimeError):
    pass


class SafeDownloader:
    def __init__(self, config: AppConfig, guard: UrlGuard, storage: Storage) -> None:
        self.config = config
        self.guard = guard
        self.storage = storage
        self.client = httpx.Client(
            headers={"User-Agent": config.crawler.user_agent},
            timeout=config.download.timeout_seconds,
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def download(self, media_url: str, title: str) -> Path:
        target = self.guard.assert_media_allowed(media_url)
        final_url, response = self._open_stream(target)
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        content_length = response.headers.get("content-length")
        self._validate_headers(final_url, content_type, content_length)

        output_path = self.storage.media_path(
            title or "media",
            final_url,
            self._ext_from_type(content_type),
        )
        if output_path.exists() and not self.config.download.overwrite:
            response.close()
            return output_path

        tmp_path = output_path.with_suffix(output_path.suffix + ".part")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = self.config.download.max_file_mb * 1024 * 1024
        downloaded = 0
        try:
            with tmp_path.open("wb") as fh:
                for chunk in response.iter_bytes(self.config.download.chunk_size):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        limit = self.config.download.max_file_mb
                        raise DownloadError(f"max_file_size_exceeded: {limit} MB")
                    fh.write(chunk)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        finally:
            response.close()
        tmp_path.replace(output_path)
        return output_path

    def _open_stream(self, url: str) -> tuple[str, httpx.Response]:
        current = url
        for _ in range(self.config.download.max_redirects + 1):
            request = self.client.build_request("GET", current)
            response = self.client.send(request, stream=True)
            if response.is_redirect:
                location = response.headers.get("location", "")
                response.close()
                if not location:
                    raise BlockedRedirect(f"download_redirect_without_location: {current}")
                current = self.guard.assert_redirect_allowed(
                    current,
                    location,
                    media=True,
                    policy=self.config.download.redirect_policy,
                )
                continue
            if response.status_code >= 400:
                status = response.status_code
                response.close()
                raise DownloadError(f"http_status_{status}: {current}")
            return current, response
        raise BlockedRedirect(f"download_too_many_redirects: {url}")

    def _validate_headers(
        self,
        media_url: str,
        content_type: str,
        content_length: str | None,
    ) -> None:
        path = Path(media_url.split("?", 1)[0])
        ext = path.suffix.lower()
        hls_types = {
            "application/vnd.apple.mpegurl",
            "application/x-mpegurl",
        }
        if ext == ".m3u8" or content_type in hls_types:
            raise DownloadError("hls_disabled_in_strict_ad_free_mode")
        if ext and ext not in self.config.download.allowed_extensions:
            raise DownloadError(f"blocked_extension: {ext}")
        if not content_type:
            raise DownloadError("missing_content_type")
        allowed = any(
            content_type.startswith(prefix.lower())
            for prefix in self.config.download.allowed_content_types
        )
        if not allowed:
            raise DownloadError(f"blocked_content_type: {content_type}")
        unproven_stream = (
            content_type == "application/octet-stream"
            and ext not in self.config.download.allowed_extensions
        )
        if unproven_stream:
            raise DownloadError("unproven_octet_stream_media")
        if content_length and content_length.isdigit():
            max_bytes = self.config.download.max_file_mb * 1024 * 1024
            if int(content_length) > max_bytes:
                raise DownloadError(f"content_length_exceeds_limit: {content_length}")

    def _ext_from_type(self, content_type: str) -> str:
        return {
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "audio/aac": ".aac",
            "audio/wav": ".wav",
            "audio/flac": ".flac",
            "application/vnd.apple.mpegurl": ".m3u8",
            "application/x-mpegurl": ".m3u8",
        }.get(content_type, ".bin")
