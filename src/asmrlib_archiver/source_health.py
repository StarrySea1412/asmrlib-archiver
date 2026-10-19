"""Health-aware ordering for online media sources.

Why this exists
---------------
``player_resolver._rank`` is a static guess read off the label and the URL.  It
cannot notice that the source it prefers has been dead since yesterday, so
every playback re-tries the dead source first and the user has to switch by
hand.  This module turns "which source should we try first?" into a decision
derived from *measurements* of earlier attempts, while keeping a source nobody
has ever tried fully worth trying.

Where the numbers live
----------------------
:class:`SourceHealth` is the in-memory shape of one ``source_health`` row,
persisted by ``asmrlib_archiver.db.health.SourceHealthRepo`` in the same sqlite
archive DB as everything else.  The key is ``health_key(url)`` — a hash of the
resolved source URL, never the display label, which changes with the site's
markup ("BI" -> "线路1" while the embed URL stays the same).

The ordering rule (read this to predict the order)
--------------------------------------------------
``rank_sources`` sorts by a tuple compared left to right:

1. **tier** — ``0`` when the user pinned the source manually, ``2`` when the
   source failed inside the current cooldown window, ``1`` otherwise.  A manual
   pin therefore beats everything, including a cooldown; a source that just
   failed is only reached after every source that is not cooling down.
2. **-score** — ``score = success_count - failure_count - 2 * consecutive_failures``.
   Higher scores sort first, so a source that has actually succeeded outranks
   one that has only failed, and a source with *no history at all* scores ``0``:
   neutral (worth trying), never bad.
3. **latency** — measured first-byte/first-segment latency in ms, ascending.
   ``-1`` means "never measured" and sorts after a measured source.
4. **static rank** — ``player_resolver._rank``'s server order.  With no
   measurements at all this reproduces the site's own preference.

Ties fall back to the URL, so the order is total and reproducible.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit

# A source needs this many consecutive failures before it is reported as
# literally "cooling down".  A single failure already demotes it and, for the
# next window, keeps the next attempt from starting there.
COOLDOWN_AFTER_FAILURES = 2
# Length of the window in which a failure keeps a source out of the front.
DEFAULT_COOLDOWN_SECONDS = 15 * 60

# Sentinel stored in ``latency_ms`` for "this source never started playing".
UNMEASURED_LATENCY_MS = -1


def health_key(url: str) -> str:
    """Stable primary key for one source URL (hash, so tokens stay bounded)."""

    return hashlib.sha256(str(url or "").strip().encode("utf-8")).hexdigest()


def _to_moment(value: datetime | str | None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _recently_failed(record: SourceHealth | None, moment: datetime) -> bool:
    """True when the source failed inside the current cooldown window.

    This is the "do not start here next time" signal.  It needs only one
    failure — the strict, longer-lived cooldown reported by
    :meth:`SourceHealth.in_cooldown` needs ``COOLDOWN_AFTER_FAILURES``.
    """

    if record is None or not record.last_failure_at:
        return False
    try:
        failed_at = datetime.fromisoformat(record.last_failure_at)
    except ValueError:
        return False
    if not failed_at.tzinfo:
        failed_at = failed_at.replace(tzinfo=UTC)
    return failed_at + timedelta(seconds=DEFAULT_COOLDOWN_SECONDS) > moment


class PlaybackOutcome(StrEnum):
    """What a real playback attempt actually did.

    Selecting a source is not an outcome; only a frame that started playing
    (``PLAYING``) or one that did not (``FAILED``) may be recorded.
    """

    PLAYING = "playing"
    FAILED = "failed"


@dataclass(frozen=True)
class SourceHealth:
    """One source's measured playback history.  Every field is optional."""

    source_key: str = ""
    source_url: str = ""
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    last_success_at: str = ""
    last_failure_at: str = ""
    # First-byte / first-segment latency of the last successful start, in ms.
    latency_ms: int = UNMEASURED_LATENCY_MS
    last_error: str = ""

    @classmethod
    def from_row(cls, row) -> SourceHealth:
        """Build a record from a ``source_health`` sqlite row."""

        def value(name: str, default):
            try:
                raw = row[name]
            except (KeyError, IndexError, TypeError):
                return default
            return default if raw is None else raw

        return cls(
            source_key=str(value("source_key", "") or ""),
            source_url=str(value("source_url", "") or ""),
            success_count=int(value("success_count", 0) or 0),
            failure_count=int(value("failure_count", 0) or 0),
            consecutive_failures=int(value("consecutive_failures", 0) or 0),
            last_success_at=str(value("last_success_at", "") or ""),
            last_failure_at=str(value("last_failure_at", "") or ""),
            latency_ms=int(value("latency_ms", UNMEASURED_LATENCY_MS)),
            last_error=str(value("last_error", "") or ""),
        )

    @property
    def has_history(self) -> bool:
        return bool(self.success_count or self.failure_count)

    def cooldown_until(self) -> str:
        """End of the post-failure cooldown window ('' when not cooling down)."""

        if self.consecutive_failures < COOLDOWN_AFTER_FAILURES:
            return ""
        if not self.last_failure_at:
            return ""
        try:
            moment = datetime.fromisoformat(self.last_failure_at)
        except ValueError:
            return ""
        if not moment.tzinfo:
            moment = moment.replace(tzinfo=UTC)
        return _stamp(moment + timedelta(seconds=DEFAULT_COOLDOWN_SECONDS))

    def in_cooldown(self, now: datetime | str | None = None) -> bool:
        """True while consecutive failures still bar this source outright."""

        until = self.cooldown_until()
        if not until:
            return False
        return until > _stamp(_to_moment(now))

    def score(self) -> float:
        """Higher is healthier.  No history scores ``0`` (neutral)."""

        return (
            float(self.success_count)
            - float(self.failure_count)
            - 2.0 * float(self.consecutive_failures)
        )


