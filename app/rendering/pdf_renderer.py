"""PDF 渲染（ReportLab Platypus）。

方案 §5.2 / §23：V1 **不要求**像素级还原。优先保证
页面顺序、标题、段落、表格、图片、基本布局。

两个必须注意的点：

1. ``Paragraph`` 会把内容当成类 HTML 标记解析，译文里出现 ``<`` ``&`` 会直接抛解析错误
   —— 所有文本进 Paragraph 前必须转义。
2. 中文必须设 ``wordWrap="CJK"``，否则整行不换行、直接溢出页面。
"""

from __future__ import annotations

from html import escape
from itertools import groupby
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.errors import RenderError
from app.core.logging import get_logger
from app.document.model import Block, BlockType, ParsedDocument
from app.rendering.fonts import register_cjk_font

log = get_logger(__name__)

_HEADING_SIZES = {1: 18, 2: 15, 3: 13, 4: 12}


def _escape(text: str) -> str:
    """转义后交给 Paragraph。

    ``escape`` 会处理 ``& < >``；引号保持原样，中文排版里引号很常见，
    转成实体反而影响可读性。
    """
    return escape(text, quote=False)


def _build_styles(font_name: str) -> dict[str, Any]:
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    base = getSampleStyleSheet()
    styles: dict[str, Any] = {}

    styles["body"] = ParagraphStyle(
        "CJKBody",
        parent=base["BodyText"],
        fontName=font_name,
        fontSize=10.5,
        leading=16,
        alignment=TA_LEFT,
        wordWrap="CJK",  # 没有这行中文不换行，整行溢出页面
        spaceAfter=6,
    )
    styles["caption"] = ParagraphStyle(
        "CJKCaption", parent=styles["body"], fontSize=9, textColor="#555555"
    )
    styles["list"] = ParagraphStyle(
        "CJKList", parent=styles["body"], leftIndent=16, bulletIndent=6
    )
    styles["cell"] = ParagraphStyle(
        "CJKCell", parent=styles["body"], fontSize=9.5, leading=13, spaceAfter=0
    )
    for level, size in _HEADING_SIZES.items():
        styles[f"h{level}"] = ParagraphStyle(
            f"CJKHeading{level}",
            parent=base["Heading1"],
            fontName=font_name,
            fontSize=size,
            leading=size + 5,
            spaceBefore=12 if level <= 2 else 8,
            spaceAfter=6,
            wordWrap="CJK",
        )
    return styles


def _heading_style(styles: dict[str, Any], level: int | None) -> Any:
    lvl = level if level in _HEADING_SIZES else 3
    return styles[f"h{lvl}"]


def _table_flowable(cells: list[Block], styles: dict[str, Any]) -> Any:
    """把同一个 table_id 的单元格重新聚合成 ReportLab Table。"""
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle

    grid: dict[tuple[int, int], str] = {}
    for cell in cells:
        if cell.table is None:
            continue
        grid[(cell.table.row, cell.table.col)] = cell.output_text

    if not grid:
        return None

    rows = max(r for r, _ in grid) + 1
    cols = max(c for _, c in grid) + 1
    data = [
        [Paragraph(_escape(grid.get((r, c), "")), styles["cell"]) for c in range(cols)]
        for r in range(rows)
    ]

    table = Table(data, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _flowables(parsed: ParsedDocument, styles: dict[str, Any]) -> list[Any]:
    from reportlab.platypus import PageBreak, Paragraph, Spacer

    flow: list[Any] = []
    current_page: int | None = None

    # 表格单元格要按 table_id 连续聚合，先把 block 序列按「是否属于同一个表」分组
    def table_key(block: Block) -> str | None:
        return block.table.table_id if block.table is not None else None

    for key, group_iter in groupby(parsed.blocks, key=table_key):
        group = list(group_iter)

        # 换页：block 的 page 变化时插 PageBreak，保证「页面顺序」这一 V1 要求
        first_page = next((b.page for b in group if b.page is not None), None)
        if first_page is not None and current_page is not None and first_page != current_page:
            flow.append(PageBreak())
        if first_page is not None:
            current_page = first_page

        if key is not None:
            table = _table_flowable(group, styles)
            if table is not None:
                flow.append(table)
                flow.append(Spacer(1, 8))
            continue

        for block in group:
            if block.type is BlockType.IMAGE:
                # V1 不重建原图（Docling 未必给出图像字节），留出空位并标注
                flow.append(Spacer(1, 10))
                flow.append(Paragraph(_escape("［图片］"), styles["caption"]))
                flow.append(Spacer(1, 10))
                continue

            text = block.output_text
            if not text.strip():
                continue

            if block.type is BlockType.HEADING:
                flow.append(Paragraph(_escape(text), _heading_style(styles, block.level)))
            elif block.type is BlockType.LIST_ITEM:
                flow.append(Paragraph(_escape(text), styles["list"], bulletText="•"))
            elif block.type is BlockType.CAPTION:
                flow.append(Paragraph(_escape(text), styles["caption"]))
            else:
                flow.append(Paragraph(_escape(text), styles["body"]))

    return flow


def render_pdf(parsed: ParsedDocument, output: Path, settings: Settings) -> None:
    """把 Block 序列排版成新的 PDF。"""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate

    font = register_cjk_font(settings.fonts.cjk_candidates or None)
    styles = _build_styles(font.name)

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".partial")

    try:
        doc = SimpleDocTemplate(
            str(tmp),
            pagesize=A4,
            leftMargin=20 * mm,
            rightMargin=20 * mm,
            topMargin=18 * mm,
            bottomMargin=18 * mm,
            title=output.stem,
        )
        flow = _flowables(parsed, styles)
        if not flow:
            from reportlab.platypus import Paragraph

            flow = [Paragraph(_escape("（本文档没有可输出的内容）"), styles["body"])]
        doc.build(flow)
        tmp.replace(output)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        if isinstance(exc, RenderError):
            raise
        raise RenderError(f"PDF 渲染失败 ({output}): {exc}") from exc
