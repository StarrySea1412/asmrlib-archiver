from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from asmrlib_archiver.config import AppConfig
from asmrlib_archiver.db import ArchiveDb
from asmrlib_archiver.local_audit import (
    CACHE,
    MEDIA_DIRS,
    OWNED,
    STATE_FILE_MISSING,
    STATE_LOCAL_READY,
    audit_local_media,
    media_state,
    servable_path,
)


class _StubRows:
    """Minimal stand-in for the media repo: proves the audit needs no HTTP."""

    def __init__(self, path: Path, rows: list[dict]) -> None:
        self.path = path
        self._rows = rows

    def list_local_media(self) -> list[dict]:
        return list(self._rows)


class LocalAuditTests(unittest.TestCase):
    """Behavioural tests for the read-only local media audit.

    The point of the audit is that a broken record is *reported* rather than
    skipped, so every test here asserts on what the report says about real
    files on a real temp filesystem.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data = root / "data"
        self.data.mkdir()
        self.config = AppConfig(
            config_path=root / "config.yaml",
            output_dir=self.data,
            database_path=self.data / "archive.sqlite3",
        )
        self.db = ArchiveDb(self.config.database_path)
        self.db.init()
        self.source = "https://asmrlib.com/posts/11111111111111111111111111111111"
        self.db.add_seed(self.source)
        self.db.update_item_result(
            self.source,
            title="Audit sample",
            published_at="2026-07-01",
            cover="",
            status="archived",
            html_path=str(self.data / "html" / "sample.html"),
        )
        self.videos = self.data / "videos"
        self.videos.mkdir()
        self.root_path = self.data.resolve()

        self.good = self.videos / "good.mp3"
        self.good.write_bytes(b"g" * 1000)
        self.gone = self.videos / "gone.mp3"
        self.gone.write_bytes(b"x" * 2000)
        self.orphan = self.videos / "stray.mp3"
        self.orphan.write_bytes(b"s" * 3000)

        self.good_id = self.db.attach_local_media(
            self.source, file_path=str(self.good), label="Good", media_url="u-good"
        )
        self.gone_id = self.db.attach_local_media(
            self.source, file_path=str(self.gone), label="Gone", media_url="u-gone"
        )
        # The defect this audit exists for: the file is deleted behind the
        # archive's back and the row is left pointing at nothing.
        self.gone.unlink()

        (self.data / "covers").mkdir()
        (self.data / "covers" / "abc.jpg").write_bytes(b"c" * 500)
        (self.data / "metadata").mkdir()
        (self.data / "metadata" / "post.json").write_bytes(b"m" * 200)
        (self.data / "exports").mkdir()
        (self.data / "exports" / "catalog.json").write_bytes(b"e" * 300)

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_deleted_file_is_reported_as_missing_not_dropped(self) -> None:
        report = audit_local_media(self.db, self.data)
        self.assertEqual([item.media_id for item in report.missing], [self.gone_id])
        entry = report.missing[0]
        self.assertEqual(entry.label, "Gone")
        self.assertEqual(entry.path, str(self.gone))
        self.assertEqual(entry.item_title, "Audit sample")
        self.assertEqual(entry.item_url, self.source)
        self.assertFalse(entry.exists_on_disk)
        # The healthy row must not be swept up in the same report.
        self.assertNotIn(self.good_id, [item.media_id for item in report.missing])

    def test_unreferenced_file_on_disk_is_reported_as_an_orphan(self) -> None:
        report = audit_local_media(self.db, self.data)
        self.assertEqual(
            [item.rel_path for item in report.orphans], ["videos/stray.mp3"]
        )
        orphan = report.orphans[0]
        self.assertEqual(orphan.size, 3000)
        self.assertGreater(orphan.mtime, 0)
        self.assertEqual(orphan.path.resolve(), self.orphan.resolve())

    def test_referenced_and_cached_files_are_not_orphans(self) -> None:
        report = audit_local_media(self.db, self.data)
        rel_paths = {item.rel_path for item in report.orphans}
        self.assertNotIn("videos/good.mp3", rel_paths)
        self.assertNotIn("videos/gone.mp3", rel_paths)
        # Files outside the managed media directories are never orphans: the
        # cover cache and exports have their own accounting rows.
        self.assertNotIn("covers/abc.jpg", rel_paths)
        self.assertNotIn("exports/catalog.json", rel_paths)
        self.assertEqual(MEDIA_DIRS, ("videos",))

    def test_healthy_referenced_file_is_neither_missing_nor_orphan(self) -> None:
        report = audit_local_media(self.db, self.data)
        self.assertNotIn(self.good_id, [item.media_id for item in report.missing])
        self.assertNotIn(
            "videos/good.mp3", [item.rel_path for item in report.orphans]
        )
        self.assertEqual(media_state(self.good, self.root_path), STATE_LOCAL_READY)

    def test_state_vocabulary_never_calls_a_broken_file_available(self) -> None:
        self.assertEqual(media_state(self.gone, self.root_path), STATE_FILE_MISSING)
        self.assertEqual(media_state("", self.root_path), STATE_FILE_MISSING)
        self.assertIsNone(servable_path(self.gone, self.root_path))
        self.assertIsNone(servable_path(self.videos / "nope.mp3", self.root_path))

    def test_file_outside_the_managed_root_is_reported_missing(self) -> None:
        # The library serves local media from /media/<relative>, so a row
        # pointing outside the root cannot be played. It must still be
        # reported — and flagged as existing, because it is not gone.
        outside = Path(self.temp_dir.name) / "elsewhere.mp3"
        outside.write_bytes(b"o" * 40)
        media_id = self.db.attach_local_media(
            self.source, file_path=str(outside), label="Outside", media_url="u-out"
        )
        report = audit_local_media(self.db, self.data)
        entries = {item.media_id: item for item in report.missing}
        self.assertIn(media_id, entries)
        self.assertTrue(entries[media_id].exists_on_disk)
        self.assertEqual(media_state(outside, self.root_path), STATE_FILE_MISSING)

    def test_storage_accounting_tags_cache_versus_owned(self) -> None:
        report = audit_local_media(self.db, self.data)
        kinds = {item.key: item for item in report.storage}
        for key in ("covers", "html", "metadata", "exports"):
            self.assertEqual(kinds[key].kind, CACHE, key)
            self.assertTrue(kinds[key].is_cache, key)
        for key in ("videos", "database"):
            self.assertEqual(kinds[key].kind, OWNED, key)
            self.assertFalse(kinds[key].is_cache, key)

        self.assertEqual(kinds["covers"].size, 500)
        self.assertEqual(kinds["covers"].files, 1)
        self.assertEqual(kinds["metadata"].size, 200)
        self.assertEqual(kinds["exports"].size, 300)
        # Only files that actually exist are counted: gone.mp3 is deleted, so
        # the media directory holds good.mp3 (1000) + stray.mp3 (3000).
        self.assertEqual(kinds["videos"].size, 4000)
        self.assertEqual(kinds["videos"].files, 2)
        self.assertGreater(kinds["database"].size, 0)
        self.assertGreaterEqual(kinds["database"].files, 1)

        self.assertEqual(report.cache_bytes, 500 + 200 + 300)
        self.assertEqual(report.owned_bytes, kinds["videos"].size + kinds["database"].size)
        self.assertEqual(report.total_bytes, report.cache_bytes + report.owned_bytes)
        self.assertEqual(report.category("videos").files, 2)
        self.assertIsNone(report.category("nope"))

    def test_scan_is_read_only(self) -> None:
        def snapshot() -> dict[str, int]:
            found: dict[str, int] = {}
            for name in ("videos", "covers", "metadata", "exports"):
                directory = self.data / name
                for path in sorted(directory.rglob("*")):
                    if path.is_file():
                        found[path.relative_to(self.data).as_posix()] = path.stat().st_size
            return found

        before = snapshot()
        audit_local_media(self.db, self.data)
        audit_local_media(self.db, self.data)
        self.assertEqual(snapshot(), before)
        # Nothing was created, deleted, moved or renamed to "repair" the
        # library: the broken record and the orphan file both survive a scan.
        self.assertFalse(self.gone.exists())
        self.assertTrue(self.orphan.is_file())
        self.assertTrue(self.good.is_file())

    def test_audit_is_a_pure_function_of_rows_and_root(self) -> None:
        # Same inputs, same report — and no database or HTTP server needed to
        # get one, which is what makes this testable at all.
        stub = _StubRows(
            self.config.database_path,
            [
                {
                    "id": 7,
                    "label": "Stub",
                    "kind": "download",
                    "file_path": str(self.gone),
                    "title": "Stub post",
                    "item_url": self.source,
                },
                {
                    "id": 8,
                    "label": "Stub healthy",
                    "kind": "download",
                    "file_path": str(self.good),
                    "title": "Stub post",
                    "item_url": self.source,
                },
            ],
        )
        first = audit_local_media(stub, self.data)
        second = audit_local_media(stub, self.data)
        self.assertEqual(first, second)
        self.assertEqual([item.media_id for item in first.missing], [7])
        self.assertEqual(
            [item.rel_path for item in first.orphans], ["videos/stray.mp3"]
        )
