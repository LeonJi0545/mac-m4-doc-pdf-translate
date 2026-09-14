#!/usr/bin/env bash
# 健康检查（方案 §28.4）。只打 127.0.0.1，不产生任何出站连接。
set -euo pipefail
rc=0
curl -sf http://127.0.0.1:8001/health           > /dev/null || { echo "llama-server DOWN"; rc=1; }
curl -sf http://127.0.0.1:8000/api/v1/health    > /dev/null || { echo "api DOWN"; rc=1; }
[ $rc -eq 0 ] && echo "both services OK"
exit $rc
