"""Execution tests for the APK shield's player-host trust rules.

``ShieldRules`` is not dependency-free — it imports ``android.util.Log`` and the
``android.webkit`` response types — so the way ``PlaybackResume`` is tested does
not work here directly.  What does work is compiling the *real* source against
minimal stand-ins for the four Android types it touches, then driving it on an
ordinary JVM and asserting on what it actually returns.

That is worth the scaffolding, because the rules under test are the ones that
decided whether the user saw a video or a blank screen:

* a refusal of the **top-level document** must produce a readable page, not an
  empty 404 — the empty body *was* the white screen;
* a refusal of a **subresource** must stay the cheap empty 404, or every blocked
  ad iframe starts painting a page;
* a rotated player domain declared by our own play page must be trusted, so the
  rotation heals itself;
* a **sibling host** reached by hopping out of a trusted player page must be
  trusted — that is the ``bysetayico.com`` → ``f7hyg4q.org`` shape documented in
  ``security_lists.py``;
* a host reached by hopping must **not** be able to hop again, and the advert
  tables must outrank every route into the allow-list.  The first version of this
  code got both wrong, and ``doubleclick.net`` ended up trusted — after which the
  shield silently stopped blocking anything.

The baked tables are read from the generated ``blocklist.json`` with Python's own
``json`` and injected through reflection, so the cases below run against the real
shipped asset rather than a hand-written fixture.

Skipped when no JDK is present so the suite stays portable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from _jdk import jdk_tool

JAVAC = jdk_tool("javac")
JAVA = jdk_tool("java")

_ROOT = Path(__file__).resolve().parents[1]
_ARCHIVER_JAVA = (
    _ROOT
    / "android"
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "asmrlib"
    / "archiver"
)
_SHIELD_SOURCE = _ARCHIVER_JAVA / "ShieldRules.java"

# The refusal page inlines Theme's CSS literals, so Theme is part of what has to
# compile - and compiling the real file is the point of this module.  That is
# also why the palette is checked here rather than by grepping for hex: a
# grepped hex passes on a comment, a compiled constant does not.
_THEME_SOURCE = _ARCHIVER_JAVA / "Theme.java"

_BLOCKLIST = _ROOT / "android" / "app" / "src" / "main" / "assets" / "blocklist.json"

# Stand-ins for the Android types ShieldRules touches.  They are deliberately
# small: they exist to satisfy javac and to let the test read back what the class
# produced.  Anything the real API has and this code does not call is left out on
# purpose, so that a new call site shows up as a compile error rather than as a
# silently missing behaviour.
_STUBS = {
    "android/util/Log.java": """
package android.util;

/** Prints to stdout so the test can assert on the shield's own log lines. */
public final class Log {
    public static int i(String tag, String msg) { return say("I", tag, msg); }
    public static int w(String tag, String msg) { return say("W", tag, msg); }
    public static int e(String tag, String msg) { return say("E", tag, msg); }
    public static int d(String tag, String msg) { return say("D", tag, msg); }
    public static int wtf(String tag, String msg) { return say("F", tag, msg); }
    private static int say(String level, String tag, String msg) {
        System.out.println("LOG " + level + " " + tag + ": " + msg);
        return 0;
    }
}
""",
    "android/net/Uri.java": """
package android.net;

/** Just enough of Uri for the shield's scheme/host decisions. */
public final class Uri {
    private final String raw;
    private Uri(String raw) { this.raw = raw; }
    public static Uri parse(String s) { return s == null ? null : new Uri(s); }
    public String getScheme() {
        int i = raw.indexOf(':');
        return i < 0 ? null : raw.substring(0, i);
    }
    public String getHost() {
        int i = raw.indexOf("://");
        if (i < 0) return null;
        String rest = raw.substring(i + 3);
        int end = rest.length();
        for (int j = 0; j < rest.length(); j++) {
            char c = rest.charAt(j);
            if (c == '/' || c == '?' || c == '#' || c == ':') { end = j; break; }
        }
        return rest.substring(0, end);
    }
    public String toString() { return raw; }
}
""",
    "android/webkit/WebView.java": """
package android.webkit;

/**
 * Empty on purpose.  ShieldRules must never call a method on the WebView from
 * shouldInterceptRequest — that runs on a Chromium worker thread and a WebView
 * call there throws WebViewMethodCalledOnWrongThreadViolation.  The test passes
 * null for the view, so introducing such a call fails here immediately.
 */
public class WebView {
}
""",
    "android/webkit/WebResourceRequest.java": """
package android.webkit;

import android.net.Uri;

/** Only the two accessors ShieldRules reads. */
public interface WebResourceRequest {
    Uri getUrl();
    boolean isForMainFrame();
}
""",
    "android/webkit/WebResourceResponse.java": """
package android.webkit;

import java.io.InputStream;
import java.util.Map;

/** Holds what the shield handed back, so the test can read it. */
public class WebResourceResponse {
    private final String mimeType;
    private final String encoding;
    private final int statusCode;
    private final Map<String, String> headers;
    private final InputStream data;

    public WebResourceResponse(String mimeType, String encoding, int statusCode,
                               String reasonPhrase, Map<String, String> headers,
                               InputStream data) {
        this.mimeType = mimeType;
        this.encoding = encoding;
        this.statusCode = statusCode;
        this.headers = headers;
        this.data = data;
    }

