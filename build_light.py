"""Build the single lightweight ASMR 收藏馆 desktop bundle.

The command deliberately builds one product:

    dist/asmrlib-archiver/asmrlib-archiver.exe

The frozen application uses the system WebView2 runtime for guarded remote
player links and the operating-system browser as fallback. Playwright,
bundled Chromium, the proxy player, and the online recorder are excluded from
the analysis graph. Local media remains beside the executable in data/.

Usage:
    .venv\\Scripts\\python.exe build_light.py
    .venv\\Scripts\\python.exe build_light.py --dry-run
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DIST_ROOT = ROOT / "dist"
DIST_NAME = "asmrlib-archiver"
MAX_RUNTIME_BYTES = 60 * 1024 * 1024
LEGACY_DIST_NAMES = ("asmrlib-light",)
FORBIDDEN_MODULES = (
    "playwright",
    "playwright.sync_api",
    "playwright.async_api",
    "greenlet",
    "pyee",
    "asmrlib_archiver.sandbox_player",
    "asmrlib_archiver.proxy_player",
    "asmrlib_archiver.recorder",
    "asmrlib_archiver.viewer.recording",
)


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _copy_tree(src: Path, dst: Path) -> int:
    """Copy a directory tree and return the number of bytes copied."""
    if not src.is_dir():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    total = 0
    for item in src.rglob("*"):
        if not item.is_file():
            continue
        target = dst / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        total += item.stat().st_size
    return total


def _has_database(root: Path) -> bool:
    """Return whether ``root`` contains a usable archive database."""
    database = root / "data" / "archive.sqlite3"
    if not database.is_file() or database.stat().st_size <= 0:
        return False
    try:
        uri = f"file:{database.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute("PRAGMA quick_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")
    except sqlite3.OperationalError as exc:
        # A valid database can be locked by a running app. Size is a safer
        # fallback than silently selecting a different user's library.
        return "locked" in str(exc).lower()
    except (OSError, sqlite3.DatabaseError):
        return False


def _state_sources(final_dir: Path) -> tuple[Path | None, Path | None]:
    """Choose data and config sources independently in priority order."""
    # A previous ZIP extraction could leave ``dist/<name>/<name>/`` while the
    # executable is still running. Treat that layout as the same product so a
    # rebuild keeps its user database and configuration instead of silently
    # falling back to the repository seed.
    candidates = [final_dir]
    nested = final_dir / DIST_NAME
    if nested.is_dir():
        candidates.append(nested)
    candidates.extend(DIST_ROOT / name for name in LEGACY_DIST_NAMES)
    candidates.append(ROOT)
    data_source = next((item for item in candidates if _has_database(item)), None)
    config_source = next(
        (item for item in candidates if (item / "config.yaml").is_file()),
        None,
    )
    return data_source, config_source


def _snapshot_state(
    data_source: Path | None,
    config_source: Path | None,
) -> Path | None:
    """Snapshot data/config before the generated directory is swapped in."""
    if data_source is None and config_source is None:
        return None
    snapshot = Path(tempfile.mkdtemp(prefix="asmrlib-state-"))
    try:
        source_data = data_source / "data" if data_source is not None else None
        if source_data is not None and source_data.is_dir():
            shutil.copytree(source_data, snapshot / "data")
        source_config = (
            config_source / "config.yaml" if config_source is not None else None
        )
        if source_config is not None and source_config.is_file():
            shutil.copy2(source_config, snapshot / "config.yaml")
    except Exception:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise
    return snapshot


def _restore_state(out: Path, snapshot: Path | None) -> tuple[bool, int]:
    """Restore user state into a newly generated output directory."""
    if snapshot is None:
        return False, 0
    copied = 0
    restored = False
    data = snapshot / "data"
    if data.is_dir() and (data / "archive.sqlite3").is_file():
        target = out / "data"
        if target.exists():
            shutil.rmtree(target)
        copied = _copy_tree(data, target)
        restored = True
    config = snapshot / "config.yaml"
    if config.is_file():
        shutil.copy2(config, out / "config.yaml")
    return restored, copied


def _copy_seed_data(out: Path) -> int:
    """Seed a fresh package from the repository data directory."""
    source = ROOT / "data"
    if not source.is_dir():
        return 0
    # Existing local media and metadata stay outside the executable size
    # budget and are never deleted by this script.
    return _copy_tree(source, out / "data")


def _runtime_config(out: Path) -> None:
    example = ROOT / "config.example.yaml"
    packaged_example = out / "config.example.yaml"
    if example.is_file() and not packaged_example.is_file():
        shutil.copy2(example, packaged_example)

    target = out / "config.yaml"
    if target.is_file():
        return
    project = ROOT / "config.yaml"
    if project.is_file():
        shutil.copy2(project, target)
    elif example.is_file():
        shutil.copy2(example, target)


def _write_package_readme(out: Path) -> None:
    (out / "使用说明.txt").write_text(
        "ASMR 收藏馆\n"
        "===========\n\n"
        "这是轻量桌面版。在线内容优先在系统 WebView2 的受控播放器中打开，\n"
        "自动拦截常见广告请求、弹窗和越界跳转；不可用时回退到系统默认浏览器。\n"
        "本地媒体可在收藏馆内播放。\n\n"
        "数据和配置位于本文件旁的 data\\ 与 config.yaml。重新构建时会优先保留已有数据，\n"
        "并在首次构建时尝试继承 dist\\asmrlib-light\\ 中的 data\\ 和 config.yaml。\n\n"
        "本产品不包含 Playwright、内置 Chromium、在线录制或代理播放器。\n"
        "需要 Windows WebView2 才能运行桌面窗口；网络内容仍需联网。\n",
        encoding="utf-8",
    )


def _pyinstaller_command(stage_dist: Path, work_dir: Path) -> list[str]:
    add_data: list[str] = []
    example = ROOT / "config.example.yaml"
    if example.is_file():
        add_data = ["--add-data", f"{example};."]

    icon_args: list[str] = []
    icon = ROOT / "icon.ico"
    if icon.is_file():
        icon_args = ["--icon", str(icon)]

    hidden_imports = (
        "asmrlib_archiver",
        "asmrlib_archiver.config",
        "asmrlib_archiver.viewer",
        "asmrlib_archiver.viewer.app",
        "asmrlib_archiver.viewer.assets",
        "asmrlib_archiver.viewer.components",
        "asmrlib_archiver.viewer.library",
        "asmrlib_archiver.viewer.live",
        "asmrlib_archiver.viewer.playback",
        "asmrlib_archiver.viewer.preview",
        "asmrlib_archiver.viewer.util",
        "asmrlib_archiver.online_shield",
        "webview",
    )
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onedir",
        "--noupx",
        "--name",
        DIST_NAME,
        "--paths",
        str(ROOT / "src"),
        "--distpath",
        str(stage_dist),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(work_dir),
        *icon_args,
    ]
    for module in hidden_imports:
        command.extend(["--hidden-import", module])
    command.extend(["--collect-all", "webview", *add_data, str(ROOT / "desktop_main.py")])
    for module in FORBIDDEN_MODULES:
        command.extend(["--exclude-module", module])
    return command


def _swap_output(staged: Path, final: Path) -> Path | None:
    """Install staged code without deleting an existing package."""
    backup: Path | None = None
    if final.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        backup = DIST_ROOT / f"{DIST_NAME}.old-{stamp}"
        try:
            final.rename(backup)
        except OSError as exc:
            raise RuntimeError(
                f"cannot replace {final}; close the running app and retry: {exc}"
            ) from exc
    try:
        staged.rename(final)
    except Exception:
        if backup is not None and not final.exists():
            backup.rename(final)
        raise
    return backup


def _rollback_output(final: Path, backup: Path | None) -> Path | None:
    """Move a failed new output aside and restore the previous package."""
    failed: Path | None = None
    if final.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        failed = DIST_ROOT / f"{DIST_NAME}.failed-{stamp}"
        final.rename(failed)
    if backup is not None and backup.exists():
        backup.rename(final)
    return failed


def _report_size(out: Path) -> None:
    runtime = _dir_bytes(out / "_internal") + _dir_bytes(out / f"{DIST_NAME}.exe")
    full = _dir_bytes(out)
    print(
        f"Bundle size: runtime={runtime / 1024 / 1024:.1f} MiB, "
        f"with data={full / 1024 / 1024:.1f} MiB",
        flush=True,
    )
    if runtime > MAX_RUNTIME_BYTES:
        raise RuntimeError(
            f"runtime is {runtime / 1024 / 1024:.1f} MiB; "
            f"the limit is {MAX_RUNTIME_BYTES / 1024 / 1024:.0f} MiB"
        )


def _assert_light_bundle(out: Path) -> None:
    """Reject accidentally collected browser/recorder runtime directories."""
    # pywebview ships a small ``platforms/edgechromium.py`` adapter.  It is
    # only a backend selector and must not be treated as an embedded browser.
    # Check path components and known runtime artifacts instead of matching
    # the substring ``chromium`` in every filename.
    forbidden_components = {
        "playwright",
        "ms-playwright",
        "sandbox_player",
        "proxy_player",
        "recorder",
    }
    browser_binary_suffixes = {".exe", ".dll", ".zip", ".tar", ".7z"}
    hits: list[Path] = []
    for item in out.rglob("*"):
        relative_parts = item.relative_to(out).parts
        lowered_parts = {part.lower() for part in relative_parts}
        if lowered_parts & forbidden_components:
            hits.append(item)
            continue
        name = item.name.lower()
        if "chromium" in name and item.suffix.lower() in browser_binary_suffixes:
            hits.append(item)
    if hits:
        shown = ", ".join(str(item.relative_to(out)) for item in hits[:8])
        raise RuntimeError(f"forbidden runtime content was bundled: {shown}")


def build(*, dry_run: bool = False) -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "PyInstaller is required. Install the desktop extra with "
            '`pip install -e ".[desktop]"`.',
            file=sys.stderr,
        )
        return 1

    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    final = DIST_ROOT / DIST_NAME
    data_source, config_source = _state_sources(final)
    stage_root: Path | None = None
    snapshot: Path | None = None
    try:
        stage_root = Path(tempfile.mkdtemp(prefix=".asmrlib-build-", dir=DIST_ROOT))
        stage_dist = stage_root / "dist"
        stage_dist.mkdir()
        work_dir = ROOT / "build" / f"pyinstaller-{DIST_NAME}"
        command = _pyinstaller_command(stage_dist, work_dir)
        print("Running:", " ".join(command), flush=True)
        if dry_run:
            print("Dry run: no files were generated or replaced.", flush=True)
            return 0

        snapshot = _snapshot_state(data_source, config_source)
        proc = subprocess.run(command, cwd=ROOT)
        if proc.returncode != 0:
            return proc.returncode
        staged = stage_dist / DIST_NAME
        executable = staged / f"{DIST_NAME}.exe"
        if not executable.is_file():
            raise RuntimeError(f"PyInstaller did not produce {executable}")

        backup = _swap_output(staged, final)
        try:
            restored, copied = _restore_state(final, snapshot)
            if not restored:
                copied = _copy_seed_data(final)
            _runtime_config(final)
            _write_package_readme(final)
            _assert_light_bundle(final)
            _report_size(final)
        except Exception:
            failed = _rollback_output(final, backup)
            if failed is not None:
                print(f"Failed output kept for inspection at {failed}", file=sys.stderr)
            raise
        print(
            f"Done: {final / executable.name} "
            f"({'preserved user data' if restored else 'seeded repository data'}, "
            f"{copied / 1024 / 1024:.1f} MiB data)"
            + (f"; previous output kept at {backup}" if backup else ""),
            flush=True,
        )
        return 0
    finally:
        if snapshot is not None:
            shutil.rmtree(snapshot, ignore_errors=True)
        if stage_root is not None:
            shutil.rmtree(stage_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    unknown = [arg for arg in args if arg != "--dry-run"]
    if unknown:
        print(f"Unknown argument(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    return build(dry_run="--dry-run" in args)


if __name__ == "__main__":
    raise SystemExit(main())
