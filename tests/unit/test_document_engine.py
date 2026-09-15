"""M2 测试：契约、.doc 转换、PDF 解析、chunking、QA、隔离性。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import ConversionError, ParseError
from app.document.chunking import build_chunks, split_response, translate_blocks
from app.document.doc_converter import DocConverter
from app.document.model import (
    Block,
    BlockType,
    JobPhase,
    ParsedDocument,
    QAWarning,
    TableRef,
)
from app.document.pdf_reader import parse_pdf
from app.document.qa import check_block, check_content
from tests.conftest import FakeProvider
from tests.fixtures.fake_docling import (
    FakeConverter,
    FakeDoclingDocument,
    TextItem,
    build_sample_document,
)

# ── 契约 A 冻结 ───────────────────────────────────────────────────────────


def test_block_defaults() -> None:
    b = Block(id="x", type=BlockType.PARAGRAPH, text="hello")
    assert b.level is None and b.page is None and b.table is None
    assert b.anchor == {} and b.translated is None
    assert b.translatable is True
    assert b.output_text == "hello"


def test_image_block_is_not_translatable() -> None:
    b = Block(id="i", type=BlockType.IMAGE, text="")
    assert b.translatable is False


def test_output_text_prefers_translation() -> None:
    b = Block(id="x", type=BlockType.PARAGRAPH, text="src", translated="dst")
    assert b.output_text == "dst"


def test_job_phase_values_match_spec() -> None:
    """方案 §9 的状态机用词。"""
    assert [p.value for p in JobPhase] == ["parsing", "translating", "rendering"]


# ── AC-2.13 契约隔离：chunking / qa 不得解释 anchor ───────────────────────


@pytest.mark.parametrize("module", ["app/document/chunking.py", "app/document/qa.py"])
def test_anchor_is_opaque_to_downstream(module: str) -> None:
    text = Path(module).read_text(encoding="utf-8")
    offenders = [
        f"{module}:{i}: {ln.strip()}"
        for i, ln in enumerate(text.splitlines(), 1)
        if "anchor" in ln and not ln.strip().startswith("#")
    ]
    assert not offenders, "下游模块读了 anchor（它只属于 reader/writer 对）:\n" + "\n".join(
        offenders
    )


# ── AC-2.5 .doc 转换 ──────────────────────────────────────────────────────


def _settings_with_soffice(tmp_settings: Settings, path: str) -> Settings:
    s = tmp_settings.model_copy(deep=True)
    s.doc_conversion.soffice_path = path
    return s


def test_soffice_command_shape(tmp_path: Path, tmp_settings: Settings,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "soffice"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    src = tmp_path / "legacy.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0")
    out_dir = tmp_path / "out"
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        (out_dir / "legacy.docx").parent.mkdir(parents=True, exist_ok=True)
        (out_dir / "legacy.docx").write_bytes(b"PK")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    settings = _settings_with_soffice(tmp_settings, str(exe))
    result = DocConverter(settings.doc_conversion).convert(src, out_dir)

    assert result == out_dir / "legacy.docx"
    cmd = captured[0]
    assert "--headless" in cmd
    assert cmd[cmd.index("--convert-to") + 1] == "docx"
    assert any(a.startswith("-env:UserInstallation=") for a in cmd), (
        "缺少独立 user profile —— 并发调用会互相阻塞"
    )


def test_soffice_returncode_zero_but_no_output_is_a_failure(
    tmp_path: Path, tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LibreOffice 有「退出码 0 但不产出文件」的已知行为。"""
    exe = tmp_path / "soffice"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    src = tmp_path / "legacy.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0")

    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")
    )
    monkeypatch.setattr("app.document.doc_converter.shutil.which", lambda name: None)

    settings = _settings_with_soffice(tmp_settings, str(exe))
    with pytest.raises(ConversionError) as exc:
        DocConverter(settings.doc_conversion).convert(src, tmp_path / "out")
    assert "退出码 0 但未生成" in str(exc.value)


def test_falls_back_to_textutil(tmp_path: Path, tmp_settings: Settings,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "legacy.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0")
    out_dir = tmp_path / "out"
    calls: list[str] = []

    monkeypatch.setattr(
        "app.document.doc_converter.shutil.which",
        lambda name: "/usr/bin/textutil" if name == "textutil" else None,
    )

    def fake_run(cmd, **kwargs):
        calls.append(cmd[0])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "legacy.docx").write_bytes(b"PK")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    settings = _settings_with_soffice(tmp_settings, "/nonexistent/soffice")
    result = DocConverter(settings.doc_conversion).convert(src, out_dir)
    assert result.name == "legacy.docx"
    assert calls == ["/usr/bin/textutil"]


