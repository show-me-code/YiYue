@echo off
chcp 65001 >nul
title YiYue - PDF Translation Workbench
cd /d "%~dp0"

echo ============================================
echo   YiYue - local PDF translation workbench
echo   Browser opens automatically; close this window to stop.
echo ============================================
echo.

cd backend
if exist ".venv\Scripts\python.exe" (
  echo [start] using bundled virtualenv .venv
  ".venv\Scripts\python.exe" "%~dp0scripts\launch.py"
) else (
  echo [start] .venv not found, preparing dependencies with uv ...
  uv run python "%~dp0scripts\launch.py"
)

if errorlevel 1 (
  echo.
  echo ------------------------------------------------------------
  echo Startup failed. Full log: backend\data\launch.log
  echo Common causes: port 8000 occupied (the launcher cleans it
  echo automatically), missing dependencies, or models not fetched.
  echo ------------------------------------------------------------
)
pause
