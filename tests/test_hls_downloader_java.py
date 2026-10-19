"""End-to-end test for the APK's pure-Java HLS downloader.

``HlsDownloader`` deliberately imports nothing from ``android.*`` or
``org.json``, so the very same source compiles with plain ``javac`` and runs on
a normal JVM.  This test does exactly that: it serves a real HLS tree from a
local HTTP server, runs the compiled class against it, and asserts on the bytes
that land on disk.

That matters because the phone's alternative is a lie.  ``DownloadManager``
handed an ``.m3u8`` saves the playlist text and reports success; a live
playlist saved offline is a truncated file presented as a complete one;
``SAMPLE-AES`` decrypted as AES-128 is a corrupt file presented as a good one.
Every one of those failure modes is proven here by execution rather than by
reading the source, and the resume path is proven by counting what the server
was actually asked for.

Skipped when no JDK is present so the suite stays portable.
"""

from __future__ import annotations

import itertools
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from _jdk import jdk_tool

try:  # declared dependency; the AES cases skip if it is missing
    from Crypto.Cipher import AES as _AES

    _HAVE_AES = True
except Exception:  # pragma: no cover - depends on the environment
    _AES = None
    _HAVE_AES = False


JAVAC = jdk_tool("javac")
JAVA = jdk_tool("java")

_HLS_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "android"
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "asmrlib"
    / "archiver"
    / "HlsDownloader.java"
)

# Generated into the temp dir and compiled next to HlsDownloader.  Output is
# forced to UTF-8 so the Chinese failure messages survive the pipe regardless
# of the host's console encoding.
RUNNER_JAVA = r"""
import com.asmrlib.archiver.HlsDownloader;

import java.io.File;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.util.ArrayList;
import java.util.List;

/** Minimal driver: one download, one parseable result line on stdout. */
public class Runner {

    public static void main(String[] args) throws Exception {
        PrintStream out = new PrintStream(new FileOutputStream(FileDescriptor.out),
                true, "UTF-8");
        final String url = args[0];
        final File dest = new File(args[1]);
        final String spec = args.length > 2 ? args[2] : "*";
        final int cancelAfter = args.length > 3 ? Integer.parseInt(args[3]) : 0;
        // 0 means "use the production default"; anything else exercises the
        // read-timeout overload so the retry cases run in seconds, not minutes.
        final int readTimeoutMs = args.length > 4 ? Integer.parseInt(args[4]) : 0;
        final List<String> prefixes = new ArrayList<String>();
        if (!"ALL".equals(spec)) {
            for (String part : spec.split(",")) {
                if (!part.isEmpty()) prefixes.add(part);
            }
        }

        HlsDownloader.Policy policy = new HlsDownloader.Policy() {
            public boolean allow(String candidate) {
                out.println("ALLOW " + candidate);
                if ("ALL".equals(spec)) return true;
                for (String prefix : prefixes) {
                    if (candidate.startsWith(prefix)) return true;
                }
                return false;
            }
        };

        HlsDownloader.Listener listener = new HlsDownloader.Listener() {
            private int seen;

            public void onProgress(int done, int total, long bytes) {
                seen++;
                out.println("PROGRESS done=" + done + " total=" + total
                        + " bytes=" + bytes);
            }

            public boolean isCancelled() {
                return cancelAfter > 0 && seen >= cancelAfter;
            }
        };

        HlsDownloader.Result result = readTimeoutMs > 0
                ? HlsDownloader.download(url, dest, policy, listener, readTimeoutMs)
                : HlsDownloader.download(url, dest, policy, listener);
        out.println("RESULT ok=" + result.ok
                + " segments=" + result.segmentsTotal
                + " bytes=" + result.bytes
                + " error=" + (result.error == null ? "" : result.error));
        out.flush();
    }
}
"""


def _playlist(body: str) -> tuple[int, dict[str, str], bytes]:
    return 200, {"Content-Type": "application/vnd.apple.mpegurl"}, body.encode("utf-8")


def _media(body: bytes, ctype: str = "video/mp2t") -> tuple[int, dict[str, str], bytes]:
    return 200, {"Content-Type": ctype}, body


