from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .components import button, empty_state
from .player_guard import PlayerUrlError, policy_from_config
from .page_shell import _page
from ..player_resolver import _rank
from ..security_lists import PLAYER_DOMAINS
from ..source_health import RankedSource, rank_sources
from .util import _h, _player_tag

# In-app player page: iframes are restricted to the known player hosts —
# anything else must never become a frame on this route.
PLAY_CSP = (
    "default-src 'none'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; media-src 'self'; "
    "form-action 'self'; base-uri 'none'; object-src 'none'; "
    "script-src 'self' 'unsafe-inline'; worker-src 'self'; connect-src 'self'; "
    "frame-src 'self' "
    + " ".join(f"https://{d} https://*.{d}" for d in PLAYER_DOMAINS)
)


class PlaybackPages:
    """Local playback pages and the external-browser landing page.

    Remote media is intentionally never fetched or embedded by the local
    viewer. The landing page offers the controlled desktop player first, with
    an explicit system-browser hand-off as a fallback/escape hatch.
    """

    def _is_mini(self, query: dict[str, list[str]]) -> bool:
        raw = (query.get("mini") or ["0"])[0].strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _external_policy(self):
        return policy_from_config(self.config)

    def _watch_target(self, query: dict[str, list[str]]) -> str:
        raw_url = (query.get("url") or [""])[0].strip()
        return self._external_policy().validate(raw_url)

    def _watch_http_status(self, query: dict[str, list[str]]) -> int:
        try:
            self._watch_target(query)
        except PlayerUrlError:
            return 400
        return 200

    # ------------------------------------------------------------- /play

    @staticmethod
    def _is_site_page(url: str) -> bool:
        try:
            host = (urlsplit(url).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        if host != "asmrlib.com" and not host.endswith(".asmrlib.com"):
            return False
        path = urlsplit(url).path or "/"
        return path.startswith("/posts/") or path.startswith("/tags/") or path == "/"

    def _play_candidates(self, target: str) -> list[tuple[str, str]]:
        """[(label, url)] raw candidates for the in-app player — bare embed first."""
        candidates: list[tuple[str, str]] = []
        if self._is_site_page(target):
            try:
                from ..player_resolver import resolve_player_candidates

                for _index, (label, url) in enumerate(
                    resolve_player_candidates(target)
                ):
                    url = str(url)
                    try:
                        self._external_policy().validate(url)
                    except PlayerUrlError:
                        continue
                    host = (urlsplit(url).hostname or "备用源").split(".")[0]
                    candidates.append((label.strip() or host, url))
            except Exception:  # noqa: BLE001 - resolution is best-effort
                pass
        if not candidates:
            candidates.append(("原页", target))
        return candidates

    def _ranked_play_sources(self, target: str) -> list[RankedSource]:
        """Candidates ordered by measured health, with the manual choice first.

        The score is the persisted per-source record (see ``source_health``);
        the static server preference only breaks ties. A source with no
        history ranks neutral, so a brand-new line is still tried.
        """

        candidates = self._play_candidates(target)
        health = {}
        preferred = ""
        try:
            health = self.db.get_source_health([url for _, url in candidates])
            preferred = self.db.get_source_preference(target)
        except Exception:  # noqa: BLE001 - health is telemetry, never fatal
            health = {}
            preferred = ""
        return rank_sources(
            candidates,
            health=health,
            preferred_url=preferred,
            static_rank=_rank,
        )

    def _render_play(self, query: dict[str, list[str]]) -> tuple[str, int]:
        """Mobile-first in-app player: bare embed in a sandboxed iframe.

        The sandbox (no allow-popups / allow-top-navigation) blocks popup-ads
        and off-site redirects at the HTML level, so in-app playback keeps the
        desktop shield's two core promises even in a plain phone browser.

        Source rows are ordered by measured health and each attempt reports
        back what actually happened. A failure records the reason, puts that
        source into cooldown and moves to the next candidate on its own; when
        every candidate has failed the page explains why and offers a retry
        instead of leaving an empty player.
        """
        try:
            target = self._watch_target(query)
        except PlayerUrlError as exc:
            body = [
                empty_state(
                    "无法打开该播放链接",
                    f"{_h(str(exc))}。只允许已批准的播放器与收藏馆域名。",
                    "<p><a class='button' href='/'>返回收藏馆</a></p>",
                    kicker="PLAY",
                )
            ]
            return _page("播放", body), 400

        sources = self._ranked_play_sources(target)
        first_src = sources[0].url
        # "主源/备源" is a *positional* label: health ordering decides which
        # row is first, so a source that was statically first can legitimately
        # end up labelled 备源. Baking the prefix into the candidate name would
        # leave "主源" sitting on the second row.
        display = [
            (f"主源 · {source.label}" if index == 0 else f"备源 · {source.label}")
            if source.label != "原页"
            else source.label
            for index, source in enumerate(sources)
        ]
        source_html = "".join(
            f"<button type='button' class='glass-chip play-src"
            f"{' is-on' if i == 0 else ''}{' is-cooling' if s.in_cooldown else ''}' "
            f"data-play-src='{_h(s.url)}' data-src-label='{_h(display[i])}' "
            f"data-health='{_h(s.reason)}' "
            f"aria-pressed='{'true' if i == 0 else 'false'}'>"
            f"<span class='play-src-name'>{_h(display[i])}</span>"
            f"<span class='play-src-hint'>{_h(s.reason)}</span>"
            "</button>"
            for i, s in enumerate(sources)
        )
        title = (query.get("title") or ["在线播放"])[0].strip() or "在线播放"
        candidates_json = json.dumps(
            [{"label": display[i], "url": s.url} for i, s in enumerate(sources)],
            ensure_ascii=False,
        )
        item_json = json.dumps(target, ensure_ascii=False)
        body = [
            "<header class='glass-bar play-bar'>"
            "<button type='button' class='glass-btn play-back' onclick='history.back()' "
            "aria-label='返回'>←</button>"
            f"<strong class='play-title'>{_h(title)}</strong>"
            "<span class='glass-badge'>沙箱拦截</span>"
            "</header>",
            "<section class='play-stage'>",
            # No src in the markup on purpose: the load listener below has to
            # be attached before the frame starts loading, otherwise a fast
            # load beats the script and the attempt looks like a failure.
            "<iframe id='playFrame' class='play-frame' "
            "sandbox='allow-scripts allow-same-origin allow-presentation' "
            "referrerpolicy='no-referrer' allowfullscreen "
            "title='内嵌播放器'></iframe>",
            "<div id='playFail' class='play-fail' hidden>"
            "<div class='play-fail-glyph' aria-hidden='true'>🎬</div>"
            "<h2 class='play-fail-title'>所有播放源都无法播放</h2>"
            "<p id='playFailReason' class='play-fail-reason'></p>"
            "<p class='play-fail-hint muted'>播放器站点在部分网络下需要代理。"
            "开启后点「重试」即可。</p>"
            "<div class='play-fail-actions'>"
            "<button type='button' class='glass-chip is-on' id='playRetry'>重试</button>"
            "</div></div>",
            "</section>",
            "<section class='play-dock'>",
            "<div class='glass-panel play-sources' id='playSources'>",
            f"{source_html}",
            "</div>",
            "<div class='play-links'>",
            f"<a class='glass-chip play-ext' href='{_h(first_src)}' target='_blank' "
            "rel='noopener noreferrer' onclick=\"return openDesktopExternal(this.href)\">"
            "系统浏览器打开</a>",
            "<button type='button' class='glass-chip play-ext' id='playPrefer'>"
            "设为优先</button>",
            f"<a class='glass-chip play-ext' href='/post/{quote(target, safe='')}'>"
            "查看详情</a>",
            "</div>",
            "<p class='play-hint muted' id='playHint'>内置轻量播放器：iframe 沙箱会直接拦截弹窗广告"
            "与越权跳转；未知的第三方子资源在 APK 内被白名单丢弃。</p>",
            "<noscript><p class='muted'>该页面需要脚本才能切换播放源，"
            f"<a href='{_h(first_src)}' target='_blank' rel='noopener noreferrer'>"
            "点此用系统浏览器打开</a>。</p></noscript>",
            "</section>",
            "<script>(function(){"
            f"var ITEM={item_json};"
            f"var CAND={candidates_json};"
            "var f=document.getElementById('playFrame');"
            "var failBox=document.getElementById('playFail');"
            "var reasonEl=document.getElementById('playFailReason');"
            "var hintEl=document.getElementById('playHint');"
            "var chips=Array.prototype.slice.call(document.querySelectorAll('[data-play-src]'));"
            "var idx=0, started=false, failures=[], watch=null, t0=Date.now();"
            "function report(outcome,latency,error){"
            "try{fetch('/api/play-outcome',{method:'POST',"
            "headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({item_url:ITEM,url:CAND[idx]&&CAND[idx].url,"
            "outcome:outcome,latency_ms:latency||-1,error:error||'',"
            "failures:failures})})"
            ".then(function(r){return r.json()})"
            ".then(function(d){if(d&&d.reason){reasonEl.textContent=d.reason;}})"
            ".catch(function(){});}catch(e){}"
            "}"
            "function select(i,userPick){"
            "if(i>=CAND.length){finish();return;}"
            "idx=i;started=false;t0=Date.now();"
            "chips.forEach(function(c,j){"
            "c.classList.toggle('is-on',j===i);"
            "c.setAttribute('aria-pressed',j===i?'true':'false');});"
            "failBox.hidden=true;"
            "if(watch)clearTimeout(watch);"
            "f.src=CAND[i].url;"
            "watch=setTimeout(function(){"
            "if(!started)onFail('\u8fde\u63a5\u8d85\u65f6\uff0c\u64ad\u653e\u5668\u6ca1\u6709\u54cd\u5e94');"
            "},20000);"
            "if(userPick){"
            "fetch('/api/play-preference',{method:'POST',"
            "headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({item_url:ITEM,url:CAND[i].url})}).catch(function(){});}"
            "}"
            "function onFail(reason){"
            "if(watch)clearTimeout(watch);"
            "if(started)return;"
            "failures.push({label:CAND[idx]?CAND[idx].label:'',"
            "url:CAND[idx]?CAND[idx].url:'',error:reason||'\u65e0\u6cd5\u52a0\u8f7d'});"
            "report('failed',-1,reason);"
            "select(idx+1,false);"
            "}"
            "function finish(){"
            "if(watch)clearTimeout(watch);"
            "f.removeAttribute('src');"
            "failBox.hidden=false;"
            "if(!reasonEl.textContent){"
            "reasonEl.textContent=failures.map(function(x){return x.label+'\uff1a'+x.error;}).join('\uff1b');}"
            "}"
            "f.addEventListener('load',function(){"
            "if(started)return;started=true;if(watch)clearTimeout(watch);"
            "hintEl.textContent='\u5df2\u52a0\u8f7d\uff1a'+CAND[idx].label+' \u00b7 '+"
            "((Date.now()-t0)+'ms');"
            "report('playing',Date.now()-t0,'');"
            "});"
            "f.addEventListener('error',function(){onFail('\u65e0\u6cd5\u52a0\u8f7d\u6b64\u64ad\u653e\u6e90');});"
            "document.getElementById('playRetry').addEventListener('click',function(){"
            "failures=[];reasonEl.textContent='';select(0,false);});"
            "document.getElementById('playPrefer').addEventListener('click',function(){"
            "select(idx,true);});"
            "chips.forEach(function(c,i){c.addEventListener('click',function(){select(i,true);});});"
            "select(0,false);"
            "})();</script>",
        ]
        return _page(f"播放 · {title}", body), 200

    def _render_watch(self, query: dict[str, list[str]]) -> str:
        """Render a no-script-required external browser hand-off page."""

        body: list[str] = []
        try:
            target = self._watch_target(query)
        except PlayerUrlError as exc:
            body.append(
                empty_state(
                    "Unable to open link",
                    f"{_h(str(exc))}. Only approved player and archive hosts are allowed.",
                    f"<p>{button('Back to library', href='/')}</p>",
                    kicker="BROWSER",
                )
            )
            return _page("Open in browser", body)

        host = urlsplit(target).netloc or "player"
        # Keep real anchors for command-line `serve` mode. The desktop JS
        # action intercepts the primary click and calls DesktopApi, while
        # ordinary browsers fall back to a normal user-gesture popup.
        escaped_target = _h(target)
        js_target = target.replace("\\", "\\\\").replace("'", "\\'")
        body.extend(
            [
                "<p class='crumb'><a href='/'>Library</a>"
                "<span class='crumb-sep'>/</span>Online playback</p>",
                "<section class='watch-shell external-watch-shell'>",
                "<div class='watch-toolbar'>",
                "<div class='watch-toolbar-main'>",
                "<p class='external-watch-kicker'>SYSTEM BROWSER</p>",
                f"<h1>Play <code>{_h(host)}</code></h1>",
                "<p class='muted'>Desktop playback automatically intercepts popups "
                "and ad redirects. Without the desktop bridge, the action falls "
                "back to your system browser.</p>",
                "</div>",
                "<div class='watch-toolbar-actions'>",
                f"<a class='button' href='{escaped_target}' target='_blank' "
                f"rel='noopener noreferrer' onclick=\"return openDesktopOnline('{_h(js_target)}')\">"
                "安全播放</a>",
                f"<a class='text-link' href='{escaped_target}' target='_blank' "
                f"rel='noopener noreferrer' onclick=\"return openDesktopExternal('{_h(js_target)}')\">"
                "打开原链</a>",
                "<a class='button button-secondary' href='/'>Back</a>",
                "</div></div>",
                f"<p class='muted watch-url'><code>{escaped_target}</code></p>",
                "</section>",
            ]
        )
        return _page(f"Open in browser - {host}", body)

    @staticmethod
    def _valid_local_src(src: str) -> bool:
        """Accept only a root-relative media path with no traversal."""

        raw = (src or "").strip()
        if not raw or "\\" in raw:
            return False
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return False
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return False
        decoded = unquote(parsed.path)
        if not decoded.startswith("/media/"):
            return False
        return ".." not in decoded.split("/")

    def _render_watch_local(self, query: dict[str, list[str]]) -> str:
        """Compact local file player for the desktop mini window."""

        mini = self._is_mini(query)
        src = (query.get("src") or [""])[0].strip()
        title = (query.get("title") or ["Local playback"])[0].strip() or "Local playback"
        body: list[str] = []

        if not self._valid_local_src(src):
            body.append(
                empty_state(
                    "Invalid local path",
                    "Only local files under /media/ can be played.",
                    kicker="LOCAL",
                )
            )
            return _page(title, body, mini=mini)

        suffix = Path(unquote(src)).suffix.lower()
        player = _player_tag(suffix, src, autoplay=mini)
        if mini:
            body.extend(
                [
                    "<section class='watch-shell watch-shell-mini watch-shell-local'>",
                    "<div class='mini-bar pywebview-drag-region'>",
                    f"<strong class='mini-title' title='{_h(title)}'>{_h(title)}</strong>",
                    "<span class='mini-tag'>LOCAL - PINNED</span>",
                    "</div>",
                    f"<div class='mini-local-player'>{player}</div>",
                    "</section>",
                ]
            )
        else:
            body.extend(
                [
                    "<p class='crumb'><a href='/'>Library</a>"
                    "<span class='crumb-sep'>/</span>Local playback</p>",
                    "<section class='watch-shell'>",
                    f"<h1>{_h(title)}</h1>",
                    f"<div class='media-card-player'>{player}</div>",
                    "</section>",
                ]
            )
        return _page(title, body, mini=mini)
