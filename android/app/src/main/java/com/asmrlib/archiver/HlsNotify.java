package com.asmrlib.archiver;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import java.util.HashMap;
import java.util.Map;

/**
 * The notifications an in-app HLS save shows while it is moving.
 *
 * <p>A download that only reports progress inside an app the user has left
 * might as well not report it: the user locks the screen or switches apps
 * precisely because a gigabyte takes a while, and the only surface they can
 * still see is the notification shade. One ongoing notification per task —
 * created by {@link #progress}, replaced by {@link #finished} under the same
 * id, so a completion never stacks on top of a live progress bar.
 *
 * <p>Direct {@code DownloadManager} saves already carry the system's own
 * progress notification; this class covers only what the system cannot see —
 * the in-app HLS worker.
 */
final class HlsNotify {

    /** Same channel MainActivity creates; one channel for all download states. */
    private static final String CHANNEL_ID = "downloads";
    private static final int ID_BASE = 51000;
    /** Progress redraws are throttled to one notification a second per task. */
    private static final long MIN_INTERVAL_MS = 1000L;

    private static final Map<String, Long> lastTick = new HashMap<String, Long>();

    private HlsNotify() {
    }

    private static boolean permitted(Context context) {
        return Build.VERSION.SDK_INT < 33
            || context.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                == android.content.pm.PackageManager.PERMISSION_GRANTED;
    }

    private static void ensureChannel(Context context) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager manager = notificationManager(context);
        if (manager == null) return;
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID, "下载通知", NotificationManager.IMPORTANCE_DEFAULT);
        channel.setDescription("视频下载进度与完成提示");
        manager.createNotificationChannel(channel);
    }

    private static NotificationManager notificationManager(Context context) {
        return (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
    }

    /** Same id rule MainActivity used, so an ongoing bar and its completion
     *  outcome collide on purpose: the latter replaces the former. */
    private static int id(String taskId) {
        return ID_BASE + Math.abs((taskId == null ? "" : taskId).hashCode() % 10000);
    }

    private static PendingIntent contentIntent(Context context) {
        Intent open = new Intent(context, DownloadsActivity.class);
        open.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int req = "asmrlib-hls-notify".hashCode();
        return PendingIntent.getActivity(context, req, open,
                Build.VERSION.SDK_INT >= 23
                        ? PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
                        : PendingIntent.FLAG_UPDATE_CURRENT);
    }

    /**
     * The ongoing download notification. Cheap enough to call from every
     * segment batch: at most one notification a second leaves this method per
     * task, and callers without the notification permission are free.
     */
    static void progress(Context context, String taskId, String fileName,
                         int done, int total, long bytes) {
        if (!permitted(context)) return;
        ensureChannel(context);
        long now = System.currentTimeMillis();
        Long at = lastTick.get(taskId);
        if (at != null && now - at < MIN_INTERVAL_MS) return;
        lastTick.put(taskId, now);
        Notification.Builder builder = build(context);
        builder.setSmallIcon(android.R.drawable.stat_sys_download)
                .setContentTitle("正在下载视频")
                .setContentText(text(fileName, done, total, bytes))
                .setOngoing(true)
                .setOnlyAlertOnce(true);
        // An HLS manifest may not know its segment count yet; an indeterminate
        // bar says "working" honestly instead of pretending a fraction.
        builder.setProgress(total > 0 ? total : 1, done, total <= 0);
        notificationManager(context).notify(id(taskId), builder.build());
    }

    /** The finished notification: 已完成 or 失败, replacing the ongoing bar. */
    static void finished(Context context, String taskId, String fileName, boolean ok) {
        lastTick.remove(taskId);
        if (!permitted(context)) return;
        ensureChannel(context);
        String safeName = fileName == null || fileName.isEmpty() ? "视频文件" : fileName;
        Notification.Builder builder = build(context);
        builder.setSmallIcon(ok ? android.R.drawable.stat_sys_download_done
                                : android.R.drawable.stat_notify_error)
                .setContentTitle(ok ? "视频已下载完成" : "视频下载失败")
                .setContentText(ok ? safeName + " · 点击打开下载中心"
                                    : safeName + " · 可在下载中心重试")
                .setAutoCancel(true)
                .setOnlyAlertOnce(true);
        notificationManager(context).notify(id(taskId), builder.build());
    }

    private static Notification.Builder build(Context context) {
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(context, CHANNEL_ID)
                : new Notification.Builder(context);
        builder.setContentIntent(contentIntent(context));
        return builder;
    }

    // ------------------------------------------------------------- MP4 export

    /** Export notifications live in their own id range: an export of the same
     *  file a download once wrote must never collide with that download's
     *  outcome notification. */
    private static final int EXPORT_ID_BASE = 55000;

    private static int exportId(String key) {
        return EXPORT_ID_BASE + Math.abs((key == null ? "" : key).hashCode() % 10000);
    }

    /** The ongoing export notification — the export runs whether or not the
     *  download centre stays on screen, and a gigabyte copy with no shade
     *  presence reads as "nothing happened". */
    static void exportProgress(Context context, String key, String fileName,
                               int percent, long bytes) {
        if (!permitted(context)) return;
        ensureChannel(context);
        long now = System.currentTimeMillis();
        Long at = lastTick.get(key);
        if (at != null && now - at < MIN_INTERVAL_MS) return;
        lastTick.put(key, now);
        Notification.Builder builder = build(context);
        String name = fileName == null || fileName.isEmpty() ? "视频文件" : fileName;
        builder.setSmallIcon(android.R.drawable.stat_sys_download)
                .setContentTitle("正在导出 MP4")
                .setContentText(name + " · " + percent + "% · " + MainActivity.formatBytes(bytes))
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setProgress(100, percent, false);
        notificationManager(context).notify(exportId(key), builder.build());
    }

    /** The export outcome, replacing the ongoing bar under the same id. */
    static void exportFinished(Context context, String key, String fileName, boolean ok) {
        lastTick.remove(key);
        if (!permitted(context)) return;
        ensureChannel(context);
        String name = fileName == null || fileName.isEmpty() ? "视频文件" : fileName;
        Notification.Builder builder = build(context);
        builder.setSmallIcon(ok ? android.R.drawable.stat_sys_download_done
                                : android.R.drawable.stat_notify_error)
                .setContentTitle(ok ? "MP4 已导出到 Download/" : "MP4 导出失败")
                .setContentText(ok ? name + " · 文件管理可见" : name)
                .setAutoCancel(true)
                .setOnlyAlertOnce(true);
        notificationManager(context).notify(exportId(key), builder.build());
    }

    private static String text(String fileName, int done, int total, long bytes) {
        String name = fileName == null || fileName.isEmpty() ? "视频文件" : fileName;
        String size = MainActivity.formatBytes(bytes);
        if (total > 0) {
            return name + " · " + done + " / " + total + " 段 · " + size;
        }
        return name + " · " + size;
    }
}