def _segment(index: int, size: int = 188) -> bytes:
    """A payload whose content encodes its own index.

    A mis-ordered or duplicated concatenation therefore cannot pass by
    accident: the sync byte keeps it shaped like MPEG-TS, and the fill byte
    makes each segment identifiable.
    """

    return bytes([0x47]) + bytes([index]) * (size - 1)


def _encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC, no padding — the HLS segment contract."""

    return _AES.new(key, _AES.MODE_CBC, iv).encrypt(data)


def _stalls_then_serves(stalls: int, payload: bytes, delay: float = 1.5):
    """A route that never answers in time for its first ``stalls`` requests.

    A transient failure has to be produced the way it happens in the field: the
    socket is open, the request was accepted, and the body simply does not
    arrive. Answering late (rather than hanging forever) keeps the server's
    worker thread finite, and the write lands on a socket the client already
    abandoned — which is exactly the case being tested.
    """

    def route(hit: int):
        if hit <= stalls:
            time.sleep(delay)
        return _media(payload)

    return route


class _HlsSite:
    """An in-memory HTTP server with a request log.

    The log is what makes "the refused host was never contacted" provable
    rather than merely asserted.
    """

    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.log: list[str] = []
        self.hits: dict[str, int] = {}
        self._lock = threading.Lock()
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                with site._lock:
                    site.log.append(self.path)
                    site.hits[self.path] = site.hits.get(self.path, 0) + 1
                    hit = site.hits[self.path]
                entry = site.routes.get(self.path)
                if callable(entry):
                    # A route may answer differently per request, which is what
                    # makes a flaky source reproducible instead of a race.
                    entry = entry(hit)
                try:
                    if entry is None:
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status, headers, body = entry
                    self.send_response(status)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if body:
                        self.wfile.write(body)
                except OSError:
                    # The client timed out and closed the socket — the whole
                    # point of the stall cases. Nobody is left to tell.
                    pass

            def log_message(self, *args) -> None:
                pass

            def handle_one_request(self) -> None:  # noqa: N802
                try:
                    super().handle_one_request()
                except (ConnectionError, OSError):
                    # The stall cases end with the client timing out and hanging
                    # up, which leaves the keep-alive socket half-open. That is
                    # the expected end of that connection, not a server fault,
                    # and letting it reach stderr would bury real failures.
                    self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def url(self, path: str) -> str:
        return self.base + path

    def count(self, path: str) -> int:
        with self._lock:
            return sum(1 for entry in self.log if entry == path)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@unittest.skipUnless(JAVAC and JAVA, "javac/java not available")
class HlsDownloaderJavaTests(unittest.TestCase):
    """Run the compiled downloader against a real HLS tree over real HTTP."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="hls-java-"))
        cls.classes = cls.tmp / "classes"
        cls.classes.mkdir()
        runner = cls.tmp / "Runner.java"
        runner.write_text(RUNNER_JAVA, encoding="utf-8")
        proc = subprocess.run(
            [JAVAC, "-encoding", "UTF-8", "-d", str(cls.classes),
             str(_HLS_SOURCE), str(runner)],
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
        self.site = _HlsSite()
        self.addCleanup(self.site.close)
        self.work = Path(tempfile.mkdtemp(prefix="hls-out-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self._counter = itertools.count()

    # ------------------------------------------------------------- helpers

    def _run(self, url: str, spec: str = "ALL", dest: Path | None = None,
             cancel_after: int = 0, read_timeout_ms: int = 0) -> dict:
        """Run one download in a fresh JVM and parse its result line."""

        if dest is None:
            dest = self.work / f"out-{next(self._counter)}.ts"
        proc = subprocess.run(
            [JAVA, "-cp", str(self.classes), "Runner", url, str(dest), spec,
             str(cancel_after), str(read_timeout_ms)],
            capture_output=True,
            timeout=180,
        )
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, f"Runner crashed:\n{stdout}\n{stderr}")
        match = re.search(
            r"^RESULT ok=(\w+) segments=(-?\d+) bytes=(-?\d+) error=(.*)$",
            stdout,
            re.MULTILINE,
        )
        self.assertIsNotNone(match, f"no RESULT line in:\n{stdout}\n{stderr}")
        return {
            "ok": match.group(1) == "true",
            "segments": int(match.group(2)),
            "bytes": int(match.group(3)),
            "error": match.group(4).strip(),
            "dest": dest,
            "stdout": stdout,
        }

    @staticmethod
    def _parts(dest: Path) -> Path:
        return Path(str(dest) + ".parts")

    # --------------------------------------------------------------- cases

    def test_master_playlist_redirect_init_and_segments_concatenate(self) -> None:
        """The happy path, with a redirect and an init segment in the way."""

        init = b"INIT-SEGMENT-BYTES"
        segments = [_segment(i) for i in range(3)]
        self.site.routes["/master.m3u8"] = (302, {"Location": "/cdn/master.m3u8"}, b"")
        self.site.routes["/cdn/master.m3u8"] = _playlist(
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
            "v/360p/index.m3u8\n"
        )
        self.site.routes["/cdn/v/360p/index.m3u8"] = _playlist(
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:4\n"
            '#EXT-X-MAP:URI="init.mp4"\n'
            "#EXTINF:4.0,\n/cdn/seg0.ts\n"
            "#EXTINF:4.0,\n/cdn/seg1.ts\n"
            "#EXTINF:4.0,\n/cdn/seg2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        self.site.routes["/cdn/v/360p/init.mp4"] = _media(init, "video/mp4")
        for index, payload in enumerate(segments):
            self.site.routes[f"/cdn/seg{index}.ts"] = _media(payload)

        result = self._run(self.site.url("/master.m3u8"))

        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["segments"], 3)
        self.assertEqual(result["dest"].read_bytes(), init + b"".join(segments))
        self.assertEqual(result["bytes"], result["dest"].stat().st_size)
        # The master 302'd, so the relative variant path had to be resolved
        # against the effective URL (/cdn/) — against the declared one it would
        # have asked for /v/360p/index.m3u8 and failed.
        self.assertEqual(self.site.count("/cdn/v/360p/index.m3u8"), 1)
        self.assertEqual(self.site.count("/v/360p/index.m3u8"), 0)
        # Progress is reported per segment, not only at the end.
        self.assertIn("PROGRESS done=1 total=3", result["stdout"])
        self.assertIn("PROGRESS done=3 total=3", result["stdout"])
        # A successful save leaves no resume material behind.
        self.assertFalse(self._parts(result["dest"]).exists())

    @unittest.skipUnless(_HAVE_AES, "pycryptodome not installed")
    def test_aes128_with_a_declared_iv_decrypts_to_the_plaintext(self) -> None:
        key = bytes(range(16))
        iv = bytes(range(16, 32))
        plain = [_segment(i, 64) for i in range(2)]
        cipher = [_encrypt(key, iv, block) for block in plain]
        self.site.routes["/aes/index.m3u8"] = _playlist(
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:4\n"
            f'#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x{iv.hex()}\n'
            "#EXTINF:4.0,\nseg0.ts\n"
            "#EXTINF:4.0,\nseg1.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        self.site.routes["/aes/key.bin"] = _media(key, "application/octet-stream")
        for index, payload in enumerate(cipher):
            self.site.routes[f"/aes/seg{index}.ts"] = _media(payload)

        result = self._run(self.site.url("/aes/index.m3u8"))

        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["dest"].read_bytes(), b"".join(plain))

    @unittest.skipUnless(_HAVE_AES, "pycryptodome not installed")
    def test_aes128_without_an_iv_uses_the_segment_index(self) -> None:
        key = bytes(range(16))
        plain = [_segment(i, 32) for i in range(3)]
        self.site.routes["/aes2/index.m3u8"] = _playlist(
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:4\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            "#EXTINF:4.0,\nseg0.ts\n"
            "#EXTINF:4.0,\nseg1.ts\n"
            "#EXTINF:4.0,\nseg2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        self.site.routes["/aes2/key.bin"] = _media(key, "application/octet-stream")
        for index, payload in enumerate(plain):
            # RFC 8216: with no IV attribute the segment sequence number is
            # the IV, big-endian in 16 bytes.
            iv = index.to_bytes(16, "big")
            self.site.routes[f"/aes2/seg{index}.ts"] = _media(
                _encrypt(key, iv, payload)
            )

        result = self._run(self.site.url("/aes2/index.m3u8"))

        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["dest"].read_bytes(), b"".join(plain))

    def test_redirect_to_a_refused_host_fails_and_is_never_requested(self) -> None:
        """An allowed host must not be able to bounce us somewhere else."""

        forbidden = _HlsSite()
        self.addCleanup(forbidden.close)
        self.site.routes["/rr/index.m3u8"] = _playlist(
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:4\n"
            "#EXTINF:4.0,\nseg0.ts\n"
            "#EXTINF:4.0,\nseg1.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        self.site.routes["/rr/seg0.ts"] = _media(_segment(0))
        self.site.routes["/rr/seg1.ts"] = (
            302, {"Location": forbidden.url("/seg1.ts")}, b""
        )
        forbidden.routes["/seg1.ts"] = _media(_segment(1))

        # Only the first origin passes the policy; the redirect target cannot.
        result = self._run(self.site.url("/rr/index.m3u8"), spec=self.site.base + "/")

        self.assertFalse(result["ok"])
        self.assertIn("安全策略", result["error"])
        self.assertEqual(forbidden.log, [], "the refused host was contacted anyway")
        self.assertFalse(result["dest"].exists())
        # The segment that did arrive is kept, so a retry resumes from it.
        parts = self._parts(result["dest"])
        self.assertTrue(parts.is_dir(), "a failure must keep the resume material")
        self.assertTrue((parts / "seg-0.part").is_file())
        self.assertFalse((parts / "seg-1.part").exists())

    def test_sample_aes_is_refused_by_name_before_any_segment_is_fetched(self) -> None:
        self.site.routes["/sa/index.m3u8"] = _playlist(
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:4\n"
            '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin"\n'
            "#EXTINF:4.0,\nseg0.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        self.site.routes["/sa/seg0.ts"] = _media(_segment(0))

        result = self._run(self.site.url("/sa/index.m3u8"))

        self.assertFalse(result["ok"])
        self.assertIn("SAMPLE-AES", result["error"])
        # Decrypting it as AES-128 would have written a corrupt file and called
        # it a success, so it must be refused before anything is downloaded.
        self.assertEqual(self.site.count("/sa/seg0.ts"), 0)
        self.assertEqual(self.site.count("/sa/key.bin"), 0)

    def test_session_key_is_refused(self) -> None:
        self.site.routes["/sk/index.m3u8"] = _playlist(
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:4\n"
            '#EXT-X-SESSION-KEY:METHOD=AES-128,URI="key.bin"\n'
            "#EXTINF:4.0,\nseg0.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        self.site.routes["/sk/seg0.ts"] = _media(_segment(0))

        result = self._run(self.site.url("/sk/index.m3u8"))

        self.assertFalse(result["ok"])
        self.assertIn("SESSION-KEY", result["error"])
        self.assertEqual(self.site.count("/sk/seg0.ts"), 0)

    def test_live_playlist_without_endlist_is_refused(self) -> None:
        self.site.routes["/live/index.m3u8"] = _playlist(
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:4\n"
            "#EXTINF:4.0,\nseg0.ts\n"
            "#EXTINF:4.0,\nseg1.ts\n"
        )
        self.site.routes["/live/seg0.ts"] = _media(_segment(0))
        self.site.routes["/live/seg1.ts"] = _media(_segment(1))

        result = self._run(self.site.url("/live/index.m3u8"))

        self.assertFalse(result["ok"])
        self.assertIn("直播", result["error"])
        # Saving a live stream yields a truncated file that would be presented
        # as a complete one, so nothing is fetched at all.
        self.assertEqual(self.site.count("/live/seg0.ts"), 0)
        self.assertFalse(result["dest"].exists())

    def test_html_segment_fails_and_then_resumes_without_refetching(self) -> None:
        """An ad interstitial where media was promised, then a real retry."""

        segments = [_segment(i) for i in range(3)]
        self.site.routes["/html/index.m3u8"] = _playlist(
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:4\n"
            "#EXTINF:4.0,\nseg0.ts\n"
            "#EXTINF:4.0,\nseg1.ts\n"
            "#EXTINF:4.0,\nseg2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        for index in range(2):
            self.site.routes[f"/html/seg{index}.ts"] = _media(segments[index])
        self.site.routes["/html/seg2.ts"] = (
            200,
            {"Content-Type": "text/html; charset=utf-8"},
            b"<!DOCTYPE html><html><body>link expired</body></html>",
        )

        first = self._run(self.site.url("/html/index.m3u8"))

        self.assertFalse(first["ok"])
        self.assertIn("返回了 HTML 而非媒体数据", first["error"])
        self.assertFalse(first["dest"].exists())
        parts = self._parts(first["dest"])
        self.assertTrue(parts.is_dir(), "a failure must keep the resume material")
        self.assertTrue((parts / "seg-0.part").is_file())
        self.assertTrue((parts / "seg-1.part").is_file())

        # Source recovers; the retry must reuse what is already on disk.
        self.site.routes["/html/seg2.ts"] = _media(segments[2])
        second = self._run(self.site.url("/html/index.m3u8"), dest=first["dest"])

        self.assertTrue(second["ok"], second["error"])
        self.assertEqual(second["dest"].read_bytes(), b"".join(segments))
        self.assertEqual(self.site.count("/html/seg0.ts"), 1)
        self.assertEqual(self.site.count("/html/seg1.ts"), 1)
        self.assertEqual(self.site.count("/html/seg2.ts"), 2)
        self.assertFalse(parts.exists(), "a successful save removes the parts dir")

    def test_html_body_is_sniffed_even_without_an_html_content_type(self) -> None:
        """A 200 is not proof of media; the leading bytes have to agree."""

        self.site.routes["/sniff/index.m3u8"] = _playlist(
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:4\n"
            "#EXTINF:4.0,\nseg0.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        self.site.routes["/sniff/seg0.ts"] = _media(
            b"  \n<!DOCTYPE html>\n<html><body>not a segment</body></html>",
            "application/octet-stream",
        )

        result = self._run(self.site.url("/sniff/index.m3u8"))

        self.assertFalse(result["ok"])
        self.assertIn("返回了 HTML 而非媒体数据", result["error"])
        self.assertFalse(result["dest"].exists())

    def test_expired_link_reports_the_status_code(self) -> None:
        for status in (403, 410):
            with self.subTest(status=status):
                path = f"/f{status}/index.m3u8"
                self.site.routes[path] = _playlist(
                    "#EXTM3U\n"
                    "#EXT-X-TARGETDURATION:4\n"
                    "#EXTINF:4.0,\nseg0.ts\n"
                    "#EXT-X-ENDLIST\n"
                )
                self.site.routes[f"/f{status}/seg0.ts"] = (
                    status, {"Content-Type": "text/plain"}, b"denied"
                )

                result = self._run(self.site.url(path))

                self.assertFalse(result["ok"])
                # An expired link is not a flaky connection, so the code and
                # the reason both have to survive into the message.
                self.assertIn(str(status), result["error"])
                self.assertIn("失效", result["error"])

    def test_master_playlist_without_a_variant_is_refused(self) -> None:
        self.site.routes["/novar/index.m3u8"] = _playlist(
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n"
        )

        result = self._run(self.site.url("/novar/index.m3u8"))

        self.assertFalse(result["ok"])
        self.assertIn("variant", result["error"])

    def test_a_response_that_is_not_a_playlist_is_not_parsed_as_one(self) -> None:
        self.site.routes["/notpl/index.m3u8"] = (
            200, {"Content-Type": "application/json"}, b'{"error":"nope"}'
        )

        result = self._run(self.site.url("/notpl/index.m3u8"))

        self.assertFalse(result["ok"])
        self.assertIn("播放列表", result["error"])

    def test_redirect_without_a_location_is_a_hard_failure(self) -> None:
        self.site.routes["/noloc/index.m3u8"] = (302, {}, b"")

        result = self._run(self.site.url("/noloc/index.m3u8"))

        self.assertFalse(result["ok"])
        self.assertIn("Location", result["error"])

    def test_cancel_aborts_cleanly_and_keeps_the_resume_material(self) -> None:
        """Cancelling must not delete the segments that already arrived."""

        self.site.routes["/cx/index.m3u8"] = _playlist(
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:4\n"
            "#EXTINF:4.0,\nseg0.ts\n"
            "#EXTINF:4.0,\nseg1.ts\n"
            "#EXTINF:4.0,\nseg2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        for index in range(3):
            self.site.routes[f"/cx/seg{index}.ts"] = _media(_segment(index))

        result = self._run(self.site.url("/cx/index.m3u8"), cancel_after=1)

        self.assertFalse(result["ok"])
        self.assertIn("已取消", result["error"])
        self.assertFalse(result["dest"].exists())
        parts = self._parts(result["dest"])
        self.assertTrue((parts / "seg-0.part").is_file())
        # The cancel flag is checked at the segment boundary, so the next
        # segment is never requested.
        self.assertEqual(self.site.count("/cx/seg1.ts"), 0)

    # -------------------------------------------------- transient retries

    def test_a_timed_out_segment_is_retried_and_the_job_finishes(self) -> None:
        """One flaky read must not strand a 139-segment job.

        This happened for real, twice, and left 877 MB on the phone with no
        file to play. The middle segment now times out twice and answers on the
        third attempt; the download has to finish, and the whole output has to
        be the real media.
        """

        segments = [_segment(i) for i in range(3)]
        self.site.routes["/rt/index.m3u8"] = _playlist(
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:4\n"
            "#EXTINF:4.0,\nseg0.ts\n"
            "#EXTINF:4.0,\nseg1.ts\n"
            "#EXTINF:4.0,\nseg2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        self.site.routes["/rt/seg0.ts"] = _media(segments[0])
        self.site.routes["/rt/seg1.ts"] = _stalls_then_serves(2, segments[1])
        self.site.routes["/rt/seg2.ts"] = _media(segments[2])

        result = self._run(self.site.url("/rt/index.m3u8"), read_timeout_ms=300)

        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["dest"].read_bytes(), b"".join(segments))
        # Three attempts, and the successful one is counted like any other: the
        # retry budget is spent on the stalled segment and nowhere else.
        self.assertEqual(self.site.count("/rt/seg1.ts"), 3)
        self.assertEqual(self.site.count("/rt/seg0.ts"), 1)
        self.assertEqual(self.site.count("/rt/seg2.ts"), 1)
        self.assertFalse(self._parts(result["dest"]).exists())

    def test_a_refused_segment_is_never_retried(self) -> None:
        """A 403 is a decision, not a flake.

        Retrying it would turn the access gate into a retry loop and would
        hammer a host that already said no.
        """

        self.site.routes["/rf/index.m3u8"] = _playlist(
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:4\n"
            "#EXTINF:4.0,\nseg0.ts\n"
            "#EXTINF:4.0,\nseg1.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        self.site.routes["/rf/seg0.ts"] = _media(_segment(0))
        self.site.routes["/rf/seg1.ts"] = (
            403, {"Content-Type": "text/plain"}, b"denied"
        )

        result = self._run(self.site.url("/rf/index.m3u8"), read_timeout_ms=300)

        self.assertFalse(result["ok"])
        self.assertIn("403", result["error"])
        self.assertIn("失效", result["error"])
        self.assertEqual(self.site.count("/rf/seg1.ts"), 1, "a 403 was retried")
        # The segment that did arrive stays, so a retry resumes from it.
        parts = self._parts(result["dest"])
        self.assertTrue((parts / "seg-0.part").is_file())
        self.assertFalse((parts / "seg-1.part").exists())

    def test_exhausted_retries_fail_and_keep_the_segments_on_disk(self) -> None:
        """Bounded means bounded: three attempts, then an honest failure.

        The point of the retry is to save the bytes that already arrived, so a
        source that never recovers must still leave the resume material alone
        and must not report success.
        """

        segments = [_segment(i) for i in range(3)]
        self.site.routes["/ex/index.m3u8"] = _playlist(
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:4\n"
            "#EXTINF:4.0,\nseg0.ts\n"
            "#EXTINF:4.0,\nseg1.ts\n"
            "#EXTINF:4.0,\nseg2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        self.site.routes["/ex/seg0.ts"] = _media(segments[0])
        self.site.routes["/ex/seg1.ts"] = _media(segments[1])
        self.site.routes["/ex/seg2.ts"] = _stalls_then_serves(999, segments[2])

        result = self._run(self.site.url("/ex/index.m3u8"), read_timeout_ms=300)

        self.assertFalse(result["ok"])
        self.assertIn("网络错误", result["error"])
        self.assertEqual(self.site.count("/ex/seg2.ts"), 3)
        self.assertFalse(result["dest"].exists())
        parts = self._parts(result["dest"])
        self.assertTrue((parts / "seg-0.part").is_file())
        self.assertTrue((parts / "seg-1.part").is_file())
        self.assertFalse((parts / "seg-2.part").exists())


if __name__ == "__main__":
    unittest.main()
