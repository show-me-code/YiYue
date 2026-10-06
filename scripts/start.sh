#!/usr/bin/env bash
# 译阅 — 一键启动（macOS / Linux）：装依赖、构建前端、起服务并自动打开浏览器
set -e
cd "$(dirname "$0")/.."
(cd backend && uv run python ../scripts/launch.py)
