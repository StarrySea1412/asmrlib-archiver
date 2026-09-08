from __future__ import annotations

PAGE_SIZE = 30
DEFAULT_CSP = (
    "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
    "media-src 'self'; form-action 'self'; base-uri 'none'; frame-src 'none'; "
    "object-src 'none'; script-src 'self' 'unsafe-inline'; worker-src 'self'; connect-src 'self'"
)
# Frame preview feature removed to reduce bundle size (no ffmpeg dependency).
TOOL_CSP = (
    "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
    "media-src 'self'; form-action 'self'; base-uri 'none'; frame-src 'none'; "
    "object-src 'none'; script-src 'self' 'unsafe-inline'; worker-src 'self'; connect-src 'self'"
)
# Watch pages only host local playback or an external-browser hand-off.
WATCH_CSP = (
    "default-src 'none'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "media-src 'self'; "
    "form-action 'self'; base-uri 'none'; "
    "frame-src 'none'; object-src 'none'; "
    "script-src 'self' 'unsafe-inline'; worker-src 'self'; connect-src 'self'"
)

_LOADING_JS = r"""
(function () {
  var overlay = document.getElementById('nav-loading');
  var bar = document.getElementById('nav-loading-bar');
  var label = document.getElementById('nav-loading-text');
  if (!overlay) return;
  var active = false;
  var timer = null;
  var safetyTimer = null;

  // opts.soft = true → show spinner but do NOT eat clicks (desktop API calls).
  // Without this, a hung pywebview Promise left the overlay up and every
  // button on the page looked "dead".
  function show(msg, opts) {
    opts = opts || {};
    var soft = !!opts.soft;
    active = true;
    // Clear any inline hide() overrides before showing.
    try {
      overlay.style.display = '';
      overlay.style.visibility = '';
      overlay.style.opacity = '';
    } catch (e) {}
    overlay.classList.add('is-on');
    if (soft) overlay.classList.add('is-soft');
    else overlay.classList.remove('is-soft');
    overlay.setAttribute('aria-hidden', 'false');
    if (label) label.textContent = msg || '加载中…';
    if (bar) {
      bar.classList.remove('is-done');
      bar.style.width = '0%';
      // force reflow then animate
      void bar.offsetWidth;
      bar.style.width = '78%';
    }
    // Slow crawl toward 92% so long fetches still feel alive.
    var w = 78;
    clearInterval(timer);
    timer = setInterval(function () {
      if (!active || w >= 92) { clearInterval(timer); return; }
      w += (92 - w) * 0.08;
      if (bar) bar.style.width = w.toFixed(1) + '%';
    }, 400);
    // Hard safety: never leave a blocking overlay up forever.
    clearTimeout(safetyTimer);
    safetyTimer = setTimeout(function () {
      if (active) hide();
    }, soft ? 12000 : 15000);
  }

  function hide() {
    active = false;
    clearInterval(timer);
    clearTimeout(safetyTimer);
    if (bar) {
      bar.style.width = '100%';
      bar.classList.add('is-done');
    }
    setTimeout(function () {
      overlay.classList.remove('is-on');
      overlay.classList.remove('is-soft');
      overlay.setAttribute('aria-hidden', 'true');
      // Force idle paint state off the compositor (WebView2 black-veil bug).
      try {
        overlay.style.display = 'none';
        overlay.style.visibility = 'hidden';
        overlay.style.opacity = '0';
      } catch (e) {}
      if (bar) { bar.style.width = '0%'; bar.classList.remove('is-done'); }
    }, 180);
  }

  function shouldSkip(a) {
    if (!a) return true;
    var href = a.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#') return true;
    if (href.indexOf('javascript:') === 0) return true;
    // New-tab links must not freeze this page with the loading overlay.
    if (a.target === '_blank' || a.getAttribute('download') != null) return true;
    // External absolute http(s) leaving this origin — browser handles it.
    if (/^https?:\/\//i.test(href)) {
      try {
        if (new URL(href, location.href).origin !== location.origin) return true;
      } catch (e) { return true; }
    }
    return false;
  }

  document.addEventListener('click', function (ev) {
    var a = ev.target && ev.target.closest ? ev.target.closest('a[href]') : null;
    if (!a || shouldSkip(a) || ev.defaultPrevented) return;
    if (ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    var msg = '加载中…';
    if (a.href.indexOf('/browse') !== -1) msg = '正在拉取 asmrlib 首页…';
    else if (a.href.indexOf('/explore') !== -1) msg = '正在拉取标签页…';
    else if (a.href.indexOf('/watch') !== -1)
      msg = '正在打开播放器…';
    else if (a.href.indexOf('/post/') !== -1) msg = '打开详情…';
    show(msg);
  }, true);

  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form || form.tagName !== 'FORM') return;
    if (form.target === '_blank') return;
    show('搜索中…');
  }, true);

  // Pager "跳到" dropdown — not a link click, so wire loading explicitly.
  document.addEventListener('change', function (ev) {
    var el = ev.target;
    if (!el || el.tagName !== 'SELECT') return;
    if (!el.classList || !el.classList.contains('pager-select')) return;
    var href = el.value;
    if (!href) return;
    var pageMatch = String(href).match(/[?&]page=(\d+)/);
    var pageNo = pageMatch ? pageMatch[1] : '';
    var msg = pageNo ? ('跳到第 ' + pageNo + ' 页…') : '跳转中…';
    if (String(href).indexOf('/browse') !== -1) {
      msg = pageNo ? ('正在拉取站点第 ' + pageNo + ' 页…') : '正在拉取站点…';
    } else if (String(href).indexOf('/explore') !== -1) {
      msg = pageNo ? ('正在拉取标签第 ' + pageNo + ' 页…') : '正在拉取标签…';
    }
    el.classList.add('is-loading');
    el.setAttribute('aria-busy', 'true');
    show(msg);
    // Navigate after paint so the overlay is visible for at least one frame.
    setTimeout(function () { location.href = href; }, 40);
  }, true);

  // bfcache / back-forward: always clear stuck overlay.
  window.addEventListener('pageshow', hide);
  window.addEventListener('pagehide', hide);
  document.addEventListener('DOMContentLoaded', hide);
  // Shared loading API for desktop action buttons / custom UI.
  window.__asmrlibLoading = show;
  window.__asmrlibLoadingDone = hide;
  // Start fully hidden (covers WebView2 first-paint veil cases).
  hide();
})();
"""

# Desktop shell bridge: only the two supported lightweight actions are exposed.
_MINI_PLAYER_JS = r"""
function _isLocalPlayerPath(path) {
  var raw = String(path || '');
  if (!raw || raw.indexOf('\\') !== -1) return false;
  var decoded = raw;
  try { decoded = decodeURIComponent(raw); } catch (e) { return false; }
  var decodedPath = decoded.split(/[?#]/, 1)[0];
  if (/(^|\/)\.\.(?:\/|$)/.test(decodedPath) ||
      !(/^\/media\//.test(decoded) || /^\/watch-local(?:[?#]|$)/.test(decoded))) {
    return false;
  }
  try {
    var parsed = new URL(raw, location.origin);
    if (parsed.origin !== location.origin || parsed.protocol !== location.protocol) return false;
    if (parsed.pathname === '/watch-local') {
      var source = parsed.searchParams.get('src') || '';
      var sourceDecoded = decodeURIComponent(source);
      if (!/^\/media\//.test(sourceDecoded) || /(^|\/)\.\.(?:\/|$)/.test(sourceDecoded)) return false;
    }
  } catch (e2) { return false; }
  return true;
}
function openMiniPlayer(path) {
  var rel = String(path || '');
  if (!_isLocalPlayerPath(rel)) {
    if (window.__asmrlibToast) window.__asmrlibToast('仅支持本地媒体小窗');
    return false;
  }
  try {
    if (window.pywebview && window.pywebview.api &&
        typeof window.pywebview.api.open_mini_player === 'function') {
      window.pywebview.api.open_mini_player(rel);
      return false;
    }
  } catch (e) {}
  var url = rel;
  try { url = new URL(rel, location.href).href; } catch (e2) {}
  var w = window.open(url, 'asmrlib-mini-player',
    'width=440,height=300,menubar=no,toolbar=no,location=no,status=no,resizable=yes');
  if (!w) location.href = rel;
  return false;
}
"""

_DESKTOP_ACTIONS_JS = r"""
function _callDesktop(name) {
  try {
    if (window.pywebview && window.pywebview.api &&
        typeof window.pywebview.api[name] === 'function') {
      var args = Array.prototype.slice.call(arguments, 1);
      var ret = window.pywebview.api[name].apply(window.pywebview.api, args);
      if (ret && typeof ret.then === 'function') {
        ret.then(function (result) {
          if (result && result.ok === false && window.__asmrlibToast)
            window.__asmrlibToast(result.error || '操作失败');
        }).catch(function () {});
      } else if (ret && ret.ok === false && window.__asmrlibToast) {
        window.__asmrlibToast(ret.error || '操作失败');
      }
      return true;
    }
  } catch (e) {}
  return false;
}
function openDesktopOnline(url) {
  var target = String(url || '');
  if (!/^https?:\/\//i.test(target)) return false;
  // The desktop bridge owns the controlled player. It is responsible for
  // popup/ad interception and must receive the exact approved URL.
  if (_callDesktop('open_online_player', target)) return false;
  // ``serve`` mode has no controlled browser. Keep the click useful by
  // falling back to the same system-browser hand-off as the original link.
  if (window.__asmrlibToast) {
    window.__asmrlibToast('桌面版会自动拦截弹窗与广告跳转；当前使用系统浏览器打开…');
  }
  return openDesktopExternal(target);
}
function openDesktopExternal(url) {
  var target = String(url || '');
  if (!/^https?:\/\//i.test(target)) return false;
  if (_callDesktop('open_external_url', target)) return false;
  try {
    var w = window.open(target, '_blank', 'noopener,noreferrer');
    if (!w && window.__asmrlibToast) window.__asmrlibToast('请允许浏览器弹窗后重试');
  } catch (e) {
    if (window.__asmrlibToast) window.__asmrlibToast('无法打开链接');
  }
  return false;
}
"""

