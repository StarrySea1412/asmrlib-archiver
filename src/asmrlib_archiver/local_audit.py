"""Honest accounting for the local files the archive claims to own.

The recordings page used to answer "is this media row real?" with
``Path(file_path).is_file()`` and then ``continue`` past every row that
failed. A deleted file therefore vanished from the page while its database
row stayed behind forever: the page presented itself as the user's library
while quietly omitting every broken entry, and the orphaned row became
invisible and unreachable.

This module replaces that silent skip with a report the UI is forced to
render. It answers three questions and nothing else:

* which recorded rows can no longer be served (``missing``),
* which files on disk no row references (``orphans``),
* how many bytes each part of the managed root costs, and whether that
  part is regenerable cache or the user's own saves (``storage``).

Nothing here writes, deletes, moves or repairs anything: the scan is
strictly read-only, and the only destructive action in the whole feature is
removing a database row when the user explicitly clicks it.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

# --------------------------------------------------------------- vocabulary
#
# The three states a file can be in, defined exactly once so the page and
# the tests can never drift apart. Every surface that talks about a local
# file must use one of these; none of them may ever claim a file is usable
# when it is not.
STATE_LOCAL_READY = "本地可用"
STATE_FILE_MISSING = "文件丢失"
STATE_ORPHAN_FILE = "孤儿文件"

# Storage categories are tagged with how safe they are to lose.
CACHE = "cache"
OWNED = "owned"

# Directories under the managed root that hold local media the library
# serves at ``/media/<path-relative-to-root>``. A file here that no row
# references is an orphan.
MEDIA_DIRS: tuple[str, ...] = ("videos",)

# (key, label, kind) for every directory the storage view accounts for.
# ``covers`` is the cover cache including its ``.missing`` negative-cache
# entries; ``exports`` is regenerable from the database, so it is cache.
_CATEGORY_DIRS: tuple[tuple[str, str, str], ...] = (
    ("covers", "封面缓存", CACHE),
    ("html", "网页快照", CACHE),
    ("metadata", "元数据", CACHE),
    ("exports", "导出文件", CACHE),
    ("videos", "本地媒体", OWNED),
)


@runtime_checkable
class _MediaRowSource(Protocol):
    """The only thing the audit needs from the database."""

    path: Path

    def list_local_media(self) -> Sequence[sqlite3.Row]: ...


@dataclass(frozen=True)
class MissingRow:
    """A recorded local file that cannot be served any more.

    ``exists_on_disk`` distinguishes "the file is gone" from "the file is
    still there but outside the managed root, so the library can neither
    serve nor verify it". Both are broken records; only the second one is
    recoverable by re-linking.
    """

    media_id: int
    label: str
    path: str
    item_title: str
    item_url: str
    exists_on_disk: bool


@dataclass(frozen=True)
class OrphanFile:
    """A file under a managed media directory that no row references."""

    rel_path: str
    path: Path
    size: int
    mtime: float


@dataclass(frozen=True)
class StorageCategory:
    key: str
    label: str
    kind: str
    path: Path
    size: int
    files: int

    @property
    def is_cache(self) -> bool:
        """True when the category is regenerable and safe to delete."""
        return self.kind == CACHE

    @property
    def exists(self) -> bool:
        return self.path.exists()


@dataclass(frozen=True)
class LocalAuditReport:
    missing: tuple[MissingRow, ...]
    orphans: tuple[OrphanFile, ...]
    storage: tuple[StorageCategory, ...]

    @property
    def cache_bytes(self) -> int:
        return sum(item.size for item in self.storage if item.is_cache)

    @property
    def owned_bytes(self) -> int:
        return sum(item.size for item in self.storage if not item.is_cache)

    @property
    def total_bytes(self) -> int:
        return self.cache_bytes + self.owned_bytes

    @property
    def total_files(self) -> int:
        return sum(item.files for item in self.storage)

    def category(self, key: str) -> StorageCategory | None:
        for item in self.storage:
            if item.key == key:
                return item
        return None


# ------------------------------------------------------------------ helpers


def _norm(value: object) -> str:
    """Case-folded absolute key so path comparison matches the filesystem.

    Windows resolves ``Videos\\A.mp3`` and ``videos\\a.mp3`` to one file;
    POSIX does not, and ``normcase`` is a no-op there.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(str(Path(text).expanduser().resolve()))
    except OSError:
        return os.path.normcase(text)


