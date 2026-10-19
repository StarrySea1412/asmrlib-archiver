from __future__ import annotations

import contextlib
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from desktop_main import DesktopApi, pick_free_port

_BI = "https://bysetayico.com/e/abc"
_UP = "https://upn.one/embed/9x8y"
_POST = "https://asmrlib.com/posts/11111111111111111111111111111111"
_MEDIA = "https://cdn.bysetayico.com/hls/master.m3u8"


class PickFreePortTests(unittest.TestCase):
    def test_returns_preferred_port_when_free(self) -> None:
        port = pick_free_port(preferred=58765)
        self.assertGreaterEqual(port, 58765)

    def test_skips_occupied_port(self) -> None:
        # A plain listener without special socket options: the old code used
        # SO_REUSEADDR, which on Windows silently double-binds this port and
        # routes the app's requests to the other process (SeeFlow symptom).
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 58766))
            blocker.listen(1)
            port = pick_free_port(preferred=58766)
        self.assertNotEqual(port, 58766)

    def test_exclusive_probe_never_double_binds(self) -> None:
        if sys.platform != "win32":
            self.skipTest("SO_EXCLUSIVEADDRUSE semantics are Windows-specific")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 58767))
            blocker.listen(1)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                with self.assertRaises(OSError):
                    probe.bind(("127.0.0.1", 58767))


class _FakeBinding:
    def __init__(self, sniffed: list[str] | None = None) -> None:
        self.sniffed_media: list[str] = list(sniffed or [])


class _FakeShield:
    """Minimal stand-in for GuardedWebViewPlayer."""

    def __init__(self, results: list[dict], sniffed: list[str] | None = None) -> None:
        self._results = list(results)
        self.binding = _FakeBinding(sniffed)
        self.opened: list[str] = []

    def open(self, url: str) -> dict:
        self.opened.append(url)
        if self._results:
            return self._results.pop(0)
        return {"ok": False, "code": "open_failed", "error": "no window"}


class _FakeViewer:
    def __init__(self, db) -> None:
        self.db = db
        self.config = None


