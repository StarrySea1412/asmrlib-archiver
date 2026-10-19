package com.asmrlib.archiver;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import org.jsoup.Jsoup;
import org.jsoup.nodes.Document;
import org.jsoup.nodes.Element;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Feed source for the bundled 发现页: fetches asmrlib.com listing pages
 * natively (plain HTTP — no WebView, so no CORS wall) and distills them to
 * card JSON for the asset page.
 *
 * ASYNC CONTRACT — the bridge never blocks the page: {@code feed} /
 * {@code resolvePost} return a request id immediately, the network runs on
 * a background executor, and the result is pushed back by evaluating
 * {@code window.__asmrlibDeliver(id, payloadJson)} on the UI thread. A
 * synchronous bridge froze the page's JS for the whole network round-trip
 * (up to the 20s timeout over a slow VPN), which read as "app hangs".
 *
 * CACHE — back navigation fully reloads the asset page (the JS heap does
 * not survive), so the reloaded script re-requests the same URL every
 * time; without a memo each return trip paid another full network
 * round-trip. Successful payloads are therefore cached per URL (feed
 * 10 min, resolve 30 min — listings churn, player sources barely) and
 * identical in-flight requests share one fetch. Only successes are
 * cached so retry-after-error truly retries.
 *
 * Selector rules are a Java port of the desktop parser
 * ({@code parser.py::_site_post_cards}): cards live in {@code .grid},
 * titles prefer headings over img alt over link text, covers come from
 * {@code img[src]/img[data-src]}, dates from {@code time[datetime]}, tags
 * from {@code /tags/} links, pagination from {@code ?page=N} links. When
 * this breaks on a site redesign the page shows its error panel with a
 * raw-site escape hatch.
 */
public final class DiscoverBridge {

    private static final String TAG = "asmrlib-discover";
    static final String SITE = "https://asmrlib.com";

    /**
     * The Eara-style second source ({@link AsmrOneSource}): the feed URL is
     * a virtual {@code asmrone://search/<encoded keyword>/<page>} the page
     * composes, and a work URL is {@code https://asmr.one/work/<id>}.
     */
    static final String ASMRONE_SCHEME = AsmrOneSource.SCHEME;

    private static final String UA =
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
            + "Chrome/120.0.0.0 Mobile Safari/537.36";

    private final ShieldRules shield;
    private final WebView webView;
    private final ExecutorService io = Executors.newFixedThreadPool(3);
    private final AtomicLong seq = new AtomicLong();
    private final Handler main = new Handler(Looper.getMainLooper());
    // Lives as long as the WebView binding — i.e. across the page reloads
    // that every back navigation triggers.
    private static final long FEED_TTL_MS = 10 * 60_000L;    // listing pages
    private static final long RESOLVE_TTL_MS = 30 * 60_000L; // player sources
    // Embed sniffs resolve to CDN URLs that often carry signed, expiring
    // query tokens — a stale hit would play as 「解析成功但播不出」. Short
    // enough to stay fresh, long enough to cover back-and-forth inside one
    // work page.
    private static final long EMBED_TTL_MS = 5 * 60_000L;
    private final Map<String, CacheEntry> cache =
        new LinkedHashMap<String, CacheEntry>(16, 0.75f, true) {
            @Override protected boolean removeEldestEntry(Map.Entry<String, CacheEntry> e) {
                return size() > 24;
            }
        };
    /** Request ids waiting on one shared in-flight fetch, keyed like cache. */
    private final Map<String, ArrayList<String>> inFlight = new ConcurrentHashMap<>();

    private static final class CacheEntry {
        final String payload;
        final long at;
        CacheEntry(String payload, long at) { this.payload = payload; this.at = at; }
    }

    public DiscoverBridge(WebView webView, ShieldRules shield) {
        this.webView = webView;
        this.shield = shield;
    }

    /** Fire-and-forget feed fetch; result arrives via {@code __asmrlibDeliver}. */
    @JavascriptInterface
    public String feed(final String url, final String requestId) {
        return enqueue("feed", url, requestId, null);
    }

    /** Fire-and-forget post resolve; result arrives via {@code __asmrlibDeliver}. */
    @JavascriptInterface
    public String resolvePost(final String url, final String requestId) {
        return enqueue("resolve", url, requestId, null);
    }

    /**
     * Resolve one third-party player page ({@code kind=embed}: the BI/UP/AB
     * buttons) to the media URL it actually streams, so it can play in the
     * app's own player like every other source. The page hands back the
     * embed URL it got from {@code resolvePost}; the sniff result arrives
     * via {@code __asmrlibDeliver} as {@code {ok,url,kind,referer}} — the
     * page then launches the same {@code playDirectQueue} path as a native
     * row.
     */
    @JavascriptInterface
    public String resolveEmbed(final String embedUrl, final String pageUrl,
            final String requestId) {
        return enqueue("embed", embedUrl, requestId, pageUrl);
    }

    /**
     * Configure the app-wide HTTP proxy. The site the app reads from is
     * TLS-blocked from this network; the proxy (a phone-routable proxy on
     * the user's computer, or a phone VPN with "allow LAN" turned on) is
     * the only way {@code feed} / {@code resolvePost} can complete a TLS
     * handshake.
     *
     * <p>Passing an empty host or zero port clears the configuration.
     * Returns the host:port string actually persisted (or empty when
     * cleared), so the page can show it back in the error panel.
     */
    @JavascriptInterface
    public String setProxy(final String host, final int port) {
        ProxyConfig.set(webView != null ? webView.getContext() : null, host, port);
        String h = ProxyConfig.host(webView != null ? webView.getContext() : null);
        int p = ProxyConfig.port(webView != null ? webView.getContext() : null);
        return h.isEmpty() || p <= 0 ? "" : h + ":" + p;
    }

