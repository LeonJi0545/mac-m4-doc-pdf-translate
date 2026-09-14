"""M2 测试：字体解析与 PDF 渲染（AC-2.8 / AC-2.9）。

字体候选按平台探测：开发机（Windows）用系统自带的 CJK 字体，
目标机（macOS）用 Noto / PingFang。找不到任何可用字体时整组 skip，
但**字体缺失的报错信息**单独测 —— 那条路在哪个平台都要能走通。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfReader

from app.core.config import Settings
from app.core.errors import RenderError
from app.document.model import Block, BlockType, ParsedDocument, TableRef
from app.document.qa import check_pdf_output
from app.rendering.fonts import register_cjk_font
from app.rendering.pdf_renderer import render_pdf

# 各平台上可能存在的 TTF / TTC（**不含 OTF** —— ReportLab 不支持 CFF 轮廓）
_PROBE = [
    {"name": "NotoSansSC", "path": "C:/Windows/Fonts/NotoSansSC-VF.ttf"},
    {"name": "MicrosoftYaHei", "path": "C:/Windows/Fonts/msyh.ttc", "subfont_index": 0},
    {"name": "SimHei", "path": "C:/Windows/Fonts/simhei.ttf"},
    {"name": "NotoSansSC", "path": "/Library/Fonts/NotoSansSC-Regular.ttf"},
    {"name": "PingFangSC", "path": "/System/Library/Fonts/PingFang.ttc", "subfont_index": 0},
    {"name": "NotoSansCJK", "path": "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
     "subfont_index": 0},
]


def _available() -> list[dict]:
    return [c for c in _PROBE if Path(c["path"]).is_file()]


requires_font = pytest.mark.skipif(not _available(), reason="本机没有可用的 CJK TTF/TTC")


def _sample_doc() -> ParsedDocument:
    return ParsedDocument(
        source_path=Path("src.pdf"),
        kind="pdf",
        blocks=[
            Block(id="h1", type=BlockType.HEADING, text="季度运营报告", level=1, page=1),
            Block(id="p1", type=BlockType.PARAGRAPH,
                  text="本季度发票合计 €1,250.00，到期日为 2024-03-15。", page=1),
            Block(id="l1", type=BlockType.LIST_ITEM, text="第一条要点", page=1),
            Block(id="img", type=BlockType.IMAGE, text="", page=1),
            Block(id="cap", type=BlockType.CAPTION, text="图 1 收入分布", page=1),
            Block(id="c0", type=BlockType.TABLE_CELL, text="地区",
                  table=TableRef("t0", 0, 0), page=2),
            Block(id="c1", type=BlockType.TABLE_CELL, text="收入",
                  table=TableRef("t0", 0, 1), page=2),
            Block(id="c2", type=BlockType.TABLE_CELL, text="伦巴第",
                  table=TableRef("t0", 1, 0), page=2),
            Block(id="c3", type=BlockType.TABLE_CELL, text="€980.00",
                  table=TableRef("t0", 1, 1), page=2),
        ],
    )


# ── AC-2.9 字体缺失的错误信息 ─────────────────────────────────────────────


def test_missing_font_error_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(RenderError) as exc:
        register_cjk_font([{"name": "Nope", "path": str(tmp_path / "absent.ttf")}])
    msg = str(exc.value)
    assert "absent.ttf" in msg          # 列出尝试过的路径
    assert "OTF" in msg and "TTF" in msg  # 明确指出格式要求
    assert "cjk_candidates" in msg      # 指到具体配置项


def test_otf_candidate_is_rejected_with_reason(tmp_path: Path) -> None:
    """Noto Sans CJK SC 最常见的发布形态就是 .otf —— 必须给出明确原因。"""
    otf = tmp_path / "NotoSansCJKsc-Regular.otf"
    otf.write_bytes(b"OTTO")  # CFF 轮廓的魔数
    with pytest.raises(RenderError) as exc:
        register_cjk_font([{"name": "NotoSansCJKsc", "path": str(otf)}])
    assert "OTF(CFF)" in str(exc.value)
    assert "不支持" in str(exc.value)


def test_empty_candidates_still_explains(tmp_path: Path) -> None:
    with pytest.raises(RenderError, match="找不到可用的中文字体"):
        register_cjk_font([])


# ── AC-2.8 渲染 ───────────────────────────────────────────────────────────


@requires_font
def test_renders_pdf_with_cjk(tmp_path: Path, tmp_settings: Settings) -> None:
    settings = tmp_settings.model_copy(deep=True)
    settings.fonts.cjk_candidates = _available()
    out = tmp_path / "out.pdf"

    render_pdf(_sample_doc(), out, settings)

    assert out.is_file()
    reader = PdfReader(str(out))
    assert len(reader.pages) >= 1

    text = "\n".join(p.extract_text() or "" for p in reader.pages)
    assert "季度运营报告" in text
    assert "伦巴第" in text          # 表格被渲染出来
    assert "€1,250.00" in text or "1,250.00" in text


@requires_font
def test_font_is_embedded(tmp_path: Path, tmp_settings: Settings) -> None:
    settings = tmp_settings.model_copy(deep=True)
    settings.fonts.cjk_candidates = _available()
    out = tmp_path / "out.pdf"
    render_pdf(_sample_doc(), out, settings)

    fonts: set[str] = set()
    for page in PdfReader(str(out)).pages:
        for ref in page.get("/Resources", {}).get("/Font", {}).values():
            fonts.add(str(ref.get_object().get("/BaseFont", "")))
    assert fonts, "输出 PDF 没有任何字体资源"
    expected = _available()[0]["name"]
    assert any(expected in f for f in fonts), f"未嵌入 {expected}，实际: {fonts}"


@requires_font
def test_page_breaks_follow_source_pages(tmp_path: Path, tmp_settings: Settings) -> None:
    """方案 §5.2 的 V1 底线之一：保证页面顺序。"""
    settings = tmp_settings.model_copy(deep=True)
    settings.fonts.cjk_candidates = _available()
    out = tmp_path / "out.pdf"
    render_pdf(_sample_doc(), out, settings)
    assert len(PdfReader(str(out)).pages) >= 2


@requires_font
def test_html_like_text_does_not_break_renderer(tmp_path: Path,
                                                 tmp_settings: Settings) -> None:
    """ReportLab 的 Paragraph 会解析类 HTML 标记，译文里的 < & 必须先转义。"""
    settings = tmp_settings.model_copy(deep=True)
    settings.fonts.cjk_candidates = _available()
    doc = ParsedDocument(
        source_path=Path("x.pdf"),
        kind="pdf",
        blocks=[
            Block(id="p", type=BlockType.PARAGRAPH,
                  text="条件为 a < b && c > d，参数 <token> 与 R&D 部门"),
        ],
    )
    out = tmp_path / "esc.pdf"
    render_pdf(doc, out, settings)
    text = "\n".join(p.extract_text() or "" for p in PdfReader(str(out)).pages)
    assert "R&D" in text
    assert "<token>" in text


@requires_font
def test_empty_document_still_produces_a_pdf(tmp_path: Path,
                                              tmp_settings: Settings) -> None:
    settings = tmp_settings.model_copy(deep=True)
    settings.fonts.cjk_candidates = _available()
    out = tmp_path / "empty.pdf"
    render_pdf(ParsedDocument(source_path=Path("e.pdf"), kind="pdf", blocks=[]),
               out, settings)
    assert len(PdfReader(str(out)).pages) == 1


@requires_font
def test_no_partial_file_left(tmp_path: Path, tmp_settings: Settings) -> None:
    settings = tmp_settings.model_copy(deep=True)
    settings.fonts.cjk_candidates = _available()
    out = tmp_path / "out.pdf"
    render_pdf(_sample_doc(), out, settings)
    assert not list(tmp_path.glob("*.partial"))


# ── QA 的 PDF 检查 ────────────────────────────────────────────────────────


@requires_font
def test_pdf_qa_reports_font_absence(tmp_path: Path, tmp_settings: Settings) -> None:
    settings = tmp_settings.model_copy(deep=True)
    settings.fonts.cjk_candidates = _available()
    out = tmp_path / "out.pdf"
    render_pdf(_sample_doc(), out, settings)

    # 真实字体名 → 无告警
    assert not [
        w for w in check_pdf_output(out, _available()[0]["name"])
        if w.code == "pdf_font_not_embedded"
    ]
    # 不存在的字体名 → 有告警
    assert [
        w for w in check_pdf_output(out, "NonexistentFont")
        if w.code == "pdf_font_not_embedded"
    ]


def test_pdf_qa_handles_unreadable_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    assert any(w.code == "pdf_unreadable" for w in check_pdf_output(bad))
