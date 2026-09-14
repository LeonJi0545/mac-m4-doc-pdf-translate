#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 阶段五：断网验收（方案 §28.5）。**这一步不能省。**
# 联网环境下的「跑通」不能证明离线可用 —— Docling、OCR、tokenizer 都可能在
# 首次使用某功能时才尝试联网。
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cat <<'TXT'
断网验收清单（逐条人工确认）：

  1. 物理断网：关 Wi-Fi / 拔网线，不要只靠防火墙规则
       networksetup -setairportpower en0 off

  2. 重启 Mac，验证自动登录 + 两个服务自起
       sudo reboot

  3. 重启后确认服务在跑
       launchctl list | grep offline-translator
       ./scripts/health.sh

  4. 跑 §25 的四份测试文档，覆盖 EN / DE / IT / ZH 四个方向
     （必须跑完整业务流程，含扫描件 OCR —— 否则不算验过）

  5. 确认整个过程没有任何出站连接
       lsof -i -P | grep -v '127.0.0.1' | grep -iE 'llama|python'

TXT

echo "── 当前状态 ──"
echo "服务："
launchctl list 2>/dev/null | grep offline-translator || echo "  （未加载）"

echo "健康："
"$(dirname "${BASH_SOURCE[0]}")/health.sh" || true

echo "出站连接（应为空）："
if lsof -i -P 2>/dev/null | grep -v '127.0.0.1' | grep -iE 'llama|python'; then
  echo "  ⚠ 检出非 loopback 连接 —— 违反 §20，需排查"
  exit 1
else
  echo "  无"
fi
