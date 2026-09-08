"""Desktop shell for the local ASMR library and its guarded player windows.

Remote links are opened in an isolated system WebView2 window when the desktop
bridge is available. The window blocks known advertising requests, popups and
off-site redirects, while the normal system browser remains the fallback.
Online recording and proxying are intentionally not part of the bridge.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


def app_root() -> Path:
    """Directory that owns config.yaml / data/ (exe dir when frozen)."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def ensure_runtime_files(root: Path) -> Path:
    """Prefer config.yaml beside the app; seed from example if missing."""

    config_path = root / "config.yaml"
    if not config_path.is_file():
        example = root / "config.example.yaml"
        if example.is_file():
            config_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            config_path.write_text(
                "tag_seeds: []\n"
                "allowed_domains:\n  - asmrlib.com\n"
                'output_dir: "./data"\n'
                'database_path: "./data/archive.sqlite3"\n',
                encoding="utf-8",
            )
    (root / "data").mkdir(parents=True, exist_ok=True)
    return config_path


def maybe_seed_data_from_dev_tree(root: Path) -> None:
    """Seed a fresh packaged database from a nearby project tree once."""

    db = root / "data" / "archive.sqlite3"
    if db.is_file() and db.stat().st_size > 100_000:
        return
    candidates = [
        root.parent.parent / "data" / "archive.sqlite3",
        root.parent / "data" / "archive.sqlite3",
    ]
    for src_db in candidates:
        if not src_db.is_file() or src_db.stat().st_size < 100_000:
            continue
        src_root = src_db.parent
        try:
            import shutil

            shutil.copy2(src_db, root / "data" / "archive.sqlite3")
            source_metadata = src_root / "metadata"
            target_metadata = root / "data" / "metadata"
            if source_metadata.is_dir():
                target_metadata.mkdir(parents=True, exist_ok=True)
                for item in source_metadata.rglob("*"):
                    if not item.is_file():
                        continue
                    target = target_metadata / item.relative_to(source_metadata)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target)
            break
        except Exception:
            continue


def pick_free_port(host: str = "127.0.0.1", preferred: int = 8765) -> int:
    for port in range(preferred, preferred + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            # Windows lets SO_REUSEADDR bind an already-listening port, which
            # silently double-binds and routes requests to the other process.
            # Availability must be probed with an exclusive bind instead.
            if sys.platform == "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def wait_for_port(host: str, port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _valid_media_path(value: str) -> bool:
    raw = unquote((value or "").strip())
    if not raw.startswith("/media/") or "\\" in raw:
        return False
    return ".." not in raw.split("/")


def _safe_local_path(base: str, path: str) -> str:
    """Return a same-origin URL for an explicitly allowed local player path."""

    raw = path.strip() if isinstance(path, str) else ""
    if not raw:
        raise ValueError("empty path")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in raw):
        raise ValueError("path contains whitespace or control characters")
    if "\\" in raw or not raw.startswith("/"):
        raise ValueError("only root-relative local paths are allowed")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("invalid local path") from exc
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError("only root-relative local paths are allowed")

    decoded_path = unquote(parsed.path)
    if ".." in decoded_path.split("/"):
        raise ValueError("path traversal is not allowed")
    if parsed.path == "/watch-local":
        params = parse_qs(parsed.query, keep_blank_values=True)
        if not set(params).issubset({"src", "title", "mini"}):
            raise ValueError("unsupported local player parameter")
        sources = params.get("src") or []
        if len(sources) != 1 or not _valid_media_path(sources[0]):
            raise ValueError("watch-local requires one /media/ source")
    elif parsed.path.startswith("/media/"):
        if parsed.query or not _valid_media_path(parsed.path):
            raise ValueError("invalid media path")
    else:
        raise ValueError("only /watch-local and /media/ paths are allowed")
    return f"{base.rstrip('/')}{raw}"


