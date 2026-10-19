package com.asmrlib.archiver;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Pattern;

import javax.crypto.Cipher;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * Segment-aware, resumable HLS downloader for the APK shell.
 *
 * <p>{@code DownloadManager} cannot save an HLS stream: handed an {@code .m3u8}
 * it stores the playlist text and reports success, which is a fake success.
 * This class does the real work — resolve a master playlist to its variant,
 * refuse what cannot be saved faithfully (live streams, unsupported
 * encryption), fetch every dependency through a caller-supplied policy with
 * redirects resolved by hand, and concatenate the segments into one file.
 *
 * <p>It deliberately imports nothing from {@code android.*} or {@code org.json}
 * so the very same source compiles with plain {@code javac} and runs on a
 * normal JVM. That is what lets {@code tests/test_hls_downloader_java.py}
 * prove every failure mode below by execution instead of by inspection.
 *
 * <p>Mirrors the desktop implementation in {@code src/asmrlib_archiver/
 * media_save.py} ({@code _get}, {@code _fetch_playlist}, {@code download_hls}).
 */
public final class HlsDownloader {

    /** Access gate consulted before every fetch — redirect hops included. */
    public interface Policy {
        boolean allow(String url);
    }

    /** Progress and cancellation. A null listener means "never cancelled". */
    public interface Listener {
        void onProgress(int segmentsDone, int segmentsTotal, long bytesDone);

        boolean isCancelled();
    }

    /**
     * Outcome of one attempt. {@code ok} is only ever true when the output
     * file exists on disk and is non-empty — bytes written, not bytes promised.
     */
    public static final class Result {
        public final boolean ok;
        public final String error;
        public final int segmentsTotal;
        public final long bytes;

        Result(boolean ok, String error, int segmentsTotal, long bytes) {
            this.ok = ok;
            this.error = error == null ? "" : error;
            this.segmentsTotal = segmentsTotal;
            this.bytes = bytes;
        }
    }

    private static final int MAX_REDIRECTS = 5;
    private static final int MAX_VARIANT_HOPS = 5;
    private static final int CONNECT_TIMEOUT_MS = 15000;
    private static final int READ_TIMEOUT_MS = 30000;
    /** Attempts per segment, including the first one. */
    private static final int SEGMENT_ATTEMPTS = 3;
    /** Pause before attempt 2, then before attempt 3. */
    private static final int[] RETRY_BACKOFF_MS = {300, 900};
    private static final int KEY_LENGTH = 16;
    private static final String USER_AGENT = "ASMRLibAndroid/1.0 (+local personal archive)";
    private static final Pattern HTML_BODY_RE =
            Pattern.compile("^\\s*<(?:!doctype|html)\\b", Pattern.CASE_INSENSITIVE);

    private HlsDownloader() {
    }

    /** Internal control-flow failure; the message is user-facing. */
    private static class HlsError extends Exception {
        HlsError(String message) {
            super(message);
        }
    }

    /**
     * The only failure worth another attempt: the socket dropped, the read
     * timed out, the connection was reset mid-body.
     *
     * <p>It extends {@link HlsError} so every existing {@code catch (HlsError)}
     * site keeps working, and it is thrown from exactly one place — the
     * {@code IOException} branch of {@link #fetch}. A policy refusal, a
     * redirect-policy refusal and a non-2xx status are all raised as plain
     * {@code HlsError} and therefore never enter the retry loop: retrying
     * those would turn the access gate into a retry loop.
     */
    private static final class TransientError extends HlsError {
        TransientError(String message) {
            super(message);
        }
    }

    private static final class Response {
        final String contentType;
        final byte[] body;
        final String url;

        Response(String contentType, byte[] body, String url) {
            this.contentType = contentType;
            this.body = body;
            this.url = url;
        }
    }

    /** A media playlist plus everything needed to fetch and reassemble it. */
    private static final class Playlist {
        final String url;
        final List<String> segments = new ArrayList<String>();
        String keyUrl;
        byte[] keyIv;
        String initUrl;

