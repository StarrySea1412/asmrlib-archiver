"""Deprecated compatibility wrapper for the single desktop build.

There is no longer a separate full desktop/Chromium product.  Keep this
module as a small forwarding shim for scripts that still invoke the old file;
all arguments and behavior are owned by :mod:`build_light`.
"""

from __future__ import annotations

import sys

from build_light import main


if __name__ == "__main__":
    print(
        "build_desktop.py is deprecated; use build_light.py "
        "(dist/asmrlib-archiver/asmrlib-archiver.exe).",
        file=sys.stderr,
    )
    raise SystemExit(main())
