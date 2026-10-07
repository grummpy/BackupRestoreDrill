#!/bin/bash
# BackupRestoreDrill launcher for macOS and Linux.
# First run creates .venv and installs pinned requirements. Later runs start faster.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.11+ was not found."
  echo "Install it from https://www.python.org/downloads/ and then open this launcher again."
  exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 3.11 or newer is required. This machine has:"
  python3 --version || true
  echo "Download a newer Python from https://www.python.org/downloads/"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating a virtual environment and installing pinned requirements..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

exec .venv/bin/python -m backuprestoredrill
