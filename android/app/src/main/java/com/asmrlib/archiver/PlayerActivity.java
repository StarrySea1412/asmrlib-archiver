package com.asmrlib.archiver;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.ColorFilter;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.graphics.drawable.Drawable;
import android.graphics.RectF;
import android.media.AudioAttributes;
import android.media.MediaMetadataRetriever;
import android.media.MediaPlayer;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import android.view.GestureDetector;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.ImageButton;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.Locale;

import java.net.HttpURLConnection;

/**
 * In-app player for a file this shell saved itself.
 *
 * <p>The download center used to hand every finished task to {@code
 * ACTION_VIEW} with the mime type derived from its file name. That is fine for
 * a direct {@code .mp4} — the system gallery claims {@code video/*} — but the
 * in-app HLS saver produces concatenated MPEG-TS and therefore reports
 * {@code video/mp2t}, and on a bare emulator (and on plenty of real tablets)
 * <em>nothing</em> resolves {@code video/mp2t}: {@code ACTION_VIEW} threw
 * {@code ActivityNotFoundException}, so 打开 on a 已完成 row did nothing at
 * all. A save the user cannot play is not a save.
 *
 * <p>The platform can still <em>decode</em> that file. On API 34 {@code
 * dumpsys media.extractor} registers {@code MPEG2-PS/TS Extractor ... supports:
 * m2p m2ts mts ts}, and H.264 + AAC are universal. What is missing is an
 * <em>activity</em> that claims the intent, so the shell supplies one, with no
 * third-party player required.
 *
 * <p>Deliberately not exported and carrying no intent filter: the shell starts
 * it explicitly with a one-shot read grant on the {@code content://} URI the
 * {@link DownloadFileProvider} serves, so the app never becomes a global
 * handler for a media type it does not own.
 *
 * <h2>Why this owns a SurfaceView instead of using VideoView</h2>
 *
 * <p>{@code VideoView} bundles two things that are wrong here. It owns its
 * {@code MediaPlayer} <em>and</em> it drives {@code MediaController}, which is
 * a floating popup window: every show/hide re-measures the video surface and
 * re-parents a window, so the transport bar flickers in over the frame it is
 * supposed to be controlling. And because the player is welded to the
 * VideoView's surface, the surface being recreated — which is exactly what a
 * rotation does, and this activity's manifest entry deliberately absorbs
 * rotations rather than recreating — takes the player down with it. Owning the
 * surface directly fixes both: the transport bar is an ordinary view in this
 * activity's own hierarchy, and the player outlives a surface swap by
 * remembering where it was.
 *
 * <h2>What the transport bar is</h2>
 *
 * <p>Hand-drawn, like the rest of this shell's native chrome: a gradient
 * progress rail, a knob, elapsed/total time, and −10s / play-pause / +10s
 * targets sized for a thumb. Nothing is a framework widget with a theme of its
 * own, so the bar cannot drift away from the app's palette the way the stock
 * {@code MediaController} did — that grey slab was the one surface in the app
 * that looked like it belonged to a different product.
 */
public class PlayerActivity extends Activity implements SurfaceHolder.Callback {

    private static final String TAG = "asmrlib-player";

    /** {@code content://} URI of the saved artifact, as a string. */
    public static final String EXTRA_URI = "com.asmrlib.archiver.PLAY_URI";
    /** The task's file name, shown as the title. */
    public static final String EXTRA_TITLE = "com.asmrlib.archiver.PLAY_TITLE";
    /** Which download record this is, so 重命名/删除 can be handed back to the
     *  download centre (the one place that owns those two destructive paths). */
    public static final String EXTRA_TASK_ID = "com.asmrlib.archiver.PLAY_TASK_ID";
    /** Byte size the download centre already measured — cheaper and more
     *  truthful than re-statting through the content resolver. */
    public static final String EXTRA_BYTES = "com.asmrlib.archiver.PLAY_BYTES";
    /** The record's own state, so the badge never claims 已完成 for a file the
     *  download centre is still writing. */
    public static final String EXTRA_STATE = "com.asmrlib.archiver.PLAY_STATE";
    /** When the bytes were last written, so the detail table can show it. */
    public static final String EXTRA_SAVED_AT = "com.asmrlib.archiver.PLAY_SAVED_AT";
    /** Remote media URL (HLS manifest or direct file) to stream through
     *  {@link StreamProxy}. Present means "play a stream", and the offline
     *  extras (task id, bytes, state) are all absent by definition. */
    public static final String EXTRA_REMOTE_URL = "com.asmrlib.archiver.PLAY_REMOTE_URL";
    /** What the remote URL is: {@code hls} or {@code direct}. */
    public static final String EXTRA_REMOTE_KIND = "com.asmrlib.archiver.PLAY_REMOTE_KIND";
    /** The page the URL was resolved from — player hosts answer only
     *  requests that carry it. */
    public static final String EXTRA_REMOTE_REFERER = "com.asmrlib.archiver.PLAY_REMOTE_REFERER";
    /** The work's poster URL, for the artwork-derived backdrop. */
    public static final String EXTRA_REMOTE_COVER = "com.asmrlib.archiver.PLAY_REMOTE_COVER";
    /**
     * The track queue behind this page (Eara's continuous playback): a JSON
     * array of {@code {"u":url,"l":label}} plus the index this page starts
     * at.  Written once by the bridge when the resolve handed back more than
     * one playable source; on completion the page walks forward instead of
     * stopping.  Absent (or single-entry) means exactly the old behaviour.
     */
    public static final String EXTRA_REMOTE_QUEUE = "com.asmrlib.archiver.PLAY_REMOTE_QUEUE";
    public static final String EXTRA_REMOTE_INDEX = "com.asmrlib.archiver.PLAY_REMOTE_INDEX";

    /** Result payload: which of the two record actions the user asked for. */
    public static final String RESULT_ACTION = "com.asmrlib.archiver.PLAY_ACTION";
    public static final String RESULT_TASK_ID = "com.asmrlib.archiver.PLAY_ACTION_TASK_ID";
    public static final String ACTION_RENAME = "rename";
    public static final String ACTION_DELETE = "delete";

    private static final long SEEK_STEP_MS = 10_000L;
    /** 4 Hz is smooth for a rail that moves at most a few px per tick, and
     *  cheap enough that it never competes with the decoder for the frame. */
    private static final long TICK_MS = 250L;
    private static final long CONTROLS_TIMEOUT_MS = 4_000L;

    private static final String STATE_RUNNING = "running";
    private static final String STATE_SUCCESS = "success";
    private static final String STATE_FAILED = "failed";
    private static final String STATE_LOST = "lost";

    /** The same preferences file the download centre and the shell write. */
    private static final String PREFS = "asmrlib";

    /** How many times a failed stream is re-prepared before the failure is
     *  reported as final. Three, not "forever": a stream that needs a fourth
     *  attempt is not buffering, it is broken, and the honest thing then is to
     *  say so and hand the user the exit. */
    private static final int MAX_AUTO_RETRIES = 3;
    /** Seconds between a failure and the next {@code prepareAsync}. The head of
     *  a stream is exactly where the extractor races the writer, and that race
     *  settles on its own — which is why this is a delay and not an instant
     *  retry that would fail for the same reason three times in a row. */
    private static final int RETRY_COUNTDOWN_SECONDS = 5;
    /** One second per tick, so the label reads as a countdown and not a jump. */
    private static final long RETRY_TICK_MS = 1_000L;

    /**
     * The player's chrome pins the dark half of {@link Theme} rather than
     * following the system theme the web pages do. That is a choice, not an
     * oversight: this is a video surface, the picture is the only thing that
     * should be bright, and every phone's own player agrees. What it must not
     * do is invent a fourth accent blue - which is what it used to do.
     */
    private static final int ACCENT_A = Theme.ACCENT;
    private static final int ACCENT_B = Theme.ACCENT_ALT;
    private static final int TEXT_PRIMARY = Theme.TEXT;
    private static final int TEXT_MUTED = Theme.TEXT_2;

    private SurfaceView surface;
    private SurfaceHolder holder;
    private MediaPlayer player;

    /** Video stage: the surface, its poster, and every overlay that belongs to
     *  the picture. Kept separate from the info column so a rotation can move
     *  the stage without touching the player. */
    private FrameLayout videoFrame;
    /**
     * The immersive floor: a blurred, dimmed enlargement of the poster,
     * laid under the surface so the letterbox bars around a portrait video
     * carry the work's own art instead of dead black. The reference player
     * paints its whole page from the artwork this way; the frame's black
     * stays (it is what reads before any frame decodes), the backdrop only
     * takes over once a poster exists.
     */
    private ImageView backdrop;
    /** The hue wash and the scrim of the Eara backdrop stack (page → hue →
     *  blurred art → scrim). Views, not colour filters, so each layer can
     *  appear only once its input exists and animate independently. */
    private View backdropTint;
    private View backdropScrim;
    private ImageView cover;
    /** Portrait-only column below the stage: title, facts, and the two record
     *  actions. Hidden outright in landscape — there is no room for it and a
     *  squeezed half-column is worse than none. */
    private ScrollView infoScroll;
    private ImageView thumb;
    private TextView metaTitle;
    private TextView metaSubtitle;
    private TextView metaBadge;
    private LinearLayout metaRows;
    /** Poster + facts are read off-thread; this is what they land in. */
    private int metaDurationMs = -1;
    private int metaWidth;
    private int metaHeight;
    private long mediaBytes = -1L;
    private String taskId = "";
    private String mediaState = "";
    private long mediaSavedAt = 0L;
    private int metaBitrate;
    /** Crop-to-fill: a 16:9 recording on a 9:16 phone can only ever be 607px
     *  tall if it is letterboxed, so "bigger" needs an opt-in fill. */
    private boolean fillScreen;
    private boolean landscapeRequested;
    /** The video-slot overlay chips (Eara style): 填充 and 全屏 live on the
     *  video itself now, not in a top-bar slab. */
    private ImageButton fillButton;
    private ImageButton fullscreenButton;
    /** 倍速 chip on the video slot: same overlay language as 填充/全屏, text
     *  because "1.5x" says it and a glyph would not. Null on old devices. */
    private TextView speedChip;
    /** 息屏播放: hands the audio to PlaybackService and leaves the surface. */
    private ImageButton sleepButton;
    /** The "audio is playing in the background" card, built on demand. */
    private View backgroundBar;

    /** Playback speed: the ladder every audio app on the platform settles on.
     *  1.0 is deliberately out of the middle meta-position (last) so the
     *  cycle reads slow→fast→正常. */
    private static final float[] SPEED_STEPS = {0.75f, 1.0f, 1.5f, 2.0f};
    private float playbackSpeed = 1.0f;
    /** The queue panel slide-over, built once and toggled. */
    private View queuePanel;

    private View controls;
    /** 滑动快进: the drag state between onDown and the release that seeks. */
    private float scrubStartX;
    private int scrubSeconds;
    private boolean scrubbing;
    private TextView scrubBubble;
    private LoadingRing loading;
    private PlayerSeekBar seek;
    private TextView elapsedView;
    private TextView totalView;
    private PlayPauseButton playPause;

    private LinearLayout errorPanel;
    private TextView errorText;
    private TextView retryCountdownLabel;

    /** The square artwork of audio mode (Eara's ArtworkBox): 28dp-rounded,
     *  clamped to 360dp, floating on the artwork-derived backdrop. */
    private ImageView audioCover;
    /** Audio mode identity block (Eara's ClassicPlayerIdentity). */
    private TextView identityTitle;
    private TextView identityMeta;
    /** The video page's header title. advanceQueue keeps it on the track it
     *  just switched to — a bar naming track one over track four's audio is
     *  the kind of lie that reads as a bug. */
    private TextView headerTitleView;
    /** The activity's outermost view — where full-screen overlays attach when
     *  there is no video stage to hold them (audio mode). */
    private View chromeRoot;
    /** Audio mode's full-screen overlay layer: error panel, loading ring,
     *  background bar — anything that needs Gravity.CENTER over the column. */
    private FrameLayout overlayHost;

    private Uri mediaUri;
    private String mediaTitle = "";
    /** Remote streaming state. {@code remoteUrl} non-empty means this page is
     *  playing a stream through {@link StreamProxy}; {@code proxiedUrl} is the
     *  loopback URL the player actually opens (also what 息屏播放 hands the
     *  service, so the service and this page share one proxy). */
    private String remoteUrl = "";
    private String remoteKind = "";
    private String remoteReferer = "";
    private String remoteCover = "";
    private String proxiedUrl = "";
    /** The work's track list ({@link #EXTRA_REMOTE_QUEUE}) and where in it
     *  this page sits.  Empty queue = single track, the old behaviour. */
    private org.json.JSONArray remoteQueue = null;
    private int remoteQueueIndex = 0;
    /**
     * Audio layout (Eara's now-playing page) instead of the video stage.
     * Decided once, in onCreate: a remote source the resolver already called
     * an audio file is audio from the first frame; everything else opens as
     * video, because a wrong guess the other way would put a live video
     * surface inside a square artwork slot.
     */
    private boolean audioMode;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Runnable ticker = new Runnable() {
        @Override
        public void run() {
            syncProgress();
            if (player != null && player.isPlaying()) ui.postDelayed(this, TICK_MS);
        }
    };
    private final Runnable hideControls = new Runnable() {
        @Override
        public void run() {
            if (!userSeeking && player != null && player.isPlaying()) setControlsVisible(false);
        }
    };

    /** Position carried across a surface swap (rotation, split-screen resize). */
    private int pendingSeekMs;
    private boolean resumeOnSurface;
    private boolean userSeeking;
    private boolean prepared;
    private boolean errorSeen;
    private int durationMs = -1;

    /** The resume record this page reads and writes. Empty means "remember
     *  nothing", which is what a file with no identity at all gets. */
    private String resumeKey = "";
    /** Where the stored record put us, so the toast can name the place. -1 for
     *  none. Deliberately not {@code pendingSeekMs}: that also carries a
     *  rotation and the 「回到画面播放」 handover, neither of which is a resume
     *  and neither of which may announce itself as one. */
    private long resumeAnnounceMs = -1L;

    /** Last real failure, so the honest message names it rather than guessing. */
    private int errorWhat;
    private int errorExtra;
    /** Whether the extractor had already accepted this file when it failed.
     *  Only a stream can fail mid-play, so this switches {@link #describe}
     *  between "the device cannot decode this" and "the delivery broke". */
    private boolean errorWasMidPlay;