        Playlist(String url) {
            this.url = url;
        }
    }

    /**
     * Download {@code url} and write the concatenated segments to {@code dest}.
     *
     * <p>Segments are cached in {@code <dest>.parts/seg-<index>.part} as they
     * arrive, so a retry after a failure resumes instead of restarting. The
     * parts directory is removed only once {@code dest} has been written and
     * verified; on any failure it is left in place.
     */
    public static Result download(String url, File dest, Policy policy, Listener listener) {
        return download(url, dest, policy, listener, READ_TIMEOUT_MS);
    }

    /**
     * Same as {@link #download(String, File, Policy, Listener)} but with an
     * explicit per-read timeout. The timeout is a parameter rather than a
     * static field on purpose: two downloads run concurrently, so a shared
     * mutable setting would leak from one job into the other.
     */
    public static Result download(String url, File dest, Policy policy, Listener listener,
            int readTimeoutMs) {
        String start = url == null ? "" : url.trim();
        if (start.isEmpty()) return new Result(false, "无效的视频地址", 0, 0L);
        if (dest == null) return new Result(false, "无效的保存路径", 0, 0L);
        int readTimeout = readTimeoutMs > 0 ? readTimeoutMs : READ_TIMEOUT_MS;

        Playlist playlist;
        try {
            playlist = resolvePlaylist(start, policy, readTimeout);
        } catch (HlsError e) {
            return new Result(false, e.getMessage(), 0, 0L);
        }

        int total = playlist.segments.size();
        File partsDir = new File(dest.getAbsolutePath() + ".parts");
        File partial = new File(dest.getAbsolutePath() + ".part");
        long bytes = 0L;
        try {
            File parent = dest.getParentFile();
            if (parent != null && !parent.isDirectory() && !parent.mkdirs()) {
                throw new HlsError("无法创建保存目录");
            }
            if (!partsDir.isDirectory() && !partsDir.mkdirs()) {
                throw new HlsError("无法创建续传目录");
            }

            byte[] init = new byte[0];
            if (playlist.initUrl != null) {
                init = fetchBody(playlist.initUrl, policy, "初始化分段", readTimeout);
                bytes += init.length;
            }

            byte[] key = new byte[0];
            if (playlist.keyUrl != null) {
                byte[] raw = fetchBody(playlist.keyUrl, policy, "密钥", readTimeout);
                if (raw.length != KEY_LENGTH) {
                    throw new HlsError("HLS 密钥长度异常（应为 16 字节）");
                }
                key = raw;
            }

            for (int index = 0; index < total; index++) {
                if (listener != null && listener.isCancelled()) {
                    // Abort cleanly: the parts already on disk are the resume
                    // material for the next attempt.
                    throw new HlsError("已取消");
                }
                File part = new File(partsDir, "seg-" + index + ".part");
                byte[] data;
                if (part.isFile() && part.length() > 0) {
                    data = readFile(part);
                } else {
                    byte[] body = fetchSegment(playlist.segments.get(index), policy,
                            readTimeout, listener);
                    data = key.length > 0 ? decrypt(body, key, ivFor(playlist, index)) : body;
                    writeFile(part, data);
                }
                bytes += data.length;
                if (listener != null) {
                    listener.onProgress(index + 1, total, bytes);
                }
            }

            concat(partial, init, partsDir, total);
            if (dest.exists() && !dest.delete()) {
                throw new HlsError("无法覆盖已存在的文件");
            }
            if (!partial.renameTo(dest)) {
                throw new HlsError("无法写入保存文件");
            }
            if (!dest.isFile() || dest.length() <= 0L) {
                throw new HlsError("保存文件为空");
            }
            deleteTree(partsDir);
            return new Result(true, "", total, dest.length());
        } catch (HlsError e) {
            partial.delete();
            return new Result(false, e.getMessage(), total, bytes);
        } catch (Exception e) {
            partial.delete();
            return new Result(false, "保存失败：" + e, total, bytes);
        }
    }

