from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import shutil
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse  # urlparse also used by live sync

from ..config import AppConfig
from ..db import ArchiveDb
from ..source_health import PlaybackOutcome, compose_failure_reason
from ..storage import Storage
from .assets import (
    DEFAULT_CSP,
    WATCH_CSP,
)
from .playback import PLAY_CSP
from .library import LibraryPages
from .live import LivePages
from .playback import PlaybackPages
from .preview import generate_video_preview
from .page_shell import _page
from .ui_assets import (
    UI_VERSION,
    app_icon_svg,
    app_manifest,
    cover_service_worker,
    static_asset,
)
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
        # Mobile/phone network suitability probe: TCP-RTT to the site and the
        # main player hosts, cached so a burst of phone tabs costs one check.
        # Refreshed on a background thread — never inline in a request.
        self._net_lock = threading.Lock()
        self._net_cache: dict[str, object] | None = None
        self._net_cache_at = 0.0
        self._net_refreshing = False
        # Cover proxy: host allowlist (archive covers + configured domains),
        # rebuilt rarely because it only ever grows as the archive grows.
        self._cover_hosts: set[str] | None = None
        self._cover_hosts_at = 0.0
        # "Save to library" tasks: the fetch runs on a worker thread because
        # an HLS playlist can take minutes, and the page polls for progress.
        self._save_tasks: dict[str, dict] = {}
        self._save_lock = threading.Lock()
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
                with contextlib.suppress(RuntimeError):
                    self._sync_lock.release()

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
                with contextlib.suppress(RuntimeError):
                    self._sync_lock.release()

        threading.Thread(
            target=worker, daemon=True, name=f"viewer-tag-sync-{reason}"
        ).start()
        return True

    # ---------------------------------------------------------- network check

    def _net_probe_hosts(self) -> list[str]:
        """Hosts the suitability probe should reach (site + main players)."""
        hosts: list[str] = []
        for domain in self.config.allowed_domains or []:
            host = str(domain).strip().lower().rstrip(".")
            if host and host not in hosts:
                hosts.append(host)
        try:
            from ..security_lists import PLAYER_DOMAINS

            for host in PLAYER_DOMAINS:
                host = str(host).strip().lower().rstrip(".")
                if host and host not in hosts:
                    hosts.append(host)
        except Exception:  # noqa: BLE001 - probe must never break the app
            pass
        return hosts[:5]

    @staticmethod
    def _tcp_rtt_ms(host: str, port: int, timeout: float = 2.5) -> tuple[bool, int, str]:
        """One TCP connect probe. Returns (ok, rtt_ms, error)."""
        start = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                rtt = int((time.perf_counter() - start) * 1000)
                return True, rtt, ""
        except OSError as exc:
            return False, 0, f"{type(exc).__name__}"

    @staticmethod
    def _tls_rtt_ms(
        host: str, port: int = 443, timeout: float = 2.5
    ) -> tuple[bool, int, str]:
        """One TLS-handshake probe: connect, complete the handshake, hang up.

        No HTTP request is ever written, so this keeps the probe's original
        bargain (no content, no site quota, nothing for robots.txt to govern)
        while answering the question TCP connect cannot: is HTTPS actually
        *usable*? Certificate validation is deliberately off — an expired or
        self-signed cert is not what makes the site unreachable, and treating
        it as a failure would produce false "网络不适合" banners.
        """
        start = time.perf_counter()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw:
                with context.wrap_socket(raw, server_hostname=host):
                    return True, int((time.perf_counter() - start) * 1000), ""
        except OSError as exc:  # ssl.SSLError is an OSError subclass
            return False, 0, f"{type(exc).__name__}"

    def _probe_upstream(self) -> dict:
        """Run one upstream probe round and return its payload (no caching)."""
        hosts = self._net_probe_hosts()
        results: list[dict] = []
        for host in hosts:
            tls_ok, tls_err = False, ""
            try:
                ok, rtt, err = self._tcp_rtt_ms(host, 443)
                if not ok:
                    ok, rtt, err = self._tcp_rtt_ms(host, 80)
                if ok:
                    # A successful connect is not the same as a usable site:
                    # some networks let the TCP handshake through and then
                    # break the TLS one, so every fetch dies with
                    # `SSL: UNEXPECTED_EOF_WHILE_READING` while the probe
                    # reported everything fine (measured 2026-09-13).
                    tls_ok, tls_rtt, tls_err = self._tls_rtt_ms(host, 443)
                    if tls_ok:
                        rtt = tls_rtt
            except Exception as exc:  # noqa: BLE001 - probe never breaks serving
                ok, rtt, err = False, 0, f"{type(exc).__name__}"
            results.append(
                {
                    "host": host,
                    "ok": ok,
                    "rtt_ms": rtt,
                    "error": err,
                    "tls_ok": tls_ok,
                    "tls_error": tls_err,
                }
            )
        reachable = [r for r in results if r["ok"]]
        # Latency should describe the best *usable* upstream: a host whose
        # TLS is torn down is not usable however fast its connect was.
        usable = [r for r in results if r["tls_ok"]] or reachable
        return {
            "ok": bool(reachable),
            "tls_ok": any(r["tls_ok"] for r in results),
            # The site itself is hosts[0] (see _net_probe_hosts) — the phone
            # banner weighs its TLS state separately from a player's, because
            # losing the site kills discovery/archive/online playback while
            # losing one player only kills that source.
            "site_host": hosts[0] if hosts else "",
            "checked_at": int(time.time()),
            "upstream": results,
            "best_rtt_ms": min((r["rtt_ms"] for r in usable), default=0),
        }

    def _network_check_payload(self, client_host: str) -> dict:
        """Upstream reachability + RTT for the phone-side network banner.

        TCP connect plus a TLS handshake — no HTTP requests, no content, no
        robots impact. Answers instantly from cache (TTL 60 s, stale allowed)
        and refreshes in a background thread: a cold probe takes seconds and
        would poison the phone's RTT measurement with false "bad" verdicts.
        """
        now = time.time()
        with self._net_lock:
            cache = self._net_cache
            fresh = cache is not None and now - self._net_cache_at < 60.0
            refreshing = self._net_refreshing
            if not fresh and not refreshing:
                self._net_refreshing = True
                threading.Thread(
                    target=self._net_refresh_worker, daemon=True,
                    name="viewer-net-probe",
                ).start()

        if cache is None:
            payload: dict[str, object] = {
                "ok": None,
                "pending": True,
                "checked_at": 0,
                "upstream": [],
                "best_rtt_ms": 0,
            }
        else:
            payload = dict(cache)
            payload["stale"] = not fresh
        payload["client"] = client_host
        payload["client_is_local"] = client_host in {
            "127.0.0.1",
            "::1",
            "localhost",
        }
        return payload

    def _net_refresh_worker(self) -> None:
        try:
            payload = self._probe_upstream()
            with self._net_lock:
                self._net_cache = payload
                self._net_cache_at = time.time()
        finally:
            with self._net_lock:
                self._net_refreshing = False

    def _resolve_player_payload(self, query: dict[str, list[str]]) -> dict:
        """Resolve a post page to its bare player embed for the APK shell.

        The APK opens online playback in its own (shielded) WebView; handing
        it the bare embed keeps that in-app browser lightweight — no site
        header, related grid or ad iframes, same as the desktop sandbox.
        Best-effort: on any failure the caller navigates to the original URL.
        """
        raw = (query.get("url") or [""])[0].strip()
        if not raw.startswith(("http://", "https://")):
            return {"ok": False, "error": "invalid url"}
        try:
            from ..player_guard import policy_from_config

            policy = policy_from_config(self.config)
            validated = policy.validate(raw)
        except Exception as exc:  # noqa: BLE001 - best-effort endpoint
            return {"ok": False, "error": str(getattr(exc, "code", "invalid_url"))}
        try:
            from ..player_resolver import resolve_player_url

            resolved = resolve_player_url(validated)
            player_url = policy.validate(resolved.player_url)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}"}
        return {
            "ok": True,
            "player_url": player_url,
            "source_url": validated,
            "is_bare": resolved.is_direct,
        }

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
            if path == "/storage":
                self._send_html(handler, self._render_storage(query))
                return
            if path == "/sync-status":
                self._send_json(handler, self._sync_status_payload())
                return
            if path == "/api/network-check":
                client_ip = ""
                with contextlib.suppress(Exception):
                    client_ip = handler.client_address[0]
                self._send_json(handler, self._network_check_payload(client_ip))
                return
            if path == "/api/resolve-player":
                self._send_json(handler, self._resolve_player_payload(query))
                return
            if path == "/api/save-status":
                task = (query.get("task") or [""])[0]
                with self._save_lock:
                    snapshot = dict(self._save_tasks.get(task, {}))
                if not snapshot:
                    self._send_json(
                        handler, {"ok": False, "error": "unknown_task"}, status=404
                    )
                    return
                if snapshot.get("state") == "downloaded":
                    self._finish_media_save(task)
                    with self._save_lock:
                        snapshot = dict(self._save_tasks.get(task, {}))
                if snapshot.get("state") == "error":
                    self._record_save_health(task, ok=False)
                    with self._save_lock:
                        snapshot = dict(self._save_tasks.get(task, {}))
                self._send_json(handler, {"ok": True, "task": task, **snapshot})
                return
            if path == "/manifest.webmanifest":
                self._send_json(handler, app_manifest())
                return
            if path == "/ui/icon.svg":
                body = app_icon_svg().encode("utf-8")
                handler.send_response(200)
                handler.send_header("Content-Type", "image/svg+xml; charset=utf-8")
                handler.send_header("Content-Length", str(len(body)))
                handler.send_header("Cache-Control", "public, max-age=86400")
                handler.end_headers()
                handler.wfile.write(body)
                return
            if path == "/api/live-feed":
                self._send_json(handler, self._live_feed_cached(query))
                return
            if path in {"/ui/app.css", "/ui/app.js"}:
                asset = static_asset(path)
                if asset is None:
                    self._send_text(handler, 404, "Not found")
                else:
                    self._send_ui_asset(handler, asset)
                return
            if path == "/cover-cache-sw.js":
                # The Service Worker only ever received configured domains, so
                # it silently cached nothing: every cover in this archive comes
                # from a CDN that list does not name. Feed it the hosts the
                # archive actually uses.
                cover_domains = sorted(self._cover_allow_hosts())
                self._send_ui_asset(handler, cover_service_worker(cover_domains))
                return
            if path == "/cover":
                self._send_cover(handler, (query.get("u") or [""])[0])
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
            if path == "/play":
                html, status = self._render_play(query)
                self._send_html(handler, html, status=status, csp=PLAY_CSP)
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

        page = _page(
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
            if path == "/recordings/forget":
                # Drop the database row and nothing else. "删除" removes the
                # file too, so it cannot be used on a record whose file is
                # already gone: it would report a file removal that never
                # happened. Forgetting is the honest, non-destructive option.
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
                self.db.delete_media_row(media_id)
                self._send_json(
                    handler,
                    {"ok": True, "media_id": media_id, "row_removed": True},
                )
                return
            if path == "/recordings/relink":
                # Point a broken record at a file that is already inside the
                # managed root. The path arrives as a root-relative string so
                # the endpoint can never be aimed at an arbitrary disk path.
                length = int(handler.headers.get("Content-Length") or 0)
                body = handler.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    payload = {}
                media_id = int(payload.get("id") or 0)
                relative = str(payload.get("path") or "").strip()
                if not media_id or not relative:
                    self._send_json(handler, {"ok": False, "error": "missing id/path"})
                    return
                target = self._safe_data_path(relative)
                if target is None or not target.is_file():
                    self._send_json(handler, {"ok": False, "error": "库内找不到这个文件"})
                    return
                if not self.db.relink_media_file(media_id, str(target)):
                    self._send_json(handler, {"ok": False, "error": "记录不存在"})
                    return
                self._send_json(
                    handler,
                    {"ok": True, "media_id": media_id, "file_path": str(target)},
                )
                return
            if path == "/api/save-media":
                length = int(handler.headers.get("Content-Length") or 0)
                body = handler.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    payload = {}
                self._start_media_save(
                    handler,
                    str(payload.get("source_url") or "").strip(),
                    str(payload.get("media_url") or "").strip(),
                    str(payload.get("label") or "").strip(),
                )
                return
            if path == "/api/play-outcome":
                length = int(handler.headers.get("Content-Length") or 0)
                body = handler.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    payload = {}
                self._send_json(handler, self._record_play_outcome(payload))
                return
            if path == "/api/play-preference":
                length = int(handler.headers.get("Content-Length") or 0)
                body = handler.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except ValueError:
                    payload = {}
                item_url = str(payload.get("item_url") or "").strip()
                source_url = str(payload.get("url") or "").strip()
                if not item_url or not source_url:
                    self._send_json(
                        handler, {"ok": False, "error": "missing item_url/url"}
                    )
                    return
                self.db.set_source_preference(item_url, source_url)
                self._send_json(handler, {"ok": True, "preferred": source_url})
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

    # ---- cover proxy ---------------------------------------------------
    _COVER_HOST_TTL = 300.0
    _COVER_MAX_BYTES = 8 * 1024 * 1024
    _COVER_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
    # Negative caching: without it every broken cover (dead 404s and hosts this
    # network cannot reach alike) costs a fresh round-trip on *every* page load,
    # which is the very slowness the proxy exists to remove. A 4xx means the
    # image is gone, so it is not retried for a week; a timeout or reset may be
    # transient, so it is retried within the hour.
    _COVER_MISSING_TTL = 3600.0
    _COVER_GONE_TTL = 7 * 24 * 3600.0

    def _cover_allow_hosts(self) -> set[str]:
        """Hosts the cover proxy (and the cover Service Worker) will accept.

        Deliberately closed: only the configured domains plus the hosts the
        archive's own covers already come from. That keeps the proxy from
        becoming an open relay into the LAN while still covering every real
        cover — a CDN the archive has never used gets no proxying and simply
        falls back to the browser fetching it directly.
        """
        now = time.time()
        hosts = self._cover_hosts
        if hosts is not None and now - self._cover_hosts_at < self._COVER_HOST_TTL:
            return hosts
        hosts = set()
        for domain in list(self.config.allowed_domains or []) + list(
            self.config.download.allowed_media_domains or []
        ):
            cleaned = str(domain).strip().lower().rstrip(".")
            if cleaned:
                hosts.add(cleaned)
        try:
            for url in self.db.list_cover_urls():
                host = (urlparse(url).hostname or "").lower().rstrip(".")
                if host:
                    hosts.add(host)
        except Exception:  # noqa: BLE001 - a missing row must not break a page
            pass
        self._cover_hosts = hosts
        self._cover_hosts_at = now
        return hosts

    def _media_allow_hosts(self) -> set[str]:
        """Hosts the "save to library" endpoint may fetch from.

        Same closed-set reasoning as the cover proxy, widened with the hosts
        the archive's own media references already use: the endpoint takes a
        URL from the page, so it must not become a fetcher for arbitrary
        addresses (SSRF / LAN probe surface).
        """
        # Same-origin is allowed for media candidates stored by the archive,
        # but private/loopback hosts must never be admitted: a malicious row
        # must not turn this endpoint into an SSRF bridge.
        hosts = {
            host
            for host in self._cover_allow_hosts()
            if host not in {"localhost", "127.0.0.1", "::1"}
        }
        try:
            for url in self.db.list_media_urls():
                host = (urlparse(url).hostname or "").lower().rstrip(".")
                if host:
                    hosts.add(host)
        except Exception:  # noqa: BLE001 - a missing row must not break a page
            pass
        return hosts

    def _start_media_save(
        self, handler: BaseHTTPRequestHandler, source_url: str, media_url: str, label: str
    ) -> None:
        """Kick off a background download of one media URL into the library."""
        if not source_url or not media_url:
            self._send_json(
                handler, {"ok": False, "error": "missing source_url/media_url"}, status=400
            )
            return
        try:
            known = self.db.get_item(source_url) is not None
        except Exception:  # noqa: BLE001
            known = False
        if not known:
            self._send_json(
                handler, {"ok": False, "error": "unknown_source_url"}, status=404
            )
            return
        parsed = urlparse(media_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host:
            self._send_json(handler, {"ok": False, "error": "bad_media_url"}, status=400)
            return
        if host not in self._media_allow_hosts():
            self._send_json(
                handler, {"ok": False, "error": "host_not_allowed"}, status=403
            )
            return
        # Only actual media URLs belong here. Player/embed pages are HTML and
        # must first be opened in the guarded player, where the request sniffer
        # captures the true .m3u8/.mp4 URL. Rejecting early keeps the endpoint
        # honest and avoids a fake "saved" 1.6 KB HTML file.
        media_path = parsed.path.lower()
        if not any(
            media_path.endswith(ext)
            for ext in (
                ".m3u8", ".mpd", ".mp4", ".webm", ".m4s", ".ts",
                ".m4a", ".mp3", ".flac", ".ogg", ".wav", ".aac",
            )
        ):
            self._send_json(
                handler,
                {
                    "ok": False,
                    "error": "not_direct_media",
                    "message": "请先播放，待播放器识别真实媒体地址后再保存",
                },
                status=400,
            )
            return

        # Resolve the allowlist on the request thread: the worker must not
        # touch the sqlite connection from another thread (the connection is
        # owned here, and a cross-thread query can block behind the request
        # handler for the whole download).
        allowed = frozenset(self._media_allow_hosts())
        task_id = f"m{uuid.uuid4().hex[:12]}"
        videos = self.root / "videos"
        videos.mkdir(parents=True, exist_ok=True)
        with self._save_lock:
            self._save_tasks[task_id] = {
                "state": "running",
                "bytes": 0,
                "segments_done": 0,
                "segments_total": 0,
                "source_url": source_url,
                "media_url": media_url,
                "label": label or "保存的媒体",
            }

        def _run() -> None:
            try:
                # app.py lives in asmrlib_archiver.viewer; media_save is its
                # sibling at package root, so this is a two-dot import. Keep
                # the import inside the try: an optional dependency/import
                # failure must transition the task to error, never leave it
                # stuck forever at state=running.
                from ..media_save import save_media
                result = save_media(
                    media_url,
                    save_dir=videos,
                    allow_media=lambda value: (
                        (urlparse(value).hostname or "").lower().rstrip(".") in allowed
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the poller
                result = {"ok": False, "code": "save_crashed", "error": str(exc)}
            state = dict(result)
            # "downloaded" is a hand-off state: the DB attach happens on the
            # request thread (see _finish_media_save) because the sqlite
            # connection is not safe to use from this worker.
            state["state"] = "downloaded" if result.get("ok") else "error"
            with self._save_lock:
                self._save_tasks[task_id] = {
                    **self._save_tasks.get(task_id, {}),
                    **state,
                }

        threading.Thread(
            target=_run, name=f"asmrlib-save-{task_id}", daemon=True
        ).start()
        self._send_json(handler, {"ok": True, "task": task_id, "state": "running"})

    def _record_play_outcome(self, payload: dict) -> dict:
        """Record what one playback attempt actually did.

        The player page only reports ``playing`` once the player frame really
        loaded; merely selecting a source reports nothing, so a source can
        never earn a success it did not have. A ``failed`` report also puts
        the source into cooldown so the next attempt does not start there,
        and once every candidate has failed the response carries a readable
        reason for the page to show instead of a blank player.
        """

        source_url = str(payload.get("url") or "").strip()
        outcome = str(payload.get("outcome") or "").strip().lower()
        if not source_url or outcome not in {"playing", "failed"}:
            return {"ok": False, "error": "missing url/outcome"}
        try:
            latency_ms = int(payload.get("latency_ms") or -1)
        except (TypeError, ValueError):
            latency_ms = -1
        try:
            if outcome == "playing":
                self.db.record_playback_outcome(
                    source_url, PlaybackOutcome.PLAYING, max(latency_ms, 0), ""
                )
                return {"ok": True, "recorded": "playing"}
            self.db.record_playback_outcome(
                source_url,
                PlaybackOutcome.FAILED,
                -1,
                str(payload.get("error") or ""),
            )
        except Exception:  # noqa: BLE001 - telemetry never breaks playback
            return {"ok": False, "error": "record_failed"}

        result: dict[str, object] = {"ok": True, "recorded": "failed"}
        failures = payload.get("failures")
        if isinstance(failures, list) and failures:
            result["reason"] = compose_failure_reason(
                [
                    (
                        str(item.get("label") or ""),
                        str(item.get("url") or ""),
                        str(item.get("error") or ""),
                    )
                    for item in failures
                    if isinstance(item, dict)
                ]
            )
        return result

    def _record_save_health(self, task_id: str, *, ok: bool) -> None:
        """Record a completed save result exactly once on the request thread."""
        with self._save_lock:
            task = dict(self._save_tasks.get(task_id, {}))
        if task.get("health_recorded"):
            return
        source_url = str(task.get("source_url") or "")
        media_url = str(task.get("media_url") or "")
        if not source_url or not media_url:
            return
        try:
            self.db.record_media_health(source_url, media_url, ok=ok)
        except Exception:  # noqa: BLE001 - health telemetry never breaks saving
            return
        with self._save_lock:
            self._save_tasks[task_id] = {
                **self._save_tasks.get(task_id, {}),
                "health_recorded": True,
            }

    def _finish_media_save(self, task_id: str) -> None:
        """Attach a finished download to the library (request thread only)."""
        with self._save_lock:
            task = dict(self._save_tasks.get(task_id, {}))
        if task.get("state") != "downloaded" or task.get("media_id") is not None:
            return
        path = str(task.get("path") or "")
        source_url = str(task.get("source_url") or "")
        if not path or not Path(path).is_file():
            return
        try:
            media_id = self.db.attach_local_media(
                source_url,
                file_path=path,
                label=str(task.get("label") or "保存的媒体"),
                media_url=str(task.get("media_url") or ""),
                kind="download",
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the poller
            with self._save_lock:
                self._save_tasks[task_id] = {
                    **task,
                    "state": "error",
                    "error": f"attach_failed: {exc}",
                }
            return
        with self._save_lock:
            self._save_tasks[task_id] = {
                **task,
                "state": "done",
                "media_id": media_id,
            }
        self._record_save_health(task_id, ok=True)

    def _fetch_cover(self, url: str, target: Path) -> None:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": str(self.config.crawler.user_agent)},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("image/"):
                raise ValueError(f"not an image: {content_type or 'unknown'}")
            data = response.read(self._COVER_MAX_BYTES + 1)
        if len(data) > self._COVER_MAX_BYTES:
            raise ValueError("cover too large")
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(data)
        partial.replace(target)

    def _send_cover(self, handler: BaseHTTPRequestHandler, url: str) -> None:
        """Serve a remote cover from this machine's cache, fetching it once.

        Covers live on CDNs no project allowlist names, so the cover Service
        Worker — which only ever saw ``allowed_domains`` — cached nothing and
        every page load re-fetched 20-30 remote images at 200-1000 ms each.
        Through here a cover is same-origin, fetched once for every client
        (desktop and phone), and immutable afterwards.
        """
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            self._send_text(handler, 400, "Bad cover URL")
            return
        host = parsed.hostname.lower().rstrip(".")
        if host not in self._cover_allow_hosts():
            self._send_text(handler, 403, "Cover host not allowed")
            return
        suffix = Path(unquote(parsed.path)).suffix.lower()
        ext = suffix if suffix in self._COVER_EXT else ".img"
        cache_dir = self.root / "covers"
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        target = cache_dir / (digest + ext)
        if not target.is_file():
            missing = cache_dir / (digest + ".missing")
            if missing.is_file():
                kind = missing.read_text(encoding="utf-8", errors="replace").strip()
                ttl = (
                    self._COVER_GONE_TTL if kind == "gone" else self._COVER_MISSING_TTL
                )
                if time.time() - missing.stat().st_mtime < ttl:
                    self._send_text(handler, 404, "Cover unavailable")
                    return
            cache_dir.mkdir(parents=True, exist_ok=True)
            try:
                self._fetch_cover(url, target)
            except urllib.error.HTTPError as exc:
                # 4xx = the image is gone for good; anything else may recover.
                missing.write_text(
                    "gone" if 400 <= int(exc.code) < 500 else "error",
                    encoding="utf-8",
                )
                self._send_text(handler, 404, "Cover unavailable")
                return
            except Exception:  # noqa: BLE001 - the card's plate fallback covers it
                with contextlib.suppress(OSError):
                    missing.write_text("error", encoding="utf-8")
                self._send_text(handler, 502, "Cover unavailable")
                return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "image/jpeg"
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        # The name is a hash of the URL, so a hit can never be stale.
        handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
        handler.send_header(
            "Content-Security-Policy", "default-src 'none'; img-src 'self'"
        )
        handler.end_headers()
        handler.wfile.write(body)

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
