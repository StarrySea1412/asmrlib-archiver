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

_APP_JS_EXTRA = r"""
(function () {
  'use strict';

  function currentNav() {
    var path = location.pathname || '/';
    var key = 'home';
    if (path.indexOf('/browse') === 0 || path.indexOf('/explore') === 0) key = 'discover';
    else if (path.indexOf('/posts') === 0 || path.indexOf('/post/') === 0 || path.indexOf('/author') === 0) key = 'library';
    else if (path.indexOf('/recordings') === 0) key = 'local';
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
    (_LOADING_JS, _MINI_PLAYER_JS, _DESKTOP_ACTIONS_JS, _UI_JS, _APP_JS_EXTRA)
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


__all__ = [
    "APP_CSS",
    "APP_JS",
    "UI_VERSION",
    "UiAsset",
    "cover_service_worker",
    "static_asset",
]
