"""单文件流水线 —— M2 与 M3 的接缝（父任务 design 的契约 C）。

对上层（Job runner）完全屏蔽文档格式差异：runner 只管拿到 (源, 目标) 对，
调用这里，收一个 ``FileResult``。

流程：``转换 → 解析 → chunk → 翻译 → 写回/渲染 → QA``
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.core.config import Settings
from app.core.errors import ParseError
from app.core.logging import get_logger
from app.document.chunking import translate_blocks
from app.document.doc_converter import DocConverter
from app.document.model import FileResult, JobPhase, ParsedDocument, QAWarning
from app.document.qa import check_content, check_docx_structure, check_pdf_output
from app.translation.base import TranslationProvider

log = get_logger(__name__)

DOCX_SUFFIXES = {".docx"}
DOC_SUFFIXES = {".doc"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = DOC_SUFFIXES | DOCX_SUFFIXES | PDF_SUFFIXES

PhaseCallback = Callable[[JobPhase], None]


def _noop(_: JobPhase) -> None:
    return None


def translate_file(
    src: Path,
    dst: Path,
    *,
    provider: TranslationProvider,
    settings: Settings,
    source_language: str = "auto",
    target_language: str = "zh",
    on_phase: PhaseCallback | None = None,
    pdf_converter: object | None = None,
) -> FileResult:
    """翻译一个文件。

    任何异常都向上抛 —— 由 Job runner 捕获成**单文件** failed，不影响其他文件
    （方案 §26「失败文件不影响其他文件」）。

    Args:
        pdf_converter: Docling converter 的注入点，仅测试用。
    """
    phase = on_phase or _noop
    suffix = src.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ParseError(f"不支持的文件类型: {src.suffix}（只处理 .doc/.docx/.pdf）")

    warnings: list[QAWarning] = []

    # ── 解析 ──────────────────────────────────────────────────────────────
    phase(JobPhase.PARSING)
    real_src = src
    if suffix in DOC_SUFFIXES:
        temp_dir = Path(settings.paths.temp_dir)
        real_src = DocConverter(settings.doc_conversion).convert(src, temp_dir)

    if suffix in PDF_SUFFIXES:
        from app.document.pdf_reader import parse_pdf

        parsed: ParsedDocument = parse_pdf(src, settings, converter=pdf_converter)
    else:
        from app.document.docx_reader import parse_docx

        parsed = parse_docx(real_src)

    translatable = parsed.translatable_blocks()

    # ── 翻译 ──────────────────────────────────────────────────────────────
    phase(JobPhase.TRANSLATING)
    warnings.extend(
        translate_blocks(
            parsed.blocks,
            provider,
            source_language=source_language,
            target_language=target_language,
            max_chars=settings.chunking.max_chars,
        )
    )

    # ── 写回 / 渲染 ───────────────────────────────────────────────────────
    phase(JobPhase.RENDERING)
    if parsed.kind == "docx":
        from app.document.docx_writer import write_docx

        write_docx(parsed, real_src, dst)
    else:
        from app.rendering.pdf_renderer import render_pdf

        render_pdf(parsed, dst, settings)

    # ── QA ────────────────────────────────────────────────────────────────
    warnings.extend(check_content(parsed))
    if parsed.kind == "docx":
        warnings.extend(check_docx_structure(real_src, dst))
    else:
        font_name = None
        candidates = settings.fonts.cjk_candidates
        if candidates:
            font_name = str(candidates[0].get("name") or "") or None
        warnings.extend(check_pdf_output(dst, font_name))

    if warnings:
        log.info("%s 完成，%d 条 QA 告警", src.name, len(warnings))

    return FileResult(
        source=src,
        output=dst,
        status="completed",
        warnings=warnings,
        blocks_total=len(parsed.blocks),
        blocks_translated=sum(1 for b in translatable if b.translated is not None),
    )