@dataclass(frozen=True)
class RankedSource:
    """One candidate plus the health reasoning behind its position.

    ``in_cooldown`` means "this source failed recently, so the next attempt
    should not start here" — it is the flag the source row renders as cooling.
    """

    label: str
    url: str
    reason: str = ""
    in_cooldown: bool = False
    score: float = 0.0
    manual: bool = False


def describe_source_health(
    record: SourceHealth | None, *, now: datetime | str | None = None
) -> str:
    """One short, human-readable line explaining a source's current standing."""

    if record is None or not record.has_history:
        return "尚无记录"
    if record.in_cooldown(now):
        return f"冷却中（连续失败 {record.consecutive_failures} 次）"
    bits: list[str] = []
    if record.success_count:
        bits.append(f"成功 {record.success_count} 次")
    if record.failure_count:
        bits.append(f"失败 {record.failure_count} 次")
    if record.latency_ms >= 0:
        bits.append(f"{record.latency_ms}ms")
    return " · ".join(bits) if bits else "尚无记录"


def rank_sources(
    candidates: Iterable[tuple[str, str]],
    *,
    health: Mapping[str, SourceHealth] | None = None,
    preferred_url: str | None = None,
    static_rank: Callable[[str, str], int] | None = None,
    now: datetime | str | None = None,
) -> list[RankedSource]:
    """Order ``[(label, url)]`` candidates best-first; see the module docstring.

    Duplicates (same URL) collapse to their first label.  Sources with no
    health record are neutral: they land after known-good sources but ahead of
    sources that have only failed or are cooling down.  ``preferred_url`` is
    the user's manual choice for this post and outranks everything else.
    """

    health = health or {}
    preferred = str(preferred_url or "").strip()
    moment = _to_moment(now)

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, url in candidates:
        candidate_url = str(url or "")
        if not candidate_url or candidate_url in seen:
            continue
        seen.add(candidate_url)
        unique.append((str(label or ""), candidate_url))

    def record_for(url: str) -> SourceHealth | None:
        # Health is keyed by URL hash; the raw-URL lookup keeps callers that
        # build the mapping by hand (tests, ad-hoc tools) working.
        return health.get(health_key(url)) or health.get(url)

    def sort_key(item: tuple[str, str]) -> tuple[int, float, int, int, str]:
        label, url = item
        record = record_for(url)
        if preferred and url == preferred:
            tier = 0
        elif _recently_failed(record, moment):
            tier = 2
        else:
            tier = 1
        score = record.score() if record is not None else 0.0
        latency = record.latency_ms if record is not None else UNMEASURED_LATENCY_MS
        # "Never measured" sorts after a measured source inside an equal score.
        latency_key = latency if latency >= 0 else 1 << 30
        rank = static_rank(label, url) if static_rank is not None else 9
        return (tier, -score, latency_key, rank, url)

    ranked: list[RankedSource] = []
    for label, url in sorted(unique, key=sort_key):
        record = record_for(url)
        manual = bool(preferred) and url == preferred
        ranked.append(
            RankedSource(
                label=label,
                url=url,
                reason="手动首选" if manual else describe_source_health(record, now=moment),
                in_cooldown=_recently_failed(record, moment),
                score=record.score() if record is not None else 0.0,
                manual=manual,
            )
        )
    return ranked


