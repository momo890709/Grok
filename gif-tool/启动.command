#!/bin/bash
# Mac 上双击这个文件就能打开 GIF 工具
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "没找到 ffmpeg，先在终端跑：brew install ffmpeg"
fi
exec python3 server.py