    // ------------------------------------------------------------- playlist

    /**
     * Fetch the playlist at {@code url}, following a master playlist down to
     * its variant. Returns the media playlist with its effective (post-redirect)
     * URL, which is the base every relative dependency must be resolved against.
     */
    private static Playlist resolvePlaylist(String url, Policy policy, int readTimeoutMs)
            throws HlsError {
        String current = url;
        for (int hop = 0; hop <= MAX_VARIANT_HOPS; hop++) {
            if (policy != null && !policy.allow(current)) {
                throw new HlsError("地址未通过安全策略：" + current);
            }
            Response response = fetch(current, policy, readTimeoutMs);
            rejectHtml(response.body, response.contentType, "播放列表");
            String text = new String(response.body, StandardCharsets.UTF_8);
            if (text.indexOf("#EXTM3U") < 0) {
                throw new HlsError("不是有效的 HLS 播放列表");
            }
            String variant = firstVariant(text);
            if (variant == null) {
                return parsePlaylist(text, response.url);
            }
            current = resolve(response.url, variant);
        }
        throw new HlsError("master playlist 层级过多");
    }

    /**
     * The variant URI of a master playlist, or null when this is already a
     * media playlist. A master with no usable variant is a hard failure: there
     * is nothing to download and pretending otherwise would be a fake success.
     */
    private static String firstVariant(String text) throws HlsError {
        String[] lines = text.split("\n");
        boolean isMaster = false;
        for (int i = 0; i < lines.length; i++) {
            if (!lines[i].trim().startsWith("#EXT-X-STREAM-INF")) continue;
            isMaster = true;
            for (int j = i + 1; j < lines.length; j++) {
                String next = lines[j].trim();
                if (next.isEmpty() || next.startsWith("#")) continue;
                return next;
            }
        }
        if (isMaster) throw new HlsError("master playlist 没有可用的 variant");
        return null;
    }

    private static Playlist parsePlaylist(String text, String baseUrl) throws HlsError {
        Playlist playlist = new Playlist(baseUrl);
        boolean endList = false;
        for (String raw : text.split("\n")) {
            String line = raw.trim();
            if (line.isEmpty()) continue;
            if (line.startsWith("#EXT-X-SESSION-KEY")) {
                // Session keys are commonly SAMPLE-AES / DRM. Decrypting them
                // as AES-128 writes garbage that would be reported as success.
                throw new HlsError("HLS 加密方式不受支持：EXT-X-SESSION-KEY");
            }
            if (line.startsWith("#EXT-X-KEY")) {
                Map<String, String> attrs = attributes(line);
                String method = value(attrs, "METHOD").toUpperCase(Locale.US);
                if (method.isEmpty() || "NONE".equals(method)) {
                    playlist.keyUrl = null;
                    playlist.keyIv = null;
                } else if ("AES-128".equals(method)) {
                    String uri = value(attrs, "URI");
                    if (uri.isEmpty()) throw new HlsError("HLS key 行缺少 URI");
                    playlist.keyUrl = resolve(baseUrl, uri);
                    playlist.keyIv = parseIv(value(attrs, "IV"));
                } else {
                    // SAMPLE-AES / SAMPLE-AES-CTR / anything unknown is not
                    // AES-128-CBC. Refuse by name instead of writing a corrupt
                    // file and calling it a successful save.
                    throw new HlsError("HLS 加密方式不受支持：" + method);
                }
                continue;
            }
            if (line.startsWith("#EXT-X-MAP")) {
                String uri = value(attributes(line), "URI");
                if (!uri.isEmpty()) playlist.initUrl = resolve(baseUrl, uri);
                continue;
            }
            if (line.startsWith("#EXT-X-ENDLIST")) {
                endList = true;
                continue;
            }
            if (line.startsWith("#")) continue;
            playlist.segments.add(resolve(baseUrl, line));
        }
        if (!endList) {
            // A live playlist keeps growing; saving it yields a truncated file
            // that would be presented to the user as a complete one.
            throw new HlsError("这是直播流（缺少 EXT-X-ENDLIST），无法离线保存");
        }
        if (playlist.segments.isEmpty()) {
            throw new HlsError("播放列表中没有媒体分段");
        }
        return playlist;
    }