    public String getMimeType() { return mimeType; }
    public String getEncoding() { return encoding; }
    public int getStatusCode() { return statusCode; }
    public Map<String, String> getResponseHeaders() { return headers; }
    public InputStream getData() { return data; }
}
""",
    "android/content/res/AssetManager.java": """
package android.content.res;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;

/**
 * Compile-time placeholder.  ``ShieldRules.fromAssets`` is not driven from this
 * test: the generated blocklist.json is parsed with Python's json module and the
 * tables are injected by reflection instead, which keeps the parsing honest.
 * Calling this stub throws, so a test that starts relying on it says so loudly.
 */
public class AssetManager {
    private final File root;
    public AssetManager(File root) { this.root = root; }
    public InputStream open(String name) throws IOException {
        File file = new File(root, name);
        if (!file.isFile()) throw new IOException("no such asset: " + name);
        return new FileInputStream(file);
    }
}
""",
    "android/content/res/Configuration.java": """
package android.content.res;

/**
 * Compile-time placeholder, with a settable ``uiMode`` so Theme's dark/light
 * selection can be driven from the runner instead of being assumed.
 */
public class Configuration {
    public static final int UI_MODE_NIGHT_MASK = 0x30;
    public static final int UI_MODE_NIGHT_NO = 0x10;
    public static final int UI_MODE_NIGHT_YES = 0x20;
    public int uiMode = UI_MODE_NIGHT_YES;
}
""",
    "android/content/res/Resources.java": """
package android.content.res;

/** Compile-time placeholder: one Configuration, flipped by the runner. */
public class Resources {
    private final Configuration configuration = new Configuration();
    public Configuration getConfiguration() { return configuration; }
    public void setNight(boolean night) {
        configuration.uiMode = (configuration.uiMode & ~Configuration.UI_MODE_NIGHT_MASK)
            | (night ? Configuration.UI_MODE_NIGHT_YES : Configuration.UI_MODE_NIGHT_NO);
    }
}
""",
    "android/content/Context.java": """
package android.content;

import android.content.res.Resources;

/** Compile-time placeholder for the only Context method Theme calls. */
public class Context {
    private final Resources resources = new Resources();
    public Resources getResources() { return resources; }
}
""",
    "org/json/JSONObject.java": """
package org.json;

/** Compile-time placeholder; see AssetManager in this same stub set. */
public class JSONObject {
    public JSONObject(String text) { throw new UnsupportedOperationException("stub"); }
    public JSONArray optJSONArray(String name) { throw new UnsupportedOperationException("stub"); }
}
""",
    "org/json/JSONArray.java": """
package org.json;

/** Compile-time placeholder; see AssetManager in this same stub set. */
public class JSONArray {
    public int length() { throw new UnsupportedOperationException("stub"); }
    public String optString(int index, String fallback) { throw new UnsupportedOperationException("stub"); }
}
""",
    "FakeRequest.java": """
import android.net.Uri;
import android.webkit.WebResourceRequest;

/** A request the test controls, standing in for Chromium's. */
public class FakeRequest implements WebResourceRequest {
    private final Uri url;
    private final boolean mainFrame;
    public FakeRequest(String url, boolean mainFrame) {
        this.url = Uri.parse(url);
        this.mainFrame = mainFrame;
    }
    public Uri getUrl() { return url; }
    public boolean isForMainFrame() { return mainFrame; }
}
""",
}

_RUNNER_JAVA = r"""
import android.webkit.WebResourceResponse;
import android.content.Context;
import com.asmrlib.archiver.ShieldRules;
import com.asmrlib.archiver.Theme;

import java.io.ByteArrayOutputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.PrintStream;
import java.lang.reflect.Field;
import java.util.List;
import java.util.Set;

/**
 * Minimal driver: one parseable line per observation and no assertions of its
 * own, so a failure report says what the shield *did* rather than only that
 * something inside Java broke.  The Python side owns the expected values.
 */
public class Runner {

    static PrintStream out;

    public static void main(String[] args) throws Exception {
        out = new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8");

        bakedTables();
        interceptBranches();
        refusalPalette();
        themeSelection();
        declaredLearning();
        hopLearning();
        embeddedLearning();
        ceiling();
        navigationGate();
        adUrlGate();

        out.flush();
    }

    // ------------------------------------------------------------- scenarios

    /** The refusal page is painted from Theme, in both themes. */
    static void refusalPalette() throws Exception {
        ShieldRules rules = freshRules();
        palette("refused", rules, "https://random9.example/watch", "random9.example");
    }

    /** Theme picks the right half from the system's uiMode. */
    static void themeSelection() {
        theme("dark", true);
        theme("light", false);
    }

    /** The generated asset really drives the three baked tables. */
    static void bakedTables() throws Exception {
        ShieldRules rules = freshRules();
        host("baked_bys", rules, "bysetayico.com");
        host("baked_sibling", rules, "f7hyg4q.org");
        host("baked_subdomain", rules, "v.upn.one");
        host("baked_stranger", rules, "random9.example");
        asset("baked_cdn", rules, "iamcdn.net");
        asset("baked_cdn_sub", rules, "ssl.p.jwpcdn.com");
        ad("baked_ad", rules, "https://doubleclick.net/");
        ad("baked_ad_subdomain", rules, "https://ads.adsterra.com/x");
    }

