"""Optional monthly schedulers. Nothing is installed until the user runs install."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

LABEL = "com.grummpy.backuprestoredrill"
TASK_NAME = "BackupRestoreDrill"
MARKER = "# backuprestoredrill-monthly"
DAY = 15
HOUR = 9


def cron_line(python: str, repo: Path) -> str:
    command = (
        f"cd {shlex.quote(str(repo))} && {shlex.quote(python)} -m backuprestoredrill drill --all"
    )
    return f"0 {HOUR} {DAY} * * {command} {MARKER}"


def render_launchd(python: str, repo: Path) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>WorkingDirectory</key>
  <string>{_xml(str(repo))}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{_xml(python)}</string>
    <string>-m</string>
    <string>backuprestoredrill</string>
    <string>drill</string>
    <string>--all</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Day</key>
    <integer>{DAY}</integer>
    <key>Hour</key>
    <integer>{HOUR}</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
"""


def render_task_xml(python: str, repo: Path) -> str:
    months = "".join(
        f"<{name}/>"
        for name in (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        )
    )
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-15T0{HOUR}:00:00</StartBoundary>
      <ScheduleByMonth>
        <DaysOfMonth><Day>{DAY}</Day></DaysOfMonth>
        <Months>{months}</Months>
      </ScheduleByMonth>
    </CalendarTrigger>
  </Triggers>
  <Actions>
    <Exec>
      <Command>{_xml(python)}</Command>
      <Arguments>-m backuprestoredrill drill --all</Arguments>
      <WorkingDirectory>{_xml(str(repo))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install_cron(python: str, repo: Path, runner) -> str:
    line = cron_line(python, repo)
    listed = runner(["crontab", "-l"])
    existing = listed.stdout if listed.returncode == 0 else ""
    kept = [row for row in existing.splitlines() if MARKER not in row and row.strip()]
    kept.append(line)
    payload = ("\n".join(kept) + "\n").encode()
    result = runner(["crontab", "-"], input=payload)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "crontab refused the monthly drill.")
    return line


def uninstall_cron(runner) -> None:
    listed = runner(["crontab", "-l"])
    if listed.returncode != 0:
        return
    kept = [row for row in listed.stdout.splitlines() if MARKER not in row]
    payload = ("\n".join(kept) + ("\n" if kept else "")).encode()
    runner(["crontab", "-"], input=payload)


def install_launchd(python: str, repo: Path, *, home: Path, runner) -> Path:
    path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_launchd(python, repo), encoding="utf-8")
    result = runner(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "launchctl could not load the monthly drill.")
    return path


def uninstall_launchd(*, home: Path, runner) -> None:
    path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    runner(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)])
    if path.exists():
        path.unlink()


def install_windows(python: str, repo: Path, *, xml_path: Path, runner) -> Path:
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(render_task_xml(python, repo), encoding="utf-16")
    result = runner(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"]
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "schtasks could not create the monthly drill.")
    return xml_path


def uninstall_windows(runner) -> None:
    runner(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])


def _xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
