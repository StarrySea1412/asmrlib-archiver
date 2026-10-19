"""Locating a JDK, so the pure-Java tests can actually compile and run.

Two modules need this — ``test_hls_downloader_java`` and
``test_playback_resume_java`` — and neither can do anything without it, so it
lives in one place instead of being copied into both.  Both tests skip cleanly
when no JDK is present, which keeps the suite portable.

``tests/`` is on ``sys.path`` under pytest's default import mode, so this is a
plain top-level import for its two callers.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Where a JDK hides when it is not on PATH.  The first two entries are this
# machine's; the rest are the usual Windows install roots, so the tests do not
# silently skip on a machine that has a JDK but a different JAVA_HOME.
_JDK_ROOTS = (
    "D:/jdk17/jdk-17.0.2",
    "/d/jdk17/jdk-17.0.2",
    "C:/Program Files/Java/jdk-17.0.2",
    "C:/Program Files/Java/jdk-17",
)


def jdk_tool(name: str) -> str | None:
    """Locate a JDK tool, preferring PATH and then the usual install roots."""

    exe = name + (".exe" if os.name == "nt" else "")
    found = shutil.which(exe) or shutil.which(name)
    if found:
        return found
    for base in (os.environ.get("JAVA_HOME"), *_JDK_ROOTS):
        if not base:
            continue
        candidate = Path(base) / "bin" / exe
        if candidate.is_file():
            return str(candidate)
    return None
