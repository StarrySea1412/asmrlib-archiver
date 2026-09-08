from __future__ import annotations

from html import escape

from bs4 import BeautifulSoup, Tag

from .models import ParsedPage, ParsedTagPage

ARCHIVE_MARKER = "asmrlib-archive-safe-v1"
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; base-uri 'none'; form-action 'none'; frame-src 'none'; "
    "object-src 'none'; script-src 'none'; connect-src 'none'; img-src 'self' data:; "
    "media-src 'self'; style-src 'none'"
)
FORBIDDEN_TAGS = {
    "applet",
    "base",
    "embed",
    "form",
    "frame",
    "frameset",
    "iframe",
    "link",
    "noscript",
    "object",
    "script",
    "style",
    "svg",
    "template",
}
URI_ATTRIBUTES = {
    "action",
    "background",
    "cite",
    "data",
    "formaction",
    "href",
    "ping",
    "poster",
    "src",
    "srcdoc",
    "srcset",
}


def render_detail_archive(
    page: ParsedPage,
    *,
    local_media: list[dict[str, str]] | None = None,
) -> str:
    body = [
        "<main>",
        f"<h1>{_text(page.title or 'Archived page')}</h1>",
        "<dl>",
        f"<dt>Source</dt><dd><code>{_text(page.source_url)}</code></dd>",
    ]
    if page.author:
        body.append(f"<dt>Author</dt><dd>{_text(page.author)}</dd>")
    if page.published_at:
        body.append(f"<dt>Published</dt><dd>{_text(page.published_at)}</dd>")
    body.append("</dl>")
    body.extend(_text_list("Tags", page.tags))
    body.extend(_text_list("Servers", page.servers))

    # Static detail archives are written to <output>/html/<key>.html and served
    # back through /file/html/<key>.html. Local media files live in <output>/videos/
    # and are served through /file/videos/<name>. A bare relative src like
    # "videos/x.mp4" would resolve against the document's directory
    # (/file/html/...) to /file/html/videos/x.mp4, which 404s. To point at the
    # real /file/videos/x.mp4 the src must climb one level: "../videos/x.mp4".
    # _normalize_local_media_src below keeps the CSP safety invariant "src must
    # stay inside the archive root" by checking the resolved path can't escape.
    playable = [item for item in (local_media or []) if item.get("src")]
    if playable:
        body.extend(["<section>", "<h2>Local media</h2>"])
        for item in playable:
            label = item.get("label") or item.get("kind") or "media"
            src = _normalize_local_media_src(item["src"])
            kind = (item.get("kind") or "").lower()
            tag = "audio" if kind.startswith("audio") or src.lower().endswith(
                (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg")
            ) else "video"
            body.extend(
                [
                    f"<p><strong>{_text(label)}</strong></p>",
                    f'<{tag} controls preload="metadata" src="{_text(src)}"></{tag}>',
                ]
            )
        body.append("</section>")

    if page.media:
        body.extend(["<section>", "<h2>Media references</h2>", "<ul>"])
        for candidate in page.media:
            label = candidate.label or candidate.kind or "media"
            status = candidate.status or ""
            body.append(
                "<li>"
                f"<strong>{_text(label)}</strong> "
                f"<code>{_text(candidate.media_url)}</code> "
                f"<span>{_text(candidate.kind)}</span>"
                f"{f' <em>{_text(status)}</em>' if status else ''}"
                "</li>"
            )
        body.extend(["</ul>", "</section>"])
    if page.text_excerpt:
        body.extend([
            "<section>",
            "<h2>Content</h2>",
            f"<pre>{_text(page.text_excerpt)}</pre>",
            "</section>",
        ])
    body.append("</main>")
    return _document(page.title or "Archived page", body)


def render_tag_archive(page: ParsedTagPage) -> str:
    body = [
        "<main>",
        f"<h1>{_text(page.title or 'Archived tag page')}</h1>",
        f"<p>Source: <code>{_text(page.source_url)}</code></p>",
    ]
    body.extend(_text_list("Posts", page.post_urls, code=True))
    body.extend(_text_list("Pagination", page.next_pages, code=True))
    if page.text_excerpt:
        body.extend([
            "<section>",
            "<h2>Content</h2>",
            f"<pre>{_text(page.text_excerpt)}</pre>",
            "</section>",
        ])
    body.append("</main>")
    return _document(page.title or "Archived tag page", body)


def render_archive_html(page: ParsedPage | ParsedTagPage) -> str:
    if isinstance(page, ParsedTagPage):
        return render_tag_archive(page)
    return render_detail_archive(page)


def is_safe_archive_html(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("html")
    if not isinstance(root, Tag) or root.get("data-asmrlib-archive") != ARCHIVE_MARKER:
        return False
    marker = soup.find("meta", attrs={"name": "asmrlib-archive"})
    if not isinstance(marker, Tag) or marker.get("content") != ARCHIVE_MARKER:
        return False
    csp = soup.find(
        "meta",
        attrs={
            "http-equiv": lambda value: value
            and value.lower() == "content-security-policy"
        },
    )
    if not isinstance(csp, Tag) or csp.get("content") != CONTENT_SECURITY_POLICY:
        return False
    if soup.find(FORBIDDEN_TAGS):
        return False

    for node in soup.find_all(True):
        for raw_name, raw_value in node.attrs.items():
            name = str(raw_name).lower()
            if name.startswith("on") or name == "style":
                return False
            if name in URI_ATTRIBUTES and not _is_allowed_local_media_attr(node, name, raw_value):
                return False
        if node.name == "meta" and node.get("http-equiv") and str(node.get("http-equiv")).lower() != "content-security-policy":
            return False
    return True


def _is_allowed_local_media_attr(node: Tag, name: str, raw_value: object) -> bool:
    """Allow only relative local media sources on video/audio players.

    Archives are served at /file/html/<key>.html and media at /file/videos/<x>,
    so a playable src is "../videos/<x>" (climb one dir from html/ to root,
    then into videos/). The safety invariant is "resolves inside the archive
    root": a single leading "../" to reach the root is fine; any deeper
    traversal, a scheme/blob/data, an absolute path, or a drive letter is not.
    """
    if name != "src" or node.name not in {"video", "audio"}:
        return False
    if isinstance(raw_value, list):
        if len(raw_value) != 1:
            return False
        value = str(raw_value[0]).strip()
    else:
        value = str(raw_value).strip()
    if not value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    lowered = value.lower()
    if lowered.startswith(("http:", "https:", "//", "data:", "blob:", "javascript:")):
        return False
    if "\\" in value:
        return False
    segments = value.split("/")
    # Permit exactly one leading ".." (html/<key>.html -> ../videos/<x>). Any
    # further ".." could escape the archive root.
    if ".." in segments[1:]:
        return False
    return not (value.startswith("/") or ":" in segments[0])


def _normalize_local_media_src(src: str) -> str:
    """Rewrite a root-relative media path into a document-relative one.

    ``_local_media_for_render`` returns paths like ``videos/<name>`` (relative
    to the archive root). The detail archive lives one directory deeper, at
    ``html/<key>.html`` served through ``/file/html/<key>.html``, so a bare
    ``videos/<name>`` resolves to ``/file/html/videos/<name>`` and 404s. Prefix
    ``../`` to climb back to the root: ``../videos/<name>`` resolves to
    ``/file/videos/<name>``. Idempotent — if the caller already prefixed
    ``../`` we don't double it.
    """
    if not src:
        return src
    if src.startswith("../"):
        return src
    if src.startswith("/"):
        # Absolute path against root: turn into a same-document-ish climb.
        # ``/videos/x`` -> ``../videos/x`` (one level up from html/).
        return "../" + src.lstrip("/")
    return "../" + src


def is_safe(html: str) -> bool:
    return is_safe_archive_html(html)


def _document(title: str, body: list[str]) -> str:
    head = [
        "<!doctype html>",
        f'<html lang="en" data-asmrlib-archive="{ARCHIVE_MARKER}">',
        "<head>",
        '<meta charset="utf-8">',
        f'<meta name="asmrlib-archive" content="{ARCHIVE_MARKER}">',
        '<meta http-equiv="Content-Security-Policy" '
        f'content="{escape(CONTENT_SECURITY_POLICY, quote=True)}">',
        f"<title>{_text(title)}</title>",
        "</head>",
        "<body>",
    ]
    return "\n".join([*head, *body, "</body>", "</html>", ""])


def _text_list(heading: str, values: list[str], *, code: bool = False) -> list[str]:
    if not values:
        return []
    result = ["<section>", f"<h2>{_text(heading)}</h2>", "<ul>"]
    tag = "code" if code else "span"
    result.extend(f"<li><{tag}>{_text(value)}</{tag}></li>" for value in values)
    result.extend(["</ul>", "</section>"])
    return result


def _text(value: str) -> str:
    return escape(value, quote=True)