    /** The white-screen regression, and the branch that must NOT change. */
    static void interceptBranches() throws Exception {
        ShieldRules rules = freshRules();
        intercept("main_frame_refused", rules, "https://random9.example/watch", true, "random9.example");
        intercept("subresource_refused", rules, "https://random9.example/ad.js", false, "random9.example");
        intercept("main_frame_player", rules, "https://bysetayico.com/e/03c306930363", true, "bysetayico.com");
        intercept("subresource_cdn", rules, "https://iamcdn.net/player-v2.js", false, "iamcdn.net");
        intercept("main_frame_media", rules, "https://short-lived.example/v/9f2.mp4", true, "short-lived.example");
        intercept("main_frame_ad", rules, "https://doubleclick.net/x", true, "doubleclick.net");
        intercept("main_frame_blank", rules, "about:blank", true, "about");
    }

    /** A rotated player domain declared by our own play page heals itself. */
    static void declaredLearning() throws Exception {
        ShieldRules rules = freshRules();
        learn("declared_new", rules.trustDeclaredPlayerUrl("https://rotated9.example/l3y/abc"));
        host("declared_host", rules, "rotated9.example");
        host("declared_subdomain", rules, "v.rotated9.example");
        intercept("declared_serves", rules, "https://rotated9.example/l3y/abc", true, "rotated9.example");
        nav("declared_navigates", rules, "https://rotated9.example/l3y/abc");
        learn("declared_refuses_ad", rules.trustDeclaredPlayerUrl("https://doubleclick.net/x"));
        host("declared_ad_still_blocked", rules, "doubleclick.net");
        learn("declared_blank", rules.trustDeclaredPlayerUrl(""));
    }

    /** The sibling hop those players actually use. */
    static void hopLearning() throws Exception {
        ShieldRules rules = freshRules();
        learn("hop_from_baked", rules.trustHoppedPlayerUrl("bysetayico.com", "https://sibling7.example/l3y/1"));
        host("hop_host", rules, "sibling7.example");
        intercept("hop_serves", rules, "https://sibling7.example/l3y/1", true, "sibling7.example");

        // Trust must not compound: a host reached by hopping is not an anchor.
        learn("hop_no_chain", rules.trustHoppedPlayerUrl("sibling7.example", "https://third8.example/"));
        host("chain_host_untrusted", rules, "third8.example");
        nav("chain_navigation_blocked", rules, "https://third8.example/");

        // Nor is an arbitrary page an anchor.
        learn("hop_from_stranger", rules.trustHoppedPlayerUrl("random9.example", "https://x10.example/"));
        host("stranger_hop_untrusted", rules, "x10.example");

        // Nor may a hop launder an advert.
        learn("hop_refuses_ad", rules.trustHoppedPlayerUrl("bysetayico.com", "https://doubleclick.net/x"));
        host("hopped_ad_still_blocked", rules, "doubleclick.net");
    }

    /** A rotated full-screen iframe is not a top-frame navigation. */
    static void embeddedLearning() throws Exception {
        ShieldRules rules = freshRules();
        String parent = "https://bysetayico.com/e/iv4asvgdcjlj";
        String child = "https://n1mwq.org/16a/iv4asvgdcjlj";
        intercept("embedded_before", rules, child, false, "n1mwq.org");
        learn("embedded_same_id", rules.trustEmbeddedPlayerUrl(parent, child));
        intercept("embedded_after", rules, child, false, "n1mwq.org");
        learn("embedded_query", rules.trustEmbeddedPlayerUrl(parent + "?p=1", child + "?token=2"));
        learn("embedded_mismatched_id", rules.trustEmbeddedPlayerUrl(parent, "https://other.example/16a/different"));
        learn("embedded_ad", rules.trustEmbeddedPlayerUrl(parent, "https://doubleclick.net/16a/iv4asvgdcjlj"));
        learn("embedded_http", rules.trustEmbeddedPlayerUrl(parent, "http://insecure.example/16a/iv4asvgdcjlj"));
        learn("embedded_http_parent", rules.trustEmbeddedPlayerUrl(parent.replace("https:", "http:"), child));
        learn("embedded_credentials", rules.trustEmbeddedPlayerUrl(parent, "https://user@other.example/16a/iv4asvgdcjlj"));
        learn("embedded_unknown_parent", rules.trustEmbeddedPlayerUrl("https://unknown.example/e/iv4asvgdcjlj", child));
        learn("embedded_non_embed_parent", rules.trustEmbeddedPlayerUrl("https://bysetayico.com/posts/iv4asvgdcjlj", child));
        learn("embedded_script", rules.trustEmbeddedPlayerUrl(parent, "https://other.example/16a/iv4asvgdcjlj.js"));
        learn("embedded_encoded_slash", rules.trustEmbeddedPlayerUrl("https://bysetayico.com/e/id%2Fother", "https://other.example/16a/id%2Fother"));
        learn("embedded_chain", rules.trustEmbeddedPlayerUrl("https://n1mwq.org/e/iv4asvgdcjlj", "https://third.example/16a/iv4asvgdcjlj"));
        learn("embedded_empty", rules.trustEmbeddedPlayerUrl(null, null));
        learn("embedded_malformed", rules.trustEmbeddedPlayerUrl(parent, "not a URI"));
        host("embedded_ad_untrusted", rules, "doubleclick.net");
        host("embedded_mismatch_untrusted", rules, "other.example");
        // Shares the existing bounded hop budget; embedding cannot bypass it.
        int cap = (Integer) field(ShieldRules.class, "MAX_LEARNED_HOSTS").get(null);
        for (int i = 1; i < cap; i++) {
            rules.trustEmbeddedPlayerUrl(parent, "https://embed" + i + ".example/v/iv4asvgdcjlj");
        }
        learn("embedded_ceiling", rules.trustEmbeddedPlayerUrl(parent, "https://overflow.example/v/iv4asvgdcjlj"));
    }

