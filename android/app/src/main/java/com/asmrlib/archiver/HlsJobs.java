package com.asmrlib.archiver;

import java.util.HashMap;
import java.util.Map;

/**
 * Process-wide registry of the in-app HLS downloads that are still running.
 *
 * <p>The worker itself lives in whichever Activity started it, but 删除 does
 * not: {@link DownloadsActivity} and {@link MainActivity} both render the same
 * stored task list, and either one can be the Activity that deletes a task.
 * A per-Activity map meant a delete from the download centre left the worker
 * running with no way to reach it — the cancelled flag was never set, the
 * worker kept writing, and the segments the user had just deleted came back.
 *
 * <p>So the map has to outlive any single Activity. It is keyed by the task's
 * stable {@code taskId} (the same id the stored record carries), which is what
 * lets a delete issued from one Activity stop a job started by the other.
 *
 * <p>Only the main thread mutates the map; the worker observes cancellation
 * through the volatile flag on its own job.
 */
public final class HlsJobs {

    private static final Map<String, HlsJob> JOBS = new HashMap<String, HlsJob>();

    private HlsJobs() {
    }

    public static void put(String taskId, HlsJob job) {
        if (taskId == null || job == null) return;
        synchronized (JOBS) {
            JOBS.put(taskId, job);
        }
    }

    public static HlsJob get(String taskId) {
        if (taskId == null) return null;
        synchronized (JOBS) {
            return JOBS.get(taskId);
        }
    }

    public static HlsJob remove(String taskId) {
        if (taskId == null) return null;
        synchronized (JOBS) {
            return JOBS.remove(taskId);
        }
    }

    /**
     * Ask a running worker to stop. The flag is read before each segment, so
     * the worker aborts on its own rather than being killed mid-write; the
     * caller is then free to delete what it had already written.
     */
    public static void cancel(String taskId) {
        HlsJob job = get(taskId);
        if (job != null) job.cancelled = true;
    }

    /** Tear the whole registry down, cancelling anything still running. */
    public static void clear() {
        synchronized (JOBS) {
            for (HlsJob job : JOBS.values()) job.cancelled = true;
            JOBS.clear();
        }
    }
}

/** One running in-app HLS download. The worker polls {@link #cancelled}. */
final class HlsJob {
    final String taskId;
    final String localPath;
    volatile boolean cancelled;

    HlsJob(String taskId, String localPath) {
        this.taskId = taskId;
        this.localPath = localPath;
    }
}
