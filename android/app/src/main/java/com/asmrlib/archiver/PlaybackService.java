package com.asmrlib.archiver;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.media.MediaMetadata;
import android.media.MediaPlayer;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.util.Log;

/**
 * Audio-only playback that survives the screen going dark.
 *
 * <p>There is no platform API for "keep decoding video with the screen off".
 * Turning the screen off destroys the {@code SurfaceView}'s surface, and a
 * MediaPlayer whose display has gone away stops — deliberately, because a
 * device decoding H.264 for nobody is a battery fire. Video cannot continue
 * and no permission will change that.
 *
 * <p>Audio can, and the platform's answer is three pieces that have to be used
 * together:
 *
 * <ol>
 *   <li>{@link MediaPlayer#setWakeMode(Context, int)} with
 *       {@code PARTIAL_WAKE_LOCK} — screen off, CPU awake, decoder still fed.
 *       Without this the device dozes mid-sentence and the audio stalls.</li>
 *   <li>A {@code mediaPlayback} foreground service — the only way a process
 *       is allowed to keep running, and stay un-killed, once the activity is
 *       no longer visible. Its notification is also what makes the playback
 *       visible to the user instead of mysterious.</li>
 *   <li>A {@link MediaSession} — publishes the transport to the lock screen,
 *       headset buttons, Bluetooth and Android Auto, so 息屏播放 is actually
 *       controllable while the screen is off.</li>
 * </ol>
 *
 * <p>The service never holds a surface: it decodes the audio track only. That
 * is the honest shape of the feature, and for this library — ASMR, which is
 * mostly something you listen to with your eyes closed — it is the point.
 */