def test_both_converters_missing_gives_actionable_error(
    tmp_path: Path, tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "legacy.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0")
    monkeypatch.setattr("app.document.doc_converter.shutil.which", lambda name: None)

    settings = _settings_with_soffice(tmp_settings, "/nonexistent/soffice")
    with pytest.raises(ConversionError) as exc:
        DocConverter(settings.doc_conversion).convert(src, tmp_path / "out")
    msg = str(exc.value)
    assert "soffice_path" in msg
    assert "textutil" in msg
    assert "enabled" in msg  # 给出「移出 V1 范围」这条路


def test_disabled_conversion_explains_itself(tmp_path: Path, tmp_settings: Settings) -> None:
    s = tmp_settings.model_copy(deep=True)
    s.doc_conversion.enabled = False
    src = tmp_path / "legacy.doc"
    src.write_bytes(b"x")
    with pytest.raises(ConversionError, match="enabled"):
        DocConverter(s.doc_conversion).convert(src, tmp_path / "out")


# ── AC-2.6 / AC-2.7 PDF 解析 ──────────────────────────────────────────────


def test_pdf_reader_imports_without_docling() -> None:
    """AC-2.7：延迟导入生效 —— 没装 docling 也能 import 本模块。"""
    import importlib

    module = importlib.import_module("app.document.pdf_reader")
    assert hasattr(module, "parse_pdf")


def test_missing_docling_artifacts_fails_loudly(tmp_settings: Settings) -> None:
    """实机回归（tests/dev-mac/test.log）。

    artifacts 目录缺失时，旧代码是「不设 artifacts_path，让 docling 自己去下载」——
    在断网机上表现为一条跟真正原因无关的报错。现在必须在这里就炸，并且指出去哪修。
    """
    from app.document.pdf_reader import _resolve_artifacts_path

    cfg = tmp_settings.model_copy(deep=True)
    cfg.pdf.docling_artifacts_path = str(Path(tmp_settings.paths.install_root) / "nope")

    with pytest.raises(ParseError) as exc:
        _resolve_artifacts_path(cfg)
    # 报错要能让人直接定位到配置项和备料步骤，而不是只说「失败了」
    assert "pdf.docling_artifacts_path" in str(exc.value)
    assert "§28.1" in str(exc.value)


def test_hf_object_store_layout_is_rejected(tmp_settings: Settings,
                                            tmp_path: Path) -> None:
    """实机回归第三轮 —— 目录形状不对，不是文件损坏。

    `docling-tools models download-hf-repo`（docling 自己报错时给的第 1 条建议）
    产出的是 HF 的对象存储布局：<org>--<repo>/{blobs,refs,snapshots,trees}。
    docling 按平铺路径去找，报的是
    "Image processor config not found: .../preprocessor_config.json" —— 看着像
    文件损坏，实际是形状不对。要平铺那份得用 `docling-tools models download -o`。
    """
    from app.document.pdf_reader import _resolve_artifacts_path

    artifacts = tmp_path / "docling"
    repo = artifacts / "docling-project--docling-layout-heron"
    for sub in ("blobs", "refs", "snapshots", "trees"):
        (repo / sub).mkdir(parents=True)

    cfg = tmp_settings.model_copy(deep=True)
    cfg.pdf.docling_artifacts_path = str(artifacts)

    with pytest.raises(ParseError) as exc:
        _resolve_artifacts_path(cfg)
    assert "blobs/refs/snapshots" in str(exc.value)
    assert "download-hf-repo" in str(exc.value)


def test_prepare_bundle_checks_artifact_shape() -> None:
    """备料期就该拦下这个形状，而不是等 B 机解析第一份 PDF。"""
    text = Path("scripts/prepare-bundle.sh").read_text(encoding="utf-8")
    assert "blobs" in text and "snapshots" in text


