"""站点适配器扩展点。

``default_registry()`` 返回只含内置适配器（当前只有 asmrlib.com）的登记表。
要支持新的视频源，实现 ``SiteAdapter`` 并显式 ``register`` 即可；没有登记的
站点不会被任何检查放行。
"""

from __future__ import annotations

from .asmrlib import AsmrlibSite
from .base import SiteAdapter, SiteRegistry

__all__ = [
    "AsmrlibSite",
    "SiteAdapter",
    "SiteRegistry",
    "adapter_for_url",
    "default_registry",
]


def default_registry() -> SiteRegistry:
    """内置适配器的登记表。"""
    registry = SiteRegistry()
    registry.register(AsmrlibSite())
    return registry


def adapter_for_url(url: str) -> SiteAdapter | None:
    """便捷入口：在默认登记表里找 ``url`` 所属的适配器。"""
    return default_registry().for_url(url)
