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
# 锁文件必须落进 bundle —— install.sh 在离线机上从 bundle 读它。
# 只编译到仓库根目录的话，离线机上就没有这个文件，安装会直接失败。
uv pip compile requirements.in -o "$BUNDLE/requirements.txt"
pip3 download -r "$BUNDLE/requirements.txt" -d "$BUNDLE/wheels"

echo "[2/6] llama.cpp（Apple Silicon arm64）"
if [ ! -d llama.cpp ]; then
  git clone --depth 1 https://github.com/ggml-org/llama.cpp
fi
( cd llama.cpp && cmake -B build && cmake --build build --config Release -j )
cp llama.cpp/build/bin/llama-server "$BUNDLE/llama/"

echo "[3/6] 模型权重（默认 1.8B，见 §3.2）"
hf download "$MODEL_REPO" --local-dir "$BUNDLE/models/hy-mt1.5"

echo "[4/6] Docling / OCR 模型产物"
# ─────────────────────────────────────────────────────────────────────────────
# ⚠ 这一步最容易被漏。Docling、OCR 引擎普遍在首次运行时**静默联网**拉模型，
#   不提前固化就会在断网环境直接失败，而且报错通常不明显。
#
# ⚠ 这里翻过一次车，见 tests/dev-mac/test.log：
#     ParseError: Model 'docling-project/docling-layout-heron' not found in
#     artifacts_path. Expected location: ~/.cache/docling/docling-project--docling-layout-heron
#     Available models in ~/.cache/docling: RapidOcr
#
#   原因是旧写法「不带 artifacts_path 跑一次 DocumentConverter，再把 ~/.cache/docling
#   整个拷走」：不带 artifacts_path 时 docling 走的是 Hugging Face 自己的缓存
#   （~/.cache/huggingface/hub），落进 ~/.cache/docling 的只有 OCR 引擎那一份。
#   于是 bundle 里缺了 layout / tableformer，到了断网的 B 机才炸。
#
#   现在改成两段式，把「碰运气捡缓存」换成「显式下载 + 断网验收」：
#     ① 用 docling 自己的下载器把标准模型集**显式**落到 $BUNDLE/docling；
#        产物形态就是 artifacts 目录该有的样子 —— 每个模型一个 <org>--<repo> 子目录。
#     ② 带 artifacts_path + HF 离线开关真跑一次解析。缺任何一个模型都在 A 机当场炸，
#        而不是等 B 机断网后才暴露。
# ─────────────────────────────────────────────────────────────────────────────
ARTIFACTS="$BUNDLE/docling"
mkdir -p "$ARTIFACTS"

echo "    ① 显式下载模型集 -> $ARTIFACTS"
if command -v docling-tools >/dev/null 2>&1; then
  docling-tools models download -o "$ARTIFACTS"
  # OCR 引擎在哪些版本进默认集、叫什么名字都会变。取不到就跳过，
  # 真正的判据是 ② 的断网验收，不是这里的返回码。
  for engine in easyocr rapidocr; do
    docling-tools models download -o "$ARTIFACTS" "$engine" >/dev/null 2>&1 \
      || echo "       （本版 docling 不认识模型名 $engine，跳过）"
  done
else
  python3 - "$ARTIFACTS" <<'PY'
import sys
from pathlib import Path

from docling.utils.model_downloader import download_models

download_models(output_dir=Path(sys.argv[1]), progress=True)
PY
fi

# snapshot_download(local_dir=...) 写的是真实文件。但万一落进来的是 HF 缓存那种
# 符号链接树，cp -R 到 B 机之后就是一堆断链 —— 症状正是 test.log 第二条：
#   Image processor config not found: .../preprocessor_config.json
# 目录在、文件不在。宁可在这里炸。
links="$(find "$ARTIFACTS" -type l)"
if [ -n "$links" ]; then
  echo "⚠ $ARTIFACTS 内含符号链接，拷到离线机后会断链：" >&2
  echo "$links" >&2
  exit 1
fi

# 形状自检：每个 <org>--<repo> 目录底下必须是平铺的模型文件，
# 而不是 HF 的 blobs/refs/snapshots 对象存储。后者是 `download-hf-repo` 的产出，
# docling 按平铺路径去找会报 "Image processor config not found"，看着像文件损坏。
for repo_dir in "$ARTIFACTS"/*/; do
  if [ -d "$repo_dir/blobs" ] && [ -d "$repo_dir/snapshots" ]; then
    echo "⚠ $repo_dir 是 HF 缓存布局（blobs/refs/snapshots），不是平铺快照。" >&2
    echo '  请确认用的是 docling-tools models download -o，而不是 download-hf-repo。' >&2
    exit 1
  fi
done

echo "    ② 断网语义下真跑一次解析（验收）"
# ⚠ sample.pdf **必须含扫描页**，否则 OCR 引擎的模型不会被触发，
#   B 机处理扫描件时才会失败。
DOCLING_ARTIFACTS_PATH="$ARTIFACTS" \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
python3 - "$ARTIFACTS" <<'PY'
import sys
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

artifacts = Path(sys.argv[1])
sample = Path("sample.pdf")
if not sample.is_file():
    raise SystemExit("请在当前目录放一份 sample.pdf（必须含扫描页，以便一并拉取 OCR 模型）")

# 与 config.yaml 的 pdf.* 保持一致 —— 验收要验的是 B 机实际会跑的那条链路。
options = PdfPipelineOptions()
options.do_ocr = True
options.do_table_structure = True
options.artifacts_path = str(artifacts)

result = DocumentConverter(
    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
).convert(str(sample))
print(f"docling 离线解析通过：{len(list(result.document.iterate_items()))} 个 item")
PY

# 把 sample.pdf 一并带上：install.sh 会在 B 机上拿它再做一次离线自检，
# 这样「模型有没有完整过来」在安装时就有结论，不用等第一份真实文档。
cp sample.pdf "$BUNDLE/sample.pdf"

echo "[5/6] 中文字体"
# ⚠ ReportLab 只支持 TrueType 轮廓，**不支持 OTF(CFF)**。
#   Noto Sans CJK SC 最常见的发布形态恰好是 .otf —— 取它会在渲染时失败。
#   请放 NotoSansSC-Regular.ttf（最省事）或 NotoSansCJK-Regular.ttc。
echo "    请手动把 NotoSansSC-Regular.ttf 或 NotoSansCJK-Regular.ttc 放进 $BUNDLE/fonts/"

echo "[6/6] LibreOffice（.doc 转换，见 §5.1.1）"
echo "    请手动把 macOS arm64 的 LibreOffice dmg 放进 $BUNDLE/libreoffice/"

echo
echo "备料完成：$BUNDLE"
echo "docling artifacts 子目录："
ls -1 "$ARTIFACTS"
echo
echo "下一步：整包拷到目标 Mac，跑 scripts/install.sh，再按 §28.5 断网验收。"
