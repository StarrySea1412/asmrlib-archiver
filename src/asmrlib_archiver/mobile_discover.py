"""Bundled 发现页 for the APK: our own skin over live site listings.

Python is the source of truth for the page, same pattern as
``mobile_blocklist``: ``python -m asmrlib_archiver.mobile_discover``
regenerates ``android/app/src/main/assets/discover.html``, and a contract test
fails when the asset drifts.  The visual system lives in ``mobile_skin`` — this
module owns only the discover page's own layout on top of it.

The page renders with no PC on the network: data arrives at runtime from
``DiscoverBridge`` (a Jsoup port of ``parser.py``'s card rules) over an
ASYNC contract — ``feed(url, id)`` / ``resolvePost(url, id)`` return a
request id immediately and the result is pushed back into
``window.__asmrlibDeliver(id, payloadJson)``. A synchronous bridge once
froze the page's JS for the whole network round-trip (20s over a slow
VPN read as "app hangs"). The bridge also memoizes successful results
per URL (feed 10 min, resolve 30 min): back navigation fully reloads
the asset page, and without that memo every return trip paid another
full round-trip. The page itself never fetches — CSP is
``connect-src 'none'``.

The page is self-contained by design.  It used to load ``viewer.assets._CSS`` —
2 456 lines written for the desktop viewer — and then spend its own CSS undoing
it, which is why the result read as crude.  See ``mobile_skin`` for that story.

Visual QA: the grid's only data source is a live site, so on a network that
blocks it there is no way to exercise the cards. The page therefore exposes
``window.__asmrlibDiscoverPreview(payload)`` to render a synthetic payload —
the same escape hatch the play page's ``__asmrlibNetBannerPreview`` offers.

There is deliberately NO search box: the site ignores ``?kw=`` and its own
``/search/<kw>`` page renders an empty result list for every query (and no
search API exists — all verified 2026-09-12), so any search control would
silently show the homepage listing. Tag chips are the discovery mechanism.
"""

from __future__ import annotations

from pathlib import Path

from .mobile_skin import SKIN, THEME_COLOR_META

MARKER = "<!-- asmrlib-discover v1 -->"
PLAY_MARKER = "<!-- asmrlib-play v1 -->"
ASSET_RELPATH = Path("android") / "app" / "src" / "main" / "assets" / "discover.html"
PLAY_ASSET_RELPATH = Path("android") / "app" / "src" / "main" / "assets" / "play.html"

