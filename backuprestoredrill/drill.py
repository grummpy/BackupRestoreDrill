"""Run one restore drill and always tear the sandbox down."""

from __future__ import annotations

import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from backuprestoredrill.archive import ArchiveError, extract_backup
from backuprestoredrill.config import AppConfig, SiteConfig
from backuprestoredrill.crawler import PageResult, crawl
from backuprestoredrill.docker import DockerCLI
from backuprestoredrill.history import DrillRecord, History, utcnow
from backuprestoredrill.report import write_report
from backuprestoredrill.sandbox import Sandbox, SandboxError, resolve_docroot
from backuprestoredrill.wordpress import find_wp_config


@dataclass
class DrillResult:
    id: str
    site_slug: str
    site_name: str
    passed: bool
    mode: str
    started_at: datetime
    finished_at: datetime
    pages: list[PageResult] = field(default_factory=list)
    report_path: Path | None = None
    log: list[str] = field(default_factory=list)
    error: str | None = None
    db_ok: bool | None = None
    torn_down: bool = False


class DrillLog:
    def __init__(self, echo: bool = False):
        self.lines: list[str] = []
        self.echo = echo

    def __call__(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"{stamp}  {message}"
        self.lines.append(line)
        if self.echo:
            print(line, flush=True)


def run_drill(
    site: SiteConfig,
    *,
    root: Path,
    history: History,
    reports_dir: Path,
    work_root: Path,
    docker: DockerCLI | None = None,
    static_only: bool = False,
    crawl_fn=crawl,
    on_ready=None,
    echo: bool = False,
    ready_probe=None,
    logger: DrillLog | None = None,
) -> DrillResult:
    """Restore, check, write a report, and tear the sandbox down even on failure."""
    started = utcnow()
    drill_id = _new_id(site.slug)
    log = logger or DrillLog(echo=echo)
    log.echo = echo or log.echo
    log(f"Drill {drill_id} for {site.name}")
    scratch = work_root / drill_id
    scratch.mkdir(parents=True, exist_ok=True)
    sandbox = Sandbox(
        docker=docker if docker is not None else DockerCLI(),
        static_only=static_only,
        log=log,
        scratch=scratch,
        ready_probe=ready_probe,
    )
    pages: list[PageResult] = []
    error: str | None = None
    db_ok: bool | None = None
    passed = False
    try:
        backup = _resolve(root, site.backup)
        log(f"Restoring {backup}")
        try:
            extracted = extract_backup(backup, scratch / "tree")
        except ArchiveError as exc:
            raise SandboxError(str(exc)) from exc
        docroot = resolve_docroot(extracted, site.web_root, site.type)
        log("Restored files are ready to serve.")
        if site.type == "wordpress":
            wp_config = find_wp_config(docroot)
            if wp_config is None:
                raise SandboxError(
                    "No wp-config.php in the backup. WordPress drills need the site files."
                )
            if not site.sql_dump:
                raise SandboxError(
                    "This WordPress site has no sql_dump. Download the database export "
                    "from hPanel and add it to the config. The Hostinger API does not "
                    "provide that archive."
                )
            if not site.production_url:
                raise SandboxError("WordPress drills need production_url for the sandbox rewrite.")
            url = sandbox.start_wordpress(
                docroot=docroot,
                wp_config=wp_config,
                sql_dump=_resolve(root, site.sql_dump),
                production_url=site.production_url,
                drill_id=drill_id,
                production_host=site.production_host,
            )
        else:
            url = sandbox.start_static(docroot, drill_id)
        if on_ready is not None:
            on_ready(sandbox, docroot, url)
        log(f"Crawling {url}")
        pages = list(crawl_fn(url, site))
        if site.type == "wordpress":
            db_ok = sandbox.check_database()
            if any(page.db_error for page in pages):
                db_ok = False
        passed = (db_ok is not False) and bool(pages) and all(page.ok for page in pages)
        if not pages:
            error = "The drill finished without checking any URL."
            passed = False
        log("Drill passed." if passed else "Drill failed.")
    except Exception as exc:  # noqa: BLE001 - recorded, then teardown still runs
        error = str(exc)
        passed = False
        log(f"Drill failed: {exc}")
    finally:
        sandbox.teardown()
    if sandbox.cleanup_problems:
        passed = False
        cleanup_error = "Incomplete sandbox cleanup: " + "; ".join(sandbox.cleanup_problems)
        error = f"{error}; {cleanup_error}" if error else cleanup_error
    finished = utcnow()
    summary = _summary(passed, pages, error)
    record = DrillRecord(
        id=drill_id,
        site_slug=site.slug,
        started_at=started,
        finished_at=finished,
        passed=passed,
        mode=sandbox.mode or "not-started",
        error=error,
        report_path=None,
        summary=summary,
    )
    report_path = reports_dir / f"{drill_id}.html"
    try:
        write_report(
            report_path,
            site_name=site.name,
            record=record,
            pages=pages,
            db_ok=db_ok,
            log_lines=log.lines,
        )
        record.report_path = str(report_path)
    except OSError as exc:
        log(f"Could not write the report: {exc}")
    try:
        history.record(record)
    except Exception as exc:  # noqa: BLE001
        log(f"Could not save drill history: {exc}")
    return DrillResult(
        id=drill_id,
        site_slug=site.slug,
        site_name=site.name,
        passed=passed,
        mode=record.mode,
        started_at=started,
        finished_at=finished,
        pages=pages,
        report_path=report_path if report_path.exists() else None,
        log=list(log.lines),
        error=error,
        db_ok=db_ok,
        torn_down=sandbox.torn_down,
    )


def run_all(config: AppConfig, **kwargs) -> list[DrillResult]:
    results = []
    for site in config.sites:
        if not site.enabled:
            continue
        results.append(run_drill(site, **kwargs))
    return results


def _resolve(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        # A frozen app keeps persistent config/state outside its read-only
        # extraction directory.  Defaults may refer to bundled demo resources.
        bundled = getattr(sys, "_MEIPASS", None)
        if bundled:
            candidate = Path(bundled) / path
            if candidate.exists():
                return candidate
        path = root / path
    return path


def _new_id(slug: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")
    return f"{stamp}-{slug}-{secrets.token_hex(2)}"


def _summary(passed: bool, pages: list[PageResult], error: str | None) -> str:
    if error and not pages:
        return error
    ok = sum(1 for page in pages if page.ok)
    noun = "page" if len(pages) == 1 else "pages"
    base = f"{ok} of {len(pages)} {noun} passed"
    if error and not passed:
        return f"{base}. {error}"
    return base
