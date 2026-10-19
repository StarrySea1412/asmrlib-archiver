package com.asmrlib.archiver;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.Dialog;
import android.app.DownloadManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.ColorFilter;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.graphics.RectF;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.LayerDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.os.Environment;
import android.Manifest;
import android.text.TextUtils;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Thin WebView shell for the local ASMR library.
 *
 * Launch flow: probe {@code <server>/api/network-check} (the phone-side
 * network test) — if the server is unreachable the user gets the setup gate
 * with concrete hints instead of a white screen. Once connected, the whole
 * library UI runs inside a WebView whose URL policy replays the desktop
 * shield (assets/blocklist.json), so 在线播放 in-app keeps the
 * 广告/弹窗/越权跳转 interception the desktop player provides.
 */
public class MainActivity extends Activity {

    private static final String TAG = "asmrlib-apk";
    private static final String PREFS = "asmrlib";
    private static final String UA_SUFFIX = " asmrlib-android/1.0";
    // Bound once per WebView lifetime for the bundled discover page
    // (file:///android_asset/) — see the field comment on
    // discoverBridgeActive for why it is never removed.
    private static final String BRIDGE = "__asmrlibDiscover";
    // ---- download center --------------------------------------------------
    // A Toast is not a download. Every enqueue is retained here so the panel
    // can show real progress, a real failure reason, a retry and an open
    // action, and so the state survives an app restart.
    private static final String KEY_DOWNLOADS = "download_tasks";
    private static final String NOTIFICATION_CHANNEL_ID = "downloads";
    private static final int NOTIFICATION_PERMISSION_REQUEST = 1505;
    private static final int NOTIFICATION_ID_BASE = 51000;
    private static final String STATE_RUNNING = "running";
    private static final String STATE_SUCCESS = "success";
    private static final String STATE_FAILED = "failed";
    /**
     * A task that finished and then lost its bytes. It is never taken from the
     * stored {@code state}: it is recomputed from the filesystem, so a row can
     * never keep claiming 已完成 for a file that is gone.
     */
    private static final String STATE_LOST = "lost";
    // Two kinds of task share this store. "direct" is a DownloadManager
    // hand-off whose state comes from a real cursor row; "hls" is an in-app
    // segment-aware job owned by this activity, which DownloadManager knows
    // nothing about.
    private static final String KIND_DIRECT = "direct";
    private static final String KIND_HLS = "hls";
    static final String OFFLINE_DIR = "offline";
    /**
     * The bundled play page. It is the only surface that knows what the media
     * <em>is</em> — the discover page hands it the tapped card's title in its
     * URL hash — so it is where a human name for the saved file comes from.
     */
    private static final String PLAY_PAGE = "file:///android_asset/play.html";
    /** Longest stem kept from a title; the rest is noise in a file list. */
    private static final int MAX_STEM_CHARS = 60;
    // Poll only while the panel is on screen; the completion receiver covers
    // the rest without waking the shell every tick.
    private static final long POLL_INTERVAL_MS = 800L;
    // ---- tablet adaptation ------------------------------------------------
    // The native chrome was drawn for a phone: on a 1280dp-wide tablet the
    // download card and every task row stretched edge to edge, and the
    // floating chips stayed phone-sized and stranded in the corner. These are
    // the only two numbers the layout helpers read — no scattered magic
    // sizes.
    /** Shortest-edge width from which a window is treated as a tablet. */
    private static final int TABLET_MIN_WIDTH_DP = 600;
    /** Widest a column of content (card, gate form, splash) may become. */
    private static final int CONTENT_MAX_WIDTH_DP = 560;
    /** Side margin the chrome keeps, and the minimum on a tablet. */
    private static final int CHROME_MARGIN_DP = 14;
    /** Bounds for the task list so it neither overflows nor strands. */
    private static final int LIST_MIN_HEIGHT_DP = 160;
    private static final int LIST_MAX_HEIGHT_DP = 720;

    /**
     * The URL of the document that is on screen right now.
     *
     * <p>Volatile, and read from both the UI thread and the Chromium worker
     * thread. The loading curtain and the hop rule both anchor on it. Written
     * from doUpdateVisitedHistory (main thread, "the commit is the one moment
     * the on-screen document changes") — and never from a callback that may
     * not touch the WebView.
     */
    private volatile String currentFrameUrl = "";

    private ShieldRules shield;
    private WebView webView;
    // Android download hand-off is deliberately native-only. Remote player JS
    // never receives a save(url) bridge, so a compromised player cannot turn
    // the app into an arbitrary downloader. We surface only a URL seen by the
    // shielded WebView and revalidate it immediately before DownloadManager.
    private volatile String verifiedDownloadUrl = "";
    // The .m3u8 counterpart, under the same rule: only a URL the shielded
    // WebView actually requested is ever offered to the downloader, and
    // HlsDownloader re-checks every dependency — and every redirect hop —
    // against the policy before it fetches it.
    private volatile String verifiedHlsUrl = "";

    // ---- download center state -------------------------------------------
    /**
     * The 保存媒体 action. It lives <em>inside</em> the download-center card,
     * not on the decor: a second floating chip cost a permanent strip of the
     * page for a button that is meaningless until a media URL has been sniffed,
     * while the entry that opens the card is useful all session. One floating
     * control, and the action sits where its own task list already is.
     *
     * It is a child of {@link #downloadCard}, so it is nulled out whenever the
     * card is discarded (see onConfigurationChanged) — re-adding a view that
     * still has a parent throws.
     */
    private Button downloadAction;
    /**
     * The shell's only floating control. Tapping it does NOT save — it expands
     * a small menu holding both actions (保存当前视频 / 下载中心). Two floating
     * buttons was rejected outright: it read as duplicate chrome rather than a
     * menu, and the two icons were indistinguishable at a glance.
     */
    private Button downloadEntry;
    /** The prominent 应用内播放 banner, shown while a media URL is sniffed. */
    private Button inappBanner;
    /** The expanded menu above {@link #downloadEntry}; built on first expand. */
    private LinearLayout fabMenu;
    /** Full-screen tap target behind the expanded menu: tap-outside dismisses. */
    private View fabScrim;
    private boolean fabMenuOpen;
    private Button fabSaveItem;
    /** The menu's 应用内播放 row; same readiness state as {@link #fabSaveItem}. */
    private Button fabPlayItem;
    /** Where the save entry is currently pinned. Top/Bottom x Left/Right. */
    private int entryGravity = Gravity.BOTTOM | Gravity.END;
    /** Live offsets the user has dragged away from the pin position. */
    private int entryDragX, entryDragY;
    /** Touch slop: drags shorter than this fall through to the click listener. */
    private int entryTouchSlop;
    /** Down-time / position so we can tell a click from a drag in onTouch. */
    private long entryDownAt;
    private float entryDownX, entryDownY;
    private boolean entryDragging;
    /**
     * The progress ring around {@link #downloadEntry}, created once and reused
     * across restyles so its fraction survives a density change or a navigation
     * instead of snapping back to empty.
     */
    private ProgressArcDrawable downloadArc;
    private FrameLayout downloadPanel;
    private LinearLayout downloadCard;
    private LinearLayout downloadList;
    private ScrollView downloadScroll;
    /** The panel header's real offline-directory figure. */
    private TextView downloadStorage;
    private boolean panelVisible;
    private boolean panelWasVisibleBeforeFullscreen;
    private boolean entryWasVisibleBeforeFullscreen;
    /**
     * One-shot guard for the "media is ready to save" Toast. Without it every
     * segment request would re-announce the same discovery — an HLS player
     * touches the manifest repeatedly, so the message has to be tied to the
     * navigation, not to the request.
     */
    private boolean mediaReadyAnnounced;
    /**
     * The post title, remembered from the play page's URL hash. The save is
     * triggered later — on the upstream player, whose own title is useless — so
     * the name has to be captured while the page that knows it is still up.
     */
    private String mediaContextTitle = "";
    /** The play page card's cover URL, for the in-app player's backdrop. */
    private String playContextCover = "";
    private String lastPlayPage = "";