_EXTRA_CSS = SKIN + """
/* =====================================================================
   discover page
   ===================================================================== */

/* The page used to inherit the desktop dock's 64px bottom reserve and then
   neutralise it, which is where the dead black band under the feed came from.
   There is no dock here, so the reserve simply never exists. */
.discover-page { padding-bottom: env(safe-area-inset-bottom); }

/* ---- pull-to-refresh ---------------------------------------------------
   A DRAWN arc, not a text glyph in a disc: a faint full track plus an accent
   arc whose stroke-dashoffset is walked from its whole length to zero as the
   pull fills. The pull is therefore its own progress read-out, and rotation is
   reserved for "the refresh is actually running" - the old version rotated a
   glyph at a rate tied to the drag distance and then handed the same property
   to a keyframe, so the inline transform and the animation fought on release. */
.ds-ptr { position: absolute; left: 0; right: 0; top: -46px; height: 40px;
  display: flex; align-items: center; justify-content: center;
  pointer-events: none; opacity: 0; }
.ds-ptr-ring { width: 26px; height: 26px; overflow: visible;
  transition: filter .18s ease; }
.ds-ptr-track { stroke: var(--sep-strong); transition: stroke .18s ease; }
.ds-ptr-arc { stroke: var(--accent); }
/* Past the threshold the whole ring takes the accent: the arc has closed and
   the track lights up under it, so "releasing now refreshes" is legible before
   the finger lifts. The cue is a stroke and not a scale on purpose - the busy
   spin animates .ds-ptr-ring's transform, and a transform here would be re-read
   as that animation's implicit start value and pulse the ring every iteration. */
.ds-ptr.armed .ds-ptr-track { stroke: var(--accent-line); }
.ds-ptr.armed .ds-ptr-ring { filter: drop-shadow(0 0 8px var(--accent-soft)); }
/* Rotation lives on the SVG root, never on the arc: the arc already carries
   rotate(-90) as its own attribute to start the fill at 12 o'clock. */
.ds-ptr.busy .ds-ptr-ring { animation: ds-ptr-spin .8s linear infinite; }
@keyframes ds-ptr-spin { to { transform: rotate(360deg); } }
/* Settle, gated on the drag class. With the finger down both the content and
   the indicator must track the touch with no lag; the transition only exists
   once the finger is up, which is what turns the snap-back below the threshold
   - and the exit after a completed refresh - into a glide. */
main.wrap:not(.is-dragging) { transition: transform .26s cubic-bezier(.22,.61,.36,1); }
main.wrap:not(.is-dragging) .ds-ptr { transition: opacity .24s ease, transform .24s ease; }
@media (prefers-reduced-motion: reduce) {
  main.wrap:not(.is-dragging), main.wrap:not(.is-dragging) .ds-ptr { transition: none; }
  /* the spin is decoration and goes; the arc fill is information and stays
     (it is a stroke-dashoffset, not an animation) */
  .ds-ptr.busy .ds-ptr-ring { animation: none; }
}

/* ---- transient status line ------------------------------------------- */
.ds-src { text-align: center; margin: var(--sp-4) 0 0;
  font-size: var(--fs-caption); color: var(--text-3); }
.ds-src[hidden] { display: none; }

/* ---- first-paint splash ------------------------------------------------
   A 96px frame with a sweeping accent ring around a 56px brand mark. The
   fade-out (.42s) only fires once - pull-to-refresh / pagination / chain
   refills must never re-show it, so the JS toggles `is-gone`, never re-adds
   the class. */
.ds-splash { position: fixed; inset: 0; z-index: 800;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: var(--sp-4); padding: var(--sp-6); pointer-events: none;
  background: var(--bg);
  opacity: 1; visibility: visible;
  transition: opacity .42s ease, visibility 0s linear 0s; }
.ds-splash.is-gone { opacity: 0; visibility: hidden;
  transition: opacity .42s ease, visibility 0s linear .42s; }
.ds-splash-mark { position: relative; width: 96px; height: 96px;
  display: inline-flex; align-items: center; justify-content: center; }
.ds-splash-mark > svg { position: absolute; inset: 0; width: 100%; height: 100%; }
.ds-splash-track { stroke: var(--sep-strong); }
/* Flat accent, not a gradient.  The old arc was stroked with an SVG
   linearGradient whose two stops were hardcoded - so it kept the dark theme's
   periwinkle in light mode while every other accent on the page had already
   deepened to #4a63e8.  A spinner is not the place to spend a gradient
   anyway: it is the one element that is always moving. */
.ds-splash-arc { stroke: var(--accent); }
.ds-splash-ring { animation: ds-splash-spin 1.6s linear infinite; transform-origin: 50% 50%; }
@keyframes ds-splash-spin { to { transform: rotate(360deg); } }
.ds-splash-glyph { position: relative; width: 56px; height: 56px;
  border-radius: 17px;
  display: inline-flex; align-items: center; justify-content: center;
  color: var(--text-on-accent);
  background: linear-gradient(150deg, var(--accent), var(--accent-strong));
  box-shadow: inset 0 -2px 0 rgba(0,0,0,.18); }
.ds-splash-label { margin: 0; font-size: var(--fs-body); font-weight: 600;
  letter-spacing: .02em; color: var(--text); }
.ds-splash-sub { margin: 0; font-size: var(--fs-footnote); color: var(--text-2); }
@media (prefers-reduced-motion: reduce) {
  .ds-splash-ring { animation: none; }
  .ds-splash { transition: opacity .15s linear, visibility 0s linear .15s; }
  .ds-splash.is-gone { transition: opacity .15s linear, visibility 0s linear .15s; }
}

/* ---- discover page: tablet ---------------------------------------------
   The auto-fit grid contract above is width-agnostic, which is exactly why the
   phone grid does not become a tablet grid on its own: at 1280px it simply
   resolves five columns at the phone's 208px ceiling and the captions stay at
   phone sizes, edge to edge. A tablet needs a cap AND a bigger card.
   Split by ORIENTATION, not width, because 1024x1366 portrait and 1280x800
   landscape are different problems - the first has room to grow downward (a
   centred reading column), the second is wide but SHORT, so the win there is a
   wider grid and shorter cards, not a taller one.
   Both ranges start at 768px, so every rule below is out of reach of the phone
   breakpoints. */
@media (min-width: 768px) and (orientation: portrait) {
  /* 240px floor: 2 comfortable columns at 800px, 3 at 1024px portrait. */
  :root { --card-min: 240px; }
  .discover-page .wrap { max-width: 920px; margin-inline: auto;
    padding-inline: var(--sp-6); padding-top: var(--sp-5);
    padding-bottom: calc(var(--sp-6) + env(safe-area-inset-bottom)); }
  .cover-grid { columns: 3; column-gap: var(--sp-4); }
  .cover-card { margin-bottom: var(--sp-5); }
  #ds-grid > .live-skeleton-grid { gap: var(--sp-5); }
  /* the rail bleeds to the wrap's own padding, which is not the phone's 16px */
  .chips { margin: 0 calc(var(--sp-6) * -1) var(--sp-4);
    padding: var(--sp-1) var(--sp-6) var(--sp-2); }
  .cover-info { padding: var(--sp-3) var(--sp-1) 0; gap: 5px; }
  .cover-title { font-size: var(--fs-headline); }
  .cover-flag { font-size: var(--fs-footnote); }
  .cover-tags { font-size: var(--fs-footnote); }
  .cover-initial { width: 48px; height: 48px; font-size: 20px; }
  .page-head h1 { font-size: 34px; }
  .ds-sec-title { font-size: var(--fs-subhead); }
}
@media (min-width: 768px) and (orientation: landscape) {
  /* 252px floor over a 1180px cap: the reference wall keeps square covers on
     every width, so the short viewport buys back a ROW, not a flatter card. */
  :root { --card-min: 252px; }
  .discover-page .wrap { max-width: 1180px; margin-inline: auto;
    padding-inline: var(--sp-6); padding-top: var(--sp-3);
    padding-bottom: calc(var(--sp-6) + env(safe-area-inset-bottom)); }
  .cover-grid { columns: 4; column-gap: var(--sp-4); }
  .cover-card { margin-bottom: var(--sp-4); }
  #ds-grid > .live-skeleton-grid { gap: var(--sp-4); }
  .chips { margin: 0 calc(var(--sp-6) * -1) var(--sp-3);
    padding: var(--sp-1) var(--sp-6) var(--sp-2); }
  .cover-info { padding: var(--sp-3) var(--sp-1) 0; gap: 5px; }
  .cover-title { font-size: var(--fs-headline); }
  .cover-flag { font-size: var(--fs-footnote); }
  .cover-tags { font-size: var(--fs-footnote); }
  .cover-initial { width: 48px; height: 48px; font-size: 20px; }
  .page-head { padding-bottom: var(--sp-3); }
  .page-head h1 { font-size: 32px; }
  .ds-sec-title { font-size: var(--fs-subhead); }
}
"""
# Runs inside the bundled asset page. window.__asmrlibDiscover is injected
# by the APK shell only while this page is the loaded document; a plain
# browser never has it, hence the explicit error panel.
_DISCOVER_JS = r"""
(function () {
  'use strict';
  var SITE = JSON.parse(document.getElementById('ds-config').textContent).site;
  // pages: feed URLs loaded into the grid, base page first — the return
  // restoration and pull-to-refresh both replay this chain. chain: an
  // in-flight multi-page sequence ({left, y, after}); mid-chain requests
  // are marked and dropped if a user action supersedes the chain.
  var state = { url: SITE + '/', pages: [], chain: null, refreshing: false,
                switching: false };
  var grid = document.getElementById('ds-grid');
  var statusEl = document.getElementById('ds-status');
  var tagsEl = document.getElementById('ds-tags');
  var errEl = document.getElementById('ds-error');
  var splashEl = document.getElementById('ds-splash');
  var prevBtn = document.getElementById('ds-prev');
  var nextBtn = document.getElementById('ds-next');
  var secEl = document.getElementById('ds-sec');
  var secTitleEl = document.getElementById('ds-sec-title');
  var countEl = document.getElementById('ds-count');
  var pagerEl = document.getElementById('ds-pager');
  var srcEl = document.getElementById('ds-src');
  var mainEl = document.querySelector('main.wrap') || document.querySelector('main');
  var ptrEl = document.getElementById('ds-ptr');
  var ptrArcEl = document.getElementById('ds-ptr-arc');
  var pending = {};   // requestId -> {url, append}; abandoned ids are ignored
  var seq = 0;
  var retryUrl = SITE + '/';
  // The second source (Eara's): asmr.one's open JSON API, fetched natively by
  // the bridge behind a virtual search URL the page composes itself.
  var ASMRONE_SEARCH_PREFIX = 'asmrone://search/';
  var tabSite = document.getElementById('ds-tab-site');
  var tabOne = document.getElementById('ds-tab-asmrone');
  var searchForm = document.getElementById('ds-search');
  var searchInput = document.getElementById('ds-search-input');
  var linesEl = document.getElementById('ds-lines');
  // The mirror family the reference player ships (one = asmr.one itself,
  // 100/200/300 = its mirrors): identical API, four hosts. The picked line
  // rides the virtual URL's tail segment and persists per device — when a
  // domain is unreachable from this network, switching lines is the fix.
  var ASMRONE_SITES = ['one', '100', '200', '300'];
  function oneSite() {
    var s = '';
    try { s = localStorage.getItem('ds-asmrone-site') || ''; } catch (e) {}
    return ASMRONE_SITES.indexOf(s) >= 0 ? s : 'one';
  }
  function oneUrl(kw, page, site) {
    return ASMRONE_SEARCH_PREFIX + encodeURIComponent(kw) + '/' + page + '/' + site;
  }
  function setOneSite(site) {
    try { localStorage.setItem('ds-asmrone-site', site); } catch (e) {}
    syncOneSite();
    // Re-request the view already on screen (keyword + page kept); on a
    // fresh tab, the default search on the new line.
    var one = /asmrone:\/\/search\/([^/]+)\/(\d+)(?:\/(one|100|200|300))?/
        .exec(state.url && state.url.indexOf(ASMRONE_SEARCH_PREFIX) === 0
              ? state.url : lastOneUrl);
    var kw = one ? decodeURIComponent(one[1]) : 'ASMR';
    var page = one ? parseInt(one[2], 10) : 1;
    lastOneUrl = oneUrl(kw, page, site);
    load(lastOneUrl);
  }
  function syncOneSite() {
    var cur = oneSite();
    if (!linesEl) return;
    var chips = linesEl.querySelectorAll('.ds-line');
    for (var i = 0; i < chips.length; i++) {
      chips[i].classList.toggle('is-active',
        chips[i].getAttribute('data-site') === cur);
    }
  }
  var lastOneUrl = oneUrl('ASMR', 1, oneSite());
  function isOneUrl(url) { return String(url || '').indexOf(ASMRONE_SEARCH_PREFIX) === 0; }
  // A switch keeps the feed on screen while the other source resolves, so the
  // whole content block dims together — the wall of cards, the tag rail and
  // the section header all change with the same answer. Written as whole
  // className strings rather than classList: these three classes are static in
  // the markup, and the harness that drives render() stubs classList with
  // contains/add only.
  function setSwitchDim(on) {
    if (grid) grid.className = on ? 'cover-grid is-switching' : 'cover-grid';
    if (tagsEl) tagsEl.className = on ? 'chips is-switching' : 'chips';
    if (secEl) secEl.className = on ? 'ds-sec is-switching' : 'ds-sec';
  }
  // Reduce Motion means exactly that: the top, without the travel to it.
  function glideToTop() {
    var reduce = window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    try { window.scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' }); }
    catch (e) { window.scrollTo(0, 0); }
  }
  function setSource(src) {
    var one = src === 'one';
    tabSite.classList.toggle('is-active', !one);
    tabOne.classList.toggle('is-active', one);
    // The pills said which source was on; the roles never did — the tabs have
    // read "本站 selected" to a screen reader since the second source landed.
    tabSite.setAttribute('aria-selected', String(!one));
    tabOne.setAttribute('aria-selected', String(one));
    // The chrome that belongs to one source collapses rather than vanishing,
    // and the feed stays where it is, dimmed, instead of blinking to a
    // skeleton and teleporting to the top.
    searchForm.classList.toggle('is-off', !one);
    if (linesEl) linesEl.classList.toggle('is-off', !one);
    state.switching = true;
    setSwitchDim(true);
    glideToTop();
    if (one) { syncOneSite(); load(lastOneUrl); }
    else load(SITE + '/');
  }
  if (linesEl) {
    linesEl.addEventListener('click', function (ev) {
      var chip = ev.target.closest ? ev.target.closest('[data-site]') : null;
      if (!chip) return;
      var site = chip.getAttribute('data-site');
      if (ASMRONE_SITES.indexOf(site) < 0 || site === oneSite()) return;
      setOneSite(site);
    });
  }
  tabSite.addEventListener('click', function () { if (!isOneUrl(state.url)) return; setSource('site'); });
  tabOne.addEventListener('click', function () { if (isOneUrl(state.url)) return; setSource('one'); });
  searchForm.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var kw = (searchInput.value || '').trim() || 'ASMR';
    lastOneUrl = oneUrl(kw, 1, oneSite());
    load(lastOneUrl);
  });
  // Transient mobile failures (DNS flap / VPN wake-up / radio handover) should
  // retry automatically before making the user tap an error panel. Manual
  // retry remains available after the bounded attempts are exhausted.
  var AUTO_RETRY_DELAYS = [900, 1800, 3600];
  var autoRetryTimer = 0;
  var autoRetryCount = 0;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function plateIdx(title) {
    var t = title || '?', n = 0;
    for (var i = 0; i < t.length; i++) n += t.charCodeAt(i);
    return n % 8;
  }
  // Every site title starts with "#Tag", so a naive first-character initial
  // printed "#" on every cover-less card. Skip the leading hashes/separators
  // and fall back to the raw title only if that empties the string.
  function initial(title) {
    var raw = String(title == null ? '' : title);
    var t = raw.replace(/^[#\s\u00b7\u30fb\-_\u2014]+/, '');
    return (t || raw || '?').slice(0, 1).toUpperCase();
  }
  function skeleton() {
    var cells = '';
    // Four rows fill a phone's first viewport and make the loading state look
    // intentional. Six cards left half the screen empty on 360px devices.
    for (var i = 0; i < 8; i++) {
      cells += '<span class="live-skeleton-card" aria-hidden="true">'
        + '<i></i><i></i></span>';
    }
    grid.innerHTML = '<div class="live-skeleton-grid" aria-label="正在加载内容">' + cells + '</div>';
  }

  function nextId() {
    seq += 1;
    return 'q' + seq + '-' + Math.floor(Math.random() * 1e6);
  }

  function clearAutoRetry() {
    if (autoRetryTimer) clearTimeout(autoRetryTimer);
    autoRetryTimer = 0;
  }

  // Schedules the next attempt and nothing else. It deliberately touches no
  // pixels: what the user sees during a retry depends on whether there is
  // content to protect, and that decision belongs to showError (below). This
  // function used to draw the skeleton and the "网络波动" line itself, which
  // meant a cold start with no network sat on a skeleton for four 20s bridge
  // timeouts - about 86 seconds - before it would say anything at all.
  function scheduleAutoRetry(msg, fromUrl, keepGrid, append) {
    var url = fromUrl || state.url;
    if (!url || autoRetryCount >= AUTO_RETRY_DELAYS.length || state.refreshing) return false;
    var delay = AUTO_RETRY_DELAYS[autoRetryCount++];
    clearAutoRetry();
    autoRetryTimer = setTimeout(function () {
      autoRetryTimer = 0;
      request(url, !!append, false, true, keepGrid);
    }, delay);
    return true;
  }

  function showError(msg, fromUrl, keepGrid, retryable, append) {
    retryUrl = fromUrl || state.url;
    // The switch is over the moment its outcome is known, however it went:
    // cleared here, above the two early returns, so no failure path can leave
    // the next listing dimmed under a class it never took off.
    state.switching = false;
    setSwitchDim(false);
    // Retrying and *telling the user* are two different jobs, and conflating
    // them was the bug: "the budget has not run out yet" was treated as "do
    // not show the error", so a phone with no network at all stared at a
    // spinner for the whole budget before admitting anything. The budget
    // exists to avoid flashing an error over content the user already has -
    // with an empty screen there is nothing to protect.
    var keep = !!keepGrid && !!grid.querySelector('.cover-card');
    var retrying = retryable !== false
      && scheduleAutoRetry(msg, retryUrl, keepGrid, append);
    if (!retrying) clearAutoRetry();
    // Cards on screen: the retry is the whole story. Keep them, say what is
    // happening on the status line, and never cover content the user has.
    if (retrying && keep) {
      statusEl.className = 'ds-status is-loading';
      statusEl.textContent = '网络波动，正在自动重试…';
      return;
    }
    // The panel is already up and a retry is in flight behind it: leave it
    // alone. Re-rendering it every attempt restarted its entrance animation
    // and strobed, because the bridge reports a slightly different exception
    // text each time (the resolved address changes).
    if (retrying && !errEl.hidden) return;
    // An error aborts whatever sequence was running: the multi-page chain
    // dies here, and a pull-to-refresh spinner must never spin forever.
    state.chain = null;
    if (state.refreshing) endRefresh();
    // The splash owns the first paint and must give the error panel room to
    // breathe: lift the layer before the panel fades in, otherwise the user
    // would see "连不上站点" floating over the brand ring for a beat.
    if (splashEl && !splashEl.classList.contains('is-gone')) {
      splashEl.classList.add('is-gone');
    }
    // An incremental failure (下一页 / pull-to-refresh) keeps the cards the
    // user already has - wiping twelve loaded cards because page two timed
    // out is the one thing an infinite-scroll surface must never do. Only a
    // failed *fresh* navigation (a tag chip, 重试 from a cold page) clears the
    // grid, and then the error owns the screen: the pager would otherwise sit
    // above it as two dead buttons with a data-source note between them.
    if (!keep) {
      grid.innerHTML = '';
      tagsEl.innerHTML = '';
      prevBtn.disabled = true;
      nextBtn.disabled = true;
      secEl.hidden = true;
      pagerEl.hidden = true;
      srcEl.hidden = true;
      countEl.textContent = '';
    }
    statusEl.className = 'ds-status';
    statusEl.textContent = '';
    errEl.hidden = false;
    errEl.innerHTML =
      '<div class="err-hero">'
      + '<div class="err-glyph" aria-hidden="true">'
      + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
      + 'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
      + '<path d="M5 12.6a10 10 0 0 1 14 0"/>'
      + '<path d="M8.3 15.7a5.5 5.5 0 0 1 7.4 0"/>'
      + '<path d="M12 19.3h.01"/>'
      + '<path d="M2.2 9.2a15 15 0 0 1 6.3-3.8"/>'
      + '<path d="M15.5 5.4a15 15 0 0 1 6.3 3.8"/>'
      + '<path d="M4.4 3.6 19.6 20.4"/>'
      + '</svg></div>'
      + '<h2 class="err-title">连不上站点</h2>'
      + '<p class="err-sub">' + esc(msg || '网络请求失败') + '</p>'
      // One line per <br> segment, deliberately.  The shipped WebView is
      // Chrome 113 and has no `text-wrap: pretty` (117+), so a segment longer
      // than the line wraps wherever it likes - the old copy broke between
      // 「访」 and 「问」 and left a two-character orphan centred on its own
      // line.  A phone at 360dp fits 22 CJK glyphs at --fs-subhead; these two
      // are 17 and 20, and `MobileErrorCopyTests` measures them.
      + '<p class="err-sub">该站点在国内网络需要代理（梯子）。<br>'
      + '手机开启 VPN 后点「重试」即可，无需电脑。</p>'
      + proxyRow()
      + '<div class="err-actions">'
      + '<button type="button" class="button button-primary" data-action="retry">重试</button>'
      + '<a class="button button-ghost" href="' + SITE + '/">打开原始站点</a>'
      + '</div>'
      + '</div>';
  }
  // In-app HTTP proxy affordance for the error panel.  The site's TLS
  // ClientHello gets reset from this network (GFW), so without a proxy the
  // feed call returns empty.  The host:port entered here is persisted by
  // the bridge and pushed into the JVM, so the next Jsoup.connect() honours
  // it.  The WebView does not read JVM properties - for the player iframe
  // the user has to set the phone-wide WiFi proxy, which the hint covers.
  function proxyRow() {
    var b = window.__asmrlibDiscover;
    var cur = '';
    // Error rendering must also work while the bridge is absent or failing.
    try { cur = (b && b.currentProxy) ? b.currentProxy() : ''; }
    catch (ignored) { /* The retry controls remain usable without proxy state. */ }
    var status = cur
      ? '<span class="ds-proxy-state">代理: ' + esc(cur)
        + ' <button type="button" class="ds-proxy-clear" '
        + 'data-action="proxy-clear" aria-label="清除代理">×</button></span>'
      : '<span class="ds-proxy-state ds-proxy-state-empty">未配置代理</span>';
    return '<div class="ds-proxy">' + status
      + ' <button type="button" class="ds-proxy-toggle" '
      + 'data-action="proxy-toggle">' + (cur ? '改' : '配置') + '</button>'
      + '</div>'
      + '<form class="ds-proxy-form" data-action="proxy-form" hidden>'
      + '<label class="ds-proxy-field"><span>主机</span>'
      + '<input type="text" name="host" inputmode="text" autocomplete="off" '
      + 'placeholder="192.168.1.5" required></label>'
      + '<label class="ds-proxy-field"><span>端口</span>'
      + '<input type="number" name="port" inputmode="numeric" min="1" max="65535" '
      + 'placeholder="7890" required></label>'
      + '<p class="err-sub ds-proxy-hint">仅用于本应用的网络请求。<br>'
      + '播放器仍需手机 WiFi 代理覆盖。</p>'
      + '<div class="ds-proxy-actions">'
      + '<button type="submit" class="button button-primary" '
      + 'data-action="proxy-save">保存并重试</button>'
      + '<button type="button" class="button button-ghost" '
      + 'data-action="proxy-cancel">取消</button>'
      + '</div>'
      + '</form>';
  }
  errEl.addEventListener('click', function (ev) {
    var action = ev.target.closest
      ? ev.target.closest('[data-action]') : null;
    if (!action) return;
    var act = action.getAttribute('data-action');
    if (act === 'retry') {
      clearAutoRetry();
      autoRetryCount = 0;
      errEl.hidden = true;
      load(retryUrl, 0, false);
    } else if (act === 'proxy-toggle') {
      var form = errEl.querySelector('.ds-proxy-form');
      if (form) form.hidden = !form.hidden;
    } else if (act === 'proxy-cancel') {
      var form2 = errEl.querySelector('.ds-proxy-form');
      if (form2) form2.hidden = true;
    } else if (act === 'proxy-clear') {
      var b = window.__asmrlibDiscover;
      if (b && b.setProxy) b.setProxy('', 0);
      showError('代理已清除。', retryUrl, false, false, false);
    }
  });
  errEl.addEventListener('submit', function (ev) {
    var form = ev.target.closest
      ? ev.target.closest('[data-action="proxy-form"]') : null;
    if (!form) return;
    ev.preventDefault();
    var host = (form.host.value || '').trim();
    var port = parseInt(form.port.value, 10) || 0;
    if (!host || port <= 0 || port > 65535) return;
    var b = window.__asmrlibDiscover;
    if (b && b.setProxy) b.setProxy(host, port);
    clearAutoRetry();
    autoRetryCount = 0;
    errEl.hidden = true;
    load(retryUrl, 0, false);
  });

  // Results land here from the native bridge (background fetch, UI-thread
  // evaluateJavascript). Unknown ids are stale deliveries from an abandoned
  // request or a previous page — ignored.
  window.__asmrlibDeliver = function (id, payloadJson) {
    var req = pending[id];
    if (!req) return;
    delete pending[id];
    if (req.busy) clearTimeout(req.busy);
    // A superseded chain page arrives after its chain was cancelled (user
    // tapped a chip / pager mid-chain) — drop it, never paint stale pages.
    if (req.chain && !state.chain) return;
    var res = null;
    try { res = JSON.parse(String(payloadJson == null ? '' : payloadJson)); }
    catch (e) { showError('数据桥返回异常：' + e, req.url, req.keep, false, req.append); return; }
    if (!res || !res.ok) { showError(res && res.error, req.url, req.keep, true, req.append); return; }
    state.url = req.url;
    autoRetryCount = 0;
    clearAutoRetry();
    try { render(res, req.append); }
    // A render that threw may have left half the page's markup behind, so this
    // one clears rather than keeps whatever survived.
    catch (e2) { showError('渲染失败：' + (e2 && e2.message ? e2.message : e2), req.url, false, false, req.append); return; }
    advanceChain();
  };

  // Multi-page sequences (return restore, pull-to-refresh) refill the grid
  // one page at a time; when the chain empties, scroll/finish callbacks run.
  function advanceChain() {
    var c = state.chain;
    if (!c) return;
    if (c.left.length) {
      request(c.left.shift(), true);
      return;
    }
    state.chain = null;
    if (c.y != null) requestAnimationFrame(function () { window.scrollTo(0, c.y); });
    if (c.after) c.after();
  }

  function runChain(pages, y, after, attempt) {
    // Same bridge-arrival retry window as load(): the restored boot can run
    // its script a beat before the interface lands after a back navigation.
    var b = window.__asmrlibDiscover;
    attempt = attempt || 0;
    if (!b) {
      if (attempt < 8) {
        setTimeout(function () { runChain(pages, y, after, attempt + 1); }, 400);
        return;
      }
      showError('数据桥不可用——请用 APK 打开本页', pages[0]);
      return;
    }
    state.chain = { left: pages.slice(1), y: y, after: after || null };
    request(pages[0], false, false);
  }

  function request(url, append, isRefresh, isAutoRetry, retryKeepGrid) {
    var b = window.__asmrlibDiscover;
    // A user-initiated request takes over the screen; a background retry does
    // not. Hiding the panel and repainting the skeleton on the way into a
    // silent retry made the error strobe at 0.9s / 1.8s / 3.6s while it was
    // up, so the retry leaves every pixel alone and only reports its outcome.
    if (!isAutoRetry) {
      errEl.hidden = true;
      clearAutoRetry();
    }
    var id = nextId();
    var req = { url: url, append: !!append, busy: 0, chain: !!state.chain,
      // "Keep the grid on failure": an appended page or a refresh reloads in
      // place, so whatever is already rendered stays valid. A fresh
      // navigation replaces the listing, so its failure must clear it.
      keep: !!append || !!isRefresh || !!retryKeepGrid };
    pending[id] = req;
    // Chain-first pages (return restore / refresh) keep the grid and its
    // indicator; a plain load shows the skeleton. The 250ms hold also
    // means memoized answers never flash any loading state.
    var chainFirst = !append && state.chain;
    req.busy = setTimeout(function () {
      req.busy = 0;
      if (!pending[id] || chainFirst) return;
      // A silent retry draws nothing: whatever the user is looking at (the
      // error panel, or their cards) is still true, and swapping it for a
      // skeleton would be a lie about what changed.
      if (isAutoRetry) return;
      if (append) {
        // Keep the existing cards visible; just mark the page busy.
        statusEl.className = 'ds-status is-loading';
        statusEl.textContent = '正在加载下一页…';
      } else if (state.switching) {
        // Switching sources: the cards on screen are still what the user was
        // reading, so name what is coming instead of blanking them. The
        // viewport is already gliding to the top (setSource owns that move,
        // which is why no scroll happens here).
        statusEl.className = 'ds-status is-loading';
        statusEl.textContent = '正在切换到 ' + (isOneUrl(url) ? 'ASMR.one' : '本站') + '…';
      } else {
        skeleton();
        statusEl.className = 'ds-status is-loading';
        statusEl.textContent = '正在加载站点内容…';
        window.scrollTo(0, 0);
      }
    }, 250);
    // The bridge times out at 20s; this watchdog only covers a lost delivery.
    setTimeout(function () {
      if (pending[id]) {
        if (req.busy) clearTimeout(req.busy);
        delete pending[id];
        showError('连接超时——站点长时间没有响应。', url, req.keep, true, req.append);
      }
    }, 25000);
    try {
      if (isRefresh) b.refresh(JSON.stringify(state.pages.length ? state.pages : [url]), id);
      else b.feed(url, id);
    }
    catch (e) {
      if (req.busy) clearTimeout(req.busy);
      delete pending[id];
      showError('数据桥调用失败：' + e, url, req.keep, true, req.append);
    }
  }

  function load(url, attempt, append) {
    // User-initiated navigation always supersedes any running chain — its
    // in-flight pages are dropped on delivery (req.chain guard).
    state.chain = null;
    // Read the bridge fresh on every attempt: after a back navigation the
    // page script can run slightly before the interface lands, so a short
    // retry window covers the race without a reload.
    var b = window.__asmrlibDiscover;
    attempt = attempt || 0;
    if (!b) {
      if (attempt < 8) {
        setTimeout(function () { load(url, attempt + 1, append); }, 400);
        return;
      }
      showError('数据桥不可用——请用 APK 打开本页', url);
      return;
    }
    request(url, append);
  }

  function card(c, i) {
    var idx = plateIdx(c.t);
    var gn = c.g || [];
    var tags = '';
    // `b`, not `i`: `var` is function-scoped, so a `for (var i = ...)` here
    // would redeclare the `i` parameter and leave every card with the same
    // rank after the loop - which silently flattens the entrance cascade.
    for (var b = 0; b < gn.length && b < 5; b++) {
      tags += '<i class="cover-tag">#' + esc(gn[b]) + '</i>';
    }
    if (tags) tags = '<span class="cover-tags">' + tags + '</span>';
    // The host is this page's identity line: the one stable fact about where
    // a work lives, set with a globe glyph in the accent - the same
    // prominent-voice row the reference uses for its CV names.
    var host = '';
    try { host = new URL(c.u).hostname.replace(/^www\./, ''); } catch (e) {}
    var meta = '';
    if (host) {
      meta = '<span class="cover-meta-row">'
        + '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" '
        + 'stroke-width="1.5" stroke-linecap="round" aria-hidden="true">'
        + '<circle cx="8" cy="8" r="6"/>'
        + '<path d="M2 8h12M8 2c1.9 1.7 2.9 3.7 2.9 6S9.9 12.3 8 14C6.1 12.3 5.1 10.3 5.1 8S6.1 3.7 8 2z"/>'
        + '</svg><span class="cover-src">' + esc(host) + '</span></span>';
    }
    // The plate is always rendered under the cover: it shows through when the
    // image fails (fail() unhides it) and is the whole card when the site gave
    // no cover at all (is-plate). The old markup branched into two shapes and
    // leaked an unbalanced </div>, and carried a .cover-scrim that only
    // duplicated .cover-thumb::after.
    return '<a class="cover-card" href="' + esc(c.u) + '"'
      + ' style="--i:' + (i || 0) + '"'
      + ' data-t="' + esc(c.t || '') + '" data-c="' + esc(c.c || '') + '"'
      + ' data-d="' + esc(c.d || '') + '"'
      + ' aria-label="' + esc(c.t || c.u) + '">'
      + '<div class="cover-thumb' + (c.c ? '' : ' is-plate') + '">'
      + (c.c
        ? '<img class="cover-thumb-img" data-cover-img src="' + esc(c.c) + '" alt="" '
          + 'width="640" height="360" loading="lazy" decoding="async" '
          + 'referrerpolicy="no-referrer">'
        : '')
      + '<div class="cover-thumb-fallback plate-' + idx + '" aria-hidden="true">'
      + '<span class="cover-initial">' + esc(initial(c.t)) + '</span></div>'
      // The duration sits on the cover's foot the way the reference app pins
      // its play count: a measurement belongs on the picture it measures.
      + (c.d
        ? '<span class="cover-flag">'
          + '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" '
          + 'stroke-width="1.5" stroke-linecap="round" aria-hidden="true">'
          + '<circle cx="8" cy="8" r="6"/><path d="M8 4.6V8l2.3 1.4"/></svg>'
          + esc(c.d) + '</span>'
        : '')
      + '</div>'
      + '<span class="cover-info">'
      + '<strong class="cover-title">' + esc(c.t || c.u) + '</strong>'
      + meta
      // Tags ride under the title as bare #words in the secondary colour -
      // the pill chrome the old top-edge badges spent never paid for itself.
      + tags
      + '</span></a>';
  }

  // The header label must not lie: a tag page is not "最新收录", and the pill
  // beside it already carries the page number.
  function viewTitle() {
    var one = /asmrone:\/\/search\/([^/?#]+)/.exec(state.url || '');
    if (one) {
      try { return '搜索 · ' + decodeURIComponent(one[1]); } catch (e) { return '搜索'; }
    }
    var m = /\/tags\/([^/?#]+)/.exec(state.url || '');
    if (!m) return '最新收录';
    try { return '标签 · ' + decodeURIComponent(m[1]); } catch (e) { return '标签'; }
  }

  function render(res, append) {
    var html = res.cards.map(card).join('');
    var firstNew = null;
    // Any render ends a switch: the dim comes off with whatever takes the
    // grid, so a switch that failed cannot leave the next listing dimmed.
    var wasSwitching = !!state.switching;
    state.switching = false;
    setSwitchDim(false);
    // The splash is a one-shot: every later render (next page / chain refill
    // / pull-to-refresh) must NOT re-show it. A simple class toggle on the
    // existing element keeps the fade on its own timeline and skips the
    // layout cost of mounting and unmounting a layer.
    if (splashEl && !splashEl.classList.contains('is-gone')) {
      splashEl.classList.add('is-gone');
    }
    if (append && grid.querySelector('.cover-card')) {
      // 下一页: append below and glide to the new content instead of
      // teleporting to the top of the page.
      firstNew = grid.children.length;
      grid.insertAdjacentHTML('beforeend', html);
      state.pages.push(state.url);
    } else {
      grid.innerHTML = html;
      // Chain-first renders (restore / refresh) keep the viewport; only a
      // plain navigation resets to the top. A source switch is already gliding
      // there from setSource, and snapping would cut that move short a few
      // frames before it lands.
      if (!state.chain && !wasSwitching) window.scrollTo(0, 0);
      state.pages = [state.url];
    }
    fixBrokenCovers();
    var slugs = Object.keys(res.tags || {}).sort(function (a, b) {
      return (res.tags[b] - res.tags[a]) || a.localeCompare(b);
    }).slice(0, 14);
    tagsEl.innerHTML = slugs.map(function (s) {
      return '<button type="button" class="chip" data-slug="' + esc(s) + '">'
        + esc(s) + ' <em>' + res.tags[s] + '</em></button>';
    }).join('');
    prevBtn.disabled = !(res.page > 1);
    nextBtn.disabled = !res.next;
    // The count is chrome, not a status line: it belongs in the section
    // header's pill, which leaves #ds-status for transient messages only.
    // Total is read off the grid so an appended page still reports the whole
    // loaded run rather than just its own slice.
    secEl.hidden = false;
    secTitleEl.textContent = viewTitle();
    pagerEl.hidden = false;
    srcEl.hidden = false;
    // A payload that rendered is proof the error is stale (request() hides the
    // panel on the way in; this keeps that true for any other entry point).
    errEl.hidden = true;
    countEl.textContent = '第 ' + (res.page || 1) + ' 页 · 共 '
      + grid.querySelectorAll('.cover-card').length + ' 条';
    statusEl.className = 'ds-status';
    statusEl.textContent = '';
    // The glide-to-new-content only applies to user-triggered pagination —
    // background chain refills (restore / refresh) keep the viewport.
    if (firstNew !== null && !state.chain && grid.children[firstNew]) {
      // Reduce Motion means exactly that: a smooth glide is a large, fast
      // movement across the viewport, which is the thing the setting exists
      // to avoid. Jump instead.
      var glide = window.matchMedia
        && window.matchMedia('(prefers-reduced-motion: reduce)').matches
        ? 'auto' : 'smooth';
      grid.children[firstNew].scrollIntoView({ behavior: glide, block: 'start' });
    }
  }

  function fixBrokenCovers() {
    var imgs = grid.querySelectorAll('img[data-cover-img]');
    for (var i = 0; i < imgs.length; i++) {
      if (imgs[i].complete && imgs[i].naturalWidth === 0) fail(imgs[i]);
    }
  }
  function fail(img) {
    var box = img.parentElement;
    img.hidden = true;
    if (box) {
      box.classList.add('has-cover-fallback');
      var fb = box.querySelector('.cover-thumb-fallback');
      if (fb) fb.hidden = false;
    }
  }
  document.addEventListener('error', function (ev) {
    var el = ev.target;
    if (el && el.tagName === 'IMG' && el.hasAttribute('data-cover-img')) fail(el);
  }, true);

  tagsEl.addEventListener('click', function (ev) {
    var btn = ev.target.closest ? ev.target.closest('[data-slug]') : null;
    if (!btn) return;
    var slug = btn.getAttribute('data-slug');
    // asmr.one tags are display names, not slugs: they route back into the
    // virtual search URL so the chips work identically on both sources.
    if (state.url.indexOf('asmrone://') === 0) {
      load(oneUrl(slug, 1, oneSite()));
    } else {
      load(SITE + '/tags/' + slug);
    }
  });
  // Card tap: resolve the post to bare player embeds and play inside the
  // bundled play page (our UI). The card's title/cover/date ride along in
  // the hash so the play page can render its hero without new requests.
  // Without the bridge (plain browser) the anchor default stands — the
  // site's own page opens instead.
  grid.addEventListener('click', function (ev) {
    var cardEl = ev.target.closest ? ev.target.closest('.cover-card') : null;
    if (!cardEl || !window.__asmrlibDiscover) return;
    ev.preventDefault();
    var payload = JSON.stringify({
      u: cardEl.getAttribute('href'),
      t: cardEl.getAttribute('data-t') || '',
      c: cardEl.getAttribute('data-c') || '',
      d: cardEl.getAttribute('data-d') || ''
    });
    saveReturn();
    location.href = 'play.html#' + encodeURIComponent(payload);
  });
  prevBtn.addEventListener('click', function () {
    var one = /asmrone:\/\/search\/([^/]+)\/(\d+)(?:\/(one|100|200|300))?/.exec(state.url);
    if (one) {
      var p = parseInt(one[2], 10);
      if (p > 1) load(oneUrl(decodeURIComponent(one[1]), p - 1, oneSite()));
      return;
    }
    var m = /[?&]page=(\d+)/.exec(state.url);
    var p = m ? parseInt(m[1], 10) : 1;
    if (p > 1) load(p === 2 ? SITE + '/' : SITE + '/?page=' + (p - 1));
  });
  nextBtn.addEventListener('click', function () {
    var one = /asmrone:\/\/search\/([^/]+)\/(\d+)(?:\/(one|100|200|300))?/.exec(state.url);
    if (one) {
      load(oneUrl(decodeURIComponent(one[1]), parseInt(one[2], 10) + 1, oneSite()), 0, true);
      return;
    }
    var m = /[?&]page=(\d+)/.exec(state.url);
    var p = m ? parseInt(m[1], 10) : 1;
    load(SITE + '/?page=' + (p + 1), 0, true);
  });

  // ---- pull-to-refresh ----
  // The pull is JS-driven: arm only when the scroll is at the very top,
  // translate the content with resistance, and trigger past a threshold.
  // The WebView's own overscroll glow is disabled natively so the two
  // effects never fight. Listeners stay passive — nothing here needs
  // preventDefault since the page cannot scroll above its top edge.
  var PTR_THRESHOLD = 72, PTR_MAX = 120;
  // Must match the arc's stroke-dasharray in the markup (2*pi*r, r=14):
  // the pull walks the dashoffset from this down to 0, so the arc fills.
  var PTR_ARC_LEN = 87.96;
  var ptr = { active: false, armed: false, y0: 0, pull: 0 };

  function setPull(px) {
    ptr.pull = px;
    mainEl.style.transform = px > 0 ? 'translateY(' + px + 'px)' : '';
    var t = Math.min(1, px / PTR_THRESHOLD);
    ptrEl.style.opacity = px > 0 ? String(t) : '0';
    // The arc IS the progress read-out: no rotation while pulling, just fill.
    ptrArcEl.style.strokeDashoffset = String(PTR_ARC_LEN * (1 - t));
    // "armed" means the release would refresh, so it is the threshold
    // crossing itself rather than "a downward pull happened" — that is what
    // the ring's accent state and the release both key on.
    var armed = px >= PTR_THRESHOLD;
    ptr.armed = armed;
    ptrEl.classList.toggle('armed', armed);
  }

  function endRefresh() {
    state.refreshing = false;
    ptrEl.classList.remove('busy');
    ptrEl.classList.remove('armed');
    ptr.armed = false;
    ptrEl.style.opacity = '0';
    ptr.pull = 0;
    mainEl.style.transform = '';
    ptrArcEl.style.strokeDashoffset = String(PTR_ARC_LEN);
    // The header button shares the same busy class so its icon spins while
    // the arc ring does, and goes still when the refresh finishes.
    if (refreshBtn) refreshBtn.classList.remove('is-busy');
  }

  function startRefresh() {
    if (state.refreshing || !state.pages.length) return;
    state.refreshing = true;
    ptrEl.classList.add('busy');
    ptrEl.style.opacity = '1';
    setPull(PTR_THRESHOLD);
    if (refreshBtn) refreshBtn.classList.add('is-busy');
    // Refresh the full loaded chain: the bridge purges every page's memo
    // (unblocking its stale waiters), re-fetches the base page, and the
    // chain below re-requests the appended pages — now cache misses.
    var pages = state.pages.slice();
    state.chain = { left: pages.slice(1), y: null, after: function () {
      endRefresh();
      window.scrollTo(0, 0);
    } };
    request(pages[0], false, true);
  }

  // Fallback refresh control — always available even when WebView swallows
  // the pull gesture. Same startRefresh() the gesture would have called;
  // re-entrant guard in startRefresh() keeps a double tap from queuing
  // two refreshes.
  var refreshBtn = document.getElementById('ds-refresh-btn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', function () {
      // A bare load() before any chain exists is also valid — the very
      // first paint also benefits from a tap-to-retry path.
      if (!state.pages.length) {
        if (typeof load === 'function') load(SITE + '/');
        return;
      }
      startRefresh();
    });
  }

  mainEl.addEventListener('touchstart', function (ev) {
    if (ev.touches.length !== 1) { ptr.active = false; return; }
    ptr.active = document.scrollingElement.scrollTop <= 0 && !state.refreshing;
    ptr.armed = false;
    ptr.y0 = ev.touches[0].clientY;
    // With the finger down the content tracks the touch with no lag; the
    // settle transition only exists after the release (see the CSS).
    if (ptr.active) mainEl.classList.add('is-dragging');
  }, { passive: true });
  mainEl.addEventListener('touchmove', function (ev) {
    if (!ptr.active || state.refreshing) return;
    if (document.scrollingElement.scrollTop > 0) { setPull(0); return; }
    var dy = ev.touches[0].clientY - ptr.y0;
    if (dy <= 0) { setPull(0); return; }
    setPull(Math.min(dy * 0.4, PTR_MAX));
  }, { passive: true });
  function releasePull() {
    mainEl.classList.remove('is-dragging');
    if (!ptr.active) return;
    ptr.active = false;
    if (ptr.armed && !state.refreshing) startRefresh();
    else setPull(0);
  }
  mainEl.addEventListener('touchend', releasePull, { passive: true });
  // A cancelled touch (system gesture, incoming call) never fires touchend;
  // without this the page would keep .is-dragging and never settle again.
  mainEl.addEventListener('touchcancel', releasePull, { passive: true });

  // ---- return restoration ----
  // Back navigation fully reloads this page, so the feed position has to
  // ride through sessionStorage: the card tap (and pagehide as a safety
  // net) persists the loaded page chain plus the scroll offset, and the
  // next boot replays the chain from the memoized bridge before restoring
  // the exact viewport.
  var RETURN_KEY = 'ds-return';
  function saveReturn() {
    if (!state.pages.length) return;
    try {
      sessionStorage.setItem(RETURN_KEY, JSON.stringify({
        url: state.url,
        pages: state.pages,
        y: window.scrollY,
        at: Date.now()
      }));
    } catch (e) { /* storage unavailable — back lands at the top */ }
  }
  window.addEventListener('pagehide', saveReturn);

  // ---- nav bar: the large-title hand-off --------------------------------
  // The large title scrolls under the bar and the inline title fades in to
  // take over, which is the whole point of the pattern. Throttled through rAF
  // because a scroll listener that writes layout on every event is what makes
  // a WebView feel heavy, and passive because it never calls preventDefault.
  var navEl = document.getElementById('ds-nav');
  if (navEl) {
    var navTick = false;
    var syncNav = function () {
      navTick = false;
      var y = window.scrollY || document.documentElement.scrollTop || 0;
      navEl.classList.toggle('is-stuck', y > 12);
    };
    window.addEventListener('scroll', function () {
      if (navTick) return;
      navTick = true;
      window.requestAnimationFrame(syncNav);
    }, { passive: true });
    syncNav();
  }

  // Visual QA hook: render a synthetic payload with no bridge round-trip.
  // This page's only data source is a live site, so on a network that blocks
  // it there is otherwise no way to exercise the grid at all (the play page
  // exposes __asmrlibNetBannerPreview for exactly the same reason).
  window.__asmrlibDiscoverPreview = render;

  var boot = null;
  try { boot = JSON.parse(sessionStorage.getItem(RETURN_KEY) || 'null'); } catch (e) {}
  if (boot && boot.url && boot.pages && boot.pages.length
      && typeof boot.y === 'number'
      && Date.now() - (boot.at || 0) < 3600000) {
    runChain(boot.pages, boot.y, null);
  } else {
    try { sessionStorage.removeItem(RETURN_KEY); } catch (e) {}
    load(SITE + '/');
  }
})();
"""


