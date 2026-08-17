from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class CrawlerConfig:
    user_agent: str = "ASMRLIBPersonalArchiver/0.1 (+local personal archive)"
    timeout_seconds: int = 30
    delay_seconds: float = 3
    retries: int = 2
    obey_robots: bool = True
    robots_fail_closed: bool = False
    redirect_policy: str = "same_domain_only"
    max_redirects: int = 2
    max_response_bytes: int = 5 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("crawler.timeout_seconds must be positive")
        if self.delay_seconds < 0:
            raise ValueError("crawler.delay_seconds must not be negative")
        if self.retries < 0 or self.max_redirects < 0:
            raise ValueError("crawler retries and redirects must not be negative")
        if self.max_response_bytes <= 0:
            raise ValueError("crawler.max_response_bytes must be positive")


@dataclass(frozen=True)
class DiscoveryConfig:
    enabled: bool = True
    max_pages_per_tag: int = 100
    max_posts_per_tag: int = 2000
    page_batch_size: int = 20
    max_page_bytes: int = 5 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in (
            "max_pages_per_tag",
            "max_posts_per_tag",
            "page_batch_size",
            "max_page_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"discovery.{name} must be positive")


@dataclass(frozen=True)
class BrowserConfig:
    enabled: bool = False
    headless: bool = True
    javascript: bool = False
    wait_until: str = "domcontentloaded"
    timeout_seconds: int = 30
    block_resource_types: list[str] = field(
        default_factory=lambda: ["image", "font", "media", "websocket", "eventsource"]
    )
    block_url_keywords: list[str] = field(
        default_factory=lambda: [
            "doubleclick",
            "googlesyndication",
            "google-analytics",
            "googletagmanager",
            "adservice",
            "adsystem",
            "adserver",
            "adsterra",
            "exoclick",
            "popads",
            "propeller",
            "tracking",
            "analytics",
            "telemetry",
            "affiliate",
        ]
    )


@dataclass(frozen=True)
class DownloadConfig:
    enabled: bool = True
    allow_external_media: bool = False
    allowed_media_domains: list[str] = field(default_factory=list)
    redirect_policy: str = "same_domain_only"
    max_redirects: int = 2
    timeout_seconds: int = 60
    chunk_size: int = 1024 * 1024
    max_file_mb: int = 2048
    overwrite: bool = False
    allowed_content_types: list[str] = field(
        default_factory=lambda: [
            "video/",
            "audio/",
            "application/octet-stream",
        ]
    )
    allowed_extensions: list[str] = field(
        default_factory=lambda: [
            ".mp4",
            ".m4v",
            ".mkv",
            ".webm",
            ".mov",
            ".mp3",
            ".m4a",
            ".aac",
            ".wav",
            ".flac",
            ".ogg",
        ]
    )


@dataclass(frozen=True)
class AppConfig:
    config_path: Path
    allowed_domains: list[str] = field(default_factory=lambda: ["asmrlib.com"])
    allow_subdomains: bool = False
    seeds: list[str] = field(default_factory=list)
    tag_seeds: list[str] = field(default_factory=list)
    output_dir: Path = Path("./data")
    database_path: Path = Path("./data/archive.sqlite3")
    crawler: CrawlerConfig = field(default_factory=CrawlerConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)


def _merge(defaults: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    result = dict(defaults)
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _as_path(value: str | Path, base_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _dataclass_defaults(cls: type) -> dict[str, Any]:
    instance = cls()
    return dict(instance.__dict__)


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(
        path
        or os.getenv("ASMRLIB_ARCHIVER_CONFIG")
        or "config.yaml"
    ).resolve()
    base_dir = config_path.parent
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"Config file must contain a mapping: {config_path}")
            raw = loaded

    crawler_data = _merge(_dataclass_defaults(CrawlerConfig), raw.get("crawler", {}) or {})
    discovery_data = _merge(
        _dataclass_defaults(DiscoveryConfig), raw.get("discovery", {}) or {}
    )
    browser_data = _merge(_dataclass_defaults(BrowserConfig), raw.get("browser", {}) or {})
    download_data = _merge(_dataclass_defaults(DownloadConfig), raw.get("download", {}) or {})
    output_dir = _as_path(raw.get("output_dir", "./data"), base_dir)
    database_path = _as_path(raw.get("database_path", output_dir / "archive.sqlite3"), base_dir)

    return AppConfig(
        config_path=config_path,
        allowed_domains=list(raw.get("allowed_domains", ["asmrlib.com"])),
        allow_subdomains=bool(raw.get("allow_subdomains", False)),
        seeds=list(raw.get("seeds", []) or []),
        tag_seeds=list(raw.get("tag_seeds", []) or []),
        output_dir=output_dir,
        database_path=database_path,
        crawler=CrawlerConfig(**crawler_data),
        discovery=DiscoveryConfig(**discovery_data),
        browser=BrowserConfig(**browser_data),
        download=DownloadConfig(**download_data),
    )
