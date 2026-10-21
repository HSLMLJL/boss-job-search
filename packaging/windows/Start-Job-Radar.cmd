@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0"

if not exist "%ROOT%runtime\pythonw.exe" (
  echo Job Radar runtime is incomplete. Please download and extract the release again.
  pause
  exit /b 1
)

if not exist "%ROOT%app\job_radar\desktop.py" (
  echo Job Radar application files are incomplete. Please download and extract the release again.
  pause
  exit /b 1
)

start "" /d "%ROOT%" "%ROOT%runtime\pythonw.exe" -m job_radar.desktop %*
if errorlevel 1 (
  echo Job Radar could not be started. Please see PORTABLE-README.txt.
  pause
  exit /b 1
)
exit /b 0