public class PlaybackService extends Service
        implements MediaPlayer.OnPreparedListener,
                   MediaPlayer.OnCompletionListener,
                   MediaPlayer.OnErrorListener,
                   AudioManager.OnAudioFocusChangeListener {

    private static final String TAG = "asmrlib-playback";
    private static final String CHANNEL_ID = "playback";
    private static final int NOTIFICATION_ID = 52000;

    private static final String EXTRA_URI = "uri";
    private static final String EXTRA_TITLE = "title";
    private static final String EXTRA_POSITION = "position";
    /** The identity the resume record hangs off — the player page computed
     *  it (task id or remote URL); the service keeps writing THAT record so
     *  the page finds it on the next open. */
    private static final String EXTRA_RESUME_KEY = "resume_key";
    /** The track queue behind 息屏连播: a JSON array of
     *  {@code {"u":url,"l":label,"k":kind,"r":referer}} plus the index this
     *  service starts at. Without it the service stops after one track and
     *  Eara's continuous playback quietly ends the moment the screen goes
     *  dark — the form ASMR is mostly used in. */
    private static final String EXTRA_QUEUE = "queue";
    private static final String EXTRA_QUEUE_INDEX = "queue_index";
    private static final String ACTION_TOGGLE = "com.asmrlib.archiver.PLAYBACK_TOGGLE";
    private static final String ACTION_STOP = "com.asmrlib.archiver.PLAYBACK_STOP";

    /** Same process as the activities, so a live instance is authoritative. */
    private static volatile PlaybackService instance;

    private MediaPlayer player;
    private MediaSession session;
    private AudioManager audio;
    private AudioFocusRequest focusRequest;
    private boolean hasFocus;
    private boolean playing;
    private boolean prepared;
    /** The service holds one {@link StreamProxy} reference while it plays a
     *  proxied stream; released in onDestroy so the loopback server can stop. */
    private boolean holdingProxy;
    private int startPositionMs;
    private String title = "";
    /** The media the service is playing, so the notification's tap target
     *  can hand the player back its own file. */
    private String playingUri = "";
    /** Where to keep writing the resume record (the page's own key), or
     *  empty for a handover that carries none. */
    private String resumeKey = "";
    /** 息屏连播: the work's track list and where in it this service sits. */
    private org.json.JSONArray queue;
    private int queueIndex;
    /** Seconds of playback since the last resume write — the store is touched
     *  on a 10-second cadence, fast enough that a killed process leaves a
     *  fresh place, slow enough to never matter next to decoding. */
    private int resumeCountdown;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    /**
     * Re-publish the position every second.
     *
     * <p>Without it the lock screen shows the position from the last state
     * change — a frozen scrubber on a track that is audibly playing, which
     * reads as "the app is stuck". Cheap: one PlaybackState object per second,
     * no binder traffic until something actually reads it.
     */
    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            if (!playing || player == null) return;
            publishState();
            // The screen-off session is the one nobody watches: writing the
            // place on a cadence means a process the system reaped mid-work
            // still hands the user back to (roughly) where they were, not
            // to where the handover happened — which can be an hour earlier.
            if (++resumeCountdown >= 10) {
                resumeCountdown = 0;
                saveResume();
            }
            mainHandler.postDelayed(this, 1000L);
        }
    };

    /** Headphones pulled out: keep playing into the room is never what anyone
     *  wants from an ASMR track, so this is a hard pause. */
    private final BroadcastReceiver noisy = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (AudioManager.ACTION_AUDIO_BECOMING_NOISY.equals(intent.getAction())) pause();
        }
    };

    /** Start audio-only playback. Safe to call with the screen about to go
     *  off: the wake lock is taken by the player as soon as it starts. The
     *  queue rides along when the page was playing one (息屏连播), and the
     *  resume key is the record the page already writes — the service keeps
     *  THAT record fresh instead of forking a second one. */
    static void start(Context context, String uri, String title, int positionMs,
            String resumeKey, String queueJson, int queueIndex) {
        Intent intent = new Intent(context, PlaybackService.class);
        intent.putExtra(EXTRA_URI, uri);
        intent.putExtra(EXTRA_TITLE, title == null ? "" : title);
        intent.putExtra(EXTRA_POSITION, positionMs);
        intent.putExtra(EXTRA_RESUME_KEY, resumeKey == null ? "" : resumeKey);
        intent.putExtra(EXTRA_QUEUE, queueJson == null ? "" : queueJson);
        intent.putExtra(EXTRA_QUEUE_INDEX, queueIndex);
        if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent);
        else context.startService(intent);
    }

    /** True while the service owns playback. The activity uses this to decide
     *  whether to start its own player or offer to take the audio back. */
    static boolean isRunning() {
        PlaybackService s = instance;
        return s != null && s.player != null;
    }

    /** Live position, so returning to the screen picks up where the audio is. */
    static int positionMs() {
        PlaybackService s = instance;
        if (s == null || s.player == null || !s.prepared) return 0;
        try {
            return s.player.getCurrentPosition();
        } catch (Exception e) {
            return 0;
        }
    }

    static void stop(Context context) {
        context.stopService(new Intent(context, PlaybackService.class));
    }

    @Override
    public void onCreate() {
        super.onCreate();
        audio = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        registerReceiver(noisy, new IntentFilter(AudioManager.ACTION_AUDIO_BECOMING_NOISY));
        instance = this;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (ACTION_TOGGLE.equals(intent.getAction())) {
            if (playing) pause();
            else resume();
            return START_NOT_STICKY;
        }
        if (ACTION_STOP.equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }
        String uriString = intent.getStringExtra(EXTRA_URI);
        title = intent.getStringExtra(EXTRA_TITLE);
        if (title == null) title = "";
        startPositionMs = intent.getIntExtra(EXTRA_POSITION, 0);
        resumeKey = intent.getStringExtra(EXTRA_RESUME_KEY);
        if (resumeKey == null) resumeKey = "";
        resumeCountdown = 0;
        String queueJson = intent.getStringExtra(EXTRA_QUEUE);
        queue = null;
        queueIndex = 0;
        if (queueJson != null && !queueJson.trim().isEmpty()) {
            try {
                queue = new org.json.JSONArray(queueJson);
                queueIndex = Math.max(0,
                        Math.min(intent.getIntExtra(EXTRA_QUEUE_INDEX, 0),
                                queue.length() - 1));
            } catch (Exception e) {
                Log.w(TAG, "sleep queue unparsable, playing one track", e);
                queue = null;
            }
        }
        if (uriString == null || uriString.isEmpty()) {
            stopSelf();
            return START_NOT_STICKY;
        }
        startPlayback(Uri.parse(uriString), startPositionMs);
        return START_NOT_STICKY;
    }

    private void startPlayback(Uri uri, int positionMs) {
        startPositionMs = positionMs;
        playingUri = uri == null ? "" : uri.toString();
        ensureChannel();
        // The notification has to exist before the service is promoted, or
        // Android 12+ kills the promotion and logcats a very unhelpful
        // "startForegroundService did not call startForeground".
        startAsForeground();
        releasePlayer();
        // A proxied stream URL means the activity that registered it has (or
        // is about to) drop its own reference; this service now owns one, so
        // the loopback server lives as long as the audio does. Exactly one,
        // though: a queue advance re-enters here with the reference already
        // held, and a second retain would leak it — while a switch from a
        // proxied source to a local file must let it go.
        boolean ours = StreamProxy.isOurs(playingUri);
        if (holdingProxy && !ours) {
            holdingProxy = false;
            StreamProxy.release();
        }
        if (ours && !holdingProxy) {
            try {
                StreamProxy.retain();
                holdingProxy = true;
            } catch (Exception e) {
                Log.w(TAG, "stream proxy retain failed", e);
            }
        }
        try {
            player = new MediaPlayer();
            player.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build());
            // The point of the whole service: hold the CPU awake while the
            // screen is off, released automatically on pause/stop.
            player.setWakeMode(this, PowerManager.PARTIAL_WAKE_LOCK);
            player.setDataSource(this, uri);
            player.setOnPreparedListener(this);
            player.setOnCompletionListener(this);
            player.setOnErrorListener(this);
            player.prepareAsync();
        } catch (Exception e) {
            Log.w(TAG, "audio-only playback could not start", e);
            stopSelf();
            return;
        }
        ensureSession();
        requestFocus();
    }

    private void startAsForeground() {
        Notification notification = buildNotification(false);
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIFICATION_ID, notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
    }

    private void ensureChannel() {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager mgr = getSystemService(NotificationManager.class);
        if (mgr == null || mgr.getNotificationChannel(CHANNEL_ID) != null) return;
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID, "后台播放", NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("息屏后继续播放音频");
        channel.setSound(null, null);
        channel.enableVibration(false);
        mgr.createNotificationChannel(channel);
    }

    /**
     * Publishing a session is what makes 息屏播放 usable rather than merely
     * possible: without it the lock screen, the headset button and every
     * Bluetooth device have nothing to talk to.
     */
    private void ensureSession() {
        if (session != null) return;
        session = new MediaSession(this, "asmrlib-playback");
        session.setFlags(MediaSession.FLAG_HANDLES_MEDIA_BUTTONS
                | MediaSession.FLAG_HANDLES_TRANSPORT_CONTROLS);
        session.setCallback(new MediaSession.Callback() {
            @Override public void onPlay() { resume(); }
            @Override public void onPause() { pause(); }
            @Override public void onStop() { stopSelf(); }
            @Override public void onSeekTo(long pos) { seek((int) pos); }
        });
        session.setActive(true);
        updateSessionMetadata();
    }

    /**
     * Publish title and duration for the track playing now. Duration starts
     * unknown and lands once the extractor answers; a queue advance calls
     * this again through {@link #onPrepared}, so the lock screen never keeps
     * describing the previous track.
     */
    private void updateSessionMetadata() {
        if (session == null) return;
        long duration = 0L;
        if (player != null && prepared) {
            try {
                duration = Math.max(0L, player.getDuration());
            } catch (Exception ignored) {
                // An unprepared player answers -1; the placeholder stands.
            }
        }
        MediaMetadata.Builder meta = new MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE,
                        title.isEmpty() ? "ASMR 音频" : title)
                .putLong(MediaMetadata.METADATA_KEY_DURATION, duration);
        session.setMetadata(meta.build());
    }

    // ----------------------------------------------------------- resume store

    /** The shell's one preferences file — the same one the player page writes. */
    private static final String PREFS = "asmrlib";

    /**
     * Write this track's place into the record the page handed over, or
     * forget it when this track is at or past its end. Empty key = a
     * handover that carries none (nothing to keep, nothing to clobber).
     */
    private void saveResume() {
        if (resumeKey.isEmpty() || player == null || !prepared) return;
        int position;
        int duration;
        try {
            position = player.getCurrentPosition();
            duration = player.getDuration();
        } catch (Exception e) {
            return;
        }
        if (PlaybackResume.shouldStore(position, duration)) {
            ResumeStore.store(getSharedPreferences(PREFS, MODE_PRIVATE),
                    resumeKey, position, duration);
        } else {
            ResumeStore.drop(getSharedPreferences(PREFS, MODE_PRIVATE), resumeKey);
        }
    }

    /** Forget this track's place outright — it played to its end. */
    private void clearResume() {
        if (resumeKey.isEmpty()) return;
        ResumeStore.drop(getSharedPreferences(PREFS, MODE_PRIVATE), resumeKey);
    }

    private void requestFocus() {
        if (audio == null || hasFocus) return;
        if (Build.VERSION.SDK_INT >= 26) {
            focusRequest = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                            .build())
                    .setOnAudioFocusChangeListener(this, mainHandler)
                    .build();
            hasFocus = audio.requestAudioFocus(focusRequest)
                    == AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
        } else {
            hasFocus = audio.requestAudioFocus(this, AudioManager.STREAM_MUSIC,
                    AudioManager.AUDIOFOCUS_GAIN)
                    == AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
        }
    }

    private void abandonFocus() {
        if (audio == null || !hasFocus) return;
        if (Build.VERSION.SDK_INT >= 26 && focusRequest != null) {
            audio.abandonAudioFocusRequest(focusRequest);
        } else {
            audio.abandonAudioFocus(this);
        }
        hasFocus = false;
    }

    @Override
    public void onPrepared(MediaPlayer mp) {
        prepared = true;
        if (startPositionMs > 0) {
            try {
                mp.seekTo(startPositionMs);
            } catch (Exception e) {
                Log.w(TAG, "resume seek failed", e);
            }
        }
        mp.start();
        playing = true;
        publishState();
        // The lock screen's scrubber divides position by THIS duration: left
        // at the placeholder zero it showed a frozen knob at the far left of
        // a track that was audibly halfway through.
        updateSessionMetadata();
        notifyState();
        mainHandler.removeCallbacks(tick);
        mainHandler.postDelayed(tick, 1000L);
    }

    @Override
    public void onCompletion(MediaPlayer mp) {
        playing = false;
        publishState();
        notifyState();
        // Watched to the end, so this track's place is spent — the same rule
        // the player page applies, and the reason a finished track never
        // drops the user into its closing seconds on the next open.
        clearResume();
        // 息屏连播: step to the next track of the queue in place — same
        // service, same proxy, same notification. A queue that ran out (or
        // never existed) stops as before.
        if (advanceQueue()) return;
        stopSelf();
    }

    /**
     * The service's twin of the player page's queue walk: the next playable
     * entry replaces this one, its route registered on the shared proxy, its
     * title on the notification, its resume key recomputed per track. A dead
     * entry is skipped rather than allowed to strand the rest of the work.
     */
    private boolean advanceQueue() {
        if (queue == null) return false;
        for (int next = queueIndex + 1; next < queue.length(); next++) {
            org.json.JSONObject entry = queue.optJSONObject(next);
            if (entry == null) continue;
            String url = entry.optString("u", "").trim();
            if (url.isEmpty()) continue;
            String kind = entry.optString("k", "direct").trim();
            if (!"hls".equals(kind) && !"direct".equals(kind)) continue;
            String referer = entry.optString("r", "").trim();
            String proxied;
            try {
                proxied = StreamProxy.register(url, referer);
            } catch (Exception e) {
                Log.w(TAG, "sleep queue advance: registration failed for "
                        + url, e);
                continue;
            }
            queueIndex = next;
            resumeKey = PlaybackResume.keyFor("", url);
            resumeCountdown = 0;
            title = entry.optString("l", "").trim();
            if (title.isEmpty()) title = "第 " + (next + 1) + " 轨";
            startPlayback(Uri.parse(proxied), 0);
            return true;
        }
        return false;
    }

    @Override
    public boolean onError(MediaPlayer mp, int what, int extra) {
        Log.w(TAG, "audio-only playback error what=" + what + " extra=" + extra);
        playing = false;
        publishState();
        notifyState();
        stopSelf();
        return true;
    }

    private void pause() {
        if (player == null || !prepared) return;
        try {
            if (player.isPlaying()) player.pause();
        } catch (Exception e) {
            Log.w(TAG, "pause failed", e);
        }
        playing = false;
        mainHandler.removeCallbacks(tick);
        // The cadence write can be up to ten seconds behind; a pause is the
        // moment the user actually chose, so it is written as it happened.
        saveResume();
        publishState();
        notifyState();
    }

    private void resume() {
        if (player == null || !prepared) return;
        requestFocus();
        try {
            player.start();
        } catch (Exception e) {
            Log.w(TAG, "resume failed", e);
            return;
        }
        playing = true;
        publishState();
        notifyState();
        mainHandler.removeCallbacks(tick);
        mainHandler.postDelayed(tick, 1000L);
    }

    private void seek(int position) {
        if (player == null || !prepared) return;
        try {
            player.seekTo(position);
        } catch (Exception e) {
            Log.w(TAG, "seek failed", e);
        }
        publishState();
    }

    private void publishState() {
        if (session == null) return;
        int position = positionMs();
        PlaybackState.Builder state = new PlaybackState.Builder()
                .setActions(PlaybackState.ACTION_PLAY
                        | PlaybackState.ACTION_PAUSE
                        | PlaybackState.ACTION_STOP
                        | PlaybackState.ACTION_SEEK_TO)
                .setState(playing ? PlaybackState.STATE_PLAYING
                                  : PlaybackState.STATE_PAUSED,
                        position, 1.0f, android.os.SystemClock.elapsedRealtime());
        session.setPlaybackState(state.build());
    }

    private void notifyState() {
        NotificationManager mgr = getSystemService(NotificationManager.class);
        if (mgr == null) return;
        mgr.notify(NOTIFICATION_ID, buildNotification(playing));
    }

    private Notification buildNotification(boolean isPlaying) {
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            b = new Notification.Builder(this, CHANNEL_ID);
        } else {
            b = new Notification.Builder(this);
        }
        b.setContentTitle(title.isEmpty() ? "ASMR 音频" : title)
                .setContentText(isPlaying ? "正在后台播放 · 息屏也能继续" : "后台播放已暂停")
                .setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setOngoing(isPlaying)
                .setVisibility(Notification.VISIBILITY_PUBLIC);

        // The tap target carries the same file the service is playing.
        // Without it the player opened with no media at all — 「找不到要播放
        // 的文件」 from a notification that promised the opposite — and the
        // later URI-shaped failure mode was worse still: a garbled document
        // handed to MediaPlayer. Single-top so a tap lands on the instance
        // the user already had.
        Intent open = new Intent(this, PlayerActivity.class);
        open.putExtra(PlayerActivity.EXTRA_URI, playingUri);
        open.putExtra(PlayerActivity.EXTRA_TITLE, title);
        // The stream's real identity rides along: with the original URL the
        // page registers its own (balanced) proxy reference, derives audio
        // mode from the actual extension, and — with the queue — keeps Eara's
        // continuous playback alive after 息屏播放 handed this track over.
        org.json.JSONObject current = queue == null ? null
                : queue.optJSONObject(queueIndex);
        String currentUrl = current == null
                ? "" : current.optString("u", "").trim();
        if (!currentUrl.isEmpty()) {
            open.putExtra(PlayerActivity.EXTRA_REMOTE_URL, currentUrl);
            String kind = current.optString("k", "direct").trim();
            open.putExtra(PlayerActivity.EXTRA_REMOTE_KIND,
                    "hls".equals(kind) ? "hls" : "direct");
        }
        if (queue != null && queue.length() > 1) {
            open.putExtra(PlayerActivity.EXTRA_REMOTE_QUEUE, queue.toString());
            open.putExtra(PlayerActivity.EXTRA_REMOTE_INDEX, queueIndex);
        }
        open.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int piFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) piFlags |= PendingIntent.FLAG_IMMUTABLE;
        b.setContentIntent(PendingIntent.getActivity(this, 5301, open, piFlags));

        b.addAction(new Notification.Action.Builder(
                        android.R.drawable.ic_media_pause,
                        isPlaying ? "暂停" : "播放",
                        serviceIntent(ACTION_TOGGLE, 5302))
                .build());
        b.addAction(new Notification.Action.Builder(
                        android.R.drawable.ic_menu_close_clear_cancel,
                        "停止",
                        serviceIntent(ACTION_STOP, 5303))
                .build());
        // The session token is what turns this into a lock-screen control
        // instead of a plain notification.
        if (session != null) {
            b.setStyle(new Notification.MediaStyle().setMediaSession(session.getSessionToken()));
        }
        return b.build();
    }

    private PendingIntent serviceIntent(String action, int requestCode) {
        Intent i = new Intent(this, PlaybackService.class).setAction(action);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) flags |= PendingIntent.FLAG_IMMUTABLE;
        return PendingIntent.getService(this, requestCode, i, flags);
    }

    @Override
    public void onAudioFocusChange(int focusChange) {
        switch (focusChange) {
            case AudioManager.AUDIOFOCUS_LOSS:
            case AudioManager.AUDIOFOCUS_LOSS_TRANSIENT:
                pause();
                break;
            case AudioManager.AUDIOFOCUS_GAIN:
                break;
            default:
                break;
        }
    }

    private void releasePlayer() {
        if (player != null) {
            try {
                player.release();
            } catch (Exception e) {
                Log.w(TAG, "release failed", e);
            }
            player = null;
        }
        prepared = false;
        playing = false;
    }

    @Override
    public void onDestroy() {
        mainHandler.removeCallbacks(tick);
        // Before releasePlayer: the last thing a reaped process leaves behind
        // should be as close to "where the audio actually stopped" as
        // possible, and the cadence write can be ten seconds stale. A
        // completion already cleared the record; a no-position player makes
        // this a no-op.
        saveResume();
        releasePlayer();
        abandonFocus();
        if (holdingProxy) {
            holdingProxy = false;
            StreamProxy.release();
        }
        if (session != null) {
            session.setActive(false);
            session.release();
            session = null;
        }
        try {
            unregisterReceiver(noisy);
        } catch (Exception ignored) {
            // Receiver already gone: nothing to undo.
        }
        instance = null;
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
