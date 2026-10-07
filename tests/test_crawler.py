from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from backuprestoredrill.config import validate_config
from backuprestoredrill.crawler import CrawlerError, crawl, fetch_url

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mini_site"


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return


def _serve(directory: Path) -> tuple[ThreadingHTTPServer, str]:
    handler = partial(_Quiet, directory=str(directory))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _fixture_config():
    return validate_config(
        {
            "sites": [
                {
                    "name": "Fixture",
                    "slug": "fixture",
                    "type": "static",
                    "backup": str(FIXTURE),
                    "production_url": "https://brochure.example.test",
                    "use_sitemap": True,
                    "urls": [
                        {
                            "path": "/",
                            "title_contains": "Fixture Home",
                            "text_contains": ["Welcome to the fixture"],
                        },
                        {"path": "/about.html", "title_contains": "About Fixture"},
                        {"path": "/php-error.html", "title_contains": "Broken"},
                    ],
                }
            ]
        }
    ).sites[0]


def test_crawler_checks_fixture_and_stays_on_loopback() -> None:
    httpd, base = _serve(FIXTURE)
    seen: list[str] = []

    def opener(request, timeout=8):
        seen.append(request.full_url)
        from urllib.request import urlopen

        return urlopen(request, timeout=timeout)

    try:
        pages = crawl(base, _fixture_config(), opener=opener, timeout=5)
    finally:
        httpd.shutdown()
        httpd.server_close()

    by_path = {page.url.removeprefix(base): page for page in pages}
    assert by_path["/"].title == "Fixture Home"
    assert by_path["/"].title_ok is True
    assert by_path["/"].ok is False
    assert any(link.status == 404 and link.href.endswith("/missing.html") for link in by_path["/"].broken_links)
    assert by_path["/about.html"].ok is True
    assert by_path["/php-error.html"].ok is False
    assert by_path["/php-error.html"].php_errors
    assert "/from-map.html" in by_path
    assert by_path["/from-map.html"].ok is True
    assert seen
    assert all(url.startswith("http://127.0.0.1") for url in seen)
    assert not any("outside.example" in url for url in seen)
    assert not any("mailto:" in url for url in seen)


def test_refuses_non_sandbox_host() -> None:
    with pytest.raises(CrawlerError, match="non-sandbox"):
        fetch_url("https://brochure.example.test/", opener=lambda *args, **kwargs: None, timeout=1)


def test_refuses_redirect_off_loopback() -> None:
    class Redirect(_Quiet):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "https://brochure.example.test/stolen")
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    site = validate_config(
        {"sites": [{"name": "R", "slug": "redir", "type": "static", "backup": ".", "urls": [{"path": "/"}]}]}
    ).sites[0]
    try:
        pages = crawl(base, site, timeout=5)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert pages[0].ok is False
    assert pages[0].error and "Refusing redirect" in pages[0].error
