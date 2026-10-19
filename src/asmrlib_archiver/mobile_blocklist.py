"""Regenerate the Android APK blocklist asset from the Python security tables.

    python -m asmrlib_archiver.mobile_blocklist

Writes ``android/app/src/main/assets/blocklist.json`` relative to the repo
root. The APK replays desktop shield decisions from this file; the sync is
enforced by tests, so rerun this command after every change to
``security_lists.py`` or ``config.yaml``'s ``browser.block_url_keywords``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from .online_shield import export_mobile_blocklist

DEFAULT_ASSET = (
    Path(__file__).resolve().parents[2]
    / "android"
    / "app"
    / "src"
    / "main"
    / "assets"
    / "blocklist.json"
)


def write_blocklist(destination: Path = DEFAULT_ASSET) -> Path:
    payload = export_mobile_blocklist()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return destination


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ASSET
    path = write_blocklist(target)
    print(f"blocklist written: {path}")