    /** The session ceiling: bounded, but far above what the sources need. */
    static void ceiling() throws Exception {
        ShieldRules rules = freshRules();
        int cap = (Integer) field(ShieldRules.class, "MAX_LEARNED_HOSTS").get(null);
        out.println("CAP cap -> " + cap);
        for (int i = 0; i < cap; i++) {
            rules.trustDeclaredPlayerUrl("https://learned" + i + ".example/");
        }
        host("ceiling_last_trusted", rules, "learned" + (cap - 1) + ".example");
        learn("ceiling_overflow", rules.trustDeclaredPlayerUrl("https://learned" + cap + ".example/"));
        host("ceiling_overflow_host", rules, "learned" + cap + ".example");
    }

    /** The main-frame navigation gate: true means "dropped". */
    static void navigationGate() throws Exception {
        ShieldRules rules = freshRules();
        nav("nav_ad", rules, "https://doubleclick.net/");
        nav("nav_unknown", rules, "https://random9.example/");
        nav("nav_baked_player", rules, "https://bysetayico.com/e/x");
        nav("nav_media_suffix", rules, "https://short-lived.example/v/9f2.mp4");
        nav("nav_app_origin", rules, "http://127.0.0.1:8765/browse");
        nav("nav_file", rules, "file:///android_asset/play.html");
    }

    /** Label boundaries, not path substrings. */
    static void adUrlGate() throws Exception {
        ShieldRules rules = freshRules();
        ad("ad_affiliate_path", rules, "https://asmrlib.com/tags/affiliate");
        ad("ad_blocked_host", rules, "https://adsterra.com/x");
        ad("ad_keyword_label", rules, "https://random9.example/adserver/banner");
        ad("ad_keyword_inside_word", rules, "https://random9.example/myadservery");
        ad("ad_player_host", rules, "https://bysetayico.com/e/x");
    }

    // ---------------------------------------------------------------- helpers

    static ShieldRules freshRules() throws Exception {
        ShieldRules rules = new ShieldRules();
        Set<String> player = set(rules, "playerDomains");
        Set<String> asset = set(rules, "assetDomains");
        Set<String> blocked = set(rules, "blockedHosts");
        for (String h : Tables.PLAYER) player.add(h);
        for (String h : Tables.ASSET) asset.add(h);
        for (String h : Tables.BLOCKED) blocked.add(h);
        List<String> keywords = (List<String>) field(ShieldRules.class, "blockUrlKeywords").get(rules);
        for (String k : Tables.KEYWORDS) keywords.add(k);
        return rules;
    }

    @SuppressWarnings("unchecked")
    static Set<String> set(ShieldRules rules, String name) throws Exception {
        return (Set<String>) field(ShieldRules.class, name).get(rules);
    }

    static Field field(Class<?> type, String name) throws Exception {
        Field f = type.getDeclaredField(name);
        f.setAccessible(true);
        return f;
    }

    static void intercept(String name, ShieldRules rules, String url, boolean mainFrame,
                          String expectedHost) throws Exception {
        WebResourceResponse res = rules.intercept(null, new FakeRequest(url, mainFrame));
        if (res == null) {
            out.println("INTERCEPT " + name + " -> allowed");
            return;
        }
        String body = readAll(res.getData());
        out.println("INTERCEPT " + name + " -> status=" + res.getStatusCode()
                + " mime=" + res.getMimeType()
                + " len=" + body.length()
                + " names_host=" + body.contains(expectedHost)
                + " has_back=" + body.contains("\u8fd4\u56de\u4e0a\u4e00\u9875")
                + " has_script=" + body.contains("<script"));
    }

    static void learn(String name, boolean value) {
        out.println("LEARN " + name + " -> " + value);
    }

    /**
     * The refusal page's colours, read back off the page the shield served.
     *
     * Asserting these from the compiled class rather than by grepping the
     * source matters: a grep for ``#0a0b0f`` passes on a comment, and it cannot
     * tell a live declaration from a dead copy.  This asks the page itself.
     */
    static void palette(String name, ShieldRules rules, String url, String expectedHost)
            throws Exception {
        WebResourceResponse res = rules.intercept(null, new FakeRequest(url, true));
        String body = readAll(res.getData());
        out.println("PALETTE " + name + " ->"
                + " dark_bg=" + body.contains(Theme.BG_CSS)
                + " dark_surface=" + body.contains(Theme.SURFACE_CSS)
                + " dark_text=" + body.contains(Theme.TEXT_CSS)
                + " dark_accent=" + body.contains(Theme.ACCENT_CSS)
                + " stale_light_bg=" + body.contains("#f2f3f7")
                + " stale_light_accent=" + body.contains("#4a63e8")
                + " light_media=" + body.contains("prefers-color-scheme:light")
                + " both_schemes=" + body.contains("color-scheme:dark light")
                + " dark_only=" + body.contains("color-scheme:dark;")
                + " names_host=" + body.contains(expectedHost));
    }

