from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..archive_html import is_safe_archive_html, render_detail_archive
from ..models import MediaCandidate, ParsedPage

if TYPE_CHECKING:
    from ..config import AppConfig
    from ..db import ArchiveDb
    from ..guards import UrlGuard
    from ..parser import AsmrlibParser
    from ..robots import RobotsPolicy
    from ..storage import Storage


@dataclass
class ArchiverContext:
    """各 service 共享的依赖 bundle，避免每个 service 接 7 个参数。"""

    config: AppConfig
    db: ArchiveDb
    guard: UrlGuard
    parser: AsmrlibParser
    robots: RobotsPolicy
    storage: Storage
    report: Callable[[str], None] | None


class ServiceBase:
    """所有 service 的基类，提供共享辅助方法。

    子类通过 ``self.ctx`` 访问依赖，通过本类辅助方法做安全校验和保存。
    """

    def __init__(self, ctx: ArchiverContext) -> None:
        self.ctx = ctx

    # --- 进度上报 ---
    def _report(self, message: str) -> None:
        if self.ctx.report is not None:
            self.ctx.report(message)

    # --- 种子分类 ---
    def _classify_seed(
        self,
        url: str,
        page_urls: list[str],
        tag_urls: list[str],
    ) -> None:
        if self.ctx.guard.is_tag_url(url):
            tag_urls.append(self.ctx.guard.canonical_tag_url(url))
            return
        if self.ctx.guard.is_post_url(url):
            page_urls.append(self.ctx.guard.canonical_post_url(url))
            return
        page_urls.append(self.ctx.guard.assert_page_allowed(url))

    # --- 保存解析结果 ---
    def _save_parse_result(self, parsed: ParsedPage) -> None:
        local_media = self._local_media_for_render(parsed.source_url)
        archive_html = render_detail_archive(parsed, local_media=local_media)
        self._assert_safe_archive(archive_html)
        html_path = self.ctx.storage.html_path(parsed.source_url)
        metadata_path = self.ctx.storage.metadata_path(parsed.source_url)
        metadata = asdict(parsed)
        self._assert_safe_metadata(metadata)
        self.ctx.storage.write_text(html_path, archive_html)
        self.ctx.storage.write_json(metadata_path, metadata)
        status = (
            "crawled"
            if any(item.status == "pending" for item in parsed.media)
            else "archived"
        )
        self.ctx.db.update_item_result(
            parsed.source_url,
            title=parsed.title,
            author=parsed.author,
            published_at=parsed.published_at,
            cover=parsed.cover,
            status=status,
            html_path=str(html_path),
            metadata_path=str(metadata_path),
        )
        self.ctx.db.replace_media_candidates(parsed.source_url, parsed.media)

    def _local_media_for_render(self, source_url: str) -> list[dict[str, str]]:
        rendered: list[dict[str, str]] = []
        for row in self.ctx.db.list_media_for_item(source_url):
            if row["status"] != "downloaded" or not row["file_path"]:
                continue
            path = Path(str(row["file_path"]))
            if not path.is_file():
                continue
            try:
                relative = path.resolve().relative_to(self.ctx.storage.output_dir.resolve())
            except ValueError:
                continue
            rendered.append(
                {
                    "label": str(row["label"] or row["kind"] or "media"),
                    "src": relative.as_posix(),
                    "kind": str(row["kind"] or ""),
                }
            )
        return rendered

    def _safe_page_from_metadata(self, row) -> ParsedPage | None:
        metadata_path_value = row["metadata_path"]
        if not metadata_path_value:
            return None
        metadata_path = Path(metadata_path_value)
        if not metadata_path.is_file():
            return None
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None

        needs_cleanup = bool(payload.get("external_links"))
        media: list[MediaCandidate] = []
        raw_media = payload.get("media", [])
        if not isinstance(raw_media, list):
            raw_media = []
            needs_cleanup = True
        for value in raw_media:
            if not isinstance(value, dict):
                needs_cleanup = True
                continue
            media_url = str(value.get("media_url", ""))
            kind = str(value.get("kind", "unknown"))
            if self.ctx.guard.is_ad_url(media_url) or media_url.lower().split("?", 1)[0].endswith(
                ".m3u8"
            ):
                needs_cleanup = True
                continue
            decision = self.ctx.guard.media_decision(media_url)
            if kind == "embed":
                media.append(
                    MediaCandidate(
                        source_url=row["source_url"],
                        media_url=media_url,
                        label=str(value.get("label", ""))[:200],
                        kind=kind,
                        status="reference",
                        error="player_reference_not_downloaded",
                    )
                )
                continue
            if not decision.allowed:
                needs_cleanup = True
                continue
            media.append(
                MediaCandidate(
                    source_url=row["source_url"],
                    media_url=decision.url,
                    label=str(value.get("label", ""))[:200],
                    kind=kind,
                    status="pending",
                )
            )
        if not needs_cleanup:
            return None
        raw_tags = payload.get("tags", [])
        raw_servers = payload.get("servers", [])
        if not isinstance(raw_tags, list):
            raw_tags = []
        if not isinstance(raw_servers, list):
            raw_servers = []
        return ParsedPage(
            source_url=row["source_url"],
            title=str(payload.get("title", "")),
            author=str(payload.get("author", "")),
            published_at=str(payload.get("published_at", "")),
            tags=[str(value) for value in raw_tags],
            servers=[str(value) for value in raw_servers],
            media=media,
            external_links=[],
            text_excerpt=str(payload.get("text_excerpt", "")),
            cover=str(payload.get("cover", "")),
        )

    # --- 错误与校验 ---
    def _safe_error(self, exc: Exception) -> str:
        reason = str(exc).split(":", 1)[0].strip() or "operation_failed"
        safe_reason = "".join(
            character if character.isalnum() or character in {"_", "-"} else "_"
            for character in reason
        )
        return f"{type(exc).__name__}:{safe_reason}"[:200]

    def _validate_html_response(self, content_type: str, content_length: int) -> None:
        if content_length > self.ctx.config.discovery.max_page_bytes:
            raise RuntimeError(f"page_too_large: {content_length}")
        normalized_type = content_type.split(";", 1)[0].strip().lower()
        if not normalized_type:
            raise RuntimeError("missing_page_content_type")
        if normalized_type not in {"text/html", "application/xhtml+xml"}:
            raise RuntimeError(f"unexpected_page_content_type: {normalized_type}")

    def _assert_safe_archive(self, html: str) -> None:
        if not is_safe_archive_html(html):
            raise RuntimeError("archive_safety_check_failed")

    def _assert_safe_metadata(self, metadata: dict) -> None:
        serialized = json.dumps(metadata, ensure_ascii=False).lower()
        forbidden = (
            "downrightfootball.com",
            "wpadmngr.com",
            "histats.com",
            "sead.pages.dev",
            "bit.ly/fulise",
            "googlesyndication",
            "doubleclick",
        )
        if any(value in serialized for value in forbidden):
            raise RuntimeError("metadata_ad_content_detected")