    /** Parse {@code #EXT-X-KEY:METHOD=AES-128,URI="k.bin"} style attributes. */
    private static Map<String, String> attributes(String tag) {
        Map<String, String> out = new LinkedHashMap<String, String>();
        int colon = tag.indexOf(':');
        if (colon < 0) return out;
        String rest = tag.substring(colon + 1);
        int i = 0;
        while (i < rest.length()) {
            int eq = rest.indexOf('=', i);
            if (eq < 0) break;
            String name = rest.substring(i, eq).trim();
            int p = eq + 1;
            String raw;
            if (p < rest.length() && rest.charAt(p) == '"') {
                int close = rest.indexOf('"', p + 1);
                if (close < 0) {
                    raw = rest.substring(p + 1);
                    p = rest.length();
                } else {
                    raw = rest.substring(p + 1, close);
                    p = close + 1;
                }
            } else {
                int comma = rest.indexOf(',', p);
                if (comma < 0) {
                    raw = rest.substring(p);
                    p = rest.length();
                } else {
                    raw = rest.substring(p, comma);
                    p = comma;
                }
            }
            if (!name.isEmpty()) out.put(name.toUpperCase(Locale.US), raw.trim());
            if (p >= rest.length()) break;
            i = p + 1;
        }
        return out;
    }

    private static String value(Map<String, String> attrs, String name) {
        String v = attrs.get(name);
        return v == null ? "" : v;
    }

    /**
     * A declared IV that is not 16 valid hex bytes is a hard failure: silently
     * falling back to the sequence number would decrypt to garbage and report
     * that garbage as a successful save.
     */
    private static byte[] parseIv(String hex) throws HlsError {
        if (hex == null || hex.trim().isEmpty()) return null;
        String h = hex.trim();
        if (h.startsWith("0x") || h.startsWith("0X")) h = h.substring(2);
        if (h.length() != KEY_LENGTH * 2) {
            throw new HlsError("HLS IV 长度异常（应为 16 字节）");
        }
        byte[] out = new byte[KEY_LENGTH];
        for (int i = 0; i < out.length; i++) {
            int hi = Character.digit(h.charAt(i * 2), 16);
            int lo = Character.digit(h.charAt(i * 2 + 1), 16);
            if (hi < 0 || lo < 0) throw new HlsError("HLS IV 不是合法的十六进制");
            out[i] = (byte) ((hi << 4) | lo);
        }
        return out;
    }

    /** The declared IV, else the segment sequence number as a 16-byte BE value. */
    private static byte[] ivFor(Playlist playlist, int index) {
        if (playlist.keyIv != null) return playlist.keyIv;
        byte[] iv = new byte[KEY_LENGTH];
        long value = index;
        for (int i = KEY_LENGTH - 1; i >= 0 && value != 0L; i--) {
            iv[i] = (byte) (value & 0xFFL);
            value >>>= 8;
        }
        return iv;
    }

