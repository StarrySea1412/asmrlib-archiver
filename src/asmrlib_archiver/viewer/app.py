from __future__ import annotations

import json
import mimetypes
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse  # urlparse also used by live sync

from ..config import AppConfig
from ..db import ArchiveDb
from ..storage import Storage
from .assets import (
    DEFAULT_CSP,
    WATCH_CSP,
)
from .library import LibraryPages
from .live import LivePages
from .playback import PlaybackPages
from .preview import generate_video_preview
from .ui_assets import UI_VERSION, cover_service_worker, static_asset
from .util import (
    _delete_file_robust,
    _h,
    _open_media_stream,
    _parse_byte_range,
    _sweep_deleted_files,
)


class ArchiveViewer(LibraryPages, LivePages, PlaybackPages):
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.storage = Storage(config.output_dir)
        self.storage.ensure()
        self.db = ArchiveDb(config.database_path)
        self.db.init()
        self.root = config.output_dir.resolve()
        # Cached lazy cover resolutions: source_url -> cover ("" = already tried,
        # no cover available). Prevents re-fetching the same post in-process.
        self._cover_cache: dict[str, str] = {}
        # Live browse auto-archive: seed new posts, crawl them in a background
        # thread so the page stays fast. Separate Archiver instance = own DB conn.
        self._sync_lock = threading.Lock()
        self._sync_state: dict[str, object] = {
            "running": False,
            "phase": "",  # "" | "discover" | "crawl" | "done"
            "queued": 0,
            "last_ok": 0,
            "last_failed": 0,
            "last_added": 0,
            "last_error": "",
            "source": "",  # "startup" | "live" | ""
        }
        self._startup_sync_started = False
        # Clean up files that a previous session couldn't delete because the
        # page still had them open (Windows sharing violation).
        _sweep_deleted_files(self.root)
        # Leftover from the old on-disk preview implementation — wipe if present.
        shutil.rmtree(self.root / "previews", ignore_errors=True)
        # Open → pull latest posts from tag_seeds (e.g. yoonying) in background.
        self.start_background_tag_sync(reason="startup")

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------- live auto-archive

    def _sync_status_payload(self) -> dict:
        state = dict(self._sync_state)
        state["running"] = bool(state.get("running"))
        return state

    def _auto_archive_tag_slugs(self) -> set[str]:
        """Tags allowed for live auto-archive — only ``config.tag_seeds``.

        Homepage "发现" shows everything; we still only enqueue posts that
        carry one of these tags (e.g. yoonying). Empty set = auto-archive off.
        """
        from .util import _tag_slug

        slugs: set[str] = set()
        for raw in self.config.tag_seeds or []:
            slug = _tag_slug(str(raw)).strip().lower()
            if slug:
                slugs.add(slug)
        return slugs

    def _enqueue_live_posts(
        self,
        urls: list[str],
        *,
        auto_crawl: bool = True,
        crawl_limit: int | None = None,
        require_tags: set[str] | None = None,
        post_tags: dict[str, set[str]] | None = None,
    ) -> dict[str, int | bool | str]:
        """Seed unseen live post URLs and optionally kick a background crawl.

        When ``require_tags`` is set (normally ``tag_seeds``), a URL is only
        enqueued if ``post_tags[url]`` intersects that set. Posts without a
        tag map entry are skipped under require_tags (safe default).

        Already-archived / crawled items are left alone. Pending/error items
        count toward the crawl queue without re-inserting.
        """
        added = 0
        already_pending = 0
        already_archived = 0
        skipped_tag = 0
        allowed = {
            d.lower().rstrip(".") for d in (self.config.allowed_domains or [])
        }
        allow_sub = bool(self.config.allow_subdomains)
        need = {t.lower() for t in require_tags} if require_tags else None
        tag_map = post_tags or {}

        for raw in urls:
            url = (raw or "").strip()
            if not url or "/posts/" not in url:
                continue
            try:
                host = (urlparse(url).hostname or "").lower().rstrip(".")
            except Exception:  # noqa: BLE001
                continue
            if not host:
                continue
            if host not in allowed and not (
                allow_sub and any(host.endswith(f".{d}") for d in allowed)
            ):
                continue
            if need is not None:
                tags = {t.lower() for t in tag_map.get(url, set())}
                if not (tags & need):
                    skipped_tag += 1
                    continue
            row = self.db.get_item(url)
            if row is None:
                if self.db.add_seed(url):
                    added += 1
                else:
                    # Race / concurrent insert — treat as known.
                    already_pending += 1
                continue
            status = str(row["status"] or "")
            if status in {"archived", "crawled"}:
                already_archived += 1
            else:
                already_pending += 1

        queued = added + already_pending
        kicked = False
        if auto_crawl and queued > 0:
            limit = crawl_limit if crawl_limit is not None else min(max(queued, 1), 24)
            kicked = self._kick_background_crawl(limit=limit)

        self._sync_state["queued"] = queued
        return {
            "added": added,
            "pending": already_pending,
            "archived": already_archived,
            "skipped_tag": skipped_tag,
            "queued": queued,
            "crawl_started": kicked or bool(self._sync_state.get("running")),
            "filter_tags": ",".join(sorted(need)) if need else "",
        }

    def _kick_background_crawl(self, *, limit: int = 20) -> bool:
        """Start one background crawl if idle. Returns True when a new job started."""
        if limit <= 0:
            return False
        if not self._sync_lock.acquire(blocking=False):
            return False

        self._sync_state["running"] = True
        self._sync_state["phase"] = "crawl"
        self._sync_state["source"] = "live"
        self._sync_state["last_error"] = ""

        def worker() -> None:
            try:
                from ..crawler import Archiver

                archiver = Archiver(self.config)
                try:
                    archiver.init()
                    ok, failed = archiver.crawl(limit=limit)
                    self._sync_state["last_ok"] = ok
                    self._sync_state["last_failed"] = failed
                    self._sync_state["phase"] = "done"
                finally:
                    archiver.close()
            except Exception as exc:  # noqa: BLE001 - surface in status only
                self._sync_state["last_error"] = f"{type(exc).__name__}: {exc}"
                self._sync_state["phase"] = "done"
            finally:
                self._sync_state["running"] = False
                try:
                    self._sync_lock.release()
                except RuntimeError:
                    pass

        threading.Thread(
            target=worker, daemon=True, name="viewer-live-crawl"
        ).start()
        return True

    def start_background_tag_sync(
        self,
        *,
        reason: str = "startup",
        pages_per_tag: int = 1,
        crawl_limit: int = 24,
    ) -> bool:
        """Fetch latest posts from ``tag_seeds`` and archive them in the background.

        Called on app open so yoonying (etc.) updates land without visiting 发现.
        Returns False when there are no tag_seeds or a sync is already running.
        """
        seed_tags = self._auto_archive_tag_slugs()
        if not seed_tags:
            return False
        if reason == "startup" and self._startup_sync_started:
            return False
        if not self._sync_lock.acquire(blocking=False):
            return False

        if reason == "startup":
            self._startup_sync_started = True
        self._sync_state["running"] = True
        self._sync_state["phase"] = "discover"
        self._sync_state["source"] = reason
        self._sync_state["last_error"] = ""
        self._sync_state["last_added"] = 0

        def worker() -> None:
            import time

            try:
                # Let the window/home paint first.
                if reason == "startup":
                    time.sleep(0.6)
                all_urls: list[str] = []
                post_tag_map: dict[str, set[str]] = {}
                for slug in sorted(seed_tags):
                    for page_n in range(1, max(1, pages_per_tag) + 1):
                        page_url = self._build_tag_page_url(slug, page_n)
                        try:
                            page_url = self._assert_explore_url(page_url)
                            parser, fetch_result = self._fetch_live(page_url)
                            parsed = parser.parse_tag_page(
                                page_url, fetch_result.text
                            )
                        except Exception as exc:  # noqa: BLE001
                            self._sync_state["last_error"] = (
                                f"{slug} p{page_n}: {type(exc).__name__}: {exc}"
                            )
                            break
                        for pu in parsed.post_urls or []:
                            url = str(pu).strip()
                            if not url:
                                continue
                            all_urls.append(url)
                            post_tag_map.setdefault(url, set()).add(slug)
                        if not getattr(parsed, "next_page_url", None):
                            break

                summary = self._enqueue_live_posts(
                    all_urls,
                    auto_crawl=False,
                    require_tags=seed_tags,
                    post_tags=post_tag_map,
                )
                added = int(summary.get("added") or 0)
                queued = int(summary.get("queued") or 0)
                self._sync_state["last_added"] = added
                self._sync_state["queued"] = queued

                if queued > 0:
                    self._sync_state["phase"] = "crawl"
                    from ..crawler import Archiver

                    archiver = Archiver(self.config)
                    try:
                        archiver.init()
                        ok, failed = archiver.crawl(
                            limit=min(max(queued, 1), crawl_limit)
                        )
                        self._sync_state["last_ok"] = ok
                        self._sync_state["last_failed"] = failed
                    finally:
                        archiver.close()
                self._sync_state["phase"] = "done"
            except Exception as exc:  # noqa: BLE001
                self._sync_state["last_error"] = f"{type(exc).__name__}: {exc}"
                self._sync_state["phase"] = "done"
            finally:
                self._sync_state["running"] = False
                try:
                    self._sync_lock.release()
                except RuntimeError:
                    pass

        threading.Thread(
            target=worker, daemon=True, name=f"viewer-tag-sync-{reason}"
        ).start()
        return True

    def _startup_sync_banner_html(self) -> str:
        """Home-page strip: tag_seeds sync progress / last result."""
        seed_tags = self._auto_archive_tag_slugs()
        if not seed_tags:
            return ""
        scope = " · ".join(f"#{s}" for s in sorted(seed_tags))
        running = bool(self._sync_state.get("running"))
        phase = str(self._sync_state.get("phase") or "")
        added = int(self._sync_state.get("last_added") or 0)
        ok = int(self._sync_state.get("last_ok") or 0)
        failed = int(self._sync_state.get("last_failed") or 0)
        err = str(self._sync_state.get("last_error") or "")
        if running and phase == "discover":
            msg = f"{scope} · 启动同步：正在拉取最新列表…"
            tone = "is-active is-running"
        elif running and phase == "crawl":
            msg = f"{scope} · 启动同步：后台归档中…"
            tone = "is-active is-running"
        elif err and not ok and not added:
            msg = f"{scope} · 同步失败：{err}"
            tone = "is-idle"
        elif phase == "done" or (not running and (added or ok)):
            if added or ok:
                msg = (
                    f"{scope} · 启动同步完成：新发现 {added} · 归档成功 {ok}"
                    + (f" · 失败 {failed}" if failed else "")
                )
                tone = "is-active is-done"
            else:
                msg = f"{scope} · 启动同步完成：已是最新"
                tone = "is-idle is-done"
        else:
            msg = f"{scope} · 打开时会自动同步最新帖"
            tone = "is-idle"
        mark = ""
        if "is-running" in tone:
            mark = "<span class='sync-spinner' aria-hidden='true'></span>"
        elif "is-done" in tone and (added or ok):
            mark = "<span class='sync-dot' aria-hidden='true'></span>"
        poll = ""
        if running:
            # Keep the home banner live while startup discover/crawl runs.
            poll = (
                "<script>(function(){"
                "var el=document.querySelector('[data-sync-banner=startup]');"
                "if(!el)return;"
                "var msg=el.querySelector('.sync-banner-msg');"
                "var n=0;"
                "var t=setInterval(function(){"
                "fetch('/sync-status').then(function(r){return r.json()}).then(function(s){"
                "n++;"
                "if(s.running){"
                "if(msg){"
                "var p=s.phase||'';"
                "msg.textContent=p==='crawl'"
                "?'自动归档 · 后台归档中…'"
                ":'自动归档 · 正在拉取最新列表…';"
                "}"
                "return;"
                "}"
                "clearInterval(t);"
                "if(n>0)location.reload();"
                "}).catch(function(){});"
                "},2500);"
                "})();</script>"
            )
        return (
            f"<div class='sync-banner {tone}' role='status' "
            f"data-sync-banner='startup' aria-live='polite'>"
            f"<span class='sync-banner-k'>{mark}自动归档</span>"
            f"<span class='sync-banner-msg'>{_h(msg)}</span>"
            f"<a class='text-link' href='/explore?tag={_h(sorted(seed_tags)[0])}'>"
            f"打开标签</a>"
            f"</div>"
            f"{poll}"
        )

    def _sync_banner_html(self, summary: dict[str, int | bool | str]) -> str:
        """Quiet status strip for live browse/explore after auto-seed."""
        added = int(summary.get("added") or 0)
        pending = int(summary.get("pending") or 0)
        archived = int(summary.get("archived") or 0)
        skipped = int(summary.get("skipped_tag") or 0)
        filter_tags = str(summary.get("filter_tags") or "")
        crawling = bool(summary.get("crawl_started"))
        scope = f"仅 #{filter_tags.replace(',', ' #')}" if filter_tags else "tag_seeds"
        if not filter_tags and added == 0 and pending == 0 and archived == 0:
            msg = "未配置 tag_seeds，自动归档已关闭（请在 config.yaml 里加标签）"
            tone = "is-idle"
        elif added == 0 and pending == 0:
            if archived:
                msg = f"{scope} · 本页匹配 {archived} 条均已在收藏库"
            elif skipped:
                msg = f"{scope} · 本页 {skipped} 条都不在自动归档标签内，已跳过"
            else:
                msg = f"{scope} · 本页没有可归档的帖子"
            tone = "is-idle"
        elif added:
            msg = (
                f"{scope} · 新发现 {added} 条，已加入归档队列"
                + (f" · 另有 {pending} 条待抓" if pending else "")
            )
            tone = "is-active"
        else:
            msg = f"{scope} · 本页 {pending} 条仍在待抓队列"
            tone = "is-active"
        busy = bool(
            (crawling or self._sync_state.get("running")) and (added or pending)
        )
        if busy:
            msg += " · 后台抓取中…"
            tone = f"{tone} is-running"
        mark = (
            "<span class='sync-spinner' aria-hidden='true'></span>"
            if busy
            else ""
        )
        return (
            f"<div class='sync-banner {tone}' role='status' aria-live='polite'>"
            f"<span class='sync-banner-k'>{mark}自动归档</span>"
            f"<span class='sync-banner-msg'>{_h(msg)}</span>"
            f"<a class='text-link' href='/posts'>查看收藏</a>"
            f"</div>"
        )

    def create_http_server(
        self, host: str = "127.0.0.1", port: int = 8765
    ) -> ThreadingHTTPServer:
        """Build the local library HTTP server (caller owns serve/shutdown)."""
        viewer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                viewer.handle(self)

            def do_POST(self) -> None:  # noqa: N802
                viewer.handle_post(self)

            def do_OPTIONS(self) -> None:  # noqa: N802
                # No CORS grant here, on purpose. This is a same-origin
                # local app; its POST endpoints (delete/rename media,
                # add/remove tags, ...) carry no auth beyond "the request
                # came from this browser tab". A wildcard
                # Access-Control-Allow-Origin used to let ANY page open
                # in the user's browser -- including a malicious site in
                # another tab -- call these endpoints cross-origin, e.g.
                # blind-deleting local media by iterating ids. The
                # viewer's own same-origin JS never needs a CORS grant to
                # call same-origin fetch(); omitting the header is
                # correct -- browsers refuse cross-origin callers at the
                # preflight step.
                handler = self
                handler.send_response(204)
                handler.send_header("Content-Length", "0")
                handler.end_headers()

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                return

        return ThreadingHTTPServer((host, port), Handler)

    def serve(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        server = self.create_http_server(host=host, port=port)
        print(f"Local archive viewer: http://{host}:{port}/", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.", flush=True)
        finally:
            server.server_close()
            self.close()

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        path = unquote(parsed.path or "/")
        query = parse_qs(parsed.query)

        try:
            if path == "/":
                self._send_html(handler, self._render_home(query))
                return
            if path == "/posts":
                self._send_html(handler, self._render_list(query))
                return
            if path.startswith("/post/"):
                source_url = unquote(path[len("/post/") :])
                self._send_html(handler, self._render_detail(source_url))
                return
            if path == "/author":
                self._send_html(handler, self._render_author(query))
                return
            if path == "/browse":
                self._send_html(handler, self._render_browse(query))
                return
            if path == "/explore":
                self._send_html(handler, self._render_explore(query))
                return
            if path == "/explore/post":
                self._send_html(handler, self._render_explore_post(query))
                return
            if path == "/recordings":
                self._send_html(handler, self._render_recordings(query))
                return
            if path == "/sync-status":
                self._send_json(handler, self._sync_status_payload())
                return
            if path == "/api/live-feed":
                self._send_json(handler, self._live_feed_payload(query))
                return
            if path in {"/ui/app.css", "/ui/app.js"}:
                asset = static_asset(path)
                if asset is None:
                    self._send_text(handler, 404, "Not found")
                else:
                    self._send_ui_asset(handler, asset)
                return
            if path == "/cover-cache-sw.js":
                cover_domains = [
                    *list(self.config.allowed_domains or []),
                    *list(self.config.download.allowed_media_domains or []),
                ]
                self._send_ui_asset(handler, cover_service_worker(cover_domains))
                return
            if path.startswith("/media/"):
                self._send_media(handler, path[len("/media/") :])
                return
            if path.startswith("/file/"):
                self._send_data_file(handler, path[len("/file/") :])
                return
            if path == "/watch":
                self._send_html(
                    handler,
                    self._render_watch(query),
                    status=self._watch_http_status(query),
                    csp=WATCH_CSP,
                )
                return
            if path == "/watch-local":
                self._send_html(
                    handler,
                    self._render_watch_local(query),
                    csp=WATCH_CSP,
                )
                return
            if self._is_retired_path(path):
                self._send_retired(handler, path)
                return
            # Cloudflare beacon / noise from player pages — ignore quietly.
            if path.startswith("/cdn-cgi/"):
                self._send_text(handler, 204, "")
                return
            self._send_text(handler, 404, "Not found")
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_text(handler, 500, f"Server error: {type(exc).__name__}: {exc}")

    @staticmethod
    def _is_retired_path(path: str) -> bool:
        if path in {
            "/sandbox",
            "/sandbox-status",
            "/proxy",
            "/proxy-asset",
            "/watch-frame",
            "/watch-stop",
        }:
            return True
        return path.startswith(
            ("/sandbox/", "/proxy/", "/p/", "/record/", "/assets/", "/e/", "/embed")
        )

    def _send_retired(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        from .components import button, empty_state

        page = self._page(
            "Feature removed",
            [
                empty_state(
                    "This feature has been removed",
                    "Online proxy, sandbox, and recording routes are no longer part of "
                    "ASMR Library. Online links now open in your system browser.",
                    f"<p>{button('Back to library', href='/')}</p>",
                    kicker="410 GONE",
                ),
                f"<p class='muted watch-url'><code>{_h(path)}</code></p>",
            ],
        )
        self._send_html(handler, page, status=410)

    # --------------------------------------------------------- management API

    def _origin_is_same_site(self, handler: BaseHTTPRequestHandler) -> bool:
        origin = handler.headers.get("Origin", "").strip()
        if not origin:
            return True
        host_header = handler.headers.get("Host", "").strip().lower()
        if not host_header:
            return False
        try:
            origin_parts = urlparse(origin)
            origin_host = (origin_parts.hostname or "").lower()
            origin_port = origin_parts.port
        except ValueError:
            return False
        if not origin_host:
            return False
        origin_netloc = (
            origin_host if origin_port is None else f"{origin_host}:{origin_port}"
        )
        return origin_netloc == host_header

    def handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        """POST endpoints for local media and library metadata management."""
        parsed = urlparse(handler.path)
        path = unquote(parsed.path or "/")
        try:
            if self._is_retired_path(path):
                self._send_retired(handler, path)
                return
            if not self._origin_is_same_site(handler):
                self._send_json(
                    handler,
                    {"ok": False, "error": "cross_origin_request_rejected"},
                    status=403,
                )
                return
            if path == "/recordings/delete":
                length = int(handler.headers.get("Content-Length") or 0)
                body = handler.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    payload = {}
                media_id = int(payload.get("id") or 0)
                if not media_id:
                    self._send_json(handler, {"ok": False, "error": "missing id"})
                    return
                file_path = self.db.delete_media_row(media_id)
                if not file_path:
                    # Row already gone (repeat click) or reference-only entry:
                    # nothing left on disk, so this is a success, not a failure.
                    self._send_json(
                        handler,
                        {"ok": True, "media_id": media_id, "file_removed": True},
                    )
                    return
                removed, err = _delete_file_robust(file_path)
                result = {
                    "ok": True,
                    "media_id": media_id,
                    "file_removed": removed,
                }
                if not removed:
                    # DB row is gone either way, but tell the UI the file stayed
                    # (usually another process still holding the handle).
                    result["file_error"] = err
                self._send_json(handler, result)
                return
            if path == "/recordings/rename":
                length = int(handler.headers.get("Content-Length") or 0)
                body = handler.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    payload = {}
                media_id = int(payload.get("id") or 0)
                label = str(payload.get("label") or "").strip()[:200]
                if not media_id or not label:
                    self._send_json(handler, {"ok": False, "error": "missing id/label"})
                    return
                self.db.rename_media_label(media_id, label)
                self._send_json(handler, {"ok": True, "media_id": media_id, "label": label})
                return
            if path == "/tags/add":
                length = int(handler.headers.get("Content-Length") or 0)
                body = handler.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    payload = {}
                source_url = str(payload.get("source") or "").strip()
                label = str(payload.get("label") or "").strip()
                if not source_url or not label:
                    self._send_json(handler, {"ok": False, "error": "missing source/label"})
                    return
                try:
                    tag_url = self.db.add_user_tag(source_url, label)
                except ValueError as exc:
                    self._send_json(handler, {"ok": False, "error": str(exc)})
                    return
                self._send_json(handler, {"ok": True, "tag_url": tag_url, "label": label})
                return
            if path == "/tags/remove":
                length = int(handler.headers.get("Content-Length") or 0)
                body = handler.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    payload = {}
                source_url = str(payload.get("source") or "").strip()
                tag_url = str(payload.get("tag") or "").strip()
                removed = self.db.remove_user_tag(source_url, tag_url)
                self._send_json(handler, {"ok": True, "removed": removed})
                return
            if path == "/preview/generate":
                length = int(handler.headers.get("Content-Length") or 0)
                body = handler.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    payload = {}
                self._send_json(handler, self._generate_preview(payload))
                return
            self._send_text(handler, 404, "Not found")
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_text(handler, 500, f"Server error: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ covers

    def _send_media(self, handler: BaseHTTPRequestHandler, relative: str) -> None:
        self._send_data_file(handler, relative, media_only=True)

    def _send_data_file(
        self,
        handler: BaseHTTPRequestHandler,
        relative: str,
        *,
        media_only: bool = False,
    ) -> None:
        target = self._safe_data_path(relative)
        if target is None or not target.is_file():
            self._send_text(handler, 404, "File not found")
            return
        if media_only and target.suffix.lower() not in {
            ".mp4",
            ".m4v",
            ".webm",
            ".mkv",
            ".mov",
            ".mp3",
            ".m4a",
            ".aac",
            ".wav",
            ".flac",
            ".ogg",
            ".bin",
        }:
            self._send_text(handler, 403, "Not a media file")
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        size = target.stat().st_size
        range_header = handler.headers.get("Range", "").strip()
        if media_only and range_header:
            byte_range = _parse_byte_range(range_header, size)
            if byte_range is None:
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{size}")
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
            start, end = byte_range
            length = end - start + 1
            handler.send_response(206)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            handler.send_header("Content-Length", str(length))
            handler.send_header(
                "Content-Security-Policy", "default-src 'none'; media-src 'self'"
            )
            handler.end_headers()
            with _open_media_stream(target) as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    remaining -= len(chunk)
            return

        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(size))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Security-Policy", "default-src 'none'; media-src 'self'")
        handler.end_headers()
        with _open_media_stream(target) as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)

    def _safe_data_path(self, relative: str) -> Path | None:
        cleaned = relative.replace("\\", "/").lstrip("/")
        if not cleaned or ".." in cleaned.split("/"):
            return None
        candidate = (self.root / cleaned).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        return candidate

    # -------------------------------------------------------------- previews

    def _generate_preview(self, payload: dict) -> dict:
        """Build 3 in-memory frame stills for a local video media row."""
        media_id = int(payload.get("id") or payload.get("media_id") or 0)
        refresh = bool(payload.get("refresh"))
        if not media_id:
            return {"ok": False, "error": "missing media id"}
        row = self.db.conn.execute(
            "SELECT id, file_path, status, kind, label FROM media_candidates WHERE id = ?",
            (media_id,),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "媒体不存在"}
        file_path = str(row["file_path"] or "")
        if not file_path or str(row["status"]) != "downloaded":
            return {"ok": False, "error": "仅本地已下载视频可预览"}
        target = Path(file_path)
        if not target.is_file():
            return {"ok": False, "error": "本地文件已丢失"}
        # Stay inside the archive root — never let arbitrary paths through.
        try:
            target.resolve().relative_to(self.root)
        except ValueError:
            return {"ok": False, "error": "路径非法"}
        return generate_video_preview(
            target,
            media_id=media_id,
            refresh=refresh,
        )

    def _send_html(
        self,
        handler: BaseHTTPRequestHandler,
        content: str,
        *,
        status: int = 200,
        csp: str | None = None,
    ) -> None:
        payload = content.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Content-Security-Policy", csp or DEFAULT_CSP)
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(payload)

    def _send_json(
        self,
        handler: BaseHTTPRequestHandler,
        payload: dict,
        *,
        status: int = 200,
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(raw)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(raw)

    def _send_ui_asset(self, handler: BaseHTTPRequestHandler, asset) -> None:
        """Send a CSS/JS asset with cache metadata and conditional ETag support."""
        request_headers = getattr(handler, "headers", {})
        if request_headers.get("If-None-Match", "") == asset.etag:
            handler.send_response(304)
            handler.send_header("ETag", asset.etag)
            handler.send_header("Cache-Control", asset.cache_control)
            for key, value in asset.extra_headers:
                handler.send_header(key, value)
            handler.end_headers()
            return
        handler.send_response(200)
        handler.send_header("Content-Type", asset.content_type)
        handler.send_header("Content-Length", str(len(asset.body)))
        handler.send_header("ETag", asset.etag)
        handler.send_header("Cache-Control", asset.cache_control)
        for key, value in asset.extra_headers:
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(asset.body)

    def _send_text(self, handler: BaseHTTPRequestHandler, code: int, message: str) -> None:
        payload = message.encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def _page(self, title: str, body: list[str], *, mini: bool = False) -> str:
        if mini:
            return f"""<!doctype html>
<html lang="zh-CN" class="mini-html">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#07090d">
<title>{_h(title)} | ASMR 收藏馆</title>
<link rel="stylesheet" href="/ui/app.css?v={UI_VERSION}">
</head>
<body class="mini-body">
<main class="mini-wrap">
{"".join(body)}
</main>
<script defer src="/ui/app.js?v={UI_VERSION}"></script>
</body>
</html>
"""
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#07090d">
<title>{_h(title)} | ASMR 收藏馆</title>
<link rel="stylesheet" href="/ui/app.css?v={UI_VERSION}">
</head>
<body>
<div id="nav-loading" class="nav-loading" aria-hidden="true" role="status">
  <div class="nav-loading-bar" id="nav-loading-bar"></div>
  <div class="nav-loading-panel">
    <span class="nav-loading-spin" aria-hidden="true"></span>
    <span id="nav-loading-text">加载中…</span>
  </div>
</div>
<header class="top">
  <a class="brand" href="/">
    <span class="brand-mark" aria-hidden="true">
      <svg class="brand-ico" viewBox="0 0 16 16" width="14" height="14" fill="none">
        <rect x="1.5" y="6" width="2.2" height="4" rx="1" fill="currentColor"/>
        <rect x="4.9" y="3.5" width="2.2" height="9" rx="1" fill="currentColor"/>
        <rect x="8.3" y="5" width="2.2" height="6" rx="1" fill="currentColor"/>
        <rect x="11.7" y="2.5" width="2.2" height="11" rx="1" fill="currentColor"/>
      </svg>
    </span>
    ASMR <span>收藏馆</span>
  </a>
  <nav id="top-nav" aria-label="主导航">
    <a href="/" data-nav="home">首页</a>
    <a href="/browse" data-nav="discover">发现</a>
    <a href="/posts" data-nav="library">收藏</a>
    <a href="/recordings" data-nav="local">本地</a>
  </nav>
</header>
<main class="wrap" id="page-top">
{"".join(body)}
</main>
<script defer src="/ui/app.js?v={UI_VERSION}"></script>
</body>
</html>
"""
