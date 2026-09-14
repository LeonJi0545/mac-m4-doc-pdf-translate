"""DOCX 写回（方案 §5.1）。

**原地改 run 的文本**，不重建文档 —— 这样段落样式、标题级别、表格结构、
页眉页脚、列表编号、图片、超链接 relationship 全部原样保留。

已知取舍：段落**内部**的 run 级差异格式（比如一句话里只有两个词是粗体）会统一成
段首 run 的格式。原因是译文与原文的 run 边界不可能对齐（中文与德语词序完全不同），
强行保留只会产生错位的粗体，比统一更糟。方案 §5.1 只要求保留「基础样式」，
这个取舍在要求之内。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from docx import Document

from app.core.errors import RenderError
from app.document.docx_walk import (
    clear_run_text,
    iter_paragraph_entries,
    run_has_image,
    set_run_text,
    split_paragraph,
)
from app.document.model import Block, ParsedDocument


def _apply_to_runs(runs: list[Any], text: str) -> None:
    """把 ``text`` 写进这组 run 的第一个可写 run，其余清空。

    含图片的 run 一个都不碰 —— 清空它会把图片一起删掉。
    """
    writable = [r for r in runs if not run_has_image(r)]
    if not writable:
        return
    set_run_text(writable[0], text)
    for extra in writable[1:]:
        clear_run_text(extra)


def write_docx(parsed: ParsedDocument, source: Path, output: Path) -> None:
    """把译文写回文档结构并另存到 ``output``。

    做法是先把源文件复制一份再在副本上改 —— 保证**原文件永远不修改**（方案 §8 / §22）。
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    # 先落到临时文件，成功后再原子替换，避免半成品占位导致下次扫描误判为「已存在」
    tmp = output.with_name(output.name + ".partial")
    try:
        shutil.copyfile(source, tmp)
        document = Document(str(tmp))

        # 用与 reader 完全相同的遍历重建 key → 段落 映射
        by_key = {}
        for key, paragraph in iter_paragraph_entries(document):
            by_key.setdefault(key, paragraph)

        for block in parsed.blocks:
            anchor = block.anchor
            if anchor.get("kind") != "docx":
                continue
            paragraph = by_key.get(anchor.get("key"))
            if paragraph is None:
                continue
            parts = split_paragraph(paragraph)
            text = block.output_text

            if anchor.get("part") == "main":
                _apply_to_runs(parts.text_runs, text)
            elif anchor.get("part") == "hyperlink":
                idx = anchor.get("index", 0)
                if 0 <= idx < len(parts.hyperlink_runs):
                    _apply_to_runs(parts.hyperlink_runs[idx], text)

        document.save(str(tmp))
        tmp.replace(output)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        if isinstance(exc, RenderError):
            raise
        raise RenderError(f"DOCX 写回失败 ({output}): {exc}") from exc


def apply_translations(parsed: ParsedDocument, translations: dict[str, str]) -> None:
    """按 block id 回填译文。"""
    index: dict[str, Block] = {b.id: b for b in parsed.blocks}
    for block_id, text in translations.items():
        block = index.get(block_id)
        if block is not None:
            block.translated = text