def build_discover_html() -> str:
    """Render the single-file discover page bundled into the APK."""
    return (
        "<!doctype html>\n"
        f"{MARKER}\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, '
        'viewport-fit=cover">\n'
        '<meta http-equiv="Content-Security-Policy" content='
        '"default-src \'none\'; img-src https: data:; style-src \'unsafe-inline\'; '
        "script-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; "
        "object-src 'none'; connect-src 'none'\">\n"
        f"{THEME_COLOR_META}"
        "<title>ASMR 收藏馆 · 发现</title>\n"
        f"<style>{_EXTRA_CSS}</style>\n"
        "</head>\n<body class='discover-page'>\n"
        # First-paint splash: a centred brand mark inside a sweeping accent
        # ring, with a one-line label. It covers the page until the very
        # first render() lands, then fades (.42s) and is gone for the rest
        # of the session — pull-to-refresh / pagination / chain refills must
        # never bring it back, so the JS toggles `is-gone` and never re-adds
        # the class. Reduced-motion users keep the fade but drop the rotation.
        "<div class='ds-splash' id='ds-splash' aria-hidden='true'>"
        "<div class='ds-splash-mark'>"
        "<svg class='ds-splash-ring' viewBox='0 0 96 96' fill='none' "
        "stroke-linecap='round' aria-hidden='true'>"
        "<circle class='ds-splash-track' cx='48' cy='48' r='42' stroke-width='3'/>"
        "<circle class='ds-splash-arc' cx='48' cy='48' r='42' stroke-width='3' "
        "stroke-dasharray='263.89' stroke-dashoffset='197.92' "
        "transform='rotate(-90 48 48)'/>"
        "</svg>"
        "<span class='ds-splash-glyph' aria-hidden='true'>"
        "<svg viewBox='0 0 16 16' width='26' height='26' fill='none'>"
        "<rect x='1.5' y='6' width='2.2' height='4' rx='1' fill='currentColor'/>"
        "<rect x='4.9' y='3.5' width='2.2' height='9' rx='1' fill='currentColor'/>"
        "<rect x='8.3' y='5' width='2.2' height='6' rx='1' fill='currentColor'/>"
        "<rect x='11.7' y='2.5' width='2.2' height='11' rx='1' fill='currentColor'/>"
        "</svg></span>"
        "</div>"
        "<p class='ds-splash-label'>正在发现内容</p>"
        "<p class='ds-splash-sub'>首次连接可能需要几秒钟</p>"
        "</div>\n"
        # Nav bar. The inline title starts invisible and fades in once the
        # large title below has scrolled under the bar — the standard iOS
        # hand-off, and the reason the bar carries no border until then.
        "<header class='nav' id='ds-nav'>"
        "<span class='brand-mark' aria-hidden='true'>"
        "<svg class='brand-ico' viewBox='0 0 16 16' fill='none'>"
        "<rect x='1.5' y='6' width='2.2' height='4' rx='1' fill='currentColor'/>"
        "<rect x='4.9' y='3.5' width='2.2' height='9' rx='1' fill='currentColor'/>"
        "<rect x='8.3' y='5' width='2.2' height='6' rx='1' fill='currentColor'/>"
        "<rect x='11.7' y='2.5' width='2.2' height='11' rx='1' fill='currentColor'/>"
        "</svg></span>"
        "<strong class='nav-title'>发现</strong>"
        # The fallback refresh control: WebView's native scroll often swallows
        # the pull gesture before our touchmove listener sees it, so the
        # gesture-driven arc never appears. A button in the bar is the
        # always-available alternative — it calls the same startRefresh() the
        # gesture would have, and shares its .is-busy class so the icon spins
        # during the refresh and the arc ring below is also hidden.
        "<button type='button' class='icon-btn ds-refresh' id='ds-refresh-btn' "
        "aria-label='刷新内容'>"
        "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' "
        "stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round' "
        "aria-hidden='true'>"
        "<path d='M13.2 4.5v3.6h-3.6'/>"
        "<path d='M2.8 11.5v-3.6h3.6'/>"
        "<path d='M13.2 8.1a5.4 5.4 0 0 0-9.7-2.6'/>"
        "<path d='M2.8 7.9a5.4 5.4 0 0 0 9.7 2.6'/>"
        "</svg></button>"
        "</header>\n"
        "<div class='page-head'><h1>发现</h1></div>\n"
        "<main class='wrap'>\n"
        "<div class='ds-ptr' id='ds-ptr' aria-hidden='true'>"
        # Inline SVG, like every other icon on this page: the CSP is
        # connect-src 'none' and nothing may be fetched. The arc is the pull
        # progress read-out - setPull() walks its dashoffset from the whole
        # circumference (87.96 = 2*pi*14) to zero, so the stroke is the
        # gesture and not a decoration. It starts at 12 o'clock via the
        # circle's own rotate; the busy spin animates the SVG root instead,
        # so the two transforms can never meet on one element. Both strokes
        # come from the theme, so the indicator works in light mode too.
        "<svg class='ds-ptr-ring' viewBox='0 0 36 36' width='26' height='26' fill='none'>"
        "<circle class='ds-ptr-track' cx='18' cy='18' r='14' stroke-width='3.2'/>"
        "<circle class='ds-ptr-arc' id='ds-ptr-arc' cx='18' cy='18' r='14' "
        "stroke-width='3.2' stroke-linecap='round' "
        "stroke-dasharray='87.96' stroke-dashoffset='87.96' "
        "transform='rotate(-90 18 18)'/></svg></div>\n"
        "<div class='ds-tabs' id='ds-tabs' role='tablist'>"
        "<button type='button' class='ds-tab is-active' id='ds-tab-site' role='tab'"
        " aria-selected='true'>本站</button>"
        "<button type='button' class='ds-tab' id='ds-tab-asmrone' role='tab'"
        " aria-selected='false'>ASMR.one</button>"
        "</div>\n"
        "<form class='ds-search is-off' id='ds-search'>"
        "<input type='search' id='ds-search-input' enterkeyhint='search'"
        " placeholder='在 ASMR.one 搜索作品（如：掏耳）' autocomplete='off'>"
        "<button type='submit' class='ds-search-go'>搜索</button>"
        "</form>\n"
        # The mirror family the reference player ships as one site setting
        # (its default is 200): same API, four hosts — when one domain is
        # unreachable from this network, the next answers. The choice rides
        # the virtual search URL's tail segment and persists per device.
        "<div class='ds-lines is-off' id='ds-lines'>"
        "<span class='ds-lines-label'>线路</span>"
        "<button type='button' class='ds-line is-active' data-site='one'"
        " aria-label='线路 asmr.one'>one</button>"
        "<button type='button' class='ds-line' data-site='100'"
        " aria-label='线路 asmr-100'>100</button>"
        "<button type='button' class='ds-line' data-site='200'"
        " aria-label='线路 asmr-200'>200</button>"
        "<button type='button' class='ds-line' data-site='300'"
        " aria-label='线路 asmr-300'>300</button>"
        "</div>\n"
        "<div class='chips' id='ds-tags'></div>\n"
        "<div class='ds-sec' id='ds-sec' hidden>"
        "<span class='ds-sec-title' id='ds-sec-title'>最新收录</span>"
        "<span class='ds-sec-count' id='ds-count'></span></div>\n"
        "<div class='cover-grid' id='ds-grid'></div>\n"
        "<p class='ds-status' id='ds-status' role='status'></p>\n"
        "<div class='ds-pager' id='ds-pager' hidden>"
        "<button type='button' class='button button-secondary' id='ds-prev' disabled>"
        "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.8' "
        "stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
        "<path d='M10 3.4 5.6 8 10 12.6'/></svg>上一页</button>"
        "<button type='button' class='button button-secondary' id='ds-next' disabled>下一页"
        "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.8' "
        "stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
        "<path d='M6 3.4 10.4 8 6 12.6'/></svg></button>"
        "</div>\n"
        "<p class='ds-src' id='ds-src' hidden>来自站点实时数据</p>\n"
        "<section class='glass-panel' id='ds-error' hidden></section>\n"
        "</main>\n"
        "<script type='application/json' id='ds-config'>"
        '{"site":"https://asmrlib.com"}</script>\n'
        f"<script>{_DISCOVER_JS}</script>\n"
        "</body>\n</html>\n"
    )


