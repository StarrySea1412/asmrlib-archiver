"""End-to-end test for the asmr.one source (``AsmrOneSource``).

``AsmrOneSource`` deliberately imports nothing from ``android.*`` (see its
class comment): plain ``java.*`` plus org.json and jsoup, so the very same
source compiles with ``javac`` and runs on a normal JVM.  This test does
exactly that: it serves the slices of api.asmr.one the source touches from a
local HTTP server, rewrites the API constant to point at it, and executes
the real parser against the fixture.

That matters because the whole second source rides on two shapes that fail
silently rather than crash: a mis-parsed page number queries the wrong page,
and a track-tree walk that only handled one nesting level would drop every
track inside a format folder (the API nests them two deep in the wild).
Both are proven here by execution, not by reading the source.

Skipped when no JDK (or the SDK stub jar) is present so the suite stays
portable.
"""

from __future__ import annotations

import json
import os
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

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "android"
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "asmrlib"
    / "archiver"
    / "AsmrOneSource.java"
)

# jsoup is the source's HTTP client; org.json ships as a real (non-stub)
# jar under tests/vendor — android.jar's org.json is compile-stubs that
# throw "Stub!" at runtime, so the JVM run needs the genuine artifact.
# The classpath separator is the one thing that differs per host OS.
_CP_SEP = ";" if os.name == "nt" else ":"
_JSOUP_GLOB = Path("D:/gradle-home/caches/modules-2/files-2.1/org.jsoup")
_JSON_JAR = Path(__file__).resolve().parent / "vendor" / "json-20240303.jar"


def _work(id: int, title: str) -> dict:
    return {
        "id": id,
        "source_id": f"RJ{id:08d}",
        "title": title,
        "release": "2026-08-09 00:00:00",
        "mainCoverUrl": f"https://api.asmr.one/api/cover/{id}.jpg?type=main",
        "tags": [{"name": "掏耳"}, {"name": "耳舐め"}],
    }