    /**
     * Theme's dark/light selection, driven from the stub's uiMode.
     *
     * Every accessor is sampled in both themes, not just the plain colours.
     * The partial-alpha ones are the point: they used to be a single family
     * derived from the dark constants, so ``surfaceAlpha(190)`` answered with
     * the dark card in light mode - a black menu card over a white page.
     */
    static void theme(String name, boolean night) {
        Context ctx = new Context();
        ctx.getResources().setNight(night);
        out.println("THEME " + name + " ->"
                + " night=" + Theme.isNight(ctx)
                + " bg=" + Integer.toHexString(Theme.bg(ctx))
                + " text=" + Integer.toHexString(Theme.text(ctx))
                + " text2=" + Integer.toHexString(Theme.text2(ctx))
                + " accent=" + Integer.toHexString(Theme.accent(ctx))
                + " on_accent=" + Integer.toHexString(Theme.onAccent(ctx))
                + " surface_alpha=" + Integer.toHexString(Theme.surface(ctx, 190))
                + " accent_alpha=" + Integer.toHexString(Theme.accent(ctx, 120))
                + " text_alpha=" + Integer.toHexString(Theme.text(ctx, 160))
                + " bg_alpha=" + Integer.toHexString(Theme.bg(ctx, 210))
                + " scrim=" + Integer.toHexString(Theme.scrim(ctx))
                + " scrim_alpha=" + Integer.toHexString(Theme.scrim(ctx, 70))
                + " fill=" + Integer.toHexString(Theme.fill(ctx))
                + " fill_alpha=" + Integer.toHexString(Theme.fill(ctx, 34))
                + " sep=" + Integer.toHexString(Theme.sep(ctx))
                + " sep_strong=" + Integer.toHexString(Theme.sepStrong(ctx))
                + " danger=" + Integer.toHexString(Theme.danger(ctx)));
    }

    static void host(String name, ShieldRules rules, String host) {
        out.println("HOST " + name + " -> " + rules.isPlayerHost(host));
    }

    static void asset(String name, ShieldRules rules, String host) {
        out.println("ASSET " + name + " -> " + rules.isAssetHost(host));
    }

    static void ad(String name, ShieldRules rules, String url) {
        out.println("AD " + name + " -> " + rules.isAdUrl(url));
    }

    static void nav(String name, ShieldRules rules, String url) {
        out.println("NAV " + name + " -> " + rules.shouldBlockNavigation(url));
    }