    /** Read the currently configured proxy, for the error panel to show. */
    @JavascriptInterface
    public String currentProxy() {
        Context ctx = webView != null ? webView.getContext() : null;
        String h = ProxyConfig.host(ctx);
        int p = ProxyConfig.port(ctx);
        return h.isEmpty() || p <= 0 ? "" : h + ":" + p;
    }

    /**
     * Open a resolved media stream in the app's own player — the in-app
     * twin of the top-frame jump the page does for embed sources. Only
     * meaningful for {@code kind} hls/direct; the page is trusted to call it
     * with a URL the resolve step itself produced (the same site-scoped
     * trust {@code resolvePost} already relies on).
     *
     * <p>{@code referer} is the page the media was resolved from — player
     * hosts commonly answer only requests that carry it — and {@code cover}
     * is the work's poster for the player backdrop. Runs on the calling
     * (JS bridge) thread only to read extras; the launch itself is posted
     * to the main thread, as startActivity from a binder thread is fine but
     * the toast below is not.
     */
    @JavascriptInterface
    public void playDirect(final String url, final String kind, final String title,
            final String cover, final String referer) {
        // A single track is a one-entry queue: one code path for both.
        try {
            org.json.JSONArray one = new org.json.JSONArray();
            JSONObject e = new JSONObject();
            e.put("u", url == null ? "" : url.trim());
            e.put("k", kind == null ? "" : kind.trim());
            one.put(e);
            playDirectQueue(one.toString(), 0, title, cover, referer);
        } catch (Exception ignored) {
            // malformed single track: same silence as the guarded old path
        }
    }

    /** Nothing good comes from an intent carrying a 100 KB queue. */
    private static final int QUEUE_CAP = 200;

    /**
     * Open the in-app player on a track list (Eara's continuous playback):
     * the page hands the playable sources it already resolved, plus the
     * index the user tapped.  Embed rows never reach here — the page filters
     * to hls/direct, the same trust {@code resolvePost} already relies on.
     * The queue rides the intent only when there is something to walk.
     */
    @JavascriptInterface
    public void playDirectQueue(final String queueJson, final int index,
            final String title, final String cover, final String referer) {
        org.json.JSONArray queue;
        try {
            queue = new org.json.JSONArray(queueJson == null ? "[]" : queueJson);
        } catch (Exception e) {
            return;
        }
        if (queue.length() == 0) return;
        int at = Math.max(0, Math.min(index, queue.length() - 1));
        JSONObject first = queue.optJSONObject(at);
        if (first == null) return;
        String target = first.optString("u", "").trim();
        String k = first.optString("k", "direct").trim();
        if (target.isEmpty() || (!"hls".equals(k) && !"direct".equals(k))) return;
        Context ctx = webView != null ? webView.getContext() : null;
        if (ctx == null) return;
        android.content.Intent play = new android.content.Intent(ctx, PlayerActivity.class);
        play.putExtra(PlayerActivity.EXTRA_REMOTE_URL, target);
        play.putExtra(PlayerActivity.EXTRA_REMOTE_KIND, "hls".equals(k) ? "hls" : "direct");
        play.putExtra(PlayerActivity.EXTRA_TITLE, title == null ? "" : title.trim());
        play.putExtra(PlayerActivity.EXTRA_REMOTE_COVER, cover == null ? "" : cover.trim());
        // An entry may pin its Referer ("rr"): the embed sniffer hands back
        // the exact header the real player sent, and running it through
        // refererFor's heuristic would replace a proven value with a guess.
        String firstPinned = first.optString("rr", "").trim();
        play.putExtra(PlayerActivity.EXTRA_REMOTE_REFERER,
            firstPinned.isEmpty() ? refererFor(target, referer) : firstPinned);
        if (queue.length() > 1) {
            org.json.JSONArray capped = new org.json.JSONArray();
            for (int i = 0; i < queue.length() && i < QUEUE_CAP; i++) {
                JSONObject entry = queue.optJSONObject(i);
                if (entry == null) continue;
                // Each track carries the Referer its OWN host wants: a queue
                // can span CDNs, and handing track four track one's page
                // identity is the cross-origin rejection that plays as
                // 「第一轨能播、后面全挂」.
                try {
                    String entryPinned = entry.optString("rr", "").trim();
                    entry.put("r", entryPinned.isEmpty()
                        ? refererFor(entry.optString("u", "").trim(), referer)
                        : entryPinned);
                } catch (JSONException ignored) {
                    // advanceQueue falls back to the tapped track's referer.
                }
                capped.put(entry);
            }
            play.putExtra(PlayerActivity.EXTRA_REMOTE_QUEUE, capped.toString());
            play.putExtra(PlayerActivity.EXTRA_REMOTE_INDEX, at);
        }
        ctx.startActivity(play);
    }