def compose_failure_reason(
    failures: Sequence[tuple[str, str, str]] | Iterable[tuple[str, str, str]],
) -> str:
    """Readable reason shown when every candidate failed.

    Never returns an empty string: the player must never be left blank with no
    explanation.  Each line names the source by label *and* host so the user
    can tell "this link expired" from "this network cannot reach that host".
    """

    items = [
        (str(label or ""), str(url or ""), str(error or ""))
        for label, url, error in failures
    ]
    if not items:
        return "没有可用的播放来源，请返回详情页重试"
    parts: list[str] = []
    for label, url, error in items:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
        name = label or host or url
        where = f"（{host}）" if host and host != name else ""
        parts.append(f"{name}{where}：{error or '无法加载'}")
    return (
        f"{len(items)} 个播放来源都失败了，可能链接已失效或当前网络不可达。"
        + "；".join(parts)
        + "。可以点「重试」，或改用系统浏览器打开原页面。"
    )


@dataclass
class PlaybackSession:
    """Walk an already-ranked candidate list, recording every real attempt.

    This is the policy the player page's script mirrors one step at a time:
    ``report(FAILED)`` records the failure and hands back the next candidate,
    ``report(PLAYING)`` records the success and ends the walk, and once the
    list is exhausted :meth:`failure_reason` explains what happened instead of
    leaving a blank player.

    Selecting a source is *not* an attempt: constructing a session and reading
    :attr:`current` writes nothing, so a source can never earn a success it did
    not have.
    """

    sources: list[RankedSource]
    recorder: Callable[..., None] | None = None
    started: RankedSource | None = None
    _index: int = 0
    _failures: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def current(self) -> RankedSource | None:
        """The candidate the next attempt should use (``None`` once exhausted)."""

        if self.started is not None:
            return self.started
        if 0 <= self._index < len(self.sources):
            return self.sources[self._index]
        return None

    @property
    def exhausted(self) -> bool:
        return self.started is None and self._index >= len(self.sources)

    def report(
        self,
        outcome: PlaybackOutcome,
        *,
        latency_ms: int = UNMEASURED_LATENCY_MS,
        error: str = "",
    ) -> RankedSource | None:
        """Record one real attempt and advance past a failure."""

        current = self.current
        if current is None:
            return None
        if outcome is PlaybackOutcome.PLAYING:
            # Only a frame that actually started playing earns a success.
            self._record(current.url, outcome, max(int(latency_ms), 0), "")
            self.started = current
            return current
        self._record(current.url, outcome, UNMEASURED_LATENCY_MS, error)
        self._failures.append((current.label, current.url, error))
        self._index += 1
        return self.current

    def failure_reason(self) -> str:
        """Readable explanation when nothing played ('' once something did)."""

        if self.started is not None:
            return ""
        return compose_failure_reason(self._failures)

    def with_preference(self, url: str) -> PlaybackSession:
        """Promote a manual choice to the front of this walk."""

        target = str(url or "").strip()
        if not target:
            return self
        promoted = [
            replace(source, manual=source.url == target, reason=(
                "手动首选" if source.url == target else source.reason
            ))
            for source in self.sources
        ]
        promoted.sort(key=lambda source: 0 if source.url == target else 1)
        self.sources = promoted
        return self

    def _record(
        self, url: str, outcome: PlaybackOutcome, latency_ms: int, error: str
    ) -> None:
        if self.recorder is None:
            return
        self.recorder(url, outcome, latency_ms, error)