    private static String htmlEscape(String value) {
        return (value == null ? "" : value).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\"", "&quot;").replace("'", "&#39;");
    }
    private final List<JSONObject> downloadTasks = new ArrayList<JSONObject>();
    private BroadcastReceiver downloadReceiver;
    // ---- in-app HLS jobs --------------------------------------------------
    // The registry is process-wide (see HlsJobs) rather than a field here:
    // DownloadsActivity renders the same stored list and can delete a task
    // whose worker was started from this Activity. A map owned by MainActivity
    // left that worker unreachable — it kept writing the .parts directory the
    // user had just deleted straight back onto the disk.
    private int hlsSeq;

    /**
     * Kept running while the panel is open <em>or</em> a task is still moving,
     * so the entry icon's progress arc keeps ticking with the panel closed.
     * With nothing running it stops itself rather than waking the shell every
     * 800 ms for a list nobody is looking at.
     */
    private final Runnable pollRunnable = new Runnable() {
        @Override
        public void run() {
            refreshDownloadStates();
            updateDownloadEntryProgress();
            if (panelVisible) renderDownloadPanel();
            if (panelVisible || isAnythingRunning()) {
                mainHandler.postDelayed(this, POLL_INTERVAL_MS);
            }
        }
    };

    // ---- fullscreen video -------------------------------------------------
    // A WebView never honours the <video> fullscreen button by itself: it
    // hands the view to the host through WebChromeClient.onShowCustomView.
    // Without this pair the button is simply dead, which is the "无法全屏"
    // report. The custom view is hosted on the decor FrameLayout so the page
    // itself never has to change.
    private View customView;
    private WebChromeClient.CustomViewCallback customViewCallback;
    private int originalOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED;
    private int originalSystemUi;
    // ---- loading screen ---------------------------------------------------
    // First paint of the library page is not instant, and a WebView shows a
    // blank white surface until then. A same-colour splash plus a real
    // progress bar (fed by onProgressChanged) covers that window.
    private static final long MIN_SPLASH_MS = 420L;
    /** An external page's own first paint is only trustworthy some time
     *  after its load event — its JS keeps restyling for a beat. Revealing
     *  earlier than this was the 「白屏闪了一下」: the curtain lifted onto a
     *  document that was still white underneath. */
    private static final long EXTERNAL_REVEAL_SETTLE_MS = 600L;
    /** The curtain's hard cap: a page whose load event never fires (a hung
     *  host, a stalled ad script) releases the curtain itself instead of
     *  keeping the loading page up forever. */
    private static final long SPLASH_HARD_CAP_MS = 8000L;
    private FrameLayout splash;
    private LinearLayout splashBox;
    /** The curtain's status line — 「正在连接收藏馆」 vs 「正在打开播放器」. */
    private TextView splashSub;
    private ProgressBar progressBar;
    private long splashShownAt;
    private int splashGeneration;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    // The discover data bridge is bound once per WebView lifetime and never
    // removed: addJavascriptInterface cannot retro-inject into an already
    // restored history page, so dropping it for remote pages would
    // permanently break back-navigation to the bundled asset page. The
    // surface is read-only (parses public asmrlib.com listings; no device
    // data crosses the bridge), which keeps the exposure acceptable.
    private boolean discoverBridgeActive;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // Apply the persisted HTTP proxy to the JVM before any Jsoup fetch
        // runs.  The site is TLS-blocked from this network; without a proxy
        // the very first feed() call fails the handshake and the error
        // panel comes up blind.  See ProxyConfig for the why.
        ProxyConfig.apply(this);
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        // No PC-library any more: the APK is standalone, so the shield gets no
        // PC origin to trust and every rule comes from the bundled list.
        shield = ShieldRules.fromAssets(getAssets(), "");
        // Restart recovery: replay the persisted tasks against DownloadManager
        // so a download that was running when the app died reappears with its
        // real progress, and an id the system no longer knows about is dropped.
        loadDownloadTasks();
        registerDownloadReceiver();
        refreshDownloadStates();
        if (savedInstanceState == null) {
            // Launch straight into the online 发现页 — no PC questions up
            // front. If the site is unreachable the page shows its own
            // styled error screen with a retry.
            openDiscover();
        } else {
            setupWebView();
            ensureDiscoverBridge();
            webView.restoreState(savedInstanceState);
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        if (webView != null) webView.saveState(outState);
    }

    // ------------------------------------------------------------ discover mode

    /** The bundled 发现页: our UI, live site data, no PC. */
    private void openDiscover() {
        setupWebView();
        // Bind AFTER a renderer exists: injecting into a WebView that has
        // never rendered gets dropped by Chromium. about:blank boots the
        // renderer; the binding then survives the asset navigation.
        webView.loadUrl("about:blank");
        ensureDiscoverBridge();
        Log.i(TAG, "discover: bridge bound, loading asset page");
        webView.loadUrl("file:///android_asset/discover.html");
    }

    /** Add the data bridge once — never re-add while the binding lives. */
    private void ensureDiscoverBridge() {
        if (webView == null || discoverBridgeActive) return;
        webView.addJavascriptInterface(new DiscoverBridge(webView, shield), BRIDGE);
        discoverBridgeActive = true;
    }

    // ------------------------------------------------------------------ webview

    @SuppressLint("SetJavaScriptEnabled")
    private void setupWebView() {
        if (webView != null) {
            // Already built (e.g. we are leaving the gate, which had swapped
            // the content view to native controls). Re-attach THAT WebView —
            // creating a second one would load the library off-screen while
            // the gate stayed visible, and it would also drop the discover →
            // play history and orphan the bound data bridge.
            setContentView(webView);
            attachLoadingChrome();
            attachDownloadEntry();
            showSplash();
            return;
        }
        webView = new WebView(this);
        // The discover page draws its own pull-to-refresh indicator; the
        // native overscroll glow would glow underneath it at the top edge.
        webView.setOverScrollMode(View.OVER_SCROLL_NEVER);
        // The WebView's own background is product dark. It paints wherever
        // the page has not (the first frames of a navigation, the moment a
        // splash lifts) — left white it was exactly the 「直接白屏」 flash,
        // and no splash timing could cover every gap.
        webView.setBackgroundColor(Theme.bg(this));
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setLoadsImagesAutomatically(true);
        s.setUserAgentString(s.getUserAgentString() + UA_SUFFIX);
        s.setSupportMultipleWindows(false); // popups navigate in-view → shield sees them
        // Debug shell: allow chrome://inspect so WebView layout/paint issues
        // are debuggable from the desktop (no-op on release-shaped builds).
        WebView.setWebContentsDebuggingEnabled(true);
        CookieManager.getInstance().setAcceptCookie(true);

        webView.setWebViewClient(new ShieldWebViewClient());
        webView.setWebChromeClient(new MediaChromeClient());
        webView.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String ua, String cd,
                                        String mime, long len) {
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "无法下载：" + url, Toast.LENGTH_SHORT).show();
                }
            }
        });
        setContentView(webView);
        // Must be attached *after* setContentView: setContentView replaces the
        // decor content and would otherwise remove the freshly built splash.
        attachLoadingChrome();
        attachDownloadEntry();
    }

    // ------------------------------------------------------- loading screen

    /** Build (once) the splash + progress overlay that sits above the page. */
    private void attachLoadingChrome() {
        FrameLayout decor = (FrameLayout) getWindow().getDecorView();
        if (splash != null) {
            if (splash.getParent() == null) {
                decor.addView(splash, new FrameLayout.LayoutParams(
                        FrameLayout.LayoutParams.MATCH_PARENT,
                        FrameLayout.LayoutParams.MATCH_PARENT));
            }
            return;
        }
        MainActivity ctx = this;
        splash = new FrameLayout(ctx);
        // Matches the page's --bg for the current system theme, so the hand-off
        // is invisible.  Pinned to Theme.BG it was only right in dark mode: on a
        // phone in light mode the splash was dark, the window behind it was
        // dark, and then a light page appeared - a two-step flash on every cold
        // start, which is the thing this overlay exists to prevent.
        final boolean night = Theme.isNight(ctx);
        splash.setBackgroundColor(Theme.bg(ctx));
        LinearLayout box = new LinearLayout(ctx);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        // Capped like the rest of the chrome: a tablet must not stretch the
        // splash across the full width either.
        splashBox = box;
        splash.addView(box, splashBoxLayout());

        TextView mark = new TextView(ctx);
        mark.setText("ASMR 收藏馆");
        mark.setTextColor(Theme.text(ctx));
        mark.setTextSize(18f);
        mark.setGravity(Gravity.CENTER);
        box.addView(mark);

        TextView sub = new TextView(ctx);
        sub.setText("正在连接收藏馆…");
        sub.setTextColor(Theme.text2(ctx));
        sub.setTextSize(13f);
        sub.setGravity(Gravity.CENTER);
        sub.setPadding(0, dp(8), 0, dp(14));
        box.addView(sub);
        splashSub = sub;

        progressBar = new ProgressBar(ctx, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        progressBar.setProgress(0);
        // Tinted from our palette rather than left to the platform: the theme
        // parent differs between the light and dark resources, so an untinted
        // bar silently changes colour with the system theme.
        progressBar.setProgressTintList(
                android.content.res.ColorStateList.valueOf(Theme.accent(MainActivity.this)));
        progressBar.setProgressBackgroundTintList(android.content.res.ColorStateList.valueOf(
                Theme.accent(MainActivity.this, night ? 56 : 44)));
        LinearLayout.LayoutParams pLp = new LinearLayout.LayoutParams(dp(160), dp(3));
        pLp.gravity = Gravity.CENTER_HORIZONTAL;
        box.addView(progressBar, pLp);

        decor.addView(splash, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));
    }

    private void showSplash() {
        showSplash(null);
    }

    /** Raise the curtain with a specific status line (跳转播放器时即盖). */
    private void showSplash(String message) {
        if (splash == null || customView != null) return;
        // A WebView can report 100% for the renderer warm-up (about:blank)
        // after the real asset navigation has already started.  Advance a
        // generation here so that stale completion callbacks cannot lift the
        // real page's loading surface early.
        splashGeneration++;
        splashShownAt = SystemClock.elapsedRealtime();
        splash.animate().cancel();
        splash.setAlpha(1f);
        splash.setVisibility(View.VISIBLE);
        if (splashSub != null) {
            if (message != null && !message.isEmpty()) {
                splashSub.setText(message);
            } else if (splash.getVisibility() != View.VISIBLE) {
                // Fresh curtain: the generic line. A re-show for a navigation
                // already in flight (onPageStarted right after the hop) must
                // not overwrite the hop's own message with it.
                splashSub.setText("正在连接收藏馆…");
            }
        }
        if (progressBar != null) progressBar.setProgress(0);
        splash.bringToFront();
    }

    private void hideSplash() {
        if (splash == null || splash.getVisibility() != View.VISIBLE) return;
        long shownFor = SystemClock.elapsedRealtime() - splashShownAt;
        long remaining = MIN_SPLASH_MS - shownFor;
        if (remaining > 0) {
            final int generation = splashGeneration;
            mainHandler.postDelayed(new Runnable() {
                @Override
                public void run() {
                    if (generation == splashGeneration) hideSplashNow();
                }
            }, remaining);
            return;
        }
        hideSplashNow();
    }

    private void hideSplashNow() {
        if (splash == null || splash.getVisibility() != View.VISIBLE) return;
        splash.animate().alpha(0f).setDuration(220).withEndAction(new Runnable() {
            @Override
            public void run() {
                if (splash != null) splash.setVisibility(View.GONE);
            }
        }).start();
    }

    // ---------------------------------------------------- verified download

    private static boolean isPrivateOrLoopbackHost(String host) {
        String h = host == null ? "" : host.trim().toLowerCase();
        if (h.isEmpty() || h.equals("localhost") || h.equals("::1") || h.equals("[::1]")) return true;
        if (h.startsWith("127.") || h.startsWith("10.") || h.startsWith("192.168.")
                || h.startsWith("169.254.") || h.startsWith("fc") || h.startsWith("fd")
                || h.startsWith("fe80:" )) return true;
        if (h.startsWith("172.")) {
            String[] parts = h.split("\\.");
            if (parts.length == 4) {
                try {
                    int second = Integer.parseInt(parts[1]);
                    if (second >= 16 && second <= 31) return true;
                } catch (NumberFormatException ignored) { }
            }
        }
        return false;
    }

    private static boolean isDirectDownloadUrl(String raw) {
        if (raw == null || raw.isEmpty()) return false;
        Uri uri = Uri.parse(raw);
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase();
        if (!"http".equals(scheme) && !"https".equals(scheme)) return false;
        if (isPrivateOrLoopbackHost(uri.getHost())) return false;
        String path = uri.getPath() == null ? "" : uri.getPath().toLowerCase();
        // Direct media files only. HLS is no longer refused here — it is owned
        // by HlsDownloader, the segment-aware resumable task DownloadManager
        // cannot be (handed an .m3u8 it would save only the playlist and then
        // report success). See isHlsUrl below.
        return path.endsWith(".mp4") || path.endsWith(".webm") || path.endsWith(".m4a")
            || path.endsWith(".mp3") || path.endsWith(".aac") || path.endsWith(".ogg")
            || path.endsWith(".wav") || path.endsWith(".flac");
    }

    /**
     * An HLS playlist URL. Playlists are never handed to DownloadManager —
     * storing the manifest text and reporting success is a fake success — but
     * they are no longer refused either: they are routed to the in-app
     * segment-aware job. The query string is stripped before the check.
     */
    private static boolean isHlsUrl(String raw) {
        if (raw == null || raw.isEmpty()) return false;
        Uri uri = Uri.parse(raw);
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.US);
        if (!"http".equals(scheme) && !"https".equals(scheme)) return false;
        String path = uri.getPath() == null ? "" : uri.getPath().toLowerCase(Locale.US);
        return path.endsWith(".m3u8");
    }

    /**
     * Whether the shield has handed us anything the downloader can actually
     * save. The single predicate behind both the panel button's enabled state
     * and the entry icon's "media ready" highlight, so the two can never
     * disagree about whether a save is possible.
     */
    private boolean hasSavableMedia() {
        return isHlsUrl(verifiedHlsUrl) || isDirectDownloadUrl(verifiedDownloadUrl);
    }

    /** Called by the request interceptor for media actually observed in WebView. */
    private void captureDirectMedia(final String url) {
        if (isHlsUrl(url)) {
            // The player's manifest. HlsDownloader resolves the variant and
            // fetches every segment itself, so only the URL has to be kept.
            verifiedHlsUrl = url;
        } else if (isDirectDownloadUrl(url)) {
            verifiedDownloadUrl = url;
        } else {
            return;
        }
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                // The small floating control is the save action now, not a
                // shortcut to the list. As soon as the shield has seen a real
                // media URL, reveal it and hide the separate list shortcut so
                // the page presents one obvious next action.
                styleDownloadEntry(downloadEntry, true);
                if (downloadEntry != null && customView == null) {
                    downloadEntry.setVisibility(View.VISIBLE);
                }
                refreshFabMenuState();
                refreshDownloadActionState();
                if (customView == null && !mediaReadyAnnounced) {
                    mediaReadyAnnounced = true;
                    Toast.makeText(MainActivity.this,
                            "检测到可保存的媒体，点右下角图标保存", Toast.LENGTH_SHORT).show();
                }
                showInAppBanner();
            }
        });
    }

    /**
     * The prominent in-app playback entry. The FAB menu's third cell was too
     * deep — a user who just wants Eara's player never found it and judged
     * the feature missing. The moment the shield sniffs a media URL, this
     * banner appears bottom-centre with the accent fill: one obvious tap
     * between 「the video is playing in a webpage」 and 「it plays in OUR
     * player」. Hidden again the moment the capture resets.
     */
    private void showInAppBanner() {
        FrameLayout decor = (FrameLayout) getWindow().getDecorView();
        if (inappBanner == null) {
            Button b = new Button(this);
            b.setText("▶  在应用内播放");
            b.setAllCaps(false);
            b.setTextSize(14f);
            b.setTextColor(Color.WHITE);
            b.setStateListAnimator(null);
            b.setMinHeight(0);
            b.setMinimumHeight(0);
            b.setMinWidth(0);
            b.setMinimumWidth(0);
            b.setPadding(dp(22), 0, dp(22), 0);
            android.graphics.drawable.GradientDrawable pill =
                    new android.graphics.drawable.GradientDrawable(
                            android.graphics.drawable.GradientDrawable.Orientation.TL_BR,
                            new int[]{Theme.ACCENT, Theme.ACCENT_ALT});
            pill.setCornerRadius(dp(999));
            b.setBackground(PressFeedback.on(pill));
            b.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) {
                    launchInAppPlayback();
                }
            });
            inappBanner = b;
            FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                    FrameLayout.LayoutParams.WRAP_CONTENT, dp(46),
                    Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL);
            lp.bottomMargin = dp(28);
            decor.addView(b, lp);
        }
        inappBanner.setVisibility(View.VISIBLE);
        inappBanner.bringToFront();
    }

    private void hideInAppBanner() {
        if (inappBanner != null) inappBanner.setVisibility(View.GONE);
    }

    /**
     * Whether a task for this exact URL is still in flight. Only a running
     * record blocks a re-save: a failed or lost one must stay retryable, or the
     * only way to recover a broken download would be to clear app data.
     */
    private boolean hasLiveTaskFor(String url) {
        if (url == null || url.isEmpty()) return false;
        for (JSONObject task : downloadTasks) {
            if (url.equals(task.optString("url", ""))
                    && STATE_RUNNING.equals(task.optString("state", STATE_RUNNING))) {
                return true;
            }
        }
        return false;
    }

    /**
     * Remember what the media is, while the page that knows it is still up.
     *
     * <p>The discover page hands the tapped card to the play page as a JSON
     * hash ({@code {u,t,c,d}} — the same payload the page itself decodes), and
     * the play page is the last place a human title exists. By the time the
     * shield sniffs a media URL the WebView is on the upstream player, whose
     * title belongs to somebody else's site, so the name has to be taken here.
     */
    private void captureMediaContext(String url) {
        if (url == null || !url.startsWith(PLAY_PAGE)) return;
        int hash = url.indexOf('#');
        if (hash < 0 || hash == url.length() - 1) return;
        String raw = url.substring(hash + 1);
        JSONObject entry = parseHashPayload(raw);
        if (entry == null) {
            Log.i(TAG, "play page hash is not a card payload, keeping the old title");
            return;
        }
        String title = entry.optString("t", "").trim();
        if (!title.isEmpty()) mediaContextTitle = title;
        // The card's cover rides the same payload: the one artwork the
        // in-app player can put behind a stream that has no poster frame.
        String cover = entry.optString("c", "").trim();
        if (!cover.isEmpty()) playContextCover = cover;
    }

    /** The hash arrives percent-encoded; a plain-url hash is simply not ours. */
    private static JSONObject parseHashPayload(String raw) {
        try {
            return new JSONObject(raw);
        } catch (JSONException ignored) {
            // fall through to the decoded form
        }
        try {
            return new JSONObject(Uri.decode(raw));
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * A human name for what is being saved, best source first: the remembered
     * post title, then the live page's title, then the post URL's slug, then
     * the media URL's own file name.
     *
     * <p>The old name was the CDN's — {@code index-v1-a1-1789481775707.ts} — so
     * the download centre and the player both listed a timestamp for a file the
     * user had chosen by its title. The extension is kept out of the stem so
     * the caller can pick the one that matches what it actually wrote.
     */
    private String suggestedStem(String mediaUrl) {
        String title = mediaContextTitle.trim();
        if (title.isEmpty() && webView != null) {
            String pageTitle = webView.getTitle();
            if (pageTitle != null && !isGenericPageTitle(pageTitle)) title = pageTitle.trim();
        }
        if (title.isEmpty()) {
            title = lastSegmentStem(webView == null ? "" : webView.getUrl());
        }
        if (title.isEmpty()) title = lastSegmentStem(mediaUrl);
        return sanitizeStem(title);
    }

    /**
     * Titles of the shell's own pages, which name the app rather than the media
     * and would turn every save into "ASMR 收藏馆 (3).ts".
     */
    private static boolean isGenericPageTitle(String title) {
        String t = title == null ? "" : title.trim();
        return t.isEmpty() || t.contains("ASMR 收藏馆") || t.equals("在线播放") || t.equals("播放");
    }

    /** The last path segment with its extension dropped — the post slug. */
    private static String lastSegmentStem(String url) {
        if (url == null || url.isEmpty()) return "";
        try {
            String segment = Uri.parse(url).getLastPathSegment();
            if (segment == null) return "";
            int dot = segment.lastIndexOf('.');
            return dot > 0 ? segment.substring(0, dot) : segment;
        } catch (Exception e) {
            return "";
        }
    }

    /**
     * Make a title safe to use as a file name without making it unreadable.
     *
     * <p>CJK and emoji are kept — they are the point of the rename, and modern
     * Android storage is UTF-8 end to end. What goes is everything that is
     * either illegal on a filesystem or pure noise: the {@code #} tag markers
     * the feed's titles are full of, path separators and the other reserved
     * characters, control whitespace, and a trailing dot or space, which
     * Windows rejects outright.
     */
    private static String sanitizeStem(String raw) {
        String s = raw == null ? "" : raw;
        s = s.replace('#', ' ');
        s = s.replaceAll("[\\\\/:*?\"<>|\\r\\n\\t]+", " ");
        s = s.replaceAll("\\s+", " ").trim();
        while (s.endsWith(".") || s.endsWith(" ")) s = s.substring(0, s.length() - 1);
        if (s.length() > MAX_STEM_CHARS) s = s.substring(0, MAX_STEM_CHARS).trim();
        return s;
    }

    /** The media URL's own extension, or .mp4 when it does not declare one. */
    private static String extensionOf(String raw) {
        String path = null;
        try {
            path = Uri.parse(raw == null ? "" : raw).getPath();
        } catch (Exception ignored) {
            // treated as extension-less below
        }
        if (path == null) return ".mp4";
        int dot = path.lastIndexOf('.');
        if (dot < 0 || dot == path.length() - 1) return ".mp4";
        String ext = path.substring(dot).toLowerCase(Locale.US);
        // A path like /stream.php must not become a ".php" video.
        return ext.matches("\\.[a-z0-9]{1,5}") ? ext : ".mp4";
    }

    /** File name for a direct save into the public Downloads directory. */
    private String downloadName(String raw) {
        String stem = suggestedStem(raw);
        if (stem.isEmpty()) stem = "asmrlib-media-" + System.currentTimeMillis();
        return stem + extensionOf(raw);
    }

    /**
     * The single most recent task for this URL, or null. Used to keep a repeat
     * tap on 保存媒体 from silently starting a second copy of a media file that
     * is already downloading, already broken, or already saved.
     */
    private JSONObject findTaskFor(String url) {
        if (url == null || url.isEmpty()) return null;
        for (int i = downloadTasks.size() - 1; i >= 0; i--) {
            JSONObject task = downloadTasks.get(i);
            if (url.equals(task.optString("url", ""))) return task;
        }
        return null;
    }

    /**
     * Find the record that is this same video, across sessions.
     *
     * <p>The exact media URL is the strongest key, and in practice the
     * weakest: these players hand out signed, per-session manifests, so the
     * same video browsed tomorrow is a different URL and an exact match
     * never fires — which is how one title piled up three gigabyte copies
     * with nothing anywhere saying so. Two weaker keys close that: the play
     * page's own address (the discover page passes the card's data in the
     * fragment, stable per item), and finally the remembered title against
     * the saved file's stem, sanitised the same way both sides sanitise.
     */
    private JSONObject findTaskForMedia(String url, String pageUrl, String title) {
        if (url != null && !url.isEmpty()) {
            JSONObject exact = findTaskFor(url);
            if (exact != null) return exact;
        }
        String wanted = sanitizeStem(title == null ? "" : title)
                .toLowerCase(java.util.Locale.US);
        for (int i = downloadTasks.size() - 1; i >= 0; i--) {
            JSONObject task = downloadTasks.get(i);
            if (pageUrl != null && !pageUrl.isEmpty()
                    && pageUrl.equals(task.optString("pageUrl", ""))) {
                return task;
            }
            if (wanted.isEmpty()) continue;
            String name = task.optString("fileName", "");
            int dot = name.lastIndexOf('.');
            if (dot > 0) name = name.substring(0, dot);
            if (sanitizeStem(name).toLowerCase(java.util.Locale.US).equals(wanted)) {
                return task;
            }
        }
        return null;
    }

    /** The record for the video on screen, or null. The key the whole
     *  "is this already saved" surface reads. */
    private JSONObject findSavedTaskForCurrent() {
        if (!hasSavableMedia()) return null;
        String url = isHlsUrl(verifiedHlsUrl) ? verifiedHlsUrl : verifiedDownloadUrl;
        String pageUrl = webView == null ? "" : webView.getUrl();
        String title = mediaContextTitle.trim();
        if (title.isEmpty()) title = suggestedStem(url);
        JSONObject task = findTaskForMedia(url, pageUrl, title);
        if (task == null || !STATE_SUCCESS.equals(task.optString("state", ""))) return null;
        return task;
    }

    /**
     * Route a repeat save through the task that already exists for this URL.
     * Returns true when the caller must stop — either the tap was absorbed or
     * the existing task was resumed in place.
     *
     * <p>This is what makes the downloader's resume path reachable. The parts
     * cache is keyed by the destination path, and a fresh save mints a new
     * timestamped destination, so a second tap could only ever restart from
     * segment 0 and orphan the segments the first attempt had already paid
     * for. Observed live: three taps on one manifest produced three jobs, two
     * of which timed out holding 877 MB between them that nothing could reuse.
     */
    private boolean absorbIntoExistingTask(String url) {
        // The cross-session key, not the URL alone: a signed manifest makes
        // the same video a different URL tomorrow, and the URL-only check is
        // what let duplicates pile up unseen.
        String pageUrl = webView == null ? "" : webView.getUrl();
        String title = mediaContextTitle.trim();
        if (title.isEmpty()) title = suggestedStem(url);
        JSONObject existing = findTaskForMedia(url, pageUrl, title);
        if (existing == null) return false;
        String state = existing.optString("state", STATE_RUNNING);
        if (STATE_RUNNING.equals(state)) {
            Toast.makeText(this, "该媒体已在下载中", Toast.LENGTH_SHORT).show();
            return true;
        }
        if (STATE_SUCCESS.equals(state)) {
            // Refusing is the honest answer — the bytes are already in the
            // offline directory and a second ~1 GB copy is not something the
            // user asked for — and saying it is not enough either: the tap
            // opens the download centre so the saved file is one glance away
            // instead of a promise.
            Toast.makeText(this, "该视频已保存过，已打开下载中心", Toast.LENGTH_SHORT).show();
            launchDownloadsActivity();
            return true;
        }
        // Failed or lost: resume. retryDownload reuses the record's localPath,
        // so HlsDownloader finds the existing .parts directory and skips every
        // segment that already arrived instead of re-fetching it.
        retryDownload(existing);
        return true;
    }

    private void enqueueVerifiedDownload() {
        requestDownloadNotifications();
        if (isHlsUrl(verifiedHlsUrl)) {
            if (absorbIntoExistingTask(verifiedHlsUrl)) return;
            startHlsDownload(verifiedHlsUrl, webView == null ? "" : webView.getUrl());
            return;
        }
        String url = verifiedDownloadUrl;
        if (!isDirectDownloadUrl(url)) {
            Toast.makeText(this, "还没有检测到可保存的直链媒体", Toast.LENGTH_SHORT).show();
            return;
        }
        if (absorbIntoExistingTask(url)) return;
        String fileName = downloadName(url);
        String pageUrl = webView == null ? "" : webView.getUrl();
        try {
            long downloadId = enqueueDownload(url, pageUrl, fileName);
            // Retain the task: without this the Toast was the only evidence the
            // download ever existed, which is exactly the fake success the
            // download center replaces.
            downloadTasks.add(newTaskRecord(downloadId, url, pageUrl, fileName));
            saveDownloadTasks();
            Toast.makeText(this, "已加入下载中心", Toast.LENGTH_SHORT).show();
            if (panelVisible) {
                refreshDownloadStates();
                renderDownloadPanel();
            }
        } catch (Exception e) {
            Log.w(TAG, "verified media download failed", e);
            Toast.makeText(this, "无法创建下载任务：" + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    /** Ask only when the user starts a download, not on the first app launch. */
    private void requestDownloadNotifications() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},
                    NOTIFICATION_PERMISSION_REQUEST);
        }
        ensureNotificationChannel();
    }

    private void ensureNotificationChannel() {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager manager = (NotificationManager) getSystemService(
                Context.NOTIFICATION_SERVICE);
        if (manager == null) return;
        NotificationChannel channel = new NotificationChannel(
                NOTIFICATION_CHANNEL_ID, "下载通知", NotificationManager.IMPORTANCE_DEFAULT);
        channel.setDescription("视频下载进度与完成提示");
        manager.createNotificationChannel(channel);
    }

    /** App-owned completion notification for HLS; DownloadManager covers direct files. */
    private void notifyDownloadFinished(String taskId, String fileName, boolean success) {
        HlsNotify.finished(this, taskId, fileName, success);
    }

    /** Build and hand a Request to DownloadManager; returns the real download id. */
    private long enqueueDownload(String url, String pageUrl, String fileName) {
        DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
        request.setTitle(fileName);
        request.setDescription("ASMR 收藏馆已验证媒体");
        request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
        request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName);
        request.allowScanningByMediaScanner();
        String cookie = CookieManager.getInstance().getCookie(url);
        if (cookie != null && !cookie.isEmpty()) request.addRequestHeader("Cookie", cookie);
        String referer = pageUrl != null && !pageUrl.isEmpty()
                ? pageUrl : (webView == null ? "" : webView.getUrl());
        request.addRequestHeader("Referer", referer == null ? "" : referer);
        DownloadManager manager = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
        if (manager == null) throw new IllegalStateException("DownloadManager unavailable");
        return manager.enqueue(request);
    }

    // ----------------------------------------------------- in-app HLS job

    /**
     * Queue a real HLS save. HlsDownloader resolves the manifest to a variant,
     * fetches every dependency through the shield policy and concatenates the
     * segments into one file; DownloadManager is deliberately not involved,
     * because handed a playlist it would store the text and report success.
     */
    private void startHlsDownload(String url, String pageUrl) {
        requestDownloadNotifications();
        if (!isHlsUrl(url)) {
            Toast.makeText(this, "还没有检测到可保存的媒体", Toast.LENGTH_SHORT).show();
            return;
        }
        File dest = hlsDestination(url);
        String fileName = dest.getName();
        JSONObject task = newHlsTaskRecord(newHlsTaskId(), url, pageUrl, fileName,
                dest.getAbsolutePath());
        downloadTasks.add(task);
        saveDownloadTasks();
        // The ring has to tick with the panel closed too, so the poll starts
        // here rather than only when the panel opens.
        startPolling();
        Toast.makeText(this, "已加入下载中心", Toast.LENGTH_SHORT).show();
        if (panelVisible) {
            refreshDownloadStates();
            renderDownloadPanel();
        }
        startHlsJob(task);
    }

    /** Start (or resume) the worker behind a stored HLS record. */
    private void startHlsJob(final JSONObject task) {
        final String taskId = task.optString("taskId", "");
        final String url = task.optString("url", "");
        final String localPath = task.optString("localPath", "");
        if (taskId.isEmpty() || url.isEmpty() || localPath.isEmpty()) return;
        final HlsJob job = new HlsJob(taskId, localPath);
        HlsJobs.put(taskId, job);
        // Snapshot the policy on the UI thread; the worker must not read a
        // field the gate may reassign underneath it.
        final HlsDownloader.Policy policy = hlsPolicy();
        final HlsDownloader.Listener listener = new HlsProgressListener(taskId, job);
        Thread worker = new Thread(new Runnable() {
            @Override
            public void run() {
                HlsDownloader.Result result = HlsDownloader.download(
                        url, new File(localPath), policy, listener);
                finishHlsJob(job, result);
            }
        }, "asmrlib-hls-" + taskId);
        worker.start();
    }

    /**
     * The gate every HLS dependency and every redirect hop must pass: the same
     * ad-host/keyword rule the request interceptor applies. A segment on a
     * blocked host is refused, and a 302 pointing at one is refused before it
     * is ever requested — an auto-followed redirect would be a bypass, since
     * the policy only ever saw the declared URL.
     */
    private HlsDownloader.Policy hlsPolicy() {
        final ShieldRules rules = shield;
        return new HlsDownloader.Policy() {
            @Override
            public boolean allow(String url) {
                return rules != null && !rules.isAdUrl(url);
            }
        };
    }

    /** Reports progress on the UI thread; deliberately never touches the record. */
    private final class HlsProgressListener implements HlsDownloader.Listener {
        private final String taskId;
        private final HlsJob job;

        HlsProgressListener(String taskId, HlsJob job) {
            this.taskId = taskId;
            this.job = job;
        }

        @Override
        public void onProgress(final int done, final int total, final long bytes) {
            mainHandler.post(new Runnable() {
                @Override
                public void run() {
                    applyHlsProgress(taskId, done, total, bytes);
                }
            });
        }

        @Override
        public boolean isCancelled() {
            return job.cancelled;
        }
    }

    private void applyHlsProgress(String taskId, int done, int total, long bytes) {
        JSONObject task = findHlsTask(taskId);
        if (task == null) return;
        try {
            task.put("segmentsDone", done);
            task.put("segmentsTotal", total);
            task.put("bytesSoFar", bytes);
        } catch (JSONException e) {
            Log.w(TAG, "could not store HLS progress", e);
        }
        saveDownloadTasks();
        // The notification shade is the surface the user can still see with
        // the app in the background — a gigabyte takes a while, and the app
        // must not go quiet just because its window did.
        HlsNotify.progress(this, taskId,
                task.optString("fileName", "视频文件"), done, total, bytes);
        if (panelVisible) renderDownloadPanel();
    }

    private void finishHlsJob(HlsJob job, HlsDownloader.Result result) {
        final String taskId = job.taskId;
        final String localPath = job.localPath;
        final boolean ok = result.ok;
        final String error = result.error;
        final int segments = result.segmentsTotal;
        final long bytes = result.bytes;
        mainHandler.post(new Runnable() {
            @Override
            public void run() {
                HlsJobs.remove(taskId);
                JSONObject task = findHlsTask(taskId);
                if (task == null) {
                    if (job.cancelled) {
                        // 删除 dropped the record while the worker was still
                        // writing, so its resume material has to go as well.
                        deleteTree(new File(localPath + ".parts"));
                        new File(localPath + ".part").delete();
                    }
                    return;
                }
                try {
                    task.put("bytesSoFar", bytes);
                    if (ok) {
                        // HlsDownloader only reports ok once the bytes are on
                        // disk and non-empty, so this is a real file to open.
                        task.put("state", STATE_SUCCESS);
                        task.put("error", "");
                        task.put("segmentsDone", segments);
                        task.put("segmentsTotal", segments);
                        task.put("localUri", Uri.fromFile(new File(localPath)).toString());
                    } else {
                        task.put("state", STATE_FAILED);
                        task.put("error", error);
                    }
                } catch (JSONException e) {
                    Log.w(TAG, "could not store HLS outcome", e);
                }
                saveDownloadTasks();
                notifyDownloadFinished(taskId,
                        task.optString("fileName", "视频文件"), ok);
                if (panelVisible) renderDownloadPanel();
            }
        });
    }

    private JSONObject findHlsTask(String taskId) {
        for (int i = 0; i < downloadTasks.size(); i++) {
            JSONObject task = downloadTasks.get(i);
            if (KIND_HLS.equals(task.optString("kind", ""))
                    && taskId.equals(task.optString("taskId", ""))) {
                return task;
            }
        }
        return null;
    }

    private static boolean isHlsTask(JSONObject task) {
        return KIND_HLS.equals(task.optString("kind", KIND_DIRECT));
    }

    private String newHlsTaskId() {
        return "hls-" + System.currentTimeMillis() + "-" + (++hlsSeq);
    }

    /**
     * HLS output name: the playlist's own stem plus a timestamp, so saving the
     * same playlist twice cannot silently share one file. The extension is
     * forced to .ts because the output is concatenated MPEG-TS — calling it
     * .mp4 would be a lie about the container.
     */
    /**
     * Where an HLS save lands: the post's title, in the offline directory.
     *
     * <p>The old name appended {@code System.currentTimeMillis()} to the CDN's
     * own stem, which made the file unreadable in the player and in the panel —
     * and, worse, made the downloader's documented resume path unreachable from
     * a fresh save: the parts cache is keyed by the destination path, so a new
     * stamp meant a new directory, a restart from segment 0, and the segments
     * the first attempt had already paid for left orphaned. Uniqueness now
     * comes from {@link #uniqueDest}, which the user can see and reason about.
     */
    private File hlsDestination(String url) {
        String stem = suggestedStem(url);
        if (stem.isEmpty()) stem = "asmrlib-hls";
        return uniqueDest(offlineDir(), stem, ".ts");
    }

    /**
     * The first free name, so two posts that happen to share a title do not
     * collide. A {@code .parts} or {@code .part} sibling counts as taken:
     * reusing a name whose segments are still on disk would silently resume a
     * different download from stale bytes, which is the one failure mode the
     * delete path goes out of its way to avoid.
     */
    private static File uniqueDest(File dir, String stem, String ext) {
        File candidate = new File(dir, stem + ext);
        int n = 2;
        while (candidate.exists()
                || new File(dir, stem + ext + ".parts").exists()
                || new File(dir, stem + ext + ".part").exists()) {
            if (n > 99) {
                // Pathological: stamp it rather than loop forever.
                return new File(dir, stem + "-" + System.currentTimeMillis() + ext);
            }
            candidate = new File(dir, stem + " (" + n + ")" + ext);
            n++;
        }
        return candidate;
    }

    /** App-private storage: no runtime permission needed on minSdk 26. */
    private File offlineDir() {
        File base = getExternalFilesDir(null);
        if (base == null) base = getFilesDir();
        File dir = new File(base, OFFLINE_DIR);
        dir.mkdirs();
        return dir;
    }

    private static void deleteTree(File file) {
        if (file == null || !file.exists()) return;
        if (file.isDirectory()) {
            File[] children = file.listFiles();
            if (children != null) {
                for (File child : children) deleteTree(child);
            }
        }
        file.delete();
    }

    // ------------------------------------------------------------ download center

    /**
     * Native entry point into the download center. It is a real view in the
     * decor chrome — never a WebView overlay — so a compromised page cannot
     * draw over or dismiss it.
     *
     * The entry is an icon, not a labelled chip, and it is the only floating
     * control the shell has: 保存媒体 used to sit below it as a second chip,
     * but that button is meaningless until a media URL has been sniffed while
     * this one is useful for the whole session, so it spent a permanent strip
     * of the page for a mostly-dead label. The action now lives inside the
     * card this icon opens. A 44dp square also says the same thing in a third
     * of the width a text chip needed.
     */
    private void attachDownloadEntry() {
        FrameLayout decor = (FrameLayout) getWindow().getDecorView();
        if (downloadEntry != null) {
            if (downloadEntry.getParent() == null) {
                decor.addView(downloadEntry, downloadEntryLayout());
            }
            downloadEntry.setVisibility(View.VISIBLE);
            return;
        }
        Button button = new Button(this);
        button.setContentDescription("下载菜单：单击打开，双击置顶，长按归位");
        styleDownloadEntry(button, false);
        // Four gestures share one icon: single-tap opens the menu,
        // double-tap toggles "pinned to top" (the answer to 「能不能置顶」),
        // long-press resets to the bottom-right default, and a drag still
        // moves it freely. A GestureDetector keeps these from stepping on
        // each other: it only fires tap callbacks after the up of a gesture
        // that stayed within the touch slop, so a drag never reaches
        // onSingleTapConfirmed.
        entryTouchSlop = android.view.ViewConfiguration.get(this).getScaledTouchSlop();
        final android.view.GestureDetector gesture =
                new android.view.GestureDetector(this,
                new android.view.GestureDetector.SimpleOnGestureListener() {
                    @Override public boolean onSingleTapConfirmed(
                            android.view.MotionEvent e) {
                        // One control, two actions: the icon expands a menu
                        // instead of guessing which of the two the user
                        // meant. The menu is the discovery surface — the user
                        // who has never saved before sees both 「保存当前视频」
                        // and 「下载中心」 spelled out rather than having to
                        // infer them from a download arrow.
                        toggleFabMenu();
                        return true;
                    }
                    @Override public boolean onDoubleTap(
                            android.view.MotionEvent e) {
                        togglePinToTop();
                        return true;
                    }
                    @Override public void onLongPress(
                            android.view.MotionEvent e) {
                        collapseFabMenu();
                        entryDragX = 0;
                        entryDragY = 0;
                        entryGravity = Gravity.BOTTOM | Gravity.END;
                        if (downloadEntry != null
                                && downloadEntry.getParent() instanceof FrameLayout) {
                            downloadEntry.setLayoutParams(downloadEntryLayout());
                        }
                        Toast.makeText(MainActivity.this,
                                "已归位到右下角", Toast.LENGTH_SHORT).show();
                    }
                });
        button.setOnTouchListener(new View.OnTouchListener() {
            @Override
            public boolean onTouch(View v, android.view.MotionEvent event) {
                gesture.onTouchEvent(event);
                switch (event.getActionMasked()) {
                    case android.view.MotionEvent.ACTION_DOWN:
                        entryDownAt = event.getEventTime();
                        entryDownX = event.getRawX();
                        entryDownY = event.getRawY();
                        entryDragging = false;
                        return false; // let tap detection see the up
                    case android.view.MotionEvent.ACTION_MOVE: {
                        float dx = event.getRawX() - entryDownX;
                        float dy = event.getRawY() - entryDownY;
                        if (!entryDragging
                                && (Math.abs(dx) > entryTouchSlop
                                || Math.abs(dy) > entryTouchSlop)) {
                            entryDragging = true;
                            // The menu is pinned to the icon's corner; letting
                            // it stay open while the icon moves would drag a
                            // stale panel across the page.
                            collapseFabMenu();
                        }
                        if (!entryDragging) return false;
                        int parentW = ((View) v.getParent()).getWidth();
                        int parentH = ((View) v.getParent()).getHeight();
                        int w = v.getWidth();
                        int h = v.getHeight();
                        int edgeMargin = dp(8);
                        // Clamp so the icon cannot leave the screen.
                        entryDragX = clampInt((int) dx,
                                -(parentW - w - edgeMargin),
                                parentW - w - edgeMargin);
                        entryDragY = clampInt((int) dy,
                                -(parentH - h - edgeMargin),
                                parentH - h - edgeMargin);
                        if (downloadEntry != null
                                && downloadEntry.getParent() instanceof FrameLayout) {
                            downloadEntry.setLayoutParams(downloadEntryLayout());
                        }
                        return true;
                    }
                    case android.view.MotionEvent.ACTION_UP:
                    case android.view.MotionEvent.ACTION_CANCEL: {
                        if (!entryDragging) return false;
                        View parent = (View) v.getParent();
                        int parentW = parent.getWidth();
                        int parentH = parent.getHeight();
                        int w = v.getWidth();
                        int h = v.getHeight();
                        // Snap to nearest edge: horizontal flips to whichever
                        // side the centre ended up closer to; vertical flips
                        // to top/bottom the same way. We compute against the
                        // centre of the icon — the gesture pulls the icon's
                        // centre past the parent's midline and we choose the
                        // edge on the same side of that midline.
                        int cx = w / 2 + entryDragX;
                        int cy = h / 2 + entryDragY;
                        int vGrav = (cy < parentH / 2) ? Gravity.TOP : Gravity.BOTTOM;
                        int hGrav = (cx < parentW / 2) ? Gravity.START : Gravity.END;
                        entryGravity = vGrav | hGrav;
                        // After a snap the live drag offsets become the new
                        // baseline — long-press still resets them wholesale.
                        entryDownX = cx - w / 2;
                        entryDownY = cy - h / 2;
                        entryDragX = 0;
                        entryDragY = 0;
                        if (downloadEntry != null
                                && downloadEntry.getParent() instanceof FrameLayout) {
                            downloadEntry.setLayoutParams(downloadEntryLayout());
                        }
                        return true;
                    }
                    default:
                        return false;
                }
            }
        });
        downloadEntry = button;
        decor.addView(button, downloadEntryLayout());
        button.setVisibility(View.VISIBLE);
    }

    // ------------------------------------------------------- floating menu

    /**
     * Show or hide the action stack. The icon itself never performs an action
     * any more: with two floating buttons the user could not tell which one
     * saved, so the affordance is now "one button, expand it".
     */
    private void toggleFabMenu() {
        if (fabMenuOpen) collapseFabMenu();
        else expandFabMenu();
    }

    /**
     * Toggle the FAB between "pinned to top" and the bottom-right default.
     *
     * <p>Double-tap is the discoverable answer to 「能不能置顶」. The horizontal
     * anchor follows the corner the user expects: double-tap from any
     * position lands on the right edge (matching the default) — that way the
     * gesture reads as "swap vertical edge", not as a four-corner cycle that
     * needs a mental map.
     *
     * <p>Long-press still means "reset" — and resets to the same default the
     * toggle falls back to — so the two gestures can't strand the icon
     * somewhere unexpected.
     */
    private void togglePinToTop() {
        collapseFabMenu();
        boolean pinned = (entryGravity & Gravity.TOP) == Gravity.TOP
                && (entryGravity & Gravity.END) == Gravity.END;
        entryDragX = 0;
        entryDragY = 0;
        if (pinned) {
            entryGravity = Gravity.BOTTOM | Gravity.END;
        } else {
            entryGravity = Gravity.TOP | Gravity.END;
        }
        if (downloadEntry != null
                && downloadEntry.getParent() instanceof FrameLayout) {
            downloadEntry.setLayoutParams(downloadEntryLayout());
        }
        Toast.makeText(MainActivity.this,
                pinned ? "已归位到右下角" : "已置顶",
                Toast.LENGTH_SHORT).show();
    }

    private void expandFabMenu() {
        if (downloadEntry == null) return;
        buildFabMenuIfNeeded();
        refreshFabMenuState();
        FrameLayout decor = (FrameLayout) getWindow().getDecorView();
        if (fabScrim.getParent() == null) {
            decor.addView(fabScrim, new FrameLayout.LayoutParams(
                    FrameLayout.LayoutParams.MATCH_PARENT,
                    FrameLayout.LayoutParams.MATCH_PARENT));
        }
        if (fabMenu.getParent() == null) decor.addView(fabMenu, fabMenuLayout());
        else fabMenu.setLayoutParams(fabMenuLayout());
        fabScrim.setVisibility(View.VISIBLE);
        fabMenu.setVisibility(View.VISIBLE);
        fabMenu.bringToFront();
        downloadEntry.bringToFront();
        fabMenuOpen = true;
        downloadEntry.setContentDescription("收起下载菜单");
        // A small rise + fade: without it the card appears to jump out of the
        // icon and the two read as unrelated surfaces.
        // DecelerateInterpolator(2f) overshoots slightly - the pill eases
        // into place and "lands" instead of arriving at constant velocity.
        fabMenu.setAlpha(0f);
        fabMenu.setTranslationY(dp(10));
        fabMenu.setScaleX(0.94f);
        fabMenu.setScaleY(0.94f);
        fabMenu.animate()
                .alpha(1f).translationY(0f).scaleX(1f).scaleY(1f)
                .setDuration(200)
                .setInterpolator(new android.view.animation.DecelerateInterpolator(2f))
                .start();
    }

    private void collapseFabMenu() {
        fabMenuOpen = false;
        if (fabMenu != null) {
            fabMenu.animate().cancel();
            fabMenu.setVisibility(View.GONE);
        }
        if (fabScrim != null) fabScrim.setVisibility(View.GONE);
        if (downloadEntry != null) {
            downloadEntry.setContentDescription("打开下载菜单");
        }
    }

    /** Build once: the rows are reused so the enabled state survives a re-open.
     *
     * <p>The menu is a single horizontal pill rather than a stacked card:
     * two stacked buttons in a bordered rectangle read as "form", not as a
     * transient action sheet, and the user is left wondering which one is the
     * primary. A pill — translucent surface over the scrim, hairline divider
     * down the middle — reads as the two siblings of one moment. A vertical
     * stack was tried with three actions and rejected: three 116dp cells asked
     * for more width than a 360dp phone had.
     */
    private void buildFabMenuIfNeeded() {
        if (fabMenu != null) return;

        fabScrim = new View(this);
        fabScrim.setBackgroundColor(Theme.scrim(MainActivity.this, 70));
        fabScrim.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { collapseFabMenu(); }
        });
        fabScrim.setVisibility(View.GONE);

        LinearLayout pill = new LinearLayout(this);
        pill.setOrientation(LinearLayout.HORIZONTAL);
        pill.setGravity(Gravity.CENTER_VERTICAL);
        // Tight padding so the icon-button cells sit close to the pill edge;
        // the pill's own corner radius does the rest of the visual lift.
        pill.setPadding(dp(5), dp(5), dp(5), dp(5));
        pill.setClipToOutline(true);

        // Glass surface: the scrim's tone leaks through enough that the pill
        // reads as part of the page rather than as a sticker stuck on top.
        // An opaque surface reads as a normal card and loses the "liquid"
        // entirely.
        pill.setBackground(new GlassDrawable(dp(28)));
        pill.setElevation(dp(22));

        // Cell 1: 下载中心 (sits at the outer edge of the pill so the corner
        // radius hugs it without cropping the glyph).
        pill.addView(fabPillCell(
                "下载中心",
                new SizedIconDrawable(
                        new DownloadCenterGlyphDrawable(Theme.text2(MainActivity.this)),
                        dp(18)),
                new View.OnClickListener() {
            @Override public void onClick(View v) {
                collapseFabMenu();
                launchDownloadsActivity();
            }
        }));

        // Hairline divider, oriented vertically between the two cells.
        View divider = new View(this);
        LinearLayout.LayoutParams divLp = new LinearLayout.LayoutParams(dp(1), dp(20));
        divLp.gravity = Gravity.CENTER_VERTICAL;
        divLp.leftMargin = dp(3);
        divLp.rightMargin = dp(3);
        divider.setLayoutParams(divLp);
        divider.setBackgroundColor(Theme.sep(MainActivity.this));
        pill.addView(divider);

        // Cell 2: 保存当前视频 (the primary action; this is what the FAB
        // icon in the corner actually stands for when media is ready).
        fabSaveItem = fabPillCell(
                "保存视频",
                new SizedIconDrawable(
                        new DownloadGlyphDrawable(Theme.text2(MainActivity.this)),
                        dp(18)),
                new View.OnClickListener() {
            @Override public void onClick(View v) {
                if (!hasSavableMedia()) {
                    Toast.makeText(MainActivity.this,
                            "还没有检测到可保存的媒体", Toast.LENGTH_SHORT).show();
                    return;
                }
                collapseFabMenu();
                enqueueVerifiedDownload();
            }
        });
        pill.addView(fabSaveItem);

        // Hairline divider, oriented vertically between the cells.
        View divider2 = new View(this);
        LinearLayout.LayoutParams div2Lp = new LinearLayout.LayoutParams(dp(1), dp(20));
        div2Lp.gravity = Gravity.CENTER_VERTICAL;
        div2Lp.leftMargin = dp(3);
        div2Lp.rightMargin = dp(3);
        divider2.setLayoutParams(div2Lp);
        divider2.setBackgroundColor(Theme.sep(MainActivity.this));
        pill.addView(divider2);

        // Cell 3: 应用内播放 — the same sniffed media, played by the shell's
        // own player instead of (or after) being saved. It shares the save
        // cell's readiness state: both consume verifiedHlsUrl /
        // verifiedDownloadUrl, so a sniff that armed one armed the other.
        fabPlayItem = fabPillCell(
                "应用内播放",
                new SizedIconDrawable(
                        new PlayGlyphDrawable(Theme.text2(MainActivity.this)),
                        dp(18)),
                new View.OnClickListener() {
            @Override public void onClick(View v) {
                if (!hasSavableMedia()) {
                    Toast.makeText(MainActivity.this,
                            "还没有检测到可播放的媒体", Toast.LENGTH_SHORT).show();
                    return;
                }
                collapseFabMenu();
                launchInAppPlayback();
            }
        });
        pill.addView(fabPlayItem);

        fabMenu = pill;
        fabMenu.setVisibility(View.GONE);
    }

    /** The sniffed media in the shell's own player (StreamProxy path). */
    private void launchInAppPlayback() {
        boolean hls = isHlsUrl(verifiedHlsUrl);
        String url = hls ? verifiedHlsUrl : verifiedDownloadUrl;
        if (!hls && !isDirectDownloadUrl(url)) return;
        Intent play = new Intent(this, PlayerActivity.class);
        play.putExtra(PlayerActivity.EXTRA_REMOTE_URL, url);
        play.putExtra(PlayerActivity.EXTRA_REMOTE_KIND, hls ? "hls" : "direct");
        String title = mediaContextTitle.trim();
        if (title.isEmpty()) title = suggestedStem(url);
        play.putExtra(PlayerActivity.EXTRA_TITLE, title);
        play.putExtra(PlayerActivity.EXTRA_REMOTE_COVER, playContextCover);
        play.putExtra(PlayerActivity.EXTRA_REMOTE_REFERER,
                webView == null ? "" : String.valueOf(webView.getUrl()));
        startActivity(play);
    }

    /** One cell of the FAB pill: glyph + label, a comfortable tap target. */
    private Button fabPillCell(String label, Drawable glyph,
                               View.OnClickListener action) {
        Button cell = new Button(this);
        cell.setAllCaps(false);
        cell.setText(label);
        cell.setTextSize(13f);
        cell.setTextColor(Theme.text2(MainActivity.this));
        cell.setGravity(Gravity.CENTER);
        // Generous horizontal padding makes the row feel like a button, not a
        // label, without pushing the pill wider than the screen - the cells
        // keep their padding while the pill absorbs the rounding.
        cell.setPadding(dp(12), 0, dp(14), 0);
        // Three cells now share one pill (下载中心/保存/应用内播放), so a
        // fixed 116dp minimum would overflow the screen width and squeeze
        // the last cell into a one-character-per-line vertical column -
        // observed live. Size to the label, and pin it to one line so a
        // squeeze can never restack it.
        cell.setSingleLine(true);
        cell.setMinWidth(0);
        cell.setMinimumWidth(0);
        cell.setMinHeight(dp(42));
        cell.setMinimumHeight(dp(42));
        cell.setCompoundDrawablePadding(dp(8));
        cell.setCompoundDrawablesWithIntrinsicBounds(glyph, null, null, null);
        // Transparent base + theme-aware pressed wash.  Drawing the wash as a
        // separate selector (rather than a single round-rect on the cell) lets
        // the press highlight curve with the pill itself instead of looking
        // like a smaller rectangle slid over it.
        cell.setBackground(PressFeedback.on(pillCellBackground()));
        cell.setOnClickListener(action);
        return cell;
    }

    /** A press-only ripple: transparent default, fill-coloured on state_pressed. */
    private Drawable pillCellBackground() {
        android.graphics.drawable.StateListDrawable sld =
                new android.graphics.drawable.StateListDrawable();
        GradientDrawable pressed = new GradientDrawable();
        pressed.setColor(Theme.fill(MainActivity.this));
        pressed.setCornerRadius(dp(100));
        sld.addState(new int[]{android.R.attr.state_pressed}, pressed);
        return sld;
    }

    /**
     * A glyph wrapper that reports an intrinsic size. Both menu glyphs derive
     * their geometry from the bounds they are handed, which makes them size
     * zero to {@code setCompoundDrawablesWithIntrinsicBounds} — the row would
     * render as bare text.
     */
    private static final class SizedIconDrawable extends Drawable {
        private final Drawable inner;
        private final int size;
        SizedIconDrawable(Drawable inner, int size) {
            this.inner = inner;
            this.size = size;
        }
        @Override public int getIntrinsicWidth() { return size; }
        @Override public int getIntrinsicHeight() { return size; }
        @Override public void draw(Canvas canvas) {
            Rect b = getBounds();
            int w = Math.min(size, b.width());
            int h = Math.min(size, b.height());
            int l = b.left + (b.width() - w) / 2;
            int t = b.top + (b.height() - h) / 2;
            inner.setBounds(l, t, l + w, t + h);
            inner.draw(canvas);
        }
        @Override public void setAlpha(int alpha) { inner.setAlpha(alpha); }
        @Override public void setColorFilter(ColorFilter filter) { inner.setColorFilter(filter); }
        @Override public int getOpacity() { return inner.getOpacity(); }
    }

    /**
     * Project media-readiness onto the menu's save row. Same predicate as the
     * icon's lit ring, so the two cannot disagree about whether a save is
     * possible — the row stays tappable but greyed, and tapping it explains
     * why rather than silently doing nothing.
     */
    private void refreshFabMenuState() {
        if (fabSaveItem == null) return;
        boolean ready = hasSavableMedia();
        // The duplicate answer: a video the app already holds says so, in
        // the row the user was about to tap — and the tap opens the saved
        // file instead of quietly writing copy two.
        boolean saved = findSavedTaskForCurrent() != null;
        fabSaveItem.setText(saved ? "已保存 · 在下载中心" : "保存视频");
        // Alpha and the glyph's tint are the whole projection. The old one was
        // a gradient plate with its own dp(12) corner — a rounded rectangle
        // inside a pill-shaped row inside a rounded card, three radii in one
        // stack, which is the 「矩形弧度都不一样」 report. Every row keeps
        // the one pill shape; the ready state reads from the glyph turning
        // accent, the way the FAB icon's own lit ring does.
        fabSaveItem.setAlpha(ready ? 1f : 0.42f);
        fabSaveItem.setCompoundDrawablesWithIntrinsicBounds(new SizedIconDrawable(
                new DownloadGlyphDrawable(ready
                        ? Theme.accent(MainActivity.this)
                        : Theme.text2(MainActivity.this)), dp(18)), null, null, null);
        fabSaveItem.setContentDescription(ready
                ? "保存视频" : "还没有检测到可保存的媒体");
        // The play row shares the save row's readiness — both consume the
        // same sniffed URL — but it never changes label: 播放 is 播放 whether
        // the bytes are already saved or not.
        if (fabPlayItem != null) {
            fabPlayItem.setAlpha(ready ? 1f : 0.42f);
            fabPlayItem.setCompoundDrawablesWithIntrinsicBounds(new SizedIconDrawable(
                    new PlayGlyphDrawable(ready
                            ? Theme.accent(MainActivity.this)
                            : Theme.text2(MainActivity.this)), dp(18)), null, null, null);
            fabPlayItem.setContentDescription(ready
                    ? "在应用内播放" : "还没有检测到可播放的媒体");
        }
    }

    /**
     * Anchored to the same corner as the icon and offset by one icon height so
     * it never covers the button that opened it. Mirrored for all four pins:
     * the stack grows upward when the icon sits at the bottom and downward when
     * it sits at the top, and it hugs whichever side the icon was dragged to.
     */
    private FrameLayout.LayoutParams fabMenuLayout() {
        int hGrav = (entryGravity & Gravity.START) == Gravity.START
                ? Gravity.START : Gravity.END;
        int vGrav = (entryGravity & Gravity.TOP) == Gravity.TOP
                ? Gravity.TOP : Gravity.BOTTOM;
        int edge = isTablet() ? dp(24) : dp(14);
        // Icon height + a gap, so the card's nearest edge clears the icon.
        int past = entrySize() + dp(10);
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT,
                FrameLayout.LayoutParams.WRAP_CONTENT, hGrav | vGrav);
        int left = 0, top = 0, right = 0, bottom = 0;
        if (hGrav == Gravity.START) left = edge; else right = edge;
        if (vGrav == Gravity.TOP) top = past; else bottom = past;
        // Same mirrored trick as downloadEntryLayout: whichever margin the
        // gravity actually honours is the one that receives the drag offset.
        left += entryDragX;
        right -= entryDragX;
        top += entryDragY;
        bottom -= entryDragY;
        lp.setMargins(left, top, right, bottom);
        return lp;
    }

    /**
     * The "download centre" glyph: a folder.
     *
     * <p>The old version drew three stacked horizontal cards, which at a
     * glance read as the character 目 — or as a hamburger menu — rather than
     * as a library. A folder is the one shape that says "my saved files"
     * without borrowing the hamburger's meaning; the tab on top and the open
     * front edge are drawn with the same stroke the download arrow uses, so
     * the two menu glyphs read as one set.
     *
     * <p>Geometry is computed from the bounds handed to {@link #draw}, so a
     * single instance is correct at any density or button size.
     */
    private static final class DownloadCenterGlyphDrawable extends Drawable {
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        DownloadCenterGlyphDrawable(int color) {
            paint.setColor(color);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeCap(Paint.Cap.ROUND);
            paint.setStrokeJoin(Paint.Join.ROUND);
            paint.setStrokeWidth(2.0f);
        }
        @Override public void draw(Canvas canvas) {
            Rect b = getBounds();
            float w = b.width();
            float h = b.height();
            if (w <= 0f || h <= 0f) return;
            float side = Math.min(w, h);
            float inset = side * 0.16f;
            float left = (w - side) / 2f + inset;
            float right = left + side - 2 * inset;
            float top = (h - side) / 2f + inset * 1.15f;
            float bottom = top + side - 2 * inset * 1.15f;
            float tabH = side * 0.16f;
            float tabW = side * 0.30f;
            float radius = side * 0.08f;
            canvas.drawRoundRect(left, top + tabH * 0.55f, right, bottom,
                    radius, radius, paint);
            // The tab: a short stroke rising from the body's top-left, with
            // the fold line where a real folder bends.
            canvas.drawLine(left, top + tabH * 0.55f, left, top + tabH * 0.2f, paint);
            canvas.drawLine(left, top + tabH * 0.2f, left + tabW, top + tabH * 0.2f, paint);
            canvas.drawLine(left + tabW, top + tabH * 0.2f, left + tabW, top + tabH * 0.55f, paint);
        }
        @Override public void setAlpha(int alpha) { paint.setAlpha(alpha); }
        @Override public void setColorFilter(ColorFilter filter) { paint.setColorFilter(filter); }
        @Override public int getOpacity() { return PixelFormat.TRANSLUCENT; }
    }

    /**
     * Style the icon entry, in one of two states: idle, or lit once the shield
     * has seen media that can be saved. The lit ring is the only affordance
     * telling the user a save is now possible while the panel is closed —
     * without it, moving 保存媒体 off the decor would have made the action
     * undiscoverable rather than merely smaller.
     *
     * Called at creation <em>and</em> again from {@link #onConfigurationChanged}
     * and on every state change: the ring's stroke and the button's own px
     * metrics are resolved when they are applied, so a density change that the
     * manifest absorbs instead of recreating the activity would otherwise
     * leave a ring measured for the old bucket around a button sized for the
     * new one. The glyph itself needs no such care — it derives its geometry
     * from the bounds it is handed on every draw.
     */
    private void styleDownloadEntry(Button chip, boolean ready) {
        if (chip == null) return;
        chip.setAllCaps(false);
        chip.setText("");
        chip.setMinimumWidth(0);
        chip.setMinimumHeight(0);
        chip.setPadding(0, 0, 0, 0);
        android.graphics.drawable.GradientDrawable ring =
                new android.graphics.drawable.GradientDrawable();
        ring.setShape(android.graphics.drawable.GradientDrawable.OVAL);
        if (ready) {
            // Same accent as the panel's primary button, so the lit icon reads
            // as "that button is waiting for you".
            ring.setColors(Theme.BRAND_RAMP);
            ring.setOrientation(android.graphics.drawable.GradientDrawable.Orientation.TL_BR);
            ring.setStroke(Math.max(1, dp(1)), Theme.onAccent(MainActivity.this, 160));
        } else {
            ring.setColor(Theme.surface(MainActivity.this, 190));
            ring.setStroke(Math.max(1, dp(1)), Theme.accent(MainActivity.this, 90));
        }
        if (downloadArc == null) {
            downloadArc = new ProgressArcDrawable(
                    Theme.accentAlt(this), Theme.accent(this));
        }
        LayerDrawable layered = new LayerDrawable(new Drawable[]{
                ring, downloadArc, new DownloadGlyphDrawable(
                        ready ? Theme.onAccent(MainActivity.this)
                              : Theme.text(MainActivity.this))});
        // Keep the progress ring and glyph as layers over the shared dark
        // liquid material. The drawable gives the idle FAB the same reflective
        // edge as the expanded menu instead of the old flat grey disc.
        LayerDrawable glassLayered = new LayerDrawable(new Drawable[]{
                new GlassDrawable(entrySize() / 2f), downloadArc,
                new DownloadGlyphDrawable(ready ? Theme.onAccent(MainActivity.this)
                        : Theme.text(MainActivity.this))});
        chip.setBackground(PressFeedback.on(glassLayered));
        // A floating control needs a real shadow.  Without elevation the
        // circle sits flush with the page and the user reads it as part of
        // the wallpaper, not a separate thing to grab.
        chip.setElevation(dp(12));
        // The label names the entry's own action, which is opening the menu.
        // It used to say 保存当前视频 — a leftover from when this control *was*
        // the save chip, so a screen reader announced "save" for a tap that
        // opens a menu. The lit ring still carries the "something is ready"
        // signal, so the state survives as a parenthetical rather than as the
        // whole name.
        chip.setContentDescription(ready ? "打开下载菜单（有可保存的视频）" : "打开下载菜单");
        chip.requestLayout();
        chip.invalidate();
    }

    /** True while any task is still moving — the only time the arc is shown. */
    private boolean isAnythingRunning() {
        for (JSONObject task : downloadTasks) {
            if (STATE_RUNNING.equals(task.optString("state", STATE_RUNNING))) return true;
        }
        return false;
    }

    /**
     * The busiest running task's fraction, or -1 when nothing is running.
     *
     * <p>The maximum rather than a sum: with two saves in flight the ring
     * answers "how close is this to being done", and the furthest-along one is
     * the honest answer to that. A task whose total is not yet known (an HLS
     * job before its playlist resolves, a DownloadManager row before the first
     * size report) counts as running but contributes 0, so the ring appears and
     * waits rather than lying with a full arc.
     */
    private float runningProgress() {
        float best = -1f;
        for (JSONObject task : downloadTasks) {
            if (!STATE_RUNNING.equals(task.optString("state", STATE_RUNNING))) continue;
            if (best < 0f) best = 0f;
            if (isHlsTask(task)) {
                int total = task.optInt("segmentsTotal", 0);
                if (total > 0) {
                    best = Math.max(best, Math.min(1f,
                            task.optInt("segmentsDone", 0) / (float) total));
                }
            } else {
                long total = task.optLong("bytesTotal", -1L);
                if (total > 0) {
                    best = Math.max(best, Math.min(1f,
                            task.optLong("bytesSoFar", 0L) / (float) total));
                }
            }
        }
        return best;
    }

    /**
     * Project the stored tasks onto the entry icon. Derived from the records
     * rather than a counter of its own, so the ring can never disagree with the
     * rows the panel is about to draw.
     */
    private void updateDownloadEntryProgress() {
        if (downloadArc == null) return;
        float fraction = runningProgress();
        downloadArc.setShown(fraction >= 0f);
        if (fraction >= 0f) downloadArc.setFraction(fraction);
    }

    /**
     * The download-centre glyph, drawn instead of shipped as an asset: the
     * bundled pages are under a CSP that forbids external resources, and a
     * vector file would be one more thing to keep in sync with the ring it
     * sits inside.
     *
     * Every dimension is a fraction of the bounds passed to {@link #draw}, so
     * one instance stays correct at any density or button size — no px is
     * captured at construction time.
     */
    private static final class DownloadGlyphDrawable extends Drawable {

        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Path path = new Path();

        DownloadGlyphDrawable(int color) {
            paint.setColor(color);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeCap(Paint.Cap.ROUND);
            paint.setStrokeJoin(Paint.Join.ROUND);
        }

        @Override
        public void draw(Canvas canvas) {
            Rect bounds = getBounds();
            float side = Math.min(bounds.width(), bounds.height());
            if (side <= 0f) return;
            float centreX = bounds.exactCenterX();
            // The tray's floor lays down more ink than the shaft above it, so a
            // geometrically centred glyph reads as sagging. Nudged up by a
            // couple of percent it sits optically centred in the ring.
            float centreY = bounds.exactCenterY() - side * 0.015f;
            float trayWidth = side * 0.46f;
            float height = side * 0.50f;
            float top = centreY - height / 2f;
            float bottom = centreY + height / 2f;
            float left = centreX - trayWidth / 2f;
            float right = centreX + trayWidth / 2f;
            // The head is deliberately narrower than the tray: drawn as wide,
            // it competes with the tray for the eye and the glyph reads as
            // three parallel strokes instead of an arrow landing in a tray.
            float headHalf = trayWidth * 0.36f;
            float arrowTip = top + height * 0.62f;
            float trayTop = top + height * 0.84f;

            paint.setStrokeWidth(side * 0.075f);
            path.reset();
            // Shaft.
            path.moveTo(centreX, top);
            path.lineTo(centreX, arrowTip);
            // Head.
            path.moveTo(centreX - headHalf, arrowTip - height * 0.26f);
            path.lineTo(centreX, arrowTip);
            path.lineTo(centreX + headHalf, arrowTip - height * 0.26f);
            // Tray: two uprights joined by a floor.
            path.moveTo(left, trayTop);
            path.lineTo(left, bottom);
            path.lineTo(right, bottom);
            path.lineTo(right, trayTop);
            canvas.drawPath(path, paint);
        }

        @Override
        public void setAlpha(int alpha) {
            paint.setAlpha(alpha);
        }

        @Override
        public void setColorFilter(ColorFilter filter) {
            paint.setColorFilter(filter);
        }

        @Override
        public int getOpacity() {
            return PixelFormat.TRANSLUCENT;
        }
    }

    /**
     * The 应用内播放 glyph: a right-pointing triangle in a rounded frame —
     * the same vocabulary as the download arrow beside it (stroke outline,
     * round caps, all geometry derived from the bounds), but the shape says
     * "play here" rather than "take it away".
     */
    private static final class PlayGlyphDrawable extends Drawable {

        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Path frame = new Path();
        private final Path triangle = new Path();

        PlayGlyphDrawable(int color) {
            paint.setColor(color);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeCap(Paint.Cap.ROUND);
            paint.setStrokeJoin(Paint.Join.ROUND);
        }

        @Override
        public void draw(Canvas canvas) {
            Rect b = getBounds();
            float side = Math.min(b.width(), b.height());
            if (side <= 0f) return;
            float cx = b.exactCenterX();
            float cy = b.exactCenterY();
            paint.setStrokeWidth(2.0f);
            // Rounded-square frame, inset like the other menu glyphs.
            float half = side * 0.34f;
            float r = side * 0.12f;
            frame.reset();
            frame.addRoundRect(cx - half, cy - half, cx + half, cy + half,
                    r, r, Path.Direction.CW);
            canvas.drawPath(frame, paint);
            // Filled triangle inside the frame — the only solid shape in the
            // menu, which is exactly the weight "play" should carry.
            paint.setStyle(Paint.Style.FILL);
            float t = side * 0.16f;
            triangle.reset();
            triangle.moveTo(cx - t * 0.6f, cy - t);
            triangle.lineTo(cx + t * 0.9f, cy);
            triangle.lineTo(cx - t * 0.6f, cy + t);
            triangle.close();
            canvas.drawPath(triangle, paint);
            paint.setStyle(Paint.Style.STROKE);
        }

        @Override
        public void setAlpha(int alpha) {
            paint.setAlpha(alpha);
        }

        @Override
        public void setColorFilter(ColorFilter filter) {
            paint.setColorFilter(filter);
        }

        @Override
        public int getOpacity() {
            return PixelFormat.TRANSLUCENT;
        }
    }

    /**
     * The progress ring drawn around the entry icon while a save is moving, so
     * the one floating control also answers "is it still going?" with the panel
     * closed. A separate layer from the ring and the glyph: progress changes
     * four times a second and rebuilding the other two to show it would throw
     * away their geometry each tick.
     *
     * <p>Drawn from the bounds it is handed, like every other drawable here, so
     * one implementation is correct on a phone and a tablet.
     */
    private static final class ProgressArcDrawable extends Drawable {

        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final RectF box = new RectF();
        /*
         * The two sweep stops arrive from the caller rather than being read off
         * Theme here. This is a static nested class, so there is no enclosing
         * instance to hand a Context to - and the accent is a per-theme value,
         * so a constant would be wrong on half the devices.
         */
        private final int arcStart;
        private final int arcEnd;
        private float fraction;
        private boolean shown;

        ProgressArcDrawable(int arcStart, int arcEnd) {
            this.arcStart = arcStart;
            this.arcEnd = arcEnd;
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeCap(Paint.Cap.ROUND);
        }

        void setFraction(float value) {
            float next = Math.max(0f, Math.min(1f, value));
            // Four updates a second on a sub-pixel arc: redrawing for a change
            // nobody can see just burns the frame.
            if (Math.abs(next - fraction) < 0.002f) return;
            fraction = next;
            invalidateSelf();
        }

        void setShown(boolean shown) {
            if (this.shown == shown) return;
            this.shown = shown;
            invalidateSelf();
        }

        @Override
        public void draw(Canvas canvas) {
            if (!shown) return;
            Rect bounds = getBounds();
            float side = Math.min(bounds.width(), bounds.height());
            if (side <= 0f) return;
            float stroke = side * 0.075f;
            float inset = stroke / 2f + side * 0.035f;
            float cx = bounds.exactCenterX();
            float cy = bounds.exactCenterY();
            box.set(cx - side / 2f + inset, cy - side / 2f + inset,
                    cx + side / 2f - inset, cy + side / 2f - inset);
            paint.setStrokeWidth(stroke);
            paint.setShader(new android.graphics.SweepGradient(cx, cy, arcStart, arcEnd));
            // Clockwise from twelve o'clock — the direction every platform
            // indicator fills, so the arc reads as elapsed rather than left.
            canvas.drawArc(box, -90f, Math.max(1f, 360f * fraction), false, paint);
            paint.setShader(null);
        }

        @Override
        public void setAlpha(int alpha) {
            paint.setAlpha(alpha);
        }

        @Override
        public void setColorFilter(ColorFilter filter) {
            paint.setColorFilter(filter);
        }

        @Override
        public int getOpacity() {
            return PixelFormat.TRANSLUCENT;
        }
    }

    /**
     * Bottom-right by default, but a drag moves the icon to the corner the
     * user picked. The size is fixed at {@link #entrySize()} so the icon does
     * not grow with the shared chip height — the panel used to be the same
     * floating chip, but the icon now stands alone and a 44dp square was
     * reading as a stray button rather than a single affordance.
     */
    private FrameLayout.LayoutParams downloadEntryLayout() {
        int size = entrySize();
        int edge = isTablet() ? dp(24) : dp(14);
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                size, size, entryGravity);
        int left = 0, top = 0, right = 0, bottom = 0;
        if ((entryGravity & Gravity.START) == Gravity.START) left = edge;
        else right = edge;
        if ((entryGravity & Gravity.TOP) == Gravity.TOP) top = edge;
        else bottom = edge;
        // Live drag offsets sit on top of the pinned edge so the icon tracks
        // the finger without leaving the visible viewport (clamped in onTouch).
        //
        // Both sides get the offset, mirrored: FrameLayout honours only the
        // margin on the side the gravity pins to (bottomMargin for BOTTOM,
        // rightMargin for END), so writing the one side alone left the icon
        // frozen in the bottom-right pin — a drag "worked" only after the
        // release snap flipped the gravity. Writing both makes every corner
        // move during the drag and costs nothing on the side gravity ignores.
        left += entryDragX;
        right -= entryDragX;
        top += entryDragY;
        bottom -= entryDragY;
        lp.setMargins(left, top, right, bottom);
        return lp;
    }

    private int entrySize() {
        // Smaller than the panel chip it replaced: it is the only thing on the
        // chrome strip now and a 40dp circle reads as an icon, not a button.
        return isTablet() ? dp(48) : dp(40);
    }

    private static int clampInt(int v, int lo, int hi) {
        return v < lo ? lo : (v > hi ? hi : v);
    }

    private void toggleDownloadPanel() {
        if (panelVisible) closeDownloadPanel();
        else openDownloadPanel();
    }

    private void launchDownloadsActivity() {
        // The download list lives in its own Activity so the WebView's
        // inline fullscreen never has to share its window with a sheet.
        // The in-app panel kept here is a fallback path used by the gate
        // / status / setup flows when an Activity launch would be wrong
        // (for example when the user is already in the gate).
        Intent intent = new Intent(this, DownloadsActivity.class);
        startActivity(intent);
    }

    private void openDownloadPanel() {
        buildDownloadPanelIfNeeded();
        refreshDownloadStates();
        renderDownloadPanel();
        downloadPanel.setVisibility(View.VISIBLE);
        downloadPanel.bringToFront();
        panelVisible = true;
        startPolling();
    }

    private void closeDownloadPanel() {
        panelVisible = false;
        stopPolling();
        if (downloadPanel != null) downloadPanel.setVisibility(View.GONE);
    }

    private void startPolling() {
        mainHandler.removeCallbacks(pollRunnable);
        mainHandler.postDelayed(pollRunnable, POLL_INTERVAL_MS);
    }

    private void stopPolling() {
        mainHandler.removeCallbacks(pollRunnable);
    }

    /** Build the panel once, lazily, and park it on the decor FrameLayout. */
    private void buildDownloadPanelIfNeeded() {
        if (downloadPanel != null) return;
        FrameLayout decor = (FrameLayout) getWindow().getDecorView();
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Theme.bg(MainActivity.this, 200));
        root.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                closeDownloadPanel();
            }
        });

        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setBackground(new GlassDrawable(dp(24)));
        card.setPadding(dp(16), dp(16), dp(16), dp(16));
        // Swallow taps so the scrim's dismiss handler does not fire on the card.
        card.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { }
        });
        FrameLayout.LayoutParams cardLp = downloadCardLayout();
        downloadCard = card;
        root.addView(card, cardLp);

        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        TextView title = new TextView(this);
        title.setText("下载中心");
        title.setTextColor(Theme.text(MainActivity.this));
        title.setTextSize(17f);
        title.getPaint().setFakeBoldText(true);
        LinearLayout titles = new LinearLayout(this);
        titles.setOrientation(LinearLayout.VERTICAL);
        titles.addView(title);
        // The real size of the offline directory, not an estimate: it is the
        // sum of what is actually on disk right now, so a save still running
        // shows the bytes it has already written instead of a round number.
        downloadStorage = new TextView(this);
        downloadStorage.setTextSize(11.5f);
        downloadStorage.setTextColor(Theme.text2(MainActivity.this));
        downloadStorage.setPadding(0, dp(2), 0, 0);
        titles.addView(downloadStorage);
        header.addView(titles, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        Button close = new Button(this);
        close.setText("关闭");
        close.setAllCaps(false);
        close.setTextSize(12f);
        close.setTextColor(Theme.text2(MainActivity.this));
        close.setStateListAnimator(null);
        android.graphics.drawable.GradientDrawable closeBg = new android.graphics.drawable.GradientDrawable();
        closeBg.setColor(Theme.fill(MainActivity.this));
        closeBg.setCornerRadius(dp(16));
        close.setBackground(PressFeedback.on(closeBg));
        close.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                closeDownloadPanel();
            }
        });
        header.addView(close, new LinearLayout.LayoutParams(dp(64), dp(34)));
        card.addView(header);

        downloadScroll = new ScrollView(this);
        downloadScroll.setFillViewport(true);
        downloadList = new LinearLayout(this);
        downloadList.setOrientation(LinearLayout.VERTICAL);
        downloadScroll.addView(downloadList);
        card.addView(downloadScroll, downloadScrollLayout());

        // The 保存媒体 action, moved off the decor and into the card. It sits
        // below the list because the list is what grows: pinned above it, a
        // long task list would push the button off the bottom of the card.
        downloadAction = buildDownloadAction();
        LinearLayout.LayoutParams actionLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(46));
        actionLp.setMargins(0, dp(12), 0, 0);
        card.addView(downloadAction, actionLp);
        refreshDownloadActionState();

        downloadPanel = root;
        decor.addView(root, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));
        root.setVisibility(View.GONE);
    }

    /**
     * Card geometry: a centred column. On a phone this is simply the window
     * minus its side margins; on a 1280dp tablet the width cap is what stops
     * the card — and every task row inside it — stretching edge to edge.
     */
    private FrameLayout.LayoutParams downloadCardLayout() {
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                contentMaxWidth(), FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER_HORIZONTAL | Gravity.TOP);
        lp.setMargins(0, dp(62), 0, dp(24));
        return lp;
    }

    /**
     * List geometry: the height the window actually leaves after the panel's
     * margins, the card's padding and its header. A fixed 360dp left most of a
     * landscape tablet empty; this grows with the window and shrinks rather
     * than overflowing one that is short.
     */
    private LinearLayout.LayoutParams downloadScrollLayout() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, downloadListHeight());
        lp.setMargins(0, dp(10), 0, 0);
        return lp;
    }

    /**
     * The panel's primary action. Direct files go to DownloadManager; HLS
     * manifests go to HlsDownloader, which fetches the segments rather than
     * saving the playlist text.
     */
    private Button buildDownloadAction() {
        Button button = new Button(this);
        button.setAllCaps(false);
        button.setTextSize(14f);
        button.setTextColor(Theme.onAccent(MainActivity.this));
        button.setMinimumWidth(0);
        button.setMinimumHeight(0);
        button.setStateListAnimator(null);
        button.setBackground(PressFeedback.on(primaryActionBackground(true)));
        button.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                enqueueVerifiedDownload();
            }
        });
        return button;
    }

    /**
     * Enabled/label state of the panel's 保存媒体 button, derived from the same
     * predicate as the entry icon's highlight. Called from every place the
     * captured URLs change — navigation, a fresh sniff, and each panel render —
     * so a stale "保存媒体" can never be tapped after the URL behind it was
     * cleared by a page navigation.
     */
    private void refreshDownloadActionState() {
        if (downloadAction == null) return;
        boolean hls = isHlsUrl(verifiedHlsUrl);
        boolean direct = isDirectDownloadUrl(verifiedDownloadUrl);
        boolean ready = hls || direct;
        boolean saved = findSavedTaskForCurrent() != null;
        if (saved) {
            downloadAction.setText("已保存 · 在下载中心");
        } else if (hls) {
            downloadAction.setText("保存媒体（HLS 分段下载）");
        } else if (direct) {
            downloadAction.setText("保存媒体");
        } else {
            downloadAction.setText("还没有检测到可保存的媒体");
        }
        downloadAction.setEnabled(ready);
        downloadAction.setTextColor(ready ? Theme.onAccent(MainActivity.this) : Theme.text(MainActivity.this, 150));
        downloadAction.setBackground(PressFeedback.on(primaryActionBackground(ready)));
        downloadAction.setContentDescription(
                ready ? "保存已验证的媒体文件" : "暂无可保存的媒体");
    }

    /**
     * The primary button's plate. A disabled state keeps the same geometry but
     * drops to a flat grey, so the control reads as unavailable rather than
     * merely dimmed — a disabled accent button still looks tappable.
     */
    private android.graphics.drawable.GradientDrawable primaryActionBackground(boolean ready) {
        android.graphics.drawable.GradientDrawable bg =
                new android.graphics.drawable.GradientDrawable(
                        android.graphics.drawable.GradientDrawable.Orientation.TL_BR,
                        ready ? Theme.BRAND_RAMP
                              : new int[]{Theme.fill(MainActivity.this),
                                          Theme.fill(MainActivity.this)});
        bg.setCornerRadius(dp(14));
        return bg;
    }

    private void renderDownloadPanel() {
        if (downloadList == null) return;
        // Refreshed on every render — including the poll — so the figure tracks
        // the bytes a running save is still writing, and so the action button
        // picks up a media URL sniffed after the panel was opened.
        if (downloadStorage != null) {
            downloadStorage.setText("离线目录 · " + formatBytes(offlineBytes()));
        }
        refreshDownloadActionState();
        int scrollY = downloadScroll == null ? 0 : downloadScroll.getScrollY();
        downloadList.removeAllViews();
        if (downloadTasks.isEmpty()) {
            TextView empty = new TextView(this);
            empty.setText("还没有下载任务。\n在播放页检测到媒体后，点上方的「保存媒体」。");
            empty.setTextColor(Theme.text2(MainActivity.this));
            empty.setTextSize(13f);
            empty.setLineSpacing(dp(3), 1f);
            empty.setPadding(dp(4), dp(20), dp(4), dp(20));
            downloadList.addView(empty);
            return;
        }
        for (int i = downloadTasks.size() - 1; i >= 0; i--) {
            downloadList.addView(buildDownloadRow(downloadTasks.get(i)));
        }
        if (downloadScroll != null) downloadScroll.setScrollY(scrollY);
    }

    /**
     * One row of the panel.
     *
     * <p>The buttons deliberately capture the task's stable id rather than the
     * {@code JSONObject} itself. The list is rebuilt from the store every
     * 800 ms, so by the time a tap lands the object this row was rendered from
     * may already be gone — dropped by the other Activity, or replaced by 重试
     * with a fresh record. Acting on that stale object is how a tap on one row
     * ended up deleting another task's bytes. Each handler therefore resolves
     * the id against the live list first and does nothing at all if it is gone.
     */
    private View buildDownloadRow(final JSONObject task) {
        String state = task.optString("state", STATE_RUNNING);
        final String taskId = task.optString("taskId", "");
        final long downloadId = task.optLong("downloadId", -1L);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.VERTICAL);
        android.graphics.drawable.GradientDrawable rowBg = new android.graphics.drawable.GradientDrawable();
        rowBg.setColor(Theme.fill(MainActivity.this));
        rowBg.setStroke(dp(1), Theme.sep(MainActivity.this));
        rowBg.setCornerRadius(dp(14));
        row.setBackground(rowBg);
        row.setPadding(dp(12), dp(12), dp(12), dp(10));
        LinearLayout.LayoutParams rowLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        rowLp.setMargins(0, dp(8), 0, 0);
        row.setLayoutParams(rowLp);

        TextView name = new TextView(this);
        name.setText(task.optString("fileName", "媒体文件"));
        name.setTextColor(Theme.text(MainActivity.this));
        name.setTextSize(14f);
        name.setSingleLine(true);
        name.setEllipsize(TextUtils.TruncateAt.MIDDLE);
        row.addView(name);

        TextView status = new TextView(this);
        status.setText(stateText(task));
        status.setTextSize(12.5f);
        status.setPadding(0, dp(4), 0, 0);
        if (STATE_SUCCESS.equals(state)) status.setTextColor(Theme.SUCCESS);
        else if (STATE_FAILED.equals(state) || STATE_LOST.equals(state)) {
            status.setTextColor(Theme.danger(MainActivity.this));
        } else status.setTextColor(Theme.accent(MainActivity.this));
        row.addView(status);

        if (STATE_RUNNING.equals(state)) {
            TextView prog = new TextView(this);
            prog.setText(progressText(task));
            prog.setTextColor(Theme.text2(MainActivity.this));
            prog.setTextSize(12f);
            prog.setPadding(0, dp(2), 0, 0);
            row.addView(prog);

            ProgressBar bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
            bar.setMax(100);
            // Same reason as the splash's bar: the tint has to come from our
            // palette, not from whichever platform theme the resources picked.
            bar.setProgressTintList(
                    android.content.res.ColorStateList.valueOf(Theme.accent(MainActivity.this)));
            bar.setProgressBackgroundTintList(android.content.res.ColorStateList.valueOf(
                    Theme.accent(MainActivity.this, 56)));
            int pct = 0;
            if (isHlsTask(task)) {
                int segTotal = task.optInt("segmentsTotal", 0);
                if (segTotal > 0) {
                    pct = (int) Math.min(100L,
                            task.optInt("segmentsDone", 0) * 100L / segTotal);
                }
            } else {
                long soFar = task.optLong("bytesSoFar", 0L);
                long total = task.optLong("bytesTotal", -1L);
                pct = total > 0 ? (int) Math.min(100L, soFar * 100L / total) : 0;
            }
            bar.setProgress(pct);
            LinearLayout.LayoutParams barLp = new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT, dp(4));
            barLp.setMargins(0, dp(6), 0, 0);
            row.addView(bar, barLp);
        }

        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        actions.setGravity(Gravity.END);
        LinearLayout.LayoutParams actionsLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        actionsLp.setMargins(0, dp(8), 0, 0);
        row.addView(actions, actionsLp);

        if (STATE_SUCCESS.equals(state)) {
            actions.addView(rowButton("打开", new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    JSONObject live = findDownloadTask(taskId, downloadId);
                    if (live == null) { renderDownloadPanel(); return; }
                    openDownloadedFile(live);
                }
            }));
        }
        if (STATE_FAILED.equals(state)) {
            actions.addView(rowButton("重试", new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    JSONObject live = findDownloadTask(taskId, downloadId);
                    if (live == null) { renderDownloadPanel(); return; }
                    retryDownload(live);
                }
            }));
        }
        // Rename is offered for a finished save and for a failed one — a failed
        // task still has its parts on disk — but never while a worker holds the
        // destination path, and never for a record whose bytes are already gone.
        if (isHlsTask(task) && !STATE_RUNNING.equals(state) && !STATE_LOST.equals(state)) {
            actions.addView(rowButton("重命名", new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    JSONObject live = findDownloadTask(taskId, downloadId);
                    if (live == null) { renderDownloadPanel(); return; }
                    renameDownload(live);
                }
            }));
        }
        if (STATE_LOST.equals(state)) {
            // The bytes are already gone, so there is nothing to open and
            // nothing to delete: only the stale record is left to drop, and
            // the user is the one who decides to drop it.
            actions.addView(rowButton("移除记录", new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    JSONObject live = findDownloadTask(taskId, downloadId);
                    if (live == null) { renderDownloadPanel(); return; }
                    removeDownloadRecord(live);
                }
            }));
            return row;
        }
        actions.addView(rowButton("删除", new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                JSONObject live = findDownloadTask(taskId, downloadId);
                if (live == null) { renderDownloadPanel(); return; }
                deleteDownload(live);
            }
        }));
        return row;
    }

    /**
     * Resolve a row's stable identity against the live list.
     *
     * <p>An HLS task is identified by its {@code taskId}; a DownloadManager
     * task by the {@code downloadId} the system handed out. Identity — the
     * object a row happened to be rendered from — is not stable across a
     * reload, so nothing that mutates the list may rely on it.
     */
    private JSONObject findDownloadTask(String taskId, long downloadId) {
        for (int i = 0; i < downloadTasks.size(); i++) {
            JSONObject task = downloadTasks.get(i);
            if (task == null) continue;
            if (isHlsTask(task)) {
                if (!taskId.isEmpty() && taskId.equals(task.optString("taskId", ""))) return task;
            } else if (downloadId >= 0 && downloadId == task.optLong("downloadId", -1L)) {
                return task;
            }
        }
        return null;
    }

    private Button rowButton(String label, View.OnClickListener onClick) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextSize(12f);
        b.setTextColor(Theme.text(MainActivity.this));
        b.setStateListAnimator(null);
        b.setMinWidth(0);
        b.setMinimumWidth(0);
        b.setMinimumHeight(0);
        b.setPadding(dp(14), 0, dp(14), 0);
        android.graphics.drawable.GradientDrawable bg = new android.graphics.drawable.GradientDrawable();
        bg.setColor(Theme.fill(MainActivity.this, 30));
        bg.setStroke(dp(1), Theme.fill(MainActivity.this, 44));
        bg.setCornerRadius(dp(16));
        b.setBackground(bg);
        b.setOnClickListener(onClick);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, dp(34));
        lp.setMargins(dp(8), 0, 0, 0);
        b.setLayoutParams(lp);
        return b;
    }

    private JSONObject newTaskRecord(long downloadId, String url, String pageUrl, String fileName) {
        JSONObject o = new JSONObject();
        try {
            o.put("kind", KIND_DIRECT);
            o.put("downloadId", downloadId);
            o.put("url", url);
            o.put("pageUrl", pageUrl == null ? "" : pageUrl);
            o.put("fileName", fileName);
            o.put("state", STATE_RUNNING);
            o.put("createdAt", System.currentTimeMillis());
            o.put("bytesTotal", -1L);
            o.put("bytesSoFar", 0L);
            o.put("reason", 0);
            o.put("localUri", "");
        } catch (JSONException e) {
            Log.w(TAG, "could not build download record", e);
        }
        return o;
    }

    /**
     * A record for an in-app HLS job. It deliberately carries no DownloadManager
     * id: {@code downloadId} is -1 because the system knows nothing about this
     * task, and its progress is reported by the worker instead of a cursor row.
     */
    private JSONObject newHlsTaskRecord(String taskId, String url, String pageUrl,
                                        String fileName, String localPath) {
        JSONObject o = new JSONObject();
        try {
            o.put("kind", KIND_HLS);
            o.put("downloadId", -1L);
            o.put("taskId", taskId);
            o.put("url", url);
            o.put("pageUrl", pageUrl == null ? "" : pageUrl);
            o.put("fileName", fileName);
            o.put("localPath", localPath);
            o.put("state", STATE_RUNNING);
            o.put("createdAt", System.currentTimeMillis());
            o.put("bytesTotal", -1L);
            o.put("bytesSoFar", 0L);
            o.put("segmentsDone", 0);
            o.put("segmentsTotal", 0);
            o.put("error", "");
            o.put("reason", 0);
            o.put("localUri", "");
        } catch (JSONException e) {
            Log.w(TAG, "could not build HLS download record", e);
        }
        return o;
    }

    /**
     * Re-read the stored task list.
     *
     * <p>An unreadable store is never a reason to empty the list: this method
     * used to {@code clear()} on a parse failure, and the next poll then wrote
     * that empty list straight back — one bad read silently cost the user every
     * download record, with nothing left to recover from. So the in-memory list
     * is only replaced once a new one has been parsed in full, and the string
     * that could not be read is quarantined under a separate key instead of
     * being overwritten.
     */
    private void loadDownloadTasks() {
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        String raw = prefs.getString(KEY_DOWNLOADS, "");
        if (raw == null || raw.isEmpty()) return;
        List<JSONObject> loaded = new ArrayList<JSONObject>();
        try {
            JSONArray arr = new JSONArray(raw);
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.optJSONObject(i);
                if (o == null) continue;
                // Records written before HLS support carry no "kind". They are
                // all DownloadManager tasks, so defaulting keeps a phone that
                // already has tasks stored working across the upgrade.
                if (!KIND_HLS.equals(o.optString("kind", ""))) o.put("kind", KIND_DIRECT);
                loaded.add(o);
            }
        } catch (JSONException e) {
            Log.w(TAG, "download task store unreadable, keeping the current list", e);
            prefs.edit().putString(KEY_DOWNLOADS + ".bad", raw).apply();
            return;
        }
        downloadTasks.clear();
        downloadTasks.addAll(loaded);
    }

    private void saveDownloadTasks() {
        JSONArray arr = new JSONArray();
        for (int i = 0; i < downloadTasks.size(); i++) arr.put(downloadTasks.get(i));
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                .putString(KEY_DOWNLOADS, arr.toString()).apply();
        // Every state change is persisted, so this is the one choke point that
        // sees all of them — the entry's progress ring rides along here rather
        // than each of the dozen call sites having to remember it.
        updateDownloadEntryProgress();
    }

    /**
     * Re-read every stored task from DownloadManager. A record whose id has no
     * cursor row is dropped: the UI must never keep claiming a task exists
     * after the system has forgotten it.
     *
     * <p>A finished task is then re-checked against the filesystem, because a
     * stored {@code state} of success is a memory of the past, not evidence:
     * the user can delete the file, or the system can reclaim it, and the row
     * has to say so.
     */
    private void refreshDownloadStates() {
        DownloadManager manager = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
        if (downloadTasks.isEmpty()) return;
        boolean changed = false;
        for (int i = downloadTasks.size() - 1; i >= 0; i--) {
            JSONObject task = downloadTasks.get(i);
            // An HLS task has no DownloadManager row by design (downloadId is
            // -1) — its state is owned by the in-app worker. Letting the
            // "no cursor row → drop the record" rule below see it would delete
            // every HLS task on the first refresh.
            if (isHlsTask(task)) continue;
            if (manager == null) continue;
            long id = task.optLong("downloadId", -1L);
            DownloadManager.Query query = new DownloadManager.Query().setFilterById(id);
            Cursor cursor = null;
            try {
                cursor = manager.query(query);
                if (cursor == null || !cursor.moveToFirst()) {
                    downloadTasks.remove(i);
                    changed = true;
                    continue;
                }
                int status = cursor.getInt(
                        cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS));
                long soFar = cursor.getLong(
                        cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR));
                long total = cursor.getLong(
                        cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_TOTAL_SIZE_BYTES));
                int reason = cursor.getInt(
                        cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_REASON));
                String localUri = cursor.getString(
                        cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_LOCAL_URI));
                task.put("bytesSoFar", soFar);
                task.put("bytesTotal", total);
                task.put("reason", reason);
                if (localUri != null && !localUri.isEmpty()) task.put("localUri", localUri);
                task.put("state", stateForStatus(status));
                changed = true;
            } catch (Exception e) {
                Log.w(TAG, "download query failed for id " + id, e);
            } finally {
                if (cursor != null) cursor.close();
            }
        }
        // Second pass, deliberately after the cursor work: a DownloadManager
        // row reporting STATUS_SUCCESSFUL is a memory, not proof, so the final
        // word on a finished task belongs to the filesystem — for direct tasks
        // and for HLS ones alike. A lost record is never removed here; only the
        // user drops it, through 移除记录.
        for (int i = 0; i < downloadTasks.size(); i++) {
            if (applyArtifactCheck(downloadTasks.get(i))) changed = true;
        }
        if (changed) saveDownloadTasks();
    }

    /**
     * Re-decide a finished task's state from the bytes on disk.
     *
     * @return true when the stored state actually changed, so the caller knows
     *         whether the record needs writing back.
     */
    private boolean applyArtifactCheck(JSONObject task) {
        String state = task.optString("state", STATE_RUNNING);
        // A task that is still running has no artifact yet by definition, and a
        // failed one already says why — neither is "lost".
        if (!STATE_SUCCESS.equals(state) && !STATE_LOST.equals(state)) return false;
        String next = artifactExists(task) ? STATE_SUCCESS : STATE_LOST;
        if (next.equals(state)) return false;
        try {
            task.put("state", next);
        } catch (JSONException e) {
            Log.w(TAG, "could not store lost state", e);
            return false;
        }
        return true;
    }

    /**
     * Whether the artifact a finished task points at is still a real file with
     * real bytes. The stored state is never consulted: this is the only place
     * that decides 已完成, and it asks the filesystem every time.
     */
    private boolean artifactExists(JSONObject task) {
        if (isHlsTask(task)) {
            // An in-app HLS save is a plain app-private file, so the honest
            // check is the file itself.
            String localPath = task.optString("localPath", "");
            if (localPath.isEmpty()) return false;
            File file = new File(localPath);
            return file.isFile() && file.length() > 0L;
        }
        String localUri = task.optString("localUri", "");
        if (!localUri.isEmpty() && uriHasBytes(Uri.parse(localUri))) return true;
        // The stored localUri can be stale or unreadable; the system's own
        // handle for the row is the fallback, and it too has to open.
        DownloadManager manager = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
        if (manager == null) return false;
        return uriHasBytes(manager.getUriForDownloadedFile(task.optLong("downloadId", -1L)));
    }

    /** A file:// artifact is its length; a content:// one has to still open. */
    private boolean uriHasBytes(Uri uri) {
        if (uri == null) return false;
        if ("file".equals(uri.getScheme())) {
            File file = new File(uri.getPath());
            return file.isFile() && file.length() > 0L;
        }
        try (android.content.res.AssetFileDescriptor fd =
                     getContentResolver().openAssetFileDescriptor(uri, "r")) {
            return fd != null && fd.getLength() != 0L;
        } catch (Exception e) {
            Log.w(TAG, "artifact is no longer readable", e);
            return false;
        }
    }

    /**
     * The offline directory's real size right now — a walk of what is actually
     * on disk, so a save still running is counted by the partial bytes it has
     * already written under {@code <dest>.parts} and in {@code <dest>.part},
     * rather than as zero or as the size it has not reached.
     */
    private long offlineBytes() {
        return treeBytes(offlineDir());
    }

    private static long treeBytes(File file) {
        if (file == null || !file.exists()) return 0L;
        if (file.isFile()) return Math.max(0L, file.length());
        long total = 0L;
        File[] children = file.listFiles();
        if (children != null) {
            for (File child : children) total += treeBytes(child);
        }
        return total;
    }

    private static String stateForStatus(int status) {
        if (status == DownloadManager.STATUS_SUCCESSFUL) return STATE_SUCCESS;
        if (status == DownloadManager.STATUS_FAILED) return STATE_FAILED;
        // PENDING / RUNNING / PAUSED are all "not finished yet"; a paused task
        // still shows its real bytes so far and its pause reason.
        return STATE_RUNNING;
    }

    private String stateText(JSONObject task) {
        String state = task.optString("state", STATE_RUNNING);
        // A lost artifact is its own honest outcome, for both kinds of task,
        // and it outranks whatever state was stored: the file decides.
        if (STATE_LOST.equals(state)) return "文件丢失";
        if (isHlsTask(task)) {
            if (STATE_SUCCESS.equals(state)) return "已完成";
            if (STATE_FAILED.equals(state)) {
                String error = task.optString("error", "");
                // The worker's own message is the real reason — expired link,
                // live stream, unsupported encryption — so it is shown as is
                // rather than flattened into a generic network error.
                return error.isEmpty() ? "失败" : "失败 · " + error;
            }
            return "下载中";
        }
        if (STATE_SUCCESS.equals(state)) return "已完成";
        if (STATE_FAILED.equals(state)) {
            // A failure is always rendered as a failure with a real reason.
            return "失败 · " + reasonText(task.optInt("reason", 0));
        }
        int reason = task.optInt("reason", 0);
        if (reason == DownloadManager.PAUSED_WAITING_FOR_NETWORK
                || reason == DownloadManager.PAUSED_QUEUED_FOR_WIFI
                || reason == DownloadManager.PAUSED_WAITING_TO_RETRY) {
            return "暂停 · " + reasonText(reason);
        }
        return "下载中";
    }

    /** Map DownloadManager's numeric reason to something a user can act on. */
    private static String reasonText(int reason) {
        switch (reason) {
            case DownloadManager.ERROR_FILE_ERROR: return "文件写入失败";
            case DownloadManager.ERROR_INSUFFICIENT_SPACE: return "空间不足";
            case DownloadManager.ERROR_HTTP_DATA_ERROR: return "网络错误";
            case DownloadManager.ERROR_UNHANDLED_HTTP_CODE: return "HTTP 错误";
            case DownloadManager.ERROR_TOO_MANY_REDIRECTS: return "重定向过多";
            case DownloadManager.ERROR_DEVICE_NOT_FOUND: return "存储不可用";
            case DownloadManager.ERROR_CANNOT_RESUME: return "无法续传";
            case DownloadManager.ERROR_FILE_ALREADY_EXISTS: return "文件已存在";
            // ERROR_BLOCKED (1010) is not part of the public SDK surface, so it
            // is matched by its stable numeric value.
            case 1010: return "被系统策略阻止";
            case DownloadManager.ERROR_UNKNOWN: return "未知错误";
            case DownloadManager.PAUSED_WAITING_TO_RETRY: return "等待重试";
            case DownloadManager.PAUSED_WAITING_FOR_NETWORK: return "等待网络";
            case DownloadManager.PAUSED_QUEUED_FOR_WIFI: return "等待 WiFi";
            case DownloadManager.PAUSED_UNKNOWN: return "已暂停";
            default: break;
        }
        // An HTTP status code arrives as the raw code (4xx/5xx).
        if (reason >= 400 && reason <= 599) return "HTTP 错误码 " + reason;
        return "错误码 " + reason;
    }

    private String progressText(JSONObject task) {
        if (isHlsTask(task)) {
            // An HLS task has no Content-Length to work from, so the segment
            // count is the honest unit of progress.
            return "已完成 " + task.optInt("segmentsDone", 0) + " / "
                + task.optInt("segmentsTotal", 0) + " 段 · "
                + formatBytes(task.optLong("bytesSoFar", 0L));
        }
        long soFar = task.optLong("bytesSoFar", 0L);
        long total = task.optLong("bytesTotal", -1L);
        if (total > 0) {
            int pct = (int) Math.min(100L, soFar * 100L / total);
            return pct + "% · " + formatBytes(soFar) + " / " + formatBytes(total);
        }
        return formatBytes(soFar) + " / 未知大小";
    }

    /** Package-visible: the download notification shows the same format. */
    static String formatBytes(long bytes) {
        if (bytes < 0) return "未知大小";
        if (bytes < 1024) return bytes + " B";
        double kb = bytes / 1024.0;
        if (kb < 1024) return String.format(Locale.US, "%.1f KB", kb);
        double mb = kb / 1024.0;
        if (mb < 1024) return String.format(Locale.US, "%.1f MB", mb);
        return String.format(Locale.US, "%.2f GB", mb / 1024.0);
    }

    private static String mimeOf(String name) {
        String n = name == null ? "" : name.toLowerCase(Locale.US);
        // The in-app HLS output is concatenated MPEG-TS, so it is reported as
        // exactly that rather than dressed up as an .mp4.
        if (n.endsWith(".ts")) return "video/mp2t";
        if (n.endsWith(".mp4")) return "video/mp4";
        if (n.endsWith(".webm")) return "video/webm";
        if (n.endsWith(".m4a")) return "audio/mp4";
        if (n.endsWith(".mp3")) return "audio/mpeg";
        if (n.endsWith(".aac")) return "audio/aac";
        if (n.endsWith(".ogg")) return "audio/ogg";
        if (n.endsWith(".wav")) return "audio/wav";
        if (n.endsWith(".flac")) return "audio/flac";
        return "application/octet-stream";
    }

    private void openDownloadedFile(JSONObject task) {
        Uri uri = null;
        if (isHlsTask(task)) {
            // An in-app HLS save lives in app-private storage, which
            // DownloadManager — and therefore localUri — knows nothing about.
            // It still has to leave as a content:// URI, not a file:// one.
            String localPath = task.optString("localPath", "");
            File file = localPath.isEmpty() ? null : new File(localPath);
            if (file != null && file.isFile()) uri = DownloadFileProvider.uriForFile(file);
        } else {
            String localUri = task.optString("localUri", "");
            if (localUri != null && !localUri.isEmpty()) {
                Uri parsed = Uri.parse(localUri);
                if ("file".equals(parsed.getScheme())) {
                    // file:// → ACTION_VIEW is FileUriExposedException on API 24+.
                    uri = DownloadFileProvider.uriForFile(parsed);
                } else {
                    uri = parsed;
                }
            }
            if (uri == null) {
                DownloadManager manager = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                if (manager != null) {
                    uri = manager.getUriForDownloadedFile(task.optLong("downloadId", -1L));
                }
            }
        }
        if (uri == null) {
            Toast.makeText(this, "找不到已下载的文件", Toast.LENGTH_SHORT).show();
            return;
        }
        String fileName = task.optString("fileName", "");
        String mime = mimeOf(fileName);
        // An in-app HLS save is concatenated MPEG-TS reported as video/mp2t,
        // and nothing on a bare device (or plenty of tablets) claims that
        // type: ACTION_VIEW would throw ActivityNotFoundException and 打开
        // would silently do nothing on a row that says 已完成. The shell plays
        // it itself instead of depending on a third-party player being
        // installed. A direct download keeps going to the system handler, and
        // only falls back in-app when nothing resolves it either.
        if (isHlsTask(task) || !hasExternalViewer(uri, mime)) {
            if (startInAppPlayer(uri, fileName)) return;
            Toast.makeText(this, "无法播放：系统里没有能打开该格式的应用",
                    Toast.LENGTH_LONG).show();
            return;
        }
        try {
            Intent view = new Intent(Intent.ACTION_VIEW);
            view.setDataAndType(uri, mime);
            view.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            startActivity(view);
        } catch (Exception e) {
            // Resolution can still race an app being disabled, so the failure
            // path falls back rather than surfacing a crash.
            Log.w(TAG, "external open failed, falling back in-app", e);
            if (!startInAppPlayer(uri, fileName)) {
                Toast.makeText(this, "无法打开文件：" + e.getMessage(),
                        Toast.LENGTH_SHORT).show();
            }
        }
    }

    /**
     * Does anything actually claim this URI + type? Asked <em>before</em>
     * {@code startActivity}, because an unresolved {@code ACTION_VIEW} throws
     * rather than failing softly — which is precisely how 打开 came to do
     * nothing at all on a finished HLS task.
     */
    private boolean hasExternalViewer(Uri uri, String mime) {
        Intent probe = new Intent(Intent.ACTION_VIEW);
        probe.setDataAndType(uri, mime);
        probe.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        try {
            return getPackageManager()
                    .resolveActivity(probe, PackageManager.MATCH_DEFAULT_ONLY) != null;
        } catch (Exception e) {
            Log.w(TAG, "resolveActivity failed for " + mime, e);
            return false;
        }
    }

    /** Launch the bundled player for a saved artifact; false if even that fails. */
    private boolean startInAppPlayer(Uri uri, String fileName) {
        try {
            Intent play = new Intent(this, PlayerActivity.class);
            play.setDataAndType(uri, mimeOf(fileName));
            play.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            play.putExtra(PlayerActivity.EXTRA_URI, uri.toString());
            play.putExtra(PlayerActivity.EXTRA_TITLE, fileName);
            startActivity(play);
            return true;
        } catch (Exception e) {
            Log.w(TAG, "in-app player could not start", e);
            return false;
        }
    }

    /** Re-enqueue a failed task's stored url and replace the dead record. */
    private void retryDownload(JSONObject task) {
        if (isHlsTask(task)) {
            // A failed HLS job left its .parts directory in place, so restarting
            // the worker resumes from the segments that already arrived instead
            // of re-fetching them.
            String hlsUrl = task.optString("url", "");
            if (!isHlsUrl(hlsUrl)) {
                Toast.makeText(this, "原始地址已不可用，无法重试", Toast.LENGTH_SHORT).show();
                return;
            }
            try {
                task.put("state", STATE_RUNNING);
                task.put("error", "");
                task.put("segmentsDone", 0);
                task.put("bytesSoFar", 0L);
            } catch (JSONException e) {
                Log.w(TAG, "could not reset HLS task for retry", e);
            }
            saveDownloadTasks();
            startHlsJob(task);
            renderDownloadPanel();
            Toast.makeText(this, "已重新加入下载", Toast.LENGTH_SHORT).show();
            return;
        }
        String url = task.optString("url", "");
        if (!isDirectDownloadUrl(url)) {
            Toast.makeText(this, "原始地址已不可用，无法重试", Toast.LENGTH_SHORT).show();
            return;
        }
        String fileName = task.optString("fileName", downloadName(url));
        String pageUrl = task.optString("pageUrl", "");
        try {
            long newId = enqueueDownload(url, pageUrl, fileName);
            downloadTasks.remove(task);
            downloadTasks.add(newTaskRecord(newId, url, pageUrl, fileName));
            saveDownloadTasks();
            refreshDownloadStates();
            renderDownloadPanel();
            Toast.makeText(this, "已重新加入下载", Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            Log.w(TAG, "retry download failed", e);
            Toast.makeText(this, "重试失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    /**
     * 移除记录 drops the stored row and nothing else. The artifact is already
     * gone — that is why the row says 文件丢失 — so this deliberately deletes no
     * bytes, leaves any resume material alone, and does not touch the
     * DownloadManager row. The user asked for the record to go, not the data.
     */
    private void removeDownloadRecord(JSONObject task) {
        downloadTasks.remove(task);
        saveDownloadTasks();
        renderDownloadPanel();
    }

    /**
     * Rename a saved artifact, and its resume material with it.
     *
     * <p>Offered because the name is the user's, not the CDN's. A save made
     * before the title-based naming existed still carries a timestamp, and the
     * only other way to fix it would be to download a gigabyte again. The parts
     * directory moves alongside, so a failed task renamed here is still
     * resumable afterwards — and, just as importantly, so a later save of the
     * same name cannot silently resume from segments that belong to something
     * else.
     *
     * <p>Not offered while a task is running: the worker holds the destination
     * path it was started with, so moving the file out from under it would
     * scatter the segments it is still writing.
     */
    private void renameDownload(final JSONObject task) {
        final String localPath = task.optString("localPath", "");
        final File dest = localPath.isEmpty() ? null : new File(localPath);
        if (dest == null) {
            Toast.makeText(this, "这条记录没有可重命名的文件", Toast.LENGTH_SHORT).show();
            return;
        }
        // Same product modal the download centre shows (see productDialog):
        // a dim scrim, one glass card, and a filled 保存 pill the eye lands
        // on first — the platform AlertDialog's muted pair is retired.
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);

        TextView title = new TextView(this);
        title.setText("重命名");
        title.setTextSize(17f);
        title.setTextColor(Theme.text(MainActivity.this));
        title.setTypeface(title.getTypeface(), android.graphics.Typeface.BOLD);
        LinearLayout.LayoutParams titleLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        titleLp.bottomMargin = dp(12);
        root.addView(title, titleLp);

        final EditText input = new EditText(this);
        input.setSingleLine(true);
        String current = dest.getName();
        int dot = current.lastIndexOf('.');
        input.setText(dot > 0 ? current.substring(0, dot) : current);
        input.setSelection(input.getText().length());
        input.setTextColor(Theme.text(MainActivity.this));
        input.setHintTextColor(Theme.text3(MainActivity.this));
        input.setHint("新的文件名（不含后缀）");
        android.graphics.drawable.GradientDrawable inputBg =
                new android.graphics.drawable.GradientDrawable();
        inputBg.setCornerRadius(dp(12));
        inputBg.setColor(Theme.surface2(MainActivity.this));
        inputBg.setStroke(dp(1), Theme.sepStrong(MainActivity.this));
        input.setBackground(inputBg);
        int inPad = dp(14);
        input.setPadding(inPad, inPad, inPad, inPad);
        LinearLayout.LayoutParams inLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        inLp.bottomMargin = dp(22);
        root.addView(input, inLp);

        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        buttons.setGravity(Gravity.END);

        Button cancelBtn = new Button(this);
        cancelBtn.setAllCaps(false);
        cancelBtn.setText("取消");
        cancelBtn.setTextColor(Theme.text2(MainActivity.this));
        android.graphics.drawable.GradientDrawable cancelBg =
                new android.graphics.drawable.GradientDrawable();
        cancelBg.setCornerRadius(dp(12));
        cancelBg.setColor(Theme.surface2(MainActivity.this));
        cancelBg.setStroke(dp(1), Theme.sepStrong(MainActivity.this));
        cancelBtn.setBackground(cancelBg);
        cancelBtn.setPadding(dp(20), dp(12), dp(20), dp(12));
        LinearLayout.LayoutParams cancelLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        cancelLp.rightMargin = dp(10);
        buttons.addView(cancelBtn, cancelLp);

        final Button saveBtn = new Button(this);
        saveBtn.setAllCaps(false);
        saveBtn.setText("保存");
        saveBtn.setTextColor(Theme.onAccent(MainActivity.this));
        saveBtn.setTypeface(saveBtn.getTypeface(), android.graphics.Typeface.BOLD);
        android.graphics.drawable.GradientDrawable saveBg =
                new android.graphics.drawable.GradientDrawable(
                        android.graphics.drawable.GradientDrawable.Orientation.LEFT_RIGHT,
                        new int[]{Theme.accent(MainActivity.this), Theme.accentAlt(MainActivity.this)});
        saveBg.setCornerRadius(dp(12));
        saveBtn.setBackground(saveBg);
        saveBtn.setPadding(dp(26), dp(12), dp(26), dp(12));
        buttons.addView(saveBtn, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        root.addView(buttons, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        final Dialog dialog = productDialog(root);
        cancelBtn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { dialog.dismiss(); }
        });
        saveBtn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                applyRename(task, dest, input.getText().toString());
                dialog.dismiss();
            }
        });
        dialog.show();
    }

    private void applyRename(JSONObject task, File dest, String raw) {
        String stem = sanitizeStem(raw);
        if (stem.isEmpty()) {
            Toast.makeText(this, "文件名不能为空", Toast.LENGTH_SHORT).show();
            return;
        }
        String name = dest.getName();
        int dot = name.lastIndexOf('.');
        String ext = dot > 0 ? name.substring(dot) : ".ts";
        File target = uniqueDest(dest.getParentFile(), stem, ext);
        if (target.equals(dest)) return;
        File oldParts = new File(dest.getAbsolutePath() + ".parts");
        File oldPartial = new File(dest.getAbsolutePath() + ".part");
        try {
            if (dest.isFile() && !dest.renameTo(target)) {
                Toast.makeText(this, "重命名失败：无法移动文件", Toast.LENGTH_LONG).show();
                return;
            }
            if (oldParts.isDirectory() && !oldParts.renameTo(
                    new File(target.getAbsolutePath() + ".parts"))) {
                Log.w(TAG, "resume directory left behind after rename: " + oldParts);
            }
            if (oldPartial.isFile()) {
                oldPartial.renameTo(new File(target.getAbsolutePath() + ".part"));
            }
            task.put("fileName", target.getName());
            task.put("localPath", target.getAbsolutePath());
            task.put("localUri", "file://" + target.getAbsolutePath());
        } catch (Exception e) {
            Log.w(TAG, "rename failed", e);
            Toast.makeText(this, "重命名失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
            return;
        }
        saveDownloadTasks();
        renderDownloadPanel();
        Toast.makeText(this, "已重命名为 " + target.getName(), Toast.LENGTH_SHORT).show();
    }

    /**
     * 删除 a task: confirm first, then drop the record and its bytes.
     *
     * <p>The guard matters because this ran on whatever object the tapped row
     * was rendered from, which the 800 ms reload may already have replaced.
     * Deleting "a record" that is no longer in the list meant deleting paths
     * that now belong to a different task — and the deletion is irreversible,
     * up to gigabytes at a time. So the id is resolved against the live list
     * before anything is touched, and again after the confirmation, because the
     * dialog does not stop the poll.
     */
    private void deleteDownload(JSONObject task) {
        JSONObject target = findDownloadTask(
                task.optString("taskId", ""), task.optLong("downloadId", -1L));
        if (target == null) {
            if (panelVisible) renderDownloadPanel();
            return;
        }
        confirmDelete(target);
    }

    private void confirmDelete(final JSONObject task) {
        final String taskId = task.optString("taskId", "");
        final long downloadId = task.optLong("downloadId", -1L);
        // Same product modal as the download centre's (see productDialog): a
        // dim scrim, one glass card, and a red delete pill instead of a muted
        // "positive" tint — the platform pair is retired.
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);

        TextView title = new TextView(this);
        title.setText("删除下载");
        title.setTextSize(17f);
        title.setTextColor(Theme.text(MainActivity.this));
        title.setTypeface(title.getTypeface(), android.graphics.Typeface.BOLD);
        LinearLayout.LayoutParams titleLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        titleLp.bottomMargin = dp(12);
        root.addView(title, titleLp);

        TextView desc = new TextView(this);
        desc.setText("将删除该下载记录以及已下载的文件与分段缓存，\n删除后不可恢复。");
        desc.setTextSize(13f);
        desc.setTextColor(Theme.text2(MainActivity.this));
        desc.setLineSpacing(dp(3), 1f);
        LinearLayout.LayoutParams descLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        descLp.bottomMargin = dp(22);
        root.addView(desc, descLp);

        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        buttons.setGravity(Gravity.END);

        Button cancelBtn = new Button(this);
        cancelBtn.setAllCaps(false);
        cancelBtn.setText("取消");
        cancelBtn.setTextColor(Theme.text2(MainActivity.this));
        android.graphics.drawable.GradientDrawable cancelBg =
                new android.graphics.drawable.GradientDrawable();
        cancelBg.setCornerRadius(dp(12));
        cancelBg.setColor(Theme.surface2(MainActivity.this));
        cancelBg.setStroke(dp(1), Theme.sepStrong(MainActivity.this));
        cancelBtn.setBackground(cancelBg);
        cancelBtn.setPadding(dp(20), dp(12), dp(20), dp(12));
        LinearLayout.LayoutParams cancelLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        cancelLp.rightMargin = dp(10);
        buttons.addView(cancelBtn, cancelLp);

        final Button deleteBtn = new Button(this);
        deleteBtn.setAllCaps(false);
        deleteBtn.setText("删除");
        deleteBtn.setTextColor(Theme.ON_DANGER);
        deleteBtn.setTypeface(deleteBtn.getTypeface(), android.graphics.Typeface.BOLD);
        // Red-to-rose gradient: a destructive action should not borrow the
        // brand blue used by 保存, or the eye reads it as a confirm action.
        android.graphics.drawable.GradientDrawable deleteBg =
                new android.graphics.drawable.GradientDrawable(
                        android.graphics.drawable.GradientDrawable.Orientation.LEFT_RIGHT,
                        new int[]{Theme.danger(MainActivity.this), Theme.DANGER_ALT});
        deleteBg.setCornerRadius(dp(12));
        deleteBtn.setBackground(deleteBg);
        deleteBtn.setPadding(dp(26), dp(12), dp(26), dp(12));
        buttons.addView(deleteBtn, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        root.addView(buttons, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        final Dialog dialog = productDialog(root);
        cancelBtn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { dialog.dismiss(); }
        });
        deleteBtn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                performDelete(taskId, downloadId);
                dialog.dismiss();
            }
        });
        dialog.show();
    }

    /**
     * The shell both destructive paths share: a dim scrim over the page, one
     * centred glass card, nothing else. Same implementation the download
     * centre shows — one look, and the dialog reads as one more surface of
     * the app rather than as a system sticker.
     */
    private Dialog productDialog(View content) {
        FrameLayout scrim = new FrameLayout(this);
        scrim.setBackgroundColor(Theme.scrim(MainActivity.this, 70));
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setBackground(new GlassDrawable(dp(24)));
        card.setElevation(dp(22));
        card.setPadding(dp(20), dp(20), dp(20), dp(20));
        int width = Math.min(
                getResources().getDisplayMetrics().widthPixels - dp(56), dp(380));
        FrameLayout.LayoutParams cardLp = new FrameLayout.LayoutParams(
                width, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.CENTER);
        scrim.addView(card, cardLp);
        card.addView(content, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        final Dialog dialog = new Dialog(this, android.R.style.Theme_Panel);
        dialog.getWindow().setBackgroundDrawable(
                new android.graphics.drawable.ColorDrawable(android.graphics.Color.TRANSPARENT));
        dialog.setContentView(scrim, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        scrim.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { dialog.dismiss(); }
        });
        card.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { }
        });
        return dialog;
    }

    private void performDelete(String taskId, long downloadId) {
        JSONObject task = findDownloadTask(taskId, downloadId);
        if (task == null) {
            if (panelVisible) renderDownloadPanel();
            return;
        }
        if (isHlsTask(task)) {
            // The worker reads the flag before each segment and then aborts,
            // leaving whatever it had already written on disk.
            HlsJobs.cancel(taskId);
            if (!taskId.isEmpty()) HlsJobs.remove(taskId);
            String localPath = task.optString("localPath", "");
            if (!localPath.isEmpty()) {
                // Deleting the task deletes its resume material too, otherwise
                // a later save would silently reuse stale segments.
                deleteTree(new File(localPath + ".parts"));
                new File(localPath + ".part").delete();
                new File(localPath).delete();
            }
            downloadTasks.remove(task);
            saveDownloadTasks();
            renderDownloadPanel();
            return;
        }
        long id = task.optLong("downloadId", -1L);
        try {
            DownloadManager manager = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
            if (manager != null) manager.remove(id);
        } catch (Exception e) {
            Log.w(TAG, "remove download failed", e);
        }
        downloadTasks.remove(task);
        saveDownloadTasks();
        renderDownloadPanel();
    }

    /** Completion is pushed by the system — no need to wait for the next poll. */
    private final class DownloadCompleteReceiver extends BroadcastReceiver {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (intent == null
                    || !DownloadManager.ACTION_DOWNLOAD_COMPLETE.equals(intent.getAction())) {
                return;
            }
            long id = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1L);
            if (id <= 0) return;
            Log.i(TAG, "download complete broadcast: " + id);
            refreshDownloadStates();
            JSONObject finished = findDownloadTask("", id);
            if (finished != null) {
                String state = finished.optString("state", STATE_FAILED);
                notifyDownloadFinished("direct-" + id,
                        finished.optString("fileName", "视频文件"), STATE_SUCCESS.equals(state));
            }
            saveDownloadTasks();
            if (panelVisible) renderDownloadPanel();
        }
    }

    private void registerDownloadReceiver() {
        if (downloadReceiver != null) return;
        BroadcastReceiver receiver = new DownloadCompleteReceiver();
        IntentFilter filter = new IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE);
        if (Build.VERSION.SDK_INT >= 33) {
            // A context-registered receiver must declare its export flag on
            // API 33+ or registration throws SecurityException.
            registerReceiver(receiver, filter, Context.RECEIVER_EXPORTED);
        } else {
            registerReceiver(receiver, filter);
        }
        downloadReceiver = receiver;
    }

    private void unregisterDownloadReceiver() {
        if (downloadReceiver == null) return;
        try {
            unregisterReceiver(downloadReceiver);
        } catch (Exception e) {
            Log.w(TAG, "unregister download receiver failed", e);
        }
        downloadReceiver = null;
    }

    // ----------------------------------------------------------- fullscreen

    private void enterFullscreen(View view, WebChromeClient.CustomViewCallback callback) {
        if (customView != null) {
            // Already fullscreen (a second <video> asked while one was up).
            callback.onCustomViewHidden();
            return;
        }
        originalOrientation = getRequestedOrientation();
        originalSystemUi = getWindow().getDecorView().getSystemUiVisibility();
        // The inline fullscreen view arrives before its video has a frame;
        // left to the default it composites over white for that first beat.
        // The same dark as everything else means the worst case is dark.
        view.setBackgroundColor(Theme.bg(this));
        customView = view;
        customViewCallback = callback;

        FrameLayout decor = (FrameLayout) getWindow().getDecorView();
        decor.addView(customView, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));
        if (webView != null) webView.setVisibility(View.GONE);

        // The floating download-centre entry lives in the decor too. Leaving
        // it visible would put a 44dp square on top of the user's fullscreen
        // video — exactly the "下载的button影响我全屏了" complaint. The panel
        // itself is also a decor child, so close it here for the same reason:
        // an open panel from before the fullscreen gesture must not survive
        // into it. Both states are restored on exit.
        if (downloadEntry != null) {
            entryWasVisibleBeforeFullscreen =
                    downloadEntry.getVisibility() == View.VISIBLE;
            downloadEntry.setVisibility(View.GONE);
        }
        collapseFabMenu();
        panelWasVisibleBeforeFullscreen = panelVisible;
        if (panelVisible) closeDownloadPanel();

        // Immersive + landscape + screen-on: the three things a fullscreen
        // video actually needs from the host.
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE);
    }

    private void exitFullscreen() {
        if (customView == null) return;
        FrameLayout decor = (FrameLayout) getWindow().getDecorView();
        decor.removeView(customView);
        customView = null;
        if (webView != null) webView.setVisibility(View.VISIBLE);
        // Restore the chrome we hid in enterFullscreen, but only if it was
        // visible in the first place — a layout pass before our first frame
        // (e.g. on rotation before any page ever loaded) must not conjure a
        // floating button out of thin air.
        if (downloadEntry != null && entryWasVisibleBeforeFullscreen) {
            downloadEntry.setVisibility(View.VISIBLE);
        }
        if (downloadEntry != null) entryWasVisibleBeforeFullscreen = false;
        if (panelWasVisibleBeforeFullscreen) openDownloadPanel();
        panelWasVisibleBeforeFullscreen = false;
        getWindow().getDecorView().setSystemUiVisibility(originalSystemUi);
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setRequestedOrientation(originalOrientation);
        if (customViewCallback != null) {
            customViewCallback.onCustomViewHidden();
            customViewCallback = null;
        }
    }

    private final class MediaChromeClient extends WebChromeClient {
        @Override
        public void onShowCustomView(View view, CustomViewCallback callback) {
            enterFullscreen(view, callback);
        }

        @Override
        public void onShowCustomView(View view, int requestedOrientation,
                                     CustomViewCallback callback) {
            enterFullscreen(view, callback);
        }

        @Override
        public void onHideCustomView() {
            exitFullscreen();
        }

        @Override
        public void onProgressChanged(WebView view, int newProgress) {
            if (progressBar != null) progressBar.setProgress(newProgress);
            if (newProgress < 100) return;
            // Let the page's own first paint land before lifting the
            // curtain, otherwise the swap flashes an empty surface.  A
            // progress event from a superseded navigation must not hide
            // the next page's splash.
            final int generation = splashGeneration;
            final boolean external =
                    currentFrameUrl != null && currentFrameUrl.startsWith("http");
            if (external) {
                // Progress is a lie on an external page: its 100% often
                // arrives while the document is still an unpainted white
                // body, and lifting here is exactly the 白屏 flash. The
                // reveal for those pages waits for onPageFinished (see
                // onPageFinished below); progress only feeds the bar.
                return;
            }
            mainHandler.postDelayed(new Runnable() {
                @Override
                public void run() {
                    if (generation == splashGeneration) hideSplash();
                }
            }, 180);
        }
    }

    private final class ShieldWebViewClient extends WebViewClient {
        private int pageGeneration;
        private String failedUrl = "";

        // Inspect only the trusted top document on the UI thread. The
        // returned URLs are real fullscreen player frames, not arbitrary
        // subresource requests (which would accidentally trust ad scripts).
        private void inspectPlayerFrames(final WebView view, final int generation, final int attempt) {
            if (view != webView || generation != pageGeneration || isFinishing()) return;
            final String parentUrl = view.getUrl();
            if (!shield.mayOpenPlayerHop(ShieldRules.hostOf(parentUrl))) return;
            view.evaluateJavascript("JSON.stringify(Array.from(document.querySelectorAll('iframe'))"
                    + ".filter(function(f){var r=f.getBoundingClientRect();return r.width>=160&&r.height>=90"
                    + "&&(f.hasAttribute('allowfullscreen')||(f.getAttribute('allow')||'').indexOf('fullscreen')>=0);})"
                    + ".map(function(f){return f.src;}).slice(0,6))", new android.webkit.ValueCallback<String>() {
                @Override public void onReceiveValue(String value) {
                    if (view != webView || generation != pageGeneration || !parentUrl.equals(view.getUrl())) return;
                    try {
                        Object decoded = new org.json.JSONTokener(value).nextValue();
                        JSONArray frames = new JSONArray(String.valueOf(decoded));
                        for (int i = 0; i < frames.length(); i++) {
                            String frame = frames.optString(i, "");
                            String host = ShieldRules.hostOf(frame);
                            if (!shield.isPlayerHost(host)
                                    && shield.trustHoppedPlayerUrl(ShieldRules.hostOf(parentUrl), frame)) {
                                // This frame may already have received the shield's empty
                                // refusal. Reload that exact element after registering it.
                                final String quoted = JSONObject.quote(frame);
                                view.evaluateJavascript("document.querySelectorAll('iframe').forEach(function(f){"
                                        + "if(f.src===" + quoted + ")f.src=" + quoted + ";})", null);
                                Log.i(TAG, "restored declared player iframe: " + host);
                            }
                        }
                    } catch (Exception e) { Log.w(TAG, "player frame inspection failed", e); }
                    if (attempt < 24) mainHandler.postDelayed(new Runnable() {
                        @Override public void run() { inspectPlayerFrames(view, generation, attempt + 1); }
                    }, 750);
                }
            });
        }

        @Override public void onPageFinished(WebView view, String url) {
            super.onPageFinished(view, url);
            inspectPlayerFrames(view, pageGeneration, 0);
            if (url == null || !url.startsWith("http")) return;
            // The external reveal: finish + a short settle. The load event is
            // the one honest "this document is done" moment — a page that
            // paints a white body before its own dark CSS arrives has already
            // darkened by the settle, and a page that never finishes is
            // released by the hard cap set in onPageStarted instead of
            // keeping the curtain hostage forever.
            final int generation = pageGeneration;
            mainHandler.postDelayed(new Runnable() {
                @Override public void run() {
                    if (generation == pageGeneration) hideSplash();
                }
            }, EXTERNAL_REVEAL_SETTLE_MS);
        }

        @Override public void onReceivedError(WebView view, WebResourceRequest request,
                                               android.webkit.WebResourceError error) {
            if (request.isForMainFrame()) showLoadFailure(view, request.getUrl().toString(),
                    "连接失败（" + error.getErrorCode() + "）", String.valueOf(error.getDescription()));
        }

        @Override public void onReceivedHttpError(WebView view, WebResourceRequest request,
                                                   WebResourceResponse response) {
            if (request.isForMainFrame()) showLoadFailure(view, request.getUrl().toString(),
                    "播放源暂时不可用", "服务器返回 HTTP " + response.getStatusCode() + "，可以重试或返回切换其他源。");
        }

        private void showLoadFailure(WebView view, String url, String title, String detail) {
            if (url.equals(failedUrl) || !url.startsWith("http")) return;
            failedUrl = url;
            pageGeneration++;
            hideSplash();
            Log.w(TAG, "player load failed: " + url + " " + detail);
            String safeUrl = htmlEscape(url);
            String html = "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
                    + "<meta name='color-scheme' content='dark'><title>播放连接失败</title>"
                    + "<style>body{margin:0;min-height:100vh;box-sizing:border-box;display:grid;place-items:center;"
                    + "background:#0a0b0f;color:#f1f3f8;font:15px/1.6 system-ui;padding:24px}"
                    + "main{max-width:420px;padding:28px;border-radius:28px;border:1px solid #ffffff30;"
                    + "background:linear-gradient(145deg,#363e50,#171b25);box-shadow:0 24px 70px #0008}"
                    + "h1{font-size:22px}p{color:#a2acbd;overflow-wrap:anywhere}a{display:block;padding:13px;"
                    + "margin-top:12px;border-radius:16px;text-align:center;color:#f1f3f8;background:#ffffff12;text-decoration:none}"
                    + "a:first-of-type{background:#7c9cff;color:#06101f}small{color:#a2acbd}</style>"
                    + "<main><small>ASMR 收藏馆 · 播放状态</small><h1>" + htmlEscape(title) + "</h1><p>"
                    + htmlEscape(detail) + "</p><p>" + htmlEscape(ShieldRules.hostOf(url))
                    + "</p><a href='" + safeUrl + "'>重新连接</a><a href='"
                    + htmlEscape(lastPlayPage.isEmpty() ? "file:///android_asset/discover.html" : lastPlayPage)
                    + "'>返回选择播放源</a></main>";
            view.loadDataWithBaseURL(url, html, "text/html", "UTF-8", null);
        }

        /**
         * The URL of a top-level document we answered with the shield's own
         * explanation page. The history hook must not bounce that page away,
         * or the explanation flashes and the user is dumped back where they
         * started with no idea what happened.
         *
         * <p>Written from the worker thread, read from the main thread.
         */
        private volatile String explainedFrameUrl = "";

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            super.onPageStarted(view, url, favicon);
            pageGeneration++;
            if (isOurPlayPage(url)) lastPlayPage = url;
            if (url != null && !url.equals(failedUrl)) failedUrl = "";
            // about:blank is only the renderer warm-up; don't flash a splash
            // for it. Real navigations get progress-backed loading feedback.
            if (url != null && !url.startsWith("about:blank")) {
                if (url.startsWith("file:")) {
                    // Back on a bundled page: the online player's keep-alive
                    // has no page left to keep alive, and its wake lock must
                    // not outlive the playback it stood guard over. Stopping
                    // a not-running service is a no-op, so this is safe on
                    // every file:// start.
                    WebAudioService.stop(MainActivity.this);
                }
                captureMediaContext(url);
                verifiedDownloadUrl = "";
                verifiedHlsUrl = "";
                // A navigation invalidates the capture, so the shell's "media
                // is ready" affordances go back to idle with it. Leaving the
                // icon lit or the panel button enabled here would offer a save
                // whose URL no longer exists — the tap would fall through to
                // the "还没有检测到" branch and read as a broken button. The
                // remembered title deliberately survives: the save happens on
                // the player page, one navigation later.
                mediaReadyAnnounced = false;
                styleDownloadEntry(downloadEntry, false);
                refreshDownloadActionState();
                hideInAppBanner();
                showSplash();
                // The hard cap. The external reveal waits for onPageFinished;
                // a page whose load event never fires (a hung host, a stalled
                // ad) must still release the curtain on its own, or the
                // "loading page" the user asked for becomes a wall. The
                // generation check means a NEW navigation's cap is not
                // cancelled by an old timer, and an old timer cannot lift a
                // new navigation's curtain.
                final int generation = pageGeneration;
                mainHandler.postDelayed(new Runnable() {
                    @Override public void run() {
                        if (generation == pageGeneration) hideSplashNow();
                    }
                }, SPLASH_HARD_CAP_MS);
                // The icon is the ONLY way into both 保存 and 下载中心, so it
                // deliberately survives onto the play page — hiding it there
                // is exactly why "我还是看不懂怎么下载视频" happened. Real
                // fullscreen still hides it through enterFullscreen(), and a
                // drag parks it on any other corner if it sits over the video.
                collapseFabMenu();
            }
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri uri = request.getUrl();
            String url = uri == null ? "" : uri.toString();
            String scheme = uri == null ? "" : String.valueOf(uri.getScheme()).toLowerCase();
            if ("file".equals(scheme) || "about".equals(scheme) || "data".equals(scheme)) {
                // In-app internal pages (the bundled discover asset, the
                // about:blank renderer warm-up) — never external handlers.
                return false;
            }
            if ("app".equals(scheme)) {
                // In-app navigation requests from the bundled pages. There is
                // no PC-library route any more — the APK is standalone — so
                // every app:// link is simply dropped rather than dispatched.
                Log.i(TAG, "app link dropped (no in-app routes): " + url);
                return true;
            }
            if (!scheme.equals("http") && !scheme.equals("https")) {
                // tel:/mailto:/intent:/market: → system handler, never in-app.
                try {
                    startActivity(Intent.parseUri(url, Intent.URI_INTENT_SCHEME));
                } catch (Exception e) {
                    Log.i(TAG, "external scheme dropped: " + url);
                }
                return true;
            }
            // Learn the player host *before* the policy runs, so a rotated
            // player domain becomes a playable page instead of a button that
            // does nothing.
            learnPlayerUrl(url);
            boolean blocked = shield.shouldBlockNavigation(url);
            if (!blocked && request.isForMainFrame() && isOurPlayPage(currentFrameUrl)) {
                // Handover to the online player: start the keep-alive so 息屏
                // keeps the WebView's own audio alive — no stream restart, no
                // position loss (WebAudioService holds the process and the
                // CPU; the WebView is never paused). Title comes from the
                // play page's media context when it has one. The curtain
                // rises HERE, at tap time: onPageStarted only fires once the
                // navigation commits, and the teardown gap before it is the
                // 「点了播放闪一下黑」 the screenshots caught.
                showSplash("正在打开播放器…");
                String t = suggestedStem(url);
                WebAudioService.start(MainActivity.this, t);
            }
            return blocked;
        }

        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
            String url = request.getUrl() == null ? "" : request.getUrl().toString();
            if (request.isForMainFrame()) {
                // A server-side redirect of the top frame never reaches
                // shouldOverrideUrlLoading on every WebView build, so the hop is
                // learned here as well. Both paths use the same anchor — the
                // document that initiated the navigation — and neither touches
                // the WebView, which is not callable from this thread.
                learnPlayerUrl(url);
            }
            // Apply the same allow/block decision before exposing a URL to the
            // native downloader; an advert with an .mp4 suffix must never make
            // the save action appear.
            WebResourceResponse blocked = shield.intercept(view, request);
            if (blocked != null) {
                if (request.isForMainFrame()) explainedFrameUrl = url;
                return blocked;
            }
            if (request.isForMainFrame()) explainedFrameUrl = "";
            Uri uri = request.getUrl();
            if (uri != null) captureDirectMedia(uri.toString());
            return null;
        }

        @Override
        public void doUpdateVisitedHistory(WebView view, String url, boolean isReload) {
            // The shield's own explanation page is the destination the user is
            // meant to read; bouncing it would flash a sentence and dump them
            // back with no idea what happened. It is also not a document to
            // anchor a hop on, so the anchor deliberately stays put.
            if (url != null && url.equals(explainedFrameUrl)) {
                explainedFrameUrl = "";
                return;
            }
            // Ad redirects that slip through as same-page navigations are
            // still blocked here on the main frame.
            if (!isReload && shield.shouldBlockNavigation(url)) {
                view.goBack();
                return;
            }
            // A commit is the one moment the on-screen document changes, and it
            // is main-thread — the only place this field may be written.
            if (url != null && !url.startsWith("about:")) currentFrameUrl = url;
            super.doUpdateVisitedHistory(view, url, isReload);
        }

        /**
         * Learn a player host from the page that is handing over to it.
         *
         * <p>Two trust levels, deliberately unequal.
         *
         * <p>A link out of our own play page is the URL the site's own player
         * markup declared — the same anchor the save path validates against — so
         * its host is trusted for the session. That is what makes a player-domain
         * rotation self-heal instead of waiting for a new build.
         *
         * <p>A link out of an already-trusted player page is the sibling hop these
         * players actually use ({@code bysetayico.com/e/<id>} →
         * {@code f7hyg4q.org/l3y/<id>}), trusted for that one hop and never as a
         * new anchor. Anything else — an untrusted page navigating somewhere, an
         * advert host, a hop out of a hop — is left to the policy, which answers
         * with the readable refusal page rather than a blank frame.
         *
         * <p>Deliberately takes no {@link WebView}: this is called from
         * {@code shouldInterceptRequest}, where any WebView method is a fatal
         * wrong-thread call. The anchor is {@link #currentFrameUrl}.
         */
        private void learnPlayerUrl(String url) {
            String current = currentFrameUrl;
            if (isOurPlayPage(current)) {
                shield.trustDeclaredPlayerUrl(url);
            } else {
                shield.trustHoppedPlayerUrl(ShieldRules.hostOf(current == null ? "" : current), url);
            }
        }
    }

    /** Whether the page the user is looking at is our bundled play page. */
    private static boolean isOurPlayPage(String url) {
        return url != null && url.startsWith("file:///android_asset/play.html");
    }

    @Override
    public void onBackPressed() {
        // Back exits fullscreen first — otherwise the video keeps playing in
        // a landscape view the user can no longer see a way out of.
        if (customView != null) {
            exitFullscreen();
            return;
        }
        // An open menu is a sheet: Back should close the sheet, not navigate
        // the page out from under it.
        if (fabMenuOpen) {
            collapseFabMenu();
            return;
        }
        if (webView != null && webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onPause() {
        super.onPause();
        // Never poll in the background: the completion receiver still updates
        // the store, and the panel re-reads it on resume.
        stopPolling();
    }

    @Override
    protected void onResume() {
        super.onResume();
        // The WebView is never paused for background audio: the keep-alive
        // service (WebAudioService) holds the process up while the online
        // player plays, so 息屏 keeps the sound and returning finds the
        // picture exactly where it was. Touching the WebView here would
        // freeze the page after the first lock screen.
        if (panelVisible) {
            refreshDownloadStates();
            renderDownloadPanel();
            startPolling();
        }
    }

    /**
     * The manifest now absorbs the resize dimensions (screenLayout /
     * smallestScreenSize / density) so entering split-screen, dragging a
     * freeform window or unfolding a foldable no longer recreates the
     * Activity — recreation would kill the in-flight HLS workers and discard
     * the WebView's state. The price is that the native chrome was sized for
     * the old window and has to be re-applied by hand here.
     */
    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        if (downloadEntry != null && downloadEntry.getParent() instanceof FrameLayout) {
            downloadEntry.setLayoutParams(downloadEntryLayout());
        }
        if (fabMenu != null && fabMenu.getParent() instanceof FrameLayout) {
            fabMenu.setLayoutParams(fabMenuLayout());
        }
        // Re-applied from the current metrics: a density change that the
        // manifest absorbs instead of recreating the activity would otherwise
        // leave the icon's ring stroked for the old bucket. The lit/idle state
        // is derived rather than stored, so a resize cannot strand the icon in
        // a state the captured URLs no longer justify.
        styleDownloadEntry(downloadEntry, hasSavableMedia());
        if (splashBox != null) splashBox.setLayoutParams(splashBoxLayout());
        // The card and its rows are also built once and reused, and their text
        // was measured with the old density's metrics. Rebuilding is cheaper
        // than patching every descendant, and it re-reads the task list too.
        boolean wasVisible = panelVisible;
        if (downloadPanel != null) {
            ((FrameLayout) getWindow().getDecorView()).removeView(downloadPanel);
            downloadPanel = null;
            downloadCard = null;
            downloadList = null;
            downloadScroll = null;
            // The action button is a child of the discarded card, so the
            // reference has to go with it — buildDownloadPanelIfNeeded would
            // otherwise try to add a view that still has a parent.
            downloadAction = null;
            panelVisible = false;
            if (wasVisible) openDownloadPanel();
        }
    }

    @Override
    protected void onDestroy() {
        if (customView != null) exitFullscreen();
        // The activity is gone (user swiped the app away): the WebView's
        // playback ended with it, so the keep-alive must not become a
        // notification that guards nothing.
        WebAudioService.stop(this);
        unregisterDownloadReceiver();
        stopPolling();
        if (webView != null) webView.destroy();
        super.onDestroy();
    }

    // -------------------------------------------------------- tablet adaptation

    /**
     * True when the window's shortest edge is tablet-sized.
     * {@code smallestScreenWidthDp} is the stable measure for this: it reports
     * the shortest edge of the *display* in dp, so a phone held in landscape
     * (a 360dp window) is still a phone, while a 1280x800 tablet is not.
     */
    private boolean isTablet() {
        return getResources().getConfiguration().smallestScreenWidthDp >= TABLET_MIN_WIDTH_DP;
    }

    /**
     * Width a column of content may occupy: the window minus its side margins,
     * capped so a wide tablet gets a readable centred column instead of an
     * edge-to-edge stretch. Every piece of native chrome sizes itself from
     * this rather than from its own literal.
     */
    private int contentMaxWidth() {
        int available = getResources().getDisplayMetrics().widthPixels
                - dp(CHROME_MARGIN_DP) * 2;
        return Math.max(dp(240), Math.min(available, dp(CONTENT_MAX_WIDTH_DP)));
    }

    /** Height the task list may take out of the window it is actually in. */
    private int downloadListHeight() {
        int reserved = dp(62) + dp(24) + dp(CHROME_MARGIN_DP) * 2
                + dp(16) * 2 + dp(34) + dp(10) + dp(16)
                // The panel's own 保存媒体 button and its top margin: without
                // these the list would be sized for a card that no longer
                // exists and the action would be pushed past the bottom edge.
                + dp(46) + dp(12);
        int available = getResources().getDisplayMetrics().heightPixels - reserved;
        return Math.max(dp(LIST_MIN_HEIGHT_DP),
                Math.min(available, dp(LIST_MAX_HEIGHT_DP)));
    }

    /** Height of a floating chip — and the side of the square icon entry. */
    private int floatingChipHeight() {
        return isTablet() ? dp(52) : dp(44);
    }

    /** The splash column is capped like every other native surface. */
    private FrameLayout.LayoutParams splashBoxLayout() {
        return new FrameLayout.LayoutParams(contentMaxWidth(),
                FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.CENTER);
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
