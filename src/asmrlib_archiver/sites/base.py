"""站点适配器的扩展点定义。

这一层描述一个视频源的两件站点专属契约：

1. **URL 语法**：某个站点把帖子、标签、标签分页写成什么形状。
2. **DOM 契约**：某个站点的正文在哪里、媒体候选长什么样、列表卡片与分页
   如何标记。

抓取接线、播放器排线表都不在这里。

信任边界不受影响：适配器只塑造 URL 形态、只读 DOM，绝不授予访问权。域白名单
校验始终由 ``UrlGuard.page_decision`` / ``assert_page_allowed`` 负责，
``UrlGuard`` 在调用适配器之前就已经做过这一步。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol
from urllib.parse import unquote_to_bytes, urlparse

from ..errors import BlockedUrl
from ..models import ParsedPage, ParsedSitePage, ParsedTagPage

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，避免 sites -> guards 成环
    from ..guards import UrlGuard

_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9a-fA-F]{2})")


class SiteAdapter(Protocol):
    """一个视频源的 URL 语法。契约如下：

    所有 ``canonical_*`` 方法接收 **已经过 ``UrlGuard.normalize`` 规范化**的
    绝对 URL，返回规范形态。适配器有两条出口，不要混用：

    - **返回 ``None``** —— "这个 URL 不属于我，或者路径形态不是我认得的"。
      调用方 ``UrlGuard`` 会把它转成既有的通用错误信息
      （``invalid_tag_url_path`` / ``invalid_post_url_path`` /
      ``invalid_tag_page_query``）。别用 ``None`` 表达站点专属的语法违规，
      那会把精确消息折叠掉，丢可诊断性。
    - **抛 ``BlockedUrl``** —— 站点专属的语法违规（例如查询串非法、路径段
      含非法百分号转义）。异常消息会**原样透传**给 ``UrlGuard`` 的调用方，
      所以这里应当直接构造既有的错误串，例如
      ``BlockedUrl(f"invalid_tag_url_query: {url}")``。

    ``build_*`` 方法返回绝对 URL（协议没有 base 参数），以保证
    ``canonical_post_url(build_post_url(x)) == build_post_url(x)``。

    信任边界不在这里：域白名单由 ``UrlGuard.page_decision`` 负责，适配器
    被查到的时候 URL 早已通过校验，因此适配器**不参与授权**。

    三个 ``parse_*`` 方法描述 **DOM 契约**。它们接收 ``guard`` 与
    ``allowed_extensions`` 作为关键字参数（而不是构造参数），这样
    ``default_registry()`` 依旧不需要任何构造参数就能建出适配器。

    返回 ``None`` 表示"这段 HTML 不是我的 / 我解析不了"。``SiteParser`` 会
    把它转成响亮的 ``ParseError``，绝不静默降级成空结果。
    """

    name: str
    domains: tuple[str, ...]

    def canonical_post_url(self, url: str) -> str | None: ...

    def canonical_tag_url(self, url: str) -> str | None: ...

    def canonical_tag_page_url(self, url: str, tag_url: str) -> str | None: ...

    def build_post_url(self, post_id: str) -> str: ...

    def build_tag_url(self, slug: str) -> str: ...

    def parse_post(
        self,
        url: str,
        html: str,
        *,
        guard: UrlGuard,
        allowed_extensions: Sequence[str],
    ) -> ParsedPage | None: ...

    def parse_listing(
        self,
        page_url: str,
        html: str,
        *,
        guard: UrlGuard,
        allowed_extensions: Sequence[str],
    ) -> ParsedTagPage | None: ...

    def parse_site_page(
        self,
        url: str,
        html: str,
        *,
        guard: UrlGuard,
        allowed_extensions: Sequence[str],
    ) -> ParsedSitePage | None: ...


class SiteRegistry:
    """把 URL 路由到适配器的登记表。

    扩展是**显式登记**的：没有登记过的站点一律由 ``for_url`` 返回 ``None``，
    调用方据此拒绝该 URL，而不是放宽任何检查。
    """

    def __init__(self) -> None:
        self._adapters: list[SiteAdapter] = []

    def register(self, adapter: SiteAdapter) -> SiteAdapter:
        self._adapters.append(adapter)
        return adapter

    def for_url(self, url: str) -> SiteAdapter | None:
        host = _hostname(url)
        if not host:
            return None
        for adapter in self._adapters:
            for domain in adapter.domains:
                normalized = _normalize_domain(domain)
                if normalized and (host == normalized or host.endswith(f".{normalized}")):
                    return adapter
        return None

    def all(self) -> tuple[SiteAdapter, ...]:
        return tuple(self._adapters)


def canonical_path_segment(segment: str, label: str) -> str:
    """把单个路径段解码成安全的 NFC 字符串，否则抛 ``BlockedUrl``。"""
    if _INVALID_PERCENT_ESCAPE_RE.search(segment):
        raise BlockedUrl(f"invalid_{label.replace(' ', '_')}: malformed percent escape")
    try:
        decoded = unquote_to_bytes(segment).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BlockedUrl(f"invalid_{label.replace(' ', '_')}: invalid UTF-8") from exc
    decoded = unicodedata.normalize("NFC", decoded)
    if (
        not decoded
        or decoded in {".", ".."}
        or "/" in decoded
        or "\\" in decoded
        or any(ord(character) <= 0x1F or ord(character) == 0x7F for character in decoded)
    ):
        raise BlockedUrl(f"invalid_{label.replace(' ', '_')}: unsafe path segment")
    return decoded


def _hostname(url: str) -> str:
    try:
        return _normalize_domain(urlparse(url).hostname or "")
    except ValueError:
        return ""


def _normalize_domain(domain: str) -> str:
    value = domain.strip().rstrip(".").lower()
    if not value:
        return ""
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
