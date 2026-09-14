"""中文字体注册（方案 §15）。

**这里有一个必踩的坑**：ReportLab 的 ``TTFont`` 只支持 TrueType 轮廓，
**不支持 OTF/CFF**。而 "Noto Sans CJK SC" 最常见的官方发布形态恰恰是 ``.otf`` ——
直接喂进去会失败。可用的形态是：

- ``NotoSansSC-Regular.ttf``（Noto Sans SC，非 CJK 合集版）—— 最省事
- ``NotoSansCJK-Regular.ttc``（TrueType Collection）—— 需要 ``subfontIndex``

所以候选表优先 .ttf、其次 .ttc，全部失败时抛出**带可操作指引**的错误，
而不是静默回退成豆腐块。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.errors import RenderError
from app.core.logging import get_logger

log = get_logger(__name__)

# config.yaml 没配时的兜底候选（macOS 常见位置）
DEFAULT_CANDIDATES: list[dict[str, Any]] = [
    {"name": "NotoSansSC", "path": "~/Library/Fonts/NotoSansSC-Regular.ttf"},
    {"name": "NotoSansSC", "path": "/Library/Fonts/NotoSansSC-Regular.ttf"},
    {"name": "NotoSansCJKsc", "path": "~/Library/Fonts/NotoSansCJK-Regular.ttc",
     "subfont_index": 0},
    {"name": "NotoSerifCJKsc", "path": "~/Library/Fonts/NotoSerifCJK-Regular.ttc",
     "subfont_index": 0},
    {"name": "PingFangSC", "path": "/System/Library/Fonts/PingFang.ttc", "subfont_index": 0},
]


@dataclass(frozen=True)
class RegisteredFont:
    name: str
    path: Path


def register_cjk_font(candidates: list[dict[str, Any]] | None = None) -> RegisteredFont:
    """按候选表依次尝试注册中文字体，返回第一个成功的。

    Raises:
        RenderError: 全部候选都不可用。错误信息里逐条列出尝试过的路径与原因，
            并明确指出需要 TTF/TTC 而非 OTF。
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    entries = candidates if candidates else DEFAULT_CANDIDATES
    tried: list[str] = []

    for entry in entries:
        raw_path = str(entry.get("path", ""))
        name = str(entry.get("name") or Path(raw_path).stem or "CJKFont")
        path = Path(raw_path).expanduser()

        if not path.is_file():
            tried.append(f"{path} → 文件不存在")
            continue

        if path.suffix.lower() == ".otf":
            tried.append(
                f"{path} → OTF(CFF) 轮廓，ReportLab 的 TTFont 不支持；请改用 TTF 或 TTC"
            )
            continue

        kwargs: dict[str, Any] = {}
        if "subfont_index" in entry:
            kwargs["subfontIndex"] = int(entry["subfont_index"])

        try:
            pdfmetrics.registerFont(TTFont(name, str(path), **kwargs))
        except Exception as exc:  # ReportLab 抛的类型很杂
            tried.append(f"{path} → 注册失败: {exc}")
            continue

        log.info("已注册中文字体 %s (%s)", name, path)
        return RegisteredFont(name=name, path=path)

    detail = "\n".join(f"  - {t}" for t in tried) or "  - （候选表为空）"
    raise RenderError(
        "找不到可用的中文字体，无法生成中文 PDF（方案 §15 要求显式指定中文字体）。\n"
        f"已尝试：\n{detail}\n"
        "要点：ReportLab 只支持 TrueType 轮廓，**不支持 OTF(CFF)**。\n"
        "而 Noto Sans CJK SC 最常见的发布形态正是 .otf —— 备料时（§28.1 第 5 步）\n"
        "请取以下任一种，并在 config.yaml 的 fonts.cjk_candidates 里填好路径：\n"
        "  · NotoSansSC-Regular.ttf（Noto Sans SC 静态 TTF，最省事）\n"
        "  · NotoSansCJK-Regular.ttc（TTC 合集，需同时配 subfont_index）"
    )


def is_font_available(candidates: list[dict[str, Any]] | None = None) -> bool:
    """探测是否有可用中文字体。测试用来决定 skip。"""
    entries = candidates if candidates else DEFAULT_CANDIDATES
    return any(
        Path(str(e.get("path", ""))).expanduser().is_file()
        and Path(str(e.get("path", ""))).suffix.lower() != ".otf"
        for e in entries
    )
