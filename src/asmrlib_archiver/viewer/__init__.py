"""Local archive library HTTP viewer (package).

Public surface is re-exported here so existing imports keep working:

    from asmrlib_archiver.viewer import ArchiveViewer
    from asmrlib_archiver.viewer import DEFAULT_CSP, _parse_byte_range, _safe_filename
"""

from __future__ import annotations

from .app import ArchiveViewer
from .assets import DEFAULT_CSP, PAGE_SIZE, TOOL_CSP, WATCH_CSP
from .preview import VIDEO_PREVIEW_EXTS, generate_video_preview
from .util import _h, _parse_byte_range, _safe_filename, _tag_slug

__all__ = [
    "ArchiveViewer",
    "DEFAULT_CSP",
    "PAGE_SIZE",
    "TOOL_CSP",
    "WATCH_CSP",
    "VIDEO_PREVIEW_EXTS",
    "generate_video_preview",
    "_parse_byte_range",
    "_safe_filename",
    "_h",
    "_tag_slug",
]
