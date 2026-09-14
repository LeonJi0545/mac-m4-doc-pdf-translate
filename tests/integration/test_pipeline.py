"""M2 集成测试：契约 C 的完整单文件流水线。

DOCX 与 PDF 两条链路都跑通，用 FakeProvider 代替真实模型。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import ParseError
from app.document.model import JobPhase
from app.jobs.pipeline import translate_file
from tests.conftest import FakeProvider
from tests.fixtures.fake_docling import FakeConverter, build_sample_document
from tests.unit.test_pdf_render import _available, requires_font


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_docx_pipeline_end_to_end(tmp_path: Path, tmp_settings: Settings,
                                   fake_provider: FakeProvider) -> None:
    from tests.fixtures.docx_factory import build_rich_docx, count_drawings

    src = build_rich_docx(tmp_path / "src.docx")
    dst = tmp_path / "out" / "src_zh.docx"
    before = _sha256(src)
    phases: list[JobPhase] = []

    result = translate_file(
        src, dst,
        provider=fake_provider,
        settings=tmp_settings,
        on_phase=phases.append,
    )

    assert result.status == "completed"
    assert dst.is_file()
    # 三个阶段按 §9 的顺序上报
    assert phases == [JobPhase.PARSING, JobPhase.TRANSLATING, JobPhase.RENDERING]
    # 原文件未被修改（方案 §8 / §22）
    assert _sha256(src) == before
    # 图片保留
    assert count_drawings(dst) == count_drawings(src)
    assert result.blocks_translated > 0
    assert fake_provider.calls, "provider 应当被调用"


def test_docx_pipeline_marks_phases_even_with_warnings(
    tmp_path: Path, tmp_settings: Settings
) -> None:
    """QA 告警不使文件失败（AC-2.12）。"""
    from tests.fixtures.docx_factory import build_simple_docx

    class DropsEverything(FakeProvider):
        def translate(self, text, source_language, target_language,
                      glossary=None, context=None):
            super().translate(text, source_language, target_language, glossary, context)
            return "译文"  # 数字、URL 全丢

    src = build_simple_docx(
        tmp_path / "s.docx",
        ["Invoice €1,250.00 due 2024-03-15 see https://x.io"],
    )
    dst = tmp_path / "s_zh.docx"
    result = translate_file(src, dst, provider=DropsEverything(), settings=tmp_settings)

    assert result.status == "completed"
    codes = {w.code for w in result.warnings}
    assert {"missing_currency", "missing_date", "missing_url"} <= codes


@requires_font
def test_pdf_pipeline_end_to_end(tmp_path: Path, tmp_settings: Settings,
                                  fake_provider: FakeProvider) -> None:
    from pypdf import PdfReader

    settings = tmp_settings.model_copy(deep=True)
    settings.fonts.cjk_candidates = _available()

    src = tmp_path / "report.pdf"
    src.write_bytes(b"%PDF-1.4 stub")  # 内容不重要，解析走注入的假 converter
    dst = tmp_path / "out" / "report_zh.pdf"
    phases: list[JobPhase] = []

    result = translate_file(
        src, dst,
        provider=fake_provider,
        settings=settings,
        on_phase=phases.append,
        pdf_converter=FakeConverter(build_sample_document()),
    )

    assert result.status == "completed"
    assert dst.is_file()
    assert phases == [JobPhase.PARSING, JobPhase.TRANSLATING, JobPhase.RENDERING]
    text = "\n".join(p.extract_text() or "" for p in PdfReader(str(dst)).pages)
    assert "[zh]" in text, "译文应当出现在输出 PDF 里"


def test_unsupported_suffix_rejected(tmp_path: Path, tmp_settings: Settings,
                                      fake_provider: FakeProvider) -> None:
    src = tmp_path / "notes.txt"
    src.write_text("hello", encoding="utf-8")
    with pytest.raises(ParseError, match="不支持的文件类型"):
        translate_file(src, tmp_path / "out.txt",
                       provider=fake_provider, settings=tmp_settings)


def test_output_directory_is_created(tmp_path: Path, tmp_settings: Settings,
                                      fake_provider: FakeProvider) -> None:
    from tests.fixtures.docx_factory import build_simple_docx

    src = build_simple_docx(tmp_path / "s.docx", ["one", "two"])
    dst = tmp_path / "deep" / "nested" / "s_zh.docx"
    translate_file(src, dst, provider=fake_provider, settings=tmp_settings)
    assert dst.is_file()


def test_target_language_reaches_provider(tmp_path: Path, tmp_settings: Settings,
                                           fake_provider: FakeProvider) -> None:
    from tests.fixtures.docx_factory import build_simple_docx

    src = build_simple_docx(tmp_path / "s.docx", ["你好世界"])
    translate_file(src, tmp_path / "s_en.docx", provider=fake_provider,
                   settings=tmp_settings, source_language="zh", target_language="en")
    assert fake_provider.calls[0]["target_language"] == "en"
    assert fake_provider.calls[0]["source_language"] == "zh"