    /** Retries still owed to the current failure; 0 means "not retrying". */
    private int autoRetryRemaining;
    /** Retries already fired for the current failure, for the honest tail line
     *  on the final panel ("已自动重试 2 次仍未成功"). */
    private int autoRetrySpent;
    private int autoRetrySecondsLeft;
    private Runnable autoRetryRunnable;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Intent intent = getIntent();
        mediaUri = parseUri(intent);
        if (intent != null) {
            String t = intent.getStringExtra(EXTRA_TITLE);
            mediaTitle = t == null ? "" : t;
            taskId = String.valueOf(intent.getStringExtra(EXTRA_TASK_ID));
            if ("null".equals(taskId)) taskId = "";
            mediaBytes = intent.getLongExtra(EXTRA_BYTES, -1L);
            String st = intent.getStringExtra(EXTRA_STATE);
            mediaState = st == null ? "" : st;
            mediaSavedAt = intent.getLongExtra(EXTRA_SAVED_AT, 0L);
            String ru = intent.getStringExtra(EXTRA_REMOTE_URL);
            remoteUrl = ru == null ? "" : ru.trim();
            String rk = intent.getStringExtra(EXTRA_REMOTE_KIND);
            remoteKind = rk == null ? "" : rk.trim();
            String rr = intent.getStringExtra(EXTRA_REMOTE_REFERER);
            remoteReferer = rr == null ? "" : rr.trim();
            String rc = intent.getStringExtra(EXTRA_REMOTE_COVER);
            remoteCover = rc == null ? "" : rc.trim();
            String rq = intent.getStringExtra(EXTRA_REMOTE_QUEUE);
            remoteQueueIndex = intent.getIntExtra(EXTRA_REMOTE_INDEX, 0);
            if (rq != null && !rq.trim().isEmpty()) {
                try {
                    remoteQueue = new org.json.JSONArray(rq);
                } catch (Exception e) {
                    Log.w(TAG, "remote queue unparsable", e);
                    remoteQueue = null;
                }
            }
        }
        // A stream is registered with the proxy up front, on the main thread:
        // MediaPlayer's first connection must find the route already waiting,
        // and register() can throw (no port) — that failure belongs to the
        // error panel, not a crash.
        if (!remoteUrl.isEmpty()) {
            StreamProxy.setSink(message -> Log.w(TAG, "stream-proxy: " + message));
            try {
                proxiedUrl = StreamProxy.register(remoteUrl, remoteReferer);
                mediaUri = Uri.parse(proxiedUrl);
            } catch (Exception e) {
                Log.w(TAG, "stream proxy registration failed", e);
                remoteUrl = "";
            }
        }
        audioMode = "direct".equals(remoteKind) && isAudioExtension(remoteUrl);
        resumeKey = PlaybackResume.keyFor(taskId,
                !remoteUrl.isEmpty() ? remoteUrl
                    : mediaUri == null ? "" : mediaUri.toString());
        // Where the user left off, if anywhere. The record carries the length
        // it was written with, so the "near the end" test does not have to wait
        // for the file to be sniffed — and a spent record is dropped here
        // rather than being left to misfire on some later open.
        long[] saved = readResume(resumeKey);
        if (saved != null) {
            long start = PlaybackResume.startPosition(saved[0], saved[1]);
            if (start > 0) {
                pendingSeekMs = (int) start;
                resumeAnnounceMs = start;
            } else {
                dropResume(resumeKey);
            }
        }
        buildChrome();
        if (mediaUri == null) {
            showError("找不到要播放的文件。");
            return;
        }
        if (!remoteCover.isEmpty()) {
            loadRemoteCover();
        }
        // Poster + facts come from the file itself, on a worker: a
        // MediaMetadataRetriever sniff of a gigabyte TS is not main-thread
        // work, and the page must be usable before it answers.
        loadMetadata();
        // The surface callback does the actual open: a SurfaceView has no
        // surface to render into until it is attached, and a MediaPlayer
        // prepared against a null surface decodes audio with no picture.
    }

    /**
     * The remote work's poster, fetched through the same route the stream
     * uses. It is the one bit of chrome a stream has no other way to get —
     * a MediaMetadataRetriever cannot answer before the decoder does, and a
     * player page with no artwork reads as a broken file.
     */
    private void loadRemoteCover() {
        final String coverUrl = remoteCover;
        final String referer = remoteReferer;
        new Thread(new Runnable() {
            @Override
            public void run() {
                Bitmap bitmap = null;
                try {
                    HttpURLConnection conn = (HttpURLConnection)
                            new java.net.URL(coverUrl).openConnection();
                    conn.setConnectTimeout(15000);
                    conn.setReadTimeout(20000);
                    conn.setRequestProperty("User-Agent",
                            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                                + "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36");
                    if (!referer.isEmpty()) {
                        conn.setRequestProperty("Referer", referer);
                    }
                    conn.connect();
                    if (conn.getResponseCode() == 200) {
                        java.io.InputStream in = conn.getInputStream();
                        try {
                            bitmap = android.graphics.BitmapFactory.decodeStream(in);
                        } finally {
                            in.close();
                        }
                    }
                    conn.disconnect();
                } catch (Exception e) {
                    Log.w(TAG, "remote cover fetch failed", e);
                }
                if (bitmap != null) publishCover(bitmap);
            }
        }, "asmrlib-cover").start();
    }

    /** Cover art that arrived over the network, straight onto the poster. */
    private void publishCover(final Bitmap bitmap) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (isFinishing()) return;
                if (cover != null && cover.getVisibility() == View.VISIBLE) {
                    cover.setImageBitmap(bitmap);
                }
                if (thumb != null) thumb.setImageBitmap(bitmap);
                if (audioCover != null) audioCover.setImageBitmap(bitmap);
                applyArtworkBackdrop(bitmap);
            }
        });
    }

    /** Whether {@code url} names an audio file (audio mode candidates). */
    private static boolean isAudioExtension(String url) {
        String n = url == null ? "" : url.toLowerCase(Locale.US);
        int query = n.indexOf('?');
        if (query >= 0) n = n.substring(0, query);
        return n.endsWith(".m4a") || n.endsWith(".mp3") || n.endsWith(".aac")
            || n.endsWith(".ogg") || n.endsWith(".opus") || n.endsWith(".wav")
            || n.endsWith(".flac");
    }

    /** Everything the retriever can answer without decoding a picture. */
    private void publishFacts(final int fw, final int fh, final int fd, final int fb) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (isFinishing()) return;
                metaWidth = fw;
                metaHeight = fh;
                if (fd > 0) metaDurationMs = fd;
                metaBitrate = fb;
                applyOrientationLayout();
                renderMeta();
            }
        });
    }

    /**
     * One frame, two jobs: the full-bleed poster over the surface and the small
     * one in the detail card. Arrives after the facts, on purpose.
     */
    private void publishFrame(final Bitmap poster) {
        if (poster == null) return;
        final Bitmap floor = blurredBackdrop(poster);
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (isFinishing()) return;
                if (cover != null) cover.setImageBitmap(poster);
                if (thumb != null) thumb.setImageBitmap(poster);
                if (backdrop != null) {
                    // A 40% black wash over the art: bright covers must not
                    // outshine the video clamped in front of them.
                    backdrop.setColorFilter(0x66000000, android.graphics.PorterDuff.Mode.SRC_OVER);
                    backdrop.setImageBitmap(floor);
                    backdrop.setVisibility(View.VISIBLE);
                }
                applyArtworkBackdrop(poster);
            }
        });
    }

    /**
     * The Eara backdrop: the whole page tinted from the artwork, not a flat
     * gradient. The hue is sampled once per bitmap (a 16×16 average in
     * HSV — Eara derives a Monet seed from 256px of center crop, but the
     * player page only needs one tint that does not fight the art), then
     * layered: page gradient → hue wash → blurred art → scrim. Applied to
     * whatever surfaces exist; the layer views are built in buildChrome and
     * this only feeds them.
     */
    private void applyArtworkBackdrop(Bitmap art) {
        if (art == null || backdropTint == null || backdropScrim == null) return;
        int hue = artworkHue(art);
        backdropTint.setBackgroundColor(hue);
        backdropTint.setVisibility(View.VISIBLE);
        backdropScrim.setVisibility(View.VISIBLE);
    }

    /**
     * One colour that stands for the artwork: the mean of a 16×16 reduction,
     * pulled toward this app's accent family so a saturated red cover still
     * reads as ours. Clamped into the pastel band Eara keeps its tints in
     * (S 0.24–0.58, L 0.42–0.56 in dark), because the raw mean of an ASMR
     * cover is usually near-black or near-white and both vanish against the
     * stage.
     */
    static int artworkHue(Bitmap art) {
        Bitmap small = Bitmap.createScaledBitmap(art, 16, 16, true);
        int[] px = new int[16 * 16];
        small.getPixels(px, 0, 16, 0, 0, 16, 16);
        if (small != art) small.recycle();
        long r = 0, g = 0, b = 0;
        for (int p : px) {
            r += p >> 16 & 0xff;
            g += p >> 8 & 0xff;
            b += p & 0xff;
        }
        float[] hsv = new float[3];
        Color.RGBToHSV((int) (r / px.length), (int) (g / px.length),
                (int) (b / px.length), hsv);
        float s = Math.max(0.24f, Math.min(0.58f, hsv[1]));
        float v = Math.max(0.42f, Math.min(0.56f, hsv[2]));
        if (hsv[1] < 0.06f) {
            // A grey cover gets the app's own accent hue rather than a
            // washed-out grey wash over the whole page.
            hsv[0] = 228f;
            s = 0.30f;
        }
        return Color.HSVToColor(new float[]{hsv[0], s, v});
    }

    /**
     * The poster smeared into ambient light. A real Gaussian costs more than
     * the frame is worth; two bilinear passes do the same visual work - crush
     * the bitmap to a thumb, then stretch it back - and the decoder is never
     * kept waiting on a convolution.
     */
    private static Bitmap blurredBackdrop(Bitmap poster) {
        int w = poster.getWidth(), h = poster.getHeight();
        Bitmap small = Bitmap.createScaledBitmap(poster,
                Math.max(1, w / 16), Math.max(1, h / 16), true);
        return Bitmap.createScaledBitmap(small,
                Math.max(1, w / 5), Math.max(1, h / 5), true);
    }

    /**
     * A frame worth showing. Every sample is a sync frame, so this is cheap;
     * the first one bright enough to be a picture wins, and a black one is only
     * used if the whole file is dark.
     *
     * <p>{@code durationMs} is milliseconds (that is what
     * {@code METADATA_KEY_DURATION} reports) and {@code getFrameAtTime} wants
     * microseconds — mixing the two put every sample inside the first second,
     * which on a fade-from-black is exactly the black rectangle this exists to
     * avoid.
     */
    private static Bitmap pickFrame(MediaMetadataRetriever r, int durationMs) {
        long[] at = durationMs > 0
                ? new long[]{durationMs / 10L * 1000L, durationMs / 3L * 1000L,
                             durationMs / 2L * 1000L, 1_000_000L}
                : new long[]{1_000_000L, 3_000_000L, 10_000_000L};
        Bitmap darkest = null;
        for (long us : at) {
            Bitmap f = null;
            try {
                f = r.getFrameAtTime(us, MediaMetadataRetriever.OPTION_CLOSEST_SYNC);
            } catch (Exception e) {
                Log.w(TAG, "frame grab failed at " + us + "us", e);
            }
            if (f == null) continue;
            if (darkest == null) darkest = f;
            if (luminance(f) > 24f) return f;
        }
        return darkest;
    }

    /** Mean luma of a 16x16 reduction — enough to tell a picture from a fade-in. */
    private static float luminance(Bitmap b) {
        if (b == null) return 0f;
        Bitmap small = Bitmap.createScaledBitmap(b, 16, 16, true);
        int[] px = new int[16 * 16];
        small.getPixels(px, 0, 16, 0, 0, 16, 16);
        if (small != b) small.recycle();
        long sum = 0;
        for (int p : px) {
            sum += ((p >> 16 & 0xff) * 299 + (p >> 8 & 0xff) * 587 + (p & 0xff) * 114) / 1000;
        }
        return sum / (float) px.length;
    }

    /** Accept either the explicit extra or the intent's own data URI. */
    private static Uri parseUri(Intent intent) {
        if (intent == null) return null;
        String raw = intent.getStringExtra(EXTRA_URI);
        if (raw == null || raw.isEmpty()) {
            Uri data = intent.getData();
            raw = data == null ? "" : data.toString();
        }
        if (raw.isEmpty()) return null;
        try {
            Uri parsed = Uri.parse(raw);
            // An opaque URI ("file:name.ts" — no / after the scheme) makes
            // MediaPlayer hand the decoder a null path and throw a message
            // that names nothing (「uri param can not be null」). The file it
            // gestures at is unreachable anyway; fail here with the string
            // logged, where the next report can name it.
            if (parsed.getPath() == null) {
                Log.w(TAG, "opaque media uri: " + raw);
                return null;
            }
            return parsed;
        } catch (Exception e) {
            Log.w(TAG, "unparseable media uri", e);
            return null;
        }
    }

    // ------------------------------------------------------------------ chrome

    private void buildChrome() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackground(pageBackground());
        chromeRoot = root;

        if (audioMode) {
            // FrameLayout root: the column and the overlay layer must stack
            // on top of each other, and a LinearLayout cannot overlay — a
            // MATCH/MATCH overlay sibling there starves the weighted column
            // to zero height (observed live: the artwork vanished).
            FrameLayout audioRoot = new FrameLayout(this);
            audioRoot.setBackground(pageBackground());
            chromeRoot = audioRoot;
            buildAudioChrome(audioRoot);
            setContentView(audioRoot);
            applyOrientationLayout();
            renderAudioIdentity();
            // No surface callback will fire (there is no SurfaceView), so the
            // audio page opens its player directly — the same handoff the
            // video page gets from surfaceCreated.
            if (mediaUri != null) {
                if (PlaybackService.isRunning()) showBackgroundBar();
                else open();
            }
            return;
        }

        // Stage on top, facts below. Landscape collapses the column and hands
        // the whole window to the stage; see applyOrientationLayout().
        videoFrame = new FrameLayout(this);
        // The stage is the video surface's home: black, not the page
        // gradient. The letterbox bars (see applyStageAspect) and the beat
        // before the surface's first frame both read against this.
        videoFrame.setBackgroundColor(Color.BLACK);
        // The surface and the poster with it are re-fitted whenever the
        // stage's size settles (rotation absorbed by the manifest, immersive
        // bar transitions, the portrait clamp) — a stage that changes size
        // without a refit is how the picture ended up stretched.
        videoFrame.addOnLayoutChangeListener(new View.OnLayoutChangeListener() {
            @Override public void onLayoutChange(View v, int left, int top, int right,
                    int bottom, int oldLeft, int oldTop, int oldRight, int oldBottom) {
                if (right - left == oldRight - oldLeft
                        && bottom - top == oldBottom - oldTop) return;
                applyStageAspect();
            }
        });
        root.addView(videoFrame, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));

        // The blurred art floor sits under the surface: wherever the video
        // does not fill the stage (a 16:9 clip clamped into a portrait
        // stage), the bars around it show the work's own picture, dimmed,
        // instead of dead black. A View - not a colour filter on the frame -
        // because it must appear only once a poster exists.
        backdrop = new ImageView(this);
        backdrop.setScaleType(ImageView.ScaleType.CENTER_CROP);
        backdrop.setVisibility(View.GONE);
        videoFrame.addView(backdrop, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT, Gravity.CENTER));

        // Eara's backdrop stack, under the surface: hue wash above the blurred
        // art, scrim above both. Both start GONE — applyArtworkBackdrop is the
        // only thing that reveals them, so a page with no artwork never shows
        // a tint that came from nowhere.
        backdropTint = new View(this);
        backdropTint.setVisibility(View.GONE);
        videoFrame.addView(backdropTint, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT, Gravity.CENTER));
        backdropScrim = new View(this);
        backdropScrim.setBackgroundColor(Color.argb(120, 0, 0, 0));
        backdropScrim.setVisibility(View.GONE);
        videoFrame.addView(backdropScrim, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT, Gravity.CENTER));

        surface = new SurfaceView(this);
        holder = surface.getHolder();
        holder.addCallback(this);
        videoFrame.addView(surface, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT, Gravity.CENTER));

        // The poster covers the black SurfaceView until the first frame lands,
        // so the page opens on the video's own picture instead of a void.
        cover = new ImageView(this);
        cover.setScaleType(ImageView.ScaleType.CENTER_CROP);
        cover.setBackgroundColor(Color.BLACK);
        videoFrame.addView(cover, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT, Gravity.CENTER));

        loading = new LoadingRing(this);
        loading.setVisibility(View.GONE);
        videoFrame.addView(loading, new FrameLayout.LayoutParams(dp(54), dp(54), Gravity.CENTER));

        videoFrame.addView(buildControls(), new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));

        videoFrame.addView(buildErrorPanel(), new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT, Gravity.CENTER));

        infoScroll = new ScrollView(this);
        infoScroll.addView(buildInfoPanel());
        root.addView(infoScroll, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));

        setContentView(root);
        applyOrientationLayout();
        // Size and container are known from the intent, so the column is never
        // blank while the retriever is still sniffing the file.
        renderMeta();
    }

    /**
     * Portrait: a 16:9 stage with the facts underneath, scrollable so a long
     * file name never pushes the actions off-screen. Landscape: the stage takes
     * the whole window, the column is gone, and the system bars go with it —
     * a landscape video letterboxed inside a status bar is the single most
     * "unadapted" thing a player page can do.
     */
    private void applyOrientationLayout() {
        // The audio page has no stage to re-fit: its column reflows on its
        // own (the cover side is width-clamped), and the immersive dance is
        // video-only.
        if (audioMode) {
            exitImmersive();
            return;
        }
        boolean landscape = getResources().getConfiguration().orientation
                == Configuration.ORIENTATION_LANDSCAPE;
        if (videoFrame != null) {
            LinearLayout.LayoutParams lp =
                    (LinearLayout.LayoutParams) videoFrame.getLayoutParams();
            if (landscape) {
                lp.height = 0;
                lp.weight = 1f;
            } else {
                lp.height = stageHeightPortrait();
                lp.weight = 0f;
            }
            videoFrame.setLayoutParams(lp);
        }
        if (infoScroll != null) infoScroll.setVisibility(landscape ? View.GONE : View.VISIBLE);
        if (controls != null) {
            // The top bar needs to clear the status bar in portrait; in
            // landscape there is no status bar to clear.
            controls.setPadding(0, landscape ? 0 : statusBarInset(), 0, 0);
        }
        if (landscape) enterImmersive();
        else exitImmersive();
        // Both fullscreen controls carry the same label, or one of them ends
        // up offering 退出全屏 on a screen that is not fullscreen.
        int fsGlyph = landscape ? PlayerGlyph.COLLAPSE : PlayerGlyph.EXPAND;
        String fsDesc = landscape ? "退出横屏全屏" : "横屏全屏播放";
        if (fullscreenButton != null) setIcon(fullscreenButton, fsGlyph, fsDesc);
        // A rotation the user makes with the device is not a request we own;
        // forgetting this left the button offering 全屏 on a screen that was
        // already as large as it gets.
        if (!landscape) landscapeRequested = false;
        // The stage's shape just changed; the surface has to follow it or the
        // picture keeps the old stretch until something else happens to
        // trigger a refit.
        applyStageAspect();
    }

    private int stageHeightPortrait() {
        int w = getResources().getDisplayMetrics().widthPixels;
        int screenH = getResources().getDisplayMetrics().heightPixels;
        if (fillScreen) return Math.round(screenH * 0.68f);
        if (metaWidth > 0 && metaHeight > 0) {
            int h = Math.round(w * (metaHeight / (float) metaWidth));
            // Clamp: a 9:16 portrait recording would otherwise eat the whole
            // screen and leave the facts column a sliver below the fold.
            int max = Math.round(screenH * 0.62f);
            int min = Math.round(w * 9f / 21f);
            return Math.max(min, Math.min(h, max));
        }
        return Math.round(w * 9f / 16f);
    }

    /**
     * Fit the surface — and the poster riding on it — to the video's real
     * shape inside the stage, centred.
     *
     * <p>A SurfaceView hands its raw rectangle to the decoder, and the
     * decoder fills the rectangle. The stage in landscape is the whole
     * window, and a 20:9 phone window given a 16:9 file stretches the
     * picture: the 「全屏又是被拉伸的」 bug. {@code setVideoScalingMode}
     * cannot be trusted on a plain SurfaceView (its contract is for
     * SurfaceTexture-driven rendering), so the shell does the fitting
     * itself: the surface gets exactly the rectangle the picture needs and
     * the letterbox bars are the stage's own black.
     *
     * <p>填充 keeps the full-bleed stretch the user asked for (the
     * WITH_CROPPING contract), and an unknown aspect falls back to the old
     * full-stage behaviour until the retriever answers.
     */
    private void applyStageAspect() {
        if (videoFrame == null || surface == null) return;
        int frameW = videoFrame.getWidth();
        int frameH = videoFrame.getHeight();
        if (frameW <= 0 || frameH <= 0) return;
        int w;
        int h;
        if (fillScreen || metaWidth <= 0 || metaHeight <= 0) {
            w = FrameLayout.LayoutParams.MATCH_PARENT;
            h = FrameLayout.LayoutParams.MATCH_PARENT;
        } else {
            float videoAspect = metaWidth / (float) metaHeight;
            float stageAspect = frameW / (float) frameH;
            if (videoAspect > stageAspect) {
                w = frameW;
                h = Math.max(1, Math.round(w / videoAspect));
            } else {
                h = frameH;
                w = Math.max(1, Math.round(h * videoAspect));
            }
        }
        surface.setLayoutParams(new FrameLayout.LayoutParams(w, h, Gravity.CENTER));
        if (cover != null) {
            cover.setLayoutParams(new FrameLayout.LayoutParams(w, h, Gravity.CENTER));
        }
    }

    private int scalingMode() {
        return fillScreen ? MediaPlayer.VIDEO_SCALING_MODE_SCALE_TO_FIT_WITH_CROPPING
                          : MediaPlayer.VIDEO_SCALING_MODE_SCALE_TO_FIT;
    }

    private void setFillScreen(boolean fill) {
        fillScreen = fill;
        if (fillButton != null) {
            setIcon(fillButton, fill ? PlayerGlyph.FIT : PlayerGlyph.FILL,
                    fill ? "恢复原始比例" : "填充屏幕（裁切两侧）");
        }
        if (player != null) {
            try {
                player.setVideoScalingMode(scalingMode());
            } catch (Exception e) {
                Log.w(TAG, "scaling mode change failed", e);
            }
        }
        applyOrientationLayout();
        renderMeta();
    }

    /**
     * Real full screen: rotate to landscape and go immersive. A 16:9 picture
     * on a 9:16 phone is 607px tall no matter how the layout is tuned, so
     * "bigger" is a rotation, not a padding change.
     */
    private void setLandscapeFullscreen(boolean on) {
        landscapeRequested = on;
        setRequestedOrientation(on ? ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                                   : ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED);
    }

    private int statusBarInset() {
        int id = getResources().getIdentifier("status_bar_height", "dimen", "android");
        return id > 0 ? getResources().getDimensionPixelSize(id) : dp(24);
    }

    private void enterImmersive() {
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
    }

    private void exitImmersive() {
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_VISIBLE);
    }

    private android.graphics.drawable.Drawable pageBackground() {
        // Surface above background, both from the one palette: a slight lift at
        // the top so the page reads as lit rather than flat.
        return new android.graphics.drawable.GradientDrawable(
                android.graphics.drawable.GradientDrawable.Orientation.TOP_BOTTOM,
                new int[]{Theme.SURFACE, Theme.BG});
    }

    // --------------------------------------------------------------- metadata

    /**
     * Read the poster and the facts from the file on a worker thread.
     *
     * <p>The stage is sized from the real aspect ratio once it is known, which
     * is why {@link #stageHeightPortrait()} can consult {@code metaWidth} — a
     * stage that is 16:9 because the code assumed so is the adaptation bug this
     * page is being rebuilt to fix.
     */
    private void loadMetadata() {
        final Uri uri = mediaUri;
        if (uri == null) return;
        final boolean remote = !remoteUrl.isEmpty();
        new Thread(new Runnable() {
            @Override
            public void run() {
                MediaMetadataRetriever r = null;
                Bitmap frame = null;
                int w = 0, h = 0, dur = 0, bitrate = 0;
                try {
                    r = new MediaMetadataRetriever();
                    r.setDataSource(PlayerActivity.this, uri);
                    w = parseIntMeta(r.extractMetadata(
                            MediaMetadataRetriever.METADATA_KEY_VIDEO_WIDTH));
                    h = parseIntMeta(r.extractMetadata(
                            MediaMetadataRetriever.METADATA_KEY_VIDEO_HEIGHT));
                    dur = parseIntMeta(r.extractMetadata(
                            MediaMetadataRetriever.METADATA_KEY_DURATION));
                    bitrate = parseIntMeta(r.extractMetadata(
                            MediaMetadataRetriever.METADATA_KEY_BITRATE));
                    // The facts are already in hand, so publish them before the
                    // frame hunt: picking a frame means deep sync seeks into a
                    // 949 MB concatenated TS, and the table must never sit
                    // half-empty waiting on a picture.
                    publishFacts(w, h, dur, bitrate);
                    // A stream's picture is the network cover, not a frame:
                    // deep sync-seeks over the proxy would stall the head of
                    // the stream for a poster the player is about to replace.
                    frame = remote ? null : pickFrame(r, dur);
                } catch (Exception e) {
                    Log.w(TAG, "metadata sniff failed", e);
                } finally {
                    if (r != null) {
                        try {
                            r.release();
                        } catch (Exception ignored) {
                            // release() is best-effort; nothing to recover.
                        }
                    }
                }
                if (!remote) publishFrame(frame);
            }
        }, "asmrlib-meta").start();
    }

    private static int parseIntMeta(String value) {
        if (value == null) return 0;
        try {
            return Integer.parseInt(value.trim());
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    /** Fill the detail column: badge, headline, and the label/value table. */
    private void renderMeta() {
        if (metaSubtitle == null) return;
        if (!remoteUrl.isEmpty()) {
            // A stream has no file to describe: no size, no container, no
            // storage row, no download-state badge. What it has is a kind.
            metaSubtitle.setText("hls".equals(remoteKind) ? "在线播放 · HLS 流"
                    : "在线播放 · 直链媒体");
            if (metaBadge != null) metaBadge.setVisibility(View.GONE);
            if (metaRows != null) {
                metaRows.removeAllViews();
                if (metaDurationMs > 0) {
                    metaRows.addView(metaRow("时长", formatTime(metaDurationMs), true));
                }
                if (metaWidth > 0 && metaHeight > 0) {
                    metaRows.addView(metaRow("分辨率", metaWidth + "×" + metaHeight
                            + " (" + aspectLabel(metaWidth, metaHeight) + ")", true));
                }
                if (metaBitrate > 0) {
                    metaRows.addView(metaRow("码率", (metaBitrate / 1000) + " kbps", true));
                }
            }
            return;
        }
        String ext = containerLabel(mediaTitle);
        StringBuilder sub = new StringBuilder();
        if (mediaBytes > 0) sub.append(formatBytes(mediaBytes));
        if (!ext.isEmpty()) {
            if (sub.length() > 0) sub.append(" · ");
            sub.append(ext);
        }
        // No "本地离线" tail any more: the shell has no PC library to be offline
        // from, so every file here is local by definition — and the extra words
        // were what pushed the line into an ellipsis.
        metaSubtitle.setText(sub.length() > 0 ? sub.toString() : "本地文件");

        if (metaBadge != null) {
            boolean done = STATE_SUCCESS.equals(mediaState) || mediaState.isEmpty();
            metaBadge.setText(done ? "已完成" : stateLabel(mediaState));
            metaBadge.setBackground(badgePlate(done));
        }

        if (metaRows == null) return;
        metaRows.removeAllViews();
        if (metaDurationMs > 0) metaRows.addView(metaRow("时长", formatTime(metaDurationMs), true));
        if (metaWidth > 0 && metaHeight > 0) {
            // Aspect rides in the same row: six rows pushed 重命名/删除 below
            // the fold on a 1920px screen, and a reader who knows 1920×1080
            // does not need a separate line to be told it is 16:9.
            metaRows.addView(metaRow("分辨率", metaWidth + "×" + metaHeight
                    + " (" + aspectLabel(metaWidth, metaHeight) + ")", true));
        }
        if (mediaBytes > 0) metaRows.addView(metaRow("文件大小", formatBytes(mediaBytes), true));
        if (!ext.isEmpty()) metaRows.addView(metaRow("容器格式", ext, true));
        if (metaBitrate > 0) {
            metaRows.addView(metaRow("码率", (metaBitrate / 1000) + " kbps", true));
        }
        if (mediaSavedAt > 0) {
            metaRows.addView(metaRow("保存时间", formatStamp(mediaSavedAt), true));
        }
        metaRows.addView(metaRow("存储位置", MainActivity.OFFLINE_DIR + "/", false));
    }

    private static String aspectLabel(int w, int h) {
        int g = gcd(w, h);
        int a = w / g, b = h / g;
        if (a > 32 || b > 32) return ((int) Math.round(w / (float) h * 100f) / 100f) + " : 1";
        return a + ":" + b;
    }

    private static int gcd(int a, int b) {
        while (b != 0) {
            int t = a % b;
            a = b;
            b = t;
        }
        return a == 0 ? 1 : a;
    }

    private static String stateLabel(String state) {
        if (STATE_RUNNING.equals(state)) return "下载中";
        if (STATE_FAILED.equals(state)) return "下载失败";
        if (STATE_LOST.equals(state)) return "文件丢失";
        return "本地文件";
    }

    /**
     * One row of the facts table: muted label left, real value right, hairline
     * under it. A table reads as information; the pills it replaced read as
     * tags, and a third pill squeezed the row until its own label wrapped —
     * measured at 186px tall against 96px for its neighbours.
     */
    private View metaRow(String label, String value, boolean divider) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, dp(9), 0, dp(9));

        TextView l = new TextView(this);
        l.setText(label);
        l.setTextColor(TEXT_MUTED);
        l.setTextSize(12.5f);
        row.addView(l, new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));

        TextView v = new TextView(this);
        v.setText(value);
        v.setTextColor(TEXT_PRIMARY);
        v.setTextSize(13.5f);
        v.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        v.setGravity(Gravity.END);
        row.addView(v, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        if (!divider) return row;
        LinearLayout wrap = new LinearLayout(this);
        wrap.setOrientation(LinearLayout.VERTICAL);
        wrap.addView(row);
        View line = new View(this);
        line.setBackgroundColor(Color.argb(22, 255, 255, 255));
        wrap.addView(line, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, Math.max(1, dp(1))));
        return wrap;
    }

    private android.graphics.drawable.GradientDrawable badgePlate(boolean good) {
        android.graphics.drawable.GradientDrawable bg =
                new android.graphics.drawable.GradientDrawable();
        bg.setCornerRadius(dp(999));
        bg.setColor(good ? Color.argb(46, 74, 222, 128) : Color.argb(40, 250, 204, 21));
        bg.setStroke(Math.max(1, dp(1)),
                good ? Color.argb(90, 74, 222, 128) : Color.argb(90, 250, 204, 21));
        return bg;
    }

    private static String containerLabel(String name) {
        String n = name == null ? "" : name.toLowerCase(Locale.US);
        if (n.endsWith(".ts")) return "MPEG-TS";
        if (n.endsWith(".mp4")) return "MP4";
        if (n.endsWith(".webm")) return "WebM";
        if (n.endsWith(".m4a")) return "M4A";
        if (n.endsWith(".mp3")) return "MP3";
        if (n.endsWith(".opus")) return "Opus";
        if (n.endsWith(".flac")) return "FLAC";
        if (n.endsWith(".wav")) return "WAV";
        return "";
    }

    /**
     * The facts column: what the file is, and the two record actions that do
     * not belong on a transport bar.
     *
     * <p>重命名 and 删除 are handed back to the download centre rather than
     * re-implemented here: both touch the same persisted list and both delete
     * bytes, and a second implementation of either is a second chance to get
     * it wrong. The player says what it wants and gets out of the way.
     */
    private View buildInfoPanel() {
        LinearLayout page = new LinearLayout(this);
        page.setOrientation(LinearLayout.VERTICAL);
        page.setPadding(dp(16), dp(16), dp(16), dp(28));

        // One card, not a bare column: the facts belong to the file, and a
        // bordered surface is what tells the eye they are one object.
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setBackground(detailCardPlate());
        card.setPadding(dp(16), dp(16), dp(16), dp(16));

        LinearLayout hero = new LinearLayout(this);
        hero.setOrientation(LinearLayout.HORIZONTAL);
        hero.setGravity(Gravity.CENTER_VERTICAL);

        thumb = new ImageView(this);
        thumb.setScaleType(ImageView.ScaleType.CENTER_CROP);
        thumb.setBackground(accentPlate(dp(10)));
        hero.addView(thumb, new LinearLayout.LayoutParams(dp(104), dp(60)));

        LinearLayout heading = new LinearLayout(this);
        heading.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams headLp = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        headLp.leftMargin = dp(14);
        hero.addView(heading, headLp);

        metaTitle = new TextView(this);
        metaTitle.setText(mediaTitle.isEmpty() ? "已保存的视频" : mediaTitle);
        metaTitle.setTextColor(TEXT_PRIMARY);
        metaTitle.setTextSize(15.5f);
        metaTitle.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        metaTitle.setLineSpacing(dp(2), 1f);
        metaTitle.setMaxLines(2);
        metaTitle.setEllipsize(android.text.TextUtils.TruncateAt.END);
        heading.addView(metaTitle);

        // Size, container and status share one line. They used to be two stacked
        // full-width blocks, which is what made the top of the card read as
        // three unrelated strips rather than one summary of one file.
        metaSubtitle = new TextView(this);
        metaSubtitle.setTextColor(TEXT_MUTED);
        metaSubtitle.setTextSize(12f);
        metaSubtitle.setSingleLine(true);
        metaSubtitle.setEllipsize(android.text.TextUtils.TruncateAt.END);

        metaBadge = new TextView(this);
        metaBadge.setText("已完成");
        metaBadge.setTextColor(Color.parseColor("#bbf7d0"));
        metaBadge.setTextSize(10.5f);
        metaBadge.setPadding(dp(9), dp(3), dp(9), dp(3));

        LinearLayout statusRow = new LinearLayout(this);
        statusRow.setOrientation(LinearLayout.HORIZONTAL);
        statusRow.setGravity(Gravity.CENTER_VERTICAL);
        statusRow.addView(metaSubtitle, new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        LinearLayout.LayoutParams badgeLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        badgeLp.leftMargin = dp(10);
        statusRow.addView(metaBadge, badgeLp);
        LinearLayout.LayoutParams statusLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        statusLp.topMargin = dp(8);
        heading.addView(statusRow, statusLp);

        card.addView(hero);

        // Actions sit directly under the headline, above the table: the table
        // is seven rows and grew past the fold, which put 重命名 / 删除 at the
        // bottom of a scroll. The verbs belong where the eye lands first.
        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        LinearLayout.LayoutParams actLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(44));
        actLp.topMargin = dp(16);
        card.addView(actions, actLp);
        buildInfoActions(actions);

        View divider = new View(this);
        divider.setBackgroundColor(Color.argb(26, 255, 255, 255));
        LinearLayout.LayoutParams divLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, Math.max(1, dp(1)));
        divLp.topMargin = dp(14);
        card.addView(divider, divLp);

        metaRows = new LinearLayout(this);
        metaRows.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams rowsLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        rowsLp.topMargin = dp(4);
        card.addView(metaRows, rowsLp);

        page.addView(card);

        // Actions as a full-width row of equal pills: three text buttons of
        // different lengths on one line looked accidental, and the longest
        // one decided where the row ended.

        page.addView(playbackHint(), new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        return page;
    }

    /**
     * 重命名 / 删除 / 其他应用打开 — three icon plates, one size, one gap.
     *
     * <p>These were icon+label Buttons in a 44dp row, and a Button drags the
     * platform's own text size and padding in with it: 重命名 wrapped onto two
     * lines and 其他应用打开 lost its last line outright. Measured on device the
     * three cells came out 103 / 132 / 95 px tall sitting on one baseline —
     * the ragged row this replaces. Icons carry no text metrics, so equal cells
     * are equal by construction; the words move to contentDescription, which is
     * where they were only ever useful anyway.
     */
    private void buildInfoActions(LinearLayout actions) {
        if (!remoteUrl.isEmpty()) {
            // A stream has no download record: nothing to rename, delete, or
            // hand to another app — the actions would all be toasts.
            actions.setVisibility(View.GONE);
            return;
        }
        addAction(actions, PlayerGlyph.RENAME, "重命名", "#c8d2e8", false,
                new View.OnClickListener() {
                    @Override public void onClick(View v) { handBack(ACTION_RENAME); }
                });
        addAction(actions, PlayerGlyph.DELETE, "删除文件", "#ff9aa6", false,
                new View.OnClickListener() {
                    @Override public void onClick(View v) { handBack(ACTION_DELETE); }
                });
        addAction(actions, PlayerGlyph.OPEN, "其他应用打开", "#9fb0cf", true,
                new View.OnClickListener() {
                    @Override public void onClick(View v) { openExternally(); }
                });
    }

    /**
     * One cell of the action row. Every geometry decision lives here so the
     * three cells cannot drift apart one margin at a time.
     */
    private void addAction(LinearLayout row, int glyph, String description,
                           String tint, boolean last, View.OnClickListener click) {
        ImageButton b = new ImageButton(this);
        b.setContentDescription(description);
        b.setScaleType(ImageView.ScaleType.CENTER);
        b.setImageDrawable(new PlayerGlyph(glyph, dp(20), "", Color.parseColor(tint)));
        b.setPadding(dp(6), dp(6), dp(6), dp(6));
        b.setStateListAnimator(null);
        b.setBackground(PressFeedback.on(iconPlate()));
        b.setOnClickListener(click);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, dp(44), 1f);
        if (!last) lp.rightMargin = dp(8);
        row.addView(b, lp);
    }

    private android.graphics.drawable.GradientDrawable detailCardPlate() {
        android.graphics.drawable.GradientDrawable bg =
                new android.graphics.drawable.GradientDrawable();
        bg.setCornerRadius(dp(18));
        bg.setColor(Theme.darkSurface(190));
        bg.setStroke(Math.max(1, dp(1)), Theme.darkAccent(30));
        return bg;
    }

    private TextView playbackHint() {
        TextView t = new TextView(this);
        t.setText("横屏自动全屏观看 · 轻点画面显示/隐藏控制条");
        t.setTextColor(Theme.TEXT_3);
        t.setTextSize(11.5f);
        t.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(14);
        t.setLayoutParams(lp);
        return t;
    }

    /**
     * Hand playback to {@link PlaybackService} and close the picture.
     *
     * <p>The surface has to be released first: two MediaPlayers on the same
     * file would fight, and the point of the service is that it holds the
     * audio with no surface at all. The position travels with it, so the
     * user's place is not lost when the screen goes dark.
     */
    private void startSleepPlayback() {
        if (mediaUri == null) {
            Toast.makeText(this, "没有可播放的文件", Toast.LENGTH_SHORT).show();
            return;
        }
        int position = pendingSeekMs;
        if (player != null && prepared) {
            try {
                position = player.getCurrentPosition();
            } catch (Exception e) {
                Log.w(TAG, "could not read position before handover", e);
            }
        }
        // Written here rather than left to onPause: by the time onPause runs,
        // releasePlayer() has already cleared `prepared` and the place would be
        // gone. The service owns playback from here, but if the app is killed
        // while the screen is off this record is what brings the user back.
        saveResume();
        releasePlayer();
        // The queue rides along (Eara's continuous playback): without it the
        // service stops after one track and 息屏连播 quietly ends. The resume
        // key travels too, so the service keeps writing this track's place
        // into the same record the page would have.
        PlaybackService.start(this, mediaUri.toString(), mediaTitle, position,
                resumeKey,
                remoteQueue == null ? "" : remoteQueue.toString(),
                remoteQueueIndex);
        Toast.makeText(this, "已切换到息屏播放，锁屏也能继续听", Toast.LENGTH_SHORT).show();
        finish();
    }

    /**
     * Shown when this page is opened while the service already owns playback.
     * Without it, opening the player would start a second MediaPlayer on the
     * same file and the user would hear both.
     */
    private View buildBackgroundBar() {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setGravity(Gravity.CENTER_HORIZONTAL);
        card.setPadding(dp(22), dp(20), dp(22), dp(20));
        android.graphics.drawable.GradientDrawable bg =
                new android.graphics.drawable.GradientDrawable();
        bg.setCornerRadius(dp(18));
        bg.setColor(Theme.darkSurface(236));
        bg.setStroke(Math.max(1, dp(1)), Theme.darkAccent(120));
        card.setBackground(bg);

        TextView headline = new TextView(this);
        headline.setText("正在息屏播放");
        headline.setTextColor(TEXT_PRIMARY);
        headline.setTextSize(15f);
        headline.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        card.addView(headline);

        final TextView clock = new TextView(this);
        clock.setTextColor(TEXT_MUTED);
        clock.setTextSize(12.5f);
        clock.setPadding(0, dp(6), 0, 0);
        clock.setText("音频在后台继续 · " + formatTime(PlaybackService.positionMs()));
        card.addView(clock);

        // Ticking label: a frozen "0:00" would read as "playback is stuck".
        ui.post(new Runnable() {
            @Override
            public void run() {
                if (isFinishing()) return;
                clock.setText("音频在后台继续 · " + formatTime(PlaybackService.positionMs()));
                ui.postDelayed(this, 1000L);
            }
        });

        Button back = new Button(this);
        back.setText("回到画面播放");
        styleActionPill(back, Color.WHITE);
        back.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                pendingSeekMs = PlaybackService.positionMs();
                PlaybackService.stop(PlayerActivity.this);
                if (backgroundBar != null) backgroundBar.setVisibility(View.GONE);
                open();
            }
        });
        LinearLayout.LayoutParams btnLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, dp(42));
        btnLp.topMargin = dp(14);
        card.addView(back, btnLp);
        return card;
    }

    private void showBackgroundBar() {
        if (backgroundBar == null) {
            backgroundBar = buildBackgroundBar();
            android.view.ViewGroup host = overlayHost != null ? overlayHost : videoFrame;
            FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                    FrameLayout.LayoutParams.WRAP_CONTENT,
                    FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.CENTER);
            lp.setMargins(dp(24), 0, dp(24), 0);
            host.addView(backgroundBar, lp);
            backgroundBar.bringToFront();
        }
        backgroundBar.setVisibility(View.VISIBLE);
        if (cover != null) cover.setVisibility(View.GONE);
        loading.setBusy(false);
        setControlsVisible(true);
    }

    private void handBack(String action) {
        if (taskId.isEmpty()) {
            Toast.makeText(this, "这条记录没有可操作的下载任务", Toast.LENGTH_SHORT).show();
            return;
        }
        Intent data = new Intent();
        data.putExtra(RESULT_ACTION, action);
        data.putExtra(RESULT_TASK_ID, taskId);
        setResult(RESULT_OK, data);
        finish();
    }

    /**
     * An icon-only control on a translucent pill.
     *
     * <p>The label lives in {@code contentDescription} so TalkBack still says
     * 全屏 / 后退 10 秒; the pixels say it with a glyph, because four words in
     * a row is a settings list and not a transport bar.
     */
    /**
     * One cell of the transport row: fixed size, and the same gap on both
     * sides of every control.
     *
     * <p>The row used to give the play button 18dp margins and its neighbours
     * none, so 前进 10 秒 / 息屏播放 / 全屏 sat flush against each other
     * (measured on device: 672|672 and 846|846, zero gap) while the play button
     * floated in 54px holes. Equal margins on every cell make the gaps
     * uniform by construction rather than by remembering to add them.
     */
    private LinearLayout.LayoutParams transportLp(int widthDp, int heightDp) {
        LinearLayout.LayoutParams lp =
                new LinearLayout.LayoutParams(dp(widthDp), dp(heightDp));
        lp.setMargins(dp(4), 0, dp(4), 0);
        return lp;
    }

    /**
     * The seek bubble: a centred pill reading 「快进 2:31」 / 「快退 0:47」 that
     * rides above the drag. Only ever shown mid-gesture — the value is what
     * the release will commit, so it cannot drift from what happens.
     */
    private void showScrubBubble(int seconds) {
        if (videoFrame == null) return;
        if (scrubBubble == null) {
            scrubBubble = new TextView(this);
            scrubBubble.setTextColor(Color.WHITE);
            scrubBubble.setTextSize(15f);
            scrubBubble.setGravity(Gravity.CENTER);
            android.graphics.drawable.GradientDrawable pill =
                    new android.graphics.drawable.GradientDrawable();
            pill.setCornerRadius(dp(22));
            pill.setColor(Theme.darkBg(200));
            pill.setStroke(dp(1), 0x30FFFFFF);
            scrubBubble.setBackground(pill);
            scrubBubble.setPadding(dp(18), dp(10), dp(18), dp(10));
            videoFrame.addView(scrubBubble, new FrameLayout.LayoutParams(
                    FrameLayout.LayoutParams.WRAP_CONTENT,
                    FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.CENTER));
        }
        int target = Math.max(0, Math.min(durationMs / 1000,
                safePosition() / 1000 + seconds));
        scrubBubble.setText((seconds >= 0 ? "快进 " : "快退 ")
                + formatTime(Math.abs(seconds)) + " · " + formatTime(target));
        scrubBubble.setVisibility(View.VISIBLE);
        scrubBubble.bringToFront();
    }

    private void hideScrubBubble() {
        if (scrubBubble != null) scrubBubble.setVisibility(View.GONE);
    }

    /** Commit the dragged delta: clamped, announced by the rail sync that
     *  follows. A drag that never happened (a tap) commits nothing. */
    private void commitScrub(int seconds) {
        if (player == null || !prepared || seconds == 0) return;
        try {
            int target = Math.max(0, Math.min(durationMs, safePosition() + seconds * 1000));
            player.seekTo(target);
            pendingSeekMs = target;
        } catch (Exception e) {
            Log.w(TAG, "scrub seek failed", e);
        }
    }

    private ImageButton iconButton(int glyph, int sizeDp, String description) {
        ImageButton b = new ImageButton(this);
        b.setContentDescription(description);
        b.setScaleType(ImageView.ScaleType.CENTER);
        b.setImageDrawable(new PlayerGlyph(glyph, dp(sizeDp), ""));
        b.setPadding(dp(6), dp(6), dp(6), dp(6));
        b.setStateListAnimator(null);
        b.setBackground(PressFeedback.on(iconPlate()));
        return b;
    }

    /**
     * Eara's video-overlay chip: a 40dp black@0.35 rounded-10dp plate with a
     * 20dp glyph, pinned over the video itself. The reference player hangs
     * its fullscreen entry on the video this way instead of in a toolbar.
     */
    private ImageButton chipButton(int glyph, String description) {
        ImageButton b = new ImageButton(this);
        b.setContentDescription(description);
        b.setScaleType(ImageView.ScaleType.CENTER);
        b.setImageDrawable(new PlayerGlyph(glyph, dp(20), ""));
        b.setStateListAnimator(null);
        android.graphics.drawable.GradientDrawable chip =
                new android.graphics.drawable.GradientDrawable();
        chip.setColor(Color.argb(89, 0, 0, 0));
        chip.setCornerRadius(dp(10));
        b.setBackground(PressFeedback.on(chip));
        return b;
    }

    private android.graphics.drawable.GradientDrawable iconPlate() {
        android.graphics.drawable.GradientDrawable bg =
                new android.graphics.drawable.GradientDrawable();
        bg.setColor(Color.argb(30, 255, 255, 255));
        bg.setStroke(Math.max(1, dp(1)), Color.argb(46, 255, 255, 255));
        bg.setCornerRadius(dp(21));
        return bg;
    }

    // ------------------------------------------------------------ speed

    /** The 倍速 pill: text, not a glyph — the speed is half the information,
     *  and "1.5x" reads faster than any icon. Tap walks the ladder. */
    private TextView buildSpeedPill(String description) {
        final TextView pill = new TextView(this);
        pill.setTextColor(TEXT_MUTED);
        pill.setTextSize(12.5f);
        pill.setGravity(Gravity.CENTER);
        pill.setContentDescription(description);
        pill.setPadding(dp(12), 0, dp(12), 0);
        android.graphics.drawable.GradientDrawable bg =
                new android.graphics.drawable.GradientDrawable();
        bg.setColor(Color.argb(30, 255, 255, 255));
        bg.setStroke(Math.max(1, dp(1)), Color.argb(46, 255, 255, 255));
        bg.setCornerRadius(dp(18));
        pill.setBackground(PressFeedback.on(bg));
        pill.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { cycleSpeed(); }
        });
        pill.setText(speedLabel(playbackSpeed));
        return pill;
    }

    /** 倍速 as the video slot's third overlay chip, alongside 填充/全屏 —
     *  text because a glyph cannot say which speed you are at. */
    private TextView buildSpeedChip() {
        TextView chip = buildSpeedPill("播放速度");
        // Overlay chips are black glass over the picture, not the utility
        // row's grey plate.
        android.graphics.drawable.GradientDrawable bg =
                new android.graphics.drawable.GradientDrawable();
        bg.setColor(Color.argb(89, 0, 0, 0));
        bg.setCornerRadius(dp(10));
        chip.setBackground(PressFeedback.on(bg));
        chip.setTextColor(Color.WHITE);
        return chip;
    }

    private static String speedLabel(float speed) {
        // 1.0 shows as 倍速, not 1.0x: the resting state has nothing to say.
        if (Math.abs(speed - 1.0f) < 0.01f) return "倍速";
        return speed + "x";
    }

    private void cycleSpeed() {
        int next = 0;
        for (int i = 0; i < SPEED_STEPS.length; i++) {
            if (Math.abs(SPEED_STEPS[i] - playbackSpeed) < 0.01f) {
                next = (i + 1) % SPEED_STEPS.length;
                break;
            }
        }
        setPlaybackSpeed(SPEED_STEPS[next]);
    }

    /** Apply the ladder step. setPlaybackParams is API 23+, so an old device
     *  keeps silent 1.0 rather than a crash or a pointless toast. */
    private void setPlaybackSpeed(float speed) {
        playbackSpeed = speed;
        if (player != null && prepared
                && android.os.Build.VERSION.SDK_INT >= 23) {
            try {
                android.media.PlaybackParams params = player.getPlaybackParams();
                params.setSpeed(speed);
                player.setPlaybackParams(params);
            } catch (Exception e) {
                Log.w(TAG, "setPlaybackParams failed", e);
            }
        }
        if (speedChip != null) speedChip.setText(speedLabel(speed));
        if (audioSpeedPill != null) audioSpeedPill.setText(speedLabel(speed));
    }

    private TextView audioSpeedPill;

    // ------------------------------------------------------------ queue panel

    /**
     * The queue sheet (Eara's QueueSheet): a dim overlay that dismisses on
     * tap, and a right-anchored panel of one row per track — the checked
     * current track, tap-to-jump rows for the rest. Rows share the jump
     * with 上一轨/下一轨 through {@link #switchQueue}.
     */
    private View buildQueuePanel() {
        FrameLayout dim = new FrameLayout(this);
        dim.setBackgroundColor(Color.argb(140, 0, 0, 0));
        dim.setVisibility(View.GONE);
        dim.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { toggleQueuePanel(); }
        });

        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        android.graphics.drawable.GradientDrawable bg =
                new android.graphics.drawable.GradientDrawable();
        bg.setCornerRadius(dp(18));
        bg.setColor(Theme.darkBg(246));
        bg.setStroke(Math.max(1, dp(1)), Color.argb(46, 255, 255, 255));
        panel.setBackground(bg);
        panel.setPadding(dp(8), dp(10), dp(8), dp(10));
        TextView title = new TextView(this);
        title.setText("播放队列");
        title.setTextColor(TEXT_MUTED);
        title.setTextSize(11.5f);
        title.setPadding(dp(14), dp(2), dp(14), dp(8));
        panel.addView(title);

        ScrollView scroll = new ScrollView(this);
        LinearLayout rows = new LinearLayout(this);
        rows.setOrientation(LinearLayout.VERTICAL);
        scroll.addView(rows, new ScrollView.LayoutParams(
                ScrollView.LayoutParams.MATCH_PARENT,
                ScrollView.LayoutParams.WRAP_CONTENT));
        panel.addView(scroll, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                dp(320)));

        if (remoteQueue != null) {
            for (int i = 0; i < remoteQueue.length(); i++) {
                rows.addView(queueRow(i));
            }
        }

        FrameLayout.LayoutParams panelLp = new FrameLayout.LayoutParams(
                Math.min(getResources().getDisplayMetrics().widthPixels - dp(48),
                        dp(360)), FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL);
        panelLp.setMargins(0, 0, 0, dp(96));
        dim.addView(panel, panelLp);
        return dim;
    }

    private View queueRow(final int index) {
        org.json.JSONObject entry = remoteQueue.optJSONObject(index);
        String label = entry == null ? "" : entry.optString("l", "");
        final boolean current = index == remoteQueueIndex;

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(dp(14), dp(11), dp(14), dp(11));
        android.graphics.drawable.GradientDrawable bg =
                new android.graphics.drawable.GradientDrawable();
        bg.setCornerRadius(dp(12));
        bg.setColor(current ? Color.argb(38, 124, 156, 255) : Color.TRANSPARENT);
        row.setBackground(PressFeedback.on(bg));
        row.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                toggleQueuePanel();
                switchQueue(index);
            }
        });

        TextView num = new TextView(this);
        num.setText(String.valueOf(index + 1));
        num.setTextColor(current ? ACCENT_A : Theme.TEXT_3);
        num.setTextSize(12f);
        num.setTypeface(android.graphics.Typeface.MONOSPACE);
        num.setMinWidth(dp(26));
        row.addView(num);

        TextView name = new TextView(this);
        name.setText(label.isEmpty() ? "第 " + (index + 1) + " 轨" : label);
        name.setTextColor(current ? TEXT_PRIMARY : TEXT_MUTED);
        name.setTextSize(14f);
        name.setSingleLine(true);
        name.setEllipsize(android.text.TextUtils.TruncateAt.END);
        LinearLayout.LayoutParams nameLp = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        nameLp.setMargins(dp(10), 0, dp(10), 0);
        row.addView(name, nameLp);

        return row;
    }

    private void toggleQueuePanel() {
        FrameLayout host = overlayHost != null ? overlayHost
                : videoFrame != null ? videoFrame
                : chromeRoot instanceof FrameLayout ? (FrameLayout) chromeRoot
                : null;
        if (host == null) return;
        if (queuePanel == null) {
            queuePanel = buildQueuePanel();
            host.addView(queuePanel, new FrameLayout.LayoutParams(
                    FrameLayout.LayoutParams.MATCH_PARENT,
                    FrameLayout.LayoutParams.MATCH_PARENT));
        }
        boolean show = queuePanel.getVisibility() != View.VISIBLE;
        queuePanel.setVisibility(show ? View.VISIBLE : View.GONE);
        if (show) queuePanel.bringToFront();
    }

    private void setIcon(ImageButton b, int glyph, String description) {
        b.setContentDescription(description);
        b.setImageDrawable(new PlayerGlyph(glyph, dp(20), ""));
    }

    private void styleActionPill(Button b, int textColor) {
        b.setAllCaps(false);
        b.setTextSize(13.5f);
        b.setTextColor(textColor);
        b.setStateListAnimator(null);
        b.setMinWidth(0);
        b.setMinimumWidth(0);
        b.setMinimumHeight(0);
        b.setPadding(dp(16), 0, dp(16), 0);
        android.graphics.drawable.GradientDrawable bg =
                new android.graphics.drawable.GradientDrawable();
        bg.setColor(Color.argb(22, 255, 255, 255));
        bg.setStroke(Math.max(1, dp(1)), Color.argb(38, 255, 255, 255));
        bg.setCornerRadius(dp(21));
        b.setBackground(PressFeedback.on(bg));
    }

    /**
     * The transport layer: a title bar that never auto-hides, and a rail plus
     * buttons that do. Tapping anywhere toggles the bottom group, which is the
     * gesture every video app on the platform already trained the user for.
     */
    private View buildControls() {
        controls = new FrameLayout(this);
        ((FrameLayout) controls).setBackgroundColor(Color.argb(0, 0, 0, 0));
        // Tap toggles the bars; a horizontal drag scrubs — the same 滑动快进
        // gesture every serious player ships. One detector owns both, so a
        // drag can never fire as a tap on release and a tap can never drift
        // into a seek.
        final GestureDetector gestures = new GestureDetector(this,
                new GestureDetector.SimpleOnGestureListener() {
            @Override public boolean onDown(MotionEvent e) {
                scrubStartX = e.getX();
                scrubSeconds = 0;
                scrubbing = false;
                return true;
            }

            @Override public boolean onScroll(MotionEvent e1, MotionEvent e2,
                    float distanceX, float distanceY) {
                if (Math.abs(e2.getX() - e1.getX()) < Math.abs(e2.getY() - e1.getY())) {
                    return false; // vertical drags belong to the scroll views
                }
                if (player == null || !prepared || durationMs <= 0) return true;
                scrubbing = true;
                // Full stage width ≈ ±180s: a thumb sweep is one minute,
                // not one second — the ratio every player converges on.
                float swept = e2.getX() - scrubStartX;
                int stageW = Math.max(1, videoFrame.getWidth());
                scrubSeconds = Math.round(swept / stageW * 360f);
                showScrubBubble(scrubSeconds);
                return true;
            }

            @Override public boolean onSingleTapConfirmed(MotionEvent e) {
                setControlsVisible(controls.getAlpha() < 1f);
                return true;
            }
        });
        controls.setOnTouchListener(new View.OnTouchListener() {
            @Override public boolean onTouch(View v, MotionEvent event) {
                boolean handled = gestures.onTouchEvent(event);
                if (event.getAction() == MotionEvent.ACTION_UP
                        || event.getAction() == MotionEvent.ACTION_CANCEL) {
                    if (scrubbing) {
                        scrubbing = false;
                        commitScrub(scrubSeconds);
                        hideScrubBubble();
                    }
                }
                return handled;
            }
        });

        // Eara's header: collapse on the left, title centered, transparent —
        // no button slab floating over the picture. 填充/全屏 moved to overlay
        // chips pinned to the video slot itself (Eara's fullscreen chip).
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(12), dp(8), dp(12), dp(8));
        ImageButton back = iconButton(PlayerGlyph.BACK, 24, "收起播放器");
        back.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                finish();
            }
        });
        bar.addView(back, new LinearLayout.LayoutParams(dp(44), dp(40)));
        headerTitleView = new TextView(this);
        headerTitleView.setText(mediaTitle.isEmpty() ? "正在播放" : mediaTitle);
        headerTitleView.setTextColor(TEXT_MUTED);
        headerTitleView.setTextSize(15f);
        headerTitleView.setSingleLine(true);
        headerTitleView.setEllipsize(android.text.TextUtils.TruncateAt.MIDDLE);
        headerTitleView.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams titleLp = new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        titleLp.setMargins(dp(10), 0, dp(10), 0);
        bar.addView(headerTitleView, titleLp);

        ((FrameLayout) controls).addView(bar, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.TOP));

        // 填充/全屏 as Eara-style overlay chips pinned under the header
        // (black @0.35, rounded 10dp) — they fade with the controls and stay
        // tappable because they live inside the same overlay.
        LinearLayout chips = new LinearLayout(this);
        chips.setOrientation(LinearLayout.HORIZONTAL);
        chips.setGravity(Gravity.CENTER_VERTICAL);
        fillButton = chipButton(PlayerGlyph.FILL, "填充屏幕（裁切两侧）");
        fillButton.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { setFillScreen(!fillScreen); }
        });
        chips.addView(fillButton);
        fullscreenButton = chipButton(PlayerGlyph.EXPAND, "横屏全屏播放");
        fullscreenButton.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                setLandscapeFullscreen(!landscapeRequested);
            }
        });
        LinearLayout.LayoutParams fsChipLp = new LinearLayout.LayoutParams(
                dp(40), dp(40));
        fsChipLp.leftMargin = dp(8);
        chips.addView(fullscreenButton, fsChipLp);
        // 倍速 rides the same strip as a text chip: the plate can be wider
        // than the 40dp square because the value is the point.
        speedChip = buildSpeedChip();
        LinearLayout.LayoutParams speedLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, dp(40));
        speedLp.leftMargin = dp(8);
        chips.addView(speedChip, speedLp);
        if (remoteQueue != null && remoteQueue.length() > 1) {
            ImageButton queueChip = chipButton(PlayerGlyph.QUEUE, "播放队列");
            queueChip.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { toggleQueuePanel(); }
            });
            LinearLayout.LayoutParams qLp = new LinearLayout.LayoutParams(
                    dp(40), dp(40));
            qLp.leftMargin = dp(8);
            chips.addView(queueChip, qLp);
        }
        FrameLayout.LayoutParams chipsLp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.TOP | Gravity.END);
        chipsLp.setMargins(0, statusBarInset() + dp(58), dp(12), 0);
        ((FrameLayout) controls).addView(chips, chipsLp);

        ((FrameLayout) controls).addView(buildTransport(), new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM));
        return controls;
    }

    /**
     * Rail + times + buttons, in one card so it reads as a single surface
     * rather than three floating pieces.
     */
    private View buildTransport() {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setBackgroundColor(Theme.darkBg(158));
        card.setPadding(dp(16), dp(12), dp(16), dp(14));

        LinearLayout rail = new LinearLayout(this);
        rail.setOrientation(LinearLayout.HORIZONTAL);
        rail.setGravity(Gravity.CENTER_VERTICAL);
        elapsedView = timeLabel();
        totalView = timeLabel();
        seek = new PlayerSeekBar(this);
        seek.setOnSeekListener(new PlayerSeekBar.OnSeekListener() {
            @Override
            public void onScrubStart() {
                userSeeking = true;
                ui.removeCallbacks(hideControls);
            }

            @Override
            public void onScrub(int positionMs) {
                // The label tracks the thumb immediately; the decoder is only
                // asked once the finger lifts. Seeking a concatenated TS makes
                // the extractor rescan to the nearest sync point, so doing it
                // per motion event would queue a dozen scans behind each other.
                elapsedView.setText(formatTime(positionMs));
            }

            @Override
            public void onScrubEnd(int positionMs) {
                userSeeking = false;
                if (player != null) {
                    try {
                        player.seekTo(positionMs);
                    } catch (Exception e) {
                        Log.w(TAG, "seek failed", e);
                    }
                }
                if (player != null && player.isPlaying()) ui.postDelayed(ticker, TICK_MS);
                scheduleHide();
            }
        });
        rail.addView(elapsedView, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        LinearLayout.LayoutParams seekLp = new LinearLayout.LayoutParams(0,
                dp(28), 1f);
        seekLp.setMargins(dp(10), 0, dp(10), 0);
        rail.addView(seek, seekLp);
        rail.addView(totalView, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        card.addView(rail);

        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        buttons.setGravity(Gravity.CENTER);
        buttons.setPadding(0, dp(8), 0, 0);

        ImageButton rewind = new ImageButton(this);
        rewind.setContentDescription("后退 10 秒");
        rewind.setScaleType(ImageView.ScaleType.CENTER);
        rewind.setImageDrawable(new PlayerGlyph(PlayerGlyph.REWIND, dp(30), "10"));
        rewind.setPadding(dp(6), dp(6), dp(6), dp(6));
        rewind.setStateListAnimator(null);
        rewind.setBackground(PressFeedback.on(iconPlate()));
        rewind.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                nudge(-SEEK_STEP_MS);
            }
        });
        buttons.addView(rewind, transportLp(56, 42));

        playPause = new PlayPauseButton(this);
        playPause.setContentDescription("播放/暂停");
        playPause.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                togglePlay();
            }
        });
        buttons.addView(playPause, transportLp(52, 52));

        ImageButton forward = new ImageButton(this);
        forward.setContentDescription("前进 10 秒");
        forward.setScaleType(ImageView.ScaleType.CENTER);
        forward.setImageDrawable(new PlayerGlyph(PlayerGlyph.FORWARD, dp(30), "10"));
        forward.setPadding(dp(6), dp(6), dp(6), dp(6));
        forward.setStateListAnimator(null);
        forward.setBackground(PressFeedback.on(iconPlate()));
        forward.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                nudge(SEEK_STEP_MS);
            }
        });
        buttons.addView(forward, transportLp(56, 42));

        sleepButton = iconButton(PlayerGlyph.MOON, 22, "息屏播放（只播声音）");
        sleepButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startSleepPlayback();
            }
        });
        buttons.addView(sleepButton, transportLp(56, 42));

        card.addView(buttons);
        return card;
    }

    private TextView timeLabel() {
        TextView t = new TextView(this);
        t.setTextColor(TEXT_MUTED);
        t.setTextSize(11.5f);
        t.setTypeface(android.graphics.Typeface.MONOSPACE);
        t.setText("0:00");
        return t;
    }

    /** Shown only when playback genuinely failed — never as a soft "loading". */
    private View buildErrorPanel() {
        errorPanel = new LinearLayout(this);
        errorPanel.setOrientation(LinearLayout.VERTICAL);
        errorPanel.setGravity(Gravity.CENTER);
        errorPanel.setBackgroundColor(Theme.darkBg(235));
        errorPanel.setPadding(dp(24), dp(24), dp(24), dp(24));
        errorPanel.setVisibility(View.GONE);

        errorText = new TextView(this);
        errorText.setTextColor(TEXT_PRIMARY);
        errorText.setTextSize(14f);
        errorText.setGravity(Gravity.CENTER);
        errorText.setLineSpacing(dp(4), 1f);
        errorPanel.addView(errorText);

        // Only visible while an automatic retry is counting down. The number
        // is the proof that something is still happening: a static "加载中"
        // that sits there for five seconds is indistinguishable from a hang.
        retryCountdownLabel = new TextView(this);
        retryCountdownLabel.setTextColor(ACCENT_A);
        retryCountdownLabel.setTextSize(34f);
        retryCountdownLabel.setTypeface(android.graphics.Typeface.MONOSPACE);
        retryCountdownLabel.setGravity(Gravity.CENTER);
        retryCountdownLabel.setContentDescription("自动重试倒计时");
        retryCountdownLabel.setVisibility(View.GONE);
        LinearLayout.LayoutParams countdownLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        countdownLp.setMargins(0, dp(18), 0, 0);
        errorPanel.addView(retryCountdownLabel, countdownLp);

        Button external = new Button(this);
        external.setText("用其他应用打开");
        external.setAllCaps(false);
        external.setTextSize(14f);
        external.setTextColor(Color.WHITE);
        external.setStateListAnimator(null);
        external.setBackground(PressFeedback.on(accentPlate(dp(24))));
        external.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                openExternally();
            }
        });
        LinearLayout.LayoutParams extLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(48));
        extLp.setMargins(0, dp(20), 0, 0);
        errorPanel.addView(external, extLp);

        Button back = new Button(this);
        back.setText("返回");
        back.setAllCaps(false);
        back.setTextSize(14f);
        back.setTextColor(Theme.TEXT_2);
        back.setStateListAnimator(null);
        stylePill(back, 14f, Color.argb(20, 255, 255, 255), Theme.TEXT_2);
        back.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                finish();
            }
        });
        LinearLayout.LayoutParams backLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(48));
        backLp.setMargins(0, dp(10), 0, 0);
        errorPanel.addView(back, backLp);
        return errorPanel;
    }

    // ------------------------------------------------------------ audio mode

    /**
     * The audio page (Eara's Classic now-playing, ported to this shell's
     * hand-drawn chrome): a header row, the square artwork, the identity
     * block, and a bottom column of progress + controls. The artwork floats
     * on the artwork-derived backdrop — no card box, no border; depth comes
     * from the tinted, blurred cover behind it.
     *
     * <p>The transport views (seek, labels, play button) are the SAME
     * instances the video page uses: every tick, scrub and hide-path in this
     * activity works on {@code seek}/{@code elapsedView}/{@code playPause},
     * and building a second set for audio would fork that wiring.
     */
    private void buildAudioChrome(FrameLayout root) {
        // One column inside one overlay layer: the column carries the page
        // (header → artwork → identity → progress → controls, Eara's
        // portrait order), the overlay carries error/loading/background-bar
        // on top. Two siblings of a FrameLayout — a LinearLayout cannot
        // overlay, and a MATCH/MATCH overlay sibling there starves the
        // weighted artwork to zero height (observed live).
        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        root.addView(column, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));

        // The Eara backdrop stack lives under the column, inside the same
        // FrameLayout: hue wash → blurred art → scrim (applyArtworkBackdrop
        // feeds them once artwork arrives). All start GONE.
        backdropTint = new View(this);
        backdropTint.setVisibility(View.GONE);
        root.addView(backdropTint, 0, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));
        backdrop = new ImageView(this);
        backdrop.setScaleType(ImageView.ScaleType.CENTER_CROP);
        backdrop.setVisibility(View.GONE);
        root.addView(backdrop, 1, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));
        backdropScrim = new View(this);
        backdropScrim.setBackgroundColor(Color.argb(96, 0, 0, 0));
        backdropScrim.setVisibility(View.GONE);
        root.addView(backdropScrim, 2, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));

        // The header: collapse on the left, title centered. The video page's
        // top bar never hides; here it is the one row, and the controls-tap
        // gesture owns the rest of the page.
        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        header.setPadding(dp(12), statusBarInset() + dp(6), dp(12), dp(6));
        ImageButton back = iconButton(PlayerGlyph.BACK, 24, "收起播放器");
        back.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { finish(); }
        });
        header.addView(back, new LinearLayout.LayoutParams(dp(44), dp(40)));
        TextView headerTitle = new TextView(this);
        headerTitle.setText(mediaTitle.isEmpty() ? "正在播放" : mediaTitle);
        headerTitle.setTextColor(TEXT_MUTED);
        headerTitle.setTextSize(15f);
        headerTitle.setSingleLine(true);
        headerTitle.setEllipsize(android.text.TextUtils.TruncateAt.MIDDLE);
        headerTitle.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams htLp = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        htLp.setMargins(dp(10), 0, dp(10), 0);
        header.addView(headerTitle, htLp);
        column.addView(header, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        // The staggered-enter body: everything below the header slides up and
        // fades in slot by slot (NowPlayingMotion's 360ms / 90ms recipe).
        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        body.setGravity(Gravity.CENTER_HORIZONTAL);
        body.setPadding(dp(24), dp(8), dp(24), dp(16));
        // Eara's portrait order: the artwork and its identity sit between
        // the header and the bottom controls; the weight takes whatever the
        // header and controls leave over.
        column.addView(body, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));

        audioCover = new ImageView(this);
        audioCover.setScaleType(ImageView.ScaleType.CENTER_CROP);
        int coverSide = audioCoverSide();
        android.graphics.drawable.GradientDrawable coverPlate =
                new android.graphics.drawable.GradientDrawable();
        coverPlate.setCornerRadius(dp(28));
        coverPlate.setColor(Theme.darkSurface(120));
        audioCover.setBackground(coverPlate);
        audioCover.setClipToOutline(true);
        body.addView(audioCover, new LinearLayout.LayoutParams(coverSide, coverSide));

        identityTitle = new TextView(this);
        identityTitle.setTextColor(TEXT_PRIMARY);
        identityTitle.setTextSize(18f);
        identityTitle.setTypeface(android.graphics.Typeface.create(
                android.graphics.Typeface.DEFAULT, android.graphics.Typeface.BOLD));
        identityTitle.setLineSpacing(dp(2), 1f);
        identityTitle.setMaxLines(2);
        identityTitle.setEllipsize(android.text.TextUtils.TruncateAt.END);
        identityTitle.setShadowLayer(dp(2), 0, dp(1), Color.argb(102, 0, 0, 0));
        LinearLayout.LayoutParams titleLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        titleLp.topMargin = dp(20);
        body.addView(identityTitle, titleLp);

        identityMeta = new TextView(this);
        identityMeta.setTextColor(TEXT_MUTED);
        identityMeta.setTextSize(13f);
        identityMeta.setSingleLine(true);
        identityMeta.setEllipsize(android.text.TextUtils.TruncateAt.END);
        LinearLayout.LayoutParams metaLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        metaLp.topMargin = dp(4);
        body.addView(identityMeta, metaLp);

        // Bottom column: progress, then the transport row, pushed to the
        // page's foot and centered — Eara's portrait column spacing (4dp
        // between progress and controls).
        LinearLayout bottom = new LinearLayout(this);
        bottom.setOrientation(LinearLayout.VERTICAL);
        bottom.setPadding(dp(4), 0, dp(4), dp(8));
        bottom.addView(buildAudioProgress());

        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        buttons.setGravity(Gravity.CENTER);
        buttons.setPadding(0, dp(12), 0, 0);

        // The five-across transport: edge ±10s, inner 上一轨/下一轨, centre
        // the play button (Eara's portrait row). The track steps belong on
        // the row a thumb already lives on — a queue should not need a sheet
        // to be walked one track at a time.
        boolean multiTrack = remoteQueue != null && remoteQueue.length() > 1;

        ImageButton rewind = new ImageButton(this);
        rewind.setContentDescription("后退 10 秒");
        rewind.setScaleType(ImageView.ScaleType.CENTER);
        rewind.setImageDrawable(new PlayerGlyph(PlayerGlyph.REWIND, dp(30), "10"));
        rewind.setPadding(dp(6), dp(6), dp(6), dp(6));
        rewind.setStateListAnimator(null);
        rewind.setBackground(PressFeedback.on(iconPlate()));
        rewind.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { nudge(-SEEK_STEP_MS); }
        });
        buttons.addView(rewind, transportLp(64, 52));

        if (multiTrack) {
            ImageButton prev = new ImageButton(this);
            prev.setContentDescription("上一轨");
            prev.setScaleType(ImageView.ScaleType.CENTER);
            prev.setImageDrawable(new PlayerGlyph(PlayerGlyph.PREV, dp(30), ""));
            prev.setPadding(dp(6), dp(6), dp(6), dp(6));
            prev.setStateListAnimator(null);
            prev.setBackground(PressFeedback.on(iconPlate()));
            prev.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { previousQueue(); }
            });
            buttons.addView(prev, transportLp(52, 52));
        }

        playPause = new PlayPauseButton(this, dp(72));
        playPause.setContentDescription("播放/暂停");
        playPause.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { togglePlay(); }
        });
        buttons.addView(playPause, transportLp(84, 72));

        if (multiTrack) {
            ImageButton next = new ImageButton(this);
            next.setContentDescription("下一轨");
            next.setScaleType(ImageView.ScaleType.CENTER);
            next.setImageDrawable(new PlayerGlyph(PlayerGlyph.NEXT, dp(30), ""));
            next.setPadding(dp(6), dp(6), dp(6), dp(6));
            next.setStateListAnimator(null);
            next.setBackground(PressFeedback.on(iconPlate()));
            next.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { nextQueue(); }
            });
            buttons.addView(next, transportLp(52, 52));
        }

        ImageButton forward = new ImageButton(this);
        forward.setContentDescription("前进 10 秒");
        forward.setScaleType(ImageView.ScaleType.CENTER);
        forward.setImageDrawable(new PlayerGlyph(PlayerGlyph.FORWARD, dp(30), "10"));
        forward.setPadding(dp(6), dp(6), dp(6), dp(6));
        forward.setStateListAnimator(null);
        forward.setBackground(PressFeedback.on(iconPlate()));
        forward.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { nudge(SEEK_STEP_MS); }
        });
        buttons.addView(forward, transportLp(64, 52));

        bottom.addView(buttons);

        // The utility row sits under the transport, smaller and quieter, in
        // the place a thumb can still reach one-handed: 倍速, 队列, 息屏.
        LinearLayout utilities = new LinearLayout(this);
        utilities.setOrientation(LinearLayout.HORIZONTAL);
        utilities.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams utilLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        utilLp.topMargin = dp(6);
        utilities.setPadding(0, dp(6), 0, dp(2));
        bottom.addView(utilities, utilLp);

        TextView speed = buildSpeedPill("播放速度");
        audioSpeedPill = speed;
        utilities.addView(speed, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, dp(36)));

        sleepButton = iconButton(PlayerGlyph.MOON, 20, "息屏播放（只播声音）");
        sleepButton.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { startSleepPlayback(); }
        });
        LinearLayout.LayoutParams moonLp = new LinearLayout.LayoutParams(dp(44), dp(36));
        moonLp.leftMargin = dp(12);
        utilities.addView(sleepButton, moonLp);

        if (multiTrack) {
            ImageButton queueButton = iconButton(PlayerGlyph.QUEUE, 20, "播放队列");
            queueButton.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View v) { toggleQueuePanel(); }
            });
            LinearLayout.LayoutParams qLp = new LinearLayout.LayoutParams(dp(44), dp(36));
            qLp.leftMargin = dp(12);
            utilities.addView(queueButton, qLp);
        }
        LinearLayout.LayoutParams bottomLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        bottomLp.topMargin = dp(24);
        column.addView(bottom, bottomLp);

        // Overlays live in a full-screen FrameLayout above the column. A
        // LinearLayout child ignores Gravity.CENTER — adding the error panel
        // straight to the column is what once pushed the artwork clean off
        // the page (its "gravity" argument became a layout weight).
        FrameLayout overlay = new FrameLayout(this);
        overlay.addView(buildErrorPanel(), new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));
        loading = new LoadingRing(this);
        loading.setVisibility(View.GONE);
        overlay.addView(loading, new FrameLayout.LayoutParams(dp(54), dp(54),
                Gravity.CENTER));
        root.addView(overlay, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));
        overlayHost = overlay;

        // The artwork is the only thing on this page without a natural
        // height, so it takes what the page does not need — sized once the
        // page has been laid out and the real budget is known.
        body.addOnLayoutChangeListener(new View.OnLayoutChangeListener() {
            @Override public void onLayoutChange(View v, int l, int t, int r, int b,
                    int ol, int ot, int orb, int ob) {
                fitAudioCover(v);
            }
        });

        enterStagger(column);
    }

    /**
     * The square artwork's widest sensible side: 92% of what the page's inset
     * leaves, Eara's 360dp cap, and a 180dp floor so the identity block still
     * has height on a small phone.
     *
     * <p>The height clamp is not here. A constant for it — "screen height
     * minus 300dp" — cannot know how many rows the page has, and when the
     * utility row arrived under the transport it left a 640dp screen's
     * identity title a 33px band with the meta line squeezed to zero: the
     * weighted body hands its children a fixed leftover, and a LinearLayout
     * clips what does not fit instead of ellipsising it. {@link #fitAudioCover}
     * measures the real thing at layout time instead.
     */
    private int audioCoverSide() {
        int w = getResources().getDisplayMetrics().widthPixels;
        return Math.max(dp(180), Math.min(dp(360), Math.round((w - dp(48)) * 0.92f)));
    }

    /**
     * Give the artwork whatever the rest of the audio page does not need.
     *
     * <p>The identity lines are measured unconstrained first: under a squeezed
     * layout they report the clipped height, and sizing the cover from that
     * would lock the squeeze in. The widths do not depend on the cover, so one
     * pass settles it and the listener's own re-layout finds nothing to change.
     */
    private void fitAudioCover(View body) {
        if (audioCover == null) return;
        int innerW = body.getWidth() - body.getPaddingLeft() - body.getPaddingRight();
        int innerH = body.getHeight() - body.getPaddingTop() - body.getPaddingBottom();
        if (innerW <= 0 || innerH <= 0) return;
        int widthSpec = View.MeasureSpec.makeMeasureSpec(innerW, View.MeasureSpec.EXACTLY);
        int freeSpec = View.MeasureSpec.makeMeasureSpec(0, View.MeasureSpec.UNSPECIFIED);
        int identity = 0;
        View[] lines = {identityTitle, identityMeta};
        for (View line : lines) {
            if (line == null) continue;
            line.measure(widthSpec, freeSpec);
            identity += line.getMeasuredHeight();
            ViewGroup.LayoutParams lp = line.getLayoutParams();
            if (lp instanceof LinearLayout.LayoutParams) {
                identity += ((LinearLayout.LayoutParams) lp).topMargin;
            }
        }
        int side = Math.max(dp(180), Math.min(dp(360),
                Math.min(Math.round(innerW * 0.92f), innerH - identity)));
        ViewGroup.LayoutParams coverLp = audioCover.getLayoutParams();
        if (coverLp == null || coverLp.width == side) return;
        coverLp.width = side;
        coverLp.height = side;
        audioCover.setLayoutParams(coverLp);
    }

    /**
     * Rail + times, full width, no card behind it — on the audio page the
     * seekbar floats on the backdrop like Eara's PlayerProgress, instead of
     * sitting in the video page's bottom slab.
     */
    private View buildAudioProgress() {
        LinearLayout rail = new LinearLayout(this);
        rail.setOrientation(LinearLayout.HORIZONTAL);
        rail.setGravity(Gravity.CENTER_VERTICAL);
        elapsedView = timeLabel();
        totalView = timeLabel();
        seek = new PlayerSeekBar(this);
        seek.setOnSeekListener(new PlayerSeekBar.OnSeekListener() {
            @Override public void onScrubStart() {
                userSeeking = true;
                ui.removeCallbacks(hideControls);
            }
            @Override public void onScrub(int positionMs) {
                elapsedView.setText(formatTime(positionMs));
            }
            @Override public void onScrubEnd(int positionMs) {
                userSeeking = false;
                if (player != null) {
                    try {
                        player.seekTo(positionMs);
                    } catch (Exception e) {
                        Log.w(TAG, "seek failed", e);
                    }
                }
                if (player != null && player.isPlaying()) ui.postDelayed(ticker, TICK_MS);
                scheduleHide();
            }
        });
        rail.addView(elapsedView, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        LinearLayout.LayoutParams seekLp = new LinearLayout.LayoutParams(0, dp(32), 1f);
        seekLp.setMargins(dp(10), 0, dp(10), 0);
        rail.addView(seek, seekLp);
        rail.addView(totalView, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        return rail;
    }

    /** The audio page's identity block: title + the one meta line. */
    private void renderAudioIdentity() {
        if (identityTitle == null) return;
        identityTitle.setText(mediaTitle.isEmpty() ? "正在播放" : mediaTitle);
        String kind = containerLabel(remoteUrl.isEmpty()
                ? (mediaTitle) : remoteUrl);
        identityMeta.setText(kind.isEmpty() ? "在线音频" : "在线音频 · " + kind);
        // The cover fetch belongs to onCreate alone: this runs from
        // buildChrome, which onCreate calls right before its own fetch —
        // keeping both fired two concurrent downloads of the same image.
    }

    /**
     * NowPlayingMotion's route entrance, on the audio page's slots: each
     * direct child of the root starts 40dp low and transparent, then slides
     * and fades in over 360ms, 90ms apart. Header first, artwork second,
     * identity with it, controls last — the page assembles top-down.
     */
    private void enterStagger(LinearLayout root) {
        long delay = 0;
        for (int i = 0; i < root.getChildCount(); i++) {
            final View slot = root.getChildAt(i);
            slot.setTranslationY(dp(40));
            slot.setAlpha(0f);
            slot.animate().translationY(0f).alpha(1f)
                    .setDuration(360L)
                    .setStartDelay(delay)
                    .setInterpolator(new android.view.animation
                            .DecelerateInterpolator(1.6f))
                    .start();
            delay += 90L;
        }
    }

    // --------------------------------------------------------- surface plumbing

    @Override
    public void surfaceCreated(SurfaceHolder h) {
        holder = h;
        if (player == null) {
            // The service already owns this file's audio. Starting a second
            // MediaPlayer here would play the same track twice, out of phase,
            // so the page offers to take the audio back instead.
            if (PlaybackService.isRunning()) {
                showBackgroundBar();
                return;
            }
            open();
            return;
        }
        // The surface was swapped under a live player (the manifest absorbs a
        // rotation instead of recreating this activity, so this is the normal
        // path, not an edge case). Re-attach and restore the position rather
        // than re-preparing: a TS re-prepare means the extractor rescans the
        // head of a file that may be a gigabyte.
        try {
            player.setSurface(h.getSurface());
            if (pendingSeekMs > 0) player.seekTo(pendingSeekMs);
            if (resumeOnSurface) player.start();
            resumeOnSurface = false;
        } catch (Exception e) {
            Log.w(TAG, "surface re-attach failed, reopening", e);
            releasePlayer();
            open();
        }
    }

    @Override
    public void surfaceChanged(SurfaceHolder h, int format, int width, int height) {
        // Nothing to do: the player scales to whatever surface it is given.
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder h) {
        if (player == null) return;
        pendingSeekMs = safePosition();
        resumeOnSurface = player.isPlaying();
        try {
            player.setSurface(null);
        } catch (Exception e) {
            Log.w(TAG, "detaching surface failed", e);
        }
        // The ticker is bound to a surface that no longer exists; let the next
        // prepared/start path restart it.
        ui.removeCallbacks(ticker);
    }

    // --------------------------------------------------------------- playback

    /**
     * The completion listener's step: walk one forward. The actual switching
     * lives in {@link #jumpQueue} — completion, 下一轨 and the queue panel all
     * share it, so a track that will not register is skipped identically no
     * matter who asked.
     */
    private boolean advanceQueue() {
        return stepQueue(1);
    }

    /**
     * A failed step announces nothing on its own: completion's tail message
     * says 「连播结束」, 上一轨/下一轨 say what they tried instead. Only the
     * hand-off success reads as a toast, naming what it switched to.
     */
    private void announceJump() {
        if (remoteQueue == null) return;
        org.json.JSONObject entry = remoteQueue.optJSONObject(remoteQueueIndex);
        String label = entry == null ? "" : entry.optString("l", "").trim();
        Toast.makeText(this, "播放："
                + (label.isEmpty() ? "第 " + (remoteQueueIndex + 1) + " 轨" : label),
                Toast.LENGTH_SHORT).show();
    }

    /**
     * Prepare asynchronously. {@code prepareAsync} does the container sniffing
     * on its own thread, so a 995 MB MPEG-TS does not block the main thread
     * while the extractor walks the head of the file looking for a sync point.
     */
    private void open() {
        errorSeen = false;
        errorWasMidPlay = false;
        cancelAutoRetry();
        autoRetryRemaining = 0;
        autoRetrySpent = 0;
        errorPanel.setVisibility(View.GONE);
        setControlsVisible(true);

        MediaPlayer mp = new MediaPlayer();
        mp.setAudioAttributes(new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MOVIE)
                .build());
        mp.setOnPreparedListener(new MediaPlayer.OnPreparedListener() {
            @Override
            public void onPrepared(MediaPlayer p) {
                prepared = true;
                // A retry that got here has succeeded: drop the panel, stop the
                // countdown, and hand the next failure a fresh budget — an
                // error ten minutes into playback is a new event, not the tail
                // of the one at the head of the stream.
                cancelAutoRetry();
                autoRetryRemaining = 0;
                autoRetrySpent = 0;
                errorSeen = false;
                errorPanel.setVisibility(View.GONE);
                loading.setBusy(false);
                // The poster has done its job: from here the surface owns the
                // picture, and leaving a stale frame on top of it would show
                // last year's thumbnail during a seek.
                if (cover != null) cover.setVisibility(View.GONE);
                // 倍速 survives a track jump: the user chose it, the session
                // owns it — a queue walk must not silently reset it to 1.0.
                if (Math.abs(playbackSpeed - 1.0f) > 0.01f) {
                    setPlaybackSpeed(playbackSpeed);
                }
                durationMs = p.getDuration();
                if (metaDurationMs <= 0 && durationMs > 0) {
                    metaDurationMs = durationMs;
                    renderMeta();
                }
                if (durationMs > 0) {
                    seek.setMax(durationMs);
                    totalView.setText(formatTime(durationMs));
                    seek.setEnabled(true);
                } else {
                    // Unknown length (some TS streams report -1). The rail is
                    // parked rather than faked with a full-width fill.
                    seek.setEnabled(false);
                    totalView.setText("--:--");
                }
                if (pendingSeekMs > 0) {
                    p.seekTo(pendingSeekMs);
                    // Say it out loud: a player that silently opens at 5:00
                    // reads as a broken file, not as a remembered place.
                    if (resumeAnnounceMs > 0) {
                        Toast.makeText(PlayerActivity.this,
                                "已续播到 " + formatTime((int) resumeAnnounceMs),
                                Toast.LENGTH_SHORT).show();
                        resumeAnnounceMs = -1L;
                    }
                    pendingSeekMs = 0;
                }
                playPause.setPlaying(true);
                p.start();
                ui.removeCallbacks(ticker);
                ui.postDelayed(ticker, TICK_MS);
                scheduleHide();
            }
        });
        mp.setOnCompletionListener(new MediaPlayer.OnCompletionListener() {
            @Override
            public void onCompletion(MediaPlayer p) {
                playPause.setPlaying(false);
                setControlsVisible(true);
                ui.removeCallbacks(ticker);
                syncProgress();
                // Watched to the end, so the place is spent. Leaving it would
                // drop the user into the closing seconds on the next open —
                // the one thing a "remember where I was" feature must not do.
                clearResume();
                // Eara's continuous playback: a work that handed the page a
                // track list keeps going — the next track replaces this one
                // in place, same page, same proxy.  A failed handover (bad
                // queue entry, proxy refused) falls back to the idle stop.
                if (advanceQueue()) {
                    releasePlayer();
                    open();
                    return;
                }
                // A queue that ran to its end says so: a page that simply
                // goes idle reads as "it broke", and a single track's end
                // stays silent exactly as before.
                if (remoteQueue != null) {
                    Toast.makeText(PlayerActivity.this, "连播结束",
                            Toast.LENGTH_SHORT).show();
                }
            }
        });
        mp.setOnErrorListener(new MediaPlayer.OnErrorListener() {
            @Override
            public boolean onError(MediaPlayer p, int what, int extra) {
                handlePlaybackError(what, extra);
                return true; // handled — do not let the framework pop its own dialog
            }
        });
        mp.setOnInfoListener(new MediaPlayer.OnInfoListener() {
            @Override
            public boolean onInfo(MediaPlayer p, int what, int extra) {
                // A buffering stall is a state, not a failure: the ring comes
                // back without the error panel ever appearing.
                if (what == MediaPlayer.MEDIA_INFO_BUFFERING_START) loading.setBusy(true);
                else if (what == MediaPlayer.MEDIA_INFO_BUFFERING_END) loading.setBusy(false);
                return false;
            }
        });
        // The stage letterbox wants the real picture shape. A local file gets
        // it from the retriever sniff; a stream has no file to sniff, so the
        // decoder itself reports it the moment the first frame exists.
        mp.setOnVideoSizeChangedListener(new MediaPlayer.OnVideoSizeChangedListener() {
            @Override
            public void onVideoSizeChanged(MediaPlayer p, int width, int height) {
                if (width <= 0 || height <= 0) return;
                metaWidth = width;
                metaHeight = height;
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        if (isFinishing()) return;
                        applyOrientationLayout();
                        applyStageAspect();
                        renderMeta();
                    }
                });
            }
        });

        player = mp;
        loading.setBusy(true);
        try {
            mp.setDisplay(holder);
            mp.setDataSource(this, mediaUri);
            // Valid from Initialized onward, and deliberately not earlier: a
            // freshly constructed MediaPlayer is still Idle, and asking an
            // Idle player for anything beyond setAudioAttributes/setDisplay
            // throws IllegalStateException("wrong state"). Called here it
            // lands before the first frame, which is the point — the mode is
            // read when the output surface is configured, so setting it after
            // prepare would be too late to matter.
            mp.setVideoScalingMode(scalingMode());
            mp.prepareAsync();
        } catch (Exception e) {
            Log.w(TAG, "prepare failed", e);
            // The URI rides along: a setDataSource failure without it is
            // undiagnosable from a screenshot alone.
            showError("无法读取文件：" + e.getMessage() + "\n（" + mediaUri + "）");
        }
    }

    // ------------------------------------------------------------ auto retry

    /**
     * A playback failure is an event to recover from, not a verdict.
     *
     * <p>Reporting the error and stopping — which is all this used to do —
     * treats a stumble at the head of a stream as permanent. It is not: the
     * first seconds are exactly where the extractor races whatever produced
     * the bytes, and that race settles on its own within a second or two.
     * Making the user back out and tap again to discover that is the bug.
     *
     * <p>So a failure arms a budget of {@value #MAX_AUTO_RETRIES} attempts,
     * each preceded by a visible countdown, and only the failure that exhausts
     * the budget is reported. The budget lives here rather than in the
     * listener so that a synchronous failure of the re-bind — which never
     * reaches {@code onError} because nothing is in flight — spends an attempt
     * through exactly the same path.
     */
    private void handlePlaybackError(int what, int extra) {
        // Both captured before prepared is cleared below, because when the
        // failure happened decides what it can mean. A codec the device lacks
        // fails before onPrepared and stays terminal; the same code arriving
        // after minutes of playback is a broken delivery — a CDN segment that
        // truncates mid-read reports (1,-1010) "unsupported" for a file that
        // was visibly playing (on-device: twelve segments in, seg-12 timed
        // out). Once the extractor has accepted the file, the retry ladder is
        // the honest response to any badge the error wears. The flag also
        // stays sticky through the retry chain: a re-prepare that fails
        // before onPrepared has prepared=false again, and re-reading it as
        // "codec missing" would contradict the playback that just happened.
        boolean extractorAccepted = prepared || errorWasMidPlay;
        int playedTo = safePosition();
        errorWhat = what;
        errorExtra = extra;
        errorWasMidPlay = extractorAccepted;
        errorSeen = true;
        prepared = false;
        Log.w(TAG, "playback failed what=" + what + " extra=" + extra
                + " uri=" + mediaUri);
        if (!extractorAccepted && !isTransient(extra)) {
            // Nothing about this improves in five seconds.
            showFinalError();
            return;
        }
        // Resume the break, not the file: a re-prepare that restarts at zero
        // throws away everything watched since the failure — for a stream
        // that breaks every few minutes, that is every minute forever.
        if (extractorAccepted && playedTo > 0) pendingSeekMs = playedTo;
        if (autoRetryRemaining <= 0) autoRetryRemaining = MAX_AUTO_RETRIES;
        else autoRetryRemaining--;
        if (autoRetryRemaining <= 0) {
            showFinalError();
            return;
        }
        showRetrying();
        scheduleAutoRetry();
    }

    /**
     * Whether another attempt could plausibly succeed. A codec the device does
     * not have will still be missing after a retry, and a container that is
     * genuinely malformed cannot un-malform; queueing three attempts for those
     * only delays the honest message by fifteen seconds. An I/O or timeout
     * failure is the opposite case, and it is the common one.
     */
    private static boolean isTransient(int extra) {
        switch (extra) {
            case MediaPlayer.MEDIA_ERROR_UNSUPPORTED:
            case MediaPlayer.MEDIA_ERROR_MALFORMED:
                return false;
            default:
                return true;
        }
    }

    // ------------------------------------------------------------ queue walk

    /**
     * Switch to an arbitrary queue entry. The caller has already decided the
     * index is in range; an entry that fails to register is on the caller to
     * walk past (advanceQueue/previousQueue do their own skipping and count
     * direction by hand — a failed direct jump just reports it).
     */
    private void switchQueue(final int index) {
        if (remoteQueue == null || index < 0 || index >= remoteQueue.length()) return;
        // A queue row the user tapped: the panel closes, the player jumps.
        // The same-index tap is the one legitimate no-op — it means "play
        // what's already on", which is the play button's job.
        if (index == remoteQueueIndex) {
            if (player != null && prepared && !player.isPlaying()) togglePlay();
            return;
        }
        if (stepQueueTo(index)) {
            return;
        }
        Toast.makeText(this, "这一轨无法播放", Toast.LENGTH_SHORT).show();
    }

    /** The panel's direct jump: the entry must exist and register. */
    private boolean stepQueueTo(int index) {
        if (!jumpQueue(index)) return false;
        releasePlayer();
        open();
        announceJump();
        return true;
    }

    /** The user's 上一轨: more than three seconds in, it restarts this
     *  track — the convention every player on the platform shares. */
    private void previousQueue() {
        if (safePosition() > 3000 && player != null && prepared) {
            try {
                player.seekTo(0);
                if (!player.isPlaying()) togglePlay();
            } catch (Exception e) {
                Log.w(TAG, "previous restart failed", e);
            }
            return;
        }
        if (!stepQueue(-1)) {
            Toast.makeText(this, "已经是第一轨", Toast.LENGTH_SHORT).show();
        }
    }

    private void nextQueue() {
        if (!stepQueue(1)) {
            Toast.makeText(this, "已经是最后一轨", Toast.LENGTH_SHORT).show();
        }
    }

    /** Directional walk from the current index, skipping entries that will
     *  not register. Returns false when the queue ran out in that direction. */
    private boolean stepQueue(int delta) {
        if (remoteQueue == null) return false;
        for (int next = remoteQueueIndex + delta;
                next >= 0 && next < remoteQueue.length(); next += delta) {
            if (jumpQueue(next)) {
                releasePlayer();
                open();
                announceJump();
                return true;
            }
        }
        return false;
    }

    /** Take queue entry {@code index}: re-register the proxy, swap the media,
     *  reset identity/facts/resume. False when the entry refused. */
    private boolean jumpQueue(int index) {
        org.json.JSONObject entry = remoteQueue.optJSONObject(index);
        if (entry == null) return false;
        String url = entry.optString("u", "").trim();
        if (url.isEmpty()) return false;
        String kind = entry.optString("k", "direct").trim();
        if (!"hls".equals(kind) && !"direct".equals(kind)) return false;
        // The entry's own Referer (the bridge computed it per source —
        // a queue can span hosts); the tapped track's is the fallback
        // for entries that predate the per-entry field.
        String referer = entry.optString("r", "").trim();
        if (referer.isEmpty()) referer = remoteReferer;
        // Drop this page's reference before taking the next one:
        // register() acquires, and a queue that never released would pin
        // the loopback server open for the rest of the process.
        if (!proxiedUrl.isEmpty()) {
            proxiedUrl = "";
            StreamProxy.release();
        }
        try {
            proxiedUrl = StreamProxy.register(url, referer);
        } catch (Exception e) {
            Log.w(TAG, "queue jump: proxy registration failed", e);
            return false;
        }
        remoteQueueIndex = index;
        remoteUrl = url;
        remoteKind = kind;
        mediaUri = Uri.parse(proxiedUrl);
        String label = entry.optString("l", "").trim();
        if (!label.isEmpty()) {
            mediaTitle = label;
            if (identityTitle != null) identityTitle.setText(label);
            if (headerTitleView != null) headerTitleView.setText(label);
        }
        if (identityMeta != null && remoteQueue.length() > 1) {
            identityMeta.setText(queueIndexLine());
        }
        // The previous track's facts (duration, resolution) are not this
        // track's: a table that kept them would report them forever,
        // because onPrepared only backfills a duration it finds missing.
        metaDurationMs = -1;
        metaWidth = 0;
        metaHeight = 0;
        metaBitrate = 0;
        pendingSeekMs = 0;
        resumeAnnounceMs = -1L;
        resumeKey = PlaybackResume.keyFor(taskId, url);
        return true;
    }

    /** The 「3 / 12 · 轨道名」 line the audio page shows under the title. */
    private String queueIndexLine() {
        if (remoteQueue == null || remoteQueue.length() <= 1) return "";
        return (remoteQueueIndex + 1) + " / " + remoteQueue.length();
    }

    /**
     * Count down, then re-prepare. {@code postDelayed} rather than a
     * {@code Timer}: this is the same {@link Handler} the ticker and the
     * auto-hide already use, so one {@code removeCallbacks} in {@link
     * #releasePlayer} tears all of it down and no timer thread can outlive the
     * activity.
     */
    private void scheduleAutoRetry() {
        cancelAutoRetry();
        autoRetrySecondsLeft = RETRY_COUNTDOWN_SECONDS;
        autoRetryRunnable = new Runnable() {
            @Override
            public void run() {
                autoRetrySecondsLeft--;
                if (autoRetrySecondsLeft > 0) {
                    retryCountdownLabel.setText(String.valueOf(autoRetrySecondsLeft));
                    ui.postDelayed(this, RETRY_TICK_MS);
                    return;
                }
                autoRetryRunnable = null;
                runAutoRetry();
            }
        };
        retryCountdownLabel.setText(String.valueOf(autoRetrySecondsLeft));
        ui.postDelayed(autoRetryRunnable, RETRY_TICK_MS);
    }

    /**
     * Re-open the same source against the player we already have.
     *
     * <p>{@code reset()} is the legitimate way out of the Error state, and it
     * lands in Idle — from there {@code setDataSource} and {@code
     * prepareAsync} are the same two calls {@link #open} made, so the retry is
     * a re-prepare and not a rebuild. The player object and its listener
     * survive {@code reset}, which is what lets {@code onPrepared} fire into
     * the already-wired callback and resume playback with no re-wiring; only
     * the display has to be re-applied, because {@code reset} drops the
     * surface. Releasing and constructing a new player would work too, but it
     * would mean re-registering four listeners and would lose the surface
     * holder for no reason.
     */
    private void runAutoRetry() {
        MediaPlayer mp = player;
        if (mp == null) return;
        autoRetrySpent++;
        try {
            mp.reset();
            mp.setDisplay(holder);
            mp.setDataSource(this, mediaUri);
            // Same ordering rule as open(): the scaling mode is only valid
            // from Initialized onward, so it follows setDataSource.
            mp.setVideoScalingMode(scalingMode());
            loading.setBusy(true);
            mp.prepareAsync();
        } catch (Exception e) {
            Log.w(TAG, "auto retry could not re-open the source", e);
            // Nothing is in flight, so onError will never fire for this. Spend
            // the attempt through the same path, keeping the original cause:
            // the reason this retry failed is the reason we were retrying.
            handlePlaybackError(errorWhat, errorExtra);
        }
    }

    private void cancelAutoRetry() {
        if (autoRetryRunnable != null) {
            ui.removeCallbacks(autoRetryRunnable);
            autoRetryRunnable = null;
        }
    }

    private void releasePlayer() {
        cancelAutoRetry();
        ui.removeCallbacks(ticker);
        prepared = false;
        durationMs = -1;
        MediaPlayer mp = player;
        player = null;
        if (mp == null) return;
        try {
            mp.reset();
            mp.release();
        } catch (Exception e) {
            Log.w(TAG, "release failed", e);
        }
    }

    private void togglePlay() {
        if (player == null || !prepared) return;
        if (player.isPlaying()) {
            player.pause();
            playPause.setPlaying(false);
            setControlsVisible(true);
            ui.removeCallbacks(ticker);
            ui.removeCallbacks(hideControls);
        } else {
            player.start();
            playPause.setPlaying(true);
            ui.removeCallbacks(ticker);
            ui.postDelayed(ticker, TICK_MS);
            scheduleHide();
        }
    }

    /** ±10s relative to where the decoder actually is, clamped to the media. */
    private void nudge(long deltaMs) {
        if (player == null || !prepared) return;
        long target = safePosition() + deltaMs;
        if (target < 0) target = 0;
        if (durationMs > 0 && target > durationMs) target = durationMs;
        try {
            player.seekTo((int) target);
        } catch (Exception e) {
            Log.w(TAG, "nudge seek failed", e);
        }
        elapsedView.setText(formatTime((int) target));
        seek.setProgress((int) target);
        scheduleHide();
    }

    private int safePosition() {
        if (player == null || !prepared) return 0;
        try {
            return player.getCurrentPosition();
        } catch (Exception e) {
            return 0;
        }
    }

    /** The best length available: the decoder's, or the retriever's. */
    private int knownDuration() {
        return durationMs > 0 ? durationMs : metaDurationMs;
    }

    // ----------------------------------------------------------- resume store

    /**
     * Write the place, or forget it when the file is finished.
     *
     * <p>Both halves matter. Only ever writing would leave a stale record behind
     * every time the user watched to the end and then paused instead of letting
     * it run out — and the next open would jump them into the closing seconds.
     *
     * <p>A file that never prepared is skipped outright: it has no position, and
     * clearing on its behalf would wipe a record that an earlier, working
     * session wrote.
     */
    private void saveResume() {
        if (resumeKey.isEmpty() || !prepared || errorSeen) return;
        int position = safePosition();
        int duration = knownDuration();
        if (PlaybackResume.shouldStore(position, duration)) {
            storeResume(resumeKey, position, duration);
        } else {
            dropResume(resumeKey);
        }
    }

    /** Forget the place outright — the file was watched to its end. */
    private void clearResume() {
        if (resumeKey.isEmpty()) return;
        dropResume(resumeKey);
    }

    private void storeResume(String key, long positionMs, long durationMs) {
        ResumeStore.store(getSharedPreferences(PREFS, MODE_PRIVATE),
                key, positionMs, durationMs);
    }

    private void dropResume(String key) {
        ResumeStore.drop(getSharedPreferences(PREFS, MODE_PRIVATE), key);
    }

    /** {@code {positionMs, durationMs}} of the record, or null when there is none. */
    private long[] readResume(String key) {
        return ResumeStore.read(getSharedPreferences(PREFS, MODE_PRIVATE), key);
    }

    private void syncProgress() {
        if (player == null || !prepared || userSeeking) return;
        int position = safePosition();
        seek.setProgress(position);
        elapsedView.setText(formatTime(position));
    }

    private void scheduleHide() {
        ui.removeCallbacks(hideControls);
        ui.postDelayed(hideControls, CONTROLS_TIMEOUT_MS);
    }

    private void setControlsVisible(boolean visible) {
        if (controls == null) {
            // Audio mode has no overlay to fade: the transport is always on
            // the page, so "hide" is a no-op and "show" just rearms the
            // timer the video page shares.
            if (visible) scheduleHide();
            else ui.removeCallbacks(hideControls);
            return;
        }
        controls.animate().cancel();
        controls.animate().alpha(visible ? 1f : 0f)
                .setDuration(180L).start();
        if (visible) scheduleHide();
        else ui.removeCallbacks(hideControls);
    }

    /**
     * A named reason, not a generic "播放失败". The whole point of the shell
     * is that it never dresses a broken save up as a working one, and that
     * applies to a file it cannot decode just as much as to a failed download.
     */
    private String describe(int what, int extra) {
        // A stream that played and then broke is not a codec verdict. The
        // extractor accepted the file, so "不受支持" would claim the opposite
        // of what the user just watched; name the delivery instead. (A local
        // file that goes malformed mid-play really is corrupt — that verdict
        // stays.)
        if (errorWasMidPlay && !remoteUrl.isEmpty()
                && (extra == MediaPlayer.MEDIA_ERROR_UNSUPPORTED
                    || extra == MediaPlayer.MEDIA_ERROR_MALFORMED)) {
            return "播放中途断流。\n"
                + "刚才还能正常播，问题多半出在网络或源站，不是这份文件。";
        }
        if (extra == MediaPlayer.MEDIA_ERROR_UNSUPPORTED) {
            return "这台设备没有能解码该文件的组件。\n"
                + "文件是完整的，但容器或编码不受支持。";
        }
        if (extra == MediaPlayer.MEDIA_ERROR_MALFORMED) {
            return "文件内容不完整或已损坏，无法解码。";
        }
        if (extra == MediaPlayer.MEDIA_ERROR_IO) {
            return "读取文件失败：文件可能已被删除或无法访问。";
        }
        if (extra == MediaPlayer.MEDIA_ERROR_TIMED_OUT) {
            return "读取超时，文件可能过大或存储响应缓慢。";
        }
        return "播放失败（错误 " + what + " / " + extra + "）。";
    }

    private void showError(String message) {
        cancelAutoRetry();
        showPanel(message, false);
    }

    /**
     * The panel while a retry is still owed. The reason is withheld here on
     * purpose: telling the user "文件可能已被删除" while the shell is about to
     * successfully open that same file is the kind of message that teaches
     * people not to trust the panel. The named cause belongs to the final
     * panel, once there is genuinely nothing left to try.
     */
    private void showRetrying() {
        showPanel("播放出错，" + RETRY_COUNTDOWN_SECONDS + " 秒后自动重试。\n还剩 "
                + autoRetryRemaining + " 次机会", true);
    }

    /** The panel once the retry budget is gone: the cause, and what was tried. */
    private void showFinalError() {
        String message = describe(errorWhat, errorExtra);
        if (autoRetrySpent > 0) {
            message += "\n\n已自动重试 " + autoRetrySpent + " 次仍未成功。";
        }
        if (!remoteUrl.isEmpty()) {
            // A stream failure is invisible without its proxy log: a bare
            // MediaPlayer code reads the same for a refused upstream, an
            // expired signed URL and a proxy black hole. The proxy's own
            // last lines are the only on-screen truth.
            String proxy = StreamProxy.recentFailures();
            if (!proxy.isEmpty()) {
                message += "\n\n流代理记录：\n" + proxy;
            }
            message += "\n\n也可以「返回」后直接在网页里播放。";
        }
        showError(message);
    }

    /**
     * Shown only when playback genuinely failed — never as a soft "loading".
     * {@code countingDown} is what separates the two panels: with a retry still
     * owed the panel carries the countdown, and it is not a failure report yet.
     */
    private void showPanel(String message, boolean countingDown) {
        loading.setBusy(false);
        ui.removeCallbacks(ticker);
        ui.removeCallbacks(hideControls);
        playPause.setPlaying(false);
        errorText.setText(message);
        retryCountdownLabel.setVisibility(countingDown ? View.VISIBLE : View.GONE);
        errorPanel.setVisibility(View.VISIBLE);
        errorPanel.bringToFront();
    }

    /**
     * Hand the same URI to whatever the system does claim, if anything does.
     * Resolved first: an unresolved {@code ACTION_VIEW} here would throw
     * exactly the exception this activity exists to replace.
     */
    private void openExternally() {
        if (mediaUri == null) return;
        Intent view = new Intent(Intent.ACTION_VIEW);
        view.setDataAndType(mediaUri, mimeOf(mediaTitle));
        view.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        try {
            if (getPackageManager().resolveActivity(view, PackageManager.MATCH_DEFAULT_ONLY) == null) {
                Toast.makeText(this, "系统里没有能打开这个格式的应用", Toast.LENGTH_LONG).show();
                return;
            }
            startActivity(view);
        } catch (Exception e) {
            Log.w(TAG, "external open failed", e);
            Toast.makeText(this, "无法用其他应用打开：" + e.getMessage(),
                    Toast.LENGTH_LONG).show();
        }
    }

    /** Mirrors the shell's mapping so the external attempt declares the truth. */
    private static String mimeOf(String name) {
        String n = name == null ? "" : name.toLowerCase(Locale.US);
        if (n.endsWith(".ts")) return "video/mp2t";
        if (n.endsWith(".mp4")) return "video/mp4";
        if (n.endsWith(".webm")) return "video/webm";
        if (n.endsWith(".m4a")) return "audio/mp4";
        if (n.endsWith(".mp3")) return "audio/mpeg";
        if (n.endsWith(".aac")) return "audio/aac";
        if (n.endsWith(".ogg")) return "audio/ogg";
        if (n.endsWith(".opus")) return "audio/ogg";
        if (n.endsWith(".wav")) return "audio/wav";
        if (n.endsWith(".flac")) return "audio/flac";
        return "application/octet-stream";
    }

    // ------------------------------------------------------------- lifecycle

    @Override
    protected void onPause() {
        super.onPause();
        // Never keep decoding (and the screen lit) in the background.
        if (player != null) {
            try {
                if (player.isPlaying()) player.pause();
            } catch (Exception e) {
                Log.w(TAG, "pause failed", e);
            }
        }
        // After the pause, so the position written is the one the user left
        // rather than the one the decoder was about to reach.
        saveResume();
        if (playPause != null) playPause.setPlaying(false);
        ui.removeCallbacks(ticker);
        ui.removeCallbacks(hideControls);
        if (surface != null) surface.setKeepScreenOn(false);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (surface != null && !errorSeen) surface.setKeepScreenOn(true);
    }

    @Override
    protected void onDestroy() {
        releasePlayer();
        // The idle timer on the proxy's side covers the sleep-handoff race
        // (the service may not have acquired its reference yet); releasing
        // here only means "this page no longer needs the stream".
        if (!proxiedUrl.isEmpty()) {
            proxiedUrl = "";
            StreamProxy.release();
        }
        super.onDestroy();
    }

    @Override
    public void onConfigurationChanged(android.content.res.Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        // The manifest absorbs rotation/resize, so this activity and its player
        // survive. The surface callback restores the position if the window
        // resize forced the surface to be recreated — but the *layout* is ours
        // to redo: portrait wants a 16:9 stage plus a facts column, landscape
        // wants the whole window and no column. Without this a rotation kept
        // the old arrangement and the page simply looked broken on its side.
        applyOrientationLayout();
    }

    // -------------------------------------------------------------- utilities

    private static String formatStamp(long millis) {
        return new java.text.SimpleDateFormat("yyyy-MM-dd HH:mm",
                java.util.Locale.getDefault()).format(new java.util.Date(millis));
    }

    private static String formatBytes(long bytes) {
        if (bytes <= 0) return "0 B";
        String[] units = {"B", "KB", "MB", "GB", "TB"};
        double v = bytes;
        int i = 0;
        while (v >= 1024 && i < units.length - 1) {
            v /= 1024;
            i++;
        }
        java.text.DecimalFormat df = new java.text.DecimalFormat(i == 0 ? "0" : "#.#");
        return df.format(v) + " " + units[i];
    }

    private static String formatTime(int ms) {
        if (ms < 0) ms = 0;
        int totalSeconds = ms / 1000;
        int hours = totalSeconds / 3600;
        int minutes = (totalSeconds % 3600) / 60;
        int seconds = totalSeconds % 60;
        if (hours > 0) {
            return String.format(Locale.US, "%d:%02d:%02d", hours, minutes, seconds);
        }
        return String.format(Locale.US, "%d:%02d", minutes, seconds);
    }

    private static android.graphics.drawable.GradientDrawable accentPlate(int radiusPx) {
        android.graphics.drawable.GradientDrawable bg =
                new android.graphics.drawable.GradientDrawable(
                        android.graphics.drawable.GradientDrawable.Orientation.TL_BR,
                        new int[]{ACCENT_A, ACCENT_B});
        bg.setCornerRadius(radiusPx);
        return bg;
    }

    private void stylePill(Button button, float textSp, int fill, int textColor) {
        button.setAllCaps(false);
        button.setTextSize(textSp);
        button.setTextColor(textColor);
        button.setStateListAnimator(null);
        button.setMinWidth(0);
        button.setMinimumWidth(0);
        button.setMinimumHeight(0);
        button.setPadding(dp(6), 0, dp(6), 0);
        android.graphics.drawable.GradientDrawable bg =
                new android.graphics.drawable.GradientDrawable();
        bg.setColor(fill);
        bg.setStroke(Math.max(1, dp(1)), Color.argb(40, 255, 255, 255));
        bg.setCornerRadius(dp(21));
        button.setBackground(PressFeedback.on(bg));
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    // ------------------------------------------------------------ custom views

    /**
     * The progress rail. Hand-drawn for two reasons: the stock
     * {@code SeekBar}'s track and thumb come from the platform theme and cannot
     * be made to match this app's palette without shipping drawable XML for
     * every density bucket; and a rail whose geometry is derived from its own
     * bounds is one implementation that is correct on a phone and a tablet
     * alike. Nothing here is a fixed px.
     */
    private static final class PlayerSeekBar extends View {

        interface OnSeekListener {
            void onScrubStart();
            void onScrub(int positionMs);
            void onScrubEnd(int positionMs);
        }

        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final RectF rail = new RectF();
        private final Path fillPath = new Path();

        private OnSeekListener listener;
        private int max = 1;
        private int progress;
        private boolean enabled = true;
        private boolean dragging;
        /** Fraction of the rail the knob sits at, 0..1 — the single source of
         *  truth for both the fill and the knob, so they can never disagree. */
        private float fraction;

        PlayerSeekBar(Context context) {
            super(context);
            setFocusable(true);
            setClickable(true);
        }

        void setOnSeekListener(OnSeekListener l) {
            this.listener = l;
        }

        void setMax(int maxMs) {
            this.max = Math.max(1, maxMs);
            invalidate();
        }

        void setProgress(int positionMs) {
            if (dragging) return;
            this.progress = Math.max(0, Math.min(positionMs, max));
            this.fraction = (float) progress / (float) max;
            invalidate();
        }

        @Override
        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
            super.setEnabled(enabled);
            invalidate();
        }

        @Override
        protected void onDraw(Canvas canvas) {
            float w = getWidth();
            float h = getHeight();
            if (w <= 0f || h <= 0f) return;
            float centreY = h / 2f;
            float trackH = Math.max(2f, h * 0.10f);
            float knobR = Math.max(trackH, h * 0.20f);
            // The knob's own radius is reserved at both ends so it is never
            // clipped by the view edge at 0% or 100%.
            float left = knobR;
            float right = w - knobR;
            if (right <= left) return;
            rail.set(left, centreY - trackH / 2f, right, centreY + trackH / 2f);

            paint.setStyle(Paint.Style.FILL);
            paint.setColor(Color.argb(enabled ? 46 : 24, 255, 255, 255));
            canvas.drawRoundRect(rail, trackH / 2f, trackH / 2f, paint);

            float knobX = left + (right - left) * fraction;
            if (fraction > 0f) {
                paint.setShader(new android.graphics.LinearGradient(
                        left, 0f, right, 0f, ACCENT_A, ACCENT_B,
                        android.graphics.Shader.TileMode.CLAMP));
                fillPath.reset();
                fillPath.addRoundRect(new RectF(left, rail.top, Math.max(knobX, left + 0.5f),
                        rail.bottom), trackH / 2f, trackH / 2f, Path.Direction.CW);
                canvas.drawPath(fillPath, paint);
                paint.setShader(null);
            }

            paint.setColor(enabled ? Color.WHITE : Color.argb(120, 255, 255, 255));
            canvas.drawCircle(knobX, centreY, knobR, paint);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeWidth(Math.max(1f, h * 0.02f));
            paint.setColor(Theme.darkSurface(enabled ? 90 : 40));
            canvas.drawCircle(knobX, centreY, knobR, paint);
            paint.setStyle(Paint.Style.FILL);
            // Eara's thumb: an inner dot at 45% of the radius, always visible
            // — the drag affordance reads without the thumb growing.
            paint.setColor(Color.argb(217, 0, 0, 0));
            canvas.drawCircle(knobX, centreY, knobR * 0.45f, paint);
        }

        @Override
        public boolean onTouchEvent(MotionEvent event) {
            if (!enabled) return false;
            switch (event.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    dragging = true;
                    getParent().requestDisallowInterceptTouchEvent(true);
                    if (listener != null) listener.onScrubStart();
                    updateFromTouch(event.getX());
                    return true;
                case MotionEvent.ACTION_MOVE:
                    if (dragging) updateFromTouch(event.getX());
                    return true;
                case MotionEvent.ACTION_UP:
                case MotionEvent.ACTION_CANCEL:
                    if (dragging) {
                        dragging = false;
                        getParent().requestDisallowInterceptTouchEvent(false);
                        int target = Math.round(fraction * max);
                        progress = target;
                        if (listener != null) listener.onScrubEnd(target);
                    }
                    return true;
                default:
                    return super.onTouchEvent(event);
            }
        }

        private void updateFromTouch(float x) {
            float knobR = Math.max(2f, getHeight() * 0.20f);
            float left = knobR;
            float right = getWidth() - knobR;
            if (right <= left) return;
            fraction = Math.max(0f, Math.min(1f, (x - left) / (right - left)));
            if (listener != null) listener.onScrub(Math.round(fraction * max));
            invalidate();
        }
    }

    /**
     * The play/pause target: an accent-filled rounded square with a drawn
     * glyph, so the one control the user hits most often is also the one that
     * reads at a glance against a bright frame.
     *
     * <p>Eara morphs the corner between 36dp (paused — nearly a circle) and
     * 24dp (playing — a squircle) over 220ms; the same two states live here
     * as an animated corner radius on the accent plate.
     */
    private static final class PlayPauseButton extends View {

        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Path glyph = new Path();
        private final android.graphics.drawable.GradientDrawable disc =
                accentPlate(0);
        private final int side;
        private boolean playing;
        /** Corner radius currently drawn, in px — animated toward its target. */
        private float corner;
        private long lastFrame;

        PlayPauseButton(Context context) {
            this(context, 0);
        }

        /** Fixed side in px; 0 keeps the old measure-own-size behaviour. */
        PlayPauseButton(Context context, int sidePx) {
            super(context);
            side = sidePx;
            setClickable(true);
            setFocusable(true);
            paint.setColor(Color.WHITE);
            paint.setStyle(Paint.Style.FILL);
        }

        void setPlaying(boolean playing) {
            if (this.playing == playing) return;
            this.playing = playing;
            postInvalidateOnAnimation();
        }

        @Override
        protected void onMeasure(int widthSpec, int heightSpec) {
            if (side <= 0) {
                super.onMeasure(widthSpec, heightSpec);
                return;
            }
            setMeasuredDimension(side, side);
        }

        @Override
        protected void onDraw(Canvas canvas) {
            float w = getWidth();
            float h = getHeight();
            float size = Math.min(w, h);
            if (size <= 0f) return;
            float centreX = w / 2f;
            float centreY = h / 2f;

            // The corner morph: 44% of the side when paused (a near-circle),
            // 30% when playing (a squircle), eased 220ms — Eara's 36↔24dp on
            // a 72dp button. Stepped from the frame clock so no animator
            // object is held.
            float target = playing ? size * 0.30f : size * 0.44f;
            if (corner == 0f) corner = target;
            long now = SystemClock.uptimeMillis();
            float dt = lastFrame == 0 ? 16f : Math.min(64f, now - lastFrame);
            lastFrame = now;
            float t = Math.min(1f, dt / 220f);
            corner += (target - corner) * t;
            if (Math.abs(target - corner) < size * 0.004f) corner = target;
            else postInvalidateOnAnimation();

            disc.setCornerRadius(corner);
            disc.setBounds(0, 0, getWidth(), getHeight());
            disc.draw(canvas);

            float box = size * 0.40f;
            float left = centreX - box / 2f;
            float top = centreY - box / 2f;
            float right = centreX + box / 2f;
            float bottom = centreY + box / 2f;
            glyph.reset();
            if (playing) {
                float barW = box * 0.30f;
                float gap = box * 0.20f;
                float cx = centreX;
                glyph.addRoundRect(new RectF(cx - gap / 2f - barW, top,
                        cx - gap / 2f, bottom), barW * 0.35f, barW * 0.35f,
                        Path.Direction.CW);
                glyph.addRoundRect(new RectF(cx + gap / 2f, top,
                        cx + gap / 2f + barW, bottom), barW * 0.35f, barW * 0.35f,
                        Path.Direction.CW);
            } else {
                glyph.moveTo(left + box * 0.10f, top);
                glyph.lineTo(right, centreY);
                glyph.lineTo(left + box * 0.10f, bottom);
                glyph.close();
            }
            canvas.drawPath(glyph, paint);
        }
    }

    /**
     * The player's icons, drawn rather than shipped.
     *
     * <p>Every control on this page used to be a word — 全屏、填充、−10s、+10s —
     * which is why the bar read like a settings list instead of a transport.
     * They are drawn here for the same reason the play/pause button is: the
     * app ships no image assets, the bundled pages are under a CSP that
     * forbids external ones, and a drawable that derives its geometry from its
     * bounds is correct at any density without a per-bucket file.
     */
    private static final class PlayerGlyph extends Drawable {

        static final int BACK = 0;
        static final int EXPAND = 1;
        static final int COLLAPSE = 2;
        static final int FILL = 3;
        static final int FIT = 4;
        static final int REWIND = 5;
        static final int FORWARD = 6;
        static final int RENAME = 7;
        static final int DELETE = 8;
        static final int OPEN = 9;
        static final int MOON = 10;
        /** Previous/next track bars: the platform's universal shape, drawn
         *  small so the ±10s arcs still read as the edge controls. */
        static final int PREV = 11;
        static final int NEXT = 12;
        static final int QUEUE = 14;

        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final int kind;
        private final int size;
        private final String badge;

        PlayerGlyph(int kind, int size, String badge) {
            this(kind, size, badge, Color.WHITE);
        }

        /**
         * A tinted glyph. Every plate in this page is the same neutral glass, so
         * the only way left to say "this one deletes your file" without a word
         * is to colour the drawing itself.
         */
        PlayerGlyph(int kind, int size, String badge, int color) {
            this.kind = kind;
            this.size = size;
            this.badge = badge == null ? "" : badge;
            paint.setColor(color);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeCap(Paint.Cap.ROUND);
            paint.setStrokeJoin(Paint.Join.ROUND);
        }

        @Override public int getIntrinsicWidth() { return size; }
        @Override public int getIntrinsicHeight() { return size; }

        @Override
        public void draw(Canvas canvas) {
            Rect b = getBounds();
            float w = Math.min(size, b.width());
            float h = Math.min(size, b.height());
            if (w <= 0f || h <= 0f) return;
            float cx = b.exactCenterX();
            float cy = b.exactCenterY();
            float s = Math.min(w, h);
            float l = cx - s / 2f;
            float t = cy - s / 2f;
            float r = cx + s / 2f;
            float bo = cy + s / 2f;
            paint.setStrokeWidth(Math.max(1.5f, s * 0.085f));
            paint.setStyle(Paint.Style.STROKE);

            switch (kind) {
                case BACK:
                    canvas.drawLine(cx + s * 0.12f, cy - s * 0.24f, cx - s * 0.14f, cy, paint);
                    canvas.drawLine(cx - s * 0.14f, cy, cx + s * 0.12f, cy + s * 0.24f, paint);
                    break;
                case EXPAND:
                case COLLAPSE: {
                    float inner = s * 0.08f;
                    float outer = s * 0.40f;
                    boolean out = kind == EXPAND;
                    arrow(canvas, cx - inner, cy - inner, cx - outer, cy - outer, s, out);
                    arrow(canvas, cx + inner, cy - inner, cx + outer, cy - outer, s, out);
                    arrow(canvas, cx - inner, cy + inner, cx - outer, cy + outer, s, out);
                    arrow(canvas, cx + inner, cy + inner, cx + outer, cy + outer, s, out);
                    break;
                }
                case FILL:
                case FIT: {
                    // A wide frame crossed by a horizontal double-headed
                    // arrow — the shape reads as "width", which 填充 is about,
                    // and it cannot be confused with the four corners of
                    // 全屏 beside it (the old two-diagonal-arrows version
                    // looked like a subset of exactly that). 填充 pushes the
                    // arrow past an open frame; 恢复 cages it inside a
                    // closed one.
                    float fr = s * 0.30f;
                    float top = cy - fr;
                    float bot = cy + fr;
                    float hx = s * 0.20f;
                    canvas.drawLine(cx - hx, top, cx + hx, top, paint);
                    canvas.drawLine(cx - hx, bot, cx + hx, bot, paint);
                    if (kind == FIT) {
                        canvas.drawLine(cx - hx, top, cx - hx, bot, paint);
                        canvas.drawLine(cx + hx, top, cx + hx, bot, paint);
                    }
                    float ax0 = kind == FILL ? cx - s * 0.34f : cx - hx;
                    float ax1 = kind == FILL ? cx + s * 0.34f : cx + hx;
                    canvas.drawLine(ax0, cy, ax1, cy, paint);
                    head(canvas, ax0 + s * 0.12f, cy, ax0, cy, s);
                    head(canvas, ax1 - s * 0.12f, cy, ax1, cy, s);
                    break;
                }
                case REWIND:
                case FORWARD: {
                    float rad = s * 0.30f;
                    RectF oval = new RectF(cx - rad, cy - rad, cx + rad, cy + rad);
                    canvas.drawArc(oval, -60f, 300f, false, paint);
                    double deg = (kind == REWIND) ? -60.0 : 240.0;
                    double radA = Math.toRadians(deg);
                    float px = (float) (cx + rad * Math.cos(radA));
                    float py = (float) (cy + rad * Math.sin(radA));
                    // Tangent of travel: rewind runs back down the sweep.
                    float tx = (float) ((kind == REWIND ? 1 : -1) * Math.sin(radA));
                    float ty = (float) ((kind == REWIND ? -1 : 1) * Math.cos(radA));
                    float head = s * 0.15f;
                    canvas.drawLine(px - tx * head, py - ty * head, px, py, paint);
                    head(canvas, px - tx * head, py - ty * head, px, py, s);
                    if (!badge.isEmpty()) {
                        paint.setStyle(Paint.Style.FILL);
                        paint.setTextAlign(Paint.Align.CENTER);
                        paint.setTextSize(s * 0.32f);
                        android.graphics.Paint.FontMetrics fm = paint.getFontMetrics();
                        canvas.drawText(badge, cx, cy - (fm.ascent + fm.descent) / 2f, paint);
                    }
                    break;
                }
                case RENAME:
                    canvas.drawLine(l + s * 0.20f, bo - s * 0.20f, r - s * 0.26f, t + s * 0.26f, paint);
                    canvas.drawLine(r - s * 0.26f, t + s * 0.26f, r - s * 0.26f, t + s * 0.44f, paint);
                    canvas.drawLine(r - s * 0.26f, t + s * 0.26f, r - s * 0.44f, t + s * 0.26f, paint);
                    canvas.drawLine(l + s * 0.20f, bo - s * 0.20f, l + s * 0.38f, bo - s * 0.20f, paint);
                    break;
                case DELETE:
                    canvas.drawLine(cx - s * 0.10f, t + s * 0.16f, cx + s * 0.10f, t + s * 0.16f, paint);
                    canvas.drawLine(l + s * 0.18f, t + s * 0.28f, r - s * 0.18f, t + s * 0.28f, paint);
                    canvas.drawLine(l + s * 0.26f, t + s * 0.36f, l + s * 0.30f, bo - s * 0.18f, paint);
                    canvas.drawLine(r - s * 0.26f, t + s * 0.36f, r - s * 0.30f, bo - s * 0.18f, paint);
                    canvas.drawLine(l + s * 0.30f, bo - s * 0.18f, r - s * 0.30f, bo - s * 0.18f, paint);
                    canvas.drawLine(cx - s * 0.06f, t + s * 0.44f, cx - s * 0.04f, bo - s * 0.28f, paint);
                    canvas.drawLine(cx + s * 0.06f, t + s * 0.44f, cx + s * 0.04f, bo - s * 0.28f, paint);
                    break;
                case MOON: {
                    // Crescent: one filled disc with a second disc cut out of
                    // it, which is the only way to draw a moon with a stroke
                    // paint and no path arithmetic.
                    float rad = s * 0.34f;
                    canvas.save();
                    Path outer = new Path();
                    outer.addCircle(cx, cy, rad, Path.Direction.CW);
                    Path inner = new Path();
                    inner.addCircle(cx + rad * 0.62f, cy - rad * 0.34f, rad * 0.92f,
                            Path.Direction.CCW);
                    outer.op(inner, Path.Op.DIFFERENCE);
                    paint.setStyle(Paint.Style.FILL);
                    canvas.drawPath(outer, paint);
                    paint.setStyle(Paint.Style.STROKE);
                    canvas.restore();
                    break;
                }
                case OPEN:
                    canvas.drawLine(l + s * 0.16f, t + s * 0.30f, l + s * 0.16f, bo - s * 0.16f, paint);
                    canvas.drawLine(l + s * 0.16f, bo - s * 0.16f, r - s * 0.30f, bo - s * 0.16f, paint);
                    canvas.drawLine(r - s * 0.30f, bo - s * 0.16f, r - s * 0.30f, t + s * 0.44f, paint);
                    canvas.drawLine(r - s * 0.30f, t + s * 0.44f, r - s * 0.46f, t + s * 0.44f, paint);
                    canvas.drawLine(l + s * 0.16f, t + s * 0.30f, l + s * 0.34f, t + s * 0.30f, paint);
                    arrow(canvas, cx - s * 0.02f, cy + s * 0.02f, r - s * 0.10f, t + s * 0.10f, s, true);
                    break;
                case PREV:
                case NEXT: {
                    // The universal double-triangle-in-a-bar: sharp enough to
                    // read at 22dp, simpler than a chevron stack that would
                    // fight the ±10s arcs beside it.
                    boolean forward = kind == NEXT;
                    float triL = forward ? l + s * 0.22f : r - s * 0.22f;
                    float triR = forward ? r - s * 0.22f : l + s * 0.22f;
                    float barX = forward ? r - s * 0.10f : l + s * 0.10f;
                    Path tri = new Path();
                    tri.moveTo(triL, cy - s * 0.20f);
                    tri.lineTo(triR, cy);
                    tri.lineTo(triL, cy + s * 0.20f);
                    tri.close();
                    paint.setStyle(Paint.Style.FILL);
                    canvas.drawPath(tri, paint);
                    paint.setStyle(Paint.Style.STROKE);
                    canvas.drawLine(barX, t + s * 0.28f, barX, bo - s * 0.28f, paint);
                    break;
                }
                case QUEUE: {
                    // Three rows of dot + line — the shape every list icon on
                    // the platform already taught.
                    float dotX = l + s * 0.22f;
                    float lineX0 = l + s * 0.40f;
                    float lineX1 = r - s * 0.18f;
                    float ys[] = new float[]{cy - s * 0.20f, cy, cy + s * 0.20f};
                    paint.setStyle(Paint.Style.FILL);
                    for (float yy : ys) {
                        canvas.drawCircle(dotX, yy, paint.getStrokeWidth() * 0.7f, paint);
                    }
                    paint.setStyle(Paint.Style.STROKE);
                    for (float yy : ys) {
                        canvas.drawLine(lineX0, yy, lineX1, yy, paint);
                    }
                    break;
                }
                default:
                    break;
            }
        }

        private void arrow(Canvas canvas, float x1, float y1, float x2, float y2,
                           float s, boolean outward) {
            if (outward) canvas.drawLine(x1, y1, x2, y2, paint);
            else canvas.drawLine(x2, y2, x1, y1, paint);
            head(canvas, x1, y1, x2, y2, s);
        }

        private void head(Canvas canvas, float x1, float y1, float x2, float y2, float s) {
            double ang = Math.atan2(y2 - y1, x2 - x1);
            float len = s * 0.15f;
            float spread = 0.45f;
            canvas.drawLine(x2, y2,
                    x2 - len * (float) Math.cos(ang - spread),
                    y2 - len * (float) Math.sin(ang - spread), paint);
            canvas.drawLine(x2, y2,
                    x2 - len * (float) Math.cos(ang + spread),
                    y2 - len * (float) Math.sin(ang + spread), paint);
        }

        @Override public void setAlpha(int alpha) { paint.setAlpha(alpha); }
        @Override public void setColorFilter(ColorFilter filter) { paint.setColorFilter(filter); }
        @Override public int getOpacity() { return PixelFormat.TRANSLUCENT; }
    }

    /**
     * The busy indicator. The stock indeterminate {@code ProgressBar} is a
     * themed widget — it arrives in the platform's accent colour, which is the
     * one hue this app never uses. An arc that rotates from the uptime clock
     * costs one view and no animator, and it matches the accent gradient the
     * rest of the chrome is built from.
     */
    private static final class LoadingRing extends View {

        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final RectF box = new RectF();
        private boolean busy;

        LoadingRing(Context context) {
            super(context);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeCap(Paint.Cap.ROUND);
            setVisibility(GONE);
        }

        void setBusy(boolean busy) {
            if (this.busy == busy) return;
            this.busy = busy;
            setVisibility(busy ? VISIBLE : GONE);
            if (busy) postInvalidateOnAnimation();
        }

        @Override
        protected void onDraw(Canvas canvas) {
            if (!busy) return;
            float w = getWidth();
            float h = getHeight();
            if (w <= 0f || h <= 0f) return;
            float stroke = Math.max(2f, Math.min(w, h) * 0.09f);
            float inset = stroke / 2f + Math.min(w, h) * 0.04f;
            box.set(inset, inset, w - inset, h - inset);
            paint.setStrokeWidth(stroke);
            paint.setColor(Color.argb(60, 255, 255, 255));
            canvas.drawArc(box, 0f, 360f, false, paint);
            paint.setShader(new android.graphics.SweepGradient(
                    w / 2f, h / 2f, ACCENT_B, ACCENT_A));
            paint.setColor(ACCENT_A);
            float start = (SystemClock.uptimeMillis() % 1100L) / 1100f * 360f;
            canvas.drawArc(box, start, 108f, false, paint);
            paint.setShader(null);
            postInvalidateOnAnimation();
        }
    }
}
