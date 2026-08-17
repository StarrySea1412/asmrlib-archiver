from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse


class Storage:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.html_dir = output_dir / "html"
        self.metadata_dir = output_dir / "metadata"
        self.video_dir = output_dir / "videos"

    def ensure(self) -> None:
        self.html_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.video_dir.mkdir(parents=True, exist_ok=True)

    def key_for_url(self, url: str) -> str:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        parsed = urlparse(url)
        stem = safe_filename(parsed.path.strip("/").replace("/", "_") or parsed.netloc)
        return f"{stem}_{digest}"

    def html_path(self, url: str) -> Path:
        return self.html_dir / f"{self.key_for_url(url)}.html"

    def metadata_path(self, url: str) -> Path:
        return self.metadata_dir / f"{self.key_for_url(url)}.json"

    def media_path(self, title: str, media_url: str, fallback_ext: str = ".bin") -> Path:
        parsed = urlparse(media_url)
        ext = Path(parsed.path).suffix.lower() or fallback_ext
        digest = hashlib.sha256(media_url.encode("utf-8")).hexdigest()[:12]
        stem = safe_filename(title) or "media"
        return self.video_dir / f"{stem}_{digest}{ext}"

    def write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)

    def write_json(self, path: Path, payload: dict) -> None:
        self.write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))

    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")


def safe_filename(value: str, max_length: int = 90) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return cleaned[:max_length].strip(" ._")
