@echo off
setlocal
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo Install uv first. See README.md for setup instructions.
  pause
  exit /b 1
)
uv run python -m job_radar ui %*
if errorlevel 1 pause
