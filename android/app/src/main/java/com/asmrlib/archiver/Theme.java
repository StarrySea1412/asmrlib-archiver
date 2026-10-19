package com.asmrlib.archiver;

import android.content.Context;

/**
 * The palette, in one place, for the native chrome.
 *
 * <p>Before this existed the app carried three different accent blues: the
 * shell's progress ring and its ready-state pill used {@code #5c7ee6} →
 * {@code #8066cb}, the download centre's icon plate and the shield's refusal
 * page used {@code #7aa2ff} → {@code #a78bfa}, and the launcher icon used the
 * second pair again. The page background was three values too - the splash
 * {@code #07090d}, the window {@code #0a0d14}, the web page {@code #0a0b0f}.
 * Each difference is small enough to read as a mistake rather than a choice,
 * and a screen full of near-misses is most of what makes a UI look unfinished.
 *
 * <p>These constants mirror {@code mobile_skin}'s tokens. The two files are
 * each the single source of truth for their own half, and
 * {@code tests/test_mobile.py::test_the_web_and_native_accents_are_the_same_colour}
 * fails if they ever disagree, because "the same colour" is a property of the
 * app rather than of either half of it.
 *
 * <p><b>Product-owned dark.</b> An earlier revision kept a full light half and
 * let {@code isNight} follow the system toggle. The product is a video player
 * and its bundled pages pin {@code color-scheme: dark}; the Liquid Glass
 * material below ({@code GlassDrawable}, the FAB, the download panel) is built
 * as a dark optical surface, and a dark glass control over a light page reads
 * as a rendering fault rather than as depth. Splitting the material per theme
 * would mean maintaining two contrast systems for a surface the user mostly
 * sees at night - so the native half pins the dark palette in both system
 * modes, exactly as the web half already does under
 * {@code prefers-color-scheme: light}.
 *
 * <p>The {@code Context} the accessors take is kept on purpose: every call
 * site reads the same either way, and if a theme-aware surface is ever added
 * again the seam to ask at is already there. {@code isNight} answers
 * deterministically rather than reading {@code uiMode}, which is what keeps a
 * cold start one colour - the splash, the window and the page cannot disagree
 * when none of them consults the system.
 *
 * <p>Deliberately a plain class of constants and not a resource: the shield
 * builds its refusal page as a string with the colour inlined, and the ring
 * wants ints, so a shared Java value is the only form both can consume without
 * one of them re-encoding it.
 */
public final class Theme {

    private Theme() {
    }

    // ------------------------------------------------------------- brand

    /** Primary accent. Mirrors mobile_skin's {@code --accent} (dark). */
    public static final int ACCENT = 0xFF7C9CFF;

    /** Second stop of the brand gradient. Mirrors {@code --accent-strong}. */
    public static final int ACCENT_ALT = 0xFF9B7CF0;

    /**
     * Text/icon colour that sits on top of the accent.
     *
     * <p>This one is easy to get backwards: the dark theme's accent is a pale
     * blue, so what reads on it is near-black - not white. The native chrome
     * used {@code Color.WHITE} here for a long time, which is a 2.6:1 pairing
     * on {@code #7c9cff}, while the web pages had already switched to
     * {@code --text-on-accent} and looked right. Both halves now ask.
     */
    public static final int ON_ACCENT = 0xFF06101F;

    /** The brand gradient, low stop first, for anything that animates it. */
    public static final int[] BRAND_RAMP = {ACCENT, ACCENT_ALT};

    // ---------------------------------------------------------- surfaces

    /**
     * The page background. The web pages paint {@code --bg}, the shell paints
     * its splash with {@link #bg(Context)}, and the window behind the WebView
     * is {@code app_bg}. All three have to be the same value or a cold start
     * shows a visible step - and it did.
     */
    public static final int BG = 0xFF0A0B0F;

    /** Raised card fill. Mirrors {@code --surface}. */
    public static final int SURFACE = 0xFF15171E;

    /** Second-level raised fill, for rows inside a card. */
    public static final int SURFACE_2 = 0xFF1E212A;

    // -------------------------------------------------------------- text

    /** Primary text. Mirrors {@code --text}. */
    public static final int TEXT = 0xFFF1F3F8;

    /** Secondary text. Mirrors {@code --text-2}. */
    public static final int TEXT_2 = 0xFFA2ACBD;

    /** Tertiary text, for hints and disabled labels. Mirrors {@code --text-3}. */
    public static final int TEXT_3 = 0xFF6E7A8C;

