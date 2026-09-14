#!/usr/bin/env bash
# 备份（方案 §28.4）。只备状态 —— 模型与 runtime 可按 §28.1/§28.2 重建，不进备份。
set -euo pipefail
ROOT="${INSTALL_ROOT:-/Users/Shared/offline-translator}"
OUT="${1:-backup-$(date +%F).tgz}"
tar czf "$OUT" \
  -C "$ROOT" config data/output \
  $(cd "$ROOT" && ls *.db data/*.db 2>/dev/null || true)
echo "已备份到 $OUT"
