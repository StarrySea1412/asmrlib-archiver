"""End-to-end test for the APK's loopback stream proxy.

``StreamProxy`` deliberately imports nothing from ``android.*``, so the very
same source compiles with plain ``javac`` and runs on a normal JVM.  This test
does exactly that: it serves a real HLS tree plus a direct media file from a
local HTTP server, runs the compiled proxy in a fresh JVM, and plays the
role of MediaPlayer against the proxy URLs the proxy hands out.

That matters because the proxy is the whole ``应用内播放`` path.  A manifest
handed to the player unrewritten dies on its first relative segment URL; a
direct fetch without the page's Referer is refused by the player host; a
Range request mangled in transit breaks seeking.  Every one of those failure
modes is proven here by execution rather than by reading the source.

Skipped when no JDK is present so the suite stays portable.
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from _jdk import jdk_tool

JAVAC = jdk_tool("javac")
JAVA = jdk_tool("java")

_PROXY_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "android"
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "asmrlib"
    / "archiver"
    / "StreamProxy.java"
)

# Generated into the temp dir and compiled next to StreamProxy.  Output is
# forced to UTF-8 so the Chinese failure messages survive the pipe regardless
# of the host's console encoding.
RUNNER_JAVA = r"""
import com.asmrlib.archiver.StreamProxy;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.PrintStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Base64;

/** Minimal driver: one proxy interaction, parseable lines on stdout. */
public class Runner {

    public static void main(String[] args) throws Exception {
        PrintStream out = new PrintStream(new FileOutputStream(FileDescriptor.out),
                true, "UTF-8");
        String mode = args[0];
        if (mode.equals("direct") || mode.equals("manifest")) {
            String proxied = StreamProxy.register(args[1], args[2]);
            out.println("PROXIED " + proxied);
            out.println("PORT " + StreamProxy.port());
            HttpURLConnection c = (HttpURLConnection) new URL(proxied).openConnection();
            if (args.length > 3 && !args[3].equals("-")) {
                c.setRequestProperty("Range", args[3]);
            }
            c.connect();
            out.println("STATUS " + c.getResponseCode());
            String cr = c.getHeaderField("Content-Range");
            if (cr != null) out.println("CONTENT_RANGE " + cr);
            String ct = c.getHeaderField("Content-Type");
            if (ct != null) out.println("CONTENT_TYPE " + ct);
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            InputStream in = c.getInputStream();
            byte[] chunk = new byte[4096];
            int n;
            while ((n = in.read(chunk)) > 0) buf.write(chunk, 0, n);
            in.close();
            byte[] body = buf.toByteArray();
            out.println("BYTES " + body.length);
            out.println("BODY " + Base64.getEncoder().encodeToString(body));
        } else if (mode.equals("fetch")) {
            // args[1] is already a proxied URL (a rewritten dependency).
            HttpURLConnection c = (HttpURLConnection) new URL(args[1]).openConnection();
            c.connect();
            out.println("STATUS " + c.getResponseCode());
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            InputStream in = c.getInputStream();
            byte[] chunk = new byte[4096];
            int n;
            while ((n = in.read(chunk)) > 0) buf.write(chunk, 0, n);
            in.close();
            byte[] body = buf.toByteArray();
            out.println("BYTES " + body.length);
            out.println("BODY " + Base64.getEncoder().encodeToString(body));
        } else if (mode.equals("walk")) {
            // The player's whole walk in ONE JVM: register the master, read
            // it, follow the variant, then fetch every dependency the
            // rewritten variant names. The proxy instance lives inside this
            // JVM, so the walk cannot be split across Runner invocations.
            String proxied = StreamProxy.register(args[1], args[2]);
            out.println("PROXIED " + proxied);
            out.println("MANIFEST");
            out.println(readText(new URL(proxied)));
            out.println("WALK master read");
            String[] masterLines = readText(new URL(proxied)).split("\n");
            String variantUrl = null;
            for (String line : masterLines) {
                String t = line.trim();
                if (!t.isEmpty() && !t.startsWith("#")) variantUrl = t;
            }
            if (variantUrl == null) {
                out.println("WALK no-variant");
                return;
            }
            out.println("VARIANT_URL " + variantUrl);
            String vtext = readText(new URL(variantUrl));
            out.println("VARIANT");
            out.println(vtext);
            out.println("WALK variant read");
            int depIndex = 0;
            for (String line : vtext.split("\n")) {
                String t = line.trim();
                if (t.isEmpty()) continue;
                java.util.List<String> refs = new java.util.ArrayList<String>();
                if (t.startsWith("#")) {
                    int i = 0;
                    while ((i = t.indexOf("URI=\"", i)) >= 0) {
                        int from = i + "URI=\"".length();
                        int close = t.indexOf('"', from);
                        if (close < 0) break;
                        refs.add(t.substring(from, close));
                        i = close + 1;
                    }
                } else {
                    refs.add(t);
                }
                for (String ref : refs) {
                    byte[] body = readBytes(new URL(ref));
                    out.println("DEP " + depIndex
                            + " bytes=" + body.length
                            + " body=" + Base64.getEncoder().encodeToString(body));
                    depIndex++;
                }
            }
            out.println("WALK done deps=" + depIndex);
        } else if (mode.equals("lifecycle")) {
            out.println("PORT_BEFORE " + StreamProxy.port());
            out.println("OURS_BEFORE " + StreamProxy.isOurs("http://127.0.0.1:5/x"));
            String proxied = StreamProxy.register(args[1], "");
            out.println("PROXIED " + proxied);
            out.println("PORT_AFTER " + StreamProxy.port());
            out.println("OURS_AFTER " + StreamProxy.isOurs(proxied));
            StreamProxy.release();
            StreamProxy.release(); // one more than registered must be safe
        }
    }

