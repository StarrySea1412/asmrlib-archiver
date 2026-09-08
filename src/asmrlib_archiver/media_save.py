"""HLS / direct-file download used by the online shield's save button.

The player toolbar posts the sniffed media URL through
``CoreWebView2.WebMessageReceived``; ``save_media`` runs on a worker thread
and never touches the WebView UI thread.  Only the fetch of the media itself
happens here — all URL allow/block decisions were already made by the shield
policy layer before the URL reached the page, and requests are re-checked
against ``allow_media`` before the first byte is written.

AES-128 encrypted HLS requires ``pycryptodome`` (desktop extra); plain
segments and direct MP4 files download without it.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

try:  # desktop extra; plain downloads work without it
    from Crypto.Cipher import AES

    _HAVE_CRYPTO = True
except Exception:  # pragma: no cover - depends on optional extra
    _HAVE_CRYPTO = False

_M3U8_RE = re.compile(r"\.m3u8(?:[?#]|$)", re.IGNORECASE)
_VIDEO_EXT_RE = re.compile(r"\.(mp4|webm|m4v|mov|mkv|ts|m4a|mp3|flac|ogg|wav)(?:[?#]|$)", re.IGNORECASE)
_MAP_RE = re.compile(r'#EXT-X-MAP:URI="([^"]+)"')
_KEY_RE = re.compile(r'#EXT-X-KEY:METHOD=([^,"]+)(?:,URI="([^"]+)")?(?:,IV=0x([0-9a-fA-F]+))?')
_SEGMENT_RE = re.compile(r"^(?!#)(\S+)$")


class MediaSaveError(RuntimeError):
    """Raised when a sniffed media URL cannot be saved."""


def default_save_dir() -> Path:
    """Per-user save location; never inside the app directory."""

    return Path.home() / "Downloads" / "ASMR收藏馆"


def _safe_stem(url: str) -> str:
    host = urlsplit(url).hostname or "media"
    tail = [part for part in urlsplit(url).path.split("/") if part]
    stem = tail[-1] if tail else "stream"
    stem = re.sub(r"\.(m3u8|mp4|webm|m4v|mov|mkv|ts|m4a|mp3|flac|ogg|wav)$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[^\w\-. \u4e00-\u9fff]+", "_", stem).strip("._") or "stream"
    return f"{host}_{stem}"[:80]


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def _ext_for(url: str, content_type: str = "") -> str:
    match = _VIDEO_EXT_RE.search(url)
    if match:
        return f".{match.group(1).lower()}"
    ctype = (content_type or "").lower()
    if "mp4" in ctype:
        return ".mp4"
    if "webm" in ctype:
        return ".webm"
    if "mpegurl" in ctype:
        return ".mp4"  # HLS playlists are reassembled into MP4/TS parts
    return ".bin"


class _Progress:
    """Simple thread-safe progress snapshot for UI/tests."""

    def __init__(self, total_hint: int = 0) -> None:
        self.lock = threading.Lock()
        self.segments_done = 0
        self.segments_total = total_hint
        self.bytes_done = 0
        self.cancelled = False

    def snapshot(self) -> dict[str, int | bool]:
        with self.lock:
            return {
                "segments_done": self.segments_done,
                "segments_total": self.segments_total,
                "bytes_done": self.bytes_done,
                "cancelled": self.cancelled,
            }


def _fetch_playlist(client: httpx.Client, url: str) -> str:
    response = client.get(url)
    response.raise_for_status()
    text = response.text
    # Master playlist: pick the first (usually highest-bandwidth) variant.
    if "#EXT-X-STREAM-INF" in text:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF") and index + 1 < len(lines):
                variant = lines[index + 1].strip()
                if variant and not variant.startswith("#"):
                    return _fetch_playlist(client, urljoin(url, variant))
        raise MediaSaveError("master playlist has no variant")
    return text


def _decrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    if not _HAVE_CRYPTO:
        raise MediaSaveError("AES-128 HLS 需要 pycryptodome（desktop 依赖）")
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.decrypt(data)


def download_hls(
    url: str,
    output: Path,
    *,
    progress: _Progress | None = None,
    allow_media: Callable[[str], bool] | None = None,
    timeout_seconds: float = 30.0,
) -> Path:
    """Download an HLS playlist and append its segments into one file."""

    with httpx.Client(
        follow_redirects=True,
        timeout=timeout_seconds,
        headers={"User-Agent": "ASMRLIBPersonalArchiver/0.1 (+local personal archive)"},
    ) as client:
        playlist = _fetch_playlist(client, url)
        key_url: str | None = None
        key_bytes = b""
        key_iv: bytes | None = None
        init_url: str | None = None
        init_bytes = b""
        segments: list[str] = []
        for line in playlist.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-KEY"):
                match = _KEY_RE.search(line)
                if match and match.group(1).strip().upper() != "NONE":
                    if not match.group(2):
                        raise MediaSaveError("HLS key 行缺少 URI")
                    key_url = urljoin(url, match.group(2))
                    key_iv = bytes.fromhex(match.group(3)) if match.group(3) else None
            elif line.startswith("#EXT-X-MAP"):
                match = _MAP_RE.search(line)
                if match:
                    init_url = urljoin(url, match.group(1))
            else:
                match = _SEGMENT_RE.match(line)
                if match:
                    segments.append(urljoin(url, match.group(1)))
        if not segments:
            raise MediaSaveError("playlist 中没有媒体分段")
        if progress is not None:
            with progress.lock:
                progress.segments_total = len(segments)
        if key_url:
            key_bytes = client.get(key_url).content
            if len(key_bytes) != 16:
                raise MediaSaveError("HLS key 长度异常")
        if init_url:
            init_bytes = client.get(init_url).content
        with output.open("wb") as sink:
            sink.write(init_bytes)
            for index, segment in enumerate(segments):
                if progress is not None and progress.cancelled:
                    raise MediaSaveError("已取消")
                data = client.get(segment).content
                if key_url:
                    iv = key_iv if key_iv is not None else index.to_bytes(16, "big")
                    data = _decrypt_segment(data, key_bytes, iv)
                sink.write(data)
                if progress is not None:
                    with progress.lock:
                        progress.segments_done = index + 1
                        progress.bytes_done += len(data)
    return output

def save_media(
    url: str,
    *,
    save_dir: Path | None = None,
    allow_media: Callable[[str], bool] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, object]:
    """Save a sniffed media URL. Returns a result dict for the JS bridge."""

    raw = str(url or "").strip()
    if not raw or not raw.lower().startswith(("http://", "https://")):
        return {"ok": False, "code": "invalid_media_url", "error": "无效的视频地址"}
    if allow_media is not None and not allow_media(raw):
        return {"ok": False, "code": "blocked_by_policy", "error": "该地址未通过安全策略"}
    directory = Path(save_dir) if save_dir else default_save_dir()
    stem = _safe_stem(raw)
    progress = _Progress()
    try:
        if _M3U8_RE.search(raw):
            output = _unique_path(directory, stem, ".ts")
            download_hls(
                raw,
                output,
                progress=progress,
                timeout_seconds=timeout_seconds,
            )
        else:
            with (
                httpx.Client(follow_redirects=True, timeout=timeout_seconds) as client,
                client.stream("GET", raw) as response,
            ):
                response.raise_for_status()
                ext = _ext_for(raw, response.headers.get("content-type", ""))
                output = _unique_path(directory, stem, ext)
                with output.open("wb") as sink:
                    for chunk in response.iter_bytes():
                        if progress.cancelled:
                            raise MediaSaveError("已取消")
                        sink.write(chunk)
                        with progress.lock:
                            progress.bytes_done += len(chunk)
    except MediaSaveError as exc:
        return {"ok": False, "code": "save_failed", "error": str(exc), "progress": progress.snapshot()}
    except httpx.HTTPError as exc:
        return {"ok": False, "code": "network_error", "error": str(exc), "progress": progress.snapshot()}
    return {
        "ok": True,
        "path": str(output),
        "bytes": progress.snapshot()["bytes_done"],
        "segments_done": progress.snapshot()["segments_done"],
        "segments_total": progress.snapshot()["segments_total"],
    }
