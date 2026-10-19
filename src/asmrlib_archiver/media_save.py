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

import contextlib
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
_HTML_BODY_RE = re.compile(r"^\s*<(?:!doctype|html)\b", re.IGNORECASE)
_MAX_REDIRECTS = 5


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


def _is_html_content_type(content_type: str) -> bool:
    """True when a response advertises itself as a web page, not media."""

    ctype = (content_type or "").lower()
    return "text/html" in ctype or "application/xhtml" in ctype


def _is_supported_media_content_type(content_type: str) -> bool:
    ctype = (content_type or "").lower()
    return (
        ctype.startswith("video/")
        or ctype.startswith("audio/")
        or "mpegurl" in ctype
        or ctype == "application/octet-stream"
    )


def _body_looks_like_html(body: bytes) -> bool:
    """Sniff the leading bytes: an HTML page is not media data.

    A 200 ``text/html`` ad interstitial or error page must never be appended to
    the output as if it were a segment — that is the fake-success failure mode.
    """

    try:
        head = body[:512].decode("utf-8", "ignore")
    except Exception:  # pragma: no cover - errors='ignore' never raises
        return False
    return bool(_HTML_BODY_RE.match(head))


def _reject_html(body: bytes, content_type: str, what: str) -> None:
    if _is_html_content_type(content_type) or _body_looks_like_html(body):
        raise MediaSaveError(f"HLS {what}返回了 HTML 而非媒体数据")


def _get(
    client: httpx.Client,
    url: str,
    *,
    allow_media: Callable[[str], bool] | None = None,
    timeout_seconds: float = 30.0,
) -> httpx.Response:
    """GET ``url`` without letting httpx follow redirects behind our back.

    The client is built with ``follow_redirects=False``; every hop is resolved
    and re-checked against ``allow_media`` *before* it is requested. Otherwise a
    segment on an allowed host could 302 the download to a host the policy layer
    would have refused, which is an access-control bypass. Hops are capped so a
    redirect loop fails instead of spinning forever.
    """

    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        response = client.get(current, follow_redirects=False, timeout=timeout_seconds)
        if not response.is_redirect:
            response.raise_for_status()
            return response
        location = response.headers.get("location")
        if not location:
            raise MediaSaveError(f"重定向缺少 Location：{current}")
        target = urljoin(str(response.url), location)
        if allow_media is not None and not allow_media(target):
            raise MediaSaveError(f"重定向目标未通过安全策略：{target}")
        current = target
    raise MediaSaveError(f"重定向次数过多：{url}")


@contextlib.contextmanager
def _stream_get(
    client: httpx.Client,
    url: str,
    *,
    allow_media: Callable[[str], bool] | None = None,
    timeout_seconds: float = 30.0,
):
    """Stream ``url``, re-checking every redirect hop against ``allow_media``.

    The direct-file branch needs a streamed body for progress reporting, so it
    cannot use ``_get``.  It must not use httpx's auto-redirect either: the same
    bypass applies here — an allowed host can 302 a direct MP4 to a host the
    policy layer would have refused.
    """

    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        with client.stream(
            "GET", current, follow_redirects=False, timeout=timeout_seconds
        ) as response:
            if not response.is_redirect:
                response.raise_for_status()
                yield response
                return
            location = response.headers.get("location")
            target = urljoin(str(response.url), location) if location else ""
            if not target:
                raise MediaSaveError(f"重定向缺少 Location：{current}")
            if allow_media is not None and not allow_media(target):
                raise MediaSaveError(f"重定向目标未通过安全策略：{target}")
            current = target
    raise MediaSaveError(f"重定向次数过多：{url}")


