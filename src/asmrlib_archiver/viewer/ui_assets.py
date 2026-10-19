"""Versioned UI assets served by the local viewer.

The large legacy constants remain in ``assets`` for now, but pages reference
them through cacheable endpoints instead of repeating them in every response.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from .assets import (
    _CSS,
    _DESKTOP_ACTIONS_JS,
    _LOADING_JS,
    _MINI_PLAYER_JS,
    _UI_JS,
)

# Phone-side network suitability probe. The server answers /api/network-check
# with its own upstream reachability (site + player hosts, TCP RTT); this
# script adds the phone↔server RTT plus connection hints (effectiveType,
# saveData) and shows a fixed banner whenever the combination is not "ok".
# "ok" stays silent — the banner is only a warning surface.
_NETWORK_JS = r"""
(function () {
  'use strict';
  var STORE_KEY = 'asmrlib-net-banner';
  var conn = navigator.connection || navigator.mozConnection ||
    navigator.webkitConnection || {};

  function now() {
    return (window.performance && performance.now) ? performance.now() : Date.now();
  }

  function measure(url) {
    var t0 = now();
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (data) {
      data.client_rtt_ms = Math.round(now() - t0);
      return data;
    });
  }

  function evaluate(data) {
    var reasons = [];
    var level = 'ok';
    // A "pending" payload means the server has no upstream data yet (its
    // background probe is still running). Upstream judgment waits for the
    // next page load; the phone-side RTT below is already valid.
    var upstreamUnknown = data.ok === null || data.ok === undefined;
    var upstream = data.upstream || [];
    var siteHost = data.site_host || '';
    var reachable = [];
    var siteEntry = null;
    var tlsTried = 0;
    var tlsFailed = 0;
    for (var i = 0; i < upstream.length; i++) {
      if (siteHost && upstream[i].host === siteHost) siteEntry = upstream[i];
      if (upstream[i].ok) {
        reachable.push(upstream[i]);
        // tls_ok is absent on older payloads — count only what we know.
        if (typeof upstream[i].tls_ok === 'boolean') {
          tlsTried++;
          if (!upstream[i].tls_ok) tlsFailed++;
        }
      }
    }
    if (!upstreamUnknown && upstream.length && !reachable.length) {
      reasons.push('电脑当前无法访问站点与播放器域名（在线播放 / 实时发现会失败，本地收藏不受影响）');
      level = 'bad';
    } else if (siteEntry && siteEntry.ok && siteEntry.tls_ok === false) {
      // Ports answer but the site's TLS handshake is torn down: the classic
      // "needs a proxy" network. Without this the banner stayed silent while
      // every crawl died with SSL: UNEXPECTED_EOF_WHILE_READING.
      reasons.push('电脑能连通站点端口，但 HTTPS 握手被中断（网络层干扰，需要代理/梯子；'
        + '实时发现、归档与在线播放会失败，本地收藏不受影响）');
      level = 'bad';
    } else if (tlsFailed) {
      reasons.push('电脑到部分播放器域名（' + tlsFailed + '/' + tlsTried +
        '）的 HTTPS 握手被中断，这些源可能播不了');
      level = 'warn';
    } else if (!upstreamUnknown && data.best_rtt_ms > 2000) {
      reasons.push('电脑到站点延迟高（' + data.best_rtt_ms + 'ms）');
      level = 'warn';
    }
    var rtt = data.client_rtt_ms || 0;
    if (rtt > 800) {
      reasons.push('手机到收藏馆延迟高（' + rtt + 'ms）');
      level = 'bad';
    } else if (rtt > 350) {
      reasons.push('手机到收藏馆延迟偏高（' + rtt + 'ms）');
      if (level === 'ok') level = 'warn';
    }
    if (conn.saveData) {
      reasons.push('已开启省流模式，媒体可能被浏览器拦截');
      if (level === 'ok') level = 'warn';
    }
    var eff = String(conn.effectiveType || '');
    if (eff === 'slow-2g' || eff === '2g') {
      reasons.push('移动网络很慢（' + eff + '）');
      level = 'bad';
    }
    return { level: level, reasons: reasons, rtt: rtt, eff: eff };
  }

  function ensureBanner() {
    var el = document.getElementById('net-banner');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'net-banner';
    el.className = 'net-banner';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.innerHTML =
      '<span class="net-dot" aria-hidden="true"></span>' +
      '<span class="net-banner-text">' +
      '<strong class="net-banner-title"></strong>' +
      '<span class="net-banner-sub"></span></span>' +
      '<button type="button" class="net-retest">重测</button>' +
      '<button type="button" class="net-close" aria-label="关闭提示">✕</button>';
    document.body.appendChild(el);
    el.querySelector('.net-retest').addEventListener('click', function () {
      try { sessionStorage.removeItem(STORE_KEY); } catch (e) {}
      el.classList.remove('is-on');
      run(true);
    });
    el.querySelector('.net-close').addEventListener('click', function () {
      el.classList.remove('is-on');
      try { sessionStorage.setItem(STORE_KEY, el.getAttribute('data-level') || ''); }
      catch (e) {}
    });
    return el;
  }

  function render(result) {
    var el = ensureBanner();
    el.setAttribute('data-level', result.level);
    var titleEl = el.querySelector('.net-banner-title');
    var subEl = el.querySelector('.net-banner-sub');
    if (result.level === 'bad') {
      el.className = 'net-banner is-on is-bad';
      titleEl.textContent = '网络不适合，建议调整';
    } else if (result.level === 'warn') {
      el.className = 'net-banner is-on is-warn';
      titleEl.textContent = '网络状况一般';
    } else {
      el.className = 'net-banner';
      return;
    }
    var dismissed = '';
    try { dismissed = sessionStorage.getItem(STORE_KEY) || ''; } catch (e) {}
    if (dismissed === 'bad' && result.level !== 'bad') return;
    if (dismissed === result.level) return;
    subEl.textContent = result.reasons.join('；') +
      '。' + (result.eff ? '网络类型 ' + result.eff + '。' : '') + '点击「重测」可再次检测。';
  }

  var running = false;
  function run(force) {
    if (running || !window.fetch) return;
    running = true;
    var tries = [measure('/api/network-check?r=' + Date.now())];
    tries.push(tries[0].then(function () {
      return measure('/api/network-check?r=' + (Date.now() + 1));
    }).catch(function () { return null; }));
    Promise.all(tries).then(function (results) {
      running = false;
      var data = results[0];
      if (!data) return;
      var second = results[1];
      if (second && second.client_rtt_ms < data.client_rtt_ms) {
        data.client_rtt_ms = second.client_rtt_ms;
      }
      render(evaluate(data));
    }).catch(function () { running = false; });
  }

  function init() {
    // Only online pages should announce network suitability. The archive
    // pages (/, /posts, /post/..., /author, /recordings, /sync-status) show
    // local content that does not need any network at all; having a red
    // banner shout over the cinema hero just because the phone cannot reach
    // asmrlib.com is misleading. Without this guard the banner floated over
    // /posts and / on the desktop viewer too.
    var path = (location && location.pathname) || '/';
    var online =
      /^\/(?:browse|explore|live|api\/live-feed)(?:\/|$)/.test(path) ||
      /^\/(?:play|watch)(?:\?|$|\/)/.test(path) && !/^\/watch-local/.test(path);
    if (!online) return;
    run(false);
  }
  // Visual QA hook: render(evaluate({...})) lets a test driver show any
  // banner state without cutting the machine off the network.
  window.__asmrlibNetBannerPreview = render;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
"""

_APP_JS_EXTRA = r"""
(function () {
  'use strict';

  // APK shell marks its WebView user agent with this suffix; JS uses it to
  // pick the right hand-off copy (in-app shielded player vs system browser).
  window.__asmrlibInApk = navigator.userAgent.indexOf('asmrlib-android') !== -1;

  function currentNav() {
    var path = location.pathname || '/';
    var key = 'home';
    if (path.indexOf('/browse') === 0 || path.indexOf('/explore') === 0) key = 'discover';
    else if (path.indexOf('/posts') === 0 || path.indexOf('/post/') === 0 || path.indexOf('/author') === 0) key = 'library';
    else if (path.indexOf('/recordings') === 0 || path.indexOf('/storage') === 0) key = 'local';
    var links = document.querySelectorAll('#top-nav a[data-nav]');
    for (var i = 0; i < links.length; i++) {
      if (links[i].getAttribute('data-nav') === key) {
        links[i].classList.add('is-active');
        links[i].setAttribute('aria-current', 'page');
      }
    }
  }

  function skeleton() {
    var cells = '';
    for (var i = 0; i < 6; i++) cells += '<span class="live-skeleton-card" aria-hidden="true"></span>';
    return '<div class="live-skeleton-grid">' + cells + '</div>';
  }

  function wrapRail(target, label) {
    var grid = target && target.querySelector ? target.querySelector('.cover-grid') : null;
    if (!grid) return false;
    var shell = document.createElement('div');
    shell.className = 'cinema-rail-shell';
    var prev = document.createElement('button');
    prev.type = 'button';
    prev.className = 'cinema-rail-arrow is-prev';
    prev.setAttribute('data-rail-dir', 'prev');
    prev.setAttribute('aria-label', '向左滚动');
    prev.textContent = '‹';
    var rail = document.createElement('div');
    rail.className = 'cinema-rail';
    rail.setAttribute('data-cinema-rail', '');
    rail.setAttribute('role', 'region');
    rail.setAttribute('aria-label', label || '站点最新');
    rail.tabIndex = 0;
    while (grid.firstChild) rail.appendChild(grid.firstChild);
    var next = document.createElement('button');
    next.type = 'button';
    next.className = 'cinema-rail-arrow is-next';
    next.setAttribute('data-rail-dir', 'next');
    next.setAttribute('aria-label', '向右滚动');
    next.textContent = '›';
    shell.appendChild(prev);
    shell.appendChild(rail);
    shell.appendChild(next);
    target.innerHTML = '';
    target.appendChild(shell);
    return true;
  }

  function setFeedVisibility(host, visible) {
    var section = host && host.closest ? host.closest('.section-block') : null;
    if (!section) return;
    section.hidden = !visible;
    if (visible) section.removeAttribute('aria-hidden');
    else section.setAttribute('aria-hidden', 'true');
  }

  function goFeed(host, endpoint) {
    if (endpoint) host.setAttribute('data-live-feed', endpoint);
    loadFeed(host);
    var top = host.closest('.section-block') || host;
    if (top && top.scrollIntoView) {
      try { top.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch (e) {}
    }
  }

  function renderFeedPager(host, payload) {
    var existing = host.querySelector('.live-feed-pager');
    if (existing) existing.remove();
    var staleNote = host.querySelector('.live-feed-stale-note');
    if (staleNote) staleNote.remove();
    if (payload && payload.stale) {
      var note = document.createElement('p');
      note.className = 'live-feed-stale-note muted';
      note.setAttribute('role', 'status');
      note.textContent = '网络波动，当前显示最近一次成功加载的内容。';
      var content = host.querySelector('[data-live-feed-content]');
      if (content && content.parentElement) content.parentElement.appendChild(note);
      else host.appendChild(note);
    }
    var nextLink = payload && payload.next ? String(payload.next) : '';
    var prevLink = payload && payload.previous ? String(payload.previous) : '';
    if (!nextLink && !prevLink) return;
    var pager = document.createElement('div');
    pager.className = 'live-feed-pager';
    if (prevLink) {
      var prev = document.createElement('button');
      prev.type = 'button';
      prev.className = 'button button-secondary';
      prev.textContent = '‹ 上一页';
      prev.addEventListener('click', function () { goFeed(host, prevLink); });
      pager.appendChild(prev);
    }
    if (nextLink) {
      var next = document.createElement('button');
      next.type = 'button';
      next.className = 'button';
      next.textContent = '下一页 ›';
      next.addEventListener('click', function () { goFeed(host, nextLink); });
      pager.appendChild(next);
    }
    var content = host.querySelector('[data-live-feed-content]');
    if (content && content.parentElement) content.parentElement.appendChild(pager);
    else host.appendChild(pager);
  }

  function clearFeedExtras(host) {
    // Pager/stale note belong to a *successful* render. They must disappear
    // while loading and on failure, otherwise a dead 下一页 button sits
    // under the error panel.
    var extras = host.querySelectorAll('.live-feed-pager, .live-feed-stale-note');
    for (var i = 0; i < extras.length; i++) extras[i].remove();
  }

  function feedLoading(target) {
    target.setAttribute('aria-busy', 'true');
    target.innerHTML =
      skeleton() +
      '<div class="live-feed-state" role="status" aria-live="polite">' +
      '<span class="state-spinner" aria-hidden="true"></span>' +
      '<span class="state-text">正在加载站点内容…</span></div>';
  }

  function feedError(target, host) {
    target.removeAttribute('aria-busy');
    target.innerHTML =
      '<div class="live-feed-state live-feed-error-panel" role="alert">' +
      '<span class="state-glyph" aria-hidden="true">⚠</span>' +
      '<strong class="state-title">实时内容暂时无法加载</strong>' +
      '<span class="state-sub muted">可能是网络波动或站点暂时不可访问，稍后再试。</span>' +
      '<button type="button" class="button" data-feed-reload>重新加载</button></div>';
    var reload = target.querySelector('[data-feed-reload]');
    if (reload) reload.addEventListener('click', function () { loadFeed(host); });
  }

  function loadFeed(host) {
    var endpoint = host.getAttribute('data-live-feed') || '/api/live-feed';
    var target = host.querySelector('[data-live-feed-content]') || host;
    clearFeedExtras(host);
    feedLoading(target);
    // Remote hiccups come in bursts: retry silently with growing delays
    // (total ~4s) while the loading state stays up. Only after every
    // attempt failed does the error panel appear.
    var delaysMs = [0, 900, 3200];
    function attempt(index) {
      fetch(endpoint, {headers: {'Accept': 'application/json'}})
        .then(function (response) {
          if (!response.ok) throw new Error('HTTP ' + response.status);
          return response.json();
        })
        .then(function (payload) {
          if (!payload || !payload.ok) throw new Error((payload && payload.error) || 'feed unavailable');
          if (!payload.html) {
            target.innerHTML = '';
            target.removeAttribute('aria-busy');
            setFeedVisibility(host, false);
            return;
          }
          target.innerHTML = payload.html || '';
          if ((host.getAttribute('data-live-feed-mode') || '') === 'rail') {
            wrapRail(target, '站点最新');
          }
          target.removeAttribute('aria-busy');
          setFeedVisibility(host, true);
          renderFeedPager(host, payload);
          if (window.__asmrlibCinemaInit) window.__asmrlibCinemaInit();
        })
        .catch(function () {
          if (index + 1 < delaysMs.length) {
            setTimeout(function () { attempt(index + 1); }, delaysMs[index + 1]);
            return;
          }
          setFeedVisibility(host, true);
          feedError(target, host);
        });
    }
    attempt(0);
  }

  function initFeeds() {
    var feeds = document.querySelectorAll('[data-live-feed]');
    for (var i = 0; i < feeds.length; i++) {
      (function (host) {
        loadFeed(host);
      })(feeds[i]);
    }
  }

  function registerCoverCache() {
    if (!('serviceWorker' in navigator)) return;
    navigator.serviceWorker.register('/cover-cache-sw.js', {scope: '/'})
      .catch(function () {});
  }

  function init() {
    currentNav();
    initFeeds();
    registerCoverCache();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
"""


APP_CSS = _CSS
APP_JS = "\n".join(
    (_LOADING_JS, _MINI_PLAYER_JS, _DESKTOP_ACTIONS_JS, _UI_JS, _APP_JS_EXTRA, _NETWORK_JS)
)
UI_VERSION = hashlib.sha256((APP_CSS + APP_JS).encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class UiAsset:
    body: bytes
    content_type: str
    etag: str
    cache_control: str
    extra_headers: tuple[tuple[str, str], ...] = ()


def _etag(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()}"'


def static_asset(path: str) -> UiAsset | None:
    if path == "/ui/app.css":
        body = APP_CSS.encode("utf-8")
        content_type = "text/css; charset=utf-8"
    elif path == "/ui/app.js":
        body = APP_JS.encode("utf-8")
        content_type = "text/javascript; charset=utf-8"
    else:
        return None
    return UiAsset(
        body=body,
        content_type=content_type,
        etag=_etag(body),
        cache_control="public, max-age=31536000, immutable",
    )


def cover_service_worker(allowed_domains: Iterable[str]) -> UiAsset:
    domains = sorted(
        {
            str(domain).strip().lower().rstrip(".")
            for domain in allowed_domains
            if str(domain).strip()
        }
    )
    script = f"""
'use strict';
const CACHE_NAME = 'asmrlib-cover-v1';
const MAX_ENTRIES = 256;
const ALLOWED_HOSTS = new Set({json.dumps(domains, ensure_ascii=True)});

function allowed(url) {{
  const host = url.hostname.toLowerCase().replace(/[.]$/, '');
  for (const domain of ALLOWED_HOSTS) {{
    if (host === domain || host.endsWith('.' + domain)) return true;
  }}
  return false;
}}

async function trim(cache) {{
  const keys = await cache.keys();
  if (keys.length <= MAX_ENTRIES) return;
  await Promise.all(keys.slice(0, keys.length - MAX_ENTRIES).map(key => cache.delete(key)));
}}

async function refresh(request) {{
  const response = await fetch(request);
  if (!response || !(response.ok || response.type === 'opaque')) return response;
  const cache = await caches.open(CACHE_NAME);
  await cache.put(request, response.clone());
  await trim(cache);
  return response;
}}

self.addEventListener('fetch', event => {{
  const request = event.request;
  if (request.method !== 'GET' || request.destination !== 'image') return;
  const url = new URL(request.url);
  if (!/^https?:$/.test(url.protocol) || !allowed(url)) return;
  event.respondWith((async () => {{
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(request);
    if (cached) {{
      event.waitUntil(refresh(request).catch(() => undefined));
      return cached;
    }}
    return refresh(request);
  }})().catch(() => fetch(request)));
}});
""".strip()
    body = script.encode("utf-8")
    return UiAsset(
        body=body,
        content_type="text/javascript; charset=utf-8",
        etag=_etag(body),
        cache_control="no-cache",
        extra_headers=(("Service-Worker-Allowed", "/"),),
    )


def app_icon_svg() -> str:
    """App icon (also the PWA manifest icon): brand bars on a gradient tile."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#7aa2ff"/><stop offset="1" stop-color="#a78bfa"/>'
        "</linearGradient></defs>"
        '<rect width="512" height="512" rx="112" fill="#0a0d14"/>'
        '<rect x="1.5" y="1.5" width="509" height="509" rx="110.5" fill="none" '
        'stroke="url(#g)" stroke-width="3" opacity=".55"/>'
        '<g fill="url(#g)">'
        '<rect x="128" y="216" width="52" height="96" rx="24"/>'
        '<rect x="212" y="144" width="52" height="224" rx="24"/>'
        '<rect x="296" y="184" width="52" height="144" rx="24"/>'
        '<rect x="380" y="112" width="52" height="288" rx="24"/>'
        "</g></svg>"
    )


def app_manifest() -> dict:
    """Installable web-app manifest — 「添加到主屏幕」 without the APK."""
    return {
        "name": "ASMR 收藏馆",
        "short_name": "收藏馆",
        "description": "本地 ASMR 归档收藏馆（只读阅览 + 受控在线播放）",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#07090d",
        "theme_color": "#07090d",
        "lang": "zh-CN",
        "icons": [
            {
                "src": "/ui/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any",
            }
        ],
    }


__all__ = [
    "APP_CSS",
    "APP_JS",
    "UI_VERSION",
    "UiAsset",
    "cover_service_worker",
    "static_asset",
]
