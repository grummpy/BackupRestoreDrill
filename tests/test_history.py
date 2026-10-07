from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from backuprestoredrill.history import DrillRecord, History, is_stale, utcnow


def _add(history: History, slug: str, *, passed: bool, finished) -> None:
    history.record(
        DrillRecord(
            id=f"{slug}-{finished.timestamp()}-{int(passed)}",
            site_slug=slug,
            started_at=finished,
            finished_at=finished,
            passed=passed,
            mode="static-python",
            error=None,
            report_path=None,
            summary="test",
        )
    )


def test_stale_when_never_passed_or_older_than_35_days(tmp_path: Path) -> None:
    history = History(tmp_path / "drills.sqlite")
    now = utcnow()
    assert is_stale(history, "brochure", now=now, stale_after_days=35)

    _add(history, "brochure", passed=False, finished=now - timedelta(days=1))
    assert is_stale(history, "brochure", now=now, stale_after_days=35)

    _add(history, "old", passed=True, finished=now - timedelta(days=40))
    assert is_stale(history, "old", now=now, stale_after_days=35)

    _add(history, "fresh", passed=True, finished=now - timedelta(days=35))
    assert is_stale(history, "fresh", now=now, stale_after_days=35) is False

    _add(history, "edge", passed=True, finished=now - timedelta(days=35, seconds=1))
    assert is_stale(history, "edge", now=now, stale_after_days=35) is True

    _add(history, "recent", passed=True, finished=now - timedelta(days=10))
    _add(history, "recent", passed=False, finished=now)
    assert is_stale(history, "recent", now=now, stale_after_days=35) is False
