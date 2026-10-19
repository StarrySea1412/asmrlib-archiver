package com.asmrlib.archiver;

import android.app.Activity;
import android.app.Dialog;
import android.content.ContentValues;
import android.content.Intent;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelFileDescriptor;
import android.provider.MediaStore;
import android.text.TextUtils;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
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
import java.text.DecimalFormat;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * Full-screen 收藏馆 download management page.
 *
 * <p>Lives in its own Activity so the floating entry in {@link MainActivity}
 * does not have to share its window with anything — tapping it launches this
 * page, the WebView's inline fullscreen never has to compete with a 44dp
 * button or a panel that wants to live on top of the picture, and the panel
 * itself does not need to overflow into another Activity's content.
 *
 * <p>State lives in {@code SharedPreferences} under the same key
 * {@code download_tasks} that {@link MainActivity} uses: both activities read
 * and write the same list, the in-app HLS worker keeps running in whichever
 * Activity is in front, and this page is just a different way to look at the
 * same list.
 *
 * <p>Open / rename / delete / retry all live here: retry runs the same
 * process-wide {@link HlsJobs} registry and {@link HlsDownloader} worker
 * MainActivity's panel does, so a failed row recovers in place. Saving fresh
 * media has to be triggered from the WebView and stays there. The point of
 * this page is to give the user's "what have I got" list a real home, not to
 * centralise every download action.
 */
public class DownloadsActivity extends Activity {

    private static final String TAG = "asmrlib-downloads";
    // Same key as MainActivity — both activities share the same store so a
    // change in one is visible in the other the next time it loads.
    private static final String PREFS = "asmrlib";
    private static final String KEY_DOWNLOADS = "download_tasks";
    private static final String STATE_RUNNING = "running";
    private static final String STATE_SUCCESS = "success";
    private static final String STATE_FAILED = "failed";
    private static final String STATE_LOST = "lost";
    private static final String KIND_DIRECT = "direct";
    private static final String KIND_HLS = "hls";
    private static final long POLL_INTERVAL_MS = 800L;
    /** Player page launch: it can ask this page back for 重命名 / 删除. */
    private static final int REQUEST_PLAYER = 7101;

    private final List<JSONObject> downloadTasks = new ArrayList<JSONObject>();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    /** Source paths with an export in flight. The poll re-renders the whole
     *  list every interval, so a Button reference does not survive a rebuild;
     *  this set is what a fresh row reads to keep its 导出MP4 locked and
     *  labelled 导出中… while the worker runs. */
    private final Set<String> exportingFiles = new HashSet<>();
    /** Percent per exporting source path. The poll rebuilds the list every
     *  800ms, so the bar reads this the same way the row reads the lock. */
    private final java.util.Map<String, Integer> exportProgress = new java.util.HashMap<>();

    private LinearLayout listContainer;
    private TextView storageLabel;
    private TextView emptyLabel;
    private FrameLayout root;

