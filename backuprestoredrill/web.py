"""Local dashboard. The server binds to 127.0.0.1 only."""

from __future__ import annotations

import json
import mimetypes
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from backuprestoredrill import __version__
from backuprestoredrill.config import AppConfig, ConfigError
from backuprestoredrill.docker import DockerCLI
from backuprestoredrill.drill import DrillLog, DrillResult, run_drill
from backuprestoredrill.history import History, age_label, is_stale, utcnow
from backuprestoredrill.hostinger import WEBSITE_BACKUP_NOTE, HostingerClient, HostingerError
from backuprestoredrill.secrets import get_token

ASSET_TYPES = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".jpg": "image/jpeg",
}


@dataclass
class RunState:
    id: str
    slug: str
    done: bool = False
    result: DrillResult | None = None
    error: str | None = None
    log: list[str] = field(default_factory=list)


class AppState:
    def __init__(
        self,
        *,
        config: AppConfig,
        history: History,
        root: Path,
        reports_dir: Path,
        work_root: Path,
        assets: Path,
        docker: DockerCLI | None = None,
    ):
        self.config = config
        self.history = history
        self.root = root
        self.reports_dir = reports_dir
        self.work_root = work_root
        self.assets = assets
        self.docker = docker if docker is not None else DockerCLI()
        self._lock = threading.Lock()
        self.runs: dict[str, RunState] = {}
        self.active: RunState | None = None
        self._docker_cache: tuple[float, bool, str] | None = None

    def docker_status(self) -> tuple[bool, str]:
        now = datetime.now(timezone.utc).timestamp()
        if self._docker_cache and now - self._docker_cache[0] < 15:
            return self._docker_cache[1], self._docker_cache[2]
        ok, detail = self.docker.available()
        self._docker_cache = (now, ok, detail)
        return ok, detail

    def start(self, slug: str, *, static_only: bool) -> RunState:
        try:
            site = self.config.site(slug)
        except ConfigError as exc:
            raise KeyError(str(exc)) from exc
        with self._lock:
            if self.active and not self.active.done:
                raise RuntimeError("A drill is already running. Wait for it to finish.")
            logger = DrillLog(echo=False)
            state = RunState(id="", slug=slug, log=logger.lines)
            logger("Starting the drill.")
            self.active = state

        def _go() -> None:
            try:
                result = run_drill(
                    site,
                    root=self.root,
                    history=self.history,
                    reports_dir=self.reports_dir,
                    work_root=self.work_root,
                    docker=self.docker,
                    static_only=static_only or self.config.static_only,
                    echo=False,
                    logger=logger,
                )
                state.id = result.id
                state.result = result
                state.log = result.log
                state.done = True
            except Exception as exc:  # noqa: BLE001
                state.error = str(exc)
                state.log.append(str(exc))
                state.done = True

        threading.Thread(target=_go, daemon=True).start()
        return state

    def snapshot(self, state: RunState) -> dict:
        result = state.result
        return {
            "done": state.done,
            "slug": state.slug,
            "run_id": result.id if result else state.id,
            "passed": result.passed if result else None,
            "error": state.error or (result.error if result else None),
            "log": list(state.log if state.done and result else state.log),
            "report": f"/reports/{result.id}" if result and result.report_path else None,
        }


def make_server(state: AppState, port: int = 0) -> ThreadingHTTPServer:
    handler = _handler_class(state)

    class LocalServer(ThreadingHTTPServer):
        allow_reuse_address = True

    try:
        httpd = LocalServer(("127.0.0.1", port), handler)
    except OSError as exc:
        raise RuntimeError(f"Could not listen on 127.0.0.1:{port}: {exc}") from exc
    if httpd.server_address[0] != "127.0.0.1":
        httpd.server_close()
        raise RuntimeError("Refusing to bind anything except 127.0.0.1.")
    return httpd


