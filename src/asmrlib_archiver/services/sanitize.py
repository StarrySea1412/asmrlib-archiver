from __future__ import annotations

from pathlib import Path

from ..archive_html import is_safe_archive_html, render_detail_archive, render_tag_archive
from ..models import ParsedPage
from .base import ServiceBase


class SanitizeService(ServiceBase):
    """把旧的原始 HTML 替换为安全静态归档。

    遍历 items / tag_pages / 散落 html 文件，逐个重新解析并写回安全版本。
    """

    def run(self) -> tuple[int, int]:
        self.ctx.db.init_schema()
        sanitized = 0
        failed = 0
        known_html_paths: set[Path] = set()
        for row in self.ctx.db.list_archive_items():
            html_path_value = row["html_path"]
            if not html_path_value:
                continue
            html_path = Path(html_path_value).resolve()
            known_html_paths.add(html_path)
            if not html_path.is_file():
                continue
            html = self.ctx.storage.read_text(html_path)
            if is_safe_archive_html(html):
                parsed = self._safe_page_from_metadata(row)
                if parsed is not None:
                    self._save_parse_result(parsed)
                    sanitized += 1
                continue
            try:
                parsed = self.ctx.parser.parse(row["source_url"], html)
                self._save_parse_result(parsed)
                sanitized += 1
            except Exception:
                failed += 1

        for row in self.ctx.db.list_tag_pages():
            html_path_value = row["html_path"]
            if not html_path_value:
                continue
            html_path = Path(html_path_value).resolve()
            known_html_paths.add(html_path)
            if not html_path.is_file():
                continue
            html = self.ctx.storage.read_text(html_path)
            if is_safe_archive_html(html):
                continue
            try:
                parsed = self.ctx.parser.parse_tag_page(row["page_url"], html)
                archive_html = render_tag_archive(parsed)
                self._assert_safe_archive(archive_html)
                self.ctx.storage.write_text(html_path, archive_html)
                sanitized += 1
            except Exception:
                failed += 1

        safe_stub = render_detail_archive(
            ParsedPage(source_url="", title="Sanitized legacy archive")
        )
        for html_path in self.ctx.storage.html_dir.glob("*.html"):
            if html_path.resolve() in known_html_paths:
                continue
            html = self.ctx.storage.read_text(html_path)
            if is_safe_archive_html(html):
                continue
            self.ctx.storage.write_text(html_path, safe_stub)
            sanitized += 1
        return sanitized, failed
