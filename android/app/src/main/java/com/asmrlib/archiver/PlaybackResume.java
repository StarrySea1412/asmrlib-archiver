package com.asmrlib.archiver;

import java.util.ArrayList;
import java.util.List;

/**
 * Where playback left off, as pure logic.
 *
 * <p>Deliberately free of any {@code android.*} import, like
 * {@link HlsDownloader} and for the same reason: the whole class then compiles
 * with plain {@code javac} and runs on an ordinary JVM, so
 * {@code tests/test_playback_resume_java.py} can execute the real decision —
 * resume, start over, or forget — instead of reading the source and hoping. A
 * 22-minute file restarting at 0:00 because a boundary was off by one second is
 * exactly the class of bug a source-contract assertion cannot catch.
 *
 * <p>Three rules, and each of them exists because the other choice is wrong:
 *
 * <ul>
 *   <li><b>Near the end is the end.</b> A file watched to within
 *       {@link #NEAR_END_MS} of its length is finished. Nobody wants to be
 *       dropped into the last half-minute of a recording, and the credits of a
 *       video are not a place worth returning to.</li>
 *   <li><b>The head of a file is not a place.</b> Below {@link #MIN_RESUME_MS}
 *       the user is still at the start, so a record would only buy a
 *       「已续播到 0:04」 toast in front of nothing.</li>
 *   <li><b>An unknown length is not a finished file.</b> Some MPEG-TS report
 *       {@code duration = -1}; reading that as "0 seconds remaining" would throw
 *       away the place for every stream that answers that way.</li>
 * </ul>
 */
public final class PlaybackResume {

    /** Remaining time at or below which the file counts as watched to the end. */
    public static final long NEAR_END_MS = 30_000L;

    /** Below this the user is still at the head of the file. */
    public static final long MIN_RESUME_MS = 10_000L;

    /** How many files keep a place at once, newest first. */
    public static final int MAX_ENTRIES = 40;

    private PlaybackResume() {
    }

    /**
     * The identity a resume record hangs off.
     *
     * <p>The task id wins whenever there is one, because it survives a rename
     * and the URI does not: 重命名 is a real path in this app, and a record
     * keyed on the URI would be orphaned by it — the user renames a file and
     * silently loses their place. When no task id was supplied (the file was
     * opened by some other route) the URI is still better than nothing.
     */
    public static String keyFor(String taskId, String uri) {
        if (taskId != null && taskId.length() > 0 && !"null".equals(taskId)) return taskId;
        return uri == null ? "" : uri;
    }

    /** True when the file is at, near, or already past its end. */
    public static boolean isFinished(long positionMs, long durationMs) {
        // An unknown length cannot be nearly over. Treating -1 as "nothing
        // left" would discard the place for every stream that reports it.
        if (durationMs <= 0) return false;
        return durationMs - positionMs <= NEAR_END_MS;
    }

    /** Whether this position is worth writing down at all. */
    public static boolean shouldStore(long positionMs, long durationMs) {
        if (positionMs < MIN_RESUME_MS) return false;
        return !isFinished(positionMs, durationMs);
    }

    /**
     * Where playback should begin: the stored place, or 0 to start over.
     *
     * <p>The same predicate as {@link #shouldStore}, deliberately — one rule
     * with two views. A file can never be stored at a position that would not
     * be resumed, so the two cannot drift apart.
     */
    public static long startPosition(long storedMs, long durationMs) {
        return shouldStore(storedMs, durationMs) ? storedMs : 0L;
    }

    /**
     * Which keys to forget, given every key ordered newest first.
     *
     * <p>Bounded on purpose: an unbounded map of positions is a preferences
     * entry that grows with every file ever opened and is never read back.
     * Newest first, because the file someone is in the middle of is the one
     * they are most likely to come back to.
     */
    public static List<String> keysToDrop(List<String> keysNewestFirst) {
        List<String> drop = new ArrayList<String>();
        if (keysNewestFirst == null) return drop;
        for (int i = MAX_ENTRIES; i < keysNewestFirst.size(); i++) {
            drop.add(keysNewestFirst.get(i));
        }
        return drop;
    }
}
