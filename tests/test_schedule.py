from __future__ import annotations

import sys
from pathlib import Path

from backuprestoredrill.schedule import (
    cron_line,
    install_cron,
    install_launchd,
    render_launchd,
    render_task_xml,
)

REPO = Path(__file__).resolve().parents[1]


class _Result:
    def __init__(self, code: int = 0, out: str = ""):
        self.returncode = code
        self.stdout = out
        self.stderr = ""


def test_schedule_templates_are_monthly_and_off_until_install() -> None:
    python = "/usr/bin/python3"
    repo = Path("/work/BackupRestoreDrill")
    cron = cron_line(python, repo)
    assert cron.startswith("0 9 15 ")
    assert "python -m backuprestoredrill drill --all" in cron.replace(python, "python")
    plist = render_launchd(python, repo)
    assert "<integer>15</integer>" in plist
    assert "<false/>" in plist
    assert "backuprestoredrill" in plist
    xml = render_task_xml(python, repo)
    assert "<Day>15</Day>" in xml
    assert "backuprestoredrill drill --all" in xml
    for name in ("launch.sh", "Launch BackupRestoreDrill.command", "Launch BackupRestoreDrill.bat"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert "schedule install" not in text


def test_cron_install_uses_a_runner_and_keeps_other_lines() -> None:
    calls = []

    def runner(args, input=None):
        calls.append((args, input))
        if args == ["crontab", "-l"]:
            return _Result(0, "0 1 * * * echo keep\n")
        return _Result(0, "")

    line = install_cron(sys.executable, Path("/work"), runner)
    assert "backuprestoredrill" in line
    assert calls[1][0] == ["crontab", "-"]
    payload = calls[1][1]
    assert b"echo keep" in payload
    assert b"backuprestoredrill" in payload


def test_launchd_install_writes_plist_for_a_fake_home(tmp_path: Path) -> None:
    calls = []

    def runner(args, input=None):
        calls.append(args)
        return _Result()

    path = install_launchd(sys.executable, Path("/work"), home=tmp_path, runner=runner)
    assert path.is_file()
    assert path.parent.name == "LaunchAgents"
    assert "15" in path.read_text(encoding="utf-8")
    assert calls[0][0] == "launchctl"
    assert not (Path.home() / "Library" / "LaunchAgents" / path.name).exists()