    /**
     * The Referer the stream should carry. The page offers the post URL, but
     * player CDNs gate on their OWN domain — an asmrlib.com Referer on a
     * bysetayico CDN request is exactly the cross-origin rejection that
     * plays as 「封面能看、视频播不出」 (the cover CDN does not check, the
     * video CDN does). When the offered page is cross-origin to the media,
     * the safest Referer is the media URL's own origin; a same-origin page
     * keeps its full URL.
     */
    static String refererFor(String mediaUrl, String pageUrl) {
        String page = pageUrl == null ? "" : pageUrl.trim();
        String mediaHost = hostOf(mediaUrl);
        if (mediaHost.isEmpty()) return page;
        if (!page.isEmpty() && mediaHost.equalsIgnoreCase(hostOf(page))) return page;
        try {
            java.net.URI u = java.net.URI.create(mediaUrl);
            String scheme = u.getScheme() == null ? "https" : u.getScheme();
            return scheme + "://" + u.getRawAuthority() + "/";
        } catch (Exception e) {
            return page;
        }
    }

    // ------------------------------------------------------------------
    // Embed sniff — the unified-playback path for kind=embed sources.
    // ------------------------------------------------------------------

    /** Hard ceiling on one sniff run, including page load and player init. */
    private static final long EMBED_SNIFF_TIMEOUT_MS = 25_000L;
    /**
     * When only a bare media file has been seen, keep the page alive this
     * much longer for a manifest to show up — the HLS master is what the
     * player prefers, but some sources stream a plain .mp4 and would pay
     * the full timeout for a manifest that never comes.
     */
    private static final long DIRECT_GRACE_MS = 3_000L;
    /** Main-frame redirect hops one sniff will follow (loop guard). */
    private static final int EMBED_SNIFF_MAX_HOPS = 4;

    /** What one sniff run found. Written on the UI thread only. */
    private static final class SniffResult {
        volatile String url = "";
        volatile String kind = "";
        volatile String referer = "";
    }

    /** One embed sniff at a time — a second hidden WebView buys nothing. */
    private final Object sniffLock = new Object();

    /**
     * Blocking embed resolution (background thread only). Returns the
     * play.html source payload for the player page: the media URL the page
     * streams, its kind, and the Referer the page itself sends for it —
     * the ground truth no heuristic should second-guess (see the {@code rr}
     * entry field in {@link #playDirectQueue}).
     */
    private String resolveEmbedSync(String embedUrl, String pageUrl) {
        JSONObject out = new JSONObject();
        String target = embedUrl == null ? "" : embedUrl.trim();
        String from = pageUrl == null ? "" : pageUrl.trim();
        try {
            String kind = kindOf(target);
            if (!"embed".equals(kind)) {
                // Already media bytes (resolvePost classifies by extension):
                // no page to load, hand it over untouched.
                if (isPublicHttpUrl(target)) {
                    return out.put("ok", true)
                        .put("url", target)
                        .put("kind", kind)
                        .put("referer", refererFor(target, from))
                        .toString();
                }
                return out.put("ok", false).put("error", "该播放源地址无法解析").toString();
            }
            if (!isPublicHttpUrl(target) || (shield != null && shield.isAdUrl(target))) {
                Log.i(TAG, "embed resolve refused: " + target);
                return out.put("ok", false).put("error", "该播放源页面不可解析").toString();
            }
            SniffResult hit = sniffEmbed(target, from);
            if (hit.url.isEmpty()) {
                Log.i(TAG, "embed sniff timed out: " + target);
                return out.put("ok", false)
                    .put("error", "未能从该源页面检测到媒体流").toString();
            }
            Log.i(TAG, "embed sniff ok: " + hit.kind + " " + hit.url);
            return out.put("ok", true)
                .put("url", hit.url)
                .put("kind", hit.kind)
                .put("referer", hit.referer)
                .toString();
        } catch (Exception e) {
            Log.i(TAG, "embed sniff failed: " + e);
            try {
                return out.put("ok", false)
                    .put("error", e.getClass().getSimpleName()
                        + ": " + String.valueOf(e.getMessage()))
                    .toString();
            } catch (Exception ignored) {
                return "{\"ok\":false,\"error\":\"embed sniff failed\"}";
            }
        }
    }