class _AsmrOneApi(BaseHTTPRequestHandler):
    """The slices of api.asmr.one the source touches: search and tracks."""

    def log_message(self, *args) -> None:  # silence stderr chatter
        pass

    def do_GET(self) -> None:
        if self.path.startswith("/api/search/"):
            body = json.dumps({
                "works": [_work(111, "最初の作品"), _work(222, "第二の作品")],
                "pagination": {"currentPage": 2, "pageSize": 20, "totalCount": 45},
            }).encode("utf-8")
            self._send(body)
        elif self.path.startswith("/api/work/111"):
            self._send(json.dumps({"id": 111, "title": "最初の作品"}).encode("utf-8"))
        elif self.path.startswith("/api/tracks/111"):
            tree = [
                {"type": "folder", "title": "wav", "children": [
                    {"type": "folder", "title": "part1", "children": [
                        {"type": "audio", "title": "01_あさ.wav",
                         "mediaStreamUrl": "http://cdn.example/stream/01.wav",
                         "mediaDownloadUrl": "http://cdn.example/dl/01.wav"},
                    ]},
                    {"type": "audio", "title": "02_ひる.flac",
                     "mediaStreamUrl": "http://cdn.example/stream/02.flac"},
                ]},
                {"type": "audio", "title": "03_よる.mp3",
                 "mediaStreamUrl": "http://cdn.example/stream/03.mp3"},
                {"type": "video", "title": "04_おまけ.mp4",
                 "mediaStreamUrl": "http://cdn.example/stream/04.mp4"},
                {"type": "image", "title": "cover.png"},
            ]
            self._send(json.dumps(tree).encode("utf-8"))
        else:
            self.send_error(404)

    def _send(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AsmrOneSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not (JAVAC and JAVA):
            raise unittest.SkipTest("no JDK available")
        jsoup = sorted(_JSOUP_GLOB.rglob("jsoup-*.jar"))
        if not jsoup or not _JSON_JAR.exists():
            raise unittest.SkipTest("jsoup jar / org.json jar not found")
        cls.cp = str(jsoup[0]) + _CP_SEP + str(_JSON_JAR)

        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        base = Path(cls.tmp.name)
        pkg = base / "com" / "asmrlib" / "archiver"
        pkg.mkdir(parents=True)
        target = pkg / "AsmrOneSource.java"
        shutil.copy(_SOURCE, target)
        # Point the source's API constant at the fixture server: the constant
        # is a single literal, so a compile-time rewrite is enough.
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _AsmrOneApi)
        cls.addClassCleanup(cls.httpd.server_close)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        text = target.read_text(encoding="utf-8")
        # All four API bases (asmr.one + the three mirrors) point at the
        # fixture: the mirror choice must be provable end to end, not just
        # the default host. The constants are api.<host> — host here is the
        # bare site domain.
        for constant, api_host in (("API", "api.asmr.one"),
                                   ("API_100", "api.asmr-100.com"),
                                   ("API_200", "api.asmr-200.com"),
                                   ("API_300", "api.asmr-300.com")):
            text = text.replace(
                f'static final String {constant} = "https://{api_host}/api/";',
                f'static final String {constant} = '
                f'"http://127.0.0.1:{cls.httpd.server_address[1]}/api/";')
        target.write_text(text, encoding="utf-8")

        proc = subprocess.run(
            [JAVAC, "-encoding", "UTF-8",
             "-cp", cls.cp,
             "-d", str(base / "classes"), str(target)],
            capture_output=True, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(
                "javac failed:\n"
                + proc.stdout.decode(errors="replace")
                + "\n" + proc.stderr.decode(errors="replace"))
        cls.classes = base / "classes"

    def _run(self, *args: str) -> str:
        proc = subprocess.run(
            [JAVA, "-Dfile.encoding=UTF-8",
             "-cp", str(self.classes) + _CP_SEP + self.cp,
             "com.asmrlib.archiver.AsmrOneSource", *args],
            capture_output=True, timeout=60)
        out = proc.stdout.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0,
                         f"run failed:\n{out}\n{proc.stderr.decode('utf-8', 'replace')}")
        return out

    def test_work_id_parser(self) -> None:
        self.assertEqual(self._run("workid", "https://asmr.one/work/1652997").strip(),
                         "1652997")
        self.assertEqual(self._run("workid", "https://asmr.one/work/123?x=1").strip(),
                         "123")
        # A site post URL is not a work URL: empty means "not mine".
        self.assertEqual(self._run("workid", "https://asmrlib.com/posts/abc").strip(),
                         "")

    def test_search_feed_parses_cards_and_pagination(self) -> None:
        data = json.loads(self._run("feed", "asmrone://search/%E6%8E%8F%E8%80%B3/2"))
        self.assertTrue(data["ok"], data.get("error"))
        self.assertEqual(data["page"], 2)
        # totalCount 45 at pageSize 20 → page 2 has a page 3 after it.
        self.assertEqual(data["next"], 3)
        cards = data["cards"]
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["u"], "https://asmr.one/work/111")
        self.assertEqual(cards[0]["t"], "最初の作品")
        # Release stamps are full timestamps; a card shows the date only.
        self.assertEqual(cards[0]["d"], "2026-08-09")
        self.assertEqual(cards[0]["g"], ["掏耳", "耳舐め"])
        # The chips feed off the same tag tally the site scrape fills.
        self.assertEqual(data["tags"]["掏耳"], 2)

    def test_track_tree_becomes_direct_sources(self) -> None:
        data = json.loads(self._run("resolve", "https://asmr.one/work/111"))
        self.assertTrue(data["ok"], data.get("error"))
        self.assertEqual(data["title"], "最初の作品")
        sources = data["sources"]
        # Three audio leaves across two nesting levels plus one video extra;
        # the image node is skipped; every source is a direct stream.
        self.assertEqual(len(sources), 4)
        for s in sources:
            self.assertEqual(s["kind"], "direct")
            self.assertTrue(s["url"].startswith("http://cdn.example/stream/"))
        # The label is the track's own name and nothing else: it is what the
        # now-playing title, the queue row and the notification render, and a
        # breadcrumb in that slot pushed the name off the line.
        labels = [s["label"] for s in sources]
        self.assertEqual(labels[0], "01_あさ")
        self.assertEqual(labels[1], "02_ひる")
        self.assertEqual(labels[2], "03_よる")
        # Video leaves ride the same direct-source shape: the reference
        # player treats them as playable media, not as drop-on-the-floor.
        self.assertEqual(labels[3], "04_おまけ")
        self.assertTrue(sources[3]["url"].endswith(".mp4"))
        # The folder context still travels — as a field of its own — so a
        # 100-file work can still tell two same-named tracks apart without
        # every row repeating the same folder names in front of the name.
        self.assertEqual(sources[0]["group"], "wav · part1")
        self.assertEqual(sources[1]["group"], "wav")
        self.assertNotIn("group", sources[2])
        self.assertNotIn("group", sources[3])

    def test_mirror_site_rides_the_virtual_url(self) -> None:
        """Eara's switchable site: one API, four hosts.

        The virtual search URL's tail segment picks the mirror (Eara's
        setting defaults to 200 because asmr.one itself is the domain most
        often unreachable); cards must point at THAT mirror's work pages so
        the resolve step lands on the same family, and a resolve of a
        mirror work URL must query the mirror's API base.
        """
        # Legacy two-segment URLs still mean asmr.one.
        data = json.loads(self._run("feed", "asmrone://search/%E6%8E%8F%E8%80%B3/2"))
        self.assertEqual(data["cards"][0]["u"], "https://asmr.one/work/111")
        # With the site segment, cards follow the mirror.
        data = json.loads(
            self._run("feed", "asmrone://search/%E6%8E%8F%E8%80%B3/2/200"))
        self.assertTrue(data["ok"], data.get("error"))
        self.assertEqual(data["page"], 2)
        self.assertEqual(data["cards"][0]["u"], "https://asmr-200.com/work/111")
        # A mirror work URL resolves through the mirror's own API base
        # (rewritten onto the fixture, so a 200 here proves the routing).
        data = json.loads(self._run("resolve", "https://asmr-200.com/work/111"))
        self.assertTrue(data["ok"], data.get("error"))
        self.assertEqual(data["title"], "最初の作品")
        self.assertEqual(len(data["sources"]), 4)
        # And workId never cared about the host.
        self.assertEqual(self._run("workid", "https://asmr-300.com/work/42").strip(),
                         "42")


if __name__ == "__main__":
    unittest.main()
