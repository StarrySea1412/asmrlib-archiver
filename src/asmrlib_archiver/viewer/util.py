from __future__ import annotations

import html
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote


def _player_tag(suffix: str, src: str, *, autoplay: bool = False) -> str:
    audio_ext = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"}
    tag = "audio" if suffix in audio_ext else "video"
    extra = " autoplay" if autoplay else ""
    return (
        f'<{tag} controls preload="metadata"{extra} '
        f'src="{_h(src)}"></{tag}>'
    )


def _safe_filename(name: str, max_len: int = 60) -> str:
    """Sanitize a user/DB string into a Windows-safe filename stem.

    Keeps CJK and most unicode; strips path separators + reserved chars,
    collapses whitespace, trims dots, and caps the length.
    """
    value = (name or "").strip()
    if not value:
        return ""
    value = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        return ""
    return value[:max_len].rstrip(" .")


def _open_media_stream(path: Path):
    """Open a media file so that deletion stays possible while streaming.

    Python's builtin ``open()`` requests only FILE_SHARE_READ on Windows, so
    while the server is streaming a video to a ``<video>`` element (blocked in
    ``wfile.write`` if the client stalls) BOTH ``unlink`` and ``rename`` of that
    file fail with WinError 32 — which surfaced as "删除失败" in the UI.

    We therefore open with FILE_SHARE_READ | WRITE | DELETE on Windows, which
    lets the delete proceed (POSIX semantics: the name goes away, the handle
    keeps working until closed). Falls back to plain ``open()`` elsewhere or if
    the Win32 call fails.
    """
    if sys.platform != "win32":
        return path.open("rb")
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE

        handle = create_file(
            str(path),
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == INVALID_HANDLE_VALUE or handle is None:
            return path.open("rb")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        return os.fdopen(fd, "rb")
    except Exception:  # noqa: BLE001 - any failure: fall back to plain open
        return path.open("rb")


def _delete_file_robust(file_path: str, attempts: int = 6) -> tuple[bool, str]:
    """Delete a media file, working around Windows sharing violations.

    Media streaming now opens files with FILE_SHARE_DELETE (see
    ``_open_media_stream``), so the common "page is playing it" case deletes
    cleanly. This still retries briefly for other holders (for example an
    antivirus scan), then falls back to renaming the file aside so the
    library is at least consistent.

    Returns ``(removed, error_message)``.
    """
    if not file_path:
        return False, "no path"
    target = Path(file_path)
    if not target.exists():
        # Already gone (repeat click, or deleted outside the app) — the caller
        # removed the DB row, so treat it as success rather than scaring the user.
        return True, ""
    last = ""
    for i in range(attempts):
        try:
            target.unlink()
            return True, ""
        except FileNotFoundError:
            return True, ""
        except OSError as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.25 * (i + 1))
    # Still locked: park it under a .deleted name so the library is clean and
    # a later sweep (or app restart) can remove it.
    try:
        parked = target.with_suffix(target.suffix + ".deleted")
        if parked.exists():
            parked.unlink(missing_ok=True)
        target.rename(parked)
        try:
            parked.unlink()
            return True, ""
        except OSError:
            return False, "文件仍被占用，已标记为待删除，重启后自动清理"
    except OSError:
        return False, "文件被其他程序占用，暂时无法删除（稍后重试）"
    return False, last


def _sweep_deleted_files(root: Path) -> int:
    """Remove leftover ``*.deleted`` parks from earlier locked deletions."""
    removed = 0
    videos = root / "videos"
    if not videos.is_dir():
        return 0
    for item in videos.glob("*.deleted"):
        try:
            item.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _human_size(num: int) -> str:
    try:
        value = float(num or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _human_time(value: str) -> str:
    """``2026-08-04 08:10:40`` → ``08-04 08:10``; empty stays empty."""
    text = str(value or "").strip()
    if len(text) >= 16 and text[4] == "-":
        return text[5:16]
    return text[:16]


def _parse_byte_range(header: str, size: int) -> tuple[int, int] | None:
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or size <= 0:
        return None
    start_raw, end_raw = match.group(1), match.group(2)
    if not start_raw and not end_raw:
        return None
    if start_raw and end_raw:
        start, end = int(start_raw), int(end_raw)
    elif start_raw:
        start, end = int(start_raw), size - 1
    else:
        suffix = int(end_raw)
        if suffix <= 0:
            return None
        start = max(0, size - suffix)
        end = size - 1
    if start < 0 or end < start or start >= size:
        return None
    return start, min(end, size - 1)


def _tag_slug(tag_url: str) -> str:
    if tag_url.startswith("user-tag://"):
        return tag_url[len("user-tag://"):]
    match = re.search(r"/tags/([^/?#]+)", tag_url)
    return unquote(match.group(1)) if match else tag_url


def _h(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


