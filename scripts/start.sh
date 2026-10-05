#!/usr/bin/env bash
# 译阅 — 一键启动（macOS / Linux）
set -e
cd "$(dirname "$0")/.."

echo "[1/3] 安装后端依赖（首次较慢）..."
(cd backend && uv sync)

echo "[2/3] 构建前端..."
(cd frontend && npm install && npm run build)

echo "[3/3] 启动服务：http://127.0.0.1:8000"
(cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000)