    /**
     * Load the player page in a never-attached WebView and watch the traffic
     * the page's own player produces. Parsing the page or its scripts is how
     * this breaks on every host redesign; watching what the running player
     * fetches is not. The ad shield sits on every request, so what survives
     * to be classified is the same traffic the user's own WebView session
     * would carry.
     *
     * <p>Sound: the hosts gate their stream behind a play gesture — with the
     * gate alone the hidden page never loads a byte (proven live against
     * bysetayico and abyssplayer). So the WebView's gesture gate is opened
     * and the nudge below clicks the player's own play affordance, while a
     * document-level patch forces every media element muted before its first
     * {@code play()} and again on a re-sweep. The page may autoplay; it can
     * never be heard — an ASMR app with a phantom voice is the worst bug
     * this class could ship.
     */
    private SniffResult sniffEmbed(final String embedUrl, final String pageUrl)
            throws InterruptedException {
        final Context ctx = webView != null ? webView.getContext() : null;
        if (ctx == null) throw new IllegalStateException("no context for embed sniff");
        synchronized (sniffLock) {
            final SniffResult found = new SniffResult();
            final java.util.concurrent.CountDownLatch done =
                new java.util.concurrent.CountDownLatch(1);
            final Runnable[] timeout = new Runnable[1];
            final Runnable[] grace = new Runnable[1];
            final WebView[] holder = new WebView[1];
            final android.view.ViewGroup[] parent = new android.view.ViewGroup[1];
            // Per-run counter: top-frame hops this sniff has followed.
            final int[] hopCount = new int[1];
            // Re-fired play nudge; one instance serves every schedule so the
            // cleanup can cancel all of them at once.
            final Runnable nudge = new Runnable() {
                @Override public void run() {
                    WebView v = holder[0];
                    if (v == null) return;
                    muteAndNudge(v);
                    tapCenter(v);
                }
            };
            main.post(new Runnable() {
                @Override
                public void run() {
                    WebView wv = new WebView(ctx);
                    holder[0] = wv;
                    android.webkit.WebSettings st = wv.getSettings();
                    st.setJavaScriptEnabled(true);
                    st.setDomStorageEnabled(true);
                    st.setBlockNetworkImage(true);
                    st.setMediaPlaybackRequiresUserGesture(false);
                    wv.setBackgroundColor(0x00000000);
                    // Attached with a real size, parked off-screen and fully
                    // transparent: the player iframe inside only answers a
                    // REAL touch (user activation — synthetic el.click() is
                    // ignored across frames), so the view must exist in the
                    // hierarchy to receive one.
                    if (ctx instanceof android.app.Activity) {
                        android.view.ViewGroup content = ((android.app.Activity) ctx)
                            .findViewById(android.R.id.content);
                        if (content != null) {
                            parent[0] = content;
                            android.widget.FrameLayout.LayoutParams lp =
                                new android.widget.FrameLayout.LayoutParams(720, 405);
                            wv.setLayoutParams(lp);
                            wv.setTranslationX(-9999);
                            wv.setTranslationY(-9999);
                            wv.setAlpha(0f);
                            content.addView(wv);
                        }
                    }
                    android.webkit.WebViewClient client = new android.webkit.WebViewClient() {
                        @Override
                        public void onPageStarted(WebView view, String url,
                                android.graphics.Bitmap favicon) {
                            super.onPageStarted(view, url, favicon);
                            // Before the player's own scripts: the prototype
                            // patch must predate their first play() call.
                            muteAndNudge(view);
                        }

                        @Override
                        public void onPageFinished(WebView view, String url) {
                            super.onPageFinished(view, url);
                            muteAndNudge(view);
                            // Players that build their UI after DOMContentLoaded
                            // need a poke once it exists.
                            main.postDelayed(nudge, 2_500L);
                            main.postDelayed(nudge, 6_000L);
                            main.postDelayed(nudge, 10_000L);
                        }

                        @Override
                        public android.webkit.WebResourceResponse shouldInterceptRequest(
                                WebView view, android.webkit.WebResourceRequest request) {
                            if (shield != null && request != null) {
                                // The shell embeds the real player as a
                                // cross-origin subframe (bysetayico /e/{id} →
                                // rotated host /{route}/{id}); admit that one
                                // handover before the shield answers, or the
                                // iframe renders the refusal page and no byte
                                // ever flows. Same rule the visible shell was
                                // meant to use — trustEmbeddedPlayerUrl had
                                // no production caller until this sniffer.
                                if (!request.isForMainFrame()) {
                                    String u = request.getUrl() == null
                                        ? "" : request.getUrl().toString();
                                    shield.trustEmbeddedPlayerUrl(embedUrl, u);
                                }
                                android.webkit.WebResourceResponse blocked =
                                    shield.intercept(view, request);
                                if (blocked != null) return blocked;
                            }
                            if (request == null) return null;
                            android.net.Uri uri = request.getUrl();
                            String url = uri == null ? "" : uri.toString();
                            String kind = kindOf(url);
                            if ("embed".equals(kind) || !isPublicHttpUrl(url)) return null;
                            boolean settled = false;
                            synchronized (found) {
                                if ("hls".equals(kind)) {
                                    found.url = url;
                                    found.kind = kind;
                                    found.referer = observedReferer(request, embedUrl);
                                    settled = true;
                                } else if (found.url.isEmpty()) {
                                    // First bare file: note it and hold the
                                    // grace window open for a manifest.
                                    found.url = url;
                                    found.kind = kind;
                                    found.referer = observedReferer(request, embedUrl);
                                    if (grace[0] == null) {
                                        grace[0] = new Runnable() {
                                            @Override public void run() { done.countDown(); }
                                        };
                                        main.postDelayed(grace[0], DIRECT_GRACE_MS);
                                    }
                                }
                            }
                            // The manifest is the best answer there is — the
                            // latch opens now; timeout and grace just make
                            // sure it opens anyway. The stream address is in
                            // hand the moment the first media request is
                            // classified, so the player's speaker time ends
                            // here: onPause freezes its media before the
                            // first sample ever renders.
                            if (settled || !found.url.isEmpty()) {
                                main.post(new Runnable() {
                                    @Override public void run() {
                                        WebView w = holder[0];
                                        if (w != null) w.onPause();
                                    }
                                });
                            }
                            if (settled) done.countDown();
                            return null;
                        }

                        @Override
                        public boolean shouldOverrideUrlLoading(WebView view,
                                android.webkit.WebResourceRequest request) {
                            if (!request.isForMainFrame()) return false;
                            String url = request.getUrl() == null
                                ? "" : request.getUrl().toString();
                            String scheme = url.startsWith("https:") ? "https"
                                : url.startsWith("http:") ? "http" : "";
                            if (scheme.isEmpty()) return true; // intent:/javascript:/…
                            if (shield != null && shield.shouldBlockNavigation(url)) {
                                return true;
                            }
                            // Top-frame hops (player hosts 302 between their
                            // own domains) are followed, up to a small cap.
                            return ++hopCount[0] > EMBED_SNIFF_MAX_HOPS;
                        }
                    };
                    wv.setWebViewClient(client);
                    java.util.Map<String, String> headers = new java.util.HashMap<>();
                    if (!pageUrl.isEmpty()) headers.put("Referer", pageUrl);
                    wv.loadUrl(embedUrl, headers);
                    Log.i(TAG, "embed sniff: loading " + embedUrl);
                    timeout[0] = new Runnable() {
                        @Override public void run() { done.countDown(); }
                    };
                    main.postDelayed(timeout[0], EMBED_SNIFF_TIMEOUT_MS);
                }
            });
            try {
                done.await(EMBED_SNIFF_TIMEOUT_MS + DIRECT_GRACE_MS + 2_000L,
                    java.util.concurrent.TimeUnit.MILLISECONDS);
            } finally {
                final WebView wv = holder[0];
                final android.view.ViewGroup wvParent = parent[0];
                main.post(new Runnable() {
                    @Override
                    public void run() {
                        if (timeout[0] != null) main.removeCallbacks(timeout[0]);
                        if (grace[0] != null) main.removeCallbacks(grace[0]);
                        main.removeCallbacks(nudge);
                        if (wv != null) {
                            wv.onPause();
                            wv.stopLoading();
                            if (wvParent != null) wvParent.removeView(wv);
                            wv.destroy();
                        }
                    }
                });
            }
            return found;
        }
    }

