#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 加载两个 LaunchAgent（方案 §28.4）。
#
# ⚠ 用现代的 bootstrap/bootout，不用 `launchctl load -w`。
#   实机日志 tests/dev-mac/test.log 对应的那次运行里，旧写法打了两行
#   "Load failed: 5: Input/output error"（服务其实早已加载），却仍然返回 0 ——
#   脚本照样打印「已加载」。**报错被吞掉、脚本谎报成功**，这是最坏的一种失败。
#
# 本脚本是幂等的「重新加载」：已加载的先 bootout 再 bootstrap。
# 这样改完 plist 直接跑 start.sh 就能生效，不会静默沿用旧配置。
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
NAMES=(llama api)
STARTUP_TIMEOUT="${STARTUP_TIMEOUT:-60}"

for name in "${NAMES[@]}"; do
  label="com.offline-translator.$name"
  plist="$AGENTS/$label.plist"

  [ -f "$plist" ] || {
    echo "找不到 $plist —— 请先跑 scripts/install.sh（方案 §28.2）" >&2
    exit 1
  }

  # 占位符没被替换干净的 plist 能被 launchd 接受，但指向的是字面量路径，
  # 表现为服务反复退出。在这里拦住，比去翻 KeepAlive 的重启日志便宜得多。
  if grep -q '@@' "$plist"; then
    echo "$plist 里还留着 @@ 占位符，install.sh 的渲染没跑完" >&2
    exit 1
  fi

  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    echo "[$label] 已加载，先卸载"
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    # bootout 是异步的：不等它真的消失就 bootstrap，会拿到 EBUSY。
    for _ in $(seq 1 100); do
      launchctl print "$DOMAIN/$label" >/dev/null 2>&1 || break
      sleep 0.1
    done
  fi

  # 只有被 `launchctl disable` 过才需要，平时是空操作；失败不该中断加载。
  launchctl enable "$DOMAIN/$label" 2>/dev/null || true

  echo "[$label] bootstrap"
  launchctl bootstrap "$DOMAIN" "$plist"
done

echo
echo "── launchctl list ──"
launchctl list | grep offline-translator || true

echo
echo "── 等待服务就绪（最多 ${STARTUP_TIMEOUT}s）──"
echo "launchd 不保证启动顺序，api 可能先于 llama-server 起来 ——"
echo "这是预期行为，/api/v1/health 会如实返回 503 直到 llama-server 就绪。"

HEALTH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/health.sh"
deadline=$((SECONDS + STARTUP_TIMEOUT))
while :; do
  if "$HEALTH" >/dev/null 2>&1; then
    exec "$HEALTH"
  fi
  [ "$SECONDS" -lt "$deadline" ] || break
  sleep 2
done

echo
echo "⚠ ${STARTUP_TIMEOUT}s 内未就绪。当前健康状态："
"$HEALTH" || true
echo
echo "看日志定位（方案 §28.4）："
echo "  tail -40 /Users/Shared/offline-translator/logs/llama.err.log"
echo "  tail -40 /Users/Shared/offline-translator/logs/api.err.log"
exit 1