def servable_path(file_path: object, root: Path) -> Path | None:
    """Return the resolved path when the library can actually serve it.

    ``None`` means the record is broken: the file is missing, is not a
    regular file, or sits outside the managed root (where ``/media/`` will
    refuse to read it).
    """
    text = str(file_path or "").strip()
    if not text:
        return None
    try:
        path = Path(text).expanduser().resolve()
    except OSError:
        return None
    if not path.is_file():
        return None
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def media_state(file_path: object, root: Path) -> str:
    """The single state a recorded local file may be rendered as."""
    return STATE_LOCAL_READY if servable_path(file_path, root) is not None else STATE_FILE_MISSING


def _measure_dir(path: Path) -> tuple[int, int]:
    """Total bytes and file count under ``path`` (0, 0 when absent)."""
    if not path.is_dir():
        return 0, 0
    total = 0
    count = 0
    try:
        entries = list(path.rglob("*"))
    except OSError:
        return 0, 0
    for entry in entries:
        try:
            if entry.is_file():
                total += entry.stat().st_size
                count += 1
        except OSError:
            # A file that vanished mid-scan (or an unreadable link) is not
            # worth failing the whole report over.
            continue
    return total, count


def _measure_database(db_path: Path | None, root: Path) -> tuple[Path, int, int]:
    """Size the sqlite database including its WAL/SHM sidecars."""
    candidate = root / "archive.sqlite3" if db_path is None else Path(db_path)
    total = 0
    count = 0
    for suffix in ("", "-wal", "-shm"):
        sidecar = Path(str(candidate) + suffix)
        try:
            if sidecar.is_file():
                total += sidecar.stat().st_size
                count += 1
        except OSError:
            continue
    return candidate, total, count


def _storage_categories(root: Path, db_path: Path | None) -> tuple[StorageCategory, ...]:
    categories: list[StorageCategory] = []
    for key, label, kind in _CATEGORY_DIRS:
        path = root / key
        size, files = _measure_dir(path)
        categories.append(
            StorageCategory(
                key=key, label=label, kind=kind, path=path, size=size, files=files
            )
        )
    db_file, db_size, db_files = _measure_database(db_path, root)
    categories.append(
        StorageCategory(
            key="database",
            label="数据库",
            kind=OWNED,
            path=db_file,
            size=db_size,
            files=db_files,
        )
    )
    return tuple(categories)


def _missing_rows(rows: Iterable[sqlite3.Row], root: Path) -> tuple[MissingRow, ...]:
    missing: list[MissingRow] = []
    for row in rows:
        raw = str(row["file_path"] or "")
        if servable_path(raw, root) is not None:
            continue
        try:
            exists = Path(raw).expanduser().is_file()
        except OSError:
            exists = False
        missing.append(
            MissingRow(
                media_id=int(row["id"]),
                label=str(row["label"] or row["kind"] or "本地文件"),
                path=raw,
                item_title=str(row["title"] or row["item_url"] or "未知帖子"),
                item_url=str(row["item_url"] or ""),
                exists_on_disk=exists,
            )
        )
    return tuple(missing)


def _orphan_files(rows: Iterable[sqlite3.Row], root: Path) -> tuple[OrphanFile, ...]:
    referenced = {_norm(row["file_path"]) for row in rows if str(row["file_path"] or "")}
    orphans: list[OrphanFile] = []
    for dirname in MEDIA_DIRS:
        directory = root / dirname
        if not directory.is_dir():
            continue
        try:
            entries = list(directory.rglob("*"))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_file() or _norm(entry) in referenced:
                    continue
                stat = entry.stat()
            except OSError:
                continue
            try:
                rel = entry.resolve().relative_to(root)
            except (OSError, ValueError):
                rel = Path(dirname) / entry.name
            orphans.append(
                OrphanFile(
                    rel_path=rel.as_posix(),
                    path=entry,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )
    orphans.sort(key=lambda item: item.rel_path)
    return tuple(orphans)


# -------------------------------------------------------------------- public


def audit_local_media(db: _MediaRowSource, root: Path | str) -> LocalAuditReport:
    """Read-only audit of the local media library.

    Pure over its inputs: the same rows and the same root always produce the
    same report, no HTTP server required, and nothing on disk is touched.
    """
    resolved_root = Path(root).expanduser().resolve()
    rows = list(db.list_local_media())
    return LocalAuditReport(
        missing=_missing_rows(rows, resolved_root),
        orphans=_orphan_files(rows, resolved_root),
        storage=_storage_categories(resolved_root, getattr(db, "path", None)),
    )
