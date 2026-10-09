"""Optional PyInstaller build.

Linux CI does not need to run this. On macOS it builds a .app; on Windows, an .exe.
Both use assets/icon.icns or assets/icon.ico. The web UI is still local-only.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a local BackupRestoreDrill app bundle.")
    parser.add_argument("--skip", action="store_true", help="Exit 0 without building (for CI).")
    args = parser.parse_args()
    if args.skip:
        print("Skipping the packaged build.")
        return 0
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Install PyInstaller first: pip install pyinstaller", file=sys.stderr)
        return 1
    system = platform.system()
    icon = ROOT / ("assets/icon.icns" if system == "Darwin" else "assets/icon.ico")
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        "BackupRestoreDrill",
        "--add-data",
        f"{ROOT / 'assets'}{';' if system == 'Windows' else ':'}assets",
        "--add-data",
        f"{ROOT / 'config.example.yaml'}{';' if system == 'Windows' else ':'}." ,
        "--add-data",
        f"{ROOT / 'examples' / 'brochure'}{';' if system == 'Windows' else ':'}examples/brochure",
        "--icon",
        str(icon),
        str(ROOT / "backuprestoredrill" / "__main__.py"),
    ]
    if system == "Darwin":
        command.insert(5, "--windowed")
        command.insert(6, "--osx-bundle-identifier")
        command.insert(7, "com.grummpy.backuprestoredrill")
    elif system == "Windows":
        command.insert(5, "--windowed")
    else:
        print("This script produces a macOS .app or a Windows .exe. On Linux, pass --skip.")
        print("The launchers in the repo root are the supported way to start the app here.")
        return 0
    subprocess.run(command, cwd=ROOT, check=True)
    print("Build finished under dist/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