    static String readAll(InputStream in) throws Exception {
        if (in == null) return "";
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int n;
        while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
        return new String(bos.toByteArray(), "UTF-8");
    }
}
"""


def _java_array(values: list[str]) -> str:
    return ", ".join('"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in values)


def _rgb(value: str) -> tuple[int, int, int]:
    """The ``rrggbb`` half of an ``aarrggbb`` hex string from the Runner."""
    raw = value[-6:]
    return tuple(int(raw[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _luminance(value: str) -> float:
    """WCAG relative luminance, so "darker" means what the eye sees."""

    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in _rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _composite(over: str, under: str) -> str:
    """``over`` (with its alpha) painted on top of the opaque ``under``."""
    alpha = int(over[:2], 16) / 255
    top, bottom = _rgb(over), _rgb(under)
    return "ff" + "".join(
        f"{round(alpha * t + (1 - alpha) * b):02x}" for t, b in zip(top, bottom, strict=True)
    )


@unittest.skipUnless(JAVAC and JAVA, "javac/java not available")
class ShieldRulesJavaTests(unittest.TestCase):
    """Compile the real shield class and assert on the decisions it made."""

    @classmethod
    def setUpClass(cls) -> None:
        tables = json.loads(_BLOCKLIST.read_text(encoding="utf-8"))
        cls.cap = None

        cls.tmp = Path(tempfile.mkdtemp(prefix="shield-java-"))
        cls.classes = cls.tmp / "classes"
        cls.classes.mkdir()

        sources = []
        for rel, text in _STUBS.items():
            path = cls.tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            sources.append(path)

        # The baked tables come from the shipped asset, parsed by Python rather
        # than by the stub JSON classes, so the cases below cannot pass on a
        # fixture that has drifted away from what the APK actually ships.
        generated = cls.tmp / "Tables.java"
        generated.write_text(
            "public final class Tables {\n"
            f"    static final String[] PLAYER = {{{_java_array(tables['player_domains'])}}};\n"
            f"    static final String[] ASSET = {{{_java_array(tables['asset_domains'])}}};\n"
            f"    static final String[] BLOCKED = {{{_java_array(tables['blocked_hosts'])}}};\n"
            f"    static final String[] KEYWORDS = {{{_java_array(tables['block_url_keywords'])}}};\n"
            "}\n",
            encoding="utf-8",
        )
        sources.append(generated)

        runner = cls.tmp / "Runner.java"
        runner.write_text(_RUNNER_JAVA, encoding="utf-8")
        sources.append(runner)

        proc = subprocess.run(
            [JAVAC, "-encoding", "UTF-8", "-d", str(cls.classes), *map(str, sources),
             str(_SHIELD_SOURCE), str(_THEME_SOURCE)],
            capture_output=True,
        )
        if proc.returncode != 0:  # pragma: no cover - build failure
            raise AssertionError(
                "javac failed:\n"
                + proc.stdout.decode("utf-8", "replace")
                + proc.stderr.decode("utf-8", "replace")
            )
        run = subprocess.run(
            [JAVA, "-cp", str(cls.classes), "Runner"],
            capture_output=True,
            timeout=120,
        )
        cls.stdout = run.stdout.decode("utf-8", "replace")
        if run.returncode != 0:  # pragma: no cover - runtime failure
            raise AssertionError(
                "Runner crashed:\n" + cls.stdout + run.stderr.decode("utf-8", "replace")
            )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------- parsing

    @classmethod
    def _lines(cls, kind: str) -> dict[str, str]:
        found = {}
        prefix = kind + " "
        for line in cls.stdout.splitlines():
            if not line.startswith(prefix):
                continue
            name, _, value = line[len(prefix):].partition(" -> ")
            found[name] = value
        return found

    def _flag(self, kind: str, name: str) -> bool:
        value = self._lines(kind)[name]
        self.assertIn(value, ("true", "false"))
        return value == "true"

    def _learn(self, name: str) -> bool:
        return self._flag("LEARN", name)

    def _host(self, name: str) -> bool:
        return self._flag("HOST", name)

    def _nav(self, name: str) -> bool:
        return self._flag("NAV", name)

    def _ad(self, name: str) -> bool:
        return self._flag("AD", name)

    def _intercept(self, name: str) -> dict[str, str]:
        value = self._lines("INTERCEPT")[name]
        if value == "allowed":
            return {"verdict": "allowed"}
        parsed = {"verdict": "refused"}
        parsed.update(self._pairs("INTERCEPT", name))
        return parsed

    def _pairs(self, kind: str, name: str) -> dict[str, str]:
        """A ``key=value`` line read back as a dict."""
        value = self._lines(kind)[name]
        return dict(part.partition("=")[::2] for part in value.split())

    # ----------------------------------------------------- the shipped tables

    def test_the_generated_asset_really_drives_the_player_table(self) -> None:
        """The trust rules are only as good as the asset they read.

        ``f7hyg4q.org`` is in the list precisely because its absence once broke
        the BI embed at its second hop; pinning it here means a regeneration that
        drops it fails the suite instead of failing on the user's phone.
        """

        self.assertTrue(self._host("baked_bys"))
        self.assertTrue(self._host("baked_sibling"))
        self.assertTrue(self._host("baked_subdomain"))
        self.assertFalse(self._host("baked_stranger"))

    def test_the_generated_asset_really_drives_the_asset_table(self) -> None:
        self.assertTrue(self._flag("ASSET", "baked_cdn"))
        self.assertTrue(self._flag("ASSET", "baked_cdn_sub"))

    def test_the_generated_asset_really_drives_the_advert_table(self) -> None:
        self.assertTrue(self._ad("baked_ad"))
        self.assertTrue(self._ad("baked_ad_subdomain"))

    # -------------------------------------------------- the white-screen fix

    def test_a_refused_top_level_document_gets_a_readable_page(self) -> None:
        """This is the bug: an empty 404 on the main frame *is* a white screen.

        The page has to name the host, because that host is the thing to add to
        PLAYER_DOMAINS when a player rotates, and it has to offer a way back.
        """

        page = self._intercept("main_frame_refused")
        self.assertEqual(page["verdict"], "refused")
        self.assertEqual(page["status"], "200")
        self.assertEqual(page["mime"], "text/html")
        self.assertGreater(int(page["len"]), 200)
        self.assertEqual(page["names_host"], "true")
        self.assertEqual(page["has_back"], "true")
        # No script: a refusal page is static by construction, and a script tag
        # here would be an execution surface the shield does not need.
        self.assertEqual(page["has_script"], "false")

    def test_the_refusal_page_is_painted_from_theme(self) -> None:
        """The refusal page keeps the requested dark app palette in all modes."""
        page = self._pairs("PALETTE", "refused")
        for key in ("dark_bg", "dark_surface", "dark_text", "dark_accent",
                    "dark_only", "names_host"):
            self.assertEqual(
                page.get(key), "true", f"the refusal page has no {key}"
            )
        for key in ("light_media", "both_schemes",
                    "stale_light_bg", "stale_light_accent"):
            self.assertEqual(page.get(key), "false")

    def test_theme_selects_the_half_the_system_asked_for(self) -> None:
        """The product pins dark in both system modes, so one colour survives.

        The accessors used to follow uiMode, and a light phone got a two-step
        flash on every cold start: dark splash, dark window, then a light page.
        The product now owns its material - dark in either system mode, matching
        the bundled pages' pinned ``color-scheme: dark`` - so the runner's light
        sample must answer with the *same* dark values, not with a light half.
        """
        dark = self._pairs("THEME", "dark")
        light = self._pairs("THEME", "light")
        for name in ("night", "bg", "text", "text2", "danger"):
            self.assertEqual(
                light[name], dark[name],
                f"the pinned-dark product answered {name} differently per uiMode",
            )
        self.assertEqual(dark["night"], "true")
        self.assertEqual(dark["bg"], "ff0a0b0f")
        self.assertEqual(dark["text"], "fff1f3f8")
        self.assertEqual(dark["text2"], "ffa2acbd")
        self.assertEqual(dark["danger"], "ffff7a7a")

    def test_every_partial_alpha_tint_follows_the_theme(self) -> None:
        """Every translucent tint answers from the pinned dark palette.

        The chrome is full of translucent tints - the panel scrim, the menu
        card, the progress ring's track. They once came in two families, one
        per system mode; the light half is retired (the product owns its dark
        material in either system mode), so each tint must read back
        *identical* in the runner's two samples - that is what proves no light
        branch survived in the compiled class.

        Asserting from the compiled class rather than by grepping the source
        matters: a grep for ``0xFF15171E`` passes on a comment and cannot tell
        a live declaration from a dead one.
        """
        dark = self._pairs("THEME", "dark")
        light = self._pairs("THEME", "light")

        # Derived, not repeated: the tint carries the accent's own RGB, so a
        # stale triple cannot survive a palette change.
        self.assertEqual(dark["accent_alpha"], "787c9cff")
        self.assertEqual(dark["surface_alpha"], "be15171e")
        self.assertEqual(dark["text_alpha"], "a0f1f3f8")
        self.assertEqual(dark["bg_alpha"], "d20a0b0f")
        self.assertEqual(dark["fill_alpha"], "22ffffff")

        # What reads on the accent is near-black: the dark accent is a pale
        # blue that only a near-black reads on.
        self.assertEqual(dark["on_accent"], "ff06101f")

        # Hairlines and the quiet fill are translucent white washes.
        self.assertEqual(dark["sep"], "16ffffff")
        self.assertEqual(dark["sep_strong"], "2bffffff")
        self.assertEqual(dark["fill"], "11ffffff")

        # The retired light half must not answer from anywhere: both system
        # modes get byte-for-byte the same value.
        for name in ("accent_alpha", "surface_alpha", "text_alpha",
                     "bg_alpha", "fill_alpha", "on_accent",
                     "sep", "sep_strong", "fill"):
            self.assertEqual(
                light[name], dark[name],
                f"{name} answered differently per uiMode; a light branch "
                f"survived in Theme",
            )

    def test_a_scrim_darkens_the_page_it_covers(self) -> None:
        """The FAB scrim recedes the page, in the one palette there is.

        ``Theme.bg(70)`` was once the bug - on a light page a wash of the page
        colour over the page colour changed nothing, and the menu card floated
        with no sense of being on top of anything. On the pinned dark page the
        page colour *is* the darkening agent: more of a near-black is less
        light. The scrim is therefore BG by name, and both system modes must
        answer with the same value.
        """
        dark = self._pairs("THEME", "dark")
        light = self._pairs("THEME", "light")

        # The scrim is the page colour, at the weight the menu always had.
        self.assertEqual(dark["scrim"], "ff0a0b0f")
        self.assertEqual(dark["scrim_alpha"], "460a0b0f")
        self.assertEqual(light["scrim"], dark["scrim"])
        self.assertEqual(light["scrim_alpha"], dark["scrim_alpha"])

        # On the dark page the scrim is the page colour, so compositing it is
        # a deliberate no-op - but it must never become a *light* wash, which
        # is the failure mode a future "generalise the scrim" edit would
        # bring back: compositing then raises the page instead of sinking it.
        page = _luminance(dark["bg"])
        washed = _luminance(_composite(dark["scrim_alpha"], dark["bg"]))
        self.assertLessEqual(washed, page)

    def test_a_refused_subresource_stays_a_cheap_empty_404(self) -> None:
        """The opposite branch, and the reason it must not drift.

        Every blocked advert iframe and tracker pixel goes through here. Serving
        a full page for each one would be slow, would show up in the DOM, and
        would be pointless — nothing displays a subresource refusal.
        """

        page = self._intercept("subresource_refused")
        self.assertEqual(page["verdict"], "refused")
        self.assertEqual(page["status"], "404")
        self.assertEqual(page["mime"], "text/plain")
        self.assertEqual(page["len"], "0")

    def test_allowed_requests_are_left_to_the_network(self) -> None:
        """A null response means "carry on"; anything else breaks playback."""

        self.assertEqual(self._intercept("main_frame_player")["verdict"], "allowed")
        self.assertEqual(self._intercept("subresource_cdn")["verdict"], "allowed")
        self.assertEqual(self._intercept("main_frame_media")["verdict"], "allowed")

    def test_a_refused_advert_main_frame_is_also_a_readable_page(self) -> None:
        """A refused ad never reaches the network, so the page is ours."""

        page = self._intercept("main_frame_ad")
        self.assertEqual(page["verdict"], "refused")
        self.assertEqual(page["mime"], "text/html")
        self.assertEqual(page["names_host"], "true")

    def test_a_non_http_document_is_not_the_shield_s_business(self) -> None:
        self.assertEqual(self._intercept("main_frame_blank")["verdict"], "allowed")

    # --------------------------------------------------------- rotation heals

    def test_same_media_iframe_survives_player_host_rotation(self) -> None:
        before = self._intercept("embedded_before")
        self.assertEqual(before["status"], "404")
        self.assertEqual(before["len"], "0")
        self.assertTrue(self._learn("embedded_same_id"))
        self.assertTrue(self._learn("embedded_query"))
        self.assertEqual(self._intercept("embedded_after")["verdict"], "allowed")

    def test_embedded_trust_requires_a_secure_matching_media_handover(self) -> None:
        for name in ("mismatched_id", "ad", "http", "http_parent", "credentials",
                     "unknown_parent", "non_embed_parent", "script", "encoded_slash",
                     "chain", "empty", "malformed", "ceiling"):
            with self.subTest(case=name):
                self.assertFalse(self._learn("embedded_" + name))
        self.assertFalse(self._host("embedded_ad_untrusted"))
        self.assertFalse(self._host("embedded_mismatch_untrusted"))

    def test_a_declared_player_host_is_trusted_for_the_session(self) -> None:
        """The play page hands over the URL the site's own markup declared."""

        self.assertTrue(self._learn("declared_new"))
        self.assertTrue(self._host("declared_host"))
        self.assertTrue(self._host("declared_subdomain"))
        self.assertEqual(self._intercept("declared_serves")["verdict"], "allowed")
        self.assertFalse(self._nav("declared_navigates"))

    def test_a_declared_player_host_does_not_widen_the_advert_gate(self) -> None:
        """Learning must never be a way around the block list."""

        self.assertFalse(self._learn("declared_refuses_ad"))
        self.assertFalse(self._host("declared_ad_still_blocked"))
        self.assertTrue(self._ad("baked_ad"))

    def test_a_blank_declaration_learns_nothing(self) -> None:
        self.assertFalse(self._learn("declared_blank"))

    # ------------------------------------------------------------- sibling hop

    def test_a_trusted_player_page_may_hand_over_to_a_sibling(self) -> None:
        """``bysetayico.com`` → ``f7hyg4q.org``: unrelated hosts, same source."""

        self.assertTrue(self._learn("hop_from_baked"))
        self.assertTrue(self._host("hop_host"))
        self.assertEqual(self._intercept("hop_serves")["verdict"], "allowed")

    def test_a_hopped_host_may_not_hop_again(self) -> None:
        """The rule that was missing, and the one that mattered.

        When learning was unconditional, one trusted page was enough to get any
        subsequent top-level navigation trusted — ``doubleclick.net`` included.
        A hopped host is not an anchor.
        """

        self.assertFalse(self._learn("hop_no_chain"))
        self.assertFalse(self._host("chain_host_untrusted"))
        self.assertTrue(self._nav("chain_navigation_blocked"))

    def test_an_untrusted_page_may_not_hand_over_at_all(self) -> None:
        self.assertFalse(self._learn("hop_from_stranger"))
        self.assertFalse(self._host("stranger_hop_untrusted"))

    def test_a_hop_may_not_launder_an_advert(self) -> None:
        self.assertFalse(self._learn("hop_refuses_ad"))
        self.assertFalse(self._host("hopped_ad_still_blocked"))

    # ------------------------------------------------------------------ bounds

    def test_learning_is_capped_so_a_session_cannot_enumerate_a_network(self) -> None:
        cap = int(self._lines("CAP")["cap"])
        # A sanity bound on the constant itself: high enough for the four known
        # sibling hosts plus a rotation, low enough to stay a bound.
        self.assertGreater(cap, 1)
        self.assertLessEqual(cap, 16)
        self.assertTrue(self._host("ceiling_last_trusted"))
        self.assertFalse(self._learn("ceiling_overflow"))
        self.assertFalse(self._host("ceiling_overflow_host"))

    def test_the_ceiling_is_reported_in_the_shield_log(self) -> None:
        """Silent refusals are how the white screen went unnoticed for so long."""

        self.assertIn("learned-host ceiling reached", self.stdout)

    # ------------------------------------------------------------ advert gate

    def test_the_advert_gate_still_blocks_hosts_and_keywords(self) -> None:
        self.assertTrue(self._nav("nav_ad"))
        self.assertTrue(self._ad("ad_blocked_host"))
        self.assertTrue(self._ad("ad_keyword_label"))

    def test_an_unknown_host_cannot_navigate(self) -> None:
        self.assertTrue(self._nav("nav_unknown"))

    def test_a_player_host_and_an_ordinary_file_may_navigate(self) -> None:
        self.assertFalse(self._nav("nav_baked_player"))
        self.assertFalse(self._nav("nav_file"))

    def test_a_path_that_merely_spells_affiliate_is_not_an_advert(self) -> None:
        """The desktop shield made this mistake once; the port must not."""

        self.assertFalse(self._ad("ad_affiliate_path"))
        self.assertFalse(self._ad("ad_keyword_inside_word"))
        self.assertFalse(self._ad("ad_player_host"))

    def test_the_shield_never_calls_a_webview_method_from_the_worker_path(self) -> None:
        """The first version of the hop rule called WebView.getUrl() inside
        shouldInterceptRequest, which runs on a Chromium worker thread — the
        process died the instant a main-frame request arrived.

        The Runner passes null for the view, so any such call throws here.
        """

        source = _SHIELD_SOURCE.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith(("*", "/*", "//"))
        )
        self.assertNotIn("view.getUrl()", code)
        self.assertNotIn("view.", code)

    def test_the_learned_host_sets_are_thread_safe(self) -> None:
        """Written from the worker thread, read from the main thread."""

        source = _SHIELD_SOURCE.read_text(encoding="utf-8")
        for name in ("declaredPlayerHosts", "hoppedPlayerHosts", "playerDomains",
                     "assetDomains", "blockedHosts"):
            declaration = [
                line for line in source.splitlines()
                if f"Set<String> {name} =" in line
            ]
            self.assertEqual(len(declaration), 1, name)
            self.assertIn("ConcurrentHashMap.newKeySet()", declaration[0], name)


if __name__ == "__main__":
    unittest.main()
