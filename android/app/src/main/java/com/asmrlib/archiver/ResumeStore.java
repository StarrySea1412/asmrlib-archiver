package com.asmrlib.archiver;

import android.content.SharedPreferences;
import android.util.Log;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.Iterator;
import java.util.List;

/**
 * The resume store's shared IO: one JSON object of {@code key -> {p, d, at}}
 * in the shell's one preferences file, written by the player activity AND the
 * sleep-handoff service. Two implementations of this store would drift — the
 * service's record would stop being readable by the page or vice versa, and a
 * queue that walks through both owners would silently lose its place — so the
 * IO lives here once and both sides call it.
 *
 * <p>The <em>policy</em> (what belongs in the store, when a place is spent)
 * stays in {@link PlaybackResume}, which is pure Java and executable in a
 * test; this class is only the thin IO around it.
 */
final class ResumeStore {

    private static final String TAG = "asmrlib-resume";

    /** One JSON object of {@code key -> {p, d, at}}; the policy that decides
     *  what goes in it is {@link PlaybackResume}, which is pure Java. */
    static final String KEY = "playback.resume";

    private ResumeStore() {}

    /** {@code {positionMs, durationMs}} of the record, or null when there is none. */
    static long[] read(SharedPreferences prefs, String key) {
        if (prefs == null || key == null || key.isEmpty()) return null;
        JSONObject all = readStore(prefs);
        if (all == null) return null;
        JSONObject row = all.optJSONObject(key);
        if (row == null) return null;
        long position = row.optLong("p", 0L);
        if (position <= 0) return null;
        return new long[]{position, row.optLong("d", -1L)};
    }

    /**
     * Write the place, bounded to {@link PlaybackResume#MAX_ENTRIES} newest.
     *
     * <p>A file that never prepared is the caller's to skip: this layer has
     * no position to judge, and clearing on a caller's behalf would wipe a
     * record that an earlier, working session wrote.
     */
    static void store(SharedPreferences prefs, String key,
            long positionMs, long durationMs) {
        if (prefs == null || key == null || key.isEmpty()) return;
        JSONObject all = readStore(prefs);
        if (all == null) return;
        try {
            JSONObject row = new JSONObject();
            row.put("p", positionMs);
            row.put("d", durationMs);
            row.put("at", System.currentTimeMillis());
            all.put(key, row);
            trim(all);
        } catch (JSONException e) {
            Log.w(TAG, "could not write the resume record", e);
            return;
        }
        prefs.edit().putString(KEY, all.toString()).apply();
    }

    /** Forget one place outright — the file was watched to its end. */
    static void drop(SharedPreferences prefs, String key) {
        if (prefs == null || key == null || key.isEmpty()) return;
        JSONObject all = readStore(prefs);
        if (all == null || all.remove(key) == null) return;
        prefs.edit().putString(KEY, all.toString()).apply();
    }

    /**
     * The whole map, or {@code null} when the stored text cannot be parsed.
     *
     * <p>Null rather than an empty map, and every caller returns early on it:
     * an unreadable entry must never be rewritten as an empty one, because one
     * bad read would then cost every stored place at once. The same rule the
     * download centre's task store follows, for the same reason. The bad text
     * is kept under a separate key rather than being overwritten.
     */
    private static JSONObject readStore(SharedPreferences prefs) {
        String raw = prefs.getString(KEY, "");
        if (raw == null || raw.isEmpty()) return new JSONObject();
        try {
            return new JSONObject(raw);
        } catch (JSONException e) {
            Log.w(TAG, "resume store unreadable, leaving it alone", e);
            prefs.edit().putString(KEY + ".bad", raw).apply();
            return null;
        }
    }

    /**
     * Keep the map bounded, newest first.
     *
     * <p>The ordering lives here because it needs the {@code at} stamps; how
     * many to keep lives in {@link PlaybackResume} so that the policy stays
     * pure Java and therefore executable in a test.
     */
    private static void trim(final JSONObject all) {
        List<String> keys = new ArrayList<String>();
        for (Iterator<String> it = all.keys(); it.hasNext(); ) keys.add(it.next());
        Collections.sort(keys, new Comparator<String>() {
            @Override
            public int compare(String a, String b) {
                return Long.compare(stampOf(all, b), stampOf(all, a));
            }
        });
        for (String key : PlaybackResume.keysToDrop(keys)) all.remove(key);
    }

    private static long stampOf(JSONObject all, String key) {
        JSONObject row = all.optJSONObject(key);
        return row == null ? 0L : row.optLong("at", 0L);
    }
}
