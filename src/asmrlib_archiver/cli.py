from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .crawler import Archiver
from .export import SUPPORTED_FORMATS, CatalogExporter
from .import_media import LocalMediaImporter
from .storage import Storage

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None
    Table = None


def _console_print(message: str) -> None:
    if Console:
        Console().print(message, markup=False)
    else:
        print(message, flush=True)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asmrlib-archiver",
        description="Strict ad-free tag and detail-page archiver for authorized ASMRLIB pages.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml. Defaults to ASMRLIB_ARCHIVER_CONFIG or ./config.yaml.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create data directories and SQLite schema.")

    add = sub.add_parser("add", help="Add tag or post seed URLs from config and a text file.")
    add.add_argument(
        "file",
        nargs="?",
        default=None,
        help="Optional text file with one URL per line.",
    )

    sanitize = sub.add_parser("sanitize", help="Replace legacy raw HTML with safe static archives.")
    sanitize.set_defaults(command="sanitize")

    discover = sub.add_parser("discover", help="Discover post URLs from configured tag pages.")
    discover.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help="Maximum tag pages this run.",
    )
    discover.add_argument("--retry-errors", action="store_true")

    crawl = sub.add_parser("crawl", help="Fetch post pages and write safe static archives.")
    crawl.add_argument("--limit", type=_positive_int, default=20)
    crawl.add_argument("--retry-errors", action="store_true")
    crawl.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch already archived/crawled posts to refresh media references.",
    )
    crawl.add_argument(
        "--covers-only",
        action="store_true",
        help="Backfill missing covers only (no HTML/media rewrite).",
    )

    download = sub.add_parser("download", help="Download allowed direct media candidates.")
    download.add_argument("--limit", type=_positive_int, default=20)
    download.add_argument("--retry-errors", action="store_true")

    run = sub.add_parser("run", help="Sanitize, discover tags, crawl posts, then download.")
    run.add_argument("file", nargs="?", default=None)
    run.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help="Maximum posts this run.",
    )
    run.add_argument(
        "--page-limit",
        type=_positive_int,
        default=None,
        help="Maximum tag pages this run.",
    )
    run.add_argument("--retry-errors", action="store_true")
    run.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch already archived posts during the crawl stage.",
    )

    export = sub.add_parser(
        "export",
        help="Export archived catalog (metadata + player references, no protected media).",
    )
    export.add_argument(
        "--format",
        choices=SUPPORTED_FORMATS,
        default="json",
        help="Export format (default json).",
    )
    export.add_argument(
        "--output",
        default=None,
        help="Output path. Defaults to data/exports/catalog_<timestamp>.<ext>.",
    )
    export.add_argument("--query", default="", help="Optional title/URL/author filter.")
    export.add_argument("--tag", default="", help="Optional tag slug filter.")

    import_media = sub.add_parser(
        "import-media",
        help="Attach a local file you already own so the library can play it.",
    )
    import_media.add_argument(
        "--source",
        required=True,
        help="Post URL, 32-hex post id, or unique title fragment.",
    )
    import_media.add_argument(
        "--file",
        required=True,
        help="Path to a local media file (mp3/mp4/…).",
    )
    import_media.add_argument("--label", default="", help="Optional display label.")
    import_media.add_argument(
        "--no-copy",
        action="store_true",
        help="Link the original path instead of copying into data/videos/.",
    )

    serve = sub.add_parser("serve", help="Open a local read-only library viewer.")
    serve.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
    serve.add_argument(
        "--port",
        type=_positive_int,
        default=8765,
        help="Bind port (default 8765).",
    )

    sub.add_parser("status", help="Show database counts.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    archiver = Archiver(config, progress=_console_print)
    try:
        if args.command == "init-db":
            archiver.init()
            _console_print(f"Initialized: {config.database_path}")
            return 0

        if args.command == "add":
            archiver.init()
            added = archiver.add_config_seeds()
            if args.file:
                added += archiver.add_seed_file(Path(args.file))
            _console_print(f"Added {added} new seed(s).")
            return 0

        if args.command == "sanitize":
            _console_print("Sanitizing existing archives...")
            sanitized, failed = archiver.sanitize_existing()
            _console_print(f"Sanitize complete. sanitized={sanitized} failed={failed}")
            return 0 if failed == 0 else 1

        if args.command == "discover":
            _console_print("Discovering tag pages...")
            archiver.init()
            archiver.add_config_seeds()
            result = archiver.discover(limit=args.limit, retry_errors=args.retry_errors)
            unresolved = archiver.unresolved()["tag_pages"]
            _console_print(
                "Discovery complete. "
                f"pages={result.pages_ok} failed={result.pages_failed} "
                f"posts_added={result.posts_added} duplicates={result.duplicate_pages} "
                f"incomplete={result.incomplete} unresolved={unresolved}"
            )
            return 0 if result.pages_failed == 0 and unresolved == 0 else 1

        if args.command == "crawl":
            if args.covers_only:
                _console_print("Backfilling missing covers...")
                ok, failed = archiver.crawl_covers_only(limit=args.limit)
                _console_print(
                    f"Covers-only crawl complete. backfilled={ok} failed_or_no_cover={failed}"
                )
                return 0 if failed == 0 else 1
            _console_print("Crawling post pages...")
            ok, failed = archiver.crawl(
                limit=args.limit,
                retry_errors=args.retry_errors,
                refresh=args.refresh,
            )
            unresolved = archiver.unresolved()["items"]
            _console_print(
                f"Crawl complete. ok={ok} failed={failed} unresolved={unresolved}"
            )
            return 0 if failed == 0 and unresolved == 0 else 1

        if args.command == "download":
            _console_print("Downloading media...")
            ok, failed = archiver.download(limit=args.limit, retry_errors=args.retry_errors)
            unresolved = archiver.unresolved()["media"]
            _console_print(
                f"Download complete. ok={ok} failed={failed} unresolved={unresolved}"
            )
            return 0 if failed == 0 and unresolved == 0 else 1

        if args.command == "run":
            _console_print("Starting run.")
            archiver.init()
            _console_print("Stage 1/4: sanitize")
            sanitized, sanitize_failed = archiver.sanitize_existing()
            added = archiver.add_config_seeds()
            if args.file:
                added += archiver.add_seed_file(Path(args.file))
            _console_print("Stage 2/4: discover")
            discovery = archiver.discover(
                limit=args.page_limit,
                retry_errors=args.retry_errors,
            )
            tag_count = max(1, archiver.status().get("tags.total", 0))
            post_limit = args.limit or config.discovery.max_posts_per_tag * tag_count
            _console_print("Stage 3/4: crawl")
            crawled, crawl_failed = archiver.crawl(
                limit=post_limit,
                retry_errors=args.retry_errors,
                refresh=args.refresh,
            )
            _console_print("Stage 4/4: download")
            downloaded, download_failed = archiver.download(
                limit=post_limit * 20,
                retry_errors=args.retry_errors,
            )
            unresolved = archiver.unresolved()
            _console_print(
                "Run complete. "
                f"sanitized={sanitized} sanitize_failed={sanitize_failed} added={added} "
                f"tag_pages={discovery.pages_ok} tag_failed={discovery.pages_failed} "
                f"posts_added={discovery.posts_added} incomplete={discovery.incomplete} "
                f"crawled={crawled} crawl_failed={crawl_failed} "
                f"downloaded={downloaded} download_failed={download_failed} "
                f"unresolved_tags={unresolved['tag_pages']} "
                f"unresolved_items={unresolved['items']} "
                f"unresolved_media={unresolved['media']}"
            )
            failures = (
                sanitize_failed
                + discovery.pages_failed
                + discovery.incomplete
                + crawl_failed
                + download_failed
                + sum(unresolved.values())
            )
            return 0 if failures == 0 else 1

        if args.command == "export":
            archiver.init()
            exporter = CatalogExporter(archiver.db, Storage(config.output_dir))
            result = exporter.export(
                fmt=args.format,
                output=Path(args.output) if args.output else None,
                query=args.query,
                tag=args.tag,
            )
            _console_print(
                "Export complete. "
                f"path={result.path} posts={result.posts} media_rows={result.media_rows} "
                f"references={result.references} downloaded={result.downloaded}"
            )
            _console_print(
                "Note: references are catalog-only. Protected third-party players are not fetched."
            )
            return 0

        if args.command == "import-media":
            archiver.init()
            importer = LocalMediaImporter(archiver.db, Storage(config.output_dir))
            try:
                result = importer.import_file(
                    source=args.source,
                    file_path=Path(args.file),
                    label=args.label,
                    copy=not args.no_copy,
                )
            except (FileNotFoundError, ValueError) as exc:
                _console_print(f"Import failed: {exc}")
                return 1
            _console_print(
                "Import complete. "
                f"title={result.title!r} source={result.source_url} "
                f"media_id={result.media_id} path={result.stored_path} "
                f"copied={result.copied}"
            )
            _console_print("Run `asmrlib-archiver serve` and open the post to play locally.")
            return 0

        if args.command == "serve":
            from .viewer import ArchiveViewer

            archiver.close()
            viewer = ArchiveViewer(config)
            viewer.serve(host=args.host, port=args.port)
            return 0

        if args.command == "status":
            counts = archiver.status()
            if Console and Table:
                table = Table(title="Archive Status")
                table.add_column("Bucket")
                table.add_column("Count", justify="right")
                for key in sorted(counts):
                    table.add_row(key, str(counts[key]))
                Console().print(table)
            else:
                for key in sorted(counts):
                    print(f"{key}: {counts[key]}")
            refs = counts.get("media.reference", 0)
            downloaded = counts.get("media.downloaded", 0)
            _console_print(
                f"Playback reality: local_files={downloaded} player_references={refs} "
                "(references are not downloadable by this tool)."
            )
            return 0

        raise AssertionError(f"Unhandled command: {args.command}")
    finally:
        if args.command != "serve":
            archiver.close()


if __name__ == "__main__":
    raise SystemExit(main())
