"""Self-contained HTML report for one drill."""

from __future__ import annotations

from html import escape
from pathlib import Path

from backuprestoredrill.crawler import PageResult
from backuprestoredrill.history import DrillRecord


def write_report(
    path: Path,
    *,
    site_name: str,
    record: DrillRecord,
    pages: list[PageResult],
    db_ok: bool | None,
    log_lines: list[str],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_report(
            site_name=site_name,
            record=record,
            pages=pages,
            db_ok=db_ok,
            log_lines=log_lines,
        ),
        encoding="utf-8",
    )
    return path


def render_report(
    *,
    site_name: str,
    record: DrillRecord,
    pages: list[PageResult],
    db_ok: bool | None,
    log_lines: list[str],
) -> str:
    status = "PASS" if record.passed else "FAIL"
    rows = "\n".join(_page_row(page) for page in pages) or (
        "<tr><td colspan='6'>No pages were checked.</td></tr>"
    )
    if db_ok is None:
        db_line = "Not a WordPress drill."
    elif db_ok:
        db_line = "PASS — sandbox MariaDB answered SELECT 1, and pages did not report a connection error."
    else:
        db_line = "FAIL — the sandbox database did not answer."
    log_html = escape("\n".join(log_lines)) or "(no log)"
    error = f"<p class='warn'>{escape(record.error)}</p>" if record.error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(site_name)} drill {escape(status)}</title>
  <style>
    :root {{
      --bg: #07111f; --card: #102033; --line: #1c3d4f;
      --cyan: #2ee6c7; --lime: #b6ff3c; --text: #e8f4ff; --muted: #8aa0b8;
      --bad: #ff6b81; --ok: #7dff6b;
    }}
    body {{ margin: 0; font-family: "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }}
    main {{ max-width: 980px; margin: 0 auto; padding: 32px 20px 64px; }}
    h1 {{ font-size: 1.8rem; margin-bottom: 0.2rem; }}
    .pass {{ color: var(--lime); }} .fail {{ color: var(--bad); }}
    .muted {{ color: var(--muted); }}
    table {{ width: 100%; border-collapse: collapse; background: var(--card); }}
    th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }}
    th {{ color: var(--cyan); font-weight: 600; }}
    .badge {{ font-weight: 700; }}
    pre {{ background: #0c1a2e; padding: 16px; overflow: auto; border: 1px solid var(--line); }}
    a {{ color: var(--cyan); }}
    .warn {{ color: var(--bad); }}
  </style>
</head>
<body>
<main>
  <p class="muted"><a href="/">BackupRestoreDrill</a></p>
  <h1>{escape(site_name)} <span class="{status.lower()}">{status}</span></h1>
  <p class="muted">{escape(record.finished_at.strftime("%Y-%m-%d %H:%M UTC"))}
     · mode {escape(record.mode or "unknown")} · {escape(record.id)}</p>
  {error}
  <p>{escape(record.summary)}</p>
  <h2>Pages</h2>
  <table>
    <thead>
      <tr><th>URL</th><th>Status</th><th>Title</th><th>Text</th><th>PHP / DB</th><th>Result</th></tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>
  <h2>Database</h2>
  <p>{escape(db_line)}</p>
  <h2>Log</h2>
  <pre>{log_html}</pre>
  <p class="muted">Sandbox-only workflow: it makes no FTP, SSH, or DNS changes. Restored code may still have Docker bridge egress; use synthetic fixtures only.</p>
</main>
</body>
</html>
"""


def _page_row(page: PageResult) -> str:
    result = "PASS" if page.ok else "FAIL"
    css = "pass" if page.ok else "fail"
    title = page.title or "—"
    if page.title_ok is False:
        title += " (expected text missing)"
    text = "ok" if not page.missing_text else "missing: " + ", ".join(page.missing_text)
    problems: list[str] = []
    problems.extend(page.php_errors)
    if page.db_error:
        problems.append("database connection error")
    if page.broken_links:
        problems.append(
            "broken: "
            + ", ".join(
                f"{link.href} ({link.status or link.error})" for link in page.broken_links[:6]
            )
        )
    if page.error:
        problems.append(page.error)
    php = "; ".join(problems) if problems else "none"
    status = "—" if page.status is None else str(page.status)
    return (
        "<tr>"
        f"<td>{escape(page.url)}</td>"
        f"<td>{escape(status)}</td>"
        f"<td>{escape(title)}</td>"
        f"<td>{escape(text)}</td>"
        f"<td>{escape(php)}</td>"
        f"<td class='badge {css}'>{result}</td>"
        "</tr>"
    )
