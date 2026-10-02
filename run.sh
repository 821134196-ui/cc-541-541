#!/usr/bin/env bash
# 一条本地启动命令：./run.sh [端口]
set -e
cd "$(dirname "$0")"
PORT="${1:-8000}"
python3 -m app.seed
exec python3 -m app.server --host 0.0.0.0 --port "${PORT}"
