from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlsplit

from .components import button, empty_state
from .player_guard import PlayerUrlError, policy_from_config
from .util import _h, _player_tag


class PlaybackPages:
    """Local playback pages and the external-browser landing page.

    Remote media is intentionally never fetched or embedded by the local
    viewer. The landing page offers the controlled desktop player first, with
    an explicit system-browser hand-off as a fallback/escape hatch.
    """

    def _is_mini(self, query: dict[str, list[str]]) -> bool:
        raw = (query.get("mini") or ["0"])[0].strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _external_policy(self):
        return policy_from_config(self.config)

    def _watch_target(self, query: dict[str, list[str]]) -> str:
        raw_url = (query.get("url") or [""])[0].strip()
        return self._external_policy().validate(raw_url)

    def _watch_http_status(self, query: dict[str, list[str]]) -> int:
        try:
            self._watch_target(query)
        except PlayerUrlError:
            return 400
        return 200

    def _render_watch(self, query: dict[str, list[str]]) -> str:
        """Render a no-script-required external browser hand-off page."""

        body: list[str] = []
        try:
            target = self._watch_target(query)
        except PlayerUrlError as exc:
            body.append(
                empty_state(
                    "Unable to open link",
                    f"{_h(str(exc))}. Only approved player and archive hosts are allowed.",
                    f"<p>{button('Back to library', href='/')}</p>",
                    kicker="BROWSER",
                )
            )
            return self._page("Open in browser", body)

        host = urlsplit(target).netloc or "player"
        # Keep real anchors for command-line `serve` mode. The desktop JS
        # action intercepts the primary click and calls DesktopApi, while
        # ordinary browsers fall back to a normal user-gesture popup.
        escaped_target = _h(target)
        js_target = target.replace("\\", "\\\\").replace("'", "\\'")
        body.extend(
            [
                "<p class='crumb'><a href='/'>Library</a>"
                "<span class='crumb-sep'>/</span>Online playback</p>",
                "<section class='watch-shell external-watch-shell'>",
                "<div class='watch-toolbar'>",
                "<div class='watch-toolbar-main'>",
                "<p class='external-watch-kicker'>SYSTEM BROWSER</p>",
                f"<h1>Play <code>{_h(host)}</code></h1>",
                "<p class='muted'>Desktop playback automatically intercepts popups "
                "and ad redirects. Without the desktop bridge, the action falls "
                "back to your system browser.</p>",
                "</div>",
                "<div class='watch-toolbar-actions'>",
                f"<a class='button' href='{escaped_target}' target='_blank' "
                f"rel='noopener noreferrer' onclick=\"return openDesktopOnline('{_h(js_target)}')\">"
                "安全播放</a>",
                f"<a class='text-link' href='{escaped_target}' target='_blank' "
                f"rel='noopener noreferrer' onclick=\"return openDesktopExternal('{_h(js_target)}')\">"
                "打开原链</a>",
                "<a class='button button-secondary' href='/'>Back</a>",
                "</div></div>",
                f"<p class='muted watch-url'><code>{escaped_target}</code></p>",
                "</section>",
            ]
        )
        return self._page(f"Open in browser - {host}", body)

    @staticmethod
    def _valid_local_src(src: str) -> bool:
        """Accept only a root-relative media path with no traversal."""

        raw = (src or "").strip()
        if not raw or "\\" in raw:
            return False
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return False
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return False
        decoded = unquote(parsed.path)
        if not decoded.startswith("/media/"):
            return False
        return ".." not in decoded.split("/")

    def _render_watch_local(self, query: dict[str, list[str]]) -> str:
        """Compact local file player for the desktop mini window."""

        mini = self._is_mini(query)
        src = (query.get("src") or [""])[0].strip()
        title = (query.get("title") or ["Local playback"])[0].strip() or "Local playback"
        body: list[str] = []

        if not self._valid_local_src(src):
            body.append(
                empty_state(
                    "Invalid local path",
                    "Only local files under /media/ can be played.",
                    kicker="LOCAL",
                )
            )
            return self._page(title, body, mini=mini)

        suffix = Path(unquote(src)).suffix.lower()
        player = _player_tag(suffix, src, autoplay=mini)
        if mini:
            body.extend(
                [
                    "<section class='watch-shell watch-shell-mini watch-shell-local'>",
                    "<div class='mini-bar pywebview-drag-region'>",
                    f"<strong class='mini-title' title='{_h(title)}'>{_h(title)}</strong>",
                    "<span class='mini-tag'>LOCAL - PINNED</span>",
                    "</div>",
                    f"<div class='mini-local-player'>{player}</div>",
                    "</section>",
                ]
            )
        else:
            body.extend(
                [
                    "<p class='crumb'><a href='/'>Library</a>"
                    "<span class='crumb-sep'>/</span>Local playback</p>",
                    "<section class='watch-shell'>",
                    f"<h1>{_h(title)}</h1>",
                    f"<div class='media-card-player'>{player}</div>",
                    "</section>",
                ]
            )
        return self._page(title, body, mini=mini)
