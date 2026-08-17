from __future__ import annotations

import base64
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Local video formats that support the in-page 3-frame preview.
VIDEO_PREVIEW_EXTS = frozenset({
    ".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".ts", ".m2ts",
})


def generate_video_preview(
    media_path: Path,
    *,
    media_id: int = 0,
    refresh: bool = False,
) -> dict:
    """Grab 3 stills and return them as in-memory data URLs — nothing on disk.

    Frames live only in the JSON response / browser DOM. Refresh just re-runs
    ffmpeg with jittered timestamps; there is no server-side preview cache.
    """
    if not media_path.is_file():
        return {"ok": False, "error": "文件不存在"}
    suffix = media_path.suffix.lower()
    if suffix not in VIDEO_PREVIEW_EXTS:
        return {"ok": False, "error": f"不支持预览的格式：{suffix or 'unknown'}"}

    ffmpeg = _find_bin("ffmpeg")
    ffprobe = _find_bin("ffprobe")
    if not ffmpeg:
        return {
            "ok": False,
            "error": "未找到 ffmpeg（桌面版应在 tools/ 下）。无法截取预览帧",
        }

    duration = _probe_duration(ffprobe, media_path, ffmpeg=ffmpeg)
    if duration is None or duration < 0.4:
        return {"ok": False, "error": "无法读取视频时长，或文件过短"}

    stamps = _pick_preview_times(duration, refresh=refresh)
    frames: list[dict] = []
    try:
        for index, stamp in enumerate(stamps, start=1):
            ok, payload, err = _extract_frame_bytes(ffmpeg, media_path, stamp)
            if not ok or not payload or len(payload) < 32:
                raise RuntimeError(err or f"截帧失败 @ {stamp:.1f}s")
            data_url = "data:image/jpeg;base64," + base64.b64encode(payload).decode(
                "ascii"
            )
            frames.append(
                {
                    "index": index,
                    "t": round(stamp, 2),
                    "label": _format_timecode(stamp),
                    "url": data_url,
                }
            )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "media_id": media_id,
        "duration": round(duration, 2),
        "duration_label": _format_timecode(duration),
        "refresh": bool(refresh),
        "frames": frames,
    }


def _find_bin(name: str) -> str | None:
    """Locate ffmpeg/ffprobe for dev + frozen desktop packages.

    Search order:
      1. env override (FFMPEG_PATH / FFPROBE_PATH)
      2. tools/ next to the exe (PyInstaller desktop bundle)
      3. PyInstaller _MEIPASS (if ever added as a binary)
      4. PATH
      5. a few known Windows install spots (dev machines)
    """
    names = [name]
    if sys.platform == "win32" and not name.lower().endswith(".exe"):
        names.append(f"{name}.exe")

    env_key = "FFMPEG_PATH" if name == "ffmpeg" else "FFPROBE_PATH"
    env_val = (
        os.environ.get(env_key)
        or os.environ.get("FFMPEG_BINARY")
        or os.environ.get("FFMPEG_DIR")
    )
    candidates: list[Path] = []
    if env_val:
        env_path = Path(env_val)
        if env_path.is_dir():
            for n in names:
                candidates.append(env_path / n)
        else:
            candidates.append(env_path)

    # Desktop package layout: <app>/tools/ffmpeg.exe next to the .exe
    exe_dir = Path(sys.executable).resolve().parent
    for n in names:
        candidates.append(exe_dir / "tools" / n)
        candidates.append(exe_dir / n)

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(meipass)
        for n in names:
            candidates.append(base / "tools" / n)
            candidates.append(base / n)

    which = shutil.which(name)
    if which:
        candidates.append(Path(which))
    if sys.platform == "win32":
        which_exe = shutil.which(f"{name}.exe")
        if which_exe:
            candidates.append(Path(which_exe))

    candidates.extend(
        [
            Path(r"D:\downloadTool") / name,
            Path(r"D:\downloadTool") / f"{name}.exe",
            Path(r"C:\ffmpeg\bin") / f"{name}.exe",
            Path.home() / "scoop" / "shims" / f"{name}.exe",
        ]
    )

    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        except OSError:
            key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _probe_duration(ffprobe: str | None, path: Path, ffmpeg: str | None = None) -> float | None:
    """Best-effort media duration. WebM/recordings often lack container duration."""

    def _parse_float(raw: str) -> float | None:
        text = (raw or "").strip()
        if not text or text.upper() == "N/A":
            return None
        try:
            value = float(text.splitlines()[-1].strip())
        except ValueError:
            return None
        if value <= 0 or value != value:  # NaN
            return None
        return value

    def _run(bin_path: str, args: list[str], timeout: float = 30) -> str:
        try:
            proc = subprocess.run(
                [bin_path, *args, str(path)],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return (proc.stdout or "") + "\n" + (proc.stderr or "")

    if ffprobe:
        # 1) container duration
        value = _parse_float(
            _run(
                ffprobe,
                [
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                ],
            )
        )
        if value:
            return value

        # 2) first video stream duration
        value = _parse_float(
            _run(
                ffprobe,
                [
                    "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                ],
            )
        )
        if value:
            return value

        # 3) last packet pts — works for short recorded WebMs with N/A duration
        raw = _run(
            ffprobe,
            [
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "packet=pts_time",
                "-of", "csv=p=0",
            ],
            timeout=60,
        )
        last = None
        for line in raw.splitlines():
            parsed = _parse_float(line)
            if parsed is not None:
                last = parsed
        if last:
            return last

    # 4) ffmpeg alone: parse "Duration: HH:MM:SS.xx" from -i banner
    if ffmpeg:
        banner = _run(ffmpeg, ["-hide_banner", "-i"], timeout=30)
        match = re.search(
            r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
            banner,
        )
        if match:
            hours = int(match.group(1))
            mins = int(match.group(2))
            secs = float(match.group(3))
            total = hours * 3600 + mins * 60 + secs
            if total > 0:
                return total
    return None


def _pick_preview_times(duration: float, *, refresh: bool) -> list[float]:
    """Three in-video timestamps. First open = even thirds; refresh = jittered."""
    # Keep away from pure black intro/outro frames when the clip is long enough.
    if duration < 1.2:
        # Very short clips: just spread across the whole range.
        mid = max(0.05, duration * 0.5)
        return [
            max(0.05, duration * 0.15),
            mid,
            max(mid, duration * 0.85 - 0.05),
        ]
    lo = max(0.25, duration * 0.06)
    hi = max(lo + 0.3, duration * 0.94)
    span = max(0.2, hi - lo)
    if not refresh:
        return [lo + span * p for p in (0.18, 0.50, 0.82)]
    # Three non-overlapping bands, random sample inside each.
    picks: list[float] = []
    bands = ((0.08, 0.34), (0.38, 0.62), (0.66, 0.92))
    for a, b in bands:
        picks.append(lo + span * random.uniform(a, b))
    return picks


def _extract_frame_bytes(
    ffmpeg: str, path: Path, stamp: float
) -> tuple[bool, bytes, str]:
    """Seek + single JPEG frame, streamed to stdout — no temp files on disk."""
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{stamp:.3f}",
        "-i", str(path),
        "-frames:v", "1",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-q:v", "3",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, b"", f"截帧超时 @ {stamp:.1f}s"
    except OSError as exc:
        return False, b"", f"无法启动 ffmpeg: {exc}"
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or b"ffmpeg failed").decode("utf-8", errors="replace").strip()
        return False, b"", err[:240]
    return True, proc.stdout, ""


def _format_timecode(seconds: float) -> str:
    total = max(0, int(seconds + 0.5))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

