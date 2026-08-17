from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .config import AppConfig
from .db import ArchiveDb
from .guards import UrlGuard
from .parser import AsmrlibParser
from .robots import RobotsPolicy
from .services import (
    ArchiverContext,
    CrawlService,
    DiscoveryService,
    DiscoveryStats,
    DownloadService,
    SanitizeService,
    SeedService,
)
from .storage import Storage


class Archiver:
    """应用入口 facade。

    持有所有依赖（config / db / guard / parser / robots / storage），
    把具体业务委托给 services 包里的单职责 service。调用方（cli）保持
    原 API 不变。

    业务实现请去 services/，不要在本类里新增业务逻辑。
    """

    def __init__(
        self,
        config: AppConfig,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.storage = Storage(config.output_dir)
        self.db = ArchiveDb(config.database_path)
        self.guard = UrlGuard(
            allowed_domains=config.allowed_domains,
            allow_subdomains=config.allow_subdomains,
            allowed_media_domains=config.download.allowed_media_domains,
            allow_external_media=config.download.allow_external_media,
            ad_keywords=config.browser.block_url_keywords,
        )
        self.parser = AsmrlibParser(self.guard, config.download.allowed_extensions)
        self.robots = RobotsPolicy(
            config.crawler.user_agent,
            config.crawler.timeout_seconds,
            config.crawler.robots_fail_closed,
        )
        self._progress = progress
        self._ctx = ArchiverContext(
            config=config,
            db=self.db,
            guard=self.guard,
            parser=self.parser,
            robots=self.robots,
            storage=self.storage,
            report=progress,
        )
        self._seeds = SeedService(self._ctx)
        self._sanitize = SanitizeService(self._ctx)
        self._discovery = DiscoveryService(self._ctx)
        self._crawl = CrawlService(self._ctx)
        self._download = DownloadService(self._ctx)

    def close(self) -> None:
        self.db.close()

    def init(self) -> None:
        self.storage.ensure()
        self.db.init()

    # --- 种子 ---
    def add_seed_file(self, path: Path) -> int:
        return self._seeds.add_seed_file(path)

    def add_config_seeds(self) -> int:
        return self._seeds.add_config_seeds()

    # --- sanitize ---
    def sanitize_existing(self) -> tuple[int, int]:
        return self._sanitize.run()

    # --- discover ---
    def discover(
        self,
        limit: int | None = None,
        retry_errors: bool = False,
    ) -> DiscoveryStats:
        return self._discovery.run(limit=limit, retry_errors=retry_errors)

    # --- crawl ---
    def crawl(
        self,
        limit: int = 20,
        retry_errors: bool = False,
        *,
        refresh: bool = False,
    ) -> tuple[int, int]:
        return self._crawl.run(limit=limit, retry_errors=retry_errors, refresh=refresh)

    def crawl_covers_only(self, limit: int = 20) -> tuple[int, int]:
        return self._crawl.run_covers_only(limit=limit)

    # --- download ---
    def download(self, limit: int = 20, retry_errors: bool = False) -> tuple[int, int]:
        return self._download.run(limit=limit, retry_errors=retry_errors)

    # --- 状态 ---
    def status(self) -> dict[str, int]:
        self.init()
        return self.db.counts()

    def unresolved(self) -> dict[str, int]:
        self.init()
        return self.db.unresolved_counts()