def test_docling_cache_root_is_rejected_even_when_populated(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """实机回归第二轮。

    ~/.cache/docling 是个**非空但错误**的取值 —— 底下是 models/ 加各 OCR 引擎目录，
    不是 artifacts 要的 <org>--<repo> 布局。只判「目录非空」拦不住它，
    实际报错会变成 layout-heron 缺 preprocessor_config.json，离病因隔着两层。
    """
    from app.document.pdf_reader import _resolve_artifacts_path

    home = tmp_path / "home"
    cache_root = home / ".cache" / "docling"
    (cache_root / "RapidOcr").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    cfg = tmp_settings.model_copy(deep=True)
    cfg.pdf.docling_artifacts_path = str(cache_root)

    with pytest.raises(ParseError) as exc:
        _resolve_artifacts_path(cfg)
    assert "cache 根目录" in str(exc.value)
    assert "models/docling" in str(exc.value)


def test_install_selfcheck_uses_the_deployed_config() -> None:
    """自检必须走部署中的 config.yaml，不能拿路径硬编码去验。

    硬编码的自检验的是新目录、服务读的是旧配置 —— 自检通过、跑第一份 PDF 照炸，
    实机第二轮就是这么复现的。
    """
    install = Path("scripts/install.sh").read_text(encoding="utf-8")
    block = install[install.index("[8/8]"):]
    assert 'OFFLINE_TRANSLATOR_CONFIG="$ROOT/config/config.yaml"' in block
    assert "Settings.load" in block
    assert "parse_pdf" in block
    assert 'DOCLING_ARTIFACTS_PATH="$ROOT/models/docling"' not in block


def test_empty_docling_artifacts_dir_is_also_rejected(tmp_settings: Settings,
                                                      tmp_path: Path) -> None:
    """目录存在但是空的同样不算数 —— install.sh 建了目录却没拷进去就是这个形态。"""
    from app.document.pdf_reader import _resolve_artifacts_path

    empty = tmp_path / "docling-empty"
    empty.mkdir()
    cfg = tmp_settings.model_copy(deep=True)
    cfg.pdf.docling_artifacts_path = str(empty)

    with pytest.raises(ParseError):
        _resolve_artifacts_path(cfg)


def test_populated_docling_artifacts_dir_passes(tmp_settings: Settings,
                                                tmp_path: Path) -> None:
    from app.document.pdf_reader import _resolve_artifacts_path

    artifacts = tmp_path / "docling"
    (artifacts / "docling-project--docling-layout-heron").mkdir(parents=True)
    cfg = tmp_settings.model_copy(deep=True)
    cfg.pdf.docling_artifacts_path = str(artifacts)

    assert _resolve_artifacts_path(cfg) == str(artifacts)


def test_configured_artifacts_path_is_not_the_docling_cache_root() -> None:
    """配置里填的必须是 artifacts 目录，不是 ~/.cache/docling。

    填成 cache 根目录时 docling 会报
    "Model 'docling-project/docling-layout-heron' not found in artifacts_path"，
    而这正是 tests/dev-mac/test.log 里那两条失败的来源。
    """
    import yaml

    cfg = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
    configured = cfg["pdf"]["docling_artifacts_path"]
    assert not configured.rstrip("/").endswith(".cache/docling"), configured
    assert configured.startswith("/Users/Shared/"), configured


def test_docling_item_types_map_correctly(tmp_settings: Settings) -> None:
    converter = FakeConverter(build_sample_document())
    parsed = parse_pdf(Path("sample.pdf"), tmp_settings, converter=converter)

    by_type: dict[BlockType, list[Block]] = {}
    for b in parsed.blocks:
        by_type.setdefault(b.type, []).append(b)

    assert by_type[BlockType.HEADING][0].text == "Geschäftsbericht 2024"
    assert by_type[BlockType.HEADING][0].level == 1
    assert by_type[BlockType.HEADING][1].level == 2
    assert any("Rechnung" in b.text for b in by_type[BlockType.PARAGRAPH])
    assert by_type[BlockType.LIST_ITEM][0].text == "Erster Punkt"
    assert by_type[BlockType.CAPTION][0].text == "Abbildung 1"
    assert len(by_type[BlockType.IMAGE]) == 1
    assert [b.text for b in by_type[BlockType.TABLE_CELL]] == [
        "Region",
        "Umsatz",
        "Lombardei",
        "EUR 980,00",
    ]


def test_table_cells_carry_coordinates(tmp_settings: Settings) -> None:
    parsed = parse_pdf(
        Path("s.pdf"), tmp_settings, converter=FakeConverter(build_sample_document())
    )
    cells = [b for b in parsed.blocks if b.type is BlockType.TABLE_CELL]
    assert cells[0].table == TableRef(cells[0].table.table_id, 0, 0)
    assert cells[3].table.row == 1 and cells[3].table.col == 1
    assert len({c.table.table_id for c in cells}) == 1


def test_page_numbers_are_recorded(tmp_settings: Settings) -> None:
    parsed = parse_pdf(
        Path("s.pdf"), tmp_settings, converter=FakeConverter(build_sample_document())
    )
    assert {b.page for b in parsed.blocks} == {1, 2}
    assert parsed.meta["page_count"] == 2


def test_unknown_item_type_degrades_to_paragraph(tmp_settings: Settings) -> None:
    """Docling 升级会引入新类型，不能因此让整份文档失败。"""
    parsed = parse_pdf(
        Path("s.pdf"), tmp_settings, converter=FakeConverter(build_sample_document())
    )
    assert any(
        b.text == "Ein neuer Blocktyp" and b.type is BlockType.PARAGRAPH
        for b in parsed.blocks
    )


def test_pdf_parse_error_is_wrapped(tmp_settings: Settings) -> None:
    class Boom:
        def convert(self, path):
            raise RuntimeError("docling exploded")

    with pytest.raises(ParseError, match="PDF 解析失败"):
        parse_pdf(Path("s.pdf"), tmp_settings, converter=Boom())


def test_blank_text_items_are_dropped(tmp_settings: Settings) -> None:
    doc = FakeDoclingDocument(items=[TextItem("   "), TextItem("real")])
    parsed = parse_pdf(Path("s.pdf"), tmp_settings, converter=FakeConverter(doc))
    assert [b.text for b in parsed.blocks] == ["real"]


# ── AC-2.10 Chunking ──────────────────────────────────────────────────────


def _blocks(*specs: tuple[BlockType, str]) -> list[Block]:
    return [
        Block(id=f"b{i}", type=t, text=txt, level=1 if t is BlockType.HEADING else None)
        for i, (t, txt) in enumerate(specs)
    ]


def test_heading_plus_paragraphs_form_one_chunk() -> None:
    blocks = _blocks(
        (BlockType.HEADING, "Titel"),
        (BlockType.PARAGRAPH, "Erster Absatz."),
        (BlockType.PARAGRAPH, "Zweiter Absatz."),
        (BlockType.PARAGRAPH, "Dritter Absatz."),
    )
    chunks = build_chunks(blocks, max_chars=1200)
    assert len(chunks) == 1
    assert chunks[0].block_ids == ["b0", "b1", "b2", "b3"]


def test_budget_splits_chunk() -> None:
    blocks = _blocks(
        (BlockType.HEADING, "T"),
        (BlockType.PARAGRAPH, "a" * 700),
        (BlockType.PARAGRAPH, "b" * 700),
    )
    chunks = build_chunks(blocks, max_chars=1000)
    assert len(chunks) == 2


def test_new_heading_starts_new_chunk() -> None:
    blocks = _blocks(
        (BlockType.HEADING, "A"),
        (BlockType.PARAGRAPH, "x"),
        (BlockType.HEADING, "B"),
        (BlockType.PARAGRAPH, "y"),
    )
    assert len(build_chunks(blocks, 1200)) == 2


def test_table_cells_are_standalone_chunks() -> None:
    blocks = [
        Block(id="p", type=BlockType.PARAGRAPH, text="text"),
        Block(id="c1", type=BlockType.TABLE_CELL, text="Region",
              table=TableRef("t0", 0, 0)),
        Block(id="c2", type=BlockType.TABLE_CELL, text="Umsatz",
              table=TableRef("t0", 0, 1)),
    ]
    chunks = build_chunks(blocks, 1200)
    assert [c.block_ids for c in chunks] == [["p"], ["c1"], ["c2"]]


def test_image_blocks_are_excluded_but_keep_position() -> None:
    blocks = [
        Block(id="p1", type=BlockType.PARAGRAPH, text="one"),
        Block(id="img", type=BlockType.IMAGE, text=""),
        Block(id="p2", type=BlockType.PARAGRAPH, text="two"),
    ]
    chunks = build_chunks(blocks, 1200)
    assert [i for c in chunks for i in c.block_ids] == ["p1", "p2"]
    assert blocks[1].id == "img"  # 仍在序列里


def test_split_response_roundtrip() -> None:
    assert split_response("A\n\nB\n\nC", 3) == ["A", "B", "C"]
    assert split_response("only one", 1) == ["only one"]
    assert split_response("A\n\nB", 3) is None


def test_translate_blocks_fills_translations() -> None:
    blocks = _blocks(
        (BlockType.HEADING, "Titel"),
        (BlockType.PARAGRAPH, "Absatz eins."),
        (BlockType.PARAGRAPH, "Absatz zwei."),
    )
    provider = FakeProvider()
    warnings = translate_blocks(
        blocks, provider, source_language="auto", target_language="zh"
    )
    assert warnings == []
    assert all(b.translated for b in blocks)
    # 一个 chunk 一次请求，而不是逐段落三次（方案 §10：不逐句翻译）
    assert len(provider.calls) == 1


def test_translate_blocks_degrades_when_structure_lost() -> None:
    """模型没保留分隔结构时降级逐段，并记警告 —— 不能让一次错位毁掉整篇。"""

    class LosesStructure(FakeProvider):
        def translate(self, text, source_language, target_language,
                      glossary=None, context=None):
            super().translate(text, source_language, target_language, glossary, context)
            return "全部挤成一段没有分隔"

    blocks = _blocks(
        (BlockType.HEADING, "Titel"),
        (BlockType.PARAGRAPH, "eins"),
        (BlockType.PARAGRAPH, "zwei"),
    )
    provider = LosesStructure()
    warnings = translate_blocks(
        blocks, provider, source_language="auto", target_language="zh"
    )
    assert any(w.code == "chunk_split_mismatch" for w in warnings)
    assert all(b.translated for b in blocks)
    # 1 次分组请求失败 + 3 次逐段重试
    assert len(provider.calls) == 4


# ── AC-2.11 / AC-2.12 QA ──────────────────────────────────────────────────


def test_qa_detects_missing_entities() -> None:
    block = Block(
        id="b",
        type=BlockType.PARAGRAPH,
        text="Invoice €1,250.00 due 2024-03-15, see https://x.io or mail a@b.com",
        translated="发票已到期，请查阅。",
    )
    codes = {w.code for w in check_block(block)}
    assert "missing_currency" in codes
    assert "missing_date" in codes
    assert "missing_url" in codes
    assert "missing_email" in codes


def test_qa_passes_when_entities_preserved() -> None:
    block = Block(
        id="b",
        type=BlockType.PARAGRAPH,
        text="Invoice €1,250.00 due 2024-03-15, see https://x.io or mail a@b.com",
        translated="发票 €1,250.00 于 2024-03-15 到期，详见 https://x.io 或邮件 a@b.com",
    )
    assert check_block(block) == []


def test_qa_ignores_thousands_separator_style() -> None:
    """1,250.00 与 1250.00 是同一个数，不该报丢失。"""
    block = Block(
        id="b",
        type=BlockType.PARAGRAPH,
        text="Total 1,250.00 units",
        translated="共计 1250.00 件",
    )
    assert [w.code for w in check_block(block)] == []


def test_qa_detects_missing_variable_placeholder() -> None:
    block = Block(
        id="b", type=BlockType.PARAGRAPH, text="Hello {name}", translated="你好"
    )
    assert any(w.code == "missing_variable" for w in check_block(block))


def test_qa_does_not_report_extra_content() -> None:
    """译文多出的内容不报 —— 中文表达常会多字，反向比对误报率太高。"""
    block = Block(
        id="b", type=BlockType.PARAGRAPH, text="Total 100", translated="共计 100 件（约 5 箱）"
    )
    assert check_block(block) == []


def test_qa_skips_untranslated_blocks() -> None:
    assert check_block(Block(id="b", type=BlockType.PARAGRAPH, text="x")) == []


def test_check_content_walks_all_blocks() -> None:
    parsed = ParsedDocument(
        source_path=Path("x.docx"),
        kind="docx",
        blocks=[
            Block(id="a", type=BlockType.PARAGRAPH, text="Cost 500", translated="成本"),
            Block(id="b", type=BlockType.PARAGRAPH, text="Cost 600", translated="成本 600"),
        ],
    )
    warnings = check_content(parsed)
    assert [w.block_id for w in warnings] == ["a"]


def test_qa_warning_str_is_readable() -> None:
    assert str(QAWarning("missing_url", "缺 URL", "b1")) == "missing_url [b1]: 缺 URL"
