from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from asmrlib_archiver.config import AppConfig
from asmrlib_archiver.downloader import DownloadError, SafeDownloader
from asmrlib_archiver.guards import UrlGuard
from asmrlib_archiver.storage import Storage


class DownloaderValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        config = AppConfig(
            config_path=root / "config.yaml",
            output_dir=root / "data",
            database_path=root / "data" / "archive.sqlite3",
        )
        guard = UrlGuard(["asmrlib.com"])
        self.downloader = SafeDownloader(config, guard, Storage(config.output_dir))

    def tearDown(self) -> None:
        self.downloader.close()
        self.temp_dir.cleanup()

    def test_accepts_proven_media_headers(self) -> None:
        self.downloader._validate_headers(
            "https://asmrlib.com/media/video.mp4",
            "video/mp4",
            "1024",
        )
        self.downloader._validate_headers(
            "https://asmrlib.com/media/audio.mp3",
            "application/octet-stream",
            None,
        )

    def test_rejects_missing_or_non_media_content_type(self) -> None:
        with self.assertRaisesRegex(DownloadError, "missing_content_type"):
            self.downloader._validate_headers(
                "https://asmrlib.com/media/video.mp4",
                "",
                None,
            )
        with self.assertRaisesRegex(DownloadError, "blocked_content_type"):
            self.downloader._validate_headers(
                "https://asmrlib.com/media/video.mp4",
                "text/html",
                None,
            )

    def test_rejects_unproven_octet_stream(self) -> None:
        with self.assertRaisesRegex(DownloadError, "unproven_octet_stream_media"):
            self.downloader._validate_headers(
                "https://asmrlib.com/media/download",
                "application/octet-stream",
                None,
            )

    def test_rejects_hls_manifests_in_strict_mode(self) -> None:
        with self.assertRaisesRegex(DownloadError, "hls_disabled"):
            self.downloader._validate_headers(
                "https://asmrlib.com/media/playlist.m3u8",
                "application/vnd.apple.mpegurl",
                None,
            )


if __name__ == "__main__":
    unittest.main()