# Self-contained UI component layer (works in pywebview/WebView2 where
# confirm()/prompt() are unreliable). Exposes:
#   __asmrlibToast(msg)          — transient toast
#   __asmrlibConfirm(msg, onOk)  — async confirm modal
#   __asmrlibPrompt(msg, cur, onOk) — async text-input modal
# Also wires the nav-loading overlay behind __asmrlibLoading/Done so both the
# loading overlay and the desktop action buttons share one spinner.
_UI_JS = r"""
(function () {
  var toastHost = null;
  function toast(msg, ms) {
    try {
      if (!toastHost) {
        toastHost = document.createElement('div');
        toastHost.style.cssText =
          'position:fixed;bottom:22px;left:50%;transform:translateX(-50%);' +
          'z-index:2147483646;display:flex;flex-direction:column;gap:8px;' +
          'align-items:center;pointer-events:none;';
        document.body.appendChild(toastHost);
      }
      var el = document.createElement('div');
      el.textContent = msg;
      el.style.cssText =
        'background:rgba(17,24,38,.96);color:#eef2f9;border:1px solid rgba(148,163,184,.3);' +
        'border-radius:12px;padding:10px 16px;font:600 13px/1.4 system-ui,sans-serif;' +
        'box-shadow:0 12px 32px rgba(0,0,0,.5);opacity:0;' +
        'transform:translateY(8px);transition:opacity .18s ease,transform .18s ease;';
      toastHost.appendChild(el);
      requestAnimationFrame(function () {
        el.style.opacity = '1'; el.style.transform = 'translateY(0)';
      });
      setTimeout(function () {
        el.style.opacity = '0'; el.style.transform = 'translateY(8px)';
        setTimeout(function () { el.remove(); }, 200);
      }, ms || 2200);
    } catch (e) {}
  }

  function modal(kind, title, bodyHTML, actions) {
    return new Promise(function (resolve) {
      var wrap = document.createElement('div');
      wrap.style.cssText =
        'position:fixed;inset:0;z-index:2147483645;display:flex;align-items:center;' +
        'justify-content:center;background:rgba(6,9,14,.6);backdrop-filter:blur(3px);';
      var box = document.createElement('div');
      box.style.cssText =
        'width:min(420px,92vw);background:#111826;border:1px solid rgba(148,163,184,.3);' +
        'border-radius:18px;padding:20px 22px;box-shadow:0 24px 60px rgba(0,0,0,.55);' +
        'font:500 14px/1.5 system-ui,sans-serif;color:#eef2f9;';
      var h = document.createElement('h3');
      h.textContent = title;
      h.style.cssText = 'margin:0 0 12px;font-size:16px;font-weight:700;';
      var body = document.createElement('div');
      body.innerHTML = bodyHTML;
      body.style.cssText = 'margin:0 0 18px;color:#bfc9d6;';
      var foot = document.createElement('div');
      foot.style.cssText = 'display:flex;justify-content:flex-end;gap:10px;';
      function btn(label, style, cb) {
        var b = document.createElement('button');
        b.textContent = label;
        b.style.cssText = 'border:0;border-radius:10px;padding:9px 18px;font:inherit;font-weight:700;cursor:pointer;' + style;
        b.addEventListener('click', function () { wrap.remove(); cb(); });
        return b;
      }
      var cancel = btn('取消',
        'background:transparent;color:#8b9bb0;border:1px solid rgba(148,163,184,.4);',
        function () { resolve(kind === 'confirm' ? false : null); });
      var okBtn = btn('确定',
        'background:linear-gradient(180deg,#8bb4ff,#5b8dff);color:#061018;',
        function () {
          if (kind === 'confirm') resolve(true);
          else {
            var input = body.querySelector('input');
            resolve(input ? input.value.trim() : '');
          }
        });
      foot.appendChild(cancel);
      foot.appendChild(okBtn);
      box.appendChild(h); box.appendChild(body); box.appendChild(foot);
      wrap.appendChild(box);
      wrap.addEventListener('click', function (ev) { if (ev.target === wrap) { wrap.remove(); resolve(kind === 'confirm' ? false : null); } });
      document.body.appendChild(wrap);
      if (kind === 'prompt') {
        var input = body.querySelector('input');
        if (input) setTimeout(function () { input.focus(); input.select(); }, 30);
      }
    });
  }

  window.__asmrlibToast = toast;
  window.__asmrlibConfirm = function (msg, onOk) {
    modal('confirm', '确认', '<p style="margin:0">' + String(msg).replace(/</g, '&lt;') + '</p>', null)
      .then(function (ok) { if (ok && onOk) onOk(); });
  };
  window.__asmrlibPrompt = function (msg, cur, onOk) {
    var safe = String(cur || '').replace(/</g, '&lt;').replace(/"/g, '&quot;');
    modal('prompt', '输入', '<p style="margin:0 0 10px">' + String(msg).replace(/</g, '&lt;') + '</p>' +
      '<input type="text" value="' + safe + '" style="width:100%;box-sizing:border-box;' +
      'background:#0a0d14;border:1px solid rgba(148,163,184,.35);border-radius:10px;' +
      'padding:10px 12px;color:#eef2f9;font:inherit;">', null)
      .then(function (val) { if (val && onOk) onOk(val); });
  };
})();
"""

# Cinema UI behavior is deliberately dependency-free.  It is appended to the
# existing inline bundle so older pages and the desktop shell keep working
# while the home/list views adopt real images and keyboard-friendly rails.
_CINEMA_JS = r"""
(function () {
  'use strict';

  function photonOriginUrl(raw) {
    // WordPress Photon proxies (i0/i1/i2.wp.com) randomly 404 images that
    // still exist on their origin host. The site's own <img onerror> swaps
    // the proxy URL for the direct origin URL; mirror that behaviour.
    try {
      var url = new URL(String(raw || ''), location.href);
      if (/^i\d+\.wp\.com$/.test(url.hostname)) {
        var rest = url.pathname.replace(/^\/+/, '');
        if (!rest) return '';
        return url.protocol + '//' + rest + (url.search || '');
      }
    } catch (e) {}
    return '';
  }

  function showCoverFallback(img) {
    if (!img || img.dataset.coverFailed === '1') return;
    if (img.dataset.coverOriginTried !== '1') {
      var origin = photonOriginUrl(img.currentSrc || img.getAttribute('src') || '');
      if (origin && origin !== img.src) {
        img.dataset.coverOriginTried = '1';
        img.src = origin;
        return;
      }
    }
    img.dataset.coverFailed = '1';
    img.setAttribute('aria-hidden', 'true');
    img.hidden = true;
    var host = img.parentElement;
    if (host) {
      host.classList.add('has-cover-fallback');
      var fallback = host.querySelector('.cover-thumb-fallback, .cinema-hero-fallback, .detail-poster-fallback');
      if (fallback) fallback.hidden = false;
    }
  }

  function syncRail(rail) {
    if (!rail) return;
    var prev = rail.parentElement && rail.parentElement.querySelector('[data-rail-dir="prev"]');
    var next = rail.parentElement && rail.parentElement.querySelector('[data-rail-dir="next"]');
    var overflow = rail.scrollWidth > rail.clientWidth + 2;
    if (prev) prev.disabled = !overflow || rail.scrollLeft <= 2;
    if (next) next.disabled = !overflow || rail.scrollLeft + rail.clientWidth >= rail.scrollWidth - 2;
    if (rail.parentElement) rail.parentElement.classList.toggle('has-overflow', overflow);
  }

  function railStep(rail, direction) {
    if (!rail) return;
    var card = rail.querySelector('.cover-card');
    var width = card ? card.getBoundingClientRect().width : rail.clientWidth * .8;
    var gap = parseFloat(getComputedStyle(rail).columnGap || getComputedStyle(rail).gap || '16') || 16;
    var behavior = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
    rail.scrollBy({left: direction * (width + gap), behavior: behavior});
  }

  function wireRail(shell) {
    var rail = shell.querySelector('[data-cinema-rail]');
    if (!rail) return;
    if (shell.getAttribute('data-cinema-wired') === '1') {
      syncRail(rail);
      return;
    }
    shell.setAttribute('data-cinema-wired', '1');
    syncRail(rail);
    rail.addEventListener('scroll', function () { syncRail(rail); }, {passive: true});
    var buttons = shell.querySelectorAll('[data-rail-dir]');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener('click', function () {
        railStep(rail, this.getAttribute('data-rail-dir') === 'prev' ? -1 : 1);
      });
    }
    rail.addEventListener('keydown', function (ev) {
      var dir = ev.key === 'ArrowLeft' ? -1 : ev.key === 'ArrowRight' ? 1 : 0;
      if (dir) { ev.preventDefault(); railStep(rail, dir); return; }
      if (ev.key === 'Home' || ev.key === 'End') {
        ev.preventDefault();
        var behavior = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
        rail.scrollTo({left: ev.key === 'Home' ? 0 : rail.scrollWidth, behavior: behavior});
      } else if (ev.key === 'PageDown' || ev.key === 'PageUp') {
        ev.preventDefault();
        var pageBehavior = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
        rail.scrollBy({left: (ev.key === 'PageDown' ? 1 : -1) * rail.clientWidth * .85, behavior: pageBehavior});
      }
    });
    if (window.ResizeObserver) new ResizeObserver(function () { syncRail(rail); }).observe(rail);
  }

  function initCinema() {
    var images = document.querySelectorAll('[data-cover-img]');
    for (var i = 0; i < images.length; i++) {
      images[i].addEventListener('error', function () { showCoverFallback(this); });
      if (images[i].complete && images[i].naturalWidth === 0) showCoverFallback(images[i]);
    }
    var rails = document.querySelectorAll('.cinema-rail-shell');
    for (var j = 0; j < rails.length; j++) wireRail(rails[j]);
    // A short, bounded stagger keeps the first viewport lively without
    // creating dozens of long-lived animation timers.
    var cards = document.querySelectorAll('.cinema-rail .cover-card');
    for (var k = 0; k < cards.length && k < 6; k++) {
      cards[k].classList.add('is-rail-intro');
      cards[k].style.setProperty('--rail-index', k);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initCinema);
  else initCinema();
  window.__asmrlibCinemaInit = initCinema;
})();
"""

# Keep the existing action/loading APIs intact, then add the rail/image hooks.
_UI_JS += _CINEMA_JS

