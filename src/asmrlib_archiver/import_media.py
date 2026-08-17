from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .db import ArchiveDb
from .storage import Storage, safe_filename


MEDIA_EXTENSIONS = {
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
}


@dataclass(frozen=True)
class ImportResult:
    source_url: str
    media_id: int
    stored_path: Path
    copied: bool
    title: str


class LocalMediaImporter:
    """Attach files you already own so the local library can play them.

    This never contacts third-party players. It only links/copies a local file
    into the archive database (and optionally into data/videos/).
    """

    def __init__(self, db: ArchiveDb, storage: Storage) -> None:
        self.db = db
        self.storage = storage

    def import_file(
        self,
        *,
        source: str,
        file_path: Path,
        label: str = "",
        copy: bool = True,
    ) -> ImportResult:
        item = self._resolve_item(source)
        source_url = str(item["source_url"])
        title = str(item["title"] or source_url)
        path = file_path.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"local_media_missing: {path}")
        if path.suffix.lower() not in MEDIA_EXTENSIONS:
            raise ValueError(f"unsupported_media_extension: {path.suffix or '(none)'}")

        self.storage.ensure()
        if copy:
            digest_name = safe_filename(path.stem) or "media"
            target = self.storage.video_dir / f"{digest_name}_{path.stat().st_size}{path.suffix.lower()}"
            if target.resolve() != path:
                if not target.exists() or target.stat().st_size != path.stat().st_size:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
            stored = target.resolve()
            copied = True
        else:
            stored = path
            copied = False

        media_label = (label or path.stem or "Local file").strip()[:200]
        media_id = self.db.attach_local_media(
            source_url,
            file_path=str(stored),
            label=media_label,
            media_url=f"file://imported/{stored.name}",
            kind="local",
        )
        return ImportResult(
            source_url=source_url,
            media_id=media_id,
            stored_path=stored,
            copied=copied,
            title=title,
        )

    def _resolve_item(self, source: str):
        raw = source.strip()
        if not raw:
            raise ValueError("source_required")

        exact = self.db.get_item(raw)
        if exact is not None:
            return exact

        # Accept bare 32-hex post id.
        if re.fullmatch(r"[0-9a-fA-F]{32}", raw):
            url = f"https://asmrlib.com/posts/{raw.lower()}"
            exact = self.db.get_item(url)
            if exact is not None:
                return exact

        matches = self.db.find_items_by_title_or_url(raw, limit=5)
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"no_matching_post: {raw}")
        preview = ", ".join(
            f"{row['title'] or row['source_url']}" for row in matches[:3]
        )
        raise ValueError(f"ambiguous_post_match: {preview}")
