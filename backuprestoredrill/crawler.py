"""Fetch sandbox URLs and check status, title, text, PHP errors, and links."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from backuprestoredrill.config import LOOPBACK_HOSTS, SiteConfig, UrlCheck

USER_AGENT = "BackupRestoreDrill/1.0"
MAX_BODY = 2_000_000
PHP_ERROR_RE = re.compile(
    r"(Fatal error|Parse error|Warning|Notice|Deprecated|Recoverable fatal error):"
    r"[^\n]{0,240}\bon line \d+",
    re.IGNORECASE,
)
UNCAUGHT_RE = re.compile(r"Uncaught (?:Error|Exception|[A-Za-z_\\]+):", re.IGNORECASE)
DB_ERROR_RE = re.compile(r"Error establishing a database connection", re.IGNORECASE)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
HREF_RE = re.compile(r"""<a\b[^>]*\bhref\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


class CrawlerError(Exception):
    """The crawler refused a request or could not finish a check."""


@dataclass
class BrokenLink:
    href: str
    status: int | None
    error: str | None


@dataclass
class PageResult:
    url: str
    status: int | None
    ok: bool
    title: str = ""
    title_ok: bool | None = None
    missing_text: list[str] = field(default_factory=list)
    php_errors: list[str] = field(default_factory=list)
    broken_links: list[BrokenLink] = field(default_factory=list)
    db_error: bool = False
    error: str | None = None

    @property
    def summary(self) -> str:
        if self.ok:
            return "pass"
        if self.error:
            return self.error
        reasons: list[str] = []
        if self.status is None:
            reasons.append("no response")
        elif self.title_ok is False or self.missing_text or self.php_errors or self.broken_links:
            if self.status is not None:
                reasons.append(f"HTTP {self.status}")
        if self.title_ok is False:
            reasons.append("title")
        if self.missing_text:
            reasons.append("missing text")
        if self.php_errors:
            reasons.append("PHP error")
        if self.db_error:
            reasons.append("database")
        if self.broken_links:
            reasons.append(f"{len(self.broken_links)} broken link(s)")
        return ", ".join(reasons) or "fail"


