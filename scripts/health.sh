#!/usr/bin/env bash
# 健康检查（方案 §28.4）。只打 127.0.0.1，不产生任何出站连接。
set -euo pipefail

# --max-time：llama-server 刚起来时可能在加载权重，连上了却不回包。
# 不设超时的话这里会挂住，start.sh 的等待循环也跟着卡死。
TIMEOUT="${HEALTH_TIMEOUT:-5}"
rc=0

curl -sf --max-time "$TIMEOUT" http://127.0.0.1:8001/health        > /dev/null \
  || { echo "llama-server DOWN"; rc=1; }
curl -sf --max-time "$TIMEOUT" http://127.0.0.1:8000/api/v1/health > /dev/null \
  || { echo "api DOWN"; rc=1; }

# 原来写成 `[ $rc -eq 0 ] && echo ...`，rc≠0 时整条 AND 列表返回 1，
# set -e 直接在这里退出，底下的 exit $rc 根本执行不到 —— 结果侥幸相同，但不可读。
if [ "$rc" -eq 0 ]; then
  echo "both services OK"
fi
exit "$rc"