class _StallAfterHealthInsert:
    """sqlite connection proxy that pauses a ``source_health`` INSERT.

    ``SourceHealthRepo.record_playback_outcome`` publishes a row with
    ``INSERT OR IGNORE`` and only then counts the outcome with an ``UPDATE`` —
    two statements, one connection, one transaction.  A reader on that same
    connection can therefore observe the row while ``success_count`` is still
    ``0``.  In production that window is a few microseconds wide and needs a
    thread switch to hit; the stall makes it certain, so a test can pin the
    behaviour instead of waiting for a scheduling coincidence.
    """

    _TRIGGER = "INSERT OR IGNORE INTO SOURCE_HEALTH"

    def __init__(self, conn, seconds: float) -> None:
        self._conn = conn
        self._seconds = seconds

    def execute(self, sql, *args, **kwargs):
        cursor = self._conn.execute(sql, *args, **kwargs)
        if sql.lstrip().upper().startswith(self._TRIGGER):
            time.sleep(self._seconds)
        return cursor

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc_info):
        return self._conn.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class DesktopPlaybackHealthTests(unittest.TestCase):
    """桌面受控播放器把真实播放结果写进 source_health。"""

    def setUp(self) -> None:
        from asmrlib_archiver.db import ArchiveDb

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = ArchiveDb(Path(self.temp_dir.name) / "archive.sqlite3")
        self.db.init()
        self.api = DesktopApi("http://127.0.0.1:1", viewer=_FakeViewer(self.db))

    def tearDown(self) -> None:
        with contextlib.suppress(Exception):
            self.db.close()
        self.temp_dir.cleanup()

    def _health(self, url: str):
        from asmrlib_archiver.source_health import health_key

        return self.db.get_source_health([url]).get(health_key(url))

    def _patch_candidates(self, pairs):
        import asmrlib_archiver.player_resolver as pr

        original = pr.resolve_player_candidates
        pr.resolve_player_candidates = lambda url, **kwargs: list(pairs)
        self.addCleanup(setattr, pr, "resolve_player_candidates", original)

    def test_a_refused_source_is_recorded_and_the_next_one_is_opened(self) -> None:
        self._patch_candidates([("BI", _BI), ("UP", _UP)])
        self.api._online_shield = _FakeShield(
            [
                {"ok": False, "code": "blocked_ad_url", "error": "blocked"},
                {"ok": True, "mode": "guarded-webview"},
            ]
        )
        # The walk itself is covered here; the success watcher has its own test.
        self.api._watch_for_playback = lambda *a, **k: None

        result = self.api.open_online_player(_POST)

        self.assertTrue(result["ok"])
        self.assertEqual(self.api._online_shield.opened, [_BI, _UP])
        failed = self._health(_BI)
        self.assertIsNotNone(failed)
        self.assertEqual(failed.failure_count, 1)
        self.assertEqual(failed.success_count, 0)
        # The source that was never refused keeps no row at all.
        self.assertIsNone(self._health(_UP))

    def test_an_environment_failure_does_not_condemn_any_source(self) -> None:
        self._patch_candidates([("BI", _BI), ("UP", _UP)])
        self.api._online_shield = _FakeShield(
            [
                {"ok": False, "code": "open_failed", "error": "no webview2"},
                {"ok": False, "code": "open_failed", "error": "no webview2"},
            ]
        )
        self.api._watch_for_playback = lambda *a, **k: None
        with contextlib.suppress(Exception):
            self.api.open_external_url = lambda url: {"ok": False, "code": "open_failed"}

        result = self.api.open_online_player(_POST)

        self.assertFalse(result["ok"])
        # A missing WebView2 runtime must not be written down as "this source
        # is dead" — the walk stops instead of blaming every candidate.
        self.assertEqual(self.api._online_shield.opened, [_BI])
        self.assertIsNone(self._health(_BI))
        self.assertIsNone(self._health(_UP))

    def test_sniffed_media_is_the_only_evidence_that_playback_started(self) -> None:
        self.api._online_shield = _FakeShield([], sniffed=[_MEDIA])

        watcher = self.api._watch_for_playback(
            _BI, known_media=set(), timeout_seconds=2.0, poll_seconds=0.01
        )
        self._join_watcher(watcher)

        record = self._await_health(_BI, lambda r: r.success_count == 1)
        self.assertIsNotNone(record)
        self.assertEqual(record.success_count, 1)
        self.assertGreaterEqual(record.latency_ms, 0)

    def test_a_window_that_never_fetched_media_records_no_success(self) -> None:
        self.api._online_shield = _FakeShield([], sniffed=[])

        watcher = self.api._watch_for_playback(
            _BI, known_media=set(), timeout_seconds=0.15, poll_seconds=0.01
        )
        self._join_watcher(watcher)

        self.assertIsNone(self._health(_BI))

    def test_media_sniffed_for_an_earlier_source_is_not_reused(self) -> None:
        # The shield window is reused across opens, so its sniff list still
        # holds the previous source's media. That must not become a success
        # for the source opened afterwards.
        self.api._online_shield = _FakeShield([], sniffed=[_MEDIA])

        watcher = self.api._watch_for_playback(
            _UP, known_media={_MEDIA}, timeout_seconds=0.15, poll_seconds=0.01
        )
        self._join_watcher(watcher)

        self.assertIsNone(self._health(_UP))

    def test_a_half_written_health_row_is_not_mistaken_for_a_success(self) -> None:
        """Deterministic version of the intermittent ``0 != 1``.

        The writer publishes its row before it counts the outcome, on the
        connection this test reads.  Under load the two statements get
        separated by a thread switch, and waiting for "a record exists"
        returns the row with ``success_count`` still at ``0``.  The stall
        below makes that separation certain instead of rare, so this test
        fails every time the wait is not on the condition itself.
        """
        from asmrlib_archiver.source_health import PlaybackOutcome

        real_conn = self.db.conn
        self.db.conn = _StallAfterHealthInsert(real_conn, 0.6)
        writer = None
        try:
            writer = threading.Thread(
                target=self.db.record_playback_outcome,
                args=(_BI, PlaybackOutcome.PLAYING, 12, ""),
                daemon=True,
            )
            writer.start()

            # Existence is satisfied by the half-written row: this is what the
            # old existence-only wait returned, and why the assertion on
            # `success_count` could fail.
            half = self._await_health(_BI, timeout_seconds=0.4)
            self.assertIsNotNone(half)
            self.assertEqual(half.success_count, 0)

            # Only the condition is true once the write has really landed.
            record = self._await_health(_BI, lambda r: r.success_count == 1)
            self.assertIsNotNone(record)
            self.assertEqual(record.success_count, 1)
            self.assertGreaterEqual(record.latency_ms, 0)
        finally:
            if writer is not None:
                writer.join(5.0)
            self.db.conn = real_conn

    def test_close_waits_for_an_in_flight_health_write(self) -> None:
        """Shutdown must not close the connection under a running watcher.

        The watcher writes over the viewer's sqlite connection from a daemon
        thread.  Closing that connection mid-write is an access violation
        inside sqlite — it aborts the process instead of raising, so nothing
        gets to report it.  ``_close`` is the only thing between the two, and
        it runs before ``viewer.close()`` in ``main``.

        The stall below parks the writer inside its write, which is precisely
        the moment shutdown has to wait out.  Without the join ``_close``
        returns while the thread is still alive and the connection dies under
        it.
        """
        real_conn = self.db.conn
        self.db.conn = _StallAfterHealthInsert(real_conn, 0.6)
        try:
            self.api._online_shield = _FakeShield([], sniffed=[_MEDIA])
            watcher = self.api._watch_for_playback(
                _BI, known_media=set(), timeout_seconds=5.0, poll_seconds=0.01
            )
            self.assertIsNotNone(watcher)

            # The row exists but is not counted yet: the writer is provably
            # inside the write, not merely scheduled.
            half = self._await_health(_BI, timeout_seconds=2.0)
            self.assertIsNotNone(half)
            self.assertEqual(half.success_count, 0)
            self.assertTrue(watcher.is_alive())

            self.api._close()

            self.assertFalse(watcher.is_alive())
            record = self._health(_BI)
            self.assertIsNotNone(record)
            self.assertEqual(record.success_count, 1)
        finally:
            self.db.conn = real_conn

    def _await_health(self, url: str, predicate=None, timeout_seconds: float = 3.0):
        """Wait for a health record for ``url`` that satisfies ``predicate``.

        Waiting for mere existence is not enough.  The watcher writes from a
        daemon thread over the *same* sqlite connection this test reads
        (``DbConnection.open(..., check_same_thread=False)``), and
        ``record_playback_outcome`` creates the row with ``INSERT OR IGNORE``
        before the ``UPDATE`` that counts the outcome — so the first non-None
        record can still be a write that has not finished.

        Returns the last record seen, so a genuine failure reports the state
        it was waiting for instead of a bare ``None``.
        """

        deadline = time.monotonic() + timeout_seconds
        record = None
        while time.monotonic() < deadline:
            record = self._health(url)
            if record is not None and (predicate is None or predicate(record)):
                return record
            time.sleep(0.02)
        return record

    def _join_watcher(self, watcher, timeout_seconds: float = 5.0) -> None:
        """Let the playback watcher finish before tearDown closes the DB.

        The watcher writes through the same sqlite connection this test reads
        and asserts on, from a daemon thread.  Closing that connection while
        the write is in flight is an access violation inside sqlite rather
        than a Python exception: it aborts the whole pytest process, so no
        test can report it afterwards.
        """

        if watcher is not None:
            watcher.join(timeout_seconds)


if __name__ == "__main__":
    unittest.main()