    /**
     * Mute everything now and forever, then poke the player's own play
     * affordance. The prototype patch must predate the page's first
     * {@code play()} — it is fired from {@code onPageStarted} — and every
     * later sweep re-mutes whatever exists, because players create their
     * media elements long after document start. The nudge clicks the big
     * play control the common skins ship (jw/vjs/dplayer/art/prism) before
     * falling back to the element itself. It runs on the shell document;
     * the cross-origin player iframe is reached by admitting its document
     * through {@code trustEmbeddedPlayerUrl} in the request interceptor, not
     * by reaching across frames — embed players autoplay once unblocked.
     */
    private void muteAndNudge(WebView view) {
        view.evaluateJavascript(
            "(function(){var P=HTMLMediaElement.prototype,o=P.play;"
                + "P.play=function(){try{this.muted=true;this.volume=0;}"
                + "catch(e){}return o.apply(this,arguments);};"
                + "document.querySelectorAll('video,audio').forEach("
                + "function(m){m.muted=true;m.volume=0;});"
                + "var s=['.jw-display-icon-container','.vjs-big-play-button',"
                + "'.dplayer-button','.art-control-play','.plyr__control--overlaid',"
                + "'.prism-big-play-btn','video'];"
                + "for(var i=0;i<s.length;i++){var el=document.querySelector(s[i]);"
                + "if(el){el.click();break;}}})()", null);
    }

    /**
     * One real touch at the view's center. Cross-origin iframes ignore
     * synthetic {@code el.click()} — Chromium only grants user activation to
     * a genuine input event — and the embed player's whole surface is its
     * play button, so a dispatched touch at the center is what starts it.
     * The view is parked off-screen and transparent; only its input pipeline
     * is being used.
     */
    private void tapCenter(WebView view) {
        int w = view.getWidth();
        int h = view.getHeight();
        if (w <= 0 || h <= 0) return;
        long now = android.os.SystemClock.uptimeMillis();
        android.view.MotionEvent down = android.view.MotionEvent.obtain(
            now, now, android.view.MotionEvent.ACTION_DOWN, w / 2f, h / 2f, 0);
        view.dispatchTouchEvent(down);
        down.recycle();
        android.view.MotionEvent up = android.view.MotionEvent.obtain(
            now, now + 60, android.view.MotionEvent.ACTION_UP, w / 2f, h / 2f, 0);
        view.dispatchTouchEvent(up);
        up.recycle();
    }

    /**
     * The Referer header the player page itself attached to the media
     * request — the exact value the real player proved sufficient. Handed
     * back verbatim so playback can replay it instead of re-deriving one.
     */
    private static String observedReferer(android.webkit.WebResourceRequest request,
            String fallback) {
        if (request != null) {
            Map<String, String> headers = request.getRequestHeaders();
            if (headers != null) {
                for (Map.Entry<String, String> e : headers.entrySet()) {
                    if ("referer".equalsIgnoreCase(e.getKey())) {
                        String v = e.getValue();
                        return v == null || v.trim().isEmpty() ? fallback : v.trim();
                    }
                }
            }
        }
        return fallback;
    }

    /**
     * Whether the URL addresses public web media: http(s) only, never a
     * loopback/private host — a sniffer watching a hidden page must not
     * mistake local traffic for the stream.
     */
    static boolean isPublicHttpUrl(String raw) {
        String url = raw == null ? "" : raw.trim();
        if (!url.startsWith("http://") && !url.startsWith("https://")) return false;
        String host = hostOf(url);
        if (host.isEmpty() || host.equals("localhost") || host.equals("::1")
            || host.equals("[::1]")) return false;
        if (host.startsWith("127.") || host.startsWith("10.") || host.startsWith("192.168.")
            || host.startsWith("169.254.") || host.startsWith("fc") || host.startsWith("fd")
            || host.startsWith("fe80:")) return false;
        if (host.startsWith("172.")) {
            String[] parts = host.split("\\.");
            if (parts.length == 4) {
                try {
                    int second = Integer.parseInt(parts[1]);
                    if (second >= 16 && second <= 31) return false;
                } catch (NumberFormatException ignored) { }
            }
        }
        return true;
    }


