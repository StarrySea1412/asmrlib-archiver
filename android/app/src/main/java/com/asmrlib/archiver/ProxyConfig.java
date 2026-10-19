package com.asmrlib.archiver;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * App-level HTTP proxy configuration.
 *
 * <p>The site the app reads from is geo-blocked: TCP connects fine, the TLS
 * ClientHello is reset by the firewall. A proxy (VPN, or a phone-routable
 * proxy on the user's computer) is the only path that completes the handshake.
 *
 * <p>This class persists the user's proxy host+port in shared preferences and
 * pushes them into the JVM, where {@code HttpURLConnection} (and therefore
 * the Jsoup fetches inside {@link DiscoverBridge}) picks them up
 * automatically. The WebView's network stack does <em>not</em> read JVM
 * properties; for the WebView (which loads the player iframe), the user has
 * to set the phone-wide WiFi proxy in Android Settings. The error panel
 * surfaces that hint when no proxy is configured here.
 *
 * <p>Setting an empty host or port clears the configuration.
 */
public final class ProxyConfig {

    private static final String PREFS = "asmrlib";
    private static final String KEY_HOST = "proxy_host";
    private static final String KEY_PORT = "proxy_port";

    private ProxyConfig() {}

    /** Read the configured host, or empty string if none. */
    public static String host(Context ctx) {
        if (ctx == null) return "";
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getString(KEY_HOST, "");
    }

    /** Read the configured port, or 0 if none / invalid. */
    public static int port(Context ctx) {
        if (ctx == null) return 0;
        String p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getString(KEY_PORT, "");
        if (p == null || p.isEmpty()) return 0;
        try {
            int v = Integer.parseInt(p.trim());
            return (v > 0 && v < 65536) ? v : 0;
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    /** Whether the proxy is configured and usable. */
    public static boolean isConfigured(Context ctx) {
        return !host(ctx).isEmpty() && port(ctx) > 0;
    }

    /**
     * Persist {@code host:port} (or clear when either is empty/zero) and push
     * the result into the JVM so the next network call uses it.
     */
    public static void set(Context ctx, String host, int port) {
        if (ctx == null) return;
        SharedPreferences.Editor ed = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit();
        if (host == null || host.trim().isEmpty() || port <= 0) {
            ed.remove(KEY_HOST).remove(KEY_PORT);
        } else {
            ed.putString(KEY_HOST, host.trim()).putString(KEY_PORT, String.valueOf(port));
        }
        ed.apply();
        apply(ctx);
    }

    /**
     * Push the persisted configuration into the JVM. Call this once at
     * process start (before any {@code Jsoup.connect()}) so the very first
     * fetch already honours the proxy.
     */
    public static void apply(Context ctx) {
        String h = host(ctx);
        int p = port(ctx);
        if (h.isEmpty() || p <= 0) {
            System.clearProperty("http.proxyHost");
            System.clearProperty("http.proxyPort");
            System.clearProperty("https.proxyHost");
            System.clearProperty("https.proxyPort");
            return;
        }
        System.setProperty("http.proxyHost", h);
        System.setProperty("http.proxyPort", String.valueOf(p));
        System.setProperty("https.proxyHost", h);
        System.setProperty("https.proxyPort", String.valueOf(p));
    }
}