def _handler_class(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            self._route("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._route("POST")

        def _route(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if method == "GET" and path == "/":
                self._html(dashboard_html(state))
            elif method == "GET" and path == "/health":
                self._json(200, {"ok": True, "version": __version__})
            elif method == "GET" and path in {"/favicon.ico", "/assets/icon.ico"}:
                self._file(state.assets / "icon.ico")
            elif method == "GET" and path == "/assets/icon.png":
                self._file(state.assets / "icon.png")
            elif method == "GET" and path == "/assets/icon.svg":
                self._file(state.assets / "icon.svg")
            elif method == "GET" and path.startswith("/reports/"):
                self._report(path.removeprefix("/reports/"))
            elif method == "GET" and path.startswith("/api/runs/"):
                self._run(path.removeprefix("/api/runs/"))
            elif method == "POST" and path.startswith("/api/sites/") and path.endswith("/drill"):
                slug = path.removeprefix("/api/sites/").removesuffix("/drill").strip("/")
                self._start_drill(slug)
            elif method == "POST" and path == "/api/hostinger/sites":
                self._hostinger_sites()
            else:
                self._json(404, {"error": "Not found"})

        def _start_drill(self, slug: str) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(min(length, 65536)) if length else b""
            static_only = False
            if raw:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._json(400, {"error": "Expected JSON."})
                    return
                static_only = bool(payload.get("static_only"))
            try:
                state.start(slug, static_only=static_only)
            except KeyError as exc:
                self._json(404, {"error": str(exc)})
                return
            except RuntimeError as exc:
                self._json(409, {"error": str(exc)})
                return
            active = state.active
            self._json(202, {"run_id": active.slug if active else slug, "slug": slug})

        def _run(self, slug: str) -> None:
            active = state.active
            if active is None or (active.slug != slug and (not active.result or active.result.id != slug)):
                self._json(404, {"error": "No drill is running for that id."})
                return
            if active.slug == slug or (active.result and (active.result.id == slug or active.slug == slug)):
                self._json(200, state.snapshot(active))
                return
            self._json(404, {"error": "No drill is running for that id."})

        def _report(self, drill_id: str) -> None:
            if not drill_id or "/" in drill_id or ".." in drill_id:
                self._json(404, {"error": "Unknown report."})
                return
            record = state.history.get(drill_id)
            if record is None or not record.report_path:
                self._json(404, {"error": "Unknown report."})
                return
            path = Path(record.report_path)
            reports = state.reports_dir.resolve()
            try:
                path.resolve().relative_to(reports)
            except ValueError:
                self._json(404, {"error": "Unknown report."})
                return
            self._file(path)

        def _hostinger_sites(self) -> None:
            if not state.config.hostinger_enabled:
                self._json(
                    403,
                    {
                        "error": "Hostinger API is off. Set hostinger.enabled to true in data/config.yaml.",
                        "backups": WEBSITE_BACKUP_NOTE,
                    },
                )
                return
            token = get_token()
            if not token:
                self._json(
                    400,
                    {
                        "error": "No token. Set HOSTINGER_API_TOKEN or run: "
                        "python -m backuprestoredrill token set",
                    },
                )
                return
            try:
                websites = HostingerClient(token).list_websites()
            except HostingerError as exc:
                self._json(502, {"error": str(exc)})
                return
            self._json(200, {"websites": websites, "backups": WEBSITE_BACKUP_NOTE})

        def _html(self, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path) -> None:
            if not path.is_file():
                self._json(404, {"error": "Missing file."})
                return
            data = path.read_bytes()
            kind = ASSET_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
            if path.suffix == ".html":
                kind = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", kind or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def dashboard_html(state: AppState) -> str:
    from html import escape

    now = utcnow()
    ok, docker_detail = state.docker_status()
    cards = []
    any_stale = False
    for site in state.config.sites:
        latest = state.history.latest(site.slug)
        last_pass = state.history.latest_pass(site.slug)
        stale = is_stale(
            state.history,
            site.slug,
            now=now,
            stale_after_days=state.config.stale_after_days,
        )
        any_stale = any_stale or stale
        if latest is None:
            badge = "Not drilled yet"
            badge_class = "stale"
        elif latest.passed:
            badge = "Passed"
            badge_class = "ok"
        else:
            badge = "Failed"
            badge_class = "bad"
        age = age_label(latest.finished_at if latest else None, now)
        pass_age = age_label(last_pass.finished_at if last_pass else None, now)
        report = ""
        if latest and latest.report_path:
            report = f"<a class='ghost' href='/reports/{escape(latest.id)}'>View report</a>"
        warning = ""
        if stale:
            if last_pass is None:
                warning = "<p class='warn'>Has not passed a drill yet.</p>"
            else:
                warning = (
                    f"<p class='warn'>Has not passed a drill in over "
                    f"{state.config.stale_after_days} days. Last pass was {escape(pass_age)}.</p>"
                )
        cards.append(
            f"""
            <article class="card {badge_class}">
              <header>
                <h2>{escape(site.name)}</h2>
                <span class="pill {badge_class}">{escape(badge)}</span>
              </header>
              <p class="muted">{escape(site.type)} · last drill {escape(age)} · last pass {escape(pass_age)}</p>
              {warning}
              <div class="actions">
                <button type="button" data-slug="{escape(site.slug)}">Run drill now</button>
                {report}
              </div>
            </article>
            """
        )
    if not cards:
        cards.append(
            """
            <article class="card">
              <h2>No sites yet</h2>
              <p>Edit <code>data/config.yaml</code>. A synthetic brochure site is in the example config.</p>
            </article>
            """
        )
    stale_banner = ""
    if any_stale:
        stale_banner = (
            f"<p class='banner warn'>At least one site has not passed a drill in over "
            f"{state.config.stale_after_days} days.</p>"
        )
    if ok:
        docker_banner = (
            f"<p class='banner ok'>Sandbox engine: {escape(docker_detail)}. "
            "Web and database ports stay on this machine.</p>"
        )
    else:
        docker_banner = f"<p class='banner warn'>{escape(docker_detail)}</p>"
    hostinger = (
        "Hostinger listing is on. The token stays in the environment or the OS keychain."
        if state.config.hostinger_enabled
        else "Hostinger API is off. Drills use backup files you already downloaded."
    )
    body = "\n".join(cards)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BackupRestoreDrill</title>
  <link rel="icon" href="/assets/icon.png">
  <style>
    :root {{
      --bg: #07111f; --card: #102033; --line: #1e4158;
      --cyan: #2ee6c7; --lime: #b6ff3c; --text: #e8f4ff; --muted: #8aa0b8;
      --bad: #ff6b81; --ok: #9dff6a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100vh; color: var(--text);
      font-family: "Segoe UI", sans-serif;
      background:
        radial-gradient(900px 420px at 80% -10%, rgba(46,230,199,.16), transparent 60%),
        radial-gradient(700px 380px at 0% 0%, rgba(182,255,60,.08), transparent 55%),
        var(--bg);
    }}
    header.top {{
      display: flex; gap: 16px; align-items: center;
      padding: 28px 7vw 8px;
    }}
    header.top img {{ width: 56px; height: 56px; border-radius: 14px; }}
    h1 {{ margin: 0; font-size: 1.8rem; letter-spacing: -0.03em; }}
    h1 span.c {{ color: var(--cyan); }} h1 span.l {{ color: var(--lime); }}
    main {{ padding: 8px 7vw 48px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
    .card {{
      background: rgba(16,32,51,.92); border: 1px solid var(--line);
      border-radius: 18px; padding: 18px 18px 16px;
    }}
    .card.ok {{ box-shadow: inset 3px 0 0 var(--lime); }}
    .card.bad {{ box-shadow: inset 3px 0 0 var(--bad); }}
    .card.stale {{ box-shadow: inset 3px 0 0 #e6c14a; }}
    .card header {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; }}
    h2 {{ margin: 0; font-size: 1.15rem; }}
    .pill {{ border-radius: 999px; padding: 4px 10px; font-size: .8rem; border: 1px solid var(--line); }}
    .pill.ok {{ color: var(--lime); }} .pill.bad {{ color: var(--bad); }} .pill.stale {{ color: #e6c14a; }}
    .muted {{ color: var(--muted); }}
    .warn {{ color: #ffd37a; }}
    .banner {{ border: 1px solid var(--line); border-radius: 14px; padding: 12px 14px; background: #0c1a2e; }}
    .banner.ok {{ border-color: #1d6b62; }}
    button, .ghost {{
      border-radius: 999px; padding: 8px 14px; font: inherit; cursor: pointer;
      text-decoration: none; display: inline-block;
    }}
    button {{ background: var(--lime); color: #10200a; border: 0; font-weight: 700; }}
    .ghost {{ color: var(--cyan); border: 1px solid var(--line); }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
    #log {{
      white-space: pre-wrap; background: #07101c; border-radius: 14px; padding: 14px;
      border: 1px solid var(--line); min-height: 120px; max-height: 360px; overflow: auto;
    }}
    code {{ color: var(--cyan); }}
    @media (max-width: 640px) {{
      header.top, main {{ padding-left: 16px; padding-right: 16px; }}
      h1 {{ font-size: 1.4rem; }}
    }}
  </style>
</head>
<body>
  <header class="top">
    <img src="/assets/icon.png" alt="BackupRestoreDrill icon">
    <div>
      <h1>Backup<span class="c">Restore</span><span class="l">Drill</span></h1>
      <p class="muted">Monthly proof that a backup actually restores. Sandbox only.</p>
    </div>
  </header>
  <main>
    {docker_banner}
    {stale_banner}
    <p class="muted">{escape(hostinger)}
      <button type="button" id="list-sites">List Hostinger sites</button>
    </p>
    <div id="hostinger" class="muted"></div>
    <div class="grid">
      {body}
    </div>
    <section>
      <h2>Live log</h2>
      <label><input type="checkbox" id="static-only"> Static-only mode (Python http server, no Docker)</label>
      <pre id="log">Run a drill to see the log here.</pre>
      <p id="report-link"></p>
    </section>
  </main>
  <script>
    const logEl = document.getElementById("log");
    const reportEl = document.getElementById("report-link");
    async function poll(slug) {{
      const response = await fetch("/api/runs/" + slug);
      const data = await response.json();
      logEl.textContent = (data.log || []).join("\\n");
      if (!data.done) {{
        setTimeout(() => poll(slug), 400);
        return;
      }}
      document.querySelectorAll("button[data-slug]").forEach((button) => {{
        button.disabled = false;
        if (button.dataset.slug !== slug) return;
        const card = button.closest("article");
        const pill = card.querySelector(".pill");
        if (pill) {{
          pill.textContent = data.passed ? "Passed" : "Failed";
          pill.className = "pill " + (data.passed ? "ok" : "bad");
        }}
        if (data.report && String(data.report).startsWith("/reports/")) {{
          let link = card.querySelector("a.ghost");
          if (!link) {{
            link = document.createElement("a");
            link.className = "ghost";
            link.textContent = "View report";
            button.parentElement.appendChild(link);
          }}
          link.href = data.report;
          reportEl.innerHTML = "";
          const open = document.createElement("a");
          open.href = data.report;
          open.textContent = "Open the HTML report";
          reportEl.appendChild(open);
        }}
      }});
    }}
    document.querySelectorAll("button[data-slug]").forEach((button) => {{
      button.addEventListener("click", async () => {{
        button.disabled = true;
        reportEl.textContent = "";
        logEl.textContent = "Starting…";
        const response = await fetch("/api/sites/" + button.dataset.slug + "/drill", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{static_only: document.getElementById("static-only").checked}})
        }});
        const data = await response.json();
        if (!response.ok) {{
          logEl.textContent = data.error || "Could not start the drill.";
          button.disabled = false;
          return;
        }}
        poll(data.slug);
      }});
    }});
    document.getElementById("list-sites").addEventListener("click", async () => {{
      const box = document.getElementById("hostinger");
      box.textContent = "Asking Hostinger…";
      const response = await fetch("/api/hostinger/sites", {{method: "POST"}});
      const data = await response.json();
      if (!response.ok) {{
        box.textContent = data.error || "Hostinger listing failed.";
        if (data.backups) box.textContent += " " + data.backups;
        return;
      }}
      const lines = (data.websites || []).map((site) =>
        (site.domain || "(no domain)") + " · " + (site.website_type || "unknown")
      );
      box.textContent = (lines.join("\\n") || "No websites returned.") + "\\n\\n" + data.backups;
    }});
  </script>
</body>
</html>
"""
