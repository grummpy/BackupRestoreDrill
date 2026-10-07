from __future__ import annotations

import json
import threading
import time
from datetime import timedelta
from pathlib import Path
from urllib.request import Request, urlopen

from backuprestoredrill.config import load_config
from backuprestoredrill.history import DrillRecord, History, utcnow
from backuprestoredrill.web import AppState, make_server

REPO = Path(__file__).resolve().parents[1]


def _server(tmp_path: Path, history: History | None = None):
    config_path = tmp_path / "data"
    config_path.mkdir()
    (tmp_path / "config.example.yaml").write_text(
        (REPO / "config.example.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (config_path / "config.yaml").write_text(
        """
stale_after_days: 35
port: 0
open_browser: false
static_only: true
hostinger:
  enabled: false
sites:
  - name: Northwind Brochure
    slug: brochure
    type: static
    backup: BROCHURE
    production_url: https://brochure.example.test
    use_sitemap: true
    urls:
      - path: /
        title_contains: Northwind Brochure
        text_contains:
          - Welcome to the Northwind brochure
      - path: /about.html
        title_contains: About Northwind
""".replace("BROCHURE", str(REPO / "examples" / "brochure")),
        encoding="utf-8",
    )
    config = load_config(config_path / "config.yaml")
    store = history or History(tmp_path / "data" / "drills.sqlite")
    state = AppState(
        config=config,
        history=store,
        root=tmp_path,
        reports_dir=tmp_path / "data" / "reports",
        work_root=tmp_path / "data" / "work",
        assets=REPO / "assets",
    )
    (tmp_path / "data" / "reports").mkdir()
    (tmp_path / "data" / "work").mkdir()
    httpd = make_server(state, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", state


def _get(url: str) -> tuple[int, bytes]:
    with urlopen(url, timeout=5) as response:
        return response.status, response.read()


def test_smoke_dashboard_and_icon(tmp_path: Path) -> None:
    httpd, base, _state = _server(tmp_path)
    try:
        status, html_bytes = _get(base + "/")
        html = html_bytes.decode("utf-8")
        assert status == 200
        assert "Backup" in html and "Restore" in html and "Drill" in html
        assert "Run drill now" in html
        assert httpd.server_address[0] == "127.0.0.1"
        icon_status, icon = _get(base + "/assets/icon.png")
        assert icon_status == 200
        assert icon.startswith(b"\x89PNG")
        health, health_body = _get(base + "/health")
        assert health == 200
        assert b"ok" in health_body
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_dashboard_warns_when_last_pass_is_stale(tmp_path: Path) -> None:
    history = History(tmp_path / "prep.sqlite")
    finished = utcnow() - timedelta(days=40)
    history.record(
        DrillRecord(
            id="old-pass",
            site_slug="brochure",
            started_at=finished,
            finished_at=finished,
            passed=True,
            mode="static-python",
            error=None,
            report_path=None,
            summary="old",
        )
    )
    httpd, base, _state = _server(tmp_path, history)
    try:
        _status, html = _get(base + "/")
        assert b"over 35 days" in html
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_static_drill_from_the_api(tmp_path: Path) -> None:
    httpd, base, _state = _server(tmp_path)
    try:
        request = Request(
            base + "/api/sites/brochure/drill",
            data=json.dumps({"static_only": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            assert response.status == 202
            payload = json.loads(response.read().decode("utf-8"))
        slug = payload["slug"]
        body = {}
        for _ in range(80):
            with urlopen(base + "/api/runs/" + slug, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
            if body.get("done"):
                break
            time.sleep(0.1)
        assert body.get("done") is True
        assert body.get("passed") is True
        assert body.get("report")
        status, report = _get(base + body["report"])
        assert status == 200
        assert b"PASS" in report
        log = "\n".join(body.get("log") or [])
        assert "Static-only" in log or "static-only" in log
    finally:
        httpd.shutdown()
        httpd.server_close()
