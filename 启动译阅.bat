@echo off
chcp 65001 >nul
title 译阅 YiYue
cd /d "%~dp0"

echo ============================================
echo   译阅 YiYue — 本地 PDF 翻译工作台
echo   启动后浏览器将自动打开，关闭本窗口即停止
echo ============================================
echo.

rem 后端依赖：backend/.venv 不存在时 uv run 会自动安装（需 Python 3.12 + uv）
cd backend
uv run python "%~dp0scripts\launch.py"
pause
