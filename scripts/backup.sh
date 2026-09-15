#!/usr/bin/env bash
# 备份（方案 §28.4）。只备状态 —— 模型与 runtime 可按 §28.1/§28.2 重建，不进备份。
set -euo pipefail

ROOT="${INSTALL_ROOT:-/Users/Shared/offline-translator}"
OUT="${1:-backup-$(date +%F).tgz}"

[ -d "$ROOT" ] || { echo "找不到安装目录 $ROOT" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# 原来这里是 `tar ... $(cd "$ROOT" && ls *.db data/*.db ...)`：
# 未加引号的命令替换靠分词拼参数，路径带空格就散架；而且目录不存在时 tar 直接失败。
# 改成数组逐项判断，缺什么说什么。
args=(czf "$OUT")

for rel in config data/output; do
  if [ -e "$ROOT/$rel" ]; then
    args+=(-C "$ROOT" "$rel")
  else
    echo "⚠ 跳过不存在的 $ROOT/$rel" >&2
  fi
done

# 任务库要用 sqlite 的在线备份，不能直接打包：
# 服务在跑的时候 .db 和它的 -wal / -shm 不是同一个时间点，直接 tar 可能拿到坏库。
db="$ROOT/data/jobs.db"
if [ -f "$db" ]; then
  mkdir -p "$STAGE/data"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$db" ".backup '$STAGE/data/jobs.db'"
    echo "任务库：已用 sqlite3 .backup 取一致性快照"
  else
    cp "$db" "$STAGE/data/jobs.db"
    echo "⚠ 没有 sqlite3，直接拷了 $db —— 请先 ./scripts/stop.sh 再备份" >&2
  fi
  args+=(-C "$STAGE" data/jobs.db)
else
  echo "⚠ 没有 $db，本次不含任务库" >&2
fi

tar "${args[@]}"
echo "已备份到 $OUT"
tar tzf "$OUT"