class _LoopbackRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urlparse(newurl).hostname
        if host not in LOOPBACK_HOSTS:
            raise CrawlerError(f"Refusing redirect to {host}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_opener():
    return build_opener(_LoopbackRedirect()).open


def crawl(
    base_url: str,
    site: SiteConfig,
    *,
    opener=None,
    timeout: float = 8,
) -> list[PageResult]:
    """Check the configured URLs and, when asked, the sitemap. Sandbox hosts only."""
    fetch = opener or default_opener()
    base = base_url.rstrip("/")
    _guard_loopback(base)
    checks = list(site.urls)
    seen = {item.path for item in checks}
    if site.use_sitemap:
        for path in _sitemap_paths(base, site, fetch, timeout):
            if path not in seen and len(checks) < site.max_pages:
                checks.append(UrlCheck(path=path))
                seen.add(path)
    if not checks:
        return [
            PageResult(
                url=base + site.sitemap_path,
                status=None,
                ok=False,
                error="No URLs to check. Add urls or a sitemap.",
            )
        ]
    results: list[PageResult] = []
    fetched: dict[str, tuple[int | None, str | None]] = {}
    for check in checks[: site.max_pages]:
        url = base + check.path
        results.append(
            _check_page(
                url,
                check,
                site,
                base,
                fetch,
                timeout,
                fetched,
            )
        )
    return results


def fetch_url(url: str, opener, timeout: float) -> tuple[int, str, str]:
    """GET a loopback URL. Returns status, final URL, and body text."""
    _guard_loopback(url)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        opened = opener(request, timeout=timeout)
    except HTTPError as exc:
        raw = exc.read(MAX_BODY)
        text = raw.decode("utf-8", errors="replace")
        return int(exc.code), url, text
    with opened as response:
        final_url = response.geturl()
        _guard_loopback(final_url)
        status = getattr(response, "status", None) or response.getcode()
        raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raw = raw[:MAX_BODY]
        charset = "utf-8"
        content_type = response.headers.get("Content-Type", "") if response.headers else ""
        match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
        if match:
            charset = match.group(1)
        text = raw.decode(charset, errors="replace")
        return int(status), final_url, text


def _guard_loopback(url: str) -> None:
    host = urlparse(url).hostname
    if host not in LOOPBACK_HOSTS:
        raise CrawlerError(f"Refusing to request non-sandbox host: {host}")


def _check_page(url, check, site, base, opener, timeout, fetched) -> PageResult:
    try:
        status, _final, body = fetch_url(url, opener, timeout)
    except CrawlerError as exc:
        return PageResult(url=url, status=None, ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - network failures become a failed check
        return PageResult(url=url, status=None, ok=False, error=str(exc))
    fetched[url] = (status, None)
    title = _title(body)
    title_ok = None
    if check.title_contains:
        title_ok = check.title_contains.casefold() in title.casefold()
    missing = [
        snippet
        for snippet in check.text_contains
        if snippet.casefold() not in body.casefold()
    ]
    php_errors = _php_errors(body)
    db_error = bool(DB_ERROR_RE.search(body))
    broken = _broken_links(body, url, site, base, opener, timeout, fetched)
    ok = (
        status == check.expect_status
        and title_ok is not False
        and not missing
        and not php_errors
        and not db_error
        and not broken
    )
    return PageResult(
        url=url,
        status=status,
        ok=ok,
        title=title,
        title_ok=title_ok,
        missing_text=missing,
        php_errors=php_errors,
        broken_links=broken,
        db_error=db_error,
    )


def _title(body: str) -> str:
    match = TITLE_RE.search(body)
    if not match:
        return ""
    text = TAG_RE.sub(" ", match.group(1))
    return " ".join(unescape(text).split())


def _php_errors(body: str) -> list[str]:
    found = [match.group(0).strip() for match in PHP_ERROR_RE.finditer(body)]
    found.extend(match.group(0).strip() for match in UNCAUGHT_RE.finditer(body))
    unique: list[str] = []
    for item in found:
        if item not in unique:
            unique.append(item[:240])
    return unique[:8]


def _broken_links(body, page_url, site, base, opener, timeout, fetched) -> list[BrokenLink]:
    broken: list[BrokenLink] = []
    seen: set[str] = set()
    for match in HREF_RE.finditer(body):
        if len(broken) >= 25 or len(seen) >= 40:
            break
        mapped = map_to_sandbox(match.group(1), page_url, base, site.production_url)
        if mapped is None or mapped in seen:
            continue
        seen.add(mapped)
        if mapped in fetched and fetched[mapped][0] is not None and fetched[mapped][0] < 400:
            continue
        try:
            status, _final, _text = fetch_url(mapped, opener, timeout)
        except CrawlerError as exc:
            broken.append(BrokenLink(href=mapped, status=None, error=str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001
            broken.append(BrokenLink(href=mapped, status=None, error=str(exc)))
            continue
        fetched[mapped] = (status, None)
        if status >= 400:
            broken.append(BrokenLink(href=mapped, status=status, error=None))
    return broken


def map_to_sandbox(href: str, page_url: str, base: str, production_url: str | None) -> str | None:
    """Rewrite an internal link onto the sandbox origin.

    Returns None for mail, hash, and off-site links so they are never requested.
    """
    raw = href.strip()
    if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    absolute = urljoin(page_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return None
    sandbox = urlparse(base)
    production_host = urlparse(production_url).hostname if production_url else None
    host = parsed.hostname
    if host in LOOPBACK_HOSTS or (production_host and host == production_host):
        path = parsed.path or "/"
        if not path.startswith("/"):
            path = "/" + path
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{sandbox.scheme}://{sandbox.netloc}{path}{query}"
    return None


def _sitemap_paths(base: str, site: SiteConfig, opener, timeout: float) -> list[str]:
    url = base + site.sitemap_path
    try:
        _status, _final, body = fetch_url(url, opener, timeout)
    except (CrawlerError, OSError):
        return []
    paths: list[str] = []
    for match in LOC_RE.finditer(body):
        mapped = map_to_sandbox(unescape(match.group(1).strip()), url, base, site.production_url)
        if mapped is None:
            continue
        path = urlparse(mapped).path or "/"
        query = urlparse(mapped).query
        if query:
            path = f"{path}?{query}"
        if path not in paths:
            paths.append(path)
    return paths
