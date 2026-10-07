from __future__ import annotations

import os
import struct
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG")
    return struct.unpack(">II", data[16:24])


def test_launchers_exist_and_reference_the_entry_point() -> None:
    mac = REPO / "Launch BackupRestoreDrill.command"
    bat = REPO / "Launch BackupRestoreDrill.bat"
    shell = REPO / "launch.sh"
    desktop = REPO / "backuprestoredrill.desktop"
    for path in (mac, bat, shell, desktop):
        assert path.is_file(), path
    for path in (mac, shell):
        assert os.access(path, os.X_OK), path
    for path in (mac, bat, shell):
        text = path.read_text(encoding="utf-8")
        assert "https://www.python.org/downloads/" in text
        assert "python -m backuprestoredrill" in text
        assert "requirements.txt" in text
        assert ".venv" in text
    desktop_text = desktop.read_text(encoding="utf-8")
    assert "launch.sh" in desktop_text
    assert "icon.png" in desktop_text


def test_icon_files() -> None:
    assets = REPO / "assets"
    svg = (assets / "icon.svg").read_text(encoding="utf-8")
    assert "<svg" in svg
    assert _png_size(assets / "icon.png") == (1024, 1024)
    assert _png_size(assets / "icon-1024.png") == (1024, 1024)
    assert _png_size(assets / "icon-512.png") == (512, 512)
    assert (assets / "icon.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"
    assert (assets / "icon.icns").read_bytes()[:4] == b"icns"
    cover = (REPO / "docs" / "cover.jpg").read_bytes()
    assert cover[:2] == b"\xff\xd8"
