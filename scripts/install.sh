#!/usr/bin/env bash
# 阶段二：离线安装（方案 §28.2）。目标 Mac 上运行，全程不联网。
set -euo pipefail

ROOT="${INSTALL_ROOT:-/Users/Shared/offline-translator}"
BUNDLE="${1:-offline-bundle}"
MODEL_FILE="${MODEL_FILE:-HY-MT1.5-1.8B-Q4_K_M.gguf}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[ -d "$BUNDLE" ] || { echo "找不到 $BUNDLE，请先在联网机器上跑 prepare-bundle.sh（§28.1）" >&2; exit 1; }

echo "安装到 $ROOT"
mkdir -p "$ROOT"/{app,models/docling,data/{processing,output,temp},config,logs,runtime}

echo "[1/8] 拷贝应用代码"
# 一律用 `src/.` -> `dst/` 的写法。`cp -R src dst/` 在目标已存在同名目录时，
# BSD cp 与 GNU cp 的行为不一致（可能变成 dst/src/src），而 $ROOT 下这些目录
# 恰恰是上面 mkdir -p 刚建出来的。
cp -R "$REPO_DIR/app/." "$ROOT/app/"

# 配置与术语库安装后都是要人工改的（§4.3 填 model.path 与字体路径，§9 加术语）。
# 重跑 install.sh 不能把这些改动冲掉 —— 已存在就保留，新版落成 .new 让人自己合并。
( cd "$REPO_DIR/config" && find . -type f -print0 ) | while IFS= read -r -d '' rel; do
  src="$REPO_DIR/config/$rel"
  dst="$ROOT/config/$rel"
  mkdir -p "$(dirname "$dst")"
  if [ ! -f "$dst" ]; then
    cp "$src" "$dst"
  elif ! cmp -s "$src" "$dst"; then
    cp "$src" "$dst.new"
    echo "    保留现有 ${rel#./}；新版落在 ${rel#./}.new，请自行比对"
  fi
done

# 保留旧配置是对的（不能冲掉运维填的 model.path），但「旧配置里有必须跟进的键」
# 是个真实风险：pdf.docling_artifacts_path 就是这么留在 ~/.cache/docling 上的。
if [ -n "$(find "$ROOT/config" -name '*.new' 2>/dev/null)" ]; then
  echo "⚠ 保留了现有配置，仓库新版落成了 .new 文件："
  find "$ROOT/config" -name '*.new' | sed 's/^/    /'
  echo "  新版可能改了必须跟进的键（例如 pdf.docling_artifacts_path）。"
  echo "  第 8 步的离线自检会用**部署中的**那份配置真跑一遍，指错了会在那里失败。"
fi

# 锁文件来自 bundle（由 prepare-bundle.sh 在联网机上编译），不是仓库
[ -f "$BUNDLE/requirements.txt" ] || {
  echo "找不到 $BUNDLE/requirements.txt —— 该文件由 prepare-bundle.sh 生成，请确认 bundle 完整" >&2
  exit 1
}
cp "$BUNDLE/requirements.txt" "$ROOT/"

echo "[2/8] Python 环境（全程离线）"
# wheel 是按 cp3XX 打的：venv 的 Python 小版本必须和备料机一致，
# 否则 --no-index 下会「找不到任何匹配的 wheel」，而这台机器没法上网补。
# 不钉版本的话 uv 会自己挑一个 —— A/B 同机时碰巧对上，换台机器就翻车。
if [ -f "$BUNDLE/python-version" ]; then
  PY_VERSION="$(cat "$BUNDLE/python-version")"
  echo "    按 bundle 的记录建 Python $PY_VERSION 环境"
  uv venv --python "$PY_VERSION" "$ROOT/.venv"
else
  echo "⚠ $BUNDLE 里没有 python-version（旧版 bundle）。" >&2
  echo "  venv 的 Python 版本将由 uv 自行决定，与 wheelhouse 对不上就会装不上。" >&2
  uv venv "$ROOT/.venv"
fi
# ⚠ --offline --no-index 必须带上。不带的话 uv 会静默回落到 PyPI，
#   那就不是离线安装了（方案 §28.2 的明确警告）。
uv pip install --python "$ROOT/.venv/bin/python" \
  --offline --no-index \
  --find-links "$BUNDLE/wheels" \
  -r "$ROOT/requirements.txt"

echo "[3/8] llama-server 与模型权重"
cp "$BUNDLE/llama/llama-server" "$ROOT/runtime/"
chmod +x "$ROOT/runtime/llama-server"
mkdir -p "$ROOT/models/hy-mt1.5"
cp -R "$BUNDLE/models/hy-mt1.5/." "$ROOT/models/hy-mt1.5/"