    private final Runnable pollRunnable = new Runnable() {
        @Override public void run() {
            // Re-read the shared store FIRST. The HLS worker writes its
            // segment progress into prefs from whichever Activity is in
            // front, and refreshDownloadStates() deliberately skips HLS rows
            // (they have no DownloadManager row to query). Without this
            // reload the bar sat still for the whole download — the exact
            // 「进度条是不动的」report.
            loadDownloadTasks();
            refreshDownloadStates();
            renderList();
            mainHandler.postDelayed(this, POLL_INTERVAL_MS);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        buildChrome();
        loadDownloadTasks();
        renderList();
    }

    @Override
    protected void onResume() {
        super.onResume();
        loadDownloadTasks();
        recoverOrphanedHlsTasks();
        renderList();
        mainHandler.postDelayed(pollRunnable, POLL_INTERVAL_MS);
    }

    /**
     * Re-decide HLS rows whose worker no longer exists.
     *
     * <p>The worker lives in whichever Activity started it, and that Activity
     * can die mid-download while the record in the shared store still says
     * 下载中 forever. The registry is process-wide, so a task that is running
     * but absent from {@link HlsJobs} is finished or dead; the file on disk
     * decides which. A finished artifact becomes 打开-able, and a dead one
     * becomes a retryable failure instead of a progress bar frozen at 40%.
     */
    private void recoverOrphanedHlsTasks() {
        boolean changed = false;
        for (int i = 0; i < downloadTasks.size(); i++) {
            JSONObject task = downloadTasks.get(i);
            if (!KIND_HLS.equals(task.optString("kind", KIND_DIRECT))) continue;
            if (!STATE_RUNNING.equals(task.optString("state", STATE_RUNNING))) continue;
            String taskId = task.optString("taskId", "");
            if (HlsJobs.get(taskId) != null) continue; // still owned by a worker
            boolean ok = artifactExists(task);
            try {
                task.put("state", ok ? STATE_SUCCESS : STATE_FAILED);
                if (ok) {
                    String localPath = task.optString("localPath", "");
                    task.put("localUri", Uri.fromFile(new File(localPath)).toString());
                } else {
                    task.put("error", "下载中断，可重试");
                }
            } catch (JSONException e) {
                Log.w(TAG, "could not recover orphaned task", e);
            }
            changed = true;
        }
        if (changed) saveDownloadTasks();
    }

    @Override
    protected void onPause() {
        super.onPause();
        mainHandler.removeCallbacks(pollRunnable);
    }

    // ---------------------------------------------------------- chrome

    private void buildChrome() {
        root = new FrameLayout(this);
        GradientDrawable bg = new GradientDrawable(
                GradientDrawable.Orientation.TOP_BOTTOM,
                new int[]{Theme.surface(DownloadsActivity.this), Theme.bg(DownloadsActivity.this)});
        root.setBackground(bg);

        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        FrameLayout.LayoutParams colLp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT);
        root.addView(column, colLp);

        // Header — title + close. The header sits above the list and matches
        // the in-app panel card so the transition from MainActivity feels
        // like opening a sheet rather than launching a new app.
        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(android.view.Gravity.CENTER_VERTICAL);
        int padH = dp(20);
        header.setPadding(padH, dp(28), padH, dp(14));
        column.addView(header, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        LinearLayout titleStack = new LinearLayout(this);
        titleStack.setOrientation(LinearLayout.VERTICAL);
        TextView title = new TextView(this);
        title.setText("下载中心");
        title.setTextColor(Theme.text(DownloadsActivity.this));
        title.setTextSize(20);
        title.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        titleStack.addView(title);

        storageLabel = new TextView(this);
        storageLabel.setTextColor(Theme.text2(DownloadsActivity.this));
        storageLabel.setTextSize(12);
        storageLabel.setPadding(0, dp(2), 0, 0);
        titleStack.addView(storageLabel);

        LinearLayout.LayoutParams titleLp = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        header.addView(titleStack, titleLp);

        Button close = new Button(this);
        close.setText("关闭");
        close.setContentDescription("关闭下载中心");
        stylePill(close, 13f, Theme.fill(DownloadsActivity.this, 34), Theme.text(DownloadsActivity.this));
        close.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { finish(); }
        });
        LinearLayout.LayoutParams closeLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, dp(40));
        header.addView(close, closeLp);

        // Top actions row — the two whole-page actions. They are not about
        // any single row, and they were buried: the user asked for them 最最
        // 最顶部. 位置 opens the system Downloads view the exported MP4s land
        // in; 清空 is the bulk sweep over every record and byte at once.
        LinearLayout topActions = new LinearLayout(this);
        topActions.setOrientation(LinearLayout.HORIZONTAL);
        topActions.setGravity(android.view.Gravity.CENTER_VERTICAL);
        topActions.setPadding(padH, 0, padH, dp(6));
        column.addView(topActions, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        Button openLocation = new Button(this);
        openLocation.setText("打开文件位置");
        openLocation.setContentDescription("在文件管理中查看视频保存位置");
        stylePill(openLocation, 13f, Theme.fill(DownloadsActivity.this, 34),
                Theme.text(DownloadsActivity.this));
        openLocation.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { openSaveLocation(); }
        });
        topActions.addView(openLocation, new LinearLayout.LayoutParams(
                0, dp(40), 1f));

        Button clearAll = new Button(this);
        clearAll.setText("清空数据");
        clearAll.setContentDescription("清空全部下载数据");
        stylePill(clearAll, 13f, Theme.danger(DownloadsActivity.this, 56),
                Theme.danger(DownloadsActivity.this));
        clearAll.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { confirmClearAll(); }
        });
        LinearLayout.LayoutParams clearLp = new LinearLayout.LayoutParams(
                0, dp(40), 1f);
        clearLp.leftMargin = dp(10);
        topActions.addView(clearAll, clearLp);

        // List area — a scroll view so a long list does not push the close
        // button off the screen.
        ScrollView scroll = new ScrollView(this);
        scroll.setVerticalScrollBarEnabled(false);
        LinearLayout.LayoutParams scrollLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f);
        scrollLp.setMargins(0, dp(8), 0, dp(8));
        column.addView(scroll, scrollLp);

        listContainer = new LinearLayout(this);
        listContainer.setOrientation(LinearLayout.VERTICAL);
        listContainer.setPadding(padH, 0, padH, dp(20));
        scroll.addView(listContainer, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT));

        emptyLabel = new TextView(this);
        emptyLabel.setText("还没有下载任务。");
        emptyLabel.setTextColor(Theme.text2(DownloadsActivity.this));
        emptyLabel.setTextSize(13);
        emptyLabel.setGravity(android.view.Gravity.CENTER);
        emptyLabel.setPadding(0, dp(40), 0, dp(40));
        emptyLabel.setVisibility(View.GONE);
        FrameLayout.LayoutParams emptyLp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT);
        emptyLp.gravity = Gravity.CENTER;
        root.addView(emptyLabel, emptyLp);

        setContentView(root);
    }

    // ---------------------------------------------------------- rendering

    private void renderList() {
        if (listContainer == null) return;
        listContainer.removeAllViews();
        storageLabel.setText("离线目录 · " + formatBytes(offlineBytes()));
        boolean any = false;
        for (int i = 0; i < downloadTasks.size(); i++) {
            JSONObject task = downloadTasks.get(i);
            if (task == null) continue;
            any = true;
            listContainer.addView(buildDownloadRow(task));
        }
        emptyLabel.setVisibility(any ? View.GONE : View.VISIBLE);
    }

    /**
     * One card in the list.
     *
     * <p>The buttons capture the task's stable id, not the {@code JSONObject}.
     * The list is rebuilt every 800 ms, so the object this card was rendered
     * from may no longer be in {@code downloadTasks} by the time the tap
     * lands — deleted from the other Activity, or replaced by a reload. Acting
     * on that stale object is how a tap on one row could delete another task's
     * bytes. Every handler resolves the id against the live list first and does
     * nothing if the record is gone.
     */
    private View buildDownloadRow(final JSONObject task) {
        final String taskId = task.optString("taskId", "");
        final long downloadId = task.optLong("downloadId", -1L);
        final String state = task.optString("state", "");
        final boolean isHls = KIND_HLS.equals(task.optString("kind", KIND_DIRECT));
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        GradientDrawable bg = new GradientDrawable();
        bg.setCornerRadius(dp(18));
        bg.setStroke(dp(1), Theme.sep(DownloadsActivity.this));
        bg.setColors(new int[]{Theme.surface(DownloadsActivity.this), Theme.surface2(DownloadsActivity.this)});
        card.setBackground(bg);
        card.setPadding(dp(16), dp(14), dp(16), dp(14));
        LinearLayout.LayoutParams cardLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        cardLp.setMargins(0, 0, 0, dp(10));
        card.setLayoutParams(cardLp);

        TextView name = new TextView(this);
        name.setText(task.optString("fileName", "未命名"));
        name.setTextColor(Theme.text(DownloadsActivity.this));
        name.setTextSize(15);
        name.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        name.setSingleLine(true);
        name.setEllipsize(TextUtils.TruncateAt.END);
        card.addView(name);

        TextView status = new TextView(this);
        status.setText(stateText(task));
        status.setTextSize(12);
        status.setPadding(0, dp(4), 0, 0);
        if (STATE_SUCCESS.equals(state)) {
            status.setTextColor(Theme.SUCCESS);
        } else if (STATE_FAILED.equals(state)
                || STATE_LOST.equals(state)) {
            status.setTextColor(Theme.danger(DownloadsActivity.this));
        } else {
            status.setTextColor(Theme.text2(DownloadsActivity.this));
        }
        card.addView(status);

        // The bar exists only while the task is moving. On a finished row it
        // sat at its last fraction, and on a failed one it froze mid-bar —
        // both read as a download that is still going.
        if (STATE_RUNNING.equals(state)) {
            ProgressBar bar = new ProgressBar(this, null,
                    android.R.attr.progressBarStyleHorizontal);
            bar.setMax(100);
            bar.setProgress(progressPercent(task));
            LinearLayout.LayoutParams barLp = new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT, dp(4));
            barLp.setMargins(0, dp(8), 0, 0);
            card.addView(bar, barLp);
        }

        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        // No gravity here: the cells carry weight, so they fill the row and
        // gravity would have nothing left to place. The row is the card's width.
        LinearLayout.LayoutParams actionsLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        actionsLp.setMargins(0, dp(10), 0, 0);
        card.addView(actions, actionsLp);

        if (STATE_SUCCESS.equals(state)) {
            actions.addView(rowButton("打开", new View.OnClickListener() {
                @Override public void onClick(View v) {
                    JSONObject live = findDownloadTask(taskId, downloadId);
                    if (live == null) { renderList(); return; }
                    openDownloadedFile(live);
                }
            }));
        }
        // 导出: a finished TS artifact, remuxed into the public Download/
        // directory. The file itself decides eligibility, not the record's
        // kind field — videos saved by older app versions have records with
        // no kind at all, and their .ts is exactly as remuxable as a fresh
        // one. 导出, not 导出MP4: four cells per row already, and the fourth
        // character was the one the 320dp card cut off.
        final File exportSrc = exportableFile(task);
        if (exportSrc != null) {
            String exportKey = exportSrc.getAbsolutePath();
            Integer exportingPct = exportingFiles.contains(exportKey)
                    ? exportProgress.get(exportKey) : null;
            if (exportingPct != null) {
                // The copy is running and the shade is not the only place it
                // reports: the row carries the same bar the download had.
                ProgressBar exportBar = new ProgressBar(this, null,
                        android.R.attr.progressBarStyleHorizontal);
                exportBar.setMax(100);
                exportBar.setProgress(exportingPct);
                LinearLayout.LayoutParams barLp = new LinearLayout.LayoutParams(
                        LinearLayout.LayoutParams.MATCH_PARENT, dp(4));
                barLp.setMargins(0, dp(8), 0, 0);
                card.addView(exportBar, barLp);
                TextView exportLabel = new TextView(this);
                exportLabel.setText("正在导出 MP4 · " + exportingPct + "%");
                exportLabel.setTextColor(Theme.text2(DownloadsActivity.this));
                exportLabel.setTextSize(12);
                exportLabel.setPadding(0, dp(4), 0, 0);
                card.addView(exportLabel);
            }
            Button export = rowButton("导出", new View.OnClickListener() {
                @Override public void onClick(View v) {
                    JSONObject live = findDownloadTask(taskId, downloadId);
                    if (live == null) { renderList(); return; }
                    exportAsMp4(live);
                }
            });
            export.setContentDescription("导出为MP4");
            if (exportingFiles.contains(exportKey)) {
                export.setEnabled(false);
                if (exportingPct == null) export.setText("导出中…");
            }
            actions.addView(export);
        }
        // Retry is offered for every dead-but-recoverable outcome, exactly
        // like MainActivity's panel: a failed task (either kind) can
        // re-enqueue, and a lost HLS task may still have its .parts cache on
        // disk to resume from. The only dead end with no retry is a lost
        // direct file, whose bytes the system reclaims wholesale.
        if (STATE_FAILED.equals(state)
                || (STATE_LOST.equals(state) && isHls)) {
            actions.addView(rowButton("重试", new View.OnClickListener() {
                @Override public void onClick(View v) {
                    JSONObject live = findDownloadTask(taskId, downloadId);
                    if (live == null) { renderList(); return; }
                    retryDownload(live);
                }
            }));
        }
        // Rename is offered for a finished save and for a failed one — a
        // failed task still has its parts on disk — but never for a record
        // whose bytes are already gone and never while a worker holds the
        // destination.
        if (isHls && !STATE_RUNNING.equals(state) && !STATE_LOST.equals(state)) {
            actions.addView(rowButton("重命名", new View.OnClickListener() {
                @Override public void onClick(View v) {
                    JSONObject live = findDownloadTask(taskId, downloadId);
                    if (live == null) { renderList(); return; }
                    renameDownload(live);
                }
            }));
        }
        if (STATE_LOST.equals(state)) {
            // 移除 rather than 移除记录: four characters do not fit a third of a
            // 320dp card at font scale 1.3, and the row would ellipsise it. The
            // full phrase lives in the accessibility label, where it costs no
            // width.
            Button remove = rowButton("移除", new View.OnClickListener() {
                @Override public void onClick(View v) {
                    JSONObject live = findDownloadTask(taskId, downloadId);
                    if (live == null) { renderList(); return; }
                    removeDownloadRecord(live);
                }
            });
            remove.setContentDescription("移除这条记录");
            actions.addView(remove);
        } else {
            actions.addView(rowButton("删除", new View.OnClickListener() {
                @Override public void onClick(View v) {
                    JSONObject live = findDownloadTask(taskId, downloadId);
                    if (live == null) { renderList(); return; }
                    deleteDownload(live);
                }
            }));
        }
        return card;
    }

    /**
     * Resolve a card's stable identity against the live list: {@code taskId}
     * for an HLS task, the system's {@code downloadId} for a direct one. The
     * object a card was rendered from is not stable across a reload, so nothing
     * that mutates the list may rely on it.
     */
    private JSONObject findDownloadTask(String taskId, long downloadId) {
        for (int i = 0; i < downloadTasks.size(); i++) {
            JSONObject task = downloadTasks.get(i);
            if (task == null) continue;
            if (KIND_HLS.equals(task.optString("kind", KIND_DIRECT))) {
                if (!taskId.isEmpty() && taskId.equals(task.optString("taskId", ""))) return task;
            } else if (downloadId >= 0 && downloadId == task.optLong("downloadId", -1L)) {
                return task;
            }
        }
        return null;
    }

    private int progressPercent(JSONObject task) {
        if (KIND_HLS.equals(task.optString("kind", KIND_DIRECT))) {
            int done = task.optInt("segmentsDone", 0);
            int total = task.optInt("segmentsTotal", 0);
            if (total > 0) return (int) Math.min(100L, done * 100L / total);
            return 0;
        }
        long soFar = task.optLong("bytesSoFar", 0L);
        long total = task.optLong("bytesTotal", -1L);
        if (total > 0) return (int) Math.min(100L, soFar * 100L / total);
        return 0;
    }

    /**
     * One action in a card's row: an equal share of the row, one height, one
     * gap.
     *
     * <p>This used to be {@code WRAP_CONTENT} wide, which is fine on a 360dp
     * phone and broken on a 320dp one at font scale 1.3: the three pills asked
     * for more width than the card had, so 删除 was squeezed until its label
     * wrapped and the second line was cut off by the fixed 40dp height
     * (screenshot, 2026-09-16 — both cards showed 「删」 over a clipped 「除」).
     * Equal weights cannot overflow, because the row *is* the card's width, and
     * a single line is what keeps the fixed height honest.
     */
    private Button rowButton(String label, View.OnClickListener onClick) {
        Button b = new Button(this);
        b.setText(label);
        b.setOnClickListener(onClick);
        stylePill(b, 13f, Theme.fill(DownloadsActivity.this, 34), Theme.text(DownloadsActivity.this));
        // A third of a small card cannot afford stylePill's 18dp gutters.
        b.setPadding(dp(6), 0, dp(6), 0);
        b.setSingleLine(true);
        b.setEllipsize(android.text.TextUtils.TruncateAt.END);
        b.setMinWidth(0);
        b.setMinimumWidth(0);
        b.setMinHeight(0);
        b.setMinimumHeight(0);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, dp(40), 1f);
        // 4dp each side: 8dp between neighbours, and symmetric at both ends.
        lp.setMargins(dp(4), 0, dp(4), 0);
        b.setLayoutParams(lp);
        return b;
    }

    // ---------------------------------------------------------- store

    /**
     * Re-read the stored task list.
     *
     * <p>An unreadable store must never empty the list. It used to, and the
     * 800 ms poll then wrote that empty list straight back: one bad read cost
     * the user every download record, silently, with nothing left to recover.
     * The in-memory list is therefore only replaced once a new one has been
     * parsed in full, and the unreadable string is quarantined under a separate
     * key rather than being overwritten.
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

    /**
     * Write the list back exactly the way {@link MainActivity} does — through a
     * {@link JSONArray}, one {@code put} per record.
     *
     * <p>Both activities own the same {@code download_tasks} key, so both have
     * to produce the same bytes. {@code List.toString()} is not
     * {@code JSONArray.toString()}: it separates elements with a comma and a
     * space, and it stringifies each record through a different path, so a
     * value the two do not agree on could leave behind a string neither side
     * could read back.
     */
    private void saveDownloadTasks() {
        JSONArray arr = new JSONArray();
        for (int i = 0; i < downloadTasks.size(); i++) arr.put(downloadTasks.get(i));
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                .putString(KEY_DOWNLOADS, arr.toString()).apply();
    }

    private void refreshDownloadStates() {
        android.app.DownloadManager manager = (android.app.DownloadManager)
                getSystemService(android.content.Context.DOWNLOAD_SERVICE);
        if (manager == null || downloadTasks.isEmpty()) return;
        boolean changed = false;
        for (int i = downloadTasks.size() - 1; i >= 0; i--) {
            JSONObject task = downloadTasks.get(i);
            if (task == null) continue;
            // An HLS task has no DownloadManager row by design (downloadId
            // is -1) — its state is owned by the in-app worker. Letting the
            // "no cursor row → drop the record" rule see it would delete
            // every HLS task on the first refresh.
            if (KIND_HLS.equals(task.optString("kind", KIND_DIRECT))) continue;
            long id = task.optLong("downloadId", -1L);
            android.app.DownloadManager.Query q = new android.app.DownloadManager.Query()
                    .setFilterById(id);
            Cursor cursor = null;
            try {
                cursor = manager.query(q);
                if (cursor == null || !cursor.moveToFirst()) {
                    downloadTasks.remove(i);
                    changed = true;
                    continue;
                }
                int s = cursor.getInt(cursor.getColumnIndexOrThrow(
                        android.app.DownloadManager.COLUMN_STATUS));
                long soFar = cursor.getLong(cursor.getColumnIndexOrThrow(
                        android.app.DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR));
                long total = cursor.getLong(cursor.getColumnIndexOrThrow(
                        android.app.DownloadManager.COLUMN_TOTAL_SIZE_BYTES));
                int reason = cursor.getInt(cursor.getColumnIndexOrThrow(
                        android.app.DownloadManager.COLUMN_REASON));
                String localUri = cursor.getString(cursor.getColumnIndexOrThrow(
                        android.app.DownloadManager.COLUMN_LOCAL_URI));
                task.put("bytesSoFar", soFar);
                task.put("bytesTotal", total);
                task.put("reason", reason);
                if (localUri != null && !localUri.isEmpty()) task.put("localUri", localUri);
                task.put("state", stateForStatus(s));
                changed = true;
            } catch (Exception e) {
                Log.w(TAG, "download query failed for id " + id, e);
            } finally {
                if (cursor != null) cursor.close();
            }
        }
        for (int i = 0; i < downloadTasks.size(); i++) {
            if (applyArtifactCheck(downloadTasks.get(i))) changed = true;
        }
        if (changed) saveDownloadTasks();
    }

    private boolean applyArtifactCheck(JSONObject task) {
        if (task == null) return false;
        String state = task.optString("state", STATE_RUNNING);
        if (!STATE_SUCCESS.equals(state) && !STATE_LOST.equals(state)) return false;
        String next = artifactExists(task) ? STATE_SUCCESS : STATE_LOST;
        if (next.equals(state)) return false;
        try { task.put("state", next); } catch (JSONException e) { return false; }
        return true;
    }

    private boolean artifactExists(JSONObject task) {
        File f = artifactFile(task);
        return f != null;
    }

    /**
     * The saved artifact as a local file, or null.
     *
     * <p>Old records and new records carry the path in different fields —
     * in-app HLS saves an absolute {@code localPath}, DownloadManager saves
     * a {@code localUri} that is a {@code file://} of the same — and videos
     * downloaded before this app version have records with no {@code kind}
     * at all. So eligibility is decided by finding an actual file, never by
     * trusting a field that old records do not have.
     *
     * <p>Old direct records may carry a {@code content://} localUri (what
     * DownloadManager reports on newer Android), from which no path can be
     * derived. The destination they were enqueued with is known, though:
     * {@code setDestinationInExternalPublicDir(DIRECTORY_DOWNLOADS, fileName)}
     * — so the record's own fileName against the public Downloads directory
     * is the fallback that keeps those rows alive.
     */
    private static File artifactFile(JSONObject task) {
        if (KIND_HLS.equals(task.optString("kind", KIND_DIRECT))) {
            String localPath = task.optString("localPath", "");
            if (localPath.isEmpty()) return null;
            File f = new File(localPath);
            return f.isFile() && f.length() > 0 ? f : null;
        }
        String localUri = task.optString("localUri", "");
        if (localUri.startsWith("file:")) {
            try {
                File f = new File(Uri.parse(localUri).getPath());
                if (f.isFile() && f.length() > 0) return f;
            } catch (Exception ignored) {
                // Fall through to the Downloads-directory lookup.
            }
        }
        String fileName = task.optString("fileName", "");
        if (fileName.isEmpty()) return null;
        File f = new File(Environment.getExternalStoragePublicDirectory(
                Environment.DIRECTORY_DOWNLOADS), fileName);
        return f.isFile() && f.length() > 0 ? f : null;
    }

    /** A finished TS artifact the remux can run on, or null. Only an
     *  existing file qualifies: a success record whose bytes were lost
     *  offers no export. */
    private static File exportableFile(JSONObject task) {
        if (!STATE_SUCCESS.equals(task.optString("state", ""))) return null;
        File f = artifactFile(task);
        if (f == null) return null;
        return f.getName().toLowerCase(Locale.US).endsWith(".ts") ? f : null;
    }

    /**
     * 导出: remux the saved TS into the public Download/ directory, with a
     * progress bar on the row and in the notification shade.
     *
     * <p>Where the file lands is the whole point: the previous version wrote
     * into the app-private offline directory, which 文件管理 on Android 11+
     * cannot browse — the user exported, looked in Download/, and found
     * nothing. So the copy goes through {@link MediaStore} (API 29+): the
     * MP4 appears in Download/ the moment the row un-pends, visible to
     * gallery and every file manager. Pre-29 devices (which this app can
     * still run on) keep the in-app destination with a toast naming it.
     *
     * <p>The original stays in place — the player's resume records, the
     * 息屏播放 hand-off and the rename logic all address it — so the MP4 is
     * a second artifact, never a conversion that leaves the app with a
     * dangling record.
     */
    private void exportAsMp4(final JSONObject task) {
        final File src = artifactFile(task);
        if (src == null) {
            Toast.makeText(this, "找不到该文件，可能已被删除", Toast.LENGTH_SHORT).show();
            return;
        }
        String stem = src.getName();
        int dot = stem.lastIndexOf('.');
        if (dot > 0) stem = stem.substring(0, dot);
        final String exportName = stem + ".mp4";
        final String key = src.getAbsolutePath();
        if (!exportingFiles.add(key)) return;

        ParcelFileDescriptor outFd = null;
        Uri storeRow = null;
        final File legacyDest;
        if (Build.VERSION.SDK_INT >= 29) {
            try {
                ContentValues values = new ContentValues();
                values.put(MediaStore.MediaColumns.DISPLAY_NAME, exportName);
                values.put(MediaStore.MediaColumns.MIME_TYPE, "video/mp4");
                values.put(MediaStore.MediaColumns.RELATIVE_PATH,
                        Environment.DIRECTORY_DOWNLOADS);
                values.put(MediaStore.MediaColumns.IS_PENDING, 1);
                storeRow = getContentResolver().insert(
                        MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
                if (storeRow != null) {
                    outFd = getContentResolver().openFileDescriptor(storeRow, "rw");
                }
            } catch (Exception e) {
                Log.w(TAG, "export store row failed", e);
            }
            if (outFd == null) {
                exportingFiles.remove(key);
                if (storeRow != null) {
                    try {
                        getContentResolver().delete(storeRow, null, null);
                    } catch (Exception ignored) {
                    }
                }
                Toast.makeText(this, "导出失败：无法在 Download/ 创建文件",
                        Toast.LENGTH_LONG).show();
                return;
            }
            legacyDest = null;
        } else {
            legacyDest = uniqueDest(src.getParentFile(), stem, ".mp4");
        }

        final Uri pendingRow = storeRow;
        final ParcelFileDescriptor fd = outFd;
        final File fallbackDest = legacyDest;
        new Thread(new Runnable() {
            @Override public void run() {
                TsToMp4.Progress progress = new TsToMp4.Progress() {
                    @Override public void onProgress(final long written, final long total) {
                        if (total <= 0) return;
                        final int pct = (int) Math.min(100L, written * 100L / total);
                        exportProgress.put(key, pct);
                        HlsNotify.exportProgress(DownloadsActivity.this, key,
                                exportName, pct, written);
                        mainHandler.post(new Runnable() {
                            @Override public void run() { renderList(); }
                        });
                    }
                };
                TsToMp4.Result result;
                try {
                result = fd != null
                        ? TsToMp4.convert(src.getAbsolutePath(), exportName,
                                fd.getFileDescriptor(), progress)
                        : TsToMp4.convert(src.getAbsolutePath(),
                                fallbackDest.getAbsolutePath(), null, progress);
                } finally {
                    if (fd != null) {
                        try {
                            fd.close();
                        } catch (Exception ignored) {
                        }
                    }
                }
                exportingFiles.remove(key);
                exportProgress.remove(key);
                HlsNotify.exportFinished(DownloadsActivity.this, key, exportName, result.ok);
                if (pendingRow != null) {
                    try {
                        if (result.ok) {
                            // Un-pend: the row becomes Download/xxx.mp4,
                            // visible to 文件管理 and gallery immediately.
                            ContentValues done = new ContentValues();
                            done.put(MediaStore.MediaColumns.IS_PENDING, 0);
                            getContentResolver().update(pendingRow, done, null, null);
                        } else {
                            getContentResolver().delete(pendingRow, null, null);
                        }
                    } catch (Exception e) {
                        Log.w(TAG, "export row finalize failed", e);
                    }
                }
                mainHandler.post(new Runnable() {
                    @Override public void run() {
                        renderList();
                        if (result.ok) {
                            String where = pendingRow != null
                                    ? "Download/" : fallbackDest.getAbsolutePath();
                            Toast.makeText(DownloadsActivity.this,
                                    "已导出到 " + where + " · " + exportName,
                                    Toast.LENGTH_LONG).show();
                        } else {
                            Toast.makeText(DownloadsActivity.this,
                                    "导出失败：" + result.error, Toast.LENGTH_LONG).show();
                        }
                    }
                });
            }
        }, "asmrlib-export").start();
    }

    private static String stateForStatus(int status) {
        switch (status) {
            case android.app.DownloadManager.STATUS_SUCCESSFUL: return STATE_SUCCESS;
            case android.app.DownloadManager.STATUS_FAILED: return STATE_FAILED;
            case android.app.DownloadManager.STATUS_PAUSED:
            case android.app.DownloadManager.STATUS_PENDING:
            case android.app.DownloadManager.STATUS_RUNNING:
            default:
                return STATE_RUNNING;
        }
    }

    private String stateText(JSONObject task) {
        String state = task.optString("state", STATE_RUNNING);
        if (KIND_HLS.equals(task.optString("kind", KIND_DIRECT))) {
            if (STATE_SUCCESS.equals(state)) {
                int seg = task.optInt("segmentsTotal", 0);
                if (seg > 0) return "已完成 · " + seg + " 段";
                return "已完成";
            }
            if (STATE_FAILED.equals(state)) {
                String err = task.optString("error", "");
                if (!err.isEmpty()) return "失败 · " + err;
                return "失败";
            }
            if (STATE_LOST.equals(state)) return "文件丢失";
            int done = task.optInt("segmentsDone", 0);
            int total = task.optInt("segmentsTotal", 0);
            long bytes = task.optLong("bytesSoFar", 0L);
            if (total > 0) {
                int pct = (int) Math.min(100L, done * 100L / total);
                return pct + "% · " + done + " / " + total + " 段 · " + formatBytes(bytes);
            }
            return formatBytes(bytes);
        }
        if (STATE_SUCCESS.equals(state)) return "已完成";
        if (STATE_FAILED.equals(state)) {
            int reason = task.optInt("reason", 0);
            if (reason != 0) return "失败 · " + reasonText(reason);
            return "失败";
        }
        if (STATE_LOST.equals(state)) return "文件丢失";
        long soFar = task.optLong("bytesSoFar", 0L);
        long total = task.optLong("bytesTotal", -1L);
        if (total > 0) {
            int pct = (int) Math.min(100L, soFar * 100L / total);
            return pct + "% · " + formatBytes(soFar) + " / " + formatBytes(total);
        }
        return formatBytes(soFar) + " / 未知大小";
    }

    private static String reasonText(int reason) {
        // The numeric codes are documented in DownloadManager; surface the
        // user-facing ones in plain Chinese so a failed row reads as
        // "失败 · 空间不足" rather than "失败 · 1009".
        switch (reason) {
            case 1: return "无法继续";
            case 2: return "网络错误";
            case 4: return "存储空间不足";
            case 5: return "需要权限";
            case 6: return "请求超时";
            case 7: return "请求被拒绝";
            case 8: return "请求已取消";
            case 9: return "网络断开";
            case 10: return "地址无效";
            case 11: return "长度错误";
            case 12: return "文件未找到";
            case 13: return "下载被拦截";
            case 14: return "网络类型不支持";
            default: return "错误代码 " + reason;
        }
    }

    // ---------------------------------------------------------- actions

    private void openDownloadedFile(JSONObject task) {
        String localPath = task.optString("localPath", "");
        Uri uri = null;
        File file = null;
        if (KIND_HLS.equals(task.optString("kind", KIND_DIRECT)) && !localPath.isEmpty()) {
            file = new File(localPath);
            if (file.isFile()) uri = DownloadFileProvider.uriForFile(file);
        } else {
            String raw = task.optString("localUri", "");
            if (raw.isEmpty()) {
                Toast.makeText(this, "该文件没有可播放的位置", Toast.LENGTH_SHORT).show();
                return;
            }
            try {
                Uri parsed = Uri.parse(raw);
                if ("file".equals(parsed.getScheme())) {
                    file = new File(parsed.getPath());
                }
                uri = DownloadFileProvider.uriForFile(parsed);
            } catch (Exception e) {
                Log.w(TAG, "could not parse stored localUri", e);
            }
        }
        if (uri == null || file == null || !file.isFile()) {
            Toast.makeText(this, "找不到该文件，可能已被删除", Toast.LENGTH_SHORT).show();
            return;
        }
        String mime = "video/mp2t";
        if (file.getName().endsWith(".mp4")) mime = "video/mp4";
        else if (file.getName().endsWith(".ts")) mime = "video/mp2t";
        Intent open = new Intent(this, PlayerActivity.class);
        open.putExtra(PlayerActivity.EXTRA_URI, uri.toString());
        open.putExtra(PlayerActivity.EXTRA_TITLE, task.optString("fileName", ""));
        // The player shows the file's facts and offers 重命名 / 删除, but it
        // hands both back here rather than growing a second implementation of
        // either. Without the id it could only offer them for nothing.
        open.putExtra(PlayerActivity.EXTRA_TASK_ID, task.optString("taskId", ""));
        open.putExtra(PlayerActivity.EXTRA_BYTES, file.length());
        open.putExtra(PlayerActivity.EXTRA_STATE, task.optString("state", ""));
        open.putExtra(PlayerActivity.EXTRA_SAVED_AT, file.lastModified());
        open.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        startActivityForResult(open, REQUEST_PLAYER);
    }

    /**
     * 重命名 / 删除 asked for from the player page.
     *
     * <p>The player closes itself and this page opens the same styled dialog it
     * always has — one implementation, one look, and the destructive path never
     * runs from an activity that is about to be finished.
     */
    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQUEST_PLAYER || resultCode != RESULT_OK || data == null) return;
        String action = data.getStringExtra(PlayerActivity.RESULT_ACTION);
        String taskId = data.getStringExtra(PlayerActivity.RESULT_TASK_ID);
        if (action == null || taskId == null) return;
        JSONObject task = findDownloadTask(taskId, -1L);
        if (task == null) {
            renderList();
            return;
        }
        if (PlayerActivity.ACTION_RENAME.equals(action)) renameDownload(task);
        else if (PlayerActivity.ACTION_DELETE.equals(action)) deleteDownload(task);
    }

    private void renameDownload(final JSONObject task) {
        final String localPath = task.optString("localPath", "");
        final File dest = localPath.isEmpty() ? null : new File(localPath);
        if (dest == null) {
            Toast.makeText(this, "这条记录没有可重命名的文件", Toast.LENGTH_SHORT).show();
            return;
        }
        final String current = dest.getName();
        // Custom view inside the product modal shell (see productDialog): the
        // title + current filename (so they see what they are renaming) and an
        // inline button row where "保存" is a filled gradient pill the eye
        // lands on first.
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);

        TextView title = new TextView(this);
        title.setText("重命名文件");
        title.setTextSize(17f);
        title.setTextColor(Theme.text(DownloadsActivity.this));
        title.setTypeface(title.getTypeface(), android.graphics.Typeface.BOLD);
        LinearLayout.LayoutParams titleLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        titleLp.bottomMargin = dp(12);
        root.addView(title, titleLp);

        TextView currentLabel = new TextView(this);
        currentLabel.setText("当前：" + current);
        currentLabel.setTextSize(12.5f);
        currentLabel.setTextColor(Theme.text2(DownloadsActivity.this));
        LinearLayout.LayoutParams curLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        curLp.bottomMargin = dp(10);
        root.addView(currentLabel, curLp);

        final EditText input = new EditText(this);
        input.setSingleLine(true);
        int dot = current.lastIndexOf('.');
        String stem = dot > 0 ? current.substring(0, dot) : current;
        input.setText(stem);
        input.setSelection(input.getText().length());
        input.setTextColor(Theme.text(DownloadsActivity.this));
        input.setHintTextColor(Theme.text3(DownloadsActivity.this));
        input.setHint("新的文件名（不含后缀）");
        android.graphics.drawable.GradientDrawable inputBg =
                new android.graphics.drawable.GradientDrawable();
        inputBg.setCornerRadius(dp(12));
        inputBg.setColor(Theme.surface2(DownloadsActivity.this));
        inputBg.setStroke(dp(1), Theme.sepStrong(DownloadsActivity.this));
        input.setBackground(inputBg);
        int inPad = dp(14);
        input.setPadding(inPad, inPad, inPad, inPad);
        LinearLayout.LayoutParams inLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        inLp.bottomMargin = dp(22);
        root.addView(input, inLp);

        // Inline button row: "取消" (ghost) on the left, "保存" (filled
        // gradient pill) on the right. The save button's enabled state is
        // driven by whether the trimmed input is non-empty — disables the
        // submit path on an empty name so the user gets feedback before the
        // dialog quietly dismisses and re-opens with a toast.
        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        buttons.setGravity(Gravity.END);

        Button cancelBtn = new Button(this);
        cancelBtn.setAllCaps(false);
        cancelBtn.setText("取消");
        cancelBtn.setTextColor(Theme.text2(DownloadsActivity.this));
        android.graphics.drawable.GradientDrawable cancelBg =
                new android.graphics.drawable.GradientDrawable();
        cancelBg.setCornerRadius(dp(12));
        cancelBg.setColor(Theme.surface2(DownloadsActivity.this));
        cancelBg.setStroke(dp(1), Theme.sepStrong(DownloadsActivity.this));
        cancelBtn.setBackground(PressFeedback.on(cancelBg));
        cancelBtn.setPadding(dp(20), dp(12), dp(20), dp(12));
        LinearLayout.LayoutParams cancelLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        cancelLp.rightMargin = dp(10);
        buttons.addView(cancelBtn, cancelLp);

        final Button saveBtn = new Button(this);
        saveBtn.setAllCaps(false);
        saveBtn.setText("保存");
        saveBtn.setTextColor(Theme.onAccent(DownloadsActivity.this));
        saveBtn.setTypeface(saveBtn.getTypeface(), android.graphics.Typeface.BOLD);
        final android.graphics.drawable.GradientDrawable saveBg =
                new android.graphics.drawable.GradientDrawable(
                        android.graphics.drawable.GradientDrawable.Orientation.LEFT_RIGHT,
                        new int[]{
                                Theme.accent(DownloadsActivity.this),
                                Theme.accentAlt(DownloadsActivity.this)});
        saveBg.setCornerRadius(dp(12));
        saveBtn.setBackground(PressFeedback.on(saveBg));
        saveBtn.setPadding(dp(26), dp(12), dp(26), dp(12));
        buttons.addView(saveBtn, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        root.addView(buttons, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        final android.text.TextWatcher refresh = new android.text.TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int start, int count, int after) {}
            @Override public void onTextChanged(CharSequence s, int start, int before, int count) {}
            @Override public void afterTextChanged(android.text.Editable s) {
                boolean ok = sanitizeStem(s.toString()).length() > 0;
                saveBtn.setEnabled(ok);
                saveBtn.setAlpha(ok ? 1f : 0.45f);
            }
        };
        input.addTextChangedListener(refresh);
        refresh.afterTextChanged(input.getEditableText());

        final Dialog dialog = productDialog(root);
        cancelBtn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { dialog.dismiss(); }
        });
        saveBtn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                // Save = apply + dismiss in one motion: the user pressed the
                // primary button and the dialog must not linger. Toast confirms
                // the new name. The apply itself is idempotent against the
                // empty-name check above so reaching here means rename will
                // succeed or surface its own error toast.
                String raw = input.getText().toString();
                applyRename(task, dest, raw);
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
            if (oldParts.isDirectory()) {
                oldParts.renameTo(new File(target.getAbsolutePath() + ".parts"));
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
        renderList();
        Toast.makeText(this, "已重命名为 " + target.getName(), Toast.LENGTH_SHORT).show();
    }

    /**
     * 删除 a task: confirm first, then drop the record and its bytes.
     *
     * <p>The guard is the point of this method. It ran on whatever object the
     * tapped card was rendered from, which the 800 ms reload may already have
     * replaced, and the deletion is irreversible — gigabytes at a time for a
     * long recording. So the id is resolved against the live list before
     * anything is touched, and again after the dialog, which does not stop the
     * poll either.
     */
    private void deleteDownload(JSONObject task) {
        JSONObject target = findDownloadTask(
                task.optString("taskId", ""), task.optLong("downloadId", -1L));
        if (target == null) {
            renderList();
            return;
        }
        confirmDelete(target);
    }

    private void confirmDelete(final JSONObject task) {
        final String taskId = task.optString("taskId", "");
        final long downloadId = task.optLong("downloadId", -1L);
        // The custom view matches renameDownload so the two destructive paths
        // read as a pair, and both sit inside the product modal shell (see
        // productDialog): a dim scrim over the page, one glass card, and a red
        // delete pill instead of a muted "positive" tint.
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);

        TextView title = new TextView(this);
        title.setText("删除下载");
        title.setTextSize(17f);
        title.setTextColor(Theme.text(DownloadsActivity.this));
        title.setTypeface(title.getTypeface(), android.graphics.Typeface.BOLD);
        LinearLayout.LayoutParams titleLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        titleLp.bottomMargin = dp(12);
        root.addView(title, titleLp);

        TextView desc = new TextView(this);
        String name = task.optString("fileName", "该视频");
        String sizeText = formatBytes(taskFileBytes(task));
        desc.setText("将永久删除「" + name + "」\n（" + sizeText + "）及其下载段。");
        desc.setTextSize(13f);
        desc.setTextColor(Theme.text2(DownloadsActivity.this));
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
        cancelBtn.setTextColor(Theme.text2(DownloadsActivity.this));
        android.graphics.drawable.GradientDrawable cancelBg =
                new android.graphics.drawable.GradientDrawable();
        cancelBg.setCornerRadius(dp(12));
        cancelBg.setColor(Theme.surface2(DownloadsActivity.this));
        cancelBg.setStroke(dp(1), Theme.sepStrong(DownloadsActivity.this));
        cancelBtn.setBackground(PressFeedback.on(cancelBg));
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
        final android.graphics.drawable.GradientDrawable deleteBg =
                new android.graphics.drawable.GradientDrawable(
                        android.graphics.drawable.GradientDrawable.Orientation.LEFT_RIGHT,
                        new int[]{
                                Theme.danger(DownloadsActivity.this),
                                Theme.DANGER_ALT});
        deleteBg.setCornerRadius(dp(12));
        deleteBtn.setBackground(PressFeedback.on(deleteBg));
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
     * centred glass card, nothing else.
     *
     * <p>The platform's AlertDialog carried its own grey chrome — a second
     * theme the product never chose, floating over our palette. That is the
     * 「modal乱改」 complaint. The card here is a {@link GlassDrawable}, the
     * same material the download panel and the floating control wear, so a
     * dialog reads as one more surface of the app rather than as a system
     * sticker. The scrim's tap dismisses; the card swallows its own tap so a
     * touch inside never closes it.
     */
    private Dialog productDialog(View content) {
        FrameLayout scrim = new FrameLayout(this);
        scrim.setBackgroundColor(Theme.scrim(DownloadsActivity.this, 70));
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

    /** Bytes the task's saved artifact actually occupies on disk, for the
     *  honest deletion description. Returns 0 when the file is already gone. */
    private static long taskFileBytes(JSONObject task) {
        String localPath = task.optString("localPath", "");
        if (localPath != null && !localPath.isEmpty()) {
            File f = new File(localPath);
            if (f.isFile()) return f.length();
        }
        String localUri = task.optString("localUri", "");
        if (localUri != null && !localUri.isEmpty()) {
            try {
                android.net.Uri u = android.net.Uri.parse(localUri);
                if ("file".equals(u.getScheme()) && u.getPath() != null) {
                    File f = new File(u.getPath());
                    if (f.isFile()) return f.length();
                }
            } catch (Exception ignored) { }
        }
        return 0L;
    }

    private void performDelete(String taskId, long downloadId) {
        JSONObject task = findDownloadTask(taskId, downloadId);
        if (task == null) {
            renderList();
            return;
        }
        if (KIND_HLS.equals(task.optString("kind", KIND_DIRECT))) {
            // The worker is owned by the process (see HlsJobs), not by this
            // Activity. Without this the thread kept fetching segments and
            // rewrote the .parts directory the user had just deleted — the row
            // came back on its own a few seconds later.
            HlsJobs.cancel(taskId);
            String localPath = task.optString("localPath", "");
            if (!localPath.isEmpty()) {
                deleteTree(new File(localPath + ".parts"));
                new File(localPath + ".part").delete();
                new File(localPath).delete();
            }
        } else {
            long id = task.optLong("downloadId", -1L);
            try {
                android.app.DownloadManager mgr = (android.app.DownloadManager)
                        getSystemService(android.content.Context.DOWNLOAD_SERVICE);
                if (mgr != null) mgr.remove(id);
            } catch (Exception e) {
                Log.w(TAG, "remove download failed", e);
            }
        }
        downloadTasks.remove(task);
        saveDownloadTasks();
        renderList();
        storageLabel.setText("离线目录 · " + formatBytes(offlineBytes()));
    }

    private void removeDownloadRecord(JSONObject task) {
        downloadTasks.remove(task);
        saveDownloadTasks();
        renderList();
    }

    /**
     * Jump to the folder the saved videos live in. Exported MP4s land in the
     * public Download/ directory, so the system Downloads view is where the
     * user will find them — opening a raw file-manager path into the app's
     * private directory is a dead end on Android 11+ anyway.
     */
    private void openSaveLocation() {
        try {
            startActivity(new Intent(android.app.DownloadManager.ACTION_VIEW_DOWNLOADS));
        } catch (Exception e) {
            Toast.makeText(this, "系统里没有文件管理器", Toast.LENGTH_SHORT).show();
        }
    }

    /**
     * 清空数据: one sweep over every record and byte. This is the bulk form
     * of 删除, so it reuses 删除's own machinery — same worker cancel, same
     * .parts cleanup, same DownloadManager remove — instead of a second,
     * weaker copy that some future file shape would silently outgrow.
     */
    private void confirmClearAll() {
        if (downloadTasks.isEmpty()) {
            Toast.makeText(this, "没有可清空的数据", Toast.LENGTH_SHORT).show();
            return;
        }
        long bytes = 0;
        for (JSONObject task : downloadTasks) {
            File f = artifactFile(task);
            if (f != null) bytes += f.length();
        }
        // The same product modal 删除 uses, scaled to the bulk wording: one
        // glass card, a muted cancel, a red 清空 pill.
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);

        TextView title = new TextView(this);
        title.setText("清空数据");
        title.setTextSize(17f);
        title.setTextColor(Theme.text(DownloadsActivity.this));
        title.setTypeface(title.getTypeface(), android.graphics.Typeface.BOLD);
        LinearLayout.LayoutParams titleLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        titleLp.bottomMargin = dp(12);
        root.addView(title, titleLp);

        TextView desc = new TextView(this);
        desc.setText("将删除全部 " + downloadTasks.size() + " 条下载（"
                + formatBytes(bytes) + "）及其记录，不可恢复。");
        desc.setTextSize(13f);
        desc.setTextColor(Theme.text2(DownloadsActivity.this));
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
        cancelBtn.setTextColor(Theme.text2(DownloadsActivity.this));
        android.graphics.drawable.GradientDrawable cancelBg =
                new android.graphics.drawable.GradientDrawable();
        cancelBg.setCornerRadius(dp(12));
        cancelBg.setColor(Theme.surface2(DownloadsActivity.this));
        cancelBg.setStroke(dp(1), Theme.sepStrong(DownloadsActivity.this));
        cancelBtn.setBackground(PressFeedback.on(cancelBg));
        cancelBtn.setPadding(dp(20), dp(12), dp(20), dp(12));
        LinearLayout.LayoutParams cancelLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        cancelLp.rightMargin = dp(10);
        buttons.addView(cancelBtn, cancelLp);

        Button clearBtn = new Button(this);
        clearBtn.setAllCaps(false);
        clearBtn.setText("清空");
        clearBtn.setTextColor(Theme.ON_DANGER);
        clearBtn.setTypeface(clearBtn.getTypeface(), android.graphics.Typeface.BOLD);
        android.graphics.drawable.GradientDrawable clearBg =
                new android.graphics.drawable.GradientDrawable(
                        android.graphics.drawable.GradientDrawable.Orientation.LEFT_RIGHT,
                        new int[]{Theme.danger(DownloadsActivity.this), Theme.DANGER_ALT});
        clearBg.setCornerRadius(dp(12));
        clearBtn.setBackground(PressFeedback.on(clearBg));
        clearBtn.setPadding(dp(26), dp(12), dp(26), dp(12));
        buttons.addView(clearBtn, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        root.addView(buttons, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        final Dialog dialog = productDialog(root);
        cancelBtn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { dialog.dismiss(); }
        });
        clearBtn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                dialog.dismiss();
                performClearAll();
            }
        });
    }

    private void performClearAll() {
        for (JSONObject task : new ArrayList<>(downloadTasks)) {
            performDelete(task.optString("taskId", ""), task.optLong("downloadId", -1L));
        }
        Toast.makeText(this, "已清空全部下载数据", Toast.LENGTH_SHORT).show();
    }

    // ---------------------------------------------------------- retry

    /**
     * Bring a dead task back to life, in place.
     *
     * <p>Retry used to exist only in MainActivity's panel — the docstring here
     * once said it "requires the in-app worker" and stayed there. Nothing
     * about the worker actually requires MainActivity: {@link HlsJobs} is
     * process-wide and {@link HlsDownloader} is a plain class, so the
     * download centre can own the retry for the records it renders. Without
     * it, a failed row's only path back was to leave the page, tap through
     * the floating menu, and find the same row in a smaller panel.
     */
    private void retryDownload(JSONObject task) {
        requestNotificationPermission();
        if (KIND_HLS.equals(task.optString("kind", KIND_DIRECT))) {
            String hlsUrl = task.optString("url", "");
            if (!isHlsUrl(hlsUrl)) {
                Toast.makeText(this, "原始地址已不可用，无法重试", Toast.LENGTH_SHORT).show();
                return;
            }
            // A failed job left its .parts directory in place, so restarting
            // the worker resumes from the segments that already arrived
            // instead of re-fetching them.
            try {
                task.put("state", STATE_RUNNING);
                task.put("error", "");
                task.put("segmentsDone", 0);
                task.put("bytesSoFar", 0L);
            } catch (JSONException e) {
                Log.w(TAG, "could not reset HLS task for retry", e);
            }
            saveDownloadTasks();
            renderList();
            startHlsJob(task);
            Toast.makeText(this, "已重新加入下载", Toast.LENGTH_SHORT).show();
            return;
        }
        String url = task.optString("url", "");
        if (!isDirectDownloadUrl(url)) {
            Toast.makeText(this, "原始地址已不可用，无法重试", Toast.LENGTH_SHORT).show();
            return;
        }
        String fileName = task.optString("fileName", "");
        String pageUrl = task.optString("pageUrl", "");
        try {
            long newId = enqueueDownload(url, pageUrl, fileName);
            downloadTasks.remove(task);
            downloadTasks.add(newTaskRecord(newId, url, pageUrl, fileName));
            saveDownloadTasks();
            renderList();
            Toast.makeText(this, "已重新加入下载", Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            Log.w(TAG, "retry download failed", e);
            Toast.makeText(this, "重试失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    /** Ask only when a retry actually starts, never on page open. */
    private void requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestPermissions(
                    new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 1505);
        }
    }

    /** Start (or resume) the worker behind a stored HLS record. */
    private void startHlsJob(final JSONObject task) {
        final String taskId = task.optString("taskId", "");
        final String url = task.optString("url", "");
        final String localPath = task.optString("localPath", "");
        if (taskId.isEmpty() || url.isEmpty() || localPath.isEmpty()) return;
        final HlsJob job = new HlsJob(taskId, localPath);
        HlsJobs.put(taskId, job);
        // The same ad-host policy MainActivity's worker runs under: a segment
        // on a blocked host is refused, so a retry can never become the way
        // an advert gets fetched.
        final ShieldRules rules = ShieldRules.fromAssets(getAssets(), "");
        final HlsDownloader.Policy policy = new HlsDownloader.Policy() {
            @Override public boolean allow(String url) {
                return rules != null && !rules.isAdUrl(url);
            }
        };
        final HlsDownloader.Listener listener = new HlsDownloader.Listener() {
            @Override public void onProgress(final int done, final int total, final long bytes) {
                mainHandler.post(new Runnable() {
                    @Override public void run() {
                        applyHlsProgress(taskId, done, total, bytes);
                    }
                });
            }
            @Override public boolean isCancelled() {
                return job.cancelled;
            }
        };
        Thread worker = new Thread(new Runnable() {
            @Override public void run() {
                HlsDownloader.Result result = HlsDownloader.download(
                        url, new File(localPath), policy, listener);
                finishHlsJob(job, result);
            }
        }, "asmrlib-hls-" + taskId);
        worker.start();
    }

    /** Progress lands in the shared store and on the ongoing notification. */
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
        HlsNotify.progress(this, taskId,
                task.optString("fileName", "视频文件"), done, total, bytes);
        // the 800 ms poll renders; no renderList() here
    }

    private void finishHlsJob(final HlsJob job, final HlsDownloader.Result result) {
        final String taskId = job.taskId;
        final String localPath = job.localPath;
        final boolean ok = result.ok;
        final String error = result.error;
        final int segments = result.segmentsTotal;
        final long bytes = result.bytes;
        mainHandler.post(new Runnable() {
            @Override public void run() {
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
                HlsNotify.finished(DownloadsActivity.this, taskId,
                        task.optString("fileName", "视频文件"), ok);
            }
        });
    }

    private JSONObject findHlsTask(String taskId) {
        for (int i = 0; i < downloadTasks.size(); i++) {
            JSONObject task = downloadTasks.get(i);
            if (KIND_HLS.equals(task.optString("kind", KIND_DIRECT))
                    && taskId.equals(task.optString("taskId", ""))) {
                return task;
            }
        }
        return null;
    }

    private JSONObject newTaskRecord(long downloadId, String url, String pageUrl,
                                     String fileName) {
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

    /** Same hand-off MainActivity makes: real headers, public Downloads dir. */
    private long enqueueDownload(String url, String pageUrl, String fileName) {
        android.app.DownloadManager.Request request =
                new android.app.DownloadManager.Request(Uri.parse(url));
        request.setTitle(fileName);
        request.setDescription("ASMR 收藏馆已验证媒体");
        request.setNotificationVisibility(
                android.app.DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
        request.setDestinationInExternalPublicDir(
                android.os.Environment.DIRECTORY_DOWNLOADS, fileName);
        String cookie = android.webkit.CookieManager.getInstance().getCookie(url);
        if (cookie != null && !cookie.isEmpty()) request.addRequestHeader("Cookie", cookie);
        String referer = pageUrl == null ? "" : pageUrl;
        if (!referer.isEmpty()) request.addRequestHeader("Referer", referer);
        android.app.DownloadManager manager = (android.app.DownloadManager)
                getSystemService(android.content.Context.DOWNLOAD_SERVICE);
        if (manager == null) throw new IllegalStateException("DownloadManager unavailable");
        return manager.enqueue(request);
    }

    /** An HLS playlist URL, query string stripped before the check. */
    private static boolean isHlsUrl(String raw) {
        if (raw == null || raw.isEmpty()) return false;
        Uri uri = Uri.parse(raw);
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase();
        if (!"http".equals(scheme) && !"https".equals(scheme)) return false;
        String path = uri.getPath() == null ? "" : uri.getPath().toLowerCase();
        return path.endsWith(".m3u8");
    }

    /** Direct media files only — mirrors MainActivity's suffix list. */
    private static boolean isDirectDownloadUrl(String raw) {
        if (raw == null || raw.isEmpty()) return false;
        Uri uri = Uri.parse(raw);
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase();
        if (!"http".equals(scheme) && !"https".equals(scheme)) return false;
        if (uri.getHost() == null) return false;
        String path = uri.getPath() == null ? "" : uri.getPath().toLowerCase();
        return path.endsWith(".mp4") || path.endsWith(".webm") || path.endsWith(".m4a")
                || path.endsWith(".mp3") || path.endsWith(".aac") || path.endsWith(".ogg")
                || path.endsWith(".wav") || path.endsWith(".flac");
    }

    // ---------------------------------------------------------- helpers

    private static String sanitizeStem(String raw) {
        if (raw == null) return "";
        String s = raw.trim();
        s = s.replaceAll("[\\\\/:*?\"<>|]", "");
        s = s.replaceAll("\\s+", " ");
        s = s.replaceAll("[\\. ]+$", "");
        if (s.length() > 60) s = s.substring(0, 60);
        return s;
    }

    private static File uniqueDest(File dir, String stem, String ext) {
        File candidate = new File(dir, stem + ext);
        int n = 2;
        while (candidate.exists()
                || new File(dir, stem + ext + ".parts").exists()
                || new File(dir, stem + ext + ".part").exists()) {
            if (n > 99) return new File(dir, stem + "-" + System.currentTimeMillis() + ext);
            candidate = new File(dir, stem + " (" + n + ")" + ext);
            n++;
        }
        return candidate;
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

    private long offlineBytes() {
        File base = getExternalFilesDir(null);
        if (base == null) base = getFilesDir();
        File dir = new File(base, MainActivity.OFFLINE_DIR);
        return treeBytes(dir);
    }

    private static long treeBytes(File f) {
        if (f == null || !f.exists()) return 0;
        if (f.isFile()) return f.length();
        long total = 0;
        File[] children = f.listFiles();
        if (children != null) {
            for (File c : children) total += treeBytes(c);
        }
        return total;
    }

    private static String formatBytes(long bytes) {
        if (bytes <= 0) return "0 B";
        String[] units = {"B", "KB", "MB", "GB", "TB"};
        double v = bytes;
        int i = 0;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        DecimalFormat df = new DecimalFormat(i == 0 ? "0" : "#.#");
        return df.format(v) + " " + units[i];
    }

    private void stylePill(Button b, float textSizeSp, int bgArgb, int textArgb) {
        GradientDrawable gd = new GradientDrawable();
        gd.setCornerRadius(dp(999));
        gd.setColor(bgArgb);
        gd.setStroke(dp(1), Theme.sepStrong(DownloadsActivity.this));
        b.setBackground(PressFeedback.on(gd));
        b.setTextColor(textArgb);
        b.setTextSize(textSizeSp);
        b.setAllCaps(false);
        b.setPadding(dp(18), 0, dp(18), 0);
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}