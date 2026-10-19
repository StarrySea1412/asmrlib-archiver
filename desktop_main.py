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
        self._viewer = viewer
        self._external_policy = policy_from_config(runtime_config)
        self._mini = None
        self._online_shield = None
        # In-flight playback watchers, as ``(stop_event, thread)``. They write
        # through the viewer's sqlite connection, so shutdown has to join them
        # before that connection closes (see _stop_playback_watchers).
        self._watchers: list[tuple[threading.Event, threading.Thread]] = []
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
        player embed URLs and ordered by *measured* health, so a source that
        has been failing stops being tried first. Resolution is best-effort:
        if it fails the original page loads under the shield, which still
        strips ads/popups.

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

        # Sandboxed pure-video mode: swap a post page for its player embeds,
        # best-first by measured health (a manual choice outranks the score).
        resolved_note = ""
        candidates: list[str] = []
        if self._is_post_page(target):
            candidates = self._ranked_player_urls(target)
            if candidates:
                resolved_note = "sandboxed-player"
        if not candidates:
            candidates = [target]

        # Media URLs already sniffed by the reused shield window. Only a *new*
        # media request may count as this attempt's success, otherwise opening
        # the next source would inherit the previous source's evidence.
        known_media = self._sniffed_media()
        result: dict = {"ok": False, "code": "open_failed", "error": ""}

        for candidate in candidates:
            try:
                from asmrlib_archiver.online_shield import GuardedWebViewPlayer

                with self._lock:
                    if self._online_shield is None:
                        self._online_shield = GuardedWebViewPlayer(
                            self._runtime_config,
                            title="ASMR 收藏馆 · 安全播放",
                            save_dir=app_root() / "data" / "online-saves",
                        )
                    result = self._online_shield.open(candidate)
            except Exception as exc:
                result = {
                    "ok": False,
                    "code": "shield_unavailable",
                    "error": str(exc),
                }
            if result.get("ok"):
                if resolved_note:
                    result["mode"] = resolved_note
                # Success is never assumed here: the watcher only credits this
                # source once the player actually fetched media.
                self._watch_for_playback(candidate, known_media=known_media)
                return result
            code = str(result.get("code") or "")
            if code.startswith("blocked") or code in {"invalid_url", "host_not_allowed"}:
                # The source itself was refused, so record it and try the next
                # candidate instead of sending the user to a dead first pick.
                self._record_source_outcome(candidate, ok=False, error=code)
                continue
            # Anything else (no WebView2, window creation failed) is an
            # environment problem, not this source's fault: stop walking so a
            # broken runtime cannot mark every source as dead.
            break

        # A plain HTTP viewer and a failed WebView2 initialization both keep
        # the user journey usable through the OS browser.
        fallback = self.open_external_url(candidates[0])
        if fallback.get("ok"):
            fallback.update(
                {
                    "mode": "system-browser-fallback",
                    "shield_error": result.get("error", ""),
                    "message": "受控播放器不可用，已用系统浏览器打开",
                }
            )
        return fallback

    # --------------------------------------------------- playback health

    def _health_db(self):
        """The archive DB behind the viewer, or ``None`` in bare serve mode."""

        return getattr(self._viewer, "db", None)

    def _ranked_player_urls(self, post_url: str) -> list[str]:
        """Player embed URLs for a post, best-first by measured health."""

        try:
            from asmrlib_archiver.player_resolver import resolve_player_candidates
            from asmrlib_archiver.source_health import rank_sources

            pairs = [
                (str(label), str(candidate))
                for label, candidate in resolve_player_candidates(post_url)
            ]
        except Exception:
            return []
        if not pairs:
            return []

        order = {url: index for index, (_label, url) in enumerate(pairs)}
        health: dict = {}
        preferred = ""
        db = self._health_db()
        if db is not None:
            try:
                health = db.get_source_health([url for _label, url in pairs])
                preferred = db.get_source_preference(post_url)
            except Exception:  # noqa: BLE001 - health is telemetry, never fatal
                health, preferred = {}, {}

        ranked = rank_sources(
            pairs,
            health=health,
            preferred_url=preferred,
            static_rank=lambda _label, url: order.get(url, 9),
        )
        validated: list[str] = []
        for source in ranked:
            try:
                validated.append(self._external_policy.validate(source.url))
            except Exception:  # noqa: BLE001 - a refused candidate is skipped
                continue
        return validated

    def _sniffed_media(self) -> set[str]:
        binding = getattr(self._online_shield, "binding", None)
        return set(getattr(binding, "sniffed_media", None) or ())

    def _record_source_outcome(
        self, url: str, *, ok: bool, latency_ms: int = 0, error: str = ""
    ) -> None:
        """Persist one real playback attempt (best-effort, never raises)."""

        db = self._health_db()
        if db is None or not url:
            return
        try:
            from asmrlib_archiver.source_health import PlaybackOutcome

            db.record_playback_outcome(
                url,
                PlaybackOutcome.PLAYING if ok else PlaybackOutcome.FAILED,
                int(latency_ms),
                str(error),
            )
        except Exception:  # noqa: BLE001 - health telemetry never breaks playback
            return

    def _watch_for_playback(
        self,
        url: str,
        *,
        known_media: set[str] | None = None,
        timeout_seconds: float = 25.0,
        poll_seconds: float = 0.4,
    ) -> threading.Thread | None:
        """Record a success only once the player actually fetched media.

        The shield already observes every media request the player makes, so
        the first *new* manifest/segment URL is honest evidence that playback
        started — and its arrival time is a real first-byte/first-segment
        latency. A window that merely opened, or a page that never fetched
        media, records nothing.

        Returns the watcher thread (``None`` when there is no DB to write to).
        The write goes through the caller's sqlite connection, so anything
        that later closes that connection — a test's teardown, the app's
        shutdown — must join this thread first; closing the connection while
        the write is in flight is an access violation inside sqlite, not a
        Python exception. ``_close`` does that for the app; tests join the
        returned thread themselves.
        """

        if self._health_db() is None:
            return None
        seen = set(known_media or ())
        started = time.monotonic()
        stop = threading.Event()

        def worker() -> None:
            deadline = started + max(0.0, timeout_seconds)
            while not stop.is_set() and time.monotonic() < deadline:
                fresh = self._sniffed_media() - seen
                if fresh:
                    self._record_source_outcome(
                        url,
                        ok=True,
                        latency_ms=int((time.monotonic() - started) * 1000),
                    )
                    return
                # Event-aware wait, not time.sleep: shutdown sets the event and
                # gets the thread back immediately instead of after one poll.
                stop.wait(max(0.01, poll_seconds))

        watcher = threading.Thread(
            target=worker, daemon=True, name="asmrlib-playback-watch"
        )
        watcher.start()
        with self._lock:
            self._watchers = [
                entry for entry in self._watchers if entry[1].is_alive()
            ]
            self._watchers.append((stop, watcher))
        return watcher

    def _stop_playback_watchers(self, timeout_seconds: float = 2.0) -> None:
        """Wait for in-flight health writes before the DB connection closes.

        ``viewer.close()`` closes the shared sqlite connection. A watcher
        mid-write at that moment does not raise — it takes the whole process
        down with an access violation, so nothing gets a chance to report it.
        Bounded, because a wedged watcher must not hang app exit forever; the
        stop event is set for every watcher before any join, so they all wake
        from the same poll and unwind together.
        """

        with self._lock:
            pending, self._watchers = self._watchers, []
        for stop, _ in pending:
            stop.set()
        for _, watcher in pending:
            if watcher.is_alive():
                with contextlib.suppress(Exception):
                    watcher.join(timeout_seconds)

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

        # Before the viewer (and its sqlite connection) is closed: a watcher
        # still writing at that point crashes the process inside sqlite.
        self._stop_playback_watchers()
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
    # The viewer is passed in so the bridge can persist measured playback
    # health for each online source (see open_online_player).
    api = DesktopApi(base, viewer=viewer, config=config)
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
