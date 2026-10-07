#!/usr/bin/env bash
# 译阅 — 一键启动（macOS / Linux）：装依赖、构建前端、起服务并自动打开浏览器
# 优先用项目自带虚拟环境，避免因 PATH 里没有 uv 而启动失败
set -e
cd "$(dirname "$0")/.."

if [ -x "backend/.venv/bin/python" ]; then
  (cd backend && .venv/bin/python ../scripts/launch.py)
else
  echo "[启动] 未找到 .venv，改用 uv 准备依赖（首次较慢）..."
  (cd backend && uv run python ../scripts/launch.py)
fi
