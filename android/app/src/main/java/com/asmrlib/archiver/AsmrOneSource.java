package com.asmrlib.archiver;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.HashSet;

/**
 * The Eara-style second source: asmr.one's open JSON API, distilled into the
 * exact card / source JSON the 发现页 and play page already speak.
 *
 * <p>Zero Android imports on purpose (see HlsDownloader / PlaybackResume):
 * plain {@code java.*} plus org.json and jsoup, so {@code javac} + a JVM can
 * run the real parser against a real fixture — the whole second source rides
 * on two shapes, the virtual search URL and the track-tree walk, and both
 * fail silently (wrong page, dropped tracks) rather than crash.  Proven by
 * {@code tests/test_asmrone_java.py}.
 *
 * <p>The feed URL is a virtual {@code asmrone://search/<encoded keyword>/<page>}
 * the page composes; a work URL is {@code https://asmr.one/work/<id>}.  No
 * auth anywhere: every audio track ships a streamable {@code mediaStreamUrl}
 * that answers plain Range requests.
 */
public final class AsmrOneSource {

    static final String SCHEME = "asmrone://search/";
    /** Default API base (asmr.one). The three mirror bases share the exact
     *  same API shape — Eara ships them as one switchable site setting
     *  (its default is 200) precisely because any single domain can be
     *  unreachable from a given network; when one is blocked, another
     *  answers. Four named constants, not a derived string: the JVM test
     *  rewrites them all onto a fixture server at compile time. */
    static final String API = "https://api.asmr.one/api/";
    static final String API_100 = "https://api.asmr-100.com/api/";
    static final String API_200 = "https://api.asmr-200.com/api/";
    static final String API_300 = "https://api.asmr-300.com/api/";
    /** Site keys accepted in the virtual search URL
     *  ({@code asmrone://search/<kw>/<page>/<site>}) and mirrored to the
     *  work-page hosts the cards point at. */
    static final String SITE_ONE = "one";
    static final String SITE_100 = "100";
    static final String SITE_200 = "200";
    static final String SITE_300 = "300";
    /** An asmr.one work can ship a hundred files across format folders. */
    private static final int SOURCE_CAP = 80;
    private static final String UA =
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
            + "Chrome/120.0.0.0 Mobile Safari/537.36";

    private AsmrOneSource() {}

    /** {@code https://asmr.one/work/<id>} — the id, or empty when not one.
     *  The host is not checked on purpose: the mirrors' work pages
     *  ({@code asmr-200.com/work/...}) parse the same way, and the site key
     *  rides in the host. */
    public static String workId(String url) {
        String u = url == null ? "" : url.trim();
        int i = u.indexOf("/work/");
        if (i < 0) return "";
        String rest = u.substring(i + "/work/".length());
        int cut = rest.indexOf('?');
        if (cut >= 0) rest = rest.substring(0, cut);
        cut = rest.indexOf('#');
        if (cut >= 0) rest = rest.substring(0, cut);
        cut = rest.indexOf('/');
        if (cut >= 0) rest = rest.substring(0, cut);
        return rest.trim();
    }

    /**
     * Which mirror a URL belongs to, from its host: the work pages and the
     * virtual search URLs both carry the site in plain sight, and the API
     * base, the card URLs and the stream hosts all follow it.
     */
    static String siteFromUrl(String url) {
        String u = url == null ? "" : url.toLowerCase(java.util.Locale.US);
        if (u.contains("asmr-100.com")) return SITE_100;
        if (u.contains("asmr-200.com")) return SITE_200;
        if (u.contains("asmr-300.com")) return SITE_300;
        return SITE_ONE;
    }

    /** The API base serving {@code site} (see the constants' comment). */
    static String apiFor(String site) {
        if (SITE_100.equals(site)) return API_100;
        if (SITE_200.equals(site)) return API_200;
        if (SITE_300.equals(site)) return API_300;
        return API;
    }

    /** The human site host a work card points at for {@code site}. */
    private static String siteHost(String site) {
        if (SITE_100.equals(site)) return "asmr-100.com";
        if (SITE_200.equals(site)) return "asmr-200.com";
        if (SITE_300.equals(site)) return "asmr-300.com";
        return "asmr.one";
    }

    private static boolean isSiteKey(String value) {
        return SITE_ONE.equals(value) || SITE_100.equals(value)
            || SITE_200.equals(value) || SITE_300.equals(value);
    }