    /**
     * Pull-to-refresh support: purges the memoized entries for every page of
     * the currently loaded feed chain, unblocks any waiters on them, then
     * fetches the first page fresh. The page chains the remaining pages via
     * normal {@code feed} calls, which now miss the cache and hit the wire.
     *
     * @param urlsJson JSON array of the loaded feed URLs, base page first;
     *                 the first entry is the one actually re-fetched here.
     */
    @JavascriptInterface
    public String refresh(final String urlsJson, final String requestId) {
        String first = null;
        try {
            JSONArray pages = new JSONArray(urlsJson == null ? "[]" : urlsJson);
            for (int i = 0; i < pages.length(); i++) {
                String u = pages.optString(i, "").trim();
                if (u.isEmpty()) continue;
                if (first == null) first = u;
                String key = "feed|" + u;
                synchronized (cache) { cache.remove(key); }
                ArrayList<String> waiting;
                synchronized (inFlight) { waiting = inFlight.remove(key); }
                if (waiting != null) {
                    // Waiters were about to receive the stale copy we just
                    // purged; unblock them with an error so their watchdog
                    // never fires.
                    for (String waiter : waiting) {
                        deliver(waiter, "{\"ok\":false,\"error\":\"已被手动刷新取代\"}");
                    }
                }
            }
        } catch (Exception ignored) {
            // malformed urlsJson: fall through to a plain first-page fetch
        }
        return enqueue("feed", first == null ? SITE + "/" : first, requestId);
    }

    private String enqueue(final String kind, final String url, final String requestId) {
        return enqueue(kind, url, requestId, null);
    }

    private String enqueue(final String kind, final String url, final String requestId,
            final String extra) {
        final String id = requestId == null || requestId.trim().isEmpty()
            ? "r" + seq.incrementAndGet()
            : requestId.trim();
        final String key = kind + "|" + (url == null ? "" : url.trim());
        final long ttl = kind.equals("feed") ? FEED_TTL_MS
            : kind.equals("embed") ? EMBED_TTL_MS : RESOLVE_TTL_MS;
        synchronized (cache) {
            CacheEntry hit = cache.get(key);
            if (hit != null && System.currentTimeMillis() - hit.at < ttl) {
                Log.i(TAG, kind + " cache-hit: " + key);
                deliver(id, hit.payload);
                return id;
            }
        }
        synchronized (inFlight) {
            ArrayList<String> waiting = inFlight.get(key);
            if (waiting != null) {
                synchronized (waiting) { waiting.add(id); }
                return id; // one shared fetch serves every identical request
            }
            ArrayList<String> fresh = new ArrayList<>();
            fresh.add(id);
            inFlight.put(key, fresh);
        }
        io.execute(new Runnable() {
            @Override
            public void run() {
                String payload;
                try {
                    payload = kind.equals("feed") ? feedSync(url)
                        : kind.equals("embed") ? resolveEmbedSync(url, extra)
                        : resolveSync(url);
                } catch (Exception e) {
                    Log.i(TAG, kind + " failed: " + e);
                    try {
                        payload = new JSONObject()
                            .put("ok", false)
                            .put("error", e.getClass().getSimpleName()
                                + ": " + String.valueOf(e.getMessage()))
                            .toString();
                    } catch (Exception ignored) {
                        payload = "{\"ok\":false,\"error\":\"request failed\"}";
                    }
                }
                boolean ok = false;
                try { ok = new JSONObject(payload).optBoolean("ok", false); }
                catch (Exception ignored) { // unparseable is never cached
                }
                if (ok) {
                    synchronized (cache) {
                        cache.put(key, new CacheEntry(payload, System.currentTimeMillis()));
                    }
                }
                ArrayList<String> done;
                synchronized (inFlight) { done = inFlight.remove(key); }
                if (done != null) {
                    for (String waiter : done) deliver(waiter, payload);
                } else {
                    deliver(id, payload);
                }
            }
        });
        return id;
    }

    /** Push one result back into the page (UI thread; page parses payload). */
    private void deliver(final String id, final String payload) {
        main.post(new Runnable() {
            @Override
            public void run() {
                try {
                    webView.evaluateJavascript(
                        "window.__asmrlibDeliver && window.__asmrlibDeliver("
                            + JSONObject.quote(id) + ","
                            + JSONObject.quote(payload) + ");",
                        null);
                } catch (Exception ignored) {
                    // page navigated away mid-flight — nothing to deliver to
                }
            }
        });
    }

