"""Execution tests for the APK's pure-Java resume policy.

``PlaybackResume`` imports nothing from ``android.*``, so the very same source
compiles with plain ``javac`` and runs on an ordinary JVM.  This test does that
and then asserts on the decisions the real class produced — not on the shape of
its source.

That distinction is the point.  A 22-minute recording that restarts at 0:00
because a boundary was off by one second, or a place that is silently thrown
away for every stream that reports ``duration = -1``, are both invisible to a
source-contract assertion and both ruin the feature.  The boundaries here are
the ones that decide it: exactly thirty seconds left, thirty-one, a position
past the end, and an unknown length.

Skipped when no JDK is present so the suite stays portable.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from _jdk import jdk_tool

JAVAC = jdk_tool("javac")
JAVA = jdk_tool("java")

_RESUME_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "android"
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "asmrlib"
    / "archiver"
    / "PlaybackResume.java"
)

# Generated into the temp dir and compiled next to PlaybackResume.  Output is
# forced to UTF-8 so the class's own Chinese strings survive the pipe on a
# console whose encoding is not UTF-8.
RUNNER_JAVA = r"""
import com.asmrlib.archiver.PlaybackResume;

import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.util.ArrayList;
import java.util.List;

/** Minimal driver: one parseable line per decision, and no assertions of its
 *  own — the Python side owns the expected values, so a failure report says
 *  what the class did rather than only that something inside Java broke. */
public class Runner {

    static PrintStream out;

    public static void main(String[] args) throws Exception {
        out = new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8");

        long twentyTwo = 1_320_000L;

        start("mid_file", 300_000L, twentyTwo);
        start("exactly_thirty_left", twentyTwo - 30_000L, twentyTwo);
        start("thirty_one_left", twentyTwo - 31_000L, twentyTwo);
        start("one_second_left", twentyTwo - 1_000L, twentyTwo);
        start("past_the_end", twentyTwo + 80_000L, twentyTwo);
        start("unknown_duration", 300_000L, -1L);
        start("zero_duration", 300_000L, 0L);
        start("nine_seconds_in", 9_999L, twentyTwo);
        start("ten_seconds_in", 10_000L, twentyTwo);

        finished("exactly_thirty_left", twentyTwo - 30_000L, twentyTwo);
        finished("thirty_one_left", twentyTwo - 31_000L, twentyTwo);
        finished("unknown_duration", 300_000L, -1L);

        store("nine_seconds_in", 9_999L, twentyTwo);
        store("ten_seconds_in", 10_000L, twentyTwo);
        store("twenty_seconds_left", twentyTwo - 20_000L, twentyTwo);
        store("unknown_duration", 300_000L, -1L);

        key("task_wins", "task-1", "content://media/1");
        key("no_task_id", "", "content://media/1");
        key("null_task_id", "null", "content://media/1");
        key("nothing_at_all", "", "");
        key("null_uri", "task-2", null);

        drop("none", 0);
        drop("exactly_the_cap", PlaybackResume.MAX_ENTRIES);
        drop("over_the_cap", PlaybackResume.MAX_ENTRIES + 5);
        drop("far_over", 100);

        out.flush();
    }

    static void start(String name, long stored, long dur) {
        out.println("START " + name + " stored=" + stored + " dur=" + dur
                + " -> " + PlaybackResume.startPosition(stored, dur));
    }

    static void finished(String name, long pos, long dur) {
        out.println("FINISHED " + name + " pos=" + pos + " dur=" + dur
                + " -> " + PlaybackResume.isFinished(pos, dur));
    }

    static void store(String name, long pos, long dur) {
        out.println("STORE " + name + " pos=" + pos + " dur=" + dur
                + " -> " + PlaybackResume.shouldStore(pos, dur));
    }

    static void key(String name, String task, String uri) {
        out.println("KEY " + name + " -> " + PlaybackResume.keyFor(task, uri));
    }

