"""DOCX 遍历的**唯一**实现。

reader 与 writer 共用这里的遍历顺序 —— 两边各写一套导航逻辑是 DOCX 写回最常见的
错误来源（顺序差一个，整篇译文就错位了）。所以顺序只在这里定义一次，
Block 的 anchor 只记一个 key，writer 用同样的遍历重建 key → 段落 的映射。

实测结论（python-docx 1.2.0）：

- ``paragraph.text`` **包含**超链接内的文字
- ``paragraph.runs`` **不包含**超链接内的 run（它只取 ``w:p`` 的直接 ``w:r`` 子元素）

所以写回必须走 ``.//w:r`` 的 XPath，否则链接文字既不会被翻译、又会和译文重复。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from docx.oxml.ns import qn

# 图片可能以两种形态存在：
#   w:drawing —— OOXML 的标准内嵌图形
#   w:pict    —— 旧版 VML 图片，**.doc 转换来的文件里很常见**
# 漏判 w:pict 会导致 .doc 链路把图片清掉。
_IMAGE_TAGS = (qn("w:drawing"), qn("w:pict"), qn("w:object"))


@dataclass
class ParagraphParts:
    """一个段落拆成「正文 run」与「若干超链接」。

    超链接单独成组，是为了让写回时能做到：正文译文写进正文 run、链接文字译文写进
    链接内部的 run —— 既不会把整段变成链接，也不会留下空链接，URL 更是全程没被碰过
    （它存在 relationship 里，不在 run 上）。
    """

    text_runs: list[Any] = field(default_factory=list)
    hyperlink_runs: list[list[Any]] = field(default_factory=list)


def run_has_image(r_el: Any) -> bool:
    """该 run 是否承载图片 / 嵌入对象。这类 run 一律不许改写。"""
    return any(r_el.findall(tag) for tag in _IMAGE_TAGS)


def run_text(r_el: Any) -> str:
    return "".join(t.text or "" for t in r_el.findall(qn("w:t")))


def runs_text(runs: list[Any]) -> str:
    return "".join(run_text(r) for r in runs)


def set_run_text(r_el: Any, text: str) -> None:
    """把整段文字塞进这个 run 的第一个 w:t，其余 w:t 清空。"""
    t_els = r_el.findall(qn("w:t"))
    if not t_els:
        return
    first = t_els[0]
    first.text = text
    # 保留空白，否则 Word 会吃掉首尾空格
    first.set(qn("xml:space"), "preserve")
    for extra in t_els[1:]:
        extra.text = ""


def clear_run_text(r_el: Any) -> None:
    for t in r_el.findall(qn("w:t")):
        t.text = ""


def split_paragraph(paragraph: Any) -> ParagraphParts:
    """把段落拆成正文 run 与超链接 run 组，保持文档顺序。"""
    parts = ParagraphParts()
    for child in paragraph._p:
        if child.tag == qn("w:r"):
            parts.text_runs.append(child)
        elif child.tag == qn("w:hyperlink"):
            inner = child.findall(qn("w:r"))
            if inner:
                parts.hyperlink_runs.append(inner)
    return parts


def _iter_block_paragraphs(container: Any, prefix: str) -> Iterator[tuple[str, Any]]:
    """递归遍历一个容器（document.body / cell / header / footer）里的段落与表格。"""
    for i, paragraph in enumerate(container.paragraphs):
        yield f"{prefix}/p{i}", paragraph
    for t_idx, table in enumerate(container.tables):
        yield from _iter_table(table, f"{prefix}/t{t_idx}")


def _iter_table(table: Any, prefix: str) -> Iterator[tuple[str, Any]]:
    for r_idx, row in enumerate(table.rows):
        for c_idx, cell in enumerate(row.cells):
            # 合并单元格在多个坐标上重复出现，用 key 去重靠调用方（dict 覆盖即可）
            yield from _iter_block_paragraphs(cell, f"{prefix}/r{r_idx}c{c_idx}")


def iter_paragraph_entries(document: Any) -> Iterator[tuple[str, Any]]:
    """按确定顺序产出 ``(key, paragraph)``。

    顺序：正文 → 每个 section 的页眉页脚。key 在同一份文档里稳定可复现。
    """
    yield from _iter_block_paragraphs(document, "body")

    for s_idx, section in enumerate(document.sections):
        for part_name in (
            "header",
            "footer",
            "first_page_header",
            "first_page_footer",
            "even_page_header",
            "even_page_footer",
        ):
            part = getattr(section, part_name, None)
            if part is None:
                continue
            # is_linked_to_previous 的页眉页脚内容来自上一节，重复遍历会写两遍
            if getattr(part, "is_linked_to_previous", False):
                continue
            yield from _iter_block_paragraphs(part, f"s{s_idx}/{part_name}")


def table_coords(key: str) -> tuple[str, int, int] | None:
    """从 key 里解析出 (表格 id, 行, 列)，不是表格单元格则返回 None。"""
    if "/t" not in key:
        return None
    head, _, tail = key.rpartition("/r")
    if not tail or "c" not in tail:
        return None
    row_str, _, rest = tail.partition("c")
    col_str = rest.split("/")[0]
    try:
        return head, int(row_str), int(col_str)
    except ValueError:
        return None