echo "[4/8] Docling 模型产物"
# 放 $ROOT/models/docling，而不是 ~/.cache/docling —— 两个理由：
#   1. ~/.cache 语义上是「可以随时删掉重建」的，而这份产物在断网机上重建不了；
#   2. docling 的 artifacts_path 要的是「一个模型一个 <org>--<repo> 子目录」的目录，
#      不是它的 cache 根目录。把这两者搞混正是 tests/dev-mac/test.log 那次失败
#      的直接原因（Available models in ~/.cache/docling: RapidOcr）。
[ -n "$(ls -A "$BUNDLE/docling" 2>/dev/null || true)" ] || {
  echo "$BUNDLE/docling 是空的 —— 请在联网机上重跑 prepare-bundle.sh 第 4 步（§28.1）" >&2
  exit 1
}
cp -R "$BUNDLE/docling/." "$ROOT/models/docling/"
echo "    artifacts: $ROOT/models/docling"
ls -1 "$ROOT/models/docling"

echo "[5/8] 中文字体"
mkdir -p "$ROOT/fonts"
if compgen -G "$BUNDLE/fonts/*" > /dev/null; then
  cp "$BUNDLE"/fonts/* "$ROOT/fonts/"
  mkdir -p "$HOME/Library/Fonts"
  cp "$BUNDLE"/fonts/* "$HOME/Library/Fonts/"
else
  echo "⚠ $BUNDLE/fonts/ 是空的 —— 中文 PDF 渲染会失败（§15），请补 .ttf/.ttc" >&2
fi

echo "[6/8] 渲染 launchd plist"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS"
for name in llama api; do
  sed -e "s|@@INSTALL_ROOT@@|$ROOT|g" -e "s|@@MODEL_FILE@@|$MODEL_FILE|g" \
    "$REPO_DIR/deploy/launchd/com.offline-translator.$name.plist" \
    > "$AGENTS/com.offline-translator.$name.plist"
  # 注意别写成 `grep -q ... && { ...; }`：没命中时 grep 返回 1，
  # 整条 AND 列表跟着返回 1，set -e 会把脚本直接干掉 —— 而没命中恰恰是正常情况。
  if grep -q '@@' "$AGENTS/com.offline-translator.$name.plist"; then
    echo "$AGENTS/com.offline-translator.$name.plist 里还留着 @@ 占位符" >&2
    exit 1
  fi
done
[ -f "$ROOT/models/hy-mt1.5/$MODEL_FILE" ] || {
  echo "⚠ plist 指向的权重不存在：$ROOT/models/hy-mt1.5/$MODEL_FILE" >&2
  echo "  HF 仓库的文件名不总是默认值，请用 MODEL_FILE=<实际文件名> 重跑（§4.1）" >&2
  exit 1
}

echo "[7/8] 源目录与输出目录"
mkdir -p /Users/Shared/translator/{Source,Translated}

echo "[8/8] 离线自检：真跑一次 PDF 解析"
# 「模型有没有完整拷过来、配置有没有指对地方」必须在安装时就有结论。
#
# ⚠ 这里**必须走部署中的那份 config.yaml 和 app 自己的解析入口**，不能拿
#   $ROOT/models/docling 硬编码去验。第一版就是硬编码的，结果：重跑 install.sh
#   会保留旧 config.yaml（见第 1 步），旧值还指着 ~/.cache/docling；
#   自检验的是新目录、服务读的是旧配置，自检通过、跑第一份 PDF 照炸。
if [ "${SKIP_VERIFY:-0}" = "1" ]; then
  echo "    已按 SKIP_VERIFY=1 跳过 —— 请务必在 §28.5 断网验收时补上"
else
  [ -f "$BUNDLE/sample.pdf" ] || {
    echo "找不到 $BUNDLE/sample.pdf。" >&2
    echo "这说明 bundle 是旧版 prepare-bundle.sh 产出的 —— 那一版没有显式下载 Docling" >&2
    echo "模型，artifacts 很可能就是残的（症状：layout-heron 缺 preprocessor_config.json）。" >&2
    echo "请在联网机上重跑 scripts/prepare-bundle.sh（§28.1），再回来安装。" >&2
    exit 1
  }
  OFFLINE_TRANSLATOR_CONFIG="$ROOT/config/config.yaml" PYTHONPATH="$ROOT"   "$ROOT/.venv/bin/python" - "$BUNDLE/sample.pdf" <<'PY'
import os
import sys
from pathlib import Path

from app.core.config import Settings
from app.core.offline import apply_offline_guard

cfg = Settings.load(os.environ["OFFLINE_TRANSLATOR_CONFIG"])
# 与 app/main.py 同序：离线守卫必须在 import docling 之前写环境变量
apply_offline_guard(cfg.pdf.docling_artifacts_path)

from app.document.pdf_reader import parse_pdf  # noqa: E402

print(f"    artifacts: {cfg.pdf.docling_artifacts_path}")
parsed = parse_pdf(Path(sys.argv[1]), cfg)
print(f"    通过：离线解析出 {len(parsed.blocks)} 个 block")
PY
fi

echo
echo "安装完成。"
echo "下一步：核对 $ROOT/config/config.yaml（§4.3），再跑 scripts/start.sh 加载服务，"
echo "      最后按 §28.5 断网验收。"
echo "提醒：要满足「开机自动启动」还需开启自动登录（系统设置 → 用户与群组），见 §17.1。"
