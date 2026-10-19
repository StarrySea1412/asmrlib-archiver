package com.asmrlib.archiver;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

/**
 * The keep-alive half of 息屏播放 on the ONLINE player.
 *
 * <p>The online player lives in a cross-origin WebView iframe. Its audio is
 * decoded inside the WebView's own media stack, and nothing outside can
 * reach it: no position to read, no stream URL to re-open, no transport to
 * drive — the previous hand-off attempt (starting
 * {@link PlaybackService} on the sniffed URL) restarted the track from
 * zero, which is the bug the user rejected. What actually ends playback
 * when the screen goes dark is not the WebView stopping — it is the
 * <em>process</em> being frozen or the <em>CPU</em> dozing. So this service
 * does neither of the things a player service does; it holds the two
 * resources the WebView needs to keep decoding:
 *
 * <ol>
 *   <li>a foreground-service promotion — the process is exempt from the
 *       cached-process freeze while the activity is behind the lock
 *       screen;</li>
 *   <li>a {@code PARTIAL_WAKE_LOCK} — the CPU keeps feeding the decoder
 *       after the display is off.</li>
 * </ol>
 *
 * <p>The WebView is deliberately never paused (no {@code onPause()} on it,
 * no surface teardown), so when the user wakes the screen the picture is
 * exactly where the audio is: playback never stopped, from the page's
 * point of view nothing happened at all.
 *
 * <p>Started automatically when the user leaves our play page for a trusted
 * external player ({@code MainActivity} wires it in
 * {@code shouldOverrideUrlLoading}), and stopped on any navigation back to
 * a bundled page. It owns no media objects and holds no position — stop it
 * freely; nothing is lost.
 */
public class WebAudioService extends Service {

    private static final String TAG = "asmrlib-webaudio";
    private static final String CHANNEL_ID = "webplayback";
    private static final int NOTIFICATION_ID = 54000;

    private static final String EXTRA_TITLE = "title";
    static final String ACTION_STOP = "com.asmrlib.archiver.WEBAUDIO_STOP";

    private static volatile PowerManager.WakeLock wakeLock;

    /** Start the keep-alive. Safe to call repeatedly: the service re-runs
     *  {@code onStartCommand}, which just refreshes the notification. */
    static void start(Context context, String title) {
        Intent intent = new Intent(context, WebAudioService.class);
        intent.putExtra(EXTRA_TITLE, title == null ? "" : title);
        if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent);
        else context.startService(intent);
    }

    static void stop(Context context) {
        context.stopService(new Intent(context, WebAudioService.class));
    }

    @Override
    public void onCreate() {
        super.onCreate();
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            PowerManager.WakeLock lock =
                    pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "asmrlib:web-audio");
            lock.setReferenceCounted(false);
            lock.acquire(4 * 60 * 60 * 1000L);
            wakeLock = lock;
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }
        String title = intent == null ? "" : intent.getStringExtra(EXTRA_TITLE);
        ensureChannel();
        // The notification must exist before startForeground, or Android 12+
        // kills the promotion with an opaque "did not call startForeground".
        Notification notification = buildNotification(title == null ? "" : title);
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIFICATION_ID, notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
        return START_NOT_STICKY;
    }

    private void ensureChannel() {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager mgr = getSystemService(NotificationManager.class);
        if (mgr == null || mgr.getNotificationChannel(CHANNEL_ID) != null) return;
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID, "在线播放", NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("息屏后网页视频继续播放");
        channel.setSound(null, null);
        channel.enableVibration(false);
        mgr.createNotificationChannel(channel);
    }

    private Notification buildNotification(String title) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        b.setContentTitle(title.isEmpty() ? "在线播放中" : title)
                .setContentText("息屏播放中 · 回到应用查看画面")
                .setSmallIcon(android.R.drawable.ic_media_play)
                .setOngoing(true)
                .setVisibility(Notification.VISIBILITY_PUBLIC);

        Intent open = new Intent(this, MainActivity.class);
        open.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int piFlags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        b.setContentIntent(PendingIntent.getActivity(this, 5401, open, piFlags));

        b.addAction(new Notification.Action.Builder(
                        android.R.drawable.ic_menu_close_clear_cancel,
                        "停止",
                        serviceIntent(ACTION_STOP, 5402))
                .build());
        return b.build();
    }

    private PendingIntent serviceIntent(String action, int requestCode) {
        Intent i = new Intent(this, WebAudioService.class).setAction(action);
        return PendingIntent.getService(this, requestCode, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    @Override
    public void onDestroy() {
        PowerManager.WakeLock lock = wakeLock;
        wakeLock = null;
        if (lock != null && lock.isHeld()) {
            try {
                lock.release();
            } catch (Exception e) {
                Log.w(TAG, "wake lock release failed", e);
            }
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