def write_discover_asset(target: Path) -> Path:
    """Write the asset (idempotent)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_discover_html(), encoding="utf-8")
    return target


# The play page resolves bare player embeds and jumps the top-level WebView
# to the chosen one — the same ship-as-online-mode path the APK already
# uses (a sandboxed iframe was tried and 404s: byse's API needs cookies
# that a file://-top-level iframe sees as third-party, which Android
# WebView blocks by default). The shield keeps guarding the player page.
_PLAY_JS = r"""
(function () {
  'use strict';
  var SITE = JSON.parse(document.getElementById('pl-config').textContent).site;
  var dockEl = document.getElementById('pl-dock');
  var statusEl = document.getElementById('pl-status');
  var errEl = document.getElementById('pl-error');
  var heroEl = document.getElementById('pl-hero');
  var skelEl = document.getElementById('pl-skel');
  var titleEl = document.getElementById('pl-title');
  var pending = {};   // requestId -> post url; abandoned ids are ignored
  var seq = 0;
  // Eara's continuous playback: the playable sources of the current resolve,
  // handed to the player as one queue.  Buttons carry only an index into it —
  // copying 80 tracks into every row's attribute would bloat the DOM.
  var playQueue = [];
  // Embed rows resolve on tap (bridge resolveEmbed → hidden-WebView sniff);
  // these track the in-flight requests so a delivery finds its row, plus the
  // resolve result the degrade path needs to rebuild a failed row as a link.
  var embedPending = {};  // requestId -> row element
  var embedSeq = 0;
  var lastSources = [];

  // Inline SVG only — the page CSP forbids every external resource, so the
  // icon set rides in the markup instead of an icon font or sprite request.
  var ICON_PLAY = '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">'
    + '<path d="M5.2 3.1v9.8c0 .52.57.84 1.01.56l7.5-4.9a.66.66 0 0 0 0-1.12l-7.5-4.9a.66.66 0 0 0-1.01.56z"/></svg>';
  var ICON_CAL = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    + 'stroke-width="1.4" aria-hidden="true"><rect x="2.2" y="3.4" width="11.6" '
    + 'height="10.4" rx="2.2"/><path d="M2.2 6.6h11.6M5.4 2.2v2.6M10.6 2.2v2.6"/></svg>';
  var ICON_GLOBE = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    + 'stroke-width="1.3" aria-hidden="true"><circle cx="8" cy="8" r="6.1"/>'
    + '<path d="M1.9 8h12.2M8 1.9c1.6 1.7 2.4 3.8 2.4 6.1S9.6 12.4 8 14.1C6.4 12.4 '
    + '5.6 10.3 5.6 8S6.4 3.6 8 1.9z"/></svg>';
  var ICON_SHIELD = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    + 'stroke-width="1.4" aria-hidden="true"><path d="M8 1.8 3.2 3.6v4.2c0 3 2 5.4 '
    + '4.8 6.4 2.8-1 4.8-3.4 4.8-6.4V3.6L8 1.8z"/><path d="m5.9 7.9 1.5 1.5 2.8-2.9"/></svg>';
  var ICON_EXT = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    + 'stroke-width="1.4" aria-hidden="true"><path d="M6.4 3.2H3.6a1.2 1.2 0 0 0-1.2 '
    + '1.2v8a1.2 1.2 0 0 0 1.2 1.2h8a1.2 1.2 0 0 0 1.2-1.2v-2.8"/>'
    + '<path d="M9.6 2.4h4v4M13.6 2.4 7.6 8.4"/></svg>';
  var ICON_GRID = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    + 'stroke-width="1.4" aria-hidden="true"><rect x="2.2" y="2.2" width="5" '
    + 'height="5" rx="1.4"/><rect x="8.8" y="2.2" width="5" height="5" rx="1.4"/>'
    + '<rect x="2.2" y="8.8" width="5" height="5" rx="1.4"/>'
    + '<rect x="8.8" y="8.8" width="5" height="5" rx="1.4"/></svg>';
  var ICON_LIST = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    + 'stroke-width="1.4" aria-hidden="true"><path d="M5.2 3.6h8M5.2 8h8M5.2 12.4h8"/>'
    + '<path d="M2.6 3.6h.01M2.6 8h.01M2.6 12.4h.01" stroke-width="2" '
    + 'stroke-linecap="round"/></svg>';
  // The skeleton markup lives in the page (next to <p id=pl-status>) and
  // `load()` toggles `hidden` so the source list can fade in without a
  // repaint race.  See mobile_skin.py `.pl-skel-row` for the shimmer.

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  // The discover page passes the tapped card's data as a JSON hash
  // ({u,t,c,d}) so this page can render a cover hero without extra
  // requests. Old/direct links keep a bare-URL hash — accepted unchanged.
  function postEntry() {
    var raw = location.hash.replace(/^#/, '');
    var decoded = raw;
    try { decoded = decodeURIComponent(raw); }
    catch (ignored) { /* A malformed deep link must still reach the error UI. */ }
    try {
      var o = JSON.parse(decoded);
      if (o && o.u) {
        return { u: String(o.u), t: String(o.t || ''), c: String(o.c || ''), d: String(o.d || '') };
      }
    } catch (e) { /* plain-url hash */ }
    return { u: decoded, t: '', c: '', d: '' };
  }
  // Which world this work came from. Both sources play in the app now, but
  // the page still names which one you are in: the hero chip is where a
  // 「为什么这个站和那个站长得一样」 turns into an answer. The mirrors are
  // one source on four hosts.
  function originLabel(url) {
    var host = String(url || '').replace(/^[a-z]+:\/\//, '').split('/')[0];
    if (/^asmr(-(100|200|300))?\.(one|com)$/.test(host)) return 'ASMR.one';
    if (/asmrlib/.test(host)) return '本站';
    return host;
  }
  // A dead cover host must not leave a broken-image icon in the hero.
  document.addEventListener('error', function (ev) {
    var el = ev.target;
    if (el && el.tagName === 'IMG' && el.hasAttribute('data-hero-img')) el.remove();
  }, true);

  function errorPanel(msg, post) {
    dockEl.hidden = true;
    skelEl.hidden = true;
    statusEl.hidden = true;
    errEl.hidden = false;
    errEl.innerHTML =
      '<div class="err-hero">'
      + '<div class="err-glyph" aria-hidden="true">🎬</div>'
      + '<h2 class="err-title">无法打开播放器</h2>'
      + '<p class="err-sub">' + esc(msg || '未知错误') + '</p>'
      // Same one-line-per-segment rule as the discover page's hint: Chrome 113
      // has no orphan control, so the copy has to fit.  This one used to be 25
      // glyphs and wrapped to a three-character tail.
      + '<p class="err-sub">播放器在国内网络需要代理（梯子）。<br>'
      + '手机开启 VPN 后点「重试」即可，无需电脑。</p>'
      + proxyRow()
      + '<div class="err-actions">'
      + '<button type="button" class="button button-primary" data-action="retry">重试</button>'
      + '<a class="button button-ghost" href="discover.html">返回发现页</a>'
      + '</div>'
      + (post ? '<a class="err-link" href="' + esc(post) + '">打开原帖页 →</a>' : '')
      + '</div>';
  }
  // This page is a separate document: discover.html's helper and local bridge
  // variables do not exist here. Resolve the bridge when the controls run.
  function proxyRow() {
    var b = window.__asmrlibDiscover;
    var cur = '';
    try { cur = (b && b.currentProxy) ? b.currentProxy() : ''; }
    catch (ignored) { /* Network errors still need a visible recovery panel. */ }
    var status = cur
      ? '<span class="ds-proxy-state">代理: ' + esc(cur)
        + ' <button type="button" class="ds-proxy-clear" '
        + 'data-action="proxy-clear" aria-label="清除代理">×</button></span>'
      : '<span class="ds-proxy-state ds-proxy-state-empty">未配置代理</span>';
    return '<div class="ds-proxy">' + status
      + ' <button type="button" class="ds-proxy-toggle" '
      + 'data-action="proxy-toggle">' + (cur ? '改' : '配置') + '</button>'
      + '</div>'
      + '<form class="ds-proxy-form" data-action="proxy-form" hidden>'
      + '<label class="ds-proxy-field"><span>主机</span>'
      + '<input type="text" name="host" inputmode="text" autocomplete="off" '
      + 'placeholder="192.168.1.5" required></label>'
      + '<label class="ds-proxy-field"><span>端口</span>'
      + '<input type="number" name="port" inputmode="numeric" min="1" max="65535" '
      + 'placeholder="7890" required></label>'
      + '<p class="err-sub ds-proxy-hint">仅用于本应用的网络请求。<br>'
      + '播放器仍需手机 WiFi 代理覆盖。</p>'
      + '<div class="ds-proxy-actions">'
      + '<button type="submit" class="button button-primary" '
      + 'data-action="proxy-save">保存并重试</button>'
      + '<button type="button" class="button button-ghost" '
      + 'data-action="proxy-cancel">取消</button>'
      + '</div></form>';
  }
  errEl.addEventListener('click', function (ev) {
    var btn = ev.target.closest
      ? ev.target.closest('[data-action="retry"], [data-action^="proxy-"]') : null;
    if (!btn) return;
    var act = btn.getAttribute('data-action');
    if (act === 'retry') {
      errEl.hidden = true;
      load(0);
    } else if (act === 'proxy-toggle') {
      var form = errEl.querySelector('.ds-proxy-form');
      if (form) form.hidden = !form.hidden;
    } else if (act === 'proxy-cancel') {
      var form2 = errEl.querySelector('.ds-proxy-form');
      if (form2) form2.hidden = true;
    } else if (act === 'proxy-clear') {
      var b = window.__asmrlibDiscover;
      if (b && b.setProxy) b.setProxy('', 0);
      errorPanel('代理已清除。', postEntry().u);
    } else if (act === 'play-inapp') {
      // In-app playback: hand the stream to the native player through the
      // bridge. The cover rides along for the artwork backdrop; the source
      // page is the Referer the player host expects.  A multi-track resolve
      // (asmr.one works) goes as one queue so the player can continue.
      var b2 = window.__asmrlibDiscover;
      if (!b2) return;
      var entry = postEntry();
      var qTitle = entry.t || titleEl.textContent || '在线播放';
      var qReferer = btn.getAttribute('data-referer') || '';
      var qi = parseInt(btn.getAttribute('data-qi'), 10);
      if (b2.playDirectQueue && playQueue.length > 1 && qi >= 0) {
        b2.playDirectQueue(JSON.stringify(playQueue), qi, qTitle, entry.c || '', qReferer);
      } else if (b2.playDirect) {
        b2.playDirect(btn.getAttribute('data-url'),
          btn.getAttribute('data-kind'),
          qTitle,
          entry.c || '',
          qReferer);
      }
    }
  });
  // The strip and the per-row "+" share the queue launch; only the entry
  // index differs. The page stays put — the player opens over it — and the
  // added row shows a tick, which is the whole feedback (Eara's addedToQueue).
  dockEl.addEventListener('click', function (ev) {
    var t = ev.target.closest
      ? (ev.target.closest('[data-action="play-queue"]')
        || ev.target.closest('[data-action="queue-add"]')) : null;
    if (!t) return;
    ev.preventDefault();
    ev.stopPropagation();
    var bq = window.__asmrlibDiscover;
    if (!bq || !bq.playDirectQueue || playQueue.length < 2) return;
    var entryQ = postEntry();
    var titleQ = entryQ.t || titleEl.textContent || '在线播放';
    var qiQ = t.hasAttribute('data-qi') ? parseInt(t.getAttribute('data-qi'), 10) : 0;
    bq.playDirectQueue(JSON.stringify(playQueue), qiQ, titleQ,
      entryQ.c || '', t.getAttribute('data-referer') || '');
    if (t.getAttribute('data-action') === 'queue-add') {
      t.classList.add('is-added');
      t.textContent = '✓';
      t.setAttribute('aria-pressed', 'true');
    }
  });
  // The in-app button sits inside the row's <a>; without this a tap would
  // both play in-app AND jump to the embed page.
  dockEl.addEventListener('click', function (ev) {
    var pill = ev.target.closest
      ? ev.target.closest('[data-action="play-inapp"],[data-action="embed-play"]') : null;
    if (!pill) return;
    ev.preventDefault();
    ev.stopPropagation();
    if (pill.getAttribute('data-action') === 'embed-play') {
      playEmbed(pill.closest('.play-enter') || pill);
      return;
    }
    var b = window.__asmrlibDiscover;
    if (!b) return;
    var entry = postEntry();
    var qTitle = entry.t || titleEl.textContent || '在线播放';
    var qReferer = pill.getAttribute('data-referer') || '';
    var qi = parseInt(pill.getAttribute('data-qi'), 10);
    if (b.playDirectQueue && playQueue.length > 1 && qi >= 0) {
      b.playDirectQueue(JSON.stringify(playQueue), qi, qTitle, entry.c || '', qReferer);
    } else if (b.playDirect) {
      b.playDirect(pill.getAttribute('data-url'),
        pill.getAttribute('data-kind'),
        qTitle,
        entry.c || '',
        qReferer);
    }
  });
  // The row around it is the same action over a much bigger target: a tap
  // anywhere on a media row plays that track. Anything carrying its own
  // data-action (the pill, the "+") is left to its own handler.
  dockEl.addEventListener('click', function (ev) {
    if (!ev.target.closest) return;
    if (ev.target.closest('[data-action]')) return;
    var row = ev.target.closest('.play-enter[data-inapp]');
    if (!row) return;
    ev.preventDefault();
    if (row.hasAttribute('data-embed')) {
      playEmbed(row);
      return;
    }
    var b = window.__asmrlibDiscover;
    if (!b) return;
    var entryR = postEntry();
    var titleR = entryR.t || titleEl.textContent || '在线播放';
    var qi = parseInt(row.getAttribute('data-qi'), 10);
    var refererR = row.getAttribute('data-referer') || '';
    if (b.playDirectQueue && playQueue.length > 1 && qi >= 0) {
      b.playDirectQueue(JSON.stringify(playQueue), qi, titleR, entryR.c || '', refererR);
    } else if (b.playDirect) {
      b.playDirect(row.getAttribute('data-url'), row.getAttribute('data-kind'),
        titleR, entryR.c || '', refererR);
    }
  });
  // ---- embed resolve on tap ------------------------------------------
  // An embed row's URL addresses a player page, not media bytes, so the
  // tap first asks the bridge to sniff the page in a hidden WebView. While
  // it runs the row shows the same spinner a loading card does; on success
  // the sniffed stream goes straight into the same player every other row
  // uses; on failure the row rewinds into the plain external link this page
  // shipped with before the unified player existed.
  function playEmbed(row) {
    var b = window.__asmrlibDiscover;
    if (!b || !b.resolveEmbed) {
      degradeEmbedRow(row);
      return;
    }
    if (row.getAttribute('aria-busy') === 'true') return;
    row.setAttribute('aria-busy', 'true');
    row.classList.add('is-loading');
    embedSeq += 1;
    var id = 'e' + embedSeq + '-' + Math.floor(Math.random() * 1e6);
    embedPending[id] = row;
    b.resolveEmbed(row.getAttribute('data-url') || '',
      row.getAttribute('data-referer') || '', id);
  }
  function handleEmbedResult(row, payloadJson) {
    row.classList.remove('is-loading');
    row.removeAttribute('aria-busy');
    var res = null;
    try { res = JSON.parse(String(payloadJson == null ? '' : payloadJson)); }
    catch (e) { res = null; }
    var b = window.__asmrlibDiscover;
    var kind = res && res.kind;
    if (!res || !res.ok || !res.url
      || (kind !== 'hls' && kind !== 'direct') || !b || !b.playDirectQueue) {
      degradeEmbedRow(row);
      return;
    }
    var entry = postEntry();
    var nameEl = row.querySelector ? row.querySelector('.src-name') : null;
    var track = [{ u: res.url, l: nameEl ? nameEl.textContent : '', k: kind }];
    if (res.referer) track[0].rr = res.referer;
    // One-entry queue, tapped at 0 — the same launch path a native row
    // takes, so the pinned Referer ("rr") survives to StreamProxy untouched.
    b.playDirectQueue(JSON.stringify(track), 0,
      entry.t || titleEl.textContent || '在线播放',
      entry.c || '', row.getAttribute('data-referer') || '');
  }
  function degradeEmbedRow(row) {
    if (!row || !row.parentNode) return;
    var idx = parseInt(row.getAttribute('data-src-index'), 10);
    var s = lastSources[idx];
    var name = s ? String(s.label || ('源' + (idx + 1))).trim() : '播放源';
    var host = s
      ? String(s.url).replace(/^https?:\/\//, '').split('/')[0] : '';
    var a = document.createElement('a');
    a.className = 'play-enter';
    if (s && s.url) a.href = s.url;
    a.innerHTML = '<span class="src-ico" aria-hidden="true">' + ICON_PLAY + '</span>'
      + '<span class="src-body"><strong><span class="src-name">' + esc(name)
      + '</span></strong><span class="src-host">' + ICON_GLOBE
      + '<span class="src-meta">' + esc(host) + ' · 网页播放</span></span></span>'
      + '<span class="src-chev" aria-hidden="true">›</span>';
    row.replaceWith(a);
  }
  errEl.addEventListener('submit', function (ev) {
    var form = ev.target.closest
      ? ev.target.closest('[data-action="proxy-form"]') : null;
    if (!form) return;
    ev.preventDefault();
    var host = (form.host.value || '').trim();
    var port = parseInt(form.port.value, 10) || 0;
    if (!host || port <= 0 || port > 65535) return;
    var b = window.__asmrlibDiscover;
    if (b && b.setProxy) b.setProxy(host, port);
    errEl.hidden = true;
    load(0);
  });

  // Results land here from the native bridge (background fetch, UI-thread
  // evaluateJavascript). Unknown ids are stale deliveries from an abandoned
  // request or a previous page — ignored. Embed-resolve ids are answered on
  // their row instead of the page-level panels.
  window.__asmrlibDeliver = function (id, payloadJson) {
    var erow = embedPending[id];
    if (erow) {
      delete embedPending[id];
      try { handleEmbedResult(erow, payloadJson); } catch (e) {
        degradeEmbedRow(erow);
      }
      return;
    }
    var entry = pending[id];
    if (!entry) return;
    delete pending[id];
    if (entry.busy) clearTimeout(entry.busy);
    var post = entry.post;
    var res = null;
    try { res = JSON.parse(String(payloadJson == null ? '' : payloadJson)); }
    catch (e) { errorPanel('数据桥返回异常：' + e, post); return; }
    if (!res || !res.ok) { errorPanel(res && res.error, post); return; }
    if (!res.sources || !res.sources.length) {
      errorPanel('未在帖子里解析到播放源。', post);
      return;
    }
    try { render(res, post); }
    catch (e2) {
      errorPanel('播放源显示失败：' + (e2 && e2.message ? e2.message : e2), post);
    }
  };

  function render(data, post) {
    var entry = postEntry();
    lastSources = data.sources || [];
    var title = entry.t || data.title || '在线播放';
    titleEl.textContent = title;
    heroEl.className = 'pl-hero' + (entry.c ? '' : ' no-cover');
    heroEl.innerHTML =
      (entry.c
        ? '<img class="pl-hero-img" data-hero-img src="' + esc(entry.c) + '" alt="" '
          + 'referrerpolicy="no-referrer" decoding="async">'
          + '<div class="pl-hero-scrim" aria-hidden="true"></div>'
        : '')
      + '<div class="pl-hero-body">'
      + '<span class="pl-hero-src">' + esc(originLabel(entry.u)) + '</span>'
      + '<span class="pl-hero-title">' + esc(title) + '</span>'
      + (entry.d
          ? '<span class="pl-hero-meta">' + ICON_CAL + esc(entry.d) + '</span>'
          : '')
      + '</div>';
    heroEl.hidden = false;
    // The playable subset, in resolve order, becomes the player's queue;
    // each in-app button carries only its index into it.
    playQueue = [];
    var queueIdx = {};
    data.sources.forEach(function (s, i) {
      if (s.kind === 'hls' || s.kind === 'direct') {
        queueIdx[i] = playQueue.length;
        playQueue.push({ u: s.url, l: s.label || ('源' + (i + 1)), k: s.kind });
      }
    });
    // A multi-track work (asmr.one) gets the strip: one tap plays the whole
    // queue from the head, and every row's "+" starts it at its own track
    // (Eara's addedToQueue: a state on the row, no navigation).
    var queueStrip = playQueue.length > 1
      ? '<button type="button" class="play-queuebar" data-action="play-queue">'
        + ICON_LIST + '从第 1 轨连续播放'
        + '<span class="pl-qc">' + playQueue.length + ' 轨</span>'
        + '</button>'
      : '';
    // A track's folder is worth a row's width only when the name alone does
    // not identify it: 「01_あさ」 twice in one work needs "wav"/"wav2" beside
    // it, but the usual work repeats the same folder names on every row, and
    // that boilerplate is what used to push the name itself off the line.
    var nameCount = {};
    data.sources.forEach(function (s) {
      var n = String(s.label || '').trim();
      if (n) nameCount[n] = (nameCount[n] || 0) + 1;
    });
    var buttons = data.sources.map(function (s, i) {
      var host = String(s.url).replace(/^https?:\/\//, '').split('/')[0];
      var name = String(s.label || ('源' + (i + 1))).trim();
      // The immediate folder, not the whole path: the deepest segment is the
      // one that differs between two same-named tracks.
      var parts = String(s.group || '').split(' · ');
      var tail = nameCount[name] > 1 ? (parts[parts.length - 1] || '') : '';
      // One row style for every source now. kind === 'hls' | 'direct' means
      // the URL already addresses media bytes; 'embed' is a third-party
      // player *page*, resolved on tap — the bridge loads it in a shielded
      // hidden WebView and hands back the media the running player fetches
      // (resolveEmbed). Both paths end in the same in-app player, so both
      // rows read and behave identically up front; only a failed resolve
      // rewinds a row into the plain link it used to be.
      var media = s.kind === 'hls' || s.kind === 'direct';
      var rowOpen = '<div class="play-enter is-inapp' + (i === 0 ? ' is-primary' : '')
        + '" data-inapp="1"'
        + (media
          ? ' data-url="' + esc(s.url) + '" data-kind="' + esc(s.kind) + '"'
            + ' data-qi="' + queueIdx[i] + '"'
          : ' data-embed="1" data-src-index="' + i + '"'
            + ' data-url="' + esc(s.url) + '"')
        + ' data-referer="' + esc(s.referer || post) + '">';
      var pill = '<button type="button" class="play-inapp" data-action="'
        + (media ? 'play-inapp' : 'embed-play') + '"'
        + ' data-url="' + esc(s.url) + '"'
        + (media ? ' data-kind="' + esc(s.kind) + '"' : '')
        + ' data-referer="' + esc(s.referer || post) + '"'
        + (media ? ' data-qi="' + queueIdx[i] + '"' : '')
        + ' aria-label="在应用内播放">应用内播放</button>';
      // Per-track queue entry: the "+" starts the queue at this track. The
      // referer and index ride the button so the handler needs no closure.
      // An embed row joins only after its resolve — unresolvable URLs do
      // not belong in a queue that walks itself.
      var addQ = media && playQueue.length > 1
        ? '<button type="button" class="play-addq" data-action="queue-add"'
          + ' data-referer="' + esc(s.referer || post) + '"'
          + ' data-qi="' + queueIdx[i] + '"'
          + ' aria-label="从这一轨开始连播" aria-pressed="false">+</button>'
        : '';
      // A media row is a BUTTON, not a link. Its href would be the audio bytes
      // themselves, and a WebView that lands on those throws the page away for
      // Chromium's bare player — title gone, list gone, no way back, which is
      // what a tap on an asmr.one track used to do (and why the new source
      // read as "does not respond"). The row carries the pill's own action,
      // so the whole card is the target a thumb reaches for. Embed rows used
      // to be links by necessity (only a page could play them); they keep no
      // href either — a link would skip the resolve and land the old
      // third-party page right back on top of the unified one.
      return '<div class="play-row' + (i === 0 ? ' is-primary' : '') + '"'
        + ' style="--i:' + i + '">'
        + rowOpen
        + '<span class="src-ico" aria-hidden="true">' + ICON_PLAY + '</span>'
        + '<span class="src-body"><strong><span class="src-name">' + esc(name)
        + '</span>'
        + (i === 0 ? '<span class="src-badge">推荐</span>' : '')
        + '</strong><span class="src-host">' + ICON_GLOBE
        + '<span class="src-meta">' + esc(host)
        + (tail ? ' · ' + esc(tail) : '')
        + ' · 应用内'
        + '</span></span></span>'
        + pill
        + addQ
        + '</div>';
    }).join('');
    dockEl.innerHTML =
      '<div class="pl-sec"><span class="pl-sec-title">播放源</span>'
      + '<span class="pl-sec-count">' + data.sources.length + ' 个</span></div>'
      + queueStrip
      + buttons
      + '<div class="play-foot">'
      + '<div class="play-links">'
      + '<a class="glass-chip play-ext" href="' + esc(post) + '">'
      + ICON_EXT + '原帖页</a>'
      + '<a class="glass-chip play-ext" href="discover.html">'
      + ICON_GRID + '发现页</a>'
      + '</div>'
      + '<p class="play-hint muted">' + ICON_SHIELD
      + '<span>广告与越权跳转由应用内广告盾拦截；返回键回到本页可切换其它源。</span>'
      + '</p>'
      + '</div>';
    dockEl.hidden = false;
    skelEl.hidden = true;
    statusEl.hidden = true;
  }

  function load(attempt) {
    var b = window.__asmrlibDiscover;
    attempt = attempt || 0;
    var post = postEntry().u;
    if (!b) {
      if (attempt < 8) {
        setTimeout(function () { load(attempt + 1); }, 400);
        return;
      }
      errorPanel('数据桥不可用——请用 APK 打开本页', post);
      return;
    }
    if (!post) {
      errorPanel('缺少帖子地址。', '');
      return;
    }
    dockEl.hidden = true;
    errEl.hidden = true;
    skelEl.hidden = false;
    statusEl.textContent = '';
    statusEl.hidden = true;
    seq += 1;
    var id = 'p' + seq + '-' + Math.floor(Math.random() * 1e6);
    var entry = { post: post, busy: 0 };
    pending[id] = entry;
    // The bridge memoizes results, so a cache hit answers in milliseconds —
    // the skeleton is already painted and simply disappears on a fast hit.
    // The bridge times out at 20s; this watchdog only covers a lost delivery.
    setTimeout(function () {
      if (pending[id]) {
        if (entry.busy) clearTimeout(entry.busy);
        delete pending[id];
        errorPanel('连接超时——播放器解析长时间没有响应。', post);
      }
    }, 25000);
    try { b.resolvePost(post, id); }
    catch (e) {
      if (entry.busy) clearTimeout(entry.busy);
      delete pending[id];
      errorPanel('数据桥调用失败：' + e, post);
      return;
    }
  }

  load();
})();
"""

_PLAY_EXTRA_CSS = SKIN + """
/* =====================================================================
   play page
   ===================================================================== */

/* Two layout rules learned from the phone screenshots:
   1. the desktop skin padded body/wrap for its 4-tab dock, which this page
      does not have - neutralising both is where the dead black band under the
      sources came from;
   2. an empty #pl-status still carried its padding, adding a second gap
      between the hero and the source list - :empty removes it outright. */
.play-page { padding-bottom: env(safe-area-inset-bottom); }
.play-page .wrap { padding-top: .9rem;
  padding-bottom: calc(1.4rem + env(safe-area-inset-bottom)); }
@media (max-width: 767px) {
  .play-page { padding-bottom: env(safe-area-inset-bottom); }
  .play-page .wrap { padding-top: .7rem;
    padding-bottom: calc(1.4rem + env(safe-area-inset-bottom)); }
}

/* "直链播放" is a property of the page, not an action, so it sits in the bar
   as a quiet tinted pill rather than a button the user will try to tap. */
.glass-badge {
  flex: 0 0 auto;
  padding: 4px 10px;
  border-radius: var(--r-pill);
  font-size: var(--fs-caption);
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-soft);
  border: 1px solid var(--accent-line);
}

/* ---- cover hero -------------------------------------------------------
   The reference detail page's hero, from its own source: a full-bleed
   fixed-height artwork block (AlbumDetailScreen: heroMinHeight 280dp,
   preferred 360dp) whose foot melts into the page through a colour ramp,
   with the title riding the melt. The old boxed 16/9 stamp read as a
   thumbnail pretending to be a hero; this is the artwork owning the page. */
.pl-hero { position: relative; margin: 0 calc(var(--inset) * -1) var(--sp-2);
  height: clamp(250px, 66vw, 380px);
  overflow: hidden; background: var(--surface-3); }
.pl-hero-img { position: absolute; inset: 0; width: 100%; height: 100%;
  object-fit: cover; }
.pl-hero-scrim { position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(180deg, rgba(10,11,15,0) 34%,
  rgba(10,11,15,.58) 66%, var(--bg) 100%); }
.pl-hero-body { position: absolute; left: 0; right: 0; bottom: 0;
  padding: var(--sp-3) var(--sp-4) var(--sp-4);
  display: flex; flex-direction: column; gap: 6px; }
.pl-hero-title { font-size: 23px; font-weight: 700;
  line-height: 1.32; letter-spacing: -.014em; color: #fff;
  text-shadow: 0 2px 12px rgba(0,0,0,.6); }
.pl-hero-meta { display: inline-flex; align-items: center; gap: 6px;
  color: rgba(255,255,255,.74); font-size: var(--fs-footnote); }
.pl-hero-meta svg { width: 12px; height: 12px; opacity: .85; }
/* The origin rides above the title as a chip: the page must say which source
   it is showing, because the two behave differently (one plays here, the
   other's embeds leave for a page). Black glass with light text — the same
   treatment every other chip that sits on artwork gets — because the scrim
   has barely started this high up the hero and an accent tint on a bright
   cover was unreadable. */
.pl-hero-src { align-self: flex-start; padding: 4px 11px;
  border: 1px solid var(--glass-edge); border-radius: var(--r-pill);
  background: var(--glass-sheen), rgba(10,11,15,.66); color: #fff;
  font-size: var(--fs-caption); font-weight: 700; letter-spacing: .04em; }
/* no cover came over from the card: brand plate + static (non-overlaid) text,
   in theme colours so it works in light mode too. The variant keeps the
   full-bleed foot so both hero shapes share one silhouette. */
.pl-hero.no-cover { background: linear-gradient(150deg, var(--accent-soft), var(--surface) 62%); }
.pl-hero.no-cover .pl-hero-body { position: static; padding: var(--sp-6) var(--sp-4) var(--sp-5); }
.pl-hero.no-cover .pl-hero-title { color: var(--text); text-shadow: none; }
.pl-hero.no-cover .pl-hero-meta { color: var(--text-3); }

/* ---- footer ----------------------------------------------------------- */
.play-foot { margin-top: var(--sp-5); padding-top: var(--sp-4);
  border-top: 1px solid var(--sep); }
.play-links { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.play-hint { display: flex; align-items: flex-start; gap: var(--sp-2);
  margin-top: var(--sp-4); font-size: var(--fs-footnote);
  line-height: var(--lh-relaxed); color: var(--text-2); }
.play-hint svg { flex-shrink: 0; width: 15px; height: 15px; margin-top: 2px;
  color: var(--accent); }

/* ---- play page: tablet -------------------------------------------------
   The phone layout is one column: 16/9 hero, then the source rows, then the
   footer, each at the wrap's full width. On a tablet that is wrong twice
   over - the footer's hint line runs to 1120px (unreadable), and at 800px
   portrait the hero grows taller than the list it is introducing.
   Narrow tablets keep the stack but stop it growing: capped at 760px, one
   larger type scale, wider rows. From 900px the hero moves into its own
   column beside the source list and stays there while the list scrolls, and
   the wrap is capped at 1120px so nothing runs to 1280px.
   The split is gated on :has() rather than on #pl-dock because keying the
   columns on the dock would leave an empty left column through the whole
   resolve; keyed on the hero, the resolve keeps the phone's full-width
   skeleton and the split appears only once there is a hero to put in it.
   :has() degrades to the stacked layout on an older WebView. */
@media (min-width: 768px) and (max-width: 899px) {
  .play-page .wrap { max-width: 760px; margin-inline: auto; padding-inline: var(--sp-6); }
  /* Back to the boxed hero: a centred column has no edge to bleed to, and a
     ramp that melts into the page only reads full-bleed. Static 16/9 art,
     darkening scrim inside the box. */
  .pl-hero { margin: 0 0 var(--sp-5); height: auto; border-radius: var(--r-xl); }
  .pl-hero-img { position: static; width: 100%; height: auto; aspect-ratio: 16 / 9; }
  .pl-hero-scrim { background: linear-gradient(180deg, rgba(0,0,0,0) 34%,
    rgba(0,0,0,.55) 66%, rgba(0,0,0,.9) 100%); }
  .pl-hero-title { font-size: 23px; }
  .pl-hero-meta { font-size: var(--fs-subhead); }
  .pl-hero.no-cover .pl-hero-body { padding: var(--sp-7) var(--sp-5) var(--sp-6); }
  .play-enter { padding: var(--sp-3) var(--sp-4); contain-intrinsic-size: auto 68px; }
  .play-enter strong { font-size: var(--fs-headline); }
  .src-ico { width: 48px; height: 48px; border-radius: 14px; }
  .src-ico svg { width: 21px; height: 21px; }
  .src-host { font-size: var(--fs-subhead); }
  .play-hint { font-size: var(--fs-subhead); }
  /* the hairline follows the text start, which moves with the icon */
  .play-enter + .play-enter::before { left: 76px; }
}
@media (min-width: 900px) {
  .play-page .wrap { max-width: 1120px; margin-inline: auto; padding-inline: var(--sp-6); }
  .play-page .wrap:has(> #pl-hero:not([hidden])) {
    display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
    gap: var(--sp-5) var(--sp-7); align-items: start; }
  .play-page .wrap:has(> #pl-hero:not([hidden])) > .pl-hero {
    grid-column: 1; grid-row: 1 / -1; margin: 0;
    border-radius: var(--r-xl);
    /* sticky, not fixed: it tracks the source list and lets go at its end */
    position: sticky; top: 74px; }
  .pl-hero { height: auto; }
  .pl-hero-img { position: static; width: 100%; height: auto; aspect-ratio: 16 / 9; }
  .pl-hero-scrim { background: linear-gradient(180deg, rgba(0,0,0,0) 34%,
    rgba(0,0,0,.55) 66%, rgba(0,0,0,.9) 100%); }
  /* the dock's own side padding would double up on the grid gap */
  .play-page .wrap:has(> #pl-hero:not([hidden])) > #pl-dock {
    grid-column: 2; grid-row: 1;
    padding: 0 0 calc(var(--sp-1) + env(safe-area-inset-bottom)); }
  .play-page .wrap > #pl-error { grid-column: 1 / -1; }
  .pl-hero-title { font-size: 23px; }
  .pl-hero-meta { font-size: var(--fs-subhead); }
  .play-enter { padding: var(--sp-3) var(--sp-4); contain-intrinsic-size: auto 68px; }
  .play-enter strong { font-size: var(--fs-headline); }
  .src-ico { width: 48px; height: 48px; border-radius: 14px; }
  .src-ico svg { width: 21px; height: 21px; }
  .src-host { font-size: var(--fs-subhead); }
  .play-foot { margin-top: var(--sp-5); }
  .play-hint { font-size: var(--fs-subhead); }
  .play-enter + .play-enter::before { left: 76px; }
}
/* landscape is the short case: buy back vertical room for the source rows */
@media (min-width: 900px) and (orientation: landscape) {
  .play-page .wrap { padding-top: .8rem; }
  .play-page .wrap:has(> #pl-hero:not([hidden])) { gap: var(--sp-4) var(--sp-7); }
}
"""
def build_play_html() -> str:
    """Render the bundled play page (resolver + top-level player jump)."""
    return (
        "<!doctype html>\n"
        f"{PLAY_MARKER}\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, '
        'viewport-fit=cover">\n'
        '<meta http-equiv="Content-Security-Policy" content='
        '"default-src \'none\'; style-src \'unsafe-inline\'; img-src https: data:; '
        "script-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; "
        "object-src 'none'; connect-src 'none'\">\n"
        f"{THEME_COLOR_META}"
        "<title>播放 · ASMR 收藏馆</title>\n"
        f"<style>{_PLAY_EXTRA_CSS}</style>\n"
        "</head>\n<body class='play-page'>\n"
        # Same nav bar as the discover page, with the title pinned visible: this
        # page has no large title to hand over from (the hero carries the real
        # title), so a fading one would just be an empty bar on arrival.
        "<header class='nav play-bar'>"
        "<button type='button' class='icon-btn play-back' onclick='history.back()' "
        "aria-label='返回'>"
        "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.9' "
        "stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
        "<path d='M9.8 3.2 5 8l4.8 4.8'/></svg></button>"
        "<strong class='nav-title nav-title-always play-title' id='pl-title'>在线播放</strong>"
        "<span class='glass-badge'>直链播放</span>"
        "</header>\n"
        "<main class='wrap'>\n"
        "<section class='pl-hero' id='pl-hero' hidden></section>\n"
        "<p class='pl-status' id='pl-status' role='status'>正在解析播放源…</p>\n"
        # Skeleton is its own element rather than a status.innerHTML swap:
        # the parser auto-closes <p> around an inner <div>, and `:empty` on
        # the status paragraph then hides the wrapper, so the skeleton never
        # paints.  Two 60px shimmering rows stand in for the source list.
        "<div class='pl-skel' id='pl-skel' hidden aria-hidden='true'>"
        "<div class='pl-skel-row'></div>"
        "<div class='pl-skel-row'></div></div>\n"
        "<section class='play-dock' id='pl-dock' hidden></section>\n"
        "<section class='glass-panel' id='pl-error' hidden></section>\n"
        "</main>\n"
        "<script type='application/json' id='pl-config'>"
        '{"site":"https://asmrlib.com"}</script>\n'
        f"<script>{_PLAY_JS}</script>\n"
        "</body>\n</html>\n"
    )
def write_play_asset(target: Path) -> Path:
    """Write the play asset (idempotent)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_play_html(), encoding="utf-8")
    return target


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parents[2]
    out = write_discover_asset(repo_root / ASSET_RELPATH)
    out2 = write_play_asset(repo_root / PLAY_ASSET_RELPATH)
    size_kb = out.stat().st_size / 1024
    print(f"discover asset written: {out} ({size_kb:.0f} KB)")
    print(f"play asset written: {out2} ({out2.stat().st_size / 1024:.0f} KB)")