_CSS = """
:root {
  color-scheme: dark;
  --bg: #0a0d14;
  --bg-2: #0e1420;
  --panel: #111826;
  --panel-2: #16202f;
  --panel-3: #1b2638;
  --text: #eef2f9;
  --muted: #93a3b8;
  --faint: #6b7c93;
  --accent: #7aa2ff;
  --accent-2: #a78bfa;
  --accent-3: #f59e0b;
  --ok: #3fba74;
  --ok-soft: #9be7b4;
  --warn: #f87171;
  --line: rgba(148, 163, 184, .14);
  --line-strong: rgba(148, 163, 184, .26);
  --chip: #1a2536;
  --shadow: 0 18px 44px rgba(0, 0, 0, .42);
  --shadow-sm: 0 8px 22px rgba(0, 0, 0, .3);
  --glow: 0 0 0 1px rgba(122, 162, 255, .25), 0 12px 34px rgba(122, 162, 255, .12);
  --radius: 16px;
  --radius-sm: 10px;
  --space-1: .35rem;
  --space-2: .6rem;
  --space-3: 1rem;
  --space-4: 1.35rem;
  --space-5: 1.75rem;
  --stack-gap: 1.1rem;
  --font: "Inter", -apple-system, "Segoe UI", "PingFang SC",
           "Microsoft YaHei", ui-sans-serif, system-ui, sans-serif;
  --mono: ui-monospace, "Cascadia Code", "SF Mono", Consolas, monospace;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--font);
  background:
    radial-gradient(55% 45% at 88% -8%, rgba(122, 162, 255, .14), transparent 60%),
    radial-gradient(45% 40% at 0% 100%, rgba(167, 139, 250, .1), transparent 55%),
    radial-gradient(30% 30% at 50% 0%, rgba(245, 158, 11, .05), transparent 60%),
    var(--bg);
  color: var(--text);
  line-height: 1.55;
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--accent); text-decoration: none; }
a:hover { color: #b3caff; }

::selection { background: rgba(122, 162, 255, .35); }

/* ---------- Nav loading overlay ---------- */
/* IMPORTANT: do NOT leave a full-screen opacity:0 + backdrop-filter layer mounted.
   WebView2/Edge has painted that as a solid black veil on some GPUs, making the
   whole home page look blank even though the DOM underneath is fine. Hide with
   visibility/display when idle; only composite the blur while is-on. */
.nav-loading {
  position: fixed; inset: 0; z-index: 1000;
  display: none;
  align-items: center; justify-content: center;
  pointer-events: none;
  opacity: 0;
  visibility: hidden;
  background: rgba(6, 9, 14, .55);
  transition: opacity .15s ease, visibility 0s linear .15s;
}
.nav-loading.is-on {
  display: flex;
  opacity: 1;
  visibility: visible;
  pointer-events: auto;
  transition: opacity .15s ease, visibility 0s linear 0s;
  /* Blur only while visible — keeps idle state off the compositor. */
  backdrop-filter: blur(3px);
}
/* Soft mode: visible feedback, page still clickable (desktop API / long jobs). */
.nav-loading.is-on.is-soft {
  pointer-events: none;
  background: rgba(6, 9, 14, .28);
  backdrop-filter: blur(1px);
}
.nav-loading-bar {
  position: absolute; top: 0; left: 0; height: 3px; width: 0%;
  background: linear-gradient(90deg, #5b8dff, #a78bfa, #8be0b0);
  box-shadow: 0 0 14px rgba(122, 162, 255, .6);
  transition: width .45s cubic-bezier(.2, .8, .2, 1);
}
.nav-loading-bar.is-done { transition-duration: .15s; }
.nav-loading-panel {
  display: inline-flex; align-items: center; gap: .7rem;
  background: rgba(17, 24, 38, .94); border: 1px solid var(--line-strong);
  border-radius: 999px; padding: .75rem 1.2rem;
  color: var(--text); font-weight: 600; font-size: .92rem;
  box-shadow: var(--shadow);
}
.nav-loading-spin {
  width: 1rem; height: 1rem; border-radius: 50%;
  border: 2px solid rgba(122, 162, 255, .25);
  border-top-color: #8bb4ff;
  animation: nav-spin .7s linear infinite;
  flex-shrink: 0;
}
@keyframes nav-spin { to { transform: rotate(360deg); } }

/* ---------- Top nav ---------- */
.top {
  display: flex; justify-content: space-between; align-items: center;
  padding: .85rem 1.6rem; border-bottom: 1px solid var(--line);
  background: rgba(10, 13, 20, .78);
  position: sticky; top: 0;
  backdrop-filter: blur(16px) saturate(1.4); z-index: 20;
}
.brand {
  display: inline-flex; align-items: center; gap: .5rem;
  font-weight: 800; color: var(--text); letter-spacing: .02em;
  font-size: 1.04rem;
}
.brand-mark {
  width: 1.5rem; height: 1.5rem; border-radius: 8px;
  background: linear-gradient(135deg, #7aa2ff, #a78bfa);
  display: inline-flex; align-items: center; justify-content: center;
  color: #0a0d14;
  box-shadow: 0 4px 12px rgba(122, 162, 255, .35);
  flex-shrink: 0;
}
.brand-ico {
  display: block;
  width: 14px; height: 14px;
}
.brand span { color: var(--muted); font-weight: 600; margin-left: .1rem; }
.top nav { display: flex; align-items: center; gap: .25rem; }
.top nav a {
  margin-left: .15rem; color: var(--muted); font-size: .92rem; font-weight: 500;
  padding: .42rem .8rem; border-radius: 999px;
  transition: color .12s ease, background .12s ease, box-shadow .12s ease;
}
.top nav a:hover { color: var(--text); background: rgba(122, 162, 255, .1); }
.top nav a:first-child { margin-left: 1rem; }
.top nav a.is-active {
  color: #e8efff;
  background: rgba(122, 162, 255, .18);
  border: 1px solid rgba(148, 180, 255, .28);
  box-shadow: 0 0 0 1px rgba(122, 162, 255, .08) inset;
  font-weight: 650;
}
.top nav a.is-active:hover {
  color: #fff;
  background: rgba(122, 162, 255, .24);
}

.wrap { max-width: 1280px; margin: 0 auto; padding: 1.7rem 1.4rem 3.2rem; }
.muted { color: var(--muted); }
code {
  word-break: break-all;
  background: rgba(255, 255, 255, .06);
  border: 1px solid var(--line);
  padding: .1rem .35rem; border-radius: 6px; font-size: .88em;
  font-family: var(--mono);
}
h1 { margin: 0 0 .75rem; font-size: 1.75rem; letter-spacing: .005em; }

/* ---------- Hero ---------- */
.hero {
  position: relative; overflow: hidden;
  background: linear-gradient(150deg, #101a2e 0%, #16203a 55%, #1d2a4a 100%);
  border: 1px solid var(--line-strong); border-radius: 24px;
  padding: 2.1rem 1.9rem 1.6rem; margin-bottom: 1.5rem;
  box-shadow: var(--shadow-sm);
}
.hero:before {
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background:
    radial-gradient(42% 70% at 92% 8%, rgba(167, 139, 250, .4), transparent 60%),
    radial-gradient(38% 62% at 8% 92%, rgba(122, 162, 255, .34), transparent 55%);
  opacity: .6;
}
.hero-content { position: relative; }
.hero-kicker {
  margin: 0 0 .4rem; font-size: .7rem; letter-spacing: .2em;
  color: var(--accent-2); font-weight: 700; text-transform: uppercase;
}
.hero h1 { margin: 0 0 .4rem; font-size: 2.1rem; letter-spacing: -.01em; }
.hero-sub { color: var(--muted); margin: 0 0 1.2rem; max-width: 60ch; }
.hero-stats {
  position: relative; display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr)); gap: .75rem; margin-top: 1.4rem;
}
.hero-stat {
  background: rgba(10, 16, 24, .55); border: 1px solid var(--line);
  border-radius: 16px; padding: .95rem .5rem; text-align: center;
  backdrop-filter: blur(6px);
  transition: border-color .15s ease, transform .15s ease;
}
.hero-stat:hover { border-color: var(--line-strong); transform: translateY(-2px); }
.hero-num {
  display: block; font-size: 1.6rem; font-weight: 800;
  background: linear-gradient(120deg, #cfe0ff, #a78bfa);
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.hero-lbl { color: var(--muted); font-size: .82rem; }

/* Compact home header — title/count + real action pills + search */
.hero-compact {
  padding: 1.05rem 1.2rem 1.1rem;
  margin-bottom: .95rem;
  border-radius: 18px;
}
.hero-compact:before { opacity: .48; }
.hero-compact h1 {
  margin: 0;
  font-size: 1.32rem;
  font-weight: 780;
  letter-spacing: -.015em;
}
.hero-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: .85rem 1.1rem; flex-wrap: wrap;
}
.hero-titles { min-width: 0; flex: 0 1 auto; }
.hero-count {
  margin: .28rem 0 0;
  color: var(--muted);
  font-size: .88rem;
  line-height: 1.4;
}
.hero-count strong {
  color: var(--text);
  font-weight: 750;
  font-variant-numeric: tabular-nums;
}
.hero-count .ok { color: var(--ok); }
.hero-count .ok strong { color: var(--ok-soft); }
.hero-actions {
  display: flex; flex-wrap: wrap; align-items: center;
  gap: .45rem; justify-content: flex-end;
  flex: 1 1 auto;
}
.hero-pill {
  display: inline-flex; align-items: baseline; gap: .38rem;
  padding: .48rem .85rem;
  border-radius: 999px;
  border: 1px solid rgba(148, 163, 184, .22);
  background: rgba(8, 12, 20, .55);
  color: var(--text);
  text-decoration: none;
  font-size: .9rem;
  line-height: 1.2;
  backdrop-filter: blur(8px);
  transition: border-color .14s ease, background .14s ease, transform .14s ease,
    box-shadow .14s ease;
  box-shadow: 0 4px 14px rgba(0, 0, 0, .18);
}
.hero-pill strong { font-weight: 720; letter-spacing: -.01em; }
.hero-pill-k {
  font-size: .68rem;
  font-weight: 750;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--muted);
}
.hero-pill:hover {
  border-color: rgba(122, 162, 255, .45);
  background: rgba(122, 162, 255, .12);
  color: #fff;
  text-decoration: none;
  transform: translateY(-1px);
  box-shadow: 0 8px 20px rgba(40, 80, 180, .18);
}
.hero-pill-auto {
  border-color: rgba(167, 139, 250, .4);
  background:
    linear-gradient(120deg, rgba(167, 139, 250, .18), rgba(122, 162, 255, .1)),
    rgba(12, 14, 24, .65);
  box-shadow: 0 6px 18px rgba(120, 90, 220, .16);
}
.hero-pill-auto .hero-pill-k { color: #c4b5fd; }
.hero-pill-auto strong { color: #ede9fe; }
.hero-pill-auto:hover {
  border-color: rgba(196, 181, 253, .65);
  background:
    linear-gradient(120deg, rgba(167, 139, 250, .28), rgba(122, 162, 255, .16)),
    rgba(18, 16, 32, .75);
  box-shadow: 0 10px 24px rgba(130, 100, 230, .22);
}
.hero-pill-quiet {
  border-color: rgba(148, 163, 184, .14);
  background: rgba(255, 255, 255, .03);
  box-shadow: none;
  color: var(--muted);
}
.hero-pill-quiet:hover {
  color: var(--text);
  border-color: rgba(148, 163, 184, .28);
  background: rgba(255, 255, 255, .06);
  box-shadow: none;
}
.hero-compact .search { margin-top: .85rem; }
.search-compact input {
  min-width: 0;
  padding: .62rem .9rem;
  border-radius: 11px;
}
.search-compact button {
  padding: .62rem 1.05rem;
  border-radius: 11px;
}

/* ---------- Search ---------- */
.search { display: flex; gap: .5rem; flex-wrap: wrap; margin: .4rem 0 0; }
.search input {
  flex: 1; min-width: 160px; background: rgba(10, 16, 24, .6); color: var(--text);
  border: 1px solid var(--line); border-radius: 12px; padding: .72rem .95rem;
  font: inherit; transition: border-color .12s ease, box-shadow .12s ease;
}
.search input::placeholder { color: var(--faint); }
.search input:focus {
  outline: none; border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(122, 162, 255, .18);
}
.search button, .button {
  background: rgba(122, 162, 255, .16);
  color: #e8efff;
  border: 1px solid rgba(148, 180, 255, .38);
  border-radius: 11px;
  padding: .62rem 1.05rem; font-weight: 650; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center; gap: .35rem;
  font: inherit; line-height: 1.2;
  box-shadow: none;
  transition: background .14s ease, border-color .14s ease, color .14s ease;
}
.search button:hover, .button:hover {
  background: rgba(122, 162, 255, .26);
  border-color: rgba(180, 205, 255, .55);
  color: #fff;
  text-decoration: none;
}
.button-secondary {
  background: transparent; color: var(--muted);
  border: 1px solid var(--line-strong); font-weight: 600; box-shadow: none;
}
.button-secondary:hover {
  background: rgba(255, 255, 255, .04);
  border-color: rgba(148, 163, 184, .4);
  color: var(--text);
}
.button-sm { padding: .4rem .75rem; font-size: .82rem; border-radius: 9px; }
.button-lg { padding: .78rem 1.25rem; font-size: .95rem; border-radius: 12px; }

/* List/author page titles — one system, not raw h1 soup */
.page-header {
  display: flex; align-items: flex-end; justify-content: space-between;
  gap: .85rem 1.2rem; flex-wrap: wrap;
  margin: 0 0 1rem;
}
.page-header-main { min-width: 0; flex: 1 1 auto; }
.page-title {
  margin: 0; font-size: clamp(1.35rem, 2.4vw, 1.75rem);
  font-weight: 800; letter-spacing: -.015em; line-height: 1.25;
}
.page-sub { margin: .35rem 0 0; font-size: .9rem; }
.page-header-trail .pill { margin: 0; }
.page-header-trail {
  display: flex; align-items: center; gap: .5rem; flex-wrap: wrap;
}
.page-header + .search { margin-top: 0; margin-bottom: 1rem; }

/* ---------- Sections ---------- */
.section-block {
  background: rgba(17, 24, 38, .8); border: 1px solid var(--line);
  border-radius: 20px; padding: 1.2rem 1.3rem; margin: 1.2rem 0;
  box-shadow: var(--shadow-sm);
  backdrop-filter: blur(4px);
}
.section-title { margin: 0 0 .85rem; font-size: 1.14rem; font-weight: 700; letter-spacing: .005em; }
.section-head { display: flex; align-items: center; gap: .65rem; margin-bottom: .9rem; }
.section-head .section-title { margin: 0; flex: 1; }
.section-count { font-size: .85rem; }
.section-foot { text-align: center; padding: .75rem; background: transparent; border-style: dashed; }
.section-note { background: rgba(18, 27, 40, .85); }
.sync-banner {
  position: relative;
  display: flex; flex-wrap: wrap; align-items: center; gap: .55rem .85rem;
  margin: 0 0 1rem; padding: .75rem 1.05rem;
  border-radius: 14px;
  border: 1px solid var(--line);
  background: rgba(17, 24, 38, .78);
  overflow: hidden;
  transition: border-color .25s ease, background .25s ease, box-shadow .25s ease;
}
.sync-banner.is-active {
  border-color: rgba(122, 162, 255, .35);
  background:
    linear-gradient(90deg, rgba(122, 162, 255, .12), transparent 55%),
    rgba(17, 24, 38, .88);
}
.sync-banner.is-running {
  border-color: rgba(122, 162, 255, .5);
  box-shadow: 0 0 0 1px rgba(122, 162, 255, .08), 0 8px 28px rgba(40, 80, 180, .12);
}
.sync-banner.is-running::before {
  content: "";
  position: absolute; inset: 0 auto 0 0; width: 3px;
  background: linear-gradient(180deg, var(--accent), var(--accent-2));
  animation: sync-bar-pulse 1.2s ease-in-out infinite;
}
.sync-banner.is-running::after {
  content: "";
  position: absolute; left: -40%; top: 0; bottom: 0; width: 40%;
  background: linear-gradient(
    90deg,
    transparent,
    rgba(122, 162, 255, .1),
    transparent
  );
  animation: sync-shimmer 1.6s ease-in-out infinite;
  pointer-events: none;
}
.sync-banner.is-done {
  border-color: rgba(63, 186, 116, .4);
  background:
    linear-gradient(90deg, rgba(63, 186, 116, .12), transparent 55%),
    rgba(17, 24, 38, .88);
  animation: sync-done-flash .7s ease-out 1;
}
.sync-banner.is-idle { opacity: .92; }
.sync-banner-k {
  font-size: .68rem; letter-spacing: .14em; text-transform: uppercase;
  font-weight: 700; color: var(--accent-2);
  display: inline-flex; align-items: center; gap: .45rem;
}
.sync-banner.is-running .sync-banner-k { color: var(--accent); }
.sync-banner.is-done .sync-banner-k { color: var(--ok); }
.sync-spinner {
  width: .85rem; height: .85rem; border-radius: 50%;
  border: 2px solid rgba(122, 162, 255, .25);
  border-top-color: var(--accent);
  animation: nav-spin .7s linear infinite;
  flex-shrink: 0;
}
.sync-dot {
  width: .55rem; height: .55rem; border-radius: 50%;
  background: var(--ok);
  box-shadow: 0 0 0 0 rgba(63, 186, 116, .45);
  animation: none;
  flex-shrink: 0;
}
.sync-banner-msg {
  flex: 1 1 auto; color: var(--text); font-size: .92rem;
  min-width: 12ch;
}
.sync-banner.is-running .sync-banner-msg {
  background: linear-gradient(
    90deg,
    var(--text) 0%,
    var(--text) 40%,
    rgba(200, 220, 255, .95) 50%,
    var(--text) 60%,
    var(--text) 100%
  );
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: sync-text-shine 2s linear infinite;
}
.sync-banner .text-link { white-space: nowrap; position: relative; z-index: 1; }
@keyframes sync-shimmer {
  0% { transform: translateX(0); }
  100% { transform: translateX(320%); }
}
@keyframes sync-bar-pulse {
  0%, 100% { opacity: .55; }
  50% { opacity: 1; }
}
@keyframes sync-text-shine {
  0% { background-position: 100% 0; }
  100% { background-position: -100% 0; }
}
@keyframes sync-done-flash {
  0% { box-shadow: 0 0 0 0 rgba(63, 186, 116, .35); }
  100% { box-shadow: 0 0 0 12px rgba(63, 186, 116, 0); }
}

.empty-state {
  text-align: center; padding: 2.8rem 1.5rem 2.6rem;
  background:
    radial-gradient(50% 60% at 50% 0%, rgba(122, 162, 255, .1), transparent 70%),
    rgba(17, 24, 38, .88);
  border: 1px dashed var(--line-strong);
  border-radius: 20px;
}
.empty-state h1 { margin: 0 0 .55rem; font-size: 1.35rem; }
.empty-kicker {
  margin: 0 0 .55rem; font-size: .7rem; letter-spacing: .16em;
  text-transform: uppercase; color: var(--accent-2); font-weight: 700;
}
.empty-msg { margin: 0 auto .95rem; max-width: 42ch; line-height: 1.55; }
.empty-state .button { margin-top: .25rem; }
.empty-inline {
  text-align: center; padding: 1.4rem .75rem;
  border: 1px dashed var(--line); border-radius: 14px;
  background: rgba(10, 14, 22, .35);
}
.text-link { color: var(--accent); font-size: .92rem; font-weight: 500; }
.text-link:hover { text-decoration: underline; }

/* ---------- Chips ---------- */
.chips { display: flex; flex-wrap: wrap; gap: .5rem; margin: .2rem 0 .4rem; }
.chip {
  background: var(--chip); color: var(--text); border-radius: 999px;
  padding: .4rem .9rem; border: 1px solid var(--line);
  font-size: .9rem; font-weight: 500;
  transition: border-color .12s ease, background .12s ease, transform .12s ease;
}
.chip:hover {
  border-color: var(--accent); background: rgba(122, 162, 255, .12);
  text-decoration: none; color: var(--text); transform: translateY(-1px);
}
.chip em { color: var(--muted); font-style: normal; margin-left: .3rem; }
.chip-user {
  display: inline-flex; align-items: center; gap: .3rem;
  background: rgba(167, 139, 250, .14); border-color: rgba(167, 139, 250, .4);
}
.chip-x {
  background: none; border: 0; color: var(--muted); cursor: pointer;
  font: inherit; font-size: .8rem; line-height: 1; padding: 0 .1rem;
}
.chip-x:hover { color: var(--warn); }
.tag-add { display: flex; gap: .5rem; margin-top: .6rem; }
.tag-add input {
  flex: 1; min-width: 140px; max-width: 320px;
  background: rgba(10, 16, 24, .6); color: var(--text);
  border: 1px solid var(--line); border-radius: 10px; padding: .5rem .8rem;
  font: inherit; font-size: .88rem;
}
.tag-add input:focus { outline: none; border-color: var(--accent); }
.tag-add .button { padding: .5rem .9rem; font-size: .85rem; }
/* ---------- Cover grid ---------- */
.cover-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  gap: 1.05rem;
}
.cover-card {
  display: flex; flex-direction: column; background: var(--panel);
  border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden;
  color: inherit;
  transition: transform .16s ease, border-color .16s ease, box-shadow .16s ease;
}
.cover-card:hover {
  border-color: rgba(122, 162, 255, .6); text-decoration: none;
  transform: translateY(-4px); box-shadow: var(--glow); color: inherit;
}
.cover-thumb {
  aspect-ratio: 16 / 10; width: 100%; background-size: cover;
  background-position: center; position: relative; display: flex;
  align-items: flex-end; justify-content: flex-start; padding: .55rem;
  background-color: #0c1218;
  overflow: hidden;
}
.cover-thumb::after {
  content: ""; position: absolute; inset: 0;
  background: linear-gradient(180deg, transparent 55%, rgba(8, 12, 18, .55));
  pointer-events: none;
}
.cover-thumb img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; transition: transform .3s ease; }
.cover-card:hover .cover-thumb img { transform: scale(1.05); }
.cover-initial {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
  font-size: 3.2rem; font-weight: 800; color: rgba(255, 255, 255, .4);
  letter-spacing: .04em;
}
.cover-badge {
  background: rgba(8, 13, 20, .82); color: var(--text);
  border: 1px solid rgba(255, 255, 255, .14); border-radius: 999px;
  padding: .16rem .55rem; font-size: .75rem; margin-right: .35rem;
  backdrop-filter: blur(6px); position: relative; z-index: 1;
}
.cover-badge-ok {
  background: rgba(63, 186, 116, .24); color: var(--ok-soft);
  border-color: rgba(63, 186, 116, .4);
}
.cover-info {
  display: flex; flex-direction: column; gap: .28rem;
  padding: .8rem .9rem .95rem; flex: 1;
}
.cover-title {
  font-size: .9rem; line-height: 1.4; color: var(--text); font-weight: 600;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.cover-meta { font-size: .78rem; }

/* ---------- Explore cards ---------- */
.explore-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: .8rem;
}
.explore-card {
  display: flex; flex-direction: column; gap: .25rem;
  background: var(--panel-2); border: 1px solid var(--line);
  border-radius: 16px; padding: 1.05rem 1.1rem; color: inherit;
  transition: border-color .12s ease, transform .12s ease, box-shadow .12s ease;
}
.explore-card:hover {
  border-color: var(--accent-2); text-decoration: none;
  transform: translateY(-2px); box-shadow: var(--shadow-sm); color: inherit;
}
.explore-kicker {
  font-size: .66rem; letter-spacing: .14em; color: var(--accent-2); font-weight: 700;
  text-transform: uppercase;
}
.explore-card strong { font-size: 1.05rem; }
.explore-card-home {
  background: linear-gradient(145deg, rgba(122, 162, 255, .14), rgba(167, 139, 250, .12));
  border-color: rgba(122, 162, 255, .4);
}
.cover-tags {
  font-size: .75rem; line-height: 1.3;
  display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical;
  overflow: hidden;
}

/* ---------- In-page watch player ---------- */
.watch-shell {
  background: rgba(17, 24, 38, .85); border: 1px solid var(--line);
  border-radius: 22px; padding: 1.05rem 1.15rem 1.25rem; margin: 0 0 1.15rem;
  box-shadow: var(--shadow-sm);
}
.watch-toolbar {
  display: flex; justify-content: space-between; align-items: flex-end;
  gap: .85rem; flex-wrap: wrap; margin-bottom: .95rem;
}
.watch-toolbar h1 { margin: 0 0 .2rem; font-size: 1.25rem; font-weight: 700; }
.watch-toolbar-actions { display: flex; flex-wrap: wrap; gap: .45rem; }
.watch-url { margin: .75rem 0 0; font-size: .82rem; word-break: break-all; }
.external-watch-kicker {
  margin: 0 0 .3rem; font-size: .7rem; letter-spacing: .16em;
  color: var(--accent-2); font-weight: 700; text-transform: uppercase;
}
@media (max-width: 720px) { .watch-toolbar h1 { font-size: 1.05rem; } }

/* ---------- Mini player window ---------- */
html.mini-html, body.mini-body {
  margin: 0; padding: 0; height: 100%; overflow: hidden;
  background: #0a0d14;
}
.mini-wrap { height: 100%; min-height: 100vh; box-sizing: border-box; padding: 0; display: flex; flex-direction: column; }
.watch-shell-mini { flex: 1; margin: 0; border-radius: 0; border: 0; padding: 0; display: flex; flex-direction: column; background: #0a0d14; min-height: 100vh; }
.mini-bar {
  display: flex; align-items: center; gap: .5rem;
  padding: .45rem .65rem; background: #111826;
  border-bottom: 1px solid var(--line); flex-shrink: 0;
  user-select: none; -webkit-user-select: none;
}
.mini-title { flex: 1; min-width: 0; font-size: .82rem; font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mini-tag { font-size: .68rem; font-weight: 700; letter-spacing: .04em; color: #082f49; background: #38bdf8; border-radius: 999px; padding: .12rem .45rem; flex-shrink: 0; }
.mini-local-player { flex: 1; min-height: 0; display: flex; align-items: center; justify-content: center; background: #000; padding: .4rem; }
.mini-local-player video, .mini-local-player audio { width: 100%; max-height: 100%; border-radius: 0; background: #000; }

/* ---------- Browse (site homepage preview) ---------- */
.browse-hero {
  display: flex; justify-content: space-between; align-items: flex-end;
  gap: 1rem; flex-wrap: wrap;
  background: linear-gradient(150deg, #101a2e 0%, #16203a 55%, #1d2a4a 100%);
  border: 1px solid var(--line-strong); border-radius: 22px;
  padding: 1.5rem 1.6rem; margin-bottom: 1.15rem;
  box-shadow: var(--shadow-sm);
}
.browse-hero h1 { margin: 0 0 .35rem; font-size: 1.75rem; }
.browse-hero-meta { display: flex; flex-wrap: wrap; gap: .45rem; align-items: center; }

/* ---------- Detail ---------- */
.crumb {
  color: var(--muted); font-size: .84rem; margin: 0 0 1rem;
  display: flex; align-items: center; flex-wrap: wrap; gap: .1rem;
  letter-spacing: .01em;
}
.crumb a { color: var(--muted); padding: .1rem .15rem; border-radius: 6px; }
.crumb a:hover {
  color: var(--accent); text-decoration: none;
  background: rgba(122, 162, 255, .08);
}
.crumb-sep { margin: 0 .3rem; opacity: .45; }
.crumb-current { color: var(--text); font-weight: 650; }
.detail-hero {
  position: relative; overflow: hidden;
  display: grid;
  grid-template-columns: minmax(300px, 42%) 1fr;
  gap: 1.85rem;
  align-items: center;
  background: #0c1018;
  border: 1px solid rgba(148, 163, 184, .16);
  border-radius: 24px;
  padding: 1.55rem 1.6rem;
  margin-bottom: 1.5rem;
  box-shadow:
    0 18px 48px rgba(0, 0, 0, .5),
    0 0 0 1px rgba(122, 162, 255, .08) inset;
  transition: border-color .25s ease, box-shadow .25s ease;
}
/* Cinematic cover wash — must stay visible, not washed out. */
.detail-hero-bg {
  position: absolute; inset: -12% -8%; z-index: 0;
  background-size: cover; background-position: center;
  filter: saturate(1.55) blur(28px) brightness(1.05);
  transform: scale(1.08);
  opacity: .72;
  pointer-events: none;
}
.detail-hero-bg-fade {
  position: absolute; inset: 0; z-index: 0; pointer-events: none;
  background:
    linear-gradient(100deg,
      rgba(8, 11, 18, .28) 0%,
      rgba(8, 11, 18, .55) 42%,
      rgba(8, 11, 18, .78) 100%),
    linear-gradient(180deg,
      rgba(10, 14, 22, .15) 0%,
      rgba(10, 14, 22, .55) 100%);
}
.detail-hero > .detail-poster,
.detail-hero > .detail-hero-main { position: relative; z-index: 1; }
.detail-hero:hover {
  border-color: rgba(122, 162, 255, .38);
  box-shadow:
    0 22px 56px rgba(0, 0, 0, .55),
    0 0 0 1px rgba(122, 162, 255, .14) inset,
    0 0 40px rgba(122, 162, 255, .08);
}
/* Real asmrlib covers are landscape — stop portrait-cropping them. */
.detail-poster {
  position: relative; border-radius: 18px; overflow: hidden;
  aspect-ratio: 16 / 10; width: 100%;
  background: linear-gradient(135deg, #0a0f18 0%, #12171f 100%);
  border: 1px solid rgba(255, 255, 255, .14);
  box-shadow:
    0 16px 40px rgba(0, 0, 0, .55),
    0 0 0 1px rgba(122, 162, 255, .12),
    0 0 28px rgba(122, 162, 255, .12);
  transition: box-shadow .3s ease, transform .3s ease, border-color .3s ease;
}
.detail-poster:hover {
  border-color: rgba(180, 205, 255, .4);
  box-shadow:
    0 20px 48px rgba(0, 0, 0, .6),
    0 0 0 1px rgba(122, 162, 255, .22),
    0 0 36px rgba(122, 162, 255, .18);
  transform: translateY(-3px) scale(1.01);
}
.detail-poster-img, .detail-poster-plate {
  width: 100%; height: 100%; background-size: cover; background-position: center;
  display: flex; align-items: center; justify-content: center;
  transition: transform .45s ease;
}
.detail-hero:hover .detail-poster-img { transform: scale(1.06); }
.detail-poster-glow {
  position: absolute; inset: auto 0 0 0; height: 55%;
  background: linear-gradient(180deg, transparent, rgba(4, 8, 14, .72));
  pointer-events: none;
}
.detail-poster-initial {
  font-size: 4rem; font-weight: 800; color: rgba(255, 255, 255, .5);
  text-shadow: 0 6px 28px rgba(0, 0, 0, .45);
  letter-spacing: .02em;
}
.detail-hero-main {
  display: flex; flex-direction: column; justify-content: center;
  gap: .85rem; min-width: 0; padding: .2rem .15rem .1rem 0;
}
.detail-kicker {
  display: inline-flex; align-items: center; gap: .45rem;
  margin: 0; width: fit-content;
  font-size: .72rem; letter-spacing: .18em; font-weight: 800;
  text-transform: uppercase; color: #d4c4ff;
  background: rgba(167, 139, 250, .22);
  border: 1px solid rgba(196, 181, 253, .45);
  border-radius: 999px; padding: .28rem .85rem;
  box-shadow: 0 4px 16px rgba(139, 92, 246, .18);
}
.detail-title {
  margin: 0; font-size: clamp(1.55rem, 2.8vw, 2.15rem);
  line-height: 1.25; font-weight: 800; letter-spacing: -.02em;
  text-wrap: balance;
  text-shadow: 0 3px 22px rgba(0, 0, 0, .55);
}
.detail-pills { display: flex; flex-wrap: wrap; gap: .5rem; }
.pill {
  display: inline-flex; align-items: baseline; gap: .38rem;
  background: rgba(6, 10, 16, .55); border: 1px solid rgba(255, 255, 255, .14);
  border-radius: 999px; padding: .4rem .9rem; font-size: .84rem;
  color: inherit; backdrop-filter: blur(12px);
  box-shadow: 0 4px 14px rgba(0, 0, 0, .22);
}
a.pill:hover {
  border-color: var(--accent); text-decoration: none; color: inherit;
  background: rgba(122, 162, 255, .18);
}
.pill-k {
  color: #a8b8cc; font-size: .7rem; text-transform: uppercase;
  letter-spacing: .06em; font-weight: 700;
}
.pill-v { color: var(--text); font-weight: 700; }
.pill-status .pill-v { color: var(--ok-soft); }
.detail-tags {
  margin: 0;
  display: flex; flex-wrap: wrap; gap: .4rem;
}
.detail-panel { border-radius: 22px; }
.detail-panel .section-title { letter-spacing: .01em; }

/* Quiet band under hero: tags + annotate — not fighting hero actions */
.meta-strip {
  display: flex; flex-wrap: wrap; align-items: center; gap: .65rem 1rem;
  margin: -0.35rem 0 var(--stack-gap);
  padding: .85rem 1.05rem;
  background: rgba(17, 24, 38, .55);
  border: 1px solid var(--line);
  border-radius: 16px;
}
.meta-strip .detail-tags {
  margin: 0; padding: 0; border: 0; flex: 1 1 auto;
}
.meta-strip .tag-add {
  margin: 0; max-width: 360px; flex: 0 1 360px;
}
.meta-strip .tag-add input {
  background: rgba(6, 10, 16, .45);
}

/* Compact env panel — actions already live in the hero */
.env-panel { padding-top: .95rem; padding-bottom: .95rem; }
.env-panel .section-title { font-size: 1rem; }
.env-panel .action-hint { margin-top: .45rem; font-size: .84rem; }
.section-block { margin-bottom: var(--stack-gap); }

/* ---------- Actions (matte, not candy) ---------- */
.status-chip {
  font-size: .74rem; font-weight: 600; letter-spacing: .03em;
  padding: .2rem .6rem; border-radius: 999px;
  background: rgba(255, 255, 255, .05); color: var(--muted);
  border: 1px solid var(--line);
}
.status-ok {
  background: rgba(63, 186, 116, .12); color: var(--ok-soft);
  border-color: rgba(63, 186, 116, .32);
}
.action-panel { padding-bottom: 1rem; }
.action-row, .detail-hero-actions {
  display: flex; flex-wrap: wrap; gap: .5rem; align-items: center;
}
.detail-hero-actions {
  margin-top: .35rem;
  padding-top: .75rem;
  border-top: 1px solid rgba(255, 255, 255, .1);
}
.action-btn {
  display: inline-flex; align-items: center; gap: .42rem;
  background: rgba(255, 255, 255, .05);
  color: var(--text);
  border: 1px solid rgba(255, 255, 255, .12);
  border-radius: 11px;
  padding: .62rem .95rem;
  font-weight: 620; font-size: .88rem;
  box-shadow: none;
  cursor: pointer;
  transition: background .14s ease, border-color .14s ease, color .14s ease;
}
.action-btn:hover {
  background: rgba(255, 255, 255, .09);
  border-color: rgba(255, 255, 255, .22);
  text-decoration: none;
  color: #fff;
}
.action-ico { font-size: .82rem; opacity: .85; line-height: 1; }
/* Primary: online play — soft blue fill, light text */
.action-online {
  background: rgba(122, 162, 255, .18);
  border-color: rgba(148, 180, 255, .4);
  color: #dce8ff;
}
.action-online:hover {
  background: rgba(122, 162, 255, .28);
  border-color: rgba(180, 205, 255, .55);
  color: #fff;
}
/* Local play — soft green tint */
.action-local {
  background: rgba(63, 186, 116, .14);
  border-color: rgba(110, 210, 150, .35);
  color: #c8f0d8;
}
.action-local:hover {
  background: rgba(63, 186, 116, .22);
  border-color: rgba(140, 230, 175, .5);
  color: #e8fff0;
}
/* Mini player — quiet violet */
.action-mini {
  background: rgba(167, 139, 250, .1);
  border-color: rgba(196, 181, 253, .28);
  color: #ddd0ff;
}
.action-mini:hover {
  background: rgba(167, 139, 250, .18);
  border-color: rgba(196, 181, 253, .45);
  color: #f0eaff;
}
/* Local media management stays visually secondary. */
button.action-btn { font: inherit; }
.text-btn { background: none; border: 0; padding: 0; margin: 0; cursor: pointer; font: inherit; color: var(--accent); }
.text-btn:hover { text-decoration: underline; }
.danger-link { color: var(--warn); }
.danger-link:hover { color: #fca5a5; text-decoration: underline; }
.action-ghost {
  background: transparent;
  color: var(--muted);
  border: 1px solid rgba(255, 255, 255, .12);
}
.action-ghost:hover {
  border-color: rgba(255, 255, 255, .28);
  color: var(--text);
  background: rgba(255, 255, 255, .04);
}
.action-sm {
  padding: .4rem .7rem; font-size: .8rem; font-weight: 600;
  border-radius: 9px;
}
.action-lg {
  padding: .72rem 1.1rem; font-size: .95rem; font-weight: 650;
  border-radius: 12px;
}
.rec-panel { padding-top: 1rem; }
.rec-panel > .section-head { margin-bottom: .75rem; }
.action-danger {
  background: transparent; color: var(--warn);
  border: 1px solid rgba(248, 113, 113, .35);
}
.action-danger:hover {
  border-color: rgba(248, 113, 113, .55);
  color: #fecaca; background: rgba(239, 68, 68, .12);
}
.media-card-actions { display: flex; flex-wrap: wrap; gap: .5rem; padding: .6rem .85rem; border-top: 1px solid var(--line); }
.action-hint { margin: .6rem 0 0; font-size: .88rem; line-height: 1.5; }

/* ---------- Media / ref cards ---------- */
.media-stack { display: flex; flex-direction: column; gap: 1rem; }
.media-card {
  background: linear-gradient(135deg, #171a23 0%, #1e222e 100%);
  border: 1px solid rgba(91, 140, 255, .15);
  border-radius: 16px; overflow: hidden;
  transition: all .2s ease;
}
.media-card:hover {
  border-color: rgba(91, 140, 255, .35);
  box-shadow: 0 12px 28px rgba(0, 0, 0, .4), 0 0 0 1px rgba(91, 140, 255, .12) inset;
  transform: translateY(-2px);
}
.media-card-head {
  display: flex; align-items: center; gap: .55rem; flex-wrap: wrap;
  padding: 1rem 1.15rem;
  border-bottom: 1px solid rgba(91, 140, 255, .12);
  background: rgba(91, 140, 255, .04);
}
.media-kind {
  font-size: .68rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .05em; color: #10b981;
  background: rgba(16, 185, 129, .12); border: 1px solid rgba(16, 185, 129, .28);
  border-radius: 6px; padding: .15rem .5rem;
}
.media-card-head strong { flex: 1; min-width: 0; font-size: .95rem; font-weight: 600; }
.media-file { font-size: .78rem; color: var(--muted); }
.media-card-player {
  padding: 1.2rem 1.15rem;
  background: linear-gradient(180deg, #0a0f17 0%, #050810 100%);
}
.media-card-player video, .media-card-player audio {
  width: 100%; max-height: 55vh; border-radius: 12px; display: block;
  box-shadow:
    0 12px 32px rgba(0, 0, 0, .5),
    0 0 0 1px rgba(91, 140, 255, .08);
}
.media-card-foot {
  padding: .85rem 1.15rem;
  border-top: 1px solid rgba(91, 140, 255, .12);
  display: flex; flex-wrap: wrap; gap: .45rem; align-items: center;
  background: rgba(91, 140, 255, .03);
}
video, audio { width: 100%; max-height: 70vh; background: #000; border-radius: 14px; }

/* ---------- Temp frame preview ---------- */
.preview-panel {
  border-top: 1px dashed var(--line);
  padding: .85rem 1rem 1rem;
  background:
    radial-gradient(60% 80% at 100% 0%, rgba(122, 162, 255, .08), transparent 55%),
    rgba(8, 12, 20, .55);
}
.preview-panel.is-busy { opacity: .96; }
.preview-head {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: .6rem; flex-wrap: wrap; margin-bottom: .7rem;
}
.preview-title {
  font-size: .82rem; font-weight: 700; letter-spacing: .04em;
  text-transform: uppercase; color: var(--accent);
}
.preview-meta { font-size: .78rem; }
.preview-note { margin: .7rem 0 0; font-size: .78rem; line-height: 1.45; }
.preview-error {
  margin: .4rem 0; color: #f0a0a0; font-size: .88rem;
  background: rgba(80, 24, 24, .35); border: 1px solid #7a3030;
  border-radius: 12px; padding: .7rem .85rem;
}
.preview-grid {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: .65rem;
}
.preview-cell { margin: 0; min-width: 0; }
.preview-shot {
  position: relative; display: block; width: 100%; padding: 0; border: 0;
  border-radius: 12px; overflow: hidden; cursor: zoom-in;
  background: #0a0f18; border: 1px solid var(--line);
  aspect-ratio: 16 / 10;
  transition: border-color .14s ease, transform .14s ease, box-shadow .14s ease;
}
.preview-shot:hover {
  border-color: rgba(122, 162, 255, .55);
  transform: translateY(-2px);
  box-shadow: 0 10px 24px rgba(0, 0, 0, .35);
}
.preview-shot img {
  width: 100%; height: 100%; object-fit: cover; display: block;
  background: #0a0f18;
}
.preview-time {
  position: absolute; left: .45rem; bottom: .45rem;
  font-size: .72rem; font-weight: 700; letter-spacing: .02em;
  color: #eef2f9; background: rgba(8, 12, 18, .78);
  border: 1px solid rgba(255, 255, 255, .12); border-radius: 999px;
  padding: .12rem .48rem; backdrop-filter: blur(6px);
}
.preview-cell.is-loading {
  border-radius: 12px; overflow: hidden; border: 1px solid var(--line);
  background: rgba(12, 18, 28, .8); aspect-ratio: 16 / 10;
  position: relative;
}
.preview-skel {
  position: absolute; inset: 0;
  background: linear-gradient(
    110deg,
    rgba(255, 255, 255, .03) 0%,
    rgba(122, 162, 255, .12) 45%,
    rgba(255, 255, 255, .03) 90%
  );
  background-size: 200% 100%;
  animation: preview-shimmer 1.15s ease-in-out infinite;
}
.preview-skel-label {
  position: absolute; left: .5rem; bottom: .5rem;
  width: 2.6rem; height: .85rem; border-radius: 999px;
  background: rgba(255, 255, 255, .08);
}
@keyframes preview-shimmer {
  0% { background-position: 120% 0; }
  100% { background-position: -40% 0; }
}
.preview-lightbox {
  position: fixed; inset: 0; z-index: 1200;
  background: rgba(4, 7, 12, .82); backdrop-filter: blur(8px);
  display: flex; align-items: center; justify-content: center;
  padding: 1.5rem;
}
.preview-lightbox[hidden] { display: none !important; }
.preview-lightbox img {
  max-width: min(1100px, 94vw); max-height: 86vh;
  border-radius: 14px; border: 1px solid var(--line-strong);
  box-shadow: var(--shadow); background: #000;
}
.preview-lightbox-x {
  position: absolute; top: 1rem; right: 1rem;
  width: 2.2rem; height: 2.2rem; border-radius: 999px;
  border: 1px solid var(--line-strong); background: rgba(17, 24, 38, .9);
  color: var(--text); font: inherit; font-size: 1rem; cursor: pointer;
}
.preview-lightbox-x:hover { border-color: var(--accent); color: var(--accent); }
body.has-lightbox { overflow: hidden; }

.ref-stack { display: flex; flex-direction: column; gap: .55rem; }
.ref-card {
  display: flex; justify-content: space-between; align-items: center;
  gap: .8rem; flex-wrap: wrap;
  background: rgba(10, 16, 24, .45); border: 1px solid var(--line);
  border-radius: 14px; padding: .8rem .95rem;
  transition: border-color .12s ease, transform .12s ease;
}
.ref-card:hover { border-color: rgba(122, 162, 255, .45); transform: translateY(-1px); }
.ref-card-main { display: flex; flex-wrap: wrap; align-items: baseline; gap: .3rem .6rem; min-width: 0; flex: 1; }
.ref-label { font-size: .92rem; }
.ref-host {
  font-size: .76rem; color: var(--accent); font-family: var(--mono);
  background: rgba(122, 162, 255, .12); border-radius: 6px; padding: .08rem .4rem;
}
.ref-meta { font-size: .78rem; }
.ref-card-actions { display: flex; gap: .35rem; flex-wrap: wrap; flex-shrink: 0; }

/* ---------- Local media management ---------- */
.rec-group { margin-bottom: 1.1rem; }
.rec-group-head {
  display: flex; align-items: center; gap: .8rem;
  background: linear-gradient(120deg, rgba(17, 24, 38, .9), rgba(22, 32, 47, .9));
  border: 1px solid var(--line); border-radius: 16px;
  padding: .7rem .9rem; margin-bottom: .6rem; color: inherit;
  transition: border-color .14s ease, transform .14s ease;
}
.rec-group-head:hover { border-color: rgba(122, 162, 255, .5); text-decoration: none; color: inherit; transform: translateY(-1px); }
.rec-group-cover {
  width: 3.4rem; height: 3.4rem; border-radius: 12px; flex-shrink: 0;
  background-size: cover; background-position: center;
  border: 1px solid var(--line-strong); box-shadow: var(--shadow-sm);
}
.rec-group-cover-plain {
  background: linear-gradient(135deg, #7aa2ff, #a78bfa);
  display: flex; align-items: center; justify-content: center;
  color: #0a0d14; font-weight: 900; font-size: 1.2rem;
}
.rec-group-head-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: .1rem; }
.rec-group-head-main strong { font-size: .98rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rec-group-arrow { color: var(--faint); font-size: 1.1rem; }
.rec-group-list { display: flex; flex-direction: column; gap: .55rem; }
.rec-card {
  display: grid; grid-template-columns: 1fr auto auto; gap: .6rem .8rem;
  align-items: center;
  background: rgba(10, 16, 24, .5); border: 1px solid var(--line);
  border-radius: 14px; padding: .7rem .9rem;
  transition: border-color .14s ease;
}
.rec-card:hover { border-color: var(--line-strong); }
.rec-card-main { display: flex; flex-direction: column; gap: .1rem; min-width: 0; }
.rec-label { font-size: .92rem; font-weight: 600; }
.rec-file { font-size: .76rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 56ch; }
.rec-card-meta { display: flex; align-items: center; gap: .6rem; font-size: .82rem; }
.rec-badge {
  font-family: var(--mono); font-size: .78rem; font-weight: 700;
  color: var(--accent-2); background: rgba(167, 139, 250, .12);
  border: 1px solid rgba(167, 139, 250, .3); border-radius: 999px;
  padding: .14rem .55rem;
}
.rec-card-actions { display: flex; gap: .35rem; flex-wrap: wrap; justify-content: flex-end; }
.rec-card-player { grid-column: 1 / -1; display: none; }
.rec-card-player video { width: 100%; max-height: 48vh; border-radius: 10px; background: #000; }

/* ---------- Pager ---------- */
#page-top { scroll-margin-top: 4.5rem; }
.pager {
  display: flex; gap: .65rem; align-items: center; margin-top: 1.15rem;
  flex-wrap: wrap;
  padding: .7rem .85rem;
  background: rgba(17, 24, 38, .72);
  border: 1px solid var(--line);
  border-radius: 14px;
}
.pager-btn {
  display: inline-flex; align-items: center; gap: .28rem;
  padding: .42rem .8rem;
  border-radius: 10px;
  border: 1px solid rgba(255, 255, 255, .12);
  background: rgba(255, 255, 255, .04);
  color: var(--text);
  font-size: .88rem; font-weight: 600;
  text-decoration: none;
}
.pager-ico {
  display: inline-block;
  font-size: 1.05em;
  line-height: 1;
  opacity: .85;
  font-weight: 700;
}
.pager-btn:hover {
  border-color: rgba(148, 180, 255, .4);
  background: rgba(122, 162, 255, .12);
  color: #fff;
  text-decoration: none;
}
.pager-btn.is-disabled {
  opacity: .35; pointer-events: none; cursor: default;
}
.pager-next {
  border-color: rgba(148, 180, 255, .32);
  background: rgba(122, 162, 255, .12);
  color: #dce8ff;
}
.pager-jump {
  display: inline-flex; align-items: center; gap: .4rem;
  margin: 0;
}
.pager-jump-k {
  font-size: .78rem; color: var(--muted); font-weight: 650;
  letter-spacing: .04em; text-transform: uppercase;
}
.pager-select {
  appearance: none;
  -webkit-appearance: none;
  -moz-appearance: none;
  font: inherit;
  font-size: .88rem;
  font-weight: 650;
  color: var(--text);
  /* Clean SVG chevron — dual-gradient fake triangle glitched in WebView2. */
  background-color: rgba(8, 12, 20, .72);
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12' fill='none'%3E%3Cpath d='M2.5 4.25L6 7.75L9.5 4.25' stroke='%2399a8c7' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 10px center;
  background-size: 12px 12px;
  border: 1px solid rgba(148, 180, 255, .35);
  border-radius: 10px;
  padding: .45rem 2rem .45rem .75rem;
  min-width: 9.5rem;
  cursor: pointer;
  box-shadow: 0 0 0 1px rgba(122, 162, 255, .06) inset;
}
.pager-select:hover, .pager-select:focus {
  outline: none;
  border-color: rgba(180, 205, 255, .55);
  background-color: rgba(122, 162, 255, .12);
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12' fill='none'%3E%3Cpath d='M2.5 4.25L6 7.75L9.5 4.25' stroke='%23cfe0ff' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 10px center;
  background-size: 12px 12px;
}
.pager-select.is-loading {
  pointer-events: none;
  opacity: .72;
  border-color: rgba(122, 162, 255, .55);
  background-image:
    url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 14 14' fill='none'%3E%3Ccircle cx='7' cy='7' r='5' stroke='%237aa2ff' stroke-width='1.6' stroke-opacity='.25'/%3E%3Cpath d='M12 7a5 5 0 0 0-5-5' stroke='%23cfe0ff' stroke-width='1.6' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 9px center;
  background-size: 14px 14px;
  animation: pager-select-pulse 1s ease-in-out infinite;
}
@keyframes pager-select-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(122, 162, 255, .18); }
  50% { box-shadow: 0 0 0 4px rgba(122, 162, 255, .08); }
}
.pager-select option { background: #111826; color: var(--text); }
.pager-top {
  margin-left: auto;
  font-size: .84rem; font-weight: 600;
  color: var(--muted);
  padding: .35rem .55rem;
  border-radius: 8px;
}
.pager-top:hover {
  color: var(--text);
  background: rgba(255, 255, 255, .05);
  text-decoration: none;
}

@media (max-width: 720px) {
  .wrap { padding: 1.1rem .9rem 2.2rem; }
  .hero { padding: 1.5rem 1.1rem 1.2rem; }
  .hero h1 { font-size: 1.6rem; }
  .hero-compact { padding: .85rem 1rem .9rem; }
  .hero-compact h1 { font-size: 1.18rem; }
  .hero-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .detail-hero {
    grid-template-columns: 1fr;
    padding: 1.1rem 1rem 1.25rem;
    gap: 1.1rem; border-radius: 20px;
  }
  .detail-poster {
    max-width: 100%; margin: 0; aspect-ratio: 16 / 10; width: 100%;
  }
  .detail-hero-bg { opacity: .62; filter: saturate(1.4) blur(22px); }
  .detail-title { font-size: 1.35rem; text-align: center; }
  .detail-kicker { margin-inline: auto; }
  .detail-pills, .detail-tags { justify-content: center; text-align: center; }
  .detail-hero-main { padding: 0; align-items: center; text-align: center; }
  .detail-hero-main .tag-add { justify-content: center; width: 100%; max-width: none; }
  .detail-tags { width: 100%; }
  .tag-add { justify-content: center; width: 100%; }
  .action-row, .detail-hero-actions { justify-content: center; }
  .ref-card { flex-direction: column; align-items: stretch; }
  .ref-card-actions .action-btn { flex: 1; justify-content: center; }
  .preview-grid { grid-template-columns: 1fr; }
  .media-card-foot { gap: .35rem .45rem; }
  .top { padding: .8rem 1rem; }
  .top nav a { margin-left: .5rem; font-size: .85rem; padding: .35rem .6rem; }
  .cover-grid { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: .7rem; }
  .rec-card { grid-template-columns: 1fr; }
  .rec-card-actions { justify-content: flex-start; }
}
"""

