from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .db import ArchiveDb
from .models import utc_now
from .storage import Storage, safe_filename

SUPPORTED_FORMATS = ("json", "csv", "markdown")


@dataclass(frozen=True)
class ExportResult:
    path: Path
    posts: int
    media_rows: int
    references: int
    downloaded: int


class CatalogExporter:
    """Export archived post metadata and player references.

    This is a catalog of what the archiver already stored. It does not fetch,
    decrypt, or download third-party player content.
    """

    def __init__(self, db: ArchiveDb, storage: Storage) -> None:
        self.db = db
        self.storage = storage

    def export(
        self,
        *,
        fmt: str = "json",
        output: Path | None = None,
        query: str = "",
        tag: str = "",
    ) -> ExportResult:
        fmt_key = fmt.strip().lower()
        if fmt_key not in SUPPORTED_FORMATS:
            raise ValueError(f"unsupported export format: {fmt}")

        posts = self._load_posts(query=query, tag=tag)
        if output is None:
            stamp = utc_now().replace(":", "").replace("+00:00", "Z")
            name = f"catalog_{stamp}.{_extension(fmt_key)}"
            output = self.storage.output_dir / "exports" / name
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)

        media_rows = sum(len(post["media"]) for post in posts)
        references = sum(
            1 for post in posts for item in post["media"] if item["status"] == "reference"
        )
        downloaded = sum(
            1 for post in posts for item in post["media"] if item["status"] == "downloaded"
        )

        if fmt_key == "json":
            self._write_json(output, posts, query=query, tag=tag)
        elif fmt_key == "csv":
            self._write_csv(output, posts)
        else:
            self._write_markdown(output, posts, query=query, tag=tag)

        return ExportResult(
            path=output,
            posts=len(posts),
            media_rows=media_rows,
            references=references,
            downloaded=downloaded,
        )

    def _load_posts(self, *, query: str, tag: str) -> list[dict]:
        rows = self.db.search_items(query=query, tag=tag, limit=100_000, offset=0)
        posts: list[dict] = []
        for row in rows:
            source_url = str(row["source_url"])
            media = []
            for item in self.db.list_media_for_item(source_url):
                media_url = str(item["media_url"])
                media.append(
                    {
                        "id": int(item["id"]),
                        "label": str(item["label"] or ""),
                        "kind": str(item["kind"] or ""),
                        "status": str(item["status"] or ""),
                        "media_url": media_url,
                        "host": _host(media_url),
                        "file_path": str(item["file_path"] or ""),
                        "error": str(item["error"] or ""),
                        "playable_locally": bool(
                            item["status"] == "downloaded"
                            and item["file_path"]
                            and Path(str(item["file_path"])).is_file()
                        ),
                    }
                )
            tags = [
                _tag_slug(str(item["tag_url"]))
                for item in self.db.conn.execute(
                    """
                    SELECT tag_url FROM tag_items
                    WHERE source_url = ?
                    ORDER BY tag_url
                    """,
                    (source_url,),
                )
            ]
            servers = []
            for item in media:
                if item["status"] != "reference":
                    continue
                label = item["label"] or item["host"]
                if label and label not in servers:
                    servers.append(label)
            posts.append(
                {
                    "source_url": source_url,
                    "title": str(row["title"] or ""),
                    "author": str(row["author"] or ""),
                    "published_at": str(row["published_at"] or ""),
                    "status": str(row["status"] or ""),
                    "html_path": str(row["html_path"] or ""),
                    "metadata_path": str(row["metadata_path"] or ""),
                    "tags": tags,
                    "servers": servers,
                    "media": media,
                    "local_media_count": sum(1 for item in media if item["playable_locally"]),
                    "reference_count": sum(1 for item in media if item["status"] == "reference"),
                }
            )
        return posts

    def _write_json(self, path: Path, posts: list[dict], *, query: str, tag: str) -> None:
        server_counter: Counter[str] = Counter()
        for post in posts:
            for server in post["servers"]:
                server_counter[server] += 1
        payload = {
            "generated_at": utc_now(),
            "query": query,
            "tag": tag,
            "summary": {
                "posts": len(posts),
                "media_rows": sum(len(post["media"]) for post in posts),
                "references": sum(post["reference_count"] for post in posts),
                "local_playable": sum(post["local_media_count"] for post in posts),
                "servers": dict(server_counter.most_common()),
            },
            "notes": [
                "Player references are catalog entries only.",
                "This export does not decrypt or download third-party players.",
                "Local playback is available only for status=downloaded files.",
            ],
            "posts": posts,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_csv(self, path: Path, posts: list[dict]) -> None:
        fieldnames = [
            "source_url",
            "title",
            "author",
            "published_at",
            "status",
            "tags",
            "servers",
            "media_label",
            "media_kind",
            "media_status",
            "media_host",
            "media_url",
            "file_path",
            "playable_locally",
            "error",
        ]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for post in posts:
                base = {
                    "source_url": post["source_url"],
                    "title": post["title"],
                    "author": post["author"],
                    "published_at": post["published_at"],
                    "status": post["status"],
                    "tags": "|".join(post["tags"]),
                    "servers": "|".join(post["servers"]),
                }
                if not post["media"]:
                    writer.writerow(
                        {
                            **base,
                            "media_label": "",
                            "media_kind": "",
                            "media_status": "",
                            "media_host": "",
                            "media_url": "",
                            "file_path": "",
                            "playable_locally": "false",
                            "error": "",
                        }
                    )
                    continue
                for item in post["media"]:
                    writer.writerow(
                        {
                            **base,
                            "media_label": item["label"],
                            "media_kind": item["kind"],
                            "media_status": item["status"],
                            "media_host": item["host"],
                            "media_url": item["media_url"],
                            "file_path": item["file_path"],
                            "playable_locally": "true" if item["playable_locally"] else "false",
                            "error": item["error"],
                        }
                    )

    def _write_markdown(self, path: Path, posts: list[dict], *, query: str, tag: str) -> None:
        lines = [
            "# ASMR 收藏馆 Catalog",
            "",
            f"- Generated: `{utc_now()}`",
            f"- Posts: **{len(posts)}**",
            f"- Filter query: `{query or '(none)'}`",
            f"- Filter tag: `{tag or '(none)'}`",
            "",
            "Player references are catalog-only. Third-party players are not downloaded.",
            "",
        ]
        for post in posts:
            title = post["title"] or post["source_url"]
            lines.append(f"## {title}")
            lines.append("")
            lines.append(f"- Source: `{post['source_url']}`")
            if post["published_at"]:
                lines.append(f"- Published: {post['published_at']}")
            if post["author"]:
                lines.append(f"- Author: {post['author']}")
            if post["tags"]:
                lines.append(f"- Tags: {', '.join(post['tags'])}")
            if post["servers"]:
                lines.append(f"- Servers: {', '.join(post['servers'])}")
            lines.append(
                f"- Local files: {post['local_media_count']} · "
                f"References: {post['reference_count']}"
            )
            if post["media"]:
                lines.append("")
                lines.append("| Label | Status | Host | URL / file |")
                lines.append("| --- | --- | --- | --- |")
                for item in post["media"]:
                    target = item["file_path"] if item["playable_locally"] else item["media_url"]
                    lines.append(
                        f"| {item['label'] or item['kind']} | {item['status']} | "
                        f"{item['host']} | `{target}` |"
                    )
            lines.append("")
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _extension(fmt: str) -> str:
    return {"json": "json", "csv": "csv", "markdown": "md"}[fmt]


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _tag_slug(tag_url: str) -> str:
    parts = urlsplit(tag_url)
    path = parts.path.rstrip("/")
    if "/tags/" in path:
        return path.rsplit("/tags/", 1)[-1]
    return safe_filename(tag_url) or tag_url
