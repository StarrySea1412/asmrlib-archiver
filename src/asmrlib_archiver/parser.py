"""解析门面：按 URL 把 HTML 路由给对应站点适配器。

``SiteParser`` 自己不读任何 DOM —— 它只做两件事：用 ``UrlGuard`` 已经持有的
``SiteRegistry`` 找到 URL 所属的适配器，然后委托。这样站点专属的 DOM 契约
（媒体区域、列表卡片、分页形状）与 URL 语法一样是**显式登记**的扩展点。

没有登记过的站点会被**响亮拒绝**：绝不会静默返回一个空的 ``ParsedPage``
让上层以为"抓到了但页面是空的"。这个项目最忌讳假成功。
"""

from __future__ import annotations

from .guards import UrlGuard
from .models import ParsedPage, ParsedSitePage, ParsedTagPage
from .parsing import MEDIA_EXTENSIONS, resolve_extensions
from .sites import SiteAdapter, default_registry

__all__ = ["AsmrlibParser", "MEDIA_EXTENSIONS", "ParseError", "SiteParser"]


class ParseError(RuntimeError):
    """没有任何已登记站点适配器认领这段 HTML。

    刻意不是 ``GuardError``：这不是信任边界违规，而是"这段 DOM 不属于任何
    已知站点"。消息里保留 ``post_layout_error`` / ``tag_layout_error`` 前缀，
    好让 ``crawl`` / ``discovery`` 里已有的"站点改版 vs 代码 bug"日志分类
    继续生效。
    """


class SiteParser:
    """按 URL 路由的解析门面。

    构造签名与改造前的 ``AsmrlibParser`` 一致，``parse`` / ``parse_tag_page``
    / ``parse_site_page`` 的签名也一字未改 —— 4 个构造点与 7 个调用点因此
    不需要改动。
    """

    def __init__(self, guard: UrlGuard, allowed_extensions: list[str]) -> None:
        self.guard = guard
        self.allowed_extensions = resolve_extensions(allowed_extensions)
        # 解析路由必须与信任边界共用同一张登记表：适配器是显式登记的，guard
        # 放行的站点才允许访问。各拿一份会让两边漂移。
        self.registry = getattr(guard, "registry", None) or default_registry()

    def parse(self, source_url: str, html: str) -> ParsedPage:
        adapter = self._adapter(source_url, "post_layout_error")
        parsed = adapter.parse_post(
            source_url, html, guard=self.guard, allowed_extensions=self.allowed_extensions
        )
        if parsed is None:
            raise ParseError(f"post_layout_error:no_site_adapter: {source_url}")
        return parsed

    def parse_tag_page(self, source_url: str, html: str) -> ParsedTagPage:
        adapter = self._adapter(source_url, "tag_layout_error")
        parsed = adapter.parse_listing(
            source_url, html, guard=self.guard, allowed_extensions=self.allowed_extensions
        )
        if parsed is None:
            raise ParseError(f"tag_layout_error:no_site_adapter: {source_url}")
        return parsed

    def parse_site_page(self, source_url: str, html: str) -> ParsedSitePage:
        adapter = self._adapter(source_url, "site_layout_error")
        parsed = adapter.parse_site_page(
            source_url, html, guard=self.guard, allowed_extensions=self.allowed_extensions
        )
        if parsed is None:
            raise ParseError(f"site_layout_error:no_site_adapter: {source_url}")
        return parsed

    def _adapter(self, url: str, layout_code: str) -> SiteAdapter:
        adapter = self.registry.for_url(url)
        if adapter is None:
            raise ParseError(f"{layout_code}:no_site_adapter: {url}")
        return adapter


# 旧类名的别名：crawler / player_resolver / viewer.library / viewer.live 四个
# 构造点，以及既有测试，仍按 ``AsmrlibParser`` 构造门面，无需改动。
AsmrlibParser = SiteParser
