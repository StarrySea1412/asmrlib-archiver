from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from asmrlib_archiver.config import (
    AppConfig,
    CrawlerConfig,
    DiscoveryConfig,
    DownloadConfig,
)
from asmrlib_archiver.db import ArchiveDb
from asmrlib_archiver.guards import UrlGuard
from asmrlib_archiver.parser import AsmrlibParser
from asmrlib_archiver.robots import RobotsPolicy
from asmrlib_archiver.services import (
    ArchiverContext,
    CrawlService,
    DiscoveryService,
    DownloadService,
    SeedService,
)
from asmrlib_archiver.storage import Storage


def _make_ctx(root: Path, **overrides) -> ArchiverContext:
    """直接组装 ArchiverContext，绕过 Archiver facade，证明 service 解耦。"""
    config = AppConfig(
        config_path=root / "config.yaml",
        tag_seeds=overrides.get("tag_seeds", []),
        output_dir=root / "data",
        database_path=root / "data" / "archive.sqlite3",
        crawler=CrawlerConfig(obey_robots=False, delay_seconds=0, retries=0),
        discovery=overrides.get(
            "discovery", DiscoveryConfig(max_pages_per_tag=2, page_batch_size=1)
        ),
        download=overrides.get(
            "download", DownloadConfig(enabled=True, timeout_seconds=1)
        ),
    )
    storage = Storage(config.output_dir)
    storage.ensure()
    db = ArchiveDb(config.database_path)
    db.init()
    guard = UrlGuard(
        allowed_domains=config.allowed_domains,
        allow_subdomains=config.allow_subdomains,
        allowed_media_domains=config.download.allowed_media_domains,
        allow_external_media=config.download.allow_external_media,
        ad_keywords=config.browser.block_url_keywords,
    )
    parser = AsmrlibParser(guard, config.download.allowed_extensions)
    robots = RobotsPolicy(
        config.crawler.user_agent,
        config.crawler.timeout_seconds,
        config.crawler.robots_fail_closed,
    )
    return ArchiverContext(
        config=config,
        db=db,
        guard=guard,
        parser=parser,
        robots=robots,
        storage=storage,
        report=None,
    )


class ServiceStandaloneTests(unittest.TestCase):
    """验证各 service 不依赖 Archiver 类即可独立工作。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_seed_service_adds_tag_seed_without_archiver_class(self) -> None:
        ctx = _make_ctx(self.root, tag_seeds=["https://asmrlib.com/tags/yoonying"])
        try:
            seeds = SeedService(ctx)
            self.assertEqual(seeds.add_config_seeds(), 1)
            queued = ctx.db.list_tag_pages_for_discovery(limit=10)
            self.assertEqual(len(queued), 1)
        finally:
            ctx.db.close()

    def test_discovery_service_returns_empty_stats_when_disabled(self) -> None:
        ctx = _make_ctx(
            self.root,
            discovery=DiscoveryConfig(enabled=False, max_pages_per_tag=1, page_batch_size=1),
        )
        try:
            discovery = DiscoveryService(ctx)
            stats = discovery.run()
            self.assertEqual(stats.pages_ok, 0)
            self.assertEqual(stats.posts_added, 0)
        finally:
            ctx.db.close()

    def test_crawl_service_rejects_non_positive_limit(self) -> None:
        ctx = _make_ctx(self.root)
        try:
            crawl = CrawlService(ctx)
            with self.assertRaises(ValueError):
                crawl.run(limit=0)
        finally:
            ctx.db.close()

    def test_download_service_returns_zero_when_disabled(self) -> None:
        ctx = _make_ctx(self.root, download=DownloadConfig(enabled=False))
        try:
            download = DownloadService(ctx)
            self.assertEqual(download.run(limit=5), (0, 0))
        finally:
            ctx.db.close()

    def test_download_service_rejects_non_positive_limit(self) -> None:
        ctx = _make_ctx(self.root)
        try:
            download = DownloadService(ctx)
            with self.assertRaises(ValueError):
                download.run(limit=0)
        finally:
            ctx.db.close()


if __name__ == "__main__":
    unittest.main()