# --------------------------------------------------------------------------- cinema design layer
#
# This is intentionally appended rather than replacing the older utility CSS:
# detail/media pages still use the established class names, while the home and
# list pages get one stable, responsive visual system.  The layer contains no
# external assets or font requests, so it remains safe for the offline desktop
# bundle and can later be moved verbatim to a versioned static file.
_CINEMA_CSS = """
:root {
  --cinema-bg: #07090d;
  --cinema-surface: #101318;
  --cinema-raised: #171b21;
  --cinema-text: #f3f5f7;
  --cinema-muted: #a9b1bc;
  --cinema-line: rgba(255,255,255,.13);
  --cinema-blue: #88a8ff;
  --cinema-green: #57d49a;
  --cinema-amber: #f2bd67;
}

html { background: var(--cinema-bg); scroll-behavior: smooth; }
body {
  background: var(--cinema-bg);
  color: var(--cinema-text);
  letter-spacing: 0;
}
.wrap { max-width: 1600px; padding: 1.5rem 2rem 4rem; }
.top {
  min-height: 64px;
  padding: .65rem 2rem;
  background: rgba(7,9,13,.78);
  border-bottom-color: rgba(255,255,255,.1);
  backdrop-filter: blur(12px) saturate(1.15);
}
.brand { letter-spacing: 0; }
.top nav a, .page-title, .detail-title, .section-title, .cover-title { letter-spacing: 0; }
:where(a, button, input, select, [tabindex]):focus-visible {
  outline: 2px solid #a8c0ff;
  outline-offset: 3px;
}

/* A thin, non-blocking progress line replaces the old full-screen veil. */
.nav-loading { inset: 0 0 auto 0; height: 3px; align-items: flex-start;
  background: transparent; pointer-events: none; backdrop-filter: none; }
.nav-loading.is-on { display: block; opacity: 1; visibility: visible;
  pointer-events: none; backdrop-filter: none;
  animation: cinema-progress-reveal 0s linear .18s both; }
.nav-loading-panel { display: none; }
.nav-loading-bar { height: 3px; box-shadow: 0 0 14px rgba(136,168,255,.55); }
@keyframes cinema-progress-reveal { from { opacity: 0; } to { opacity: 1; } }

/* Sections are bands; only repeated items are framed cards. */
.section-block {
  margin: 1.65rem 0 0;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
  backdrop-filter: none;
}
.section-head { margin: 0 0 .8rem; min-height: 32px; }
.section-title { font-size: 1.15rem; font-weight: 760; }

/* Cinema hero ------------------------------------------------------------- */
.hero-compact { display: none; }
.cinema-hero {
  position: relative;
  isolation: isolate;
  min-height: clamp(340px, 56svh, 520px);
  overflow: hidden;
  border-radius: 8px;
  background: #11151b;
  border: 1px solid rgba(255,255,255,.14);
  box-shadow: 0 24px 70px rgba(0,0,0,.42);
}
.cinema-hero-media, .cinema-hero-img, .cinema-hero-fallback,
.cinema-hero-scrim { position: absolute; inset: 0; width: 100%; height: 100%; }
.cinema-hero-img { object-fit: cover; object-position: center; transition: transform .6s ease;
  animation: cinema-kenburns 18s ease-in-out alternate infinite; }
.cinema-hero-fallback { display: flex; align-items: center; justify-content: center;
  color: rgba(255,255,255,.46); font-size: clamp(3rem, 7vw, 7rem); font-weight: 800; }
.cinema-hero-fallback[hidden], .cover-thumb-fallback[hidden] { display: none; }
.cinema-hero-scrim {
  background: linear-gradient(90deg, rgba(5,7,10,.88) 0%, rgba(5,7,10,.7) 43%,
    rgba(5,7,10,.28) 78%, rgba(5,7,10,.18) 100%),
    linear-gradient(0deg, rgba(5,7,10,.76), transparent 62%);
}
.cinema-hero-copy { position: absolute; z-index: 2; inset: auto auto 2.4rem 2.5rem;
  width: min(620px, calc(100% - 5rem)); }
.cinema-hero-kicker { margin: 0 0 .65rem; color: #c4d3ff; font-size: .75rem;
  font-weight: 760; text-transform: uppercase; letter-spacing: .12em; }
.cinema-hero-title { margin: 0; max-width: 28ch; color: #fff; font-size: 40px;
  line-height: 1.14; font-weight: 800; overflow-wrap: anywhere; }
.cinema-hero-meta { margin: .65rem 0 0; color: #e0e5ec; font-size: 15px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cinema-hero-actions { display: flex; flex-wrap: wrap; gap: .65rem; margin-top: 1.1rem; }
.cinema-hero-actions > :where(a,button) { min-height: 44px; }
.cinema-hero-detail { display: inline-flex; align-items: center; justify-content: center;
  min-height: 44px; padding: .68rem 1rem; color: #f4f7ff; border: 1px solid rgba(255,255,255,.25);
  border-radius: 6px; background: rgba(255,255,255,.1); font-weight: 700; }
.cinema-hero-detail:hover { color: #fff; background: rgba(255,255,255,.18); }
@keyframes cinema-kenburns {
  from { transform: scale(1); }
  to { transform: scale(1.035); }
}

/* Rails and cinema cards -------------------------------------------------- */
.cinema-rail-shell { position: relative; min-width: 0; }
.cinema-rail { display: grid; grid-auto-flow: column; grid-auto-columns: 320px;
  grid-template-rows: 1fr; gap: 16px; overflow-x: auto; overflow-y: hidden;
  padding: 2px 2px .55rem; scroll-snap-type: x proximity; scrollbar-width: thin;
  scrollbar-color: rgba(255,255,255,.2) transparent; }
.cinema-rail > .cover-card { scroll-snap-align: start; }
.cinema-rail-arrow { position: absolute; z-index: 3; top: 50%; transform: translateY(-50%);
  width: 44px; height: 44px; padding: 0; border: 1px solid rgba(255,255,255,.24);
  border-radius: 50%; color: #fff; background: rgba(8,10,14,.78); font-size: 28px;
  line-height: 1; cursor: pointer; opacity: 0; pointer-events: none; transition: opacity .16s ease, background .16s ease; }
.cinema-rail-shell.has-overflow:hover .cinema-rail-arrow,
.cinema-rail-shell.has-overflow:focus-within .cinema-rail-arrow { opacity: 1; pointer-events: auto; }
.cinema-rail-arrow.is-prev { left: -12px; }
.cinema-rail-arrow.is-next { right: -12px; }
.cinema-rail-arrow:disabled { opacity: .28 !important; pointer-events: none; }
.cinema-live-skeleton { display: grid; grid-auto-flow: column; grid-auto-columns: 320px;
  gap: 16px; overflow: hidden; padding: 2px 2px .55rem; }
.cinema-live-skeleton .live-skeleton-card { display: block; aspect-ratio: 16 / 9;
  border-radius: 8px; background: linear-gradient(100deg, rgba(255,255,255,.06) 20%,
  rgba(255,255,255,.13) 38%, rgba(255,255,255,.06) 56%); background-size: 220% 100%;
  animation: cinema-skeleton 1.35s ease-in-out infinite; }
.cinema-explore-strip { overflow-x: auto; scrollbar-width: thin; padding-bottom: .25rem; }
.cinema-explore-strip .chips { flex-wrap: nowrap; width: max-content; margin: 0; }
@keyframes cinema-skeleton { from { background-position: 100% 0; } to { background-position: -100% 0; } }
.cover-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
.cover-card { position: relative; display: block; aspect-ratio: 16 / 9; min-width: 0;
  overflow: hidden; border: 1px solid rgba(255,255,255,.13); border-radius: 8px;
  color: inherit; background: var(--cinema-raised); box-shadow: none; transform: none; }
.cover-card:hover { color: inherit; border-color: rgba(136,168,255,.62); transform: translateY(-2px);
  box-shadow: 0 10px 28px rgba(0,0,0,.32); }
.cover-card.is-rail-intro { animation: cinema-card-in .22s ease-out backwards;
  animation-delay: calc(var(--rail-index, 0) * 35ms); }
@keyframes cinema-card-in { from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); } }
.cover-thumb { position: absolute; inset: 0; width: 100%; height: 100%; padding: 0;
  display: block; background: #12161c; }
.cover-thumb-img, .cover-thumb-fallback { position: absolute; inset: 0; width: 100%; height: 100%;
  object-fit: cover; display: block; }
.cover-thumb-fallback { display: none; align-items: center; justify-content: center;
  color: rgba(255,255,255,.42); font-size: 3rem; font-weight: 800; }
.cover-thumb.has-cover-fallback .cover-thumb-fallback { display: flex; }
.cover-thumb-fallback[hidden] { display: none; }
.cover-scrim { position: absolute; inset: 0; z-index: 1; pointer-events: none;
  background: linear-gradient(180deg, rgba(5,7,10,.08) 34%, rgba(5,7,10,.86) 100%); }
.cover-info { position: absolute; z-index: 2; left: 0; right: 0; bottom: 0; display: flex;
  flex-direction: column; gap: .22rem; padding: 2.2rem .8rem .72rem; pointer-events: none; }
.cover-title { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
  overflow: hidden; color: #fff; font-size: 15px; line-height: 1.28; font-weight: 720;
  overflow-wrap: anywhere; }
.cover-meta { display: block; color: #c3cbd5; font-size: 12px; line-height: 1.2;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cover-badge { position: relative; z-index: 3; margin: .6rem 0 0 .6rem; padding: .2rem .45rem;
  border-radius: 4px; color: #e9edf2; background: rgba(7,9,13,.76); border-color: rgba(255,255,255,.18);
  font-size: 11px; }
.cover-initial { position: relative; z-index: 0; }

/* Static plates avoid a data-dependent inline gradient. */
.plate-0 { background: linear-gradient(135deg,#182329,#31515c); }
.plate-1 { background: linear-gradient(135deg,#1a1d2d,#334b72); }
.plate-2 { background: linear-gradient(135deg,#27202d,#5a3d57); }
.plate-3 { background: linear-gradient(135deg,#17232c,#3e5363); }
.plate-4 { background: linear-gradient(135deg,#22202c,#4d405d); }
.plate-5 { background: linear-gradient(135deg,#19252e,#396171); }
.plate-6 { background: linear-gradient(135deg,#202a25,#3c6650); }
.plate-7 { background: linear-gradient(135deg,#2b2520,#66513b); }

/* Detail / player surfaces use the same neutral cinema language. */
.detail-hero { border-radius: 8px; border-color: rgba(255,255,255,.14); box-shadow: 0 24px 70px rgba(0,0,0,.38); }
.detail-hero-bg { filter: none; opacity: .34; }
.detail-hero-bg-fade { background: linear-gradient(90deg,rgba(7,9,13,.42),rgba(7,9,13,.84)); }
.detail-poster { border-radius: 8px; }
.detail-poster-img { display: block; width: 100%; height: 100%; object-fit: contain;
  background-color: #050607; background-size: cover; background-position: center; }
.detail-poster-fallback { position: absolute; inset: 0; align-items: center; justify-content: center;
  color: rgba(255,255,255,.45); font-size: 4rem; font-weight: 800; }
.detail-poster.has-cover-fallback .detail-poster-fallback { display: flex; }
.detail-poster-fallback[hidden] { display: none; }
.watch-shell, .media-card, .ref-card, .rec-group, .rec-card { border-radius: 8px; }
.detail-source-controls { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; }
.detail-source-label { color: var(--muted); font-size: .82rem; font-weight: 650; }
.detail-source-picker { min-height: 44px; min-width: min(320px, 100%); max-width: 100%;
  padding: .55rem .7rem; color: var(--text); background: var(--panel-2);
  border: 1px solid var(--line-strong); border-radius: 6px; font: inherit; }
.detail-source-controls .source-open { min-height: 44px; }

/* Mobile: top brand bar + fixed four-tab bottom navigation. */
@media (max-width: 767px) {
  body { padding-bottom: calc(64px + env(safe-area-inset-bottom)); }
  .wrap { padding: 1rem 1rem calc(2rem + env(safe-area-inset-bottom)); }
  .top { min-height: 56px; height: 56px; padding: .55rem 1rem; }
  #top-nav { position: fixed; z-index: 50; left: 0; right: 0; bottom: 0; height: calc(56px + env(safe-area-inset-bottom));
    display: flex; align-items: flex-start; padding: 4px 4px env(safe-area-inset-bottom);
    background: rgba(11,14,19,.96); border-top: 1px solid rgba(255,255,255,.14); }
  .top nav a, .top nav a:first-child { flex: 1 1 25%; min-width: 44px; min-height: 48px; margin: 0;
    display: flex; align-items: center; justify-content: center; padding: .35rem .2rem;
    border: 0; border-radius: 6px; font-size: 12px; }
  .top nav a.is-active { background: rgba(136,168,255,.16); box-shadow: inset 0 -2px #9ab5ff; }
  .cinema-hero { min-height: min(58svh,460px); border-radius: 8px; }
  .cinema-hero-copy { left: 1.1rem; right: 1.1rem; bottom: 1.2rem; width: auto; }
  .cinema-hero-title { font-size: 28px; max-width: 24ch; }
  .cinema-hero-meta { font-size: 14px; }
  .cinema-hero-actions { gap: .5rem; }
  .cinema-hero-actions > :where(a,button) { flex: 1 1 44%; }
  .cinema-rail { grid-auto-columns: 82vw; gap: 12px; scrollbar-width: none; }
  .cinema-rail::-webkit-scrollbar { display: none; }
  .cinema-rail-arrow { display: none; }
  .cinema-live-skeleton { grid-auto-columns: 82vw; gap: 12px; }
  .cinema-explore-strip { margin-inline: -1rem; padding-inline: 1rem; }
  .cover-grid { grid-template-columns: 1fr; gap: 12px; }
  .section-title { font-size: 18px; }
  .detail-hero { grid-template-columns: 1fr; padding: 1rem; gap: 1rem; }
  .detail-title { font-size: 26px; }
  .detail-hero-actions .action-btn { flex: 1 1 100%; justify-content: center; min-height: 44px; }
  .detail-source-controls { align-items: stretch; flex-direction: column; }
  .detail-source-picker, .detail-source-controls .source-open { width: 100%; }
}
@media (min-width: 768px) and (max-width: 1023px) {
  .wrap { padding-inline: 1.25rem; }
  .cinema-rail { grid-auto-columns: 240px; }
  .cinema-live-skeleton { grid-auto-columns: 240px; }
  .cover-grid { grid-template-columns: repeat(2, minmax(0,1fr)); }
  .cinema-hero-title { font-size: 36px; }
}
@media (min-width: 1024px) and (max-width: 1439px) {
  .wrap { padding-inline: 1.5rem; }
  .cinema-rail { grid-auto-columns: 280px; }
  .cinema-live-skeleton { grid-auto-columns: 280px; }
  .cover-grid { grid-template-columns: repeat(3, minmax(0,1fr)); }
  .cinema-hero { min-height: 460px; }
}
@media (min-width: 1440px) {
  .cinema-hero { min-height: 520px; }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after { animation-duration: .001ms !important; animation-iteration-count: 1 !important;
    transition-duration: .001ms !important; }
  .cinema-hero-img { transform: none !important; }
  .cinema-rail { scroll-behavior: auto; }
  .cinema-live-skeleton { animation: none; }
  .cinema-live-skeleton .live-skeleton-card { animation: none; }
}
"""
_CSS += _CINEMA_CSS
