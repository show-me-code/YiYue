@echo off
rem 译阅 — 一键启动（Windows）
cd /d "%~dp0.."

echo [1/3] 安装后端依赖（首次较慢）...
cd backend
uv sync || goto :err

echo [2/3] 构建前端...
cd ..\frontend
call npm install || goto :err
call npm run build || goto :err

echo [3/3] 启动服务：http://127.0.0.1:8000
cd ..\backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
goto :eof

:err
echo 启动失败，请检查上方错误信息。
pause
