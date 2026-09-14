#!/usr/bin/env bash
# 加载两个 LaunchAgent（方案 §28.4）。
set -euo pipefail
AGENTS="$HOME/Library/LaunchAgents"
for name in llama api; do
  launchctl load -w "$AGENTS/com.offline-translator.$name.plist"
done
launchctl list | grep offline-translator || true
echo "已加载。注意 launchd 不保证启动顺序，api 可能先于 llama-server 起来 ——"
echo "这是预期行为，/api/v1/health 会如实返回 503 直到 llama-server 就绪。"