class DesktopApi:
    """Small JS bridge exposed as ``window.pywebview.api``."""

    def __init__(self, base_url: str, viewer=None, config=None) -> None:
        from asmrlib_archiver.viewer.player_guard import policy_from_config

        self._base_url = base_url.rstrip("/")
        runtime_config = config or getattr(viewer, "config", None)
        self._runtime_config = runtime_config
        self._external_policy = policy_from_config(runtime_config)
        self._mini = None
        self._online_shield = None
        self._lock = threading.Lock()

    def open_mini_player(self, path: str) -> dict:
        """Open or reuse an always-on-top window for one local media path."""

        try:
            url = _safe_local_path(self._base_url, path)
        except ValueError as exc:
            return {"ok": False, "code": "invalid_local_path", "error": str(exc)}

        import webview

        title = "ASMR 收藏馆 · 本地小窗"
        with self._lock:
            existing = self._mini
            if existing is not None:
                try:
                    existing.load_url(url)
                    existing.set_title(title)
                    try:
                        existing.show()
                        existing.restore()
                    except Exception:
                        pass
                    return {"ok": True, "reused": True, "mode": "local"}
                except Exception:
                    self._mini = None

            window = webview.create_window(
                title,
                url=url,
                width=440,
                height=300,
                min_size=(320, 200),
                on_top=True,
                resizable=True,
                background_color="#07090d",
                text_select=True,
            )
            self._mini = window

            def _clear() -> None:
                with self._lock:
                    if self._mini is window:
                        self._mini = None

            with contextlib.suppress(Exception):
                window.events.closed += _clear
            return {"ok": True, "reused": False, "mode": "local"}

    def open_online_player(self, url: str) -> dict:
        """Open an allow-listed player in the guarded WebView2 window.

        Post pages on the site domain are first resolved to their bare
        player embed URL so the sandbox window shows a pure video page —
        no site header, related grid or ad iframes. Resolution is
        best-effort: if it fails the original page loads under the shield,
        which still strips ads/popups.

        The WebView2 integration is optional at runtime. If it cannot be
        initialized (for example in plain ``serve`` mode or on a machine
        without WebView2), the exact validated URL is handed to the system
        browser instead of making the action fail silently.
        """

        try:
            target = self._external_policy.validate(url)
        except Exception as exc:
            code = str(getattr(exc, "code", "invalid_url"))
            return {"ok": False, "code": code, "error": str(exc)}

        # Sandboxed pure-video mode: swap a post page for its player embed.
        resolved_note = ""
        if self._is_post_page(target):
            try:
                from asmrlib_archiver.player_resolver import resolve_player_url

                resolved = resolve_player_url(target)
                validated = self._external_policy.validate(resolved.player_url)
                target = validated
                resolved_note = "sandboxed-player"
            except Exception:
                # Fall back to loading the post page under the shield.
                pass

        try:
            from asmrlib_archiver.online_shield import GuardedWebViewPlayer

            with self._lock:
                if self._online_shield is None:
                    self._online_shield = GuardedWebViewPlayer(
                        self._runtime_config,
                        title="ASMR 收藏馆 · 安全播放",
                        save_dir=app_root() / "data" / "online-saves",
                    )
                result = self._online_shield.open(target)
            if result.get("ok"):
                if resolved_note:
                    result["mode"] = resolved_note
                return result
        except Exception as exc:
            result = {
                "ok": False,
                "code": "shield_unavailable",
                "error": str(exc),
            }

        # A plain HTTP viewer and a failed WebView2 initialization both keep
        # the user journey usable through the OS browser.
        fallback = self.open_external_url(target)
        if fallback.get("ok"):
            fallback.update(
                {
                    "mode": "system-browser-fallback",
                    "shield_error": result.get("error", ""),
                    "message": "受控播放器不可用，已用系统浏览器打开",
                }
            )
        return fallback

    @staticmethod
    def _is_post_page(url: str) -> bool:
        """True for site post/tag pages that wrap (not are) a player."""

        try:
            from urllib.parse import urlsplit

            host = (urlsplit(url).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        if host != "asmrlib.com" and not host.endswith(".asmrlib.com"):
            return False
        path = urlsplit(url).path or "/"
        return path.startswith("/posts/") or path.startswith("/tags/") or path == "/"

    def _close_online_player(self) -> None:
        with self._lock:
            shield, self._online_shield = self._online_shield, None
        if shield is not None:
            with contextlib.suppress(Exception):
                shield.close()

    def _close(self) -> None:
        """Close child windows owned by the desktop bridge."""

        self._close_online_player()

    def open_external_url(self, url: str) -> dict:
        """Open an approved HTTP(S) link in the operating-system browser."""

        import webbrowser

        try:
            target = self._external_policy.validate(url)
        except Exception as exc:
            code = str(getattr(exc, "code", "invalid_url"))
            return {"ok": False, "code": code, "error": str(exc)}
        try:
            opened = webbrowser.open(target, new=2)
        except Exception as exc:
            return {"ok": False, "code": "open_failed", "error": str(exc), "url": target}
        if not opened:
            return {
                "ok": False,
                "code": "open_failed",
                "error": "no default browser accepted the URL",
                "url": target,
            }
        return {"ok": True, "mode": "system-browser", "url": target}


def main() -> int:
    root = app_root()
    os.chdir(root)
    # Make the src layout importable when running from a checkout.
    src = root / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))

    try:
        import webview
    except ImportError:
        print(
            "pywebview is required for the desktop shell.\n"
            "Install the project's desktop dependencies and retry.",
            file=sys.stderr,
        )
        return 1

    config_path = ensure_runtime_files(root)
    if getattr(sys, "frozen", False):
        maybe_seed_data_from_dev_tree(root)

    from asmrlib_archiver.config import load_config
    from asmrlib_archiver.viewer import ArchiveViewer

    config = load_config(config_path)
    viewer = ArchiveViewer(config)
    host = "127.0.0.1"
    port = pick_free_port(host, 8765)
    server = viewer.create_http_server(host=host, port=port)

    server_thread = threading.Thread(
        target=server.serve_forever,
        name="archive-viewer-http",
        daemon=True,
    )
    server_thread.start()

    def _shutdown() -> None:
        with contextlib.suppress(Exception):
            server.shutdown()
        with contextlib.suppress(Exception):
            server.server_close()
        with contextlib.suppress(Exception):
            viewer.close()

    atexit.register(_shutdown)
    if not wait_for_port(host, port):
        _shutdown()
        print(f"Library server failed to start on {host}:{port}", file=sys.stderr)
        return 1

    base = f"http://{host}:{port}"
    api = DesktopApi(base, config=config)
    webview.create_window(
        "ASMR 收藏馆",
        url=f"{base}/",
        js_api=api,
        width=1280,
        height=860,
        min_size=(900, 600),
        background_color="#07090d",
        text_select=True,
    )
    # Keep WebView2 storage so cookies and the cover service-worker cache can
    # survive between desktop sessions.
    webview.start(private_mode=False)
    api._close()
    _shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
