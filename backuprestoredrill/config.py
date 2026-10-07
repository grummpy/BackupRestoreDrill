"""Load and validate per-site YAML configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
SITE_TYPES = {"static", "wordpress"}


class ConfigError(ValueError):
    """The YAML config is missing something the drill needs."""


@dataclass
class UrlCheck:
    path: str
    expect_status: int = 200
    title_contains: str | None = None
    text_contains: list[str] = field(default_factory=list)


@dataclass
class SiteConfig:
    name: str
    slug: str
    type: str
    backup: str
    sql_dump: str | None = None
    production_url: str | None = None
    web_root: str | None = None
    urls: list[UrlCheck] = field(default_factory=list)
    use_sitemap: bool = False
    sitemap_path: str = "/sitemap.xml"
    max_pages: int = 40
    enabled: bool = True

    @property
    def production_host(self) -> str | None:
        if not self.production_url:
            return None
        from urllib.parse import urlparse

        return urlparse(self.production_url).hostname


@dataclass
class AppConfig:
    stale_after_days: int = 35
    port: int = 0
    open_browser: bool = True
    static_only: bool = False
    hostinger_enabled: bool = False
    sites: list[SiteConfig] = field(default_factory=list)

    def site(self, slug: str) -> SiteConfig:
        for item in self.sites:
            if item.slug == slug:
                return item
        raise ConfigError(f"No site named {slug!r} in the config.")


def load_config(path: Path) -> AppConfig:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not read {path.name}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a YAML mapping.")
    return validate_config(raw)


def validate_config(raw: dict) -> AppConfig:
    stale = _int(raw.get("stale_after_days", 35), "stale_after_days")
    if stale < 1 or stale > 3650:
        raise ConfigError("stale_after_days must be between 1 and 3650.")
    port = _int(raw.get("port", 0), "port")
    if port < 0 or port > 65535:
        raise ConfigError("port must be between 0 and 65535.")
    bind_host = raw.get("bind_host", "127.0.0.1")
    if not isinstance(bind_host, str) or bind_host not in LOOPBACK_HOSTS:
        raise ConfigError(
            "bind_host must be 127.0.0.1, localhost, or ::1. "
            "This app only serves the UI on the local machine."
        )
    hostinger = raw.get("hostinger") or {}
    if not isinstance(hostinger, dict):
        raise ConfigError("hostinger must be a mapping.")
    sites_raw = raw.get("sites") or []
    if not isinstance(sites_raw, list):
        raise ConfigError("sites must be a list.")
    sites = [_site(item, index) for index, item in enumerate(sites_raw, start=1)]
    slugs = [site.slug for site in sites]
    if len(slugs) != len(set(slugs)):
        raise ConfigError("Each site slug must be unique.")
    return AppConfig(
        stale_after_days=stale,
        port=port,
        open_browser=_bool(raw.get("open_browser", True), "open_browser"),
        static_only=_bool(raw.get("static_only", False), "static_only"),
        hostinger_enabled=_bool(hostinger.get("enabled", False), "hostinger.enabled"),
        sites=sites,
    )


def _site(item: object, index: int) -> SiteConfig:
    if not isinstance(item, dict):
        raise ConfigError(f"Site {index} must be a mapping.")
    name = _str(item.get("name"), f"sites[{index}].name")
    slug = _str(item.get("slug"), f"sites[{index}].slug")
    if not SLUG_RE.match(slug):
        raise ConfigError(
            f"Site slug {slug!r} must be lowercase letters, numbers, and hyphens."
        )
    site_type = _str(item.get("type"), f"sites[{index}].type")
    if site_type not in SITE_TYPES:
        raise ConfigError(f"Site {slug} type must be 'static' or 'wordpress'.")
    backup = _str(item.get("backup"), f"sites[{index}].backup")
    sql_dump = item.get("sql_dump")
    if sql_dump is not None:
        sql_dump = _str(sql_dump, f"sites[{index}].sql_dump")
    production_url = item.get("production_url")
    if production_url is not None:
        production_url = _str(production_url, f"sites[{index}].production_url")
        if "://" not in production_url:
            raise ConfigError(f"Site {slug} production_url must include a scheme.")
    if site_type == "wordpress" and not production_url:
        raise ConfigError(
            f"Site {slug} is WordPress and needs production_url so the drill can "
            "rewrite siteurl to the sandbox without contacting that host."
        )
    web_root = item.get("web_root")
    if web_root is not None:
        web_root = _str(web_root, f"sites[{index}].web_root")
        if web_root.startswith("/") or ".." in Path(web_root).parts:
            raise ConfigError(f"Site {slug} web_root must stay inside the backup.")
    urls_raw = item.get("urls") or []
    if not isinstance(urls_raw, list):
        raise ConfigError(f"Site {slug} urls must be a list.")
    urls = [_url(entry, slug, n) for n, entry in enumerate(urls_raw, start=1)]
    use_sitemap = _bool(item.get("use_sitemap", False), f"sites[{index}].use_sitemap")
    if not urls and not use_sitemap:
        raise ConfigError(f"Site {slug} needs urls or use_sitemap: true.")
    sitemap_path = item.get("sitemap_path", "/sitemap.xml")
    sitemap_path = _str(sitemap_path, f"sites[{index}].sitemap_path")
    if not sitemap_path.startswith("/"):
        raise ConfigError(f"Site {slug} sitemap_path must start with /.")
    max_pages = _int(item.get("max_pages", 40), f"sites[{index}].max_pages")
    if max_pages < 1 or max_pages > 200:
        raise ConfigError(f"Site {slug} max_pages must be between 1 and 200.")
    return SiteConfig(
        name=name,
        slug=slug,
        type=site_type,
        backup=backup,
        sql_dump=sql_dump,
        production_url=production_url.rstrip("/") if production_url else None,
        web_root=web_root,
        urls=urls,
        use_sitemap=use_sitemap,
        sitemap_path=sitemap_path,
        max_pages=max_pages,
        enabled=_bool(item.get("enabled", True), f"sites[{index}].enabled"),
    )


def _url(entry: object, slug: str, index: int) -> UrlCheck:
    if not isinstance(entry, dict):
        raise ConfigError(f"Site {slug} url {index} must be a mapping.")
    path = _str(entry.get("path"), f"{slug} url {index} path")
    if not path.startswith("/") or "\\" in path or ".." in Path(path).parts:
        raise ConfigError(f"Site {slug} url path must start with / and stay on the site.")
    expect = _int(entry.get("expect_status", 200), f"{slug} url {index} expect_status")
    if expect < 100 or expect > 599:
        raise ConfigError(f"Site {slug} expect_status is not an HTTP status.")
    title = entry.get("title_contains")
    if title is not None:
        title = _str(title, f"{slug} url {index} title_contains")
    texts = entry.get("text_contains") or []
    if not isinstance(texts, list) or not all(isinstance(piece, str) for piece in texts):
        raise ConfigError(f"Site {slug} text_contains must be a list of strings.")
    return UrlCheck(
        path=path,
        expect_status=expect,
        title_contains=title,
        text_contains=list(texts),
    )


def _str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string.")
    return value.strip()


def _int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{label} must be an integer.")
    return value


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be true or false.")
    return value
