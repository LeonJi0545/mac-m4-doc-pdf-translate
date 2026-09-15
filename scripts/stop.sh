#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 卸载两个 LaunchAgent（方案 §28.4）。顺序与 start 相反：先停 api 再停 llama。
#
# ⚠ 与 start.sh 同理，用 bootout 而不是 `launchctl unload`：
#   旧写法在「服务本来就没加载」时同样会打 "Unload failed: 5: Input/output error"，
#   再被 `|| true` 吞掉，看不出到底停没停。
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DOMAIN="gui/$(id -u)"
rc=0

for name in api llama; do
  label="com.offline-translator.$name"

  if ! launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    echo "[$label] 未加载，跳过"
    continue
  fi

  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true

  # bootout 是异步的 —— 不等它落地就报「已卸载」会骗人。
  stopped=0
  for _ in $(seq 1 100); do
    if ! launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
      stopped=1
      break
    fi
    sleep 0.1
  done

  if [ "$stopped" -eq 1 ]; then
    echo "[$label] 已卸载"
  else
    echo "[$label] 卸载后仍在 launchd 里，请查 launchctl print $DOMAIN/$label" >&2
    rc=1
  fi
done

exit $rc
