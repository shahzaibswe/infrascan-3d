@echo off
rem InfraScan 3D launcher - double-click or run from a terminal.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH. Install Python 3.11+ and re-run.
  pause
  exit /b 1
)

if not exist ".venv" (
  echo Creating virtual environment...
  python -m venv .venv
  call ".venv\Scripts\activate.bat"
  echo Installing dependencies...
  python -m pip install --upgrade pip
  pip install -r requirements.txt
) else (
  call ".venv\Scripts\activate.bat"
)

if not exist "samples\road_surface.jpg" python samples\generate_samples.py

set PYTHONPATH=.
echo Starting InfraScan 3D on http://127.0.0.1:8765/  (Ctrl+C to stop)
start "" http://127.0.0.1:8765/
python -m uvicorn infrascan.server:app --host 127.0.0.1 --port 8765
pause
