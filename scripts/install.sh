#!/usr/bin/env bash
# 阶段二：离线安装（方案 §28.2）。目标 Mac 上运行，全程不联网。
set -euo pipefail

ROOT="${INSTALL_ROOT:-/Users/Shared/offline-translator}"
BUNDLE="${1:-offline-bundle}"
MODEL_FILE="${MODEL_FILE:-HY-MT1.5-1.8B-Q4_K_M.gguf}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[ -d "$BUNDLE" ] || { echo "找不到 $BUNDLE，请先在联网机器上跑 prepare-bundle.sh（§28.1）" >&2; exit 1; }

echo "安装到 $ROOT"
mkdir -p "$ROOT"/{models,data/{processing,output,temp},config,logs,runtime}

echo "[1/6] 拷贝应用代码"
cp -R "$REPO_DIR/app"    "$ROOT/"
cp -R "$REPO_DIR/config" "$ROOT/"
cp    "$REPO_DIR/requirements.in" "$ROOT/" 2>/dev/null || true
[ -f "$REPO_DIR/requirements.txt" ] && cp "$REPO_DIR/requirements.txt" "$ROOT/"

echo "[2/6] Python 环境（全程离线）"
uv venv "$ROOT/.venv"
# ⚠ --offline --no-index 必须带上。不带的话 uv 会静默回落到 PyPI，
#   那就不是离线安装了（方案 §28.2 的明确警告）。
uv pip install --python "$ROOT/.venv/bin/python" \
  --offline --no-index \
  --find-links "$BUNDLE/wheels" \
  -r "$ROOT/requirements.txt"

echo "[3/6] llama-server 与模型权重"
cp "$BUNDLE/llama/llama-server" "$ROOT/runtime/"
chmod +x "$ROOT/runtime/llama-server"
mkdir -p "$ROOT/models/hy-mt1.5"
cp -R "$BUNDLE/models/hy-mt1.5/." "$ROOT/models/hy-mt1.5/"

echo "[4/6] Docling 模型产物与中文字体"
mkdir -p "$HOME/.cache/docling"
cp -R "$BUNDLE/docling/." "$HOME/.cache/docling/"
mkdir -p "$ROOT/fonts"
if compgen -G "$BUNDLE/fonts/*" > /dev/null; then
  cp "$BUNDLE"/fonts/* "$ROOT/fonts/"
  mkdir -p "$HOME/Library/Fonts"
  cp "$BUNDLE"/fonts/* "$HOME/Library/Fonts/"
fi

echo "[5/6] 渲染 launchd plist"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS"
for name in llama api; do
  sed -e "s|@@INSTALL_ROOT@@|$ROOT|g" -e "s|@@MODEL_FILE@@|$MODEL_FILE|g" \
    "$REPO_DIR/deploy/launchd/com.offline-translator.$name.plist" \
    > "$AGENTS/com.offline-translator.$name.plist"
done

echo "[6/6] 源目录与输出目录"
mkdir -p /Users/Shared/translator/{Source,Translated}

echo
echo "安装完成。"
echo "下一步：scripts/start.sh 加载服务，再按 §28.5 断网验收。"
echo "提醒：要满足「开机自动启动」还需开启自动登录（系统设置 → 用户与群组），见 §17.1。"