    private static byte[] decrypt(byte[] data, byte[] key, byte[] iv) throws HlsError {
        try {
            Cipher cipher = Cipher.getInstance("AES/CBC/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"),
                    new IvParameterSpec(iv));
            return cipher.doFinal(data);
        } catch (Exception e) {
            // NoPadding means a segment that is not block-aligned cannot be
            // decrypted; passing the ciphertext through would corrupt the file.
            throw new HlsError("AES-128 解密失败：" + e.getMessage());
        }
    }

    // ----------------------------------------------------------------- http

    private static byte[] fetchBody(String url, Policy policy, String what, int readTimeoutMs)
            throws HlsError {
        if (policy != null && !policy.allow(url)) {
            throw new HlsError(what + "地址未通过安全策略：" + url);
        }
        Response response = fetch(url, policy, readTimeoutMs);
        rejectHtml(response.body, response.contentType, what);
        return response.body;
    }

    /**
     * Fetch one media segment, retrying only the failures that another attempt
     * can actually fix.
     *
     * <p>A 139-segment job used to die on a single {@code SocketTimeoutException},
     * stranding 877 MB that had already been written. The bytes are the expensive
     * part; the retry is bounded at {@link #SEGMENT_ATTEMPTS} attempts with
     * {@link #RETRY_BACKOFF_MS} pauses so a genuinely dead host still fails in
     * seconds rather than being hammered.
     *
     * <p>Only {@link TransientError} is caught. A policy refusal, a
     * redirect-policy refusal and a non-2xx status arrive as plain
     * {@code HlsError} and propagate on the first attempt: retrying a refusal
     * would turn the access gate into a retry loop, and retrying a 403 would
     * only repeat a decision the server already made.
     *
     * <p>The cancel flag is re-read between attempts, so a cancelled job stops
     * at the pause instead of finishing out its retry budget.
     */
    private static byte[] fetchSegment(String url, Policy policy, int readTimeoutMs,
            Listener listener) throws HlsError {
        HlsError last = null;
        for (int attempt = 0; attempt < SEGMENT_ATTEMPTS; attempt++) {
            if (attempt > 0) {
                if (listener != null && listener.isCancelled()) {
                    throw new HlsError("已取消");
                }
                backoff(RETRY_BACKOFF_MS[attempt - 1]);
            }
            try {
                return fetchBody(url, policy, "分段", readTimeoutMs);
            } catch (TransientError e) {
                // The only retryable outcome. Remember it in case this was the
                // last attempt, and keep the reason the user will be shown.
                last = e;
            }
        }
        throw last;
    }

    /** Pause between two segment attempts, waking early for a cancellation. */
    private static void backoff(int millis) throws HlsError {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException e) {
            // Re-arm the flag so the caller's thread still learns it was
            // interrupted, and report the same outcome as an explicit cancel.
            Thread.currentThread().interrupt();
            throw new HlsError("已取消");
        }
    }

    /**
     * GET {@code url} without letting the JDK follow redirects behind our back.
     * An auto-followed redirect is an access-control bypass — the policy only
     * ever saw the declared URL — so every hop is resolved and re-checked
     * before it is requested, and the hop count is capped.
     *
     * <p>{@code readTimeoutMs} is per call, not a static field: two downloads
     * run concurrently and a shared setting would leak between jobs.
     */
    private static Response fetch(String url, Policy policy, int readTimeoutMs)
            throws HlsError {
        int readTimeout = readTimeoutMs > 0 ? readTimeoutMs : READ_TIMEOUT_MS;
        String current = url;
        for (int hop = 0; hop <= MAX_REDIRECTS; hop++) {
            HttpURLConnection conn = null;
            try {
                conn = (HttpURLConnection) new URL(current).openConnection();
                conn.setInstanceFollowRedirects(false);
                conn.setConnectTimeout(CONNECT_TIMEOUT_MS);
                conn.setReadTimeout(readTimeout);
                conn.setRequestProperty("User-Agent", USER_AGENT);
                conn.setRequestProperty("Accept", "*/*");
                int code = conn.getResponseCode();
                if (code >= 300 && code < 400) {
                    String location = conn.getHeaderField("Location");
                    if (location == null || location.trim().isEmpty()) {
                        throw new HlsError("重定向缺少 Location：" + current);
                    }
                    String target = resolve(current, location.trim());
                    if (policy != null && !policy.allow(target)) {
                        throw new HlsError("重定向目标未通过安全策略：" + target);
                    }
                    current = target;
                    continue;
                }
                if (code < 200 || code >= 300) {
                    throw httpFailure(code);
                }
                byte[] body = readAll(conn.getInputStream());
                String contentType = conn.getContentType();
                return new Response(contentType == null ? "" : contentType, body, current);
            } catch (HlsError e) {
                throw e;
            } catch (IOException e) {
                // The single origin of a retryable failure: the socket dropped,
                // the read timed out, the connection reset mid-body. Every other
                // failure in this method is raised as a plain HlsError above.
                throw new TransientError("网络错误：" + e);
            } finally {
                if (conn != null) conn.disconnect();
            }
        }
        throw new HlsError("重定向次数过多：" + url);
    }

    /**
     * Non-2xx is a failure. A 403/410 on a media dependency means the link
     * expired or was refused — a different problem from a flaky connection,
     * so it says so and carries the code.
     */
    private static HlsError httpFailure(int code) {
        if (code == 403 || code == 410) {
            return new HlsError("媒体链接已失效或被拒绝（HTTP " + code + "）");
        }
        if (code == 404) return new HlsError("媒体地址不存在（HTTP 404）");
        return new HlsError("媒体请求失败（HTTP " + code + "）");
    }

    /**
     * A 200 carrying HTML is an error page or an ad interstitial, never media.
     * Appending it to the output is the fake-success failure mode, so it is
     * rejected on either signal: the declared type, or the leading bytes.
     */
    private static void rejectHtml(byte[] body, String contentType, String what) throws HlsError {
        String ctype = contentType == null ? "" : contentType.toLowerCase(Locale.US);
        boolean declared = ctype.contains("text/html") || ctype.contains("application/xhtml");
        boolean sniffed = false;
        if (body != null && body.length > 0) {
            String head = new String(body, 0, Math.min(body.length, 512), StandardCharsets.UTF_8);
            sniffed = HTML_BODY_RE.matcher(head).find();
        }
        if (declared || sniffed) {
            throw new HlsError(what + "返回了 HTML 而非媒体数据");
        }
    }

    /** Resolve a possibly-relative reference against the current URL. */
    private static String resolve(String base, String reference) {
        String ref = reference == null ? "" : reference.trim();
        if (ref.isEmpty()) return base;
        try {
            return new URL(new URL(base), ref).toString();
        } catch (Exception e) {
            return ref;
        }
    }

    private static byte[] readAll(InputStream in) throws IOException {
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            return out.toByteArray();
        } finally {
            try {
                in.close();
            } catch (IOException ignored) {
                // nothing useful to do with a failed close of a read stream
            }
        }
    }

    // --------------------------------------------------------------- output

    /** Concatenate the init segment and every part, in order, into one file. */
    private static void concat(File partial, byte[] init, File partsDir, int total)
            throws HlsError {
        try {
            OutputStream out = new FileOutputStream(partial, false);
            try {
                if (init.length > 0) out.write(init);
                byte[] buf = new byte[16384];
                for (int index = 0; index < total; index++) {
                    File part = new File(partsDir, "seg-" + index + ".part");
                    if (!part.isFile()) throw new HlsError("续传分段缺失：" + part.getName());
                    InputStream in = new FileInputStream(part);
                    try {
                        int n;
                        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                    } finally {
                        in.close();
                    }
                }
                out.flush();
            } finally {
                out.close();
            }
        } catch (HlsError e) {
            throw e;
        } catch (IOException e) {
            throw new HlsError("写入保存文件失败：" + e);
        }
    }

    private static byte[] readFile(File file) throws HlsError {
        try {
            return readAll(new FileInputStream(file));
        } catch (IOException e) {
            throw new HlsError("读取续传分段失败：" + e);
        }
    }

    private static void writeFile(File file, byte[] data) throws HlsError {
        try {
            OutputStream out = new FileOutputStream(file, false);
            try {
                out.write(data);
                out.flush();
            } finally {
                out.close();
            }
        } catch (IOException e) {
            throw new HlsError("写入续传分段失败：" + e);
        }
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
}