    // ------------------------------------------------------------ scrims

    /**
     * What a modal scrim lays over the page to push it back.
     *
     * <p>On a dark page the darkening agent is the page colour itself: laying
     * more of {@link #BG} down is exactly "less light". So this is {@link #BG}
     * and the FAB menu keeps the weight it always had.
     */
    public static final int SCRIM = BG;

    // --------------------------------------------------------- semantics

    /** Refusal, deletion, failure. Mirrors {@code --danger}. */
    public static final int DANGER = 0xFFFF7A7A;

    /** Completed, online, safe to delete the source. */
    public static final int SUCCESS = 0xFF5AD39A;

    /**
     * Hairline separator. {@code --sep} is {@code rgba(255,255,255,.085)},
     * which is {@code 0x16} of alpha - so the same value, said twice.
     */
    public static final int SEP = 0x16FFFFFF;

    /** The stronger hairline, for the edge of a floating surface. */
    public static final int SEP_STRONG = 0x2BFFFFFF;

    /**
     * The neutral fill behind chips and quiet buttons. {@code --fill} is
     * {@code rgba(255,255,255,.065)} - {@code 0x11} of alpha.
     */
    public static final int FILL = 0x11FFFFFF;

    // ------------------------------------------------------- CSS literals

    /** {@link #BG} as a CSS hex literal, for the pages the shield serves. */
    public static final String BG_CSS = "#0a0b0f";

    /** {@link #ACCENT} as a CSS hex literal. */
    public static final String ACCENT_CSS = "#7c9cff";

    /** {@link #ACCENT_ALT} as a CSS hex literal. */
    public static final String ACCENT_ALT_CSS = "#9b7cf0";

    /** {@link #ON_ACCENT} as a CSS hex literal. */
    public static final String ON_ACCENT_CSS = "#06101f";

    /** {@link #SURFACE} as a CSS hex literal. */
    public static final String SURFACE_CSS = "#15171e";

    /** {@link #TEXT} as a CSS hex literal. */
    public static final String TEXT_CSS = "#f1f3f8";

    /** {@link #TEXT_2} as a CSS hex literal. */
    public static final String TEXT_2_CSS = "#a2acbd";

    /** {@link #DANGER} as a CSS hex literal. */
    public static final String DANGER_CSS = "#ff7a7a";

    /** {@code --danger-soft} as a CSS literal, for the refusal page's badge. */
    public static final String DANGER_SOFT_CSS = "rgba(255,122,122,.14)";

    /** The hairline as CSS, matching {@code --sep}. */
    public static final String SEP_CSS = "rgba(255,255,255,.085)";

    /** The stronger hairline as CSS, matching {@code --sep-strong}. */
    public static final String SEP_STRONG_CSS = "rgba(255,255,255,.17)";

    // --------------------------------------------------------- selection

    /**
     * Whether the system is in its dark theme.
     *
     * <p>The product material is intentionally dark.  Following the phone's
     * light/dark toggle made the WebView and native chrome disagree during
     * a cold start, and it undermined the Liquid Glass contrast system.
     * Keep the context parameter for source compatibility with the colour
     * accessors, but make the selection explicit and deterministic.
     */
    public static boolean isNight(Context context) {
        return true;
    }

    private static int withAlpha(int colour, int alpha) {
        return (colour & 0x00FFFFFF) | ((alpha & 0xFF) << 24);
    }

    // ------------------------------------------- theme-following accessors

    /*
     * The accessors keep their Context parameter so the call sites did not
     * have to change when the light half was retired, and so a surface that
     * genuinely needs to ask the system again has a seam to ask at. They all
     * answer from the dark constants.
     */

    /** The page background for the current system theme. */
    public static int bg(Context context) {
        return BG;
    }

    /** {@link #bg(Context)} at a given alpha, for washes that follow the page. */
    public static int bg(Context context, int alpha) {
        return withAlpha(BG, alpha);
    }

    /**
     * The colour a modal scrim lays over the page, for the current theme.
     *
     * <p>Not {@link #bg(Context, int)}: the scrim's job is to make what is
     * behind it recede. On the product's dark page, more of the page colour is
     * exactly that, but naming the intent at the call site is what stops a
     * future reader from "simplifying" a scrim into a background wash.
     */
    public static int scrim(Context context) {
        return SCRIM;
    }

    /** {@link #scrim(Context)} at a given alpha - how a scrim is used. */
    public static int scrim(Context context, int alpha) {
        return withAlpha(scrim(context), alpha);
    }

