#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 阶段一：联网备料（方案 §28.1）。只做一次。
#
# ⚠ 本脚本是**全仓唯一允许联网**的脚本 —— 它按 §28.1 就在能联网的机器上运行，
#   产出 offline-bundle/ 后整包拷到目标 Mac。其余脚本一律零联网。
#
# ⚠ 执行顺序上，§28.6 把「法务确认许可证（§3.3）」排在第 1 步，本脚本是第 2 步。
#   第 1 步未确认前不要跑这里 —— 备料和模型选型绑定，换模型意味着第 3 步全部重做。
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BUNDLE="${1:-offline-bundle}"
MODEL_REPO="${MODEL_REPO:-tencent/HY-MT1.5-1.8B-GGUF}"

mkdir -p "$BUNDLE"/{wheels,llama,models,fonts,docling,libreoffice}

echo "[1/6] Python 依赖 -> wheelhouse"
uv pip compile requirements.in -o requirements.txt
uv pip download -r requirements.txt -d "$BUNDLE/wheels"

echo "[2/6] llama.cpp（Apple Silicon arm64）"
if [ ! -d llama.cpp ]; then
  git clone --depth 1 https://github.com/ggml-org/llama.cpp
fi
( cd llama.cpp && cmake -B build && cmake --build build --config Release -j )
cp llama.cpp/build/bin/llama-server "$BUNDLE/llama/"

echo "[3/6] 模型权重（默认 1.8B，见 §3.2）"
hf download "$MODEL_REPO" --local-dir "$BUNDLE/models/hy-mt1.5"

echo "[4/6] Docling / OCR 模型产物"
# ⚠ 这一步最容易被漏。Docling、OCR 引擎普遍在首次运行时静默联网拉模型，
#   不提前固化就会在断网环境直接失败，而且报错通常不明显。
#   必须真跑一次完整解析，把缓存目录整个拷进 bundle。
python - <<'PY'
from pathlib import Path
from docling.document_converter import DocumentConverter
sample = Path("sample.pdf")
if not sample.is_file():
    raise SystemExit("请在当前目录放一份 sample.pdf（最好含扫描页，以便一并拉取 OCR 模型）")
DocumentConverter().convert(str(sample))
print("docling 模型已落盘")
PY
cp -R "$HOME/.cache/docling/." "$BUNDLE/docling/"

echo "[5/6] 中文字体"
# ⚠ ReportLab 只支持 TrueType 轮廓，**不支持 OTF(CFF)**。
#   Noto Sans CJK SC 最常见的发布形态恰好是 .otf —— 取它会在渲染时失败。
#   请放 NotoSansSC-Regular.ttf（最省事）或 NotoSansCJK-Regular.ttc。
echo "    请手动把 NotoSansSC-Regular.ttf 或 NotoSansCJK-Regular.ttc 放进 $BUNDLE/fonts/"

echo "[6/6] LibreOffice（.doc 转换，见 §5.1.1）"
echo "    请手动把 macOS arm64 的 LibreOffice dmg 放进 $BUNDLE/libreoffice/"

echo
echo "备料完成：$BUNDLE"
echo "下一步：整包拷到目标 Mac，跑 scripts/install.sh，再按 §28.5 断网验收。"