    /** Blocking fetch + parse of one listing page (background thread only). */
    private String feedSync(String url) {
        JSONObject out = new JSONObject();
        try {
            String target = url == null ? "" : url.trim();
            if (target.startsWith(ASMRONE_SCHEME)) {
                return AsmrOneSource.feed(target);
            }
            if (!target.startsWith(SITE)) {
                return out.put("ok", false).put("error", "只允许站点列表页").toString();
            }
            Document doc = Jsoup.connect(target)
                .userAgent(UA)
                .referrer(SITE + "/")
                .timeout(20000)
                .get();
            Element root = doc.selectFirst("#main");
            if (root == null) root = doc.selectFirst("main");
            if (root == null) root = doc.body();
            if (root == null) {
                return out.put("ok", false).put("error", "页面为空").toString();
            }

            List<Element> cardRoots = new ArrayList<>();
            Element grid = root.selectFirst(".grid");
            if (grid != null) {
                cardRoots.addAll(grid.children());
            }
            if (cardRoots.isEmpty()) {
                // Layout change fallback: one synthetic root per post link's
                // nearest block ancestor (same as parser.py).
                LinkedHashSet<Element> seen = new LinkedHashSet<>();
                for (Element link : root.select("a[href*=\"/posts/\"]")) {
                    Element block = link;
                    for (Element parent : link.parents()) {
                        String name = parent.tagName();
                        if (name.equals("div") || name.equals("article")
                            || name.equals("li") || name.equals("section")) {
                            block = parent;
                            break;
                        }
                    }
                    seen.add(block);
                }
                cardRoots.addAll(seen);
            }

            JSONArray cards = new JSONArray();
            JSONObject tags = new JSONObject();
            HashSet<String> seenUrls = new HashSet<>();
            for (Element cardRoot : cardRoots) {
                JSONObject card = parseCard(target, cardRoot, tags);
                if (card != null && seenUrls.add(card.optString("u"))) {
                    cards.put(card);
                }
            }

            int page = pageOf(target);
            int next = 0;
            for (Element link : root.select("a[href]")) {
                String abs = absUrl(target, link.attr("href"));
                if (pageOf(abs) == page + 1) {
                    next = page + 1;
                    break;
                }
            }

            out.put("ok", true);
            out.put("page", page);
            out.put("next", next);
            out.put("cards", cards);
            out.put("tags", tags);
            Log.i(TAG, "feed ok: page=" + page + " cards=" + cards.length());
            return out.toString();
        } catch (Exception e) {
            Log.i(TAG, "feed failed: " + e);
            try {
                return out.put("ok", false)
                    .put("error", e.getClass().getSimpleName() + ": " + String.valueOf(e.getMessage()))
                    .toString();
            } catch (Exception ignored) {
                return "{\"ok\":false,\"error\":\"feed failed\"}";
            }
        }
    }

    /** Mirrors parser.py::_site_post_cards field rules for one card root. */
    private JSONObject parseCard(String baseUrl, Element cardRoot, JSONObject tags)
        throws JSONException {
        String postUrl = "";
        String title = "";
        String cover = "";
        String date = "";

        for (Element link : cardRoot.select("a[href]")) {
            String abs = absUrl(baseUrl, link.attr("href"));
            if (abs.isEmpty()) continue;
            boolean isPost = abs.contains("/posts/");
            if (isPost && postUrl.isEmpty()) postUrl = abs;
            if (abs.contains("/tags/")) {
                String slug = tagSlug(abs);
                if (!slug.isEmpty()) {
                    tags.put(slug, tags.optInt(slug, 0) + 1);
                }
            }
            if (isPost) {
                Element heading = link.selectFirst("h1,h2,h3");
                if (heading != null) {
                    if (title.isEmpty()) title = clean(heading.text());
                } else if (title.isEmpty()) {
                    String text = clean(link.text());
                    if (!text.isEmpty()) title = text;
                }
            }
        }
        if (title.isEmpty()) {
            Element heading = cardRoot.selectFirst("h1,h2,h3");
            if (heading != null) title = clean(heading.text());
        }
        Element img = cardRoot.selectFirst("img[src],img[data-src]");
        if (img != null) {
            String raw = img.hasAttr("src") ? img.attr("src") : img.attr("data-src");
            cover = absUrl(baseUrl, raw.trim());
            if (title.isEmpty()) title = clean(img.attr("alt"));
        }
        Element time = cardRoot.selectFirst("time[datetime],time");
        if (time != null) {
            date = time.hasAttr("datetime") ? time.attr("datetime") : clean(time.text());
        }

        if (postUrl.isEmpty()) return null;
        // Same ad-host gate as the desktop parser: a cover served by a
        // blocked host degrades to the plate, never loads.
        if (!cover.isEmpty() && shield != null && shield.isAdUrl(cover)) cover = "";
        if (title.isEmpty()) {
            title = postUrl.substring(postUrl.lastIndexOf('/') + 1);
        }
        // Tag chips for this card (deduped, site slug text).
        JSONArray slugs = new JSONArray();
        HashSet<String> seenSlugs = new HashSet<>();
        for (Element link : cardRoot.select("a[href*=\"/tags/\"]")) {
            String slug = tagSlug(absUrl(baseUrl, link.attr("href")));
            if (!slug.isEmpty() && seenSlugs.add(slug.toLowerCase())) {
                slugs.put(slug);
            }
        }
        JSONObject card = new JSONObject();
        card.put("u", postUrl);
        card.put("t", title);
        card.put("c", cover);
        card.put("d", date);
        card.put("g", slugs);
        return card;
    }

    /** Media data-type values the desktop parser accepts (parser.py). */
    private static final java.util.Set<String> MEDIA_DATA_TYPES = new HashSet<>(
        java.util.Arrays.asList("audio", "file", "hls", "iframe", "media", "video"));