    /** The raised-card fill for the current system theme. */
    public static int surface(Context context) {
        return SURFACE;
    }

    /** {@link #surface(Context)} at a given alpha. */
    public static int surface(Context context, int alpha) {
        return withAlpha(surface(context), alpha);
    }

    /** The row-inside-a-card fill for the current system theme. */
    public static int surface2(Context context) {
        return SURFACE_2;
    }

    /** The primary text colour for the current system theme. */
    public static int text(Context context) {
        return TEXT;
    }

    /** {@link #text(Context)} at a given alpha, for de-emphasised labels. */
    public static int text(Context context, int alpha) {
        return withAlpha(text(context), alpha);
    }

    /** The secondary text colour for the current system theme. */
    public static int text2(Context context) {
        return TEXT_2;
    }

    /** {@link #text2(Context)} at a given alpha. */
    public static int text2(Context context, int alpha) {
        return withAlpha(text2(context), alpha);
    }

    /** The tertiary text colour for the current system theme. */
    public static int text3(Context context) {
        return TEXT_3;
    }

    /** The accent for the current system theme. */
    public static int accent(Context context) {
        return ACCENT;
    }

    /** The gradient's far stop for the current system theme. */
    public static int accentAlt(Context context) {
        return ACCENT_ALT;
    }

    /** {@link #accent(Context)} at a given alpha. */
    public static int accent(Context context, int alpha) {
        return withAlpha(accent(context), alpha);
    }

    /** What reads on top of {@link #accent(Context)}. */
    public static int onAccent(Context context) {
        return ON_ACCENT;
    }

    /** {@link #onAccent(Context)} at a given alpha. */
    public static int onAccent(Context context, int alpha) {
        return withAlpha(onAccent(context), alpha);
    }

    /** The refusal colour for the current system theme. */
    public static int danger(Context context) {
        return DANGER;
    }

    /** {@link #danger(Context)} at a given alpha — a destructive wash, the
     *  same derived-tint rule as {@code scrim}/{@code fill}. */
    public static int danger(Context context, int alpha) {
        return withAlpha(danger(context), alpha);
    }

    /**
     * The far stop of the destructive pill's gradient.
     *
     * <p>Native-only: the web's {@code .button-danger} is a flat soft red, so
     * there is no token to mirror. It lived as a {@code Color.parseColor(
     * "#dc6fbf")} inside {@code DownloadsActivity} until the palette audit,
     * which is one definition too many for a colour that has to move with
     * {@link #danger(Context)}.
     */
    public static final int DANGER_ALT = 0xFFDC6FBF;

    /**
     * What reads on top of {@link #DANGER}/{@link #DANGER_ALT}.
     *
     * <p>Both are saturated, so this does not follow the system - always
     * white.
     */
    public static final int ON_DANGER = 0xFFFFFFFF;

    /** The hairline for the current system theme. */
    public static int sep(Context context) {
        return SEP;
    }

    /** The stronger hairline for the current system theme. */
    public static int sepStrong(Context context) {
        return SEP_STRONG;
    }

    /** The quiet-fill wash for the current system theme. */
    public static int fill(Context context) {
        return FILL;
    }

    /**
     * {@link #fill(Context)} at a given alpha.
     *
     * <p>Callers that used to write {@code Color.argb(34, 255, 255, 255)} - a
     * translucent white chip - need this so the wash has one definition.
     */
    public static int fill(Context context, int alpha) {
        return withAlpha(fill(context), alpha);
    }

    // ------------------------------------------------- pinned-dark family

    /*
     * The offline player's chrome is a video surface: the picture is the only
     * thing that should be bright, and every phone's own player agrees. It
     * therefore pins the dark half instead of following the system - a choice,
     * not an oversight. What it must not do is reach for a theme-following
     * accessor and end up light in light mode, so these take no Context and
     * say "dark" in their name.
     */

    /** {@link #BG} at a given alpha. Player chrome only. */
    public static int darkBg(int alpha) {
        return withAlpha(BG, alpha);
    }

    /** {@link #SURFACE} at a given alpha. Player chrome only. */
    public static int darkSurface(int alpha) {
        return withAlpha(SURFACE, alpha);
    }

    /** {@link #TEXT} at a given alpha. Player chrome only. */
    public static int darkText(int alpha) {
        return withAlpha(TEXT, alpha);
    }

    /** {@link #ACCENT} at a given alpha. Player chrome only. */
    public static int darkAccent(int alpha) {
        return withAlpha(ACCENT, alpha);
    }
}
