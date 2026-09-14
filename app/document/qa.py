"""QA 校验（方案 §14）。

检查项：数字 / URL / Email / 货币 / 日期 / 变量占位符 的一致性，
以及 DOCX 的段落-表格-标题-图片计数、PDF 的页数-空白页-字体嵌入。

两条设计取舍：

1. **只报「源有译文无」**（丢失），不报译文多出的内容。中文表达常会多出字词，
   反向比对误报率高到没有参考价值。
2. **告警不使文件失败**。方案 §14 只要求「检查」，V1 不做 AI 自动质量评分，
   所以告警随结果上报、由人判断。
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from app.core.logging import get_logger
from app.document.model import Block, ParsedDocument, QAWarning

log = get_logger(__name__)

# 抽取顺序有讲究：先抽结构化的（URL / Email / 日期 / 货币），把命中区段从文本里挖掉，
# 剩下的才按裸数字抽 —— 否则 "2024-03-15" 会被拆成三个数字重复报。
_URL = re.compile(r"https?://[^\s，。、）)\]】]+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_DATE = re.compile(
    r"\b("
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"      # 2024-03-15
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"   # 15.03.2024（德/意常用）
    r")\b"
)
_CURRENCY = re.compile(
    r"([€$£¥]\s?\d[\d.,]*)"
    r"|(\b\d[\d.,]*\s?(?:EUR|USD|GBP|CNY|RMB|CHF)\b)"
    r"|(\b(?:EUR|USD|GBP|CNY|RMB|CHF)\s?\d[\d.,]*)",
    re.I,
)
_VARIABLE = re.compile(r"(\{[^{}\s]*\}|\$\{[^}]*\}|%[sdif]\b)")
_NUMBER = re.compile(r"\d[\d.,]*\d|\d")


def _extract(text: str) -> dict[str, Counter[str]]:
    """按类别抽取需要保真的片段。"""
    remaining = text
    found: dict[str, Counter[str]] = {}

    for key, pattern in (
        ("url", _URL),
        ("email", _EMAIL),
        ("variable", _VARIABLE),
        ("currency", _CURRENCY),
        ("date", _DATE),
    ):
        hits: list[str] = []
        for m in pattern.finditer(remaining):
            hits.append(m.group(0).strip())
        found[key] = Counter(hits)
        remaining = pattern.sub(" ", remaining)

    # 剩余文本里的裸数字。去掉千分位分隔符再比，规避 1,250.00 与 1250.00 的表达差异。
    found["number"] = Counter(
        m.group(0).replace(",", "").rstrip(".") for m in _NUMBER.finditer(remaining)
    )
    return found


_LABELS = {
    "url": "URL",
    "email": "Email",
    "variable": "变量占位符",
    "currency": "金额",
    "date": "日期",
    "number": "数字",
}


def check_block(block: Block) -> list[QAWarning]:
    """比对单个 Block 的原文与译文。"""
    if block.translated is None or not block.translatable:
        return []

    src = _extract(block.text)
    dst = _extract(block.translated)
    warnings: list[QAWarning] = []

    for key, src_counter in src.items():
        missing = src_counter - dst[key]
        if missing:
            items = ", ".join(sorted(missing.elements()))
            warnings.append(
                QAWarning(
                    code=f"missing_{key}",
                    detail=f"译文中缺失{_LABELS[key]}: {items}",
                    block_id=block.id,
                )
            )
    return warnings


def check_content(parsed: ParsedDocument) -> list[QAWarning]:
    warnings: list[QAWarning] = []
    for block in parsed.blocks:
        warnings.extend(check_block(block))
    return warnings


def check_docx_structure(source: Path, output: Path) -> list[QAWarning]:
    """段落 / 表格 / 标题 / 图片计数比对（方案 §14）。"""
    from app.document.docx_reader import count_structure

    try:
        before = count_structure(source)
        after = count_structure(output)
    except Exception as exc:  # noqa: BLE001 —— QA 失败不该让文件失败
        return [QAWarning(code="docx_structure_check_failed", detail=str(exc))]

    labels = {
        "paragraphs": "段落数",
        "tables": "表格数",
        "headings": "标题数",
        "images": "图片数",
    }
    return [
        QAWarning(
            code=f"docx_{key}_mismatch",
            detail=f"{labels[key]}不一致: 源 {before[key]} → 输出 {after[key]}",
        )
        for key in labels
        if before[key] != after[key]
    ]


def check_pdf_output(output: Path, font_name: str | None = None) -> list[QAWarning]:
    """页数 / 空白页 / 字体嵌入检查（方案 §14）。"""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return [QAWarning(code="pdf_check_skipped", detail="未安装 pypdf，跳过 PDF 检查")]

    try:
        reader = PdfReader(str(output))
    except Exception as exc:  # noqa: BLE001
        return [QAWarning(code="pdf_unreadable", detail=f"输出 PDF 无法读取: {exc}")]

    warnings: list[QAWarning] = []
    page_count = len(reader.pages)
    if page_count == 0:
        warnings.append(QAWarning(code="pdf_empty", detail="输出 PDF 没有任何页面"))
        return warnings

    blank_pages: list[int] = []
    embedded: set[str] = set()
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            text = ""
        if not text.strip():
            blank_pages.append(i)
        try:
            fonts = page.get("/Resources", {}).get("/Font", {})
            for ref in fonts.values():
                obj = ref.get_object()
                base = str(obj.get("/BaseFont", ""))
                if base:
                    embedded.add(base.lstrip("/"))
        except Exception:  # noqa: BLE001 —— 字体检查失败不该让 QA 崩
            continue

    if blank_pages:
        warnings.append(
            QAWarning(
                code="pdf_blank_page",
                detail=f"存在空白页（第 {', '.join(map(str, blank_pages))} 页）",
            )
        )

    if font_name and not any(font_name in f for f in embedded):
        warnings.append(
            QAWarning(
                code="pdf_font_not_embedded",
                detail=(
                    f"未在输出 PDF 中检出中文字体 {font_name}"
                    f"（已检出: {', '.join(sorted(embedded)) or '无'}）。"
                    "中文很可能显示为空白或豆腐块。"
                ),
            )
        )

    return warnings