    /**
     * Resolve a post page to bare player embeds — the phone twin of the
     * desktop player_resolver: {@code #players [data-url]} entries
     * (BI/AB/UP buttons), document order keeps the primary source (BI)
     * first, mirroring the desktop preference.
     */
    private String resolveSync(String url) {
        JSONObject out = new JSONObject();
        try {
            String target = url == null ? "" : url.trim();
            String workId = AsmrOneSource.workId(target);
            if (!workId.isEmpty()) {
                return AsmrOneSource.resolve(target, workId);
            }
            if (!target.startsWith(SITE) || !target.contains("/posts/")) {
                return out.put("ok", false).put("error", "只允许站点帖子页").toString();
            }
            Document doc = Jsoup.connect(target)
                .userAgent(UA)
                .referrer(SITE + "/")
                .timeout(20000)
                .get();
            Element root = doc.selectFirst("#main");
            if (root == null) root = doc.selectFirst("main");
            if (root == null) root = doc.body();
            if (root == null) {
                return out.put("ok", false).put("error", "页面为空").toString();
            }
            String title = "";
            Element heading = root.selectFirst("h1");
            if (heading != null) title = clean(heading.text());
            if (title.isEmpty()) {
                Element og = doc.selectFirst("meta[property='og:title']");
                if (og != null) title = clean(og.attr("content"));
            }

            JSONArray sources = new JSONArray();
            HashSet<String> seen = new HashSet<>();
            for (Element node : root.select("#players [data-url]")) {
                String raw = node.attr("data-url").trim();
                String type = node.attr("data-type").toLowerCase().trim();
                if (raw.isEmpty() || (!MEDIA_DATA_TYPES.contains(type)
                    && !raw.contains("/e/") && !raw.contains("/l3y/"))) {
                    continue;
                }
                String abs = absUrl(target, raw);
                if (abs.isEmpty() || seen.contains(abs)) continue;
                if (shield != null && shield.isAdUrl(abs)) continue;
                seen.add(abs);
                String label = clean(node.text());
                if (label.isEmpty()) label = clean(node.attr("data-server"));
                if (label.isEmpty()) label = hostOf(abs);
                sources.put(sourceJson(label, abs, kindOf(abs), target));
            }
            if (sources.length() == 0) {
                // Layout-change fallback: any anchor under #players pointing
                // off-site (same idea as parser's _looks_like_media path).
                for (Element link : root.select("#players a[href]")) {
                    String abs = absUrl(target, link.attr("href"));
                    if (abs.isEmpty() || abs.startsWith(SITE)) continue;
                    if (shield != null && shield.isAdUrl(abs)) continue;
                    if (seen.add(abs)) {
                        sources.put(sourceJson(clean(link.text()), abs,
                            kindOf(abs), target));
                    }
                }
            }

            out.put("ok", true);
            out.put("url", target);
            out.put("title", title);
            out.put("sources", sources);
            Log.i(TAG, "resolve ok: sources=" + sources.length());
            return out.toString();
        } catch (Exception e) {
            Log.i(TAG, "resolve failed: " + e);
            try {
                return out.put("ok", false)
                    .put("error", e.getClass().getSimpleName() + ": " + String.valueOf(e.getMessage()))
                    .toString();
            } catch (Exception ignored) {
                return "{\"ok\":false,\"error\":\"resolve failed\"}";
            }
        }
    }

    /**
     * What a resolved source is: {@code hls} — an .m3u8 manifest the app can
     * stream itself (through StreamProxy); {@code direct} — a bare media file
     * the app can stream or hand to DownloadManager; {@code embed} — a
     * third-party player page that must be navigated to as before. The kinds
     * decide which affordance play.html renders: in-app playback is only
     * offered for the first two, because only their URLs address media bytes.
     */
    private static final String[] DIRECT_EXTS = {
        ".mp4", ".webm", ".m4v", ".mkv", ".mov",
        ".m4a", ".mp3", ".aac", ".ogg", ".opus", ".wav", ".flac",
    };

    private static String kindOf(String url) {
        String path = url == null ? "" : url.toLowerCase();
        int query = path.indexOf('?');
        if (query >= 0) path = path.substring(0, query);
        int fragment = path.indexOf('#');
        if (fragment >= 0) path = path.substring(0, fragment);
        if (path.endsWith(".m3u8")) return "hls";
        for (String ext : DIRECT_EXTS) {
            if (path.endsWith(ext)) return "direct";
        }
        return "embed";
    }

    private static JSONObject sourceJson(String label, String url, String kind,
            String referer) throws JSONException {
        JSONObject source = new JSONObject();
        source.put("label", label);
        source.put("url", url);
        source.put("kind", kind);
        source.put("referer", referer == null ? "" : referer);
        return source;
    }

    private static String hostOf(String url) {
        try {
            String host = java.net.URI.create(url).getHost();
            return host == null ? "播放源" : host;
        } catch (Exception e) {
            return "播放源";
        }
    }

    private static String clean(String text) {
        return text == null ? "" : text.replaceAll("\\s+", " ").trim();
    }

    private static String absUrl(String baseUrl, String href) {
        String raw = href == null ? "" : href.trim();
        if (raw.isEmpty()) return "";
        try {
            java.net.URI base = java.net.URI.create(baseUrl);
            return base.resolve(raw).toString();
        } catch (Exception e) {
            return "";
        }
    }

    private static int pageOf(String url) {
        if (url == null) return 1;
        int idx = url.indexOf("page=");
        if (idx < 0) return 1;
        StringBuilder digits = new StringBuilder();
        for (int i = idx + 5; i < url.length(); i++) {
            char c = url.charAt(i);
            if (c >= '0' && c <= '9') digits.append(c);
            else break;
        }
        try {
            return Math.max(1, Integer.parseInt(digits.toString()));
        } catch (Exception e) {
            return 1;
        }
    }

    private static String tagSlug(String url) {
        int idx = url.indexOf("/tags/");
        if (idx < 0) return "";
        String slug = url.substring(idx + "/tags/".length());
        int end = slug.indexOf('?');
        if (end >= 0) slug = slug.substring(0, end);
        end = slug.indexOf('#');
        if (end >= 0) slug = slug.substring(0, end);
        while (slug.endsWith("/")) slug = slug.substring(0, slug.length() - 1);
        return slug;
    }
}