    static void drop(String name, int n) {
        List<String> keys = new ArrayList<String>();
        for (int i = 0; i < n; i++) keys.add("k" + i);
        StringBuilder joined = new StringBuilder();
        for (String k : PlaybackResume.keysToDrop(keys)) {
            if (joined.length() > 0) joined.append(',');
            joined.append(k);
        }
        out.println("DROP " + name + " n=" + n + " -> " + joined);
    }
}
"""


@unittest.skipUnless(JAVAC and JAVA, "javac/java not available")
class PlaybackResumeJavaTests(unittest.TestCase):
    """Compile the real policy class and assert on what it actually returns."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="resume-java-"))
        cls.classes = cls.tmp / "classes"
        cls.classes.mkdir()
        runner = cls.tmp / "Runner.java"
        runner.write_text(RUNNER_JAVA, encoding="utf-8")
        proc = subprocess.run(
            [JAVAC, "-encoding", "UTF-8", "-d", str(cls.classes),
             str(_RESUME_SOURCE), str(runner)],
            capture_output=True,
        )
        if proc.returncode != 0:  # pragma: no cover - build failure
            raise AssertionError(
                "javac failed:\n"
                + proc.stdout.decode("utf-8", "replace")
                + proc.stderr.decode("utf-8", "replace")
            )
        run = subprocess.run(
            [JAVA, "-cp", str(cls.classes), "Runner"],
            capture_output=True,
            timeout=120,
        )
        cls.stdout = run.stdout.decode("utf-8", "replace")
        if run.returncode != 0:  # pragma: no cover - runtime failure
            raise AssertionError(
                "Runner crashed:\n" + cls.stdout
                + run.stderr.decode("utf-8", "replace")
            )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------- parsing

    @classmethod
    def _cases(cls, kind: str) -> dict[str, str]:
        """Every ``<KIND> <name> ... -> <value>`` line, keyed by name.

        The middle is optional because the KEY lines carry no arguments of
        their own — ``KEY no_task_id -> content://media/1`` — and a pattern
        that demanded one would silently report the case as missing.
        """

        found = {}
        for line in cls.stdout.splitlines():
            match = re.match(rf"^{kind} (\S+)(?: .*)? -> (.*)$", line)
            if match:
                found[match.group(1)] = match.group(2)
        return found

    @classmethod
    def _start(cls, name: str) -> int:
        return int(cls._cases("START")[name])

    @classmethod
    def _finished(cls, name: str) -> bool:
        return cls._cases("FINISHED")[name] == "true"

    @classmethod
    def _store(cls, name: str) -> bool:
        return cls._cases("STORE")[name] == "true"

    @classmethod
    def _key(cls, name: str) -> str:
        return cls._cases("KEY")[name]

    @classmethod
    def _drop(cls, name: str) -> list[str]:
        value = cls._cases("DROP")[name]
        return value.split(",") if value else []

    # --------------------------------------------------------------- cases

    def test_a_place_in_the_middle_of_a_long_file_is_resumed(self) -> None:
        """The whole reason the feature exists: 22 minutes, resume at 5:00."""

        self.assertEqual(self._start("mid_file"), 300_000)

    def test_the_last_thirty_seconds_count_as_watched(self) -> None:
        """Boundary: 30s left is finished, 31s left is not.

        The comparison is inclusive on purpose — being dropped into the final
        half-minute of a recording is worse than starting over — so the two
        sides of the boundary have to be pinned individually.
        """

        self.assertEqual(self._start("exactly_thirty_left"), 0)
        self.assertTrue(self._finished("exactly_thirty_left"))
        self.assertEqual(self._start("thirty_one_left"), 1_320_000 - 31_000)
        self.assertFalse(self._finished("thirty_one_left"))

    def test_a_position_past_the_end_starts_over(self) -> None:
        """A stale record from a re-encoded file must not seek past the end."""

        self.assertEqual(self._start("past_the_end"), 0)
        self.assertEqual(self._start("one_second_left"), 0)

    def test_an_unknown_duration_still_resumes(self) -> None:
        """Some MPEG-TS report duration = -1.

        Reading that as "nothing left" would quietly discard the place for
        every stream that answers that way, which is the failure this asserts
        against.  A zero duration is the same case: not yet known, not over.
        """

        self.assertFalse(self._finished("unknown_duration"))
        self.assertEqual(self._start("unknown_duration"), 300_000)
        self.assertEqual(self._start("zero_duration"), 300_000)

    def test_the_first_ten_seconds_are_not_a_place(self) -> None:
        """Boundary: 9 999 ms is the head of the file, 10 000 ms is a place."""

        self.assertEqual(self._start("nine_seconds_in"), 0)
        self.assertEqual(self._start("ten_seconds_in"), 10_000)

    def test_a_barely_started_or_finished_file_is_never_written(self) -> None:
        self.assertFalse(self._store("nine_seconds_in"))
        self.assertTrue(self._store("ten_seconds_in"))
        self.assertFalse(self._store("twenty_seconds_left"))
        self.assertTrue(self._store("unknown_duration"))

    def test_the_task_id_wins_over_the_uri(self) -> None:
        """A rename rewrites the URI and leaves the task id alone.

        重命名 is a real path in this app, so a record keyed on the URI would be
        orphaned the moment the user uses it — and the place would be gone with
        no error anywhere.
        """

        self.assertEqual(self._key("task_wins"), "task-1")
        self.assertEqual(self._key("null_uri"), "task-2")

    def test_the_uri_is_the_fallback_when_there_is_no_task_id(self) -> None:
        self.assertEqual(self._key("no_task_id"), "content://media/1")
        # The shell hands over the literal string "null" when an extra is
        # absent, so it has to be treated as absent.
        self.assertEqual(self._key("null_task_id"), "content://media/1")
        self.assertEqual(self._key("nothing_at_all"), "")

    def test_the_store_is_bounded_and_drops_the_oldest(self) -> None:
        """Unbounded would be a preferences entry that grows forever."""

        self.assertEqual(self._drop("none"), [])
        self.assertEqual(self._drop("exactly_the_cap"), [])
        self.assertEqual(self._drop("over_the_cap"), [f"k{i}" for i in range(40, 45)])
        self.assertEqual(len(self._drop("far_over")), 60)

    def test_the_policy_class_has_no_android_dependency(self) -> None:
        """If it ever gains one, the execution tests above stop being possible."""

        source = _RESUME_SOURCE.read_text(encoding="utf-8")
        # Only the code counts: the javadoc is allowed to *explain* that the
        # class avoids android.*, so comment lines are stripped first rather
        # than being matched by accident.
        code = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith(("*", "/*", "//"))
        )
        self.assertNotIn("import android.", code)
        self.assertNotIn("import org.json", code)
        self.assertNotIn("android.", code)
        self.assertIn("import java.util.List;", code)


if __name__ == "__main__":
    unittest.main()
