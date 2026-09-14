#!/usr/bin/env bash
# 卸载两个 LaunchAgent（方案 §28.4）。顺序与 start 相反：先停 api 再停 llama。
set -euo pipefail
AGENTS="$HOME/Library/LaunchAgents"
for name in api llama; do
  launchctl unload "$AGENTS/com.offline-translator.$name.plist" || true
done
echo "已卸载。"