    private static String readText(URL url) throws Exception {
        return new String(readBytes(url), "UTF-8");
    }

    private static byte[] readBytes(URL url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) url.openConnection();
        c.connect();
        if (c.getResponseCode() < 200 || c.getResponseCode() >= 300) {
            throw new IllegalStateException("HTTP " + c.getResponseCode());
        }
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        InputStream in = c.getInputStream();
        byte[] chunk = new byte[4096];
        int n;
        while ((n = in.read(chunk)) > 0) buf.write(chunk, 0, n);
        in.close();
        return buf.toByteArray();
    }
}
"""


def _segment(seed: int) -> bytes:
    """Deterministic pseudo-media bytes, unique per seed."""
    return bytes(((seed * 37 + i * 11) % 251) + 1 for i in range(4096 + seed))


class _MediaSite(BaseHTTPRequestHandler):
    """The player host the test pretends to be.

    Records the Referer of every request so the test can assert the proxy
    forwards the page identity.  Serves a direct MP4 and an HLS tree whose
    dependencies are deliberately spread across relative paths — including
    one that climbs out of the variant's directory — because a rewrite that
    only handled same-directory segments would still pass a naive fixture.
    """

    referers: dict = {}

    def log_message(self, *args) -> None:  # silence the default stderr chatter
        pass

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        self.__class__.referers[path] = self.headers.get("Referer", "")
        if path == "/media/clip.mp4":
            body = _segment(999)
        elif path == "/hls/master.m3u8":
            body = (
                b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1280000\nv/variant.m3u8\n"
            )
        elif path == "/hls/v/variant.m3u8":
            body = (
                b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n"
                b'#EXT-X-KEY:METHOD=AES-128,URI="../keys/k.bin",IV=0x00000000000000000000000000000001\n'
                b'#EXT-X-MAP:URI="init.mp4"\n'
                b"#EXTINF:4.0,\nseg-0.ts?token=abc\n"
                b"#EXTINF:4.0,\nseg-1.ts\n"
                b"#EXTINF:4.0,\n../s/seg-2.ts\n"
                b"#EXT-X-ENDLIST\n"
            )
        elif path == "/hls/keys/k.bin":
            body = bytes(range(16))
        elif path == "/hls/v/init.mp4":
            body = _segment(100)
        elif path == "/hls/v/seg-0.ts":
            body = _segment(101)
        elif path == "/hls/v/seg-1.ts":
            body = _segment(102)
        elif path == "/hls/s/seg-2.ts":
            body = _segment(103)
        else:
            self.send_error(404, "not found")
            return
        rng = self.headers.get("Range")
        status = 200
        headers = [("Content-Type", "video/mp2t" if path.endswith(".ts") else "application/octet-stream")]
        if rng:
            match = re.match(r"bytes=(\d+)-(\d+)$", rng)
            if match:
                lo, hi = int(match.group(1)), int(match.group(2))
                body = body[lo:hi + 1]
                status = 206
                headers.append(("Content-Range", f"bytes {lo}-{hi}/{len(_segment(999)) if path == '/media/clip.mp4' else len(body) + lo}"))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(JAVAC and JAVA, "javac/java not available")
class StreamProxyJavaTests(unittest.TestCase):
    """Run the compiled proxy against a real media tree over real HTTP."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="proxy-java-"))
        cls.classes = cls.tmp / "classes"
        cls.classes.mkdir()
        runner = cls.tmp / "Runner.java"
        runner.write_text(RUNNER_JAVA, encoding="utf-8")
        proc = subprocess.run(
            [JAVAC, "-encoding", "UTF-8", "-d", str(cls.classes),
             str(_PROXY_SOURCE), str(runner)],
            capture_output=True,
        )
        if proc.returncode != 0:  # pragma: no cover - build failure
            raise AssertionError(
                "javac failed:\n"
                + proc.stdout.decode("utf-8", "replace")
                + proc.stderr.decode("utf-8", "replace")
            )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self.site = _MediaSite
        self.site.referers = {}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _MediaSite)
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        # addCleanup runs LIFO: stop the loop first, close the socket second,
        # or the loop's select dies on the closed socket (WinError 10038).
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.port}"

    def _run(self, *args: str) -> dict:
        proc = subprocess.run(
            [JAVA, "-cp", str(self.classes), "Runner", *args],
            capture_output=True,
            timeout=120,
        )
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, f"Runner crashed:\n{stdout}\n{stderr}")
        return {"stdout": stdout, "stderr": stderr}

    @staticmethod
    def _parse(stdout: str) -> dict:
        out = {}
        body = []
        in_body = False
        for line in stdout.splitlines():
            if line.startswith("BODY "):
                in_body = True
                body.append(line[5:])
                continue
            if in_body:
                body.append(line)
                continue
            key, _, value = line.partition(" ")
            out[key] = value
        if body:
            out["body"] = base64.b64decode("".join(body))
        return out

    @staticmethod
    def _parse_walk(stdout: str) -> dict:
        """Extract the walk runner's blocks: manifest text, variant text,
        and every dependency body it fetched."""
        blocks = {"MANIFEST": [], "VARIANT": []}
        current = None
        deps = []
        for line in stdout.splitlines():
            if line == "MANIFEST":
                current = "MANIFEST"
                blocks[current] = []
            elif line == "VARIANT":
                current = "VARIANT"
                blocks[current] = []
            elif line.startswith("WALK ") or line.startswith("VARIANT_URL "):
                current = None
            elif line.startswith("DEP "):
                deps.append(base64.b64decode(line.split(" body=")[1]))
            elif current is not None:
                blocks[current].append(line)
        return {"manifest": "\n".join(blocks["MANIFEST"]),
                "variant": "\n".join(blocks["VARIANT"]),
                "deps": deps}

    # ------------------------------------------------------------- helpers

    def _proxied(self, result: dict) -> str:
        self.assertIn("PROXIED", result)
        return result["PROXIED"]

    # --------------------------------------------------------------- cases

    def test_direct_media_streams_with_referer_and_range(self) -> None:
        """A direct file: verbatim bytes, the page's Referer, honest 206s."""
        result = self._parse(self._run("direct", self.base + "/media/clip.mp4",
                                       self.base + "/posts/the-work")["stdout"])
        proxied = self._proxied(result)
        self.assertTrue(proxied.startswith("http://127.0.0.1:"))
        self.assertIn("/m?u=", proxied)
        self.assertEqual(int(result["STATUS"]), 200)
        self.assertEqual(result["body"], _segment(999))
        # The page identity rode along: without it the player host refuses.
        self.assertEqual(self.site.referers["/media/clip.mp4"],
                         self.base + "/posts/the-work")

        ranged = self._parse(self._run("direct", self.base + "/media/clip.mp4",
                                       self.base + "/posts/the-work",
                                       "bytes=100-199")["stdout"])
        self.assertEqual(int(ranged["STATUS"]), 206)
        self.assertEqual(ranged["body"], _segment(999)[100:200])
        self.assertIn("CONTENT_RANGE", ranged)

    def test_loopback_upstream_never_rides_the_jvm_proxy(self) -> None:
        """A 127.0.0.1 upstream must connect directly even when the JVM
        carries a proxy that would black-hole it — the phone's global proxy
        lands in the app's JVM properties (that is how the app process gets
        it), and a proxy asked to dial ITS OWN 127.0.0.1 goes nowhere.

        Port 9 is the discard port: nothing is listening, so any fetch that
        rides the "proxy" fails instantly instead of hanging the test.  Two
        bypass layers are exercised here: the Runner's own fetch of the proxy
        URL (saved by the http.nonProxyHosts the proxy installs at bind) and
        the proxy's upstream fetch (saved by the loopback check in open()).
        """
        proc = subprocess.run(
            [JAVA,
             "-Dhttp.proxyHost=127.0.0.1", "-Dhttp.proxyPort=9",
             "-Dhttps.proxyHost=127.0.0.1", "-Dhttps.proxyPort=9",
             "-cp", str(self.classes), "Runner",
             "direct", self.base + "/media/clip.mp4",
             self.base + "/posts/the-work"],
            capture_output=True,
            timeout=120,
        )
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, f"Runner crashed:\n{stdout}\n{stderr}")
        result = self._parse(stdout)
        self.assertEqual(int(result["STATUS"]), 200)
        self.assertEqual(result["body"], _segment(999))

    def test_manifest_is_rewritten_and_dependencies_come_back_proxied(self) -> None:
        """Every dependency leaves the manifest as a proxied absolute URL."""
        result = self._parse(self._run("manifest", self.base + "/hls/master.m3u8",
                                       self.base + "/posts/the-work")["stdout"])
        manifest = result["body"].decode()
        self.assertIn("#EXTM3U", manifest)
        self.assertIn("#EXT-X-STREAM-INF", manifest)
        self.assertIn("BANDWIDTH=1280000", manifest)  # tag payload preserved
        # No relative or upstream URL survives; everything is re-wrapped.
        # Java's URLEncoder maps ':'→%3A and '/'→%2F, so the wrapped source
        # URL is fully escaped.
        import urllib.parse
        for line in manifest.splitlines():
            if not line or line.startswith("#"):
                continue
            self.assertTrue(line.startswith("http://127.0.0.1:"),
                            f"unrewritten dependency line: {line}")
            self.assertIn("/m?u=", line)
            self.assertIn("u=" + urllib.parse.quote(self.base, safe=""), line)

    def test_rewritten_variant_serves_every_dependency_with_its_bytes(self) -> None:
        """The player's real walk: master → variant → segments, key, init.

        One Runner invocation (walk mode) does the whole walk inside the JVM
        that owns the proxy: a fresh JVM would find no proxy listening.
        """
        out = self._parse_walk(self._run("walk", self.base + "/hls/master.m3u8",
                                         self.base + "/posts/the-work")["stdout"])
        manifest = out["manifest"]
        self.assertIn("#EXTM3U", manifest)
        self.assertIn("#EXT-X-STREAM-INF", manifest)
        self.assertIn("BANDWIDTH=1280000", manifest)  # tag payload preserved
        # No relative or upstream URL survives; everything is re-wrapped.
        # Java's URLEncoder maps ':'→%3A and '/'→%2F, so the wrapped source
        # URL is fully escaped.
        import urllib.parse
        for line in manifest.splitlines():
            if not line or line.startswith("#"):
                continue
            self.assertTrue(line.startswith("http://127.0.0.1:"),
                            f"unrewritten dependency line: {line}")
            self.assertIn("/m?u=", line)
            self.assertIn("u=" + urllib.parse.quote(self.base, safe=""), line)

        vtext = out["variant"]
        self.assertIn("#EXT-X-KEY", vtext)
        self.assertIn("METHOD=AES-128", vtext)
        self.assertIn("IV=0x00000000000000000000000000000001", vtext)

        deps = [line for line in vtext.splitlines()
                if line and not line.startswith("#")]
        self.assertEqual(len(deps), 3, f"expected 3 segments, got: {deps}")
        key_uris = re.findall(r'URI="([^"]+)"', vtext)
        self.assertEqual(len(key_uris), 2, f"key + map URIs expected: {key_uris}")

        # Every DEP line the runner fetched must match the bytes the site
        # serves for that dependency, query strings and directory climbs
        # included.
        expected = [bytes(range(16)),          # the AES key
                    _segment(100),             # init map
                    _segment(101),             # seg-0 (had a query string)
                    _segment(102),             # seg-1
                    _segment(103)]             # seg-2 (outside the variant dir)
        self.assertEqual(out["deps"], expected,
                         "dependency bytes wrong through the rewritten manifest")

        # The key request is an upstream fetch too — it must carry the Referer.
        self.assertEqual(self.site.referers["/hls/keys/k.bin"],
                         self.base + "/posts/the-work")
        self.assertEqual(self.site.referers["/hls/s/seg-2.ts"],
                         self.base + "/posts/the-work")

    def test_lifecycle_registers_and_survives_extra_release(self) -> None:
        """register starts the listener; release is idempotent and safe."""
        out = self._parse(self._run("lifecycle", self.base + "/media/clip.mp4")["stdout"])
        self.assertEqual(int(out["PORT_BEFORE"]), 0)
        self.assertEqual(out["OURS_BEFORE"], "false")
        self.assertNotEqual(int(out["PORT_AFTER"]), 0)
        self.assertEqual(out["OURS_AFTER"], "true")
        self.assertTrue(out["PROXIED"].startswith(
            f"http://127.0.0.1:{out['PORT_AFTER']}/m?u="))

    def test_manifest_extension_with_query_string_still_rewrites(self) -> None:
        """A signed manifest URL (?token=...) is still recognised as HLS."""
        result = self._parse(self._run("manifest",
                                       self.base + "/hls/master.m3u8?token=s1gN",
                                       self.base + "/posts/the-work")["stdout"])
        manifest = result["body"].decode()
        self.assertIn("#EXTM3U", manifest)
        for line in manifest.splitlines():
            if line and not line.startswith("#"):
                self.assertIn("/m?u=", line)


if __name__ == "__main__":
    unittest.main()
