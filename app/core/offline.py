"""离线守卫（方案 §20 / §22）。

方案 §28.1 第 4 步特别警告：Docling、OCR 引擎这类库普遍在**首次运行某功能时才静默联网**
拉模型，不提前固化就会在断网环境直接失败，而且报错通常不明显。

这个模块把「静默联网」变成「启动期显式报错」：

1. ``apply_offline_guard()`` 设置各库的 offline 开关与 telemetry 禁用
   （必须在 import 这些库之前调用）
2. ``assert_local_model_path()`` 拒绝 ``-hf`` 形态的模型标识
3. ``assert_loopback()`` 拒绝非 loopback 的服务地址
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

from app.core.errors import OfflineViolation

# 这些环境变量必须在对应库被 import 之前写入 —— 很多库在模块导入期就读走了它们。
OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    "DO_NOT_TRACK": "1",
    "SCARF_NO_ANALYTICS": "true",
    "DOCLING_DISABLE_TELEMETRY": "1",
}

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

# Hugging Face repo id 形态：``org/name`` 或 ``org/name:QUANT``。
# 官方 model card 给的示例正是 ``-hf tencent/HY-MT1.5-7B-GGUF:Q8_0``，
# 而 ``-hf`` 会联网拉取（方案 §4.2），所以这种取值要在启动期就拦下来。
_HF_REPO_ID = re.compile(r"^[\w.-]+/[\w.-]+(:[\w.]+)?$")


def apply_offline_guard(artifacts_path: str | None = None) -> None:
    """写入离线环境变量。

    必须在 import docling / transformers / huggingface_hub 之前调用，
    因此 ``app/main.py`` 里它排在其余 import 之前（会触发 E402，是有意为之）。
    """
    for key, value in OFFLINE_ENV.items():
        os.environ.setdefault(key, value)
    if artifacts_path:
        os.environ.setdefault("DOCLING_ARTIFACTS_PATH", artifacts_path)


def assert_local_model_path(path: str) -> Path:
    """校验模型路径是本地存在的文件。

    方案 §26 的验收项「通过 llama.cpp 加载官方 GGUF 权重（本地路径，非 -hf）」
    就靠这一条在代码侧兜住。
    """
    if not path or not path.strip():
        raise OfflineViolation(
            "未配置模型路径。请在 config.yaml 的 model.path 填写本地 .gguf 文件的绝对路径。"
        )
    candidate = path.strip()
    if _HF_REPO_ID.match(candidate) and not Path(candidate).exists():
        raise OfflineViolation(
            f"model.path 看起来是 Hugging Face repo id（{candidate}），"
            "这是官方示例中 -hf 参数的写法，会联网拉取权重，违反方案 §20 的离线要求。"
            "请改为本地 .gguf 文件的绝对路径，例如 "
            "/Users/Shared/offline-translator/models/hy-mt1.5/HY-MT1.5-1.8B-Q4_K_M.gguf"
        )
    model_path = Path(candidate)
    if not model_path.is_file():
        raise OfflineViolation(
            f"模型文件不存在: {model_path}。"
            "本系统不会自动下载模型（方案 §20），请先按 §28.1 备料并按 §28.2 安装。"
        )
    return model_path


def assert_loopback(url: str, *, what: str = "服务地址") -> str:
    """校验 URL 指向 loopback。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise OfflineViolation(f"{what} 必须是 http(s) URL，实际为: {url}")
    host = parsed.hostname or ""
    if host not in LOOPBACK_HOSTS:
        raise OfflineViolation(
            f"{what} 必须指向本机 loopback（127.0.0.1 / localhost / ::1），实际为: {host}。"
            "方案 §22 要求所有服务只监听本机。"
        )
    return url
