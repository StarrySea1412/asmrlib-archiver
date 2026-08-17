from __future__ import annotations

from pathlib import Path

from .base import DbConnection
from .items import ItemRepo
from .media import MediaRepo
from .seeds import SeedRepo
from .stats import StatsRepo
from .tag_pages import TagPageRepo


class ArchiveDb(SeedRepo, TagPageRepo, ItemRepo, MediaRepo, StatsRepo):
    """面向调用方的入口 facade。

    通过多继承把 5 个单职责 repo 的方法聚合到一个对象上，调用方
    （crawler / viewer / export / import_media / cli）保持原 API 不变。
    每个Repo 只负责一张或一组紧密相关的表；ArchiveDb 仅负责连接生命周期
    和 schema 初始化。

    注意：不要在子 repo 里 override __init__，连接由本类统一创建。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = DbConnection.open(path)

    def close(self) -> None:
        self.conn.close()

    def init(self) -> None:
        self.init_schema()