    /**
     * The asmr.one search feed: same card shape the site scrape produces, so
     * the page's grid / chips / pagination all work unchanged. The virtual
     * URL carries an optional site segment
     * ({@code asmrone://search/<kw>/<page>} = asmr.one,
     * {@code .../<page>/<site>} = a mirror); parsing from the tail keeps
     * both shapes and never mistakes an encoded keyword for a page.
     */
    public static String feed(String virtualUrl) {
        JSONObject out = new JSONObject();
        try {
            String rest = virtualUrl.substring(SCHEME.length());
            String site = SITE_ONE;
            int page = 1;
            String[] segs = rest.split("/");
            int end = segs.length;
            if (end > 0 && isSiteKey(segs[end - 1])) {
                site = segs[end - 1];
                end--;
            }
            if (end > 0 && segs[end - 1].matches("\\d+")) {
                page = Math.max(1, Integer.parseInt(segs[end - 1]));
                end--;
            }
            StringBuilder kwEnc = new StringBuilder();
            for (int i = 0; i < end; i++) {
                if (i > 0) kwEnc.append('/');
                kwEnc.append(segs[i]);
            }
            String kw;
            try {
                kw = java.net.URLDecoder.decode(kwEnc.toString(), "UTF-8").trim();
            } catch (Exception e) {
                kw = kwEnc.toString().trim();
            }
            if (kw.isEmpty()) kw = "ASMR";

            JSONObject resp = httpJson(apiFor(site) + "search/"
                + java.net.URLEncoder.encode(kw, "UTF-8")
                + "?page=" + page + "&order=release&sort=desc");
            JSONArray works = resp.optJSONArray("works");
            JSONObject pag = resp.optJSONObject("pagination");
            if (works == null) {
                return out.put("ok", false).put("error", "asmr.one 返回为空").toString();
            }

            JSONArray cards = new JSONArray();
            JSONObject tags = new JSONObject();
            for (int i = 0; i < works.length(); i++) {
                JSONObject w = works.optJSONObject(i);
                if (w == null) continue;
                String title = clean(w.optString("title", ""));
                if (title.isEmpty()) title = clean(w.optString("name", ""));
                JSONObject card = new JSONObject();
                card.put("u", "https://" + siteHost(site) + "/work/" + w.optInt("id"));
                card.put("t", title);
                card.put("c", w.optString("mainCoverUrl", "").trim());
                // Release stamps are full timestamps; a card shows the date.
                String release = w.optString("release", "");
                card.put("d", release.length() > 10 ? release.substring(0, 10) : release);
                JSONArray slugs = new JSONArray();
                JSONArray tagList = w.optJSONArray("tags");
                if (tagList != null) {
                    for (int j = 0; j < tagList.length() && j < 5; j++) {
                        JSONObject tg = tagList.optJSONObject(j);
                        String name = tg == null ? "" : clean(tg.optString("name", ""));
                        if (name.isEmpty()) continue;
                        slugs.put(name);
                        tags.put(name, tags.optInt(name, 0) + 1);
                    }
                }
                card.put("g", slugs);
                cards.put(card);
            }

            boolean hasNext = false;
            if (pag != null) {
                long seen = (long) pag.optInt("currentPage", page)
                    * pag.optInt("pageSize", 20);
                hasNext = seen < pag.optLong("totalCount", 0);
            }
            out.put("ok", true);
            out.put("page", page);
            out.put("next", hasNext ? page + 1 : 0);
            out.put("cards", cards);
            out.put("tags", tags);
            return out.toString();
        } catch (Exception e) {
            try {
                return out.put("ok", false)
                    .put("error", e.getClass().getSimpleName() + ": " + String.valueOf(e.getMessage()))
                    .toString();
            } catch (Exception ignored) {
                return "{\"ok\":false,\"error\":\"asmrone feed failed\"}";
            }
        }
    }

    /**
     * A work resolves to one {@code direct} source per playable track:
     * {@code tracks/<id>} returns a tree (format folders → files) whose
     * audio AND video leaves carry a streamable {@code mediaStreamUrl} —
     * the reference player treats both as playable media, so a work whose
     * extras are mp4 files plays them in the video page instead of being
     * silently dropped.  No Referer games — the stream hosts answer plain
     * requests.  The API base follows the mirror the work URL points at.
     */
    public static String resolve(String url, String workId) {
        JSONObject out = new JSONObject();
        try {
            String api = apiFor(siteFromUrl(url));
            JSONObject work = httpJson(api + "work/" + workId);
            String title = clean(work.optString("title", ""));
            if (title.isEmpty()) title = clean(work.optString("name", ""));
            JSONArray sources = new JSONArray();
            HashSet<String> seen = new HashSet<>();
            JSONArray tracks = httpJsonArray(apiFor(siteFromUrl(url))
                    + "tracks/" + workId + "?v=2");
            collectAudio(tracks, "", sources, seen);
            out.put("ok", true);
            out.put("url", url);
            out.put("title", title);
            out.put("sources", sources);
            return out.toString();
        } catch (Exception e) {
            try {
                return out.put("ok", false)
                    .put("error", e.getClass().getSimpleName() + ": " + String.valueOf(e.getMessage()))
                    .toString();
            } catch (Exception ignored) {
                return "{\"ok\":false,\"error\":\"asmrone resolve failed\"}";
            }
        }
    }

