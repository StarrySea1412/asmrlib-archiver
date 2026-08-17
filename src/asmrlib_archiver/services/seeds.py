from __future__ import annotations

from pathlib import Path

from .base import ServiceBase


class SeedService(ServiceBase):
    """种子入库：把 config.seeds / config.tag_seeds / 种子文件解析进 db。"""

    def add_seed_file(self, path: Path) -> int:
        page_urls: list[str] = []
        tag_urls: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            self._classify_seed(stripped, page_urls, tag_urls)
        return self.ctx.db.add_seeds(page_urls) + self.ctx.db.add_tag_seeds(tag_urls)

    def add_config_seeds(self) -> int:
        page_urls: list[str] = []
        tag_urls: list[str] = []
        for url in self.ctx.config.seeds:
            self._classify_seed(url, page_urls, tag_urls)
        for url in self.ctx.config.tag_seeds:
            tag_urls.append(self.ctx.guard.canonical_tag_url(url))
        return self.ctx.db.add_seeds(page_urls) + self.ctx.db.add_tag_seeds(tag_urls)