def _fetch_playlist(
    client: httpx.Client,
    url: str,
    *,
    allow_media: Callable[[str], bool] | None = None,
    timeout_seconds: float = 30.0,
) -> tuple[str, str]:
    """Fetch a media playlist and return its text plus its effective base URL."""

    response = _get(client, url, allow_media=allow_media, timeout_seconds=timeout_seconds)
    text = response.text
    # Master playlists often point to a subdirectory. Returning the effective
    # variant URL is essential: resolving segments against the original master
    # silently creates wrong paths and looks like a random network failure.
    if "#EXT-X-STREAM-INF" in text:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF") and index + 1 < len(lines):
                variant = lines[index + 1].strip()
                if variant and not variant.startswith("#"):
                    return _fetch_playlist(
                        client,
                        urljoin(str(response.url), variant),
                        allow_media=allow_media,
                        timeout_seconds=timeout_seconds,
                    )
        raise MediaSaveError("master playlist has no variant")
    return text, str(response.url)


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
        # Redirects are followed explicitly by _get so that every hop is
        # re-checked against allow_media. Letting httpx auto-follow here would
        # silently bypass the policy for a segment that 302s off-host.
        follow_redirects=False,
        timeout=timeout_seconds,
        # trust_env: without it httpx ignores HTTP(S)_PROXY, so on a machine
        # that only reaches the internet through a proxy every save hangs
        # until it times out. urllib (the cover proxy) reads the environment
        # by default, which is why covers worked and saves did not.
        trust_env=True,
        headers={"User-Agent": "ASMRLIBPersonalArchiver/0.1 (+local personal archive)"},
    ) as client:
        playlist, playlist_url = _fetch_playlist(
            client, url, allow_media=allow_media, timeout_seconds=timeout_seconds
        )
        key_url: str | None = None
        key_bytes = b""
        key_iv: bytes | None = None
        init_url: str | None = None
        init_bytes = b""
        segments: list[str] = []
        for line in playlist.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-SESSION-KEY"):
                # Session keys are commonly SAMPLE-AES / DRM; decrypting them as
                # AES-128 would produce a corrupt file reported as a success.
                raise MediaSaveError("HLS 加密方式不受支持：EXT-X-SESSION-KEY")
            if line.startswith("#EXT-X-KEY"):
                match = _KEY_RE.search(line)
                if match:
                    method = match.group(1).strip().upper()
                    if method == "NONE":
                        pass
                    elif method == "AES-128":
                        if not match.group(2):
                            raise MediaSaveError("HLS key 行缺少 URI")
                        key_url = urljoin(playlist_url, match.group(2))
                        key_iv = bytes.fromhex(match.group(3)) if match.group(3) else None
                    else:
                        # SAMPLE-AES / SAMPLE-AES-CTR / anything else cannot be
                        # decrypted with AES-128-CBC. Refuse instead of writing
                        # garbage and calling it a successful save.
                        raise MediaSaveError(f"HLS 加密方式不受支持：{method}")
            elif line.startswith("#EXT-X-MAP"):
                match = _MAP_RE.search(line)
                if match:
                    init_url = urljoin(playlist_url, match.group(1))
            else:
                match = _SEGMENT_RE.match(line)
                if match:
                    segments.append(urljoin(playlist_url, match.group(1)))
        if not segments:
            raise MediaSaveError("playlist 中没有媒体分段")
        # HLS playlists may redirect to a CDN different from the original
        # player. Recheck every fetched dependency, not just the root manifest.
        for dependency in [playlist_url, key_url, init_url, *segments]:
            if dependency and allow_media is not None and not allow_media(dependency):
                raise MediaSaveError("HLS 分段地址未通过安全策略")
        if progress is not None:
            with progress.lock:
                progress.segments_total = len(segments)
        if key_url:
            key_bytes = _get(
                client, key_url, allow_media=allow_media, timeout_seconds=timeout_seconds
            ).content
            if len(key_bytes) != 16:
                raise MediaSaveError("HLS key 长度异常")
        if init_url:
            init_response = _get(
                client, init_url, allow_media=allow_media, timeout_seconds=timeout_seconds
            )
            init_bytes = init_response.content
            _reject_html(
                init_bytes, init_response.headers.get("content-type", ""), "初始化分段"
            )
        with output.open("wb") as sink:
            sink.write(init_bytes)
            for index, segment in enumerate(segments):
                if progress is not None and progress.cancelled:
                    raise MediaSaveError("已取消")
                segment_response = _get(
                    client, segment, allow_media=allow_media, timeout_seconds=timeout_seconds
                )
                data = segment_response.content
                _reject_html(data, segment_response.headers.get("content-type", ""), "分段")
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
            partial = output.with_name(output.name + ".part")
            try:
                download_hls(
                    raw,
                    partial,
                    progress=progress,
                    allow_media=allow_media,
                    timeout_seconds=timeout_seconds,
                )
                partial.replace(output)
            finally:
                partial.unlink(missing_ok=True)
        else:
            with (
                httpx.Client(
                    # Redirects are resolved by _stream_get so that every hop is
                    # re-checked against allow_media, exactly as in the HLS path.
                    follow_redirects=False,
                    timeout=timeout_seconds,
                    trust_env=True,
                ) as client,
                _stream_get(
                    client, raw, allow_media=allow_media, timeout_seconds=timeout_seconds
                ) as response,
            ):
                response.raise_for_status()
                content_type = (response.headers.get("content-type", "") or "").lower()
                # An embed/player URL is HTML, not a video. Saving it would
                # create a tiny .bin page that looks "successful" but cannot
                # play — exactly the false positive the end-to-end test caught.
                # The caller must first sniff/resolve the actual .m3u8/.mp4.
                if _is_html_content_type(content_type):
                    return {
                        "ok": False,
                        "code": "not_direct_media",
                        "error": "这是播放器页面，不是视频文件；请先播放，待系统识别真实媒体地址后再保存",
                        "progress": progress.snapshot(),
                    }
                if content_type and not _is_supported_media_content_type(content_type):
                    return {
                        "ok": False,
                        "code": "unsupported_media_type",
                        "error": f"响应不是可保存的媒体：{content_type}",
                        "progress": progress.snapshot(),
                    }
                ext = _ext_for(raw, content_type)
                output = _unique_path(directory, stem, ext)
                partial = output.with_name(output.name + ".part")
                try:
                    with partial.open("wb") as sink:
                        for chunk in response.iter_bytes():
                            if progress.cancelled:
                                raise MediaSaveError("已取消")
                            sink.write(chunk)
                            with progress.lock:
                                progress.bytes_done += len(chunk)
                    partial.replace(output)
                finally:
                    partial.unlink(missing_ok=True)
    except MediaSaveError as exc:
        return {"ok": False, "code": "save_failed", "error": str(exc), "progress": progress.snapshot()}
    except httpx.HTTPStatusError as exc:
        # A 403/410 on an HLS dependency means the link expired or was refused,
        # not a flaky connection. Carry the status so the UI can tell them apart.
        status = exc.response.status_code
        return {
            "ok": False,
            "code": "http_status_error",
            "status": status,
            "error": f"媒体请求被拒绝或已失效（HTTP {status}）",
            "progress": progress.snapshot(),
        }
    except httpx.HTTPError as exc:
        return {"ok": False, "code": "network_error", "error": str(exc), "progress": progress.snapshot()}
    return {
        "ok": True,
        "path": str(output),
        "bytes": progress.snapshot()["bytes_done"],
        "segments_done": progress.snapshot()["segments_done"],
        "segments_total": progress.snapshot()["segments_total"],
    }