    /** Depth-first walk of the track tree: every audio leaf becomes a source. */
    private static void collectAudio(JSONArray nodes, String folder, JSONArray sources,
            HashSet<String> seen) throws org.json.JSONException {
        if (nodes == null || sources.length() >= SOURCE_CAP) return;
        for (int i = 0; i < nodes.length() && sources.length() < SOURCE_CAP; i++) {
            JSONObject node = nodes.optJSONObject(i);
            if (node == null) continue;
            String type = node.optString("type", "");
            if ("folder".equals(type)) {
                // Nested folders join with ' · ': a track inside wav/part1
                // carries "wav · part1" as its group — the path is kept, but
                // out of the label.
                String sub = clean(node.optString("title", ""));
                collectAudio(node.optJSONArray("children"),
                    folder.isEmpty() ? sub : folder + " · " + sub, sources, seen);
                continue;
            }
            // Audio is the default medium, but works ship mp4 extras too and
            // the reference player plays them — a leaf the page can stream
            // (hls/direct) is a leaf the queue understands.
            if (!"audio".equals(type) && !"video".equals(type)) continue;
            String stream = node.optString("mediaStreamUrl", "").trim();
            if (stream.isEmpty()) stream = node.optString("mediaDownloadUrl", "").trim();
            if (stream.isEmpty() || !seen.add(stream)) continue;
            String label = clean(node.optString("title", ""));
            int dot = label.lastIndexOf('.');
            String lower = dot < 0 ? "" : label.substring(dot).toLowerCase();
            if (dot > 0 && lower.length() <= 5 && lower.matches("\\.[a-z0-9]+")) {
                label = label.substring(0, dot);
            }
            JSONObject s = new JSONObject();
            s.put("label", label);
            // The path is data, not a name. Every consumer renders the label
            // as the name of what is playing — the now-playing title, the
            // queue row, the notification — and a breadcrumb in that slot
            // crowds the name out of the line (the play page's row wrapped to
            // five lines on a work whose folders are 「06: 特典 · 03: 闹钟音频 ·
            // 01_mp3」, and the queue row ellipsised away the only word that
            // differed). The page reads the group only where two rows would
            // otherwise be identical.
            if (!folder.isEmpty()) s.put("group", folder);
            s.put("url", stream);
            s.put("kind", "direct");
            s.put("referer", "");
            sources.put(s);
        }
    }

    /** One JSON document from the asmr.one API (rides the app proxy like Jsoup). */
    private static String httpBody(String url) throws java.io.IOException {
        return org.jsoup.Jsoup.connect(url)
            .userAgent(UA)
            .referrer(API)
            .timeout(20000)
            .ignoreContentType(true)
            .execute()
            .body();
    }

    private static JSONObject httpJson(String url) throws Exception {
        return new JSONObject(httpBody(url));
    }

    private static JSONArray httpJsonArray(String url) throws Exception {
        return new JSONArray(httpBody(url));
    }

    private static String clean(String text) {
        return text == null ? "" : text.replaceAll("\\s+", " ").trim();
    }

    /**
     * JVM test entry (never shipped logic, just the door): one mode per
     * function under test, one UTF-8 line per call on stdout — the console
     * default on Windows is GBK, so the stream is forced (see
     * {@code tests/test_asmrone_java.py}).
     */
    public static void main(String[] args) throws java.io.UnsupportedEncodingException {
        java.io.PrintStream out = new java.io.PrintStream(
            new java.io.FileOutputStream(java.io.FileDescriptor.out), true, "UTF-8");
        String mode = args.length > 0 ? args[0] : "";
        if ("workid".equals(mode)) {
            out.println(workId(args[1]));
        } else if ("feed".equals(mode)) {
            out.println(feed(args[1]));
        } else if ("resolve".equals(mode)) {
            out.println(resolve(args[1], workId(args[1])));
        }
    }
}
