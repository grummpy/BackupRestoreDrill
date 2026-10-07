@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python 3.11+ was not found.
  echo Install it from https://www.python.org/downloads/ and then double-click this launcher again.
  pause
  exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo Python 3.11 or newer is required.
  python --version
  echo Download a newer Python from https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist .venv (
  echo Creating a virtual environment and installing pinned requirements...
  python -m venv .venv
  call .venv\Scripts\python -m pip install --upgrade pip
  call .venv\Scripts\python -m pip install -r requirements.txt
)

.venv\Scripts\python -m backuprestoredrill
if errorlevel 1 pause
endlocal
