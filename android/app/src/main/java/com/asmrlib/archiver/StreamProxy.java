package com.asmrlib.archiver;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URL;
import java.net.URLDecoder;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * A loopback proxy that lets {@code MediaPlayer} stream what only this app's
 * HTTP code can reach.
 *
 * <p>Two facts make it necessary. First, a player host only answers the
 * client that asked for the page: media requests must carry the page's
 * {@code Referer} (and a browser-shaped {@code User-Agent}), which
 * {@code MediaPlayer} cannot attach to its own connections. Second, the site
 * is TLS-blocked from this network, so outbound fetches must ride the proxy
 * the user configured in {@link ProxyConfig} — and {@code MediaPlayer} reads
 * neither JVM system properties nor per-connection headers. So the player is
 * pointed at {@code http://127.0.0.1:<port>/} and this class does the real
 * fetching, with the right headers, through the right route.
 *
 * <p>Two request shapes are served:
 *
 * <ul>
 *   <li><b>Direct media</b> — {@code /m?u=<encoded>}: bytes are piped through
 *       with the client's {@code Range} header forwarded and the upstream
 *       {@code Content-Range} passed back, so seeks land as range requests
 *       the way a local file would.</li>
 *   <li><b>HLS</b> — a manifest request is fetched and rewritten: every
 *       dependency (variant, segment, key, init segment) is resolved to an
 *       absolute URL and re-wrapped as another {@code /m?u=...} request. The
 *       player therefore never sees a URL it cannot fetch, and stays unaware
 *       of HLS entirely — it just keeps fetching what look like ordinary
 *       media files. Segments are still piped through as direct media, so
 *       the decoder's seeks stay cheap.</li>
 * </ul>
 *
 * <p>Like {@link HlsDownloader}, this file imports nothing from
 * {@code android.*}: the same source compiles with plain {@code javac}, which
 * is what lets {@code tests/test_stream_proxy_java.py} serve a real HLS tree
 * and assert on the bytes that come back out of the proxy.
 */
public final class StreamProxy {

    /** One source's request identity: the page it was resolved from. */
    private static final class Route {
        final String referer;
        Route(String referer) {
            this.referer = referer == null ? "" : referer;
        }
    }

    private static final int CONNECT_TIMEOUT_MS = 15000;
    private static final int READ_TIMEOUT_MS = 30000;
    private static final String BROWSER_UA =
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
            + "Chrome/120.0.0.0 Mobile Safari/537.36";

    private static final Object LOCK = new Object();
    private static StreamProxy instance;
    /** Armed when the last reference drops; cancelled by the next acquire. */
    private static java.util.Timer idleTimer;
    /** Long enough for the sleep-handoff's startService to be delivered, and
     *  short enough that a dead proxy does not linger between works. */
    private static final long IDLE_SHUTDOWN_MS = 5_000L;
    /**
     * Failure sink, injected by the host (which CAN talk to
     * {@code android.util.Log} — this file stays framework-free). Without it
     * a device-side proxy failure is a 30-second silence: the player sees a
     * closed socket, the log sees nothing, and the bug is undiagnosable from
     * a screenshot.
     */
    private static volatile Sink sink;

    /** One line of proxy failure context, per failure. */
    public interface Sink {
        void log(String message);
    }

    public static void setSink(Sink s) {
        sink = s;
    }

    private static void fail(String message) {
        Sink s = sink;
        if (s != null) s.log(message);
        recent.addLast(message);
        while (recent.size() > 6) recent.pollFirst();
    }

    /**
     * The last few proxy failures, joined — this is what the player's error
     * panel shows on a device nobody has adb attached to. A bare MediaPlayer
     * error reads as (1, -2147483648) for a refused upstream, an expired
     * signed URL and a proxy black hole alike; these lines are the only way
     * to tell them apart on the screen the user is looking at.
     */
    public static String recentFailures() {
        StringBuilder out = new StringBuilder();
        for (String line : recent) {
            if (out.length() > 0) out.append('\n');
            out.append(line);
        }
        return out.toString();
    }

    private static final java.util.Deque<String> recent =
            new java.util.concurrent.ConcurrentLinkedDeque<>();

    private final ServerSocket server;
    private final Thread acceptLoop;
    private final AtomicBoolean running = new AtomicBoolean();
    /**
     * Referer per proxied URL, keyed by the exact {@code 127.0.0.1} string
     * the player was handed. Segment URLs register themselves when their
     * manifest is rewritten, so a segment request that races the player's
     * fetch order still finds its route.
     */
    private final Map<String, Route> routes = new ConcurrentHashMap<>();
    private final AtomicInteger refs = new AtomicInteger();
    /** The port actually bound; set in the constructor, before any serving. */
    private final int port;

    private StreamProxy() throws IOException {
        // Port 0 = let the OS pick a free one; a fixed port would collide
        // with whatever else the device happens to be running. The address
        // is pinned to IPv4 127.0.0.1 — the one the proxied URL advertises —
        // because getLoopbackAddress() answers ::1 on Android, and a player
        // dialing 127.0.0.1 against a ::1-only listener reads as connection
        // refused (observed live: netstat showed ::1:<port> LISTEN, the
        // media stack's 127.0.0.1 dials all failed).
        this.server = new ServerSocket(0, 64,
                InetAddress.getByName("127.0.0.1"));
        this.port = server.getLocalPort();
        this.acceptLoop = new Thread(this::acceptLoop, "asmrlib-stream-proxy");
        this.acceptLoop.setDaemon(true);
    }

    /**
     * The JVM inherits the phone's global http_proxy into {@code
     * http.proxyHost} at bind time, and that application drops the standard
     * loopback bypass — so the media stack's own fetch of THIS proxy's
     * loopback URL gets routed through the phone's proxy node, where
     * {@code 127.0.0.1:<port>} is somebody else's loopback: a black hole
     * that eats the player's 30-second timeout (observed live). Restoring
     * the standard {@code http.nonProxyHosts} bypass fixes it at the root:
     * external hosts keep riding the proxy, loopback never leaves the
     * device.
     */
    static void ensureLoopbackBypass() {
        String current = System.getProperty("http.nonProxyHosts");
        if (current == null || !current.contains("127.")) {
            System.setProperty("http.nonProxyHosts", "localhost|127.*|[::1]");
        }
    }

    /**
     * Wrap one media URL for playback. Returns the loopback URL to hand the
     * player, and remembers {@code referer} as the request identity for it
     * and (once a manifest is rewritten) for every dependency behind it.
     */
    public static String register(String sourceUrl, String referer) throws IOException {
        if (sourceUrl == null || sourceUrl.trim().isEmpty()) {
            throw new IOException("空媒体地址");
        }
        ensureLoopbackBypass();
        StreamProxy proxy = acquire();
        // Keyed by the SOURCE url, not the proxied string: the request the
        // player sends decodes back to the source URL, and serve() looks the
        // route up under exactly that key.
        proxy.routes.put(sourceUrl.trim(), new Route(referer));
        return proxy.proxiedUrl(sourceUrl.trim());
    }

    /** One more user of the proxy; the instance lives until released. */
    private static StreamProxy acquire() throws IOException {
        synchronized (LOCK) {
            if (idleTimer != null) {
                idleTimer.cancel();
                idleTimer.purge();
                idleTimer = null;
            }
            if (instance == null) instance = new StreamProxy();
            instance.refs.incrementAndGet();
            instance.ensureRunning();
            return instance;
        }
    }

    /**
     * Hold the proxy open for a consumer that was handed an already-registered
     * proxied URL (the 息屏播放 service). Every call must be paired with one
     * {@link #release()}.
     */
    public static void retain() throws IOException {
        ensureLoopbackBypass();
        acquire();
    }

    /**
     * Drop one reference. The proxy does not stop the instant the count
     * reaches zero: the 息屏播放 handoff releases the activity's reference
     * and the service acquires its own asynchronously, and closing in
     * between would cut off the audio mid-word. So the last release only
     * arms a short idle timer; an acquire inside the window (or a third
     * party still holding a reference) keeps the proxy alive.
     */
    public static void release() {
        StreamProxy proxy;
        synchronized (LOCK) {
            proxy = instance;
        }
        if (proxy == null) return;
        if (proxy.refs.updateAndGet(current -> Math.max(0, current - 1)) > 0) return;
        synchronized (LOCK) {
            if (instance != proxy) return;
            if (idleTimer != null) idleTimer.cancel();
            idleTimer = new java.util.Timer("asmrlib-proxy-idle", true);
            idleTimer.schedule(new java.util.TimerTask() {
                @Override public void run() {
                    synchronized (LOCK) {
                        if (instance == proxy && proxy.refs.get() <= 0) {
                            proxy.shutdown();
                            instance = null;
                            idleTimer = null;
                        }
                    }
                }
            }, IDLE_SHUTDOWN_MS);
        }
    }

    /** The bound port, for tests; 0 when nothing is running. */
    public static int port() {
        synchronized (LOCK) {
            return instance == null ? 0 : instance.port;
        }
    }

    /**
     * Whether {@code url} points at this proxy — the shape of the loopback
     * URL {@link #register} returns. PlaybackService uses it to know it is
     * playing a stream and must hold a reference.
     */
    public static boolean isOurs(String url) {
        if (url == null || !url.startsWith("http://127.0.0.1:")) return false;
        synchronized (LOCK) {
            if (instance == null) return false;
            String prefix = "http://127.0.0.1:" + instance.port + "/";
            return url.startsWith(prefix);
        }
    }

    private String proxiedUrl(String url) {
        try {
            return "http://127.0.0.1:" + port + "/m?u="
                + URLEncoder.encode(url, "UTF-8");
        } catch (Exception e) {
            // Encoding a URL string with UTF-8 cannot throw; kept for the
            // same shape HlsDownloader's resolve() fallback has.
            return "http://127.0.0.1:" + port + "/m?u=" + url;
        }
    }

    private synchronized void ensureRunning() {
        if (running.get()) return;
        running.set(true);
        acceptLoop.start();
    }

    private void shutdown() {
        running.set(false);
        try {
            server.close();
        } catch (IOException ignored) {
            // closing twice is fine
        }
        routes.clear();
    }

    // ------------------------------------------------------------ accept loop

    private void acceptLoop() {
        while (running.get()) {
            Socket socket;
            try {
                socket = server.accept();
            } catch (IOException e) {
                if (running.get()) continue; // transient; keep serving
                return;                      // shutdown closed the socket
            }
            Thread worker = new Thread(() -> serve(socket), "asmrlib-proxy-conn");
            worker.setDaemon(true);
            worker.start();
        }
    }

    // ---------------------------------------------------------------- serving

    /** One HTTP request from the player, proxied to its upstream. */
    private void serve(Socket socket) {
        try (Socket s = socket) {
            s.setTcpNoDelay(true);
            Request request = Request.read(s);
            if (request == null) return;
            String url = request.param("u");
            if (url == null || url.isEmpty()) {
                respondError(s, 400, "missing u");
                return;
            }
            Route route = routes.get(url);
            if (route == null) {
                // Not registered: still proxyable (a rewritten segment URL
                // can land before its route entry in a pathological order),
                // just without the page's Referer.
                route = new Route("");
            }
            if (isManifest(url)) {
                serveManifest(s, url, route);
            } else {
                serveDirect(s, url, route, request.header("Range"));
            }
        } catch (Exception e) {
            // The player routinely walks away mid-transfer (seek, teardown);
            // a broken pipe here is the normal end of most responses.
            try { socket.close(); } catch (IOException ignored) { }
        }
    }

    /**
     * Whether {@code url} is an HLS manifest, by extension. A manifest has to
     * be rewritten before it reaches the player; everything else streams
     * verbatim.
     */
    static boolean isManifest(String url) {
        String path = url == null ? "" : url.toLowerCase(Locale.US);
        int cut = path.indexOf('?');
        if (cut >= 0) path = path.substring(0, cut);
        cut = path.indexOf('#');
        if (cut >= 0) path = path.substring(0, cut);
        return path.endsWith(".m3u8");
    }

    // ------------------------------------------------------------- direct pipe

    private void serveDirect(Socket s, String url, Route route, String range) {
        HttpURLConnection conn = null;
        try {
            conn = open(url, route);
            if (range != null && !range.isEmpty()) {
                conn.setRequestProperty("Range", range);
            }
            int code = conn.getResponseCode();
            if (code < 200 || code >= 300) {
                fail("upstream " + url + " -> HTTP " + code);
                respondError(s, code, "upstream " + code);
                return;
            }
            // A range request answered with the whole file (200, not 206) is
            // still playable — the status is passed through as-is and the
            // player handles both.
            String contentType = conn.getContentType();
            long length = conn.getContentLengthLong();
            String contentRange = conn.getHeaderField("Content-Range");
            String crlf = "\r\n";
            StringBuilder head = new StringBuilder(192);
            head.append("HTTP/1.1 ").append(code).append(" OK").append(crlf);
            head.append("Content-Type: ")
                .append(contentType == null ? "application/octet-stream" : contentType)
                .append(crlf);
            if (length >= 0) head.append("Content-Length: ").append(length).append(crlf);
            if (contentRange != null) {
                head.append("Content-Range: ").append(contentRange).append(crlf);
            }
            head.append("Accept-Ranges: bytes").append(crlf);
            head.append("Connection: close").append(crlf).append(crlf);
            OutputStream out = s.getOutputStream();
            out.write(head.toString().getBytes(StandardCharsets.US_ASCII));
            out.flush();
            pipe(conn.getInputStream(), out);
            out.flush();
        } catch (Exception e) {
            // Player walked away (normal) or upstream died mid-body; either
            // way the connection is done. MediaPlayer re-opens on its own —
            // and the sink records which of the two this was.
            fail("upstream " + url + " failed: " + e);
            try { s.close(); } catch (IOException ignored) { }
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    // --------------------------------------------------------------- HLS pass

    /**
     * Fetch a manifest, rewrite every dependency to a proxied absolute URL,
     * and hand the rewritten text to the player as an ordinary file.
     */
    private void serveManifest(Socket s, String url, Route route) {
        try {
            HttpURLConnection conn = open(url, route);
            int code;
            String text;
            try {
                code = conn.getResponseCode();
                if (code < 200 || code >= 300) {
                    respondError(s, code, "upstream " + code);
                    return;
                }
                text = new String(readAll(conn.getInputStream()), StandardCharsets.UTF_8);
            } finally {
                conn.disconnect();
            }
            if (text.indexOf("#EXTM3U") < 0) {
                respondError(s, 502, "not a playlist");
                return;
            }
            // A master playlist redirects the player to a variant; the variant
            // URL must be registered before the player requests it, which the
            // rewrite below does by construction.
            byte[] out = rewriteManifest(text, url, route).getBytes(StandardCharsets.UTF_8);
            respondBytes(s, 200, "application/vnd.apple.mpegurl", out);
        } catch (Exception e) {
            fail("manifest " + url + " failed: " + e);
            respondError(s, 502, "manifest fetch failed");
        }
    }

    /**
     * The rewrite. Every non-comment line in a media playlist is a segment
     * URI; every {@code URI="..."} attribute (key, init map) is a dependency.
     * Both become absolute against the manifest's own URL, then get re-wrapped
     * so the player's next request comes back here with the same route.
     * Comment lines without URIs pass through untouched — tags like
     * {@code #EXTINF} carry durations the player must still see.
     */
    private String rewriteManifest(String text, String baseUrl, Route route) {
        StringBuilder out = new StringBuilder(text.length() + 256);
        for (String raw : text.split("\n", -1)) {
            String line = raw.trim();
            if (line.isEmpty()) {
                out.append('\n');
            } else if (line.startsWith("#")) {
                out.append(rewriteTagUris(raw, baseUrl, route)).append('\n');
            } else {
                out.append(rewriteDependency(line, baseUrl, route)).append('\n');
            }
        }
        return out.toString();
    }

    /**
     * Tags can carry quoted URIs ({@code #EXT-X-KEY:...URI="k.key"},
     * {@code #EXT-X-MAP:...URI="init.mp4"}). Only the quoted strings behind a
     * {@code URI=} are rewritten; everything else is byte-preserved.
     */
    private String rewriteTagUris(String line, String baseUrl, Route route) {
        if (!line.contains("URI=\"")) return line;
        StringBuilder out = new StringBuilder(line.length() + 128);
        int i = 0;
        while (i < line.length()) {
            int at = line.indexOf("URI=\"", i);
            if (at < 0) {
                out.append(line, i, line.length());
                break;
            }
            int valueStart = at + "URI=\"".length();
            out.append(line, i, valueStart);
            int close = line.indexOf('"', valueStart);
            if (close < 0) {
                out.append(line, valueStart, line.length());
                break;
            }
            out.append(rewriteDependency(line.substring(valueStart, close), baseUrl, route));
            out.append('"');
            i = close + 1;
        }
        return out.toString();
    }

    /** Absolute + re-wrapped form of one reference found in a manifest. */
    private String rewriteDependency(String reference, String baseUrl, Route route) {
        String absolute = resolve(baseUrl, reference);
        routes.put(absolute, route);
        return proxiedUrl(absolute);
    }

    // ----------------------------------------------------------------- http io

    /** Upstream connection with this app's identity headers and proxy route. */
    private static HttpURLConnection open(String url, Route route) throws IOException {
        URL target = new URL(url);
        HttpURLConnection conn;
        if (isLoopback(target.getHost())) {
            // A loopback upstream must bypass the JVM/system proxy: the app
            // process inherits the phone's global http_proxy into its JVM
            // properties, and a proxy node trying to reach ITS OWN
            // 127.0.0.1 is a black hole that eats the player's read timeout.
            // (External hosts keep riding the proxy — that is the whole
            // point: the site is unreachable without it.)
            conn = (HttpURLConnection) target.openConnection(java.net.Proxy.NO_PROXY);
        } else {
            conn = (HttpURLConnection) target.openConnection();
        }
        conn.setConnectTimeout(CONNECT_TIMEOUT_MS);
        conn.setReadTimeout(READ_TIMEOUT_MS);
        conn.setInstanceFollowRedirects(true);
        conn.setRequestProperty("User-Agent", BROWSER_UA);
        conn.setRequestProperty("Accept", "*/*");
        if (!route.referer.isEmpty()) {
            conn.setRequestProperty("Referer", route.referer);
        }
        return conn;
    }

    /**
     * True when {@code host} is a loopback address (127.0.0.1, ::1, localhost).
     *
     * <p>This is a pure string check on purpose: it runs once per upstream
     * request, ahead of the connect, and a DNS lookup here would stall every
     * segment fetch on exactly the networks this proxy exists for — the
     * device's resolver is not proxied, so resolving a geo-blocked CDN name
     * from the device can hang for tens of seconds (observed live: the
     * player's 30 s budget died waiting inside this call).
     */
    static boolean isLoopback(String host) {
        if (host == null || host.isEmpty()) return false;
        String h = host.trim().toLowerCase(Locale.US);
        if (h.equals("localhost") || h.endsWith(".localhost")) return true;
        if (h.equals("::1") || h.equals("[::1]") || h.equals("0:0:0:0:0:0:0:1")) return true;
        if (h.startsWith("127.")) return true;
        return false;
    }

    /** The player's request: first line plus headers, read as raw ASCII. */
    private static final class Request {
        final String line;
        final Map<String, String> headers = new ConcurrentHashMap<>();

        private Request(String line) {
            this.line = line;
        }

        /** Read one request head, or null when the bytes are not a request. */
        static Request read(Socket s) throws IOException {
            InputStream in = s.getInputStream();
            StringBuilder head = new StringBuilder(512);
            int prev = -1;
            int c;
            while ((c = in.read()) >= 0) {
                head.append((char) c);
                if (prev == '\r' && c == '\n') {
                    // Blank line ends the head; it is part of head and split
                    // off below.
                    if (head.length() >= 4 && head.charAt(head.length() - 4) == '\r'
                            && head.charAt(head.length() - 3) == '\n') {
                        break;
                    }
                }
                prev = c;
                if (head.length() > 32768) return null;
            }
            if (head.length() == 0) return null;
            String[] lines = head.toString().split("\r\n");
            if (lines.length == 0 || !lines[0].startsWith("GET ")) return null;
            Request request = new Request(lines[0]);
            for (int i = 1; i < lines.length; i++) {
                int colon = lines[i].indexOf(':');
                if (colon <= 0) continue;
                request.headers.put(lines[i].substring(0, colon).trim().toLowerCase(Locale.US),
                        lines[i].substring(colon + 1).trim());
            }
            return request;
        }

        String header(String name) {
            return headers.get(name.toLowerCase(Locale.US));
        }

        /** The decoded value of {@code ?name=} in the request line. */
        String param(String name) {
            int q = line.indexOf('?');
            if (q < 0) return null;
            String query = line.substring(q + 1);
            int end = query.indexOf(' ');
            if (end >= 0) query = query.substring(0, end);
            for (String pair : query.split("&")) {
                int eq = pair.indexOf('=');
                if (eq <= 0) continue;
                if (name.equals(pair.substring(0, eq))) {
                    try {
                        return URLDecoder.decode(pair.substring(eq + 1), "UTF-8");
                    } catch (Exception e) {
                        return pair.substring(eq + 1);
                    }
                }
            }
            return null;
        }
    }

    private static void respondBytes(Socket s, int code, String contentType, byte[] body)
            throws IOException {
        String crlf = "\r\n";
        StringBuilder head = new StringBuilder(160);
        head.append("HTTP/1.1 ").append(code).append(" OK").append(crlf);
        head.append("Content-Type: ").append(contentType).append(crlf);
        head.append("Content-Length: ").append(body.length).append(crlf);
        head.append("Connection: close").append(crlf).append(crlf);
        OutputStream out = s.getOutputStream();
        out.write(head.toString().getBytes(StandardCharsets.US_ASCII));
        out.write(body);
        out.flush();
    }

    private static void respondError(Socket s, int code, String message) {
        try {
            byte[] body = message.getBytes(StandardCharsets.UTF_8);
            String crlf = "\r\n";
            StringBuilder head = new StringBuilder(160);
            head.append("HTTP/1.1 ").append(code).append(" ").append(message).append(crlf);
            head.append("Content-Type: text/plain; charset=utf-8").append(crlf);
            head.append("Content-Length: ").append(body.length).append(crlf);
            head.append("Connection: close").append(crlf).append(crlf);
            OutputStream out = s.getOutputStream();
            out.write(head.toString().getBytes(StandardCharsets.US_ASCII));
            out.write(body);
            out.flush();
            s.close();
        } catch (IOException ignored) {
            // player already gone
        }
    }

    private static void pipe(InputStream in, OutputStream out) throws IOException {
        byte[] buf = new byte[16384];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
    }

    private static byte[] readAll(InputStream in) throws IOException {
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            return out.toByteArray();
        } finally {
            try { in.close(); } catch (IOException ignored) { }
        }
    }

    /** Resolve a possibly-relative reference against a base URL. */
    static String resolve(String base, String reference) {
        String ref = reference == null ? "" : reference.trim();
        if (ref.isEmpty()) return base;
        try {
            return new URL(new URL(base), ref).toString();
        } catch (Exception e) {
            return ref;
        }
    }
}
