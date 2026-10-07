"""Local SQLite history of drill runs."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class DrillRecord:
    id: str
    site_slug: str
    started_at: datetime
    finished_at: datetime
    passed: bool
    mode: str
    error: str | None
    report_path: str | None
    summary: str


class History:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS drills (
                id TEXT PRIMARY KEY,
                site_slug TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                passed INTEGER NOT NULL,
                mode TEXT NOT NULL,
                error TEXT,
                report_path TEXT,
                summary TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_drills_site ON drills(site_slug, finished_at)"
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def record(self, row: DrillRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO drills (
                    id, site_slug, started_at, finished_at, passed, mode, error, report_path, summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.id,
                    row.site_slug,
                    row.started_at.isoformat(),
                    row.finished_at.isoformat(),
                    1 if row.passed else 0,
                    row.mode,
                    row.error,
                    row.report_path,
                    row.summary,
                ),
            )
            self._conn.commit()

    def get(self, drill_id: str) -> DrillRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM drills WHERE id = ?", (drill_id,)).fetchone()
        return _record(row) if row else None

    def latest(self, slug: str) -> DrillRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM drills WHERE site_slug = ? ORDER BY finished_at DESC LIMIT 1",
                (slug,),
            ).fetchone()
        return _record(row) if row else None

    def latest_pass(self, slug: str) -> DrillRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM drills
                WHERE site_slug = ? AND passed = 1
                ORDER BY finished_at DESC LIMIT 1
                """,
                (slug,),
            ).fetchone()
        return _record(row) if row else None

    def for_site(self, slug: str, limit: int = 12) -> list[DrillRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM drills WHERE site_slug = ?
                ORDER BY finished_at DESC LIMIT ?
                """,
                (slug, limit),
            ).fetchall()
        return [_record(row) for row in rows]


def is_stale(history: History, slug: str, *, now: datetime, stale_after_days: int) -> bool:
    """True when the site has no passing drill, or the last pass is more than N days old."""
    last_pass = history.latest_pass(slug)
    if last_pass is None:
        return True
    return now - last_pass.finished_at > timedelta(days=stale_after_days)


def age_label(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "never"
    delta = now - when
    if delta.days >= 1:
        unit = "day" if delta.days == 1 else "days"
        return f"{delta.days} {unit} ago"
    hours = delta.seconds // 3600
    if hours >= 1:
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit} ago"
    return "just now"


def _record(row: sqlite3.Row) -> DrillRecord:
    return DrillRecord(
        id=row["id"],
        site_slug=row["site_slug"],
        started_at=parse_time(row["started_at"]),
        finished_at=parse_time(row["finished_at"]),
        passed=bool(row["passed"]),
        mode=row["mode"],
        error=row["error"],
        report_path=row["report_path"],
        summary=row["summary"],
    )
