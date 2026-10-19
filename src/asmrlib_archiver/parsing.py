"""站点无关的 DOM 解析工具。

这一层刻意不认识任何具体站点。它只提供两类东西：

- **通用读取链**：标题、作者、发布时间、封面的多级降级规则，以及
  ``clean_text`` / URL 绝对化 / 同源判断这类纯函数。
- **媒体候选的准入判定**：把一条原始媒体 URL 变成 ``MediaCandidate`` 的
  规范化与白名单检查。

**选择器不属于这里**：媒体区域、列表卡片、分页形状是站点专属契约，由
``sites/`` 里的适配器决定，它们再调用本模块。

信任边界不在这里：这些函数只调用 ``UrlGuard`` 已有的
``is_ad_url`` / ``media_decision`` / ``canonical_*``，自己不发明任何放行规则。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from .errors import GuardError
from .models import MediaCandidate

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，避免 parsing -> guards -> sites 成环
    from .guards import UrlGuard

MEDIA_EXTENSIONS = (
    ".mp4",
    ".m4v",
    ".mkv",
    ".webm",
    ".mov",
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".flac",
    ".ogg",
    ".m3u8",
)

# 正文容器的通用降级链。站点可以在调用 ``content_root`` 时覆盖。
DEFAULT_CONTENT_SELECTORS = ("#main", "main", "article")

# 海报/缩略图 CDN 主机。与 security_lists.ASSET_DOMAINS 是两份独立清单，
# 改动其中一份前先核对另一份。
IMAGE_HOSTS = (
    "img-place.com",
    "videothumbs.me",
    "i0.wp.com",
    "i1.wp.com",
    "i2.wp.com",
)

_DATE_RE = re.compile(r"\b(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})\b")
_JSONLD_THUMBNAIL_RE = re.compile(r'"thumbnailUrl"\s*:\s*"([^"]+)"', re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")


def resolve_extensions(allowed_extensions: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """把配置里的扩展名规范化；空配置回落到内置的 ``MEDIA_EXTENSIONS``。"""
    configured = tuple(ext.lower() for ext in allowed_extensions)
    return configured or MEDIA_EXTENSIONS


def clean_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()


def norm_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def absolute_http_url(
    base_url: str,
    raw_url: str,
    preserve_fragment: bool,
) -> str | None:
    """把 ``raw_url`` 相对 ``base_url`` 绝对化；非 http(s) 或含 userinfo 时返回 None。"""
    try:
        parts = urlsplit(urljoin(base_url, raw_url.strip()))
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username or parts.password:
            return None
        port = parts.port
    except (TypeError, ValueError):
        return None

    scheme = parts.scheme.lower()
    host = parts.hostname.lower().rstrip(".")
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if port is None or default_port else f"{host}:{port}"
    fragment = parts.fragment if preserve_fragment else ""
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, fragment))


def same_origin(left: str, right: str) -> bool:
    left_url = absolute_http_url(left, left, False)
    right_url = absolute_http_url(right, right, False)
    if not left_url or not right_url:
        return False
    left_parts = urlsplit(left_url)
    right_parts = urlsplit(right_url)
    return (left_parts.scheme, left_parts.netloc) == (right_parts.scheme, right_parts.netloc)


def looks_like_image(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(_IMAGE_SUFFIXES)


def looks_like_media(url: str, allowed_extensions: tuple[str, ...]) -> bool:
    return urlsplit(url).path.lower().endswith(allowed_extensions)


def image_host_allowed(url: str) -> bool:
    """海报/缩略图专用的第三方主机白名单（同注册域或子域）。"""
    try:
        host = norm_host(urlparse(url).hostname or "")
    except (UnicodeError, ValueError):
        return False
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in IMAGE_HOSTS)


def jsonld_thumbnail(raw: str) -> str:
    """从 ``application/ld+json`` 文本里取 thumbnailUrl，取不到返回 ''。

    用正则而不是完整 JSON 解析：页面 JSON 是机器生成的，但别处一个坏掉的
    script 标签不应该导致封面被跳过。
    """
    match = _JSONLD_THUMBNAIL_RE.search(raw)
    return match.group(1) if match else ""


def content_root(
    soup: BeautifulSoup,
    selectors: tuple[str, ...] = DEFAULT_CONTENT_SELECTORS,
) -> Tag:
    """按降级链找正文容器，找不到就回落到 ``body``。"""
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            return node
    return soup.body or soup


def extract_title(content: Tag, soup: BeautifulSoup) -> str:
    heading = content.select_one("h1")
    if heading:
        value = heading.get_text(" ", strip=True)
        if value:
            return clean_text(value)
    for selector in ["h1", "meta[property='og:title']", "title"]:
        node = soup.select_one(selector)
        if not node:
            continue
        value = (
            node.get("content", "") if node.name == "meta" else node.get_text(" ", strip=True)
        )
        if value:
            return clean_text(value)
    return ""


def extract_author(soup: BeautifulSoup) -> str:
    for selector in [
        "meta[name='author']",
        "meta[property='article:author']",
        "[rel='author']",
    ]:
        node = soup.select_one(selector)
        if not node:
            continue
        value = (
            node.get("content", "") if node.name == "meta" else node.get_text(" ", strip=True)
        )
        if value:
            return value.strip()
    return ""


def extract_published_at(content: Tag) -> str:
    for selector in [
        "time[datetime]",
        "meta[property='article:published_time']",
        "meta[name='date']",
    ]:
        node = content.select_one(selector)
        if not node:
            continue
        value = node.get("datetime") or node.get("content") or node.get_text(" ", strip=True)
        if value:
            return value.strip()
    text = content.get_text("\n", strip=True)
    match = _DATE_RE.search(text)
    return match.group(1).replace("/", "-").replace(".", "-") if match else ""


def extract_cover(
    source_url: str,
    soup: BeautifulSoup,
    content: Tag,
    guard: UrlGuard,
) -> str:
    """从 og:image / JSON-LD / 正文首图里挑一个封面。

    优先级：og:image meta，其次 JSON-LD VideoObject.thumbnailUrl（在 asmrlib
    上这是可靠的单帖封面，正文首图往往是"相关帖子"缩略图），最后回落到正文
    第一个 ``<img>``。

    只接受能通过媒体守卫的 URL（同域或白名单图片主机，绝不含广告主机），
    以保持归档无广告且只存本地。
    """
    candidates: list[str] = []
    for selector in [
        "meta[property='og:image']",
        "meta[property='og:image:url']",
        "meta[property='twitter:image']",
        "meta[name='twitter:image']",
    ]:
        node = soup.select_one(selector)
        if node:
            value = str(node.get("content", "")).strip()
            if value:
                candidates.append(value)

    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = node.string
        if not raw or isinstance(raw, list):
            continue
        value = jsonld_thumbnail(raw)
        if value:
            candidates.append(value)

    img = content.select_one("img[src]")
    if img:
        value = str(img.get("src", "")).strip()
        if value:
            candidates.append(value)

    for raw in candidates:
        normalized = absolute_http_url(source_url, raw, preserve_fragment=False)
        if not normalized or guard.is_ad_url(normalized):
            continue
        decision = guard.media_decision(normalized, source_url)
        # media_decision 总是放行同域，只在 allow_external_media 打开时放行
        # 白名单图片主机。两条路径都要接受，这样 asmrlib 的帖子配上
        # img-place.com 的海报也能通过。
        if (decision.allowed or image_host_allowed(normalized)) and (
            looks_like_image(normalized) or normalized
        ):
            return normalized
    return ""


def canonical_post_url(guard: UrlGuard, source_url: str, raw_url: str) -> str | None:
    """把候选链接规范化成同源帖子 URL；不是帖子或跨域时返回 None。"""
    try:
        candidate = guard.canonical_post_url(raw_url, source_url)
    except GuardError:
        return None
    if not same_origin(source_url, candidate):
        return None
    return candidate


def append_media_candidate(
    candidates: list[MediaCandidate],
    seen: set[str],
    guard: UrlGuard,
    source_url: str,
    raw_url: str,
    label: str,
    kind: str,
    *,
    preserve_fragment: bool = False,
) -> None:
    """把一条原始媒体 URL 变成 ``MediaCandidate``，不合法就静默丢弃。"""
    normalized = absolute_http_url(source_url, raw_url, preserve_fragment)
    if not normalized or normalized in seen:
        return
    if urlsplit(normalized).path.lower().endswith(".m3u8"):
        return
    if guard.is_ad_url(normalized):
        return

    decision = guard.media_decision(raw_url, source_url)
    # 第三方 iframe 播放器永远不是直接下载源。保留成"本地引用"，帖子仍然
    # 能显示服务器与播放器主机。
    if kind == "embed":
        status = "reference"
    elif decision.allowed:
        status = "pending"
    else:
        return

    seen.add(normalized)
    candidates.append(
        MediaCandidate(
            source_url=source_url,
            media_url=normalized,
            label=label[:200],
            kind=kind,
            status=status,
            error="" if status == "pending" else "player_reference_not_downloaded",
        )
    )


def collect_servers(media: list[MediaCandidate]) -> list[str]:
    """从 embed 候选里收集去重后的服务器标签。"""
    servers: list[str] = []
    for candidate in media:
        if candidate.kind != "embed":
            continue
        label = candidate.label.strip()
        if label and label not in servers:
            servers.append(label[:80])
    return servers


def detail_excerpt(
    title: str,
    author: str,
    published_at: str,
    tags: list[str],
    servers: list[str],
) -> str:
    values = [title, author, published_at]
    values.extend(tags)
    values.extend(servers)
    return "\n".join(value for value in values if value)[:1200]
