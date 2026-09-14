"""端到端（AC-P6）：真实目录 → 真实解析 → 真实写回/渲染 → 真实 SQLite。

只有两处是假的：翻译模型（FakeProvider）与 Docling（假 converter）。
其余全部走生产代码路径 —— 扫描、路径映射、跳过、失败隔离、状态机、计数、QA。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core.config import Settings
from app.document.docx_reader import count_structure
from app.jobs.models import JobSpec, JobStatus
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from tests.conftest import FakeProvider
from tests.fixtures.docx_factory import (
    build_rich_docx,
    build_simple_docx,
    count_drawings,
    hyperlink_targets,
)
from tests.fixtures.fake_docling import FakeConverter, build_sample_document
from tests.unit.test_pdf_render import _available, requires_font


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    """构造 §7 示例那样的嵌套目录，外加跳过项与坏文件。"""
    src = tmp_path / "Source"
    out = tmp_path / "Translated"

    (src / "English").mkdir(parents=True)
    (src / "German" / "2024").mkdir(parents=True)
    (src / "Italian").mkdir(parents=True)

    build_rich_docx(src / "English" / "manual.docx")
    build_simple_docx(
        src / "German" / "2024" / "contract.docx",
        ["Die Rechnung beträgt EUR 1.250,00.", "Fällig am 2024-03-15."],
    )
    build_simple_docx(src / "Italian" / "invoice.docx", ["Fattura numero 7788."])

    # 已有输出 → 应被跳过
    build_simple_docx(src / "English" / "done.docx", ["already translated"])
    (out / "English").mkdir(parents=True)
    (out / "English" / "done_zh.docx").write_bytes(b"pre-existing")

    # 坏文件（不是合法 zip）→ 应单独失败，不影响其他文件
    (src / "German" / "broken.docx").write_bytes(b"this is not a docx")

    # 应被忽略的文件
    (src / "readme.txt").write_text("ignore me", encoding="utf-8")
    (src / "English" / "~$manual.docx").write_bytes(b"lock")

    return src, out


def _run(tmp_path: Path, settings: Settings, src: Path, out: Path):
    store = JobStore(tmp_path / "jobs.db")
    runner = JobRunner(store, FakeProvider(), settings, autostart=False)
    job_id = runner.submit(JobSpec(str(src), str(out)))
    return runner.run_job(job_id), store, job_id


def test_end_to_end_directory_translation(tmp_path: Path, workspace: tuple[Path, Path],
                                           tmp_settings: Settings) -> None:
    src, out = workspace
    before = {p: _sha256(p) for p in src.rglob("*") if p.is_file()}

    view, store, job_id = _run(tmp_path, tmp_settings, src, out)

    # ── 计数（§19 的完成显示）────────────────────────────────────────────
    assert view.completed == 3
    assert view.skipped == 1
    assert view.failed == 1
    assert view.summary_line() == "Completed: 3  Skipped: 1  Failed: 1"
    assert view.progress == view.total == 5

    # ── 有成功文件时 Job 不整体失败 ──────────────────────────────────────
    assert view.status is JobStatus.COMPLETED

    # ── 输出保持相对目录结构、_zh 命名（§7 / §8）─────────────────────────
    assert (out / "English" / "manual_zh.docx").is_file()
    assert (out / "German" / "2024" / "contract_zh.docx").is_file()
    assert (out / "Italian" / "invoice_zh.docx").is_file()

    # ── 跳过的文件没有被覆盖 ────────────────────────────────────────────
    assert (out / "English" / "done_zh.docx").read_bytes() == b"pre-existing"

    # ── 失败隔离：坏文件单独失败，其他文件照常完成（§26）─────────────────
    assert not (out / "German" / "broken_zh.docx").exists()
    failed_path, failed_error = view.failures[0]
    assert failed_path.endswith("broken.docx")
    assert failed_error, "失败原因必须被记录下来"

    # ── 原文件永不修改（§8 / §22）────────────────────────────────────────
    after = {p: _sha256(p) for p in src.rglob("*") if p.is_file()}
    assert before == after

    # ── 半成品文件不许留下（否则下次会被当成「已存在」而跳过）────────────
    assert not list(out.rglob("*.partial"))

    # ── SQLite 里可复查 ─────────────────────────────────────────────────
    files = store.list_files(job_id)
    assert len(files) == 5
    assert {str(f.status) for f in files} == {"completed", "skipped", "failed"}


def test_end_to_end_preserves_document_fidelity(
    tmp_path: Path, workspace: tuple[Path, Path], tmp_settings: Settings
) -> None:
    """结构、图片、超链接在整条链路跑完后仍然完好。"""
    src, out = workspace
    _run(tmp_path, tmp_settings, src, out)

    original = src / "English" / "manual.docx"
    translated = out / "English" / "manual_zh.docx"

    assert count_structure(original) == count_structure(translated)
    assert count_drawings(translated) == count_drawings(original)
    assert hyperlink_targets(translated) == hyperlink_targets(original)

    from docx import Document

    body = "\n".join(p.text for p in Document(str(translated)).paragraphs)
    assert "[zh]" in body, "译文应当已写回"


def test_end_to_end_state_persists_across_restart(
    tmp_path: Path, workspace: tuple[Path, Path], tmp_settings: Settings
) -> None:
    """§9：Job 历史落在 SQLite，重开仍在。"""
    src, out = workspace
    _, store, job_id = _run(tmp_path, tmp_settings, src, out)
    store.close()

    reopened = JobStore(tmp_path / "jobs.db")
    view = reopened.get_status_view(job_id)
    assert view is not None
    assert (view.completed, view.skipped, view.failed) == (3, 1, 1)
    assert view.status is JobStatus.COMPLETED


@requires_font
def test_end_to_end_includes_pdf(tmp_path: Path, tmp_settings: Settings,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """PDF 走完整链路：Docling 解析 → 翻译 → ReportLab 渲染。

    只把 Docling 换成假 converter（本机不装 docling、不下模型），
    ``parse_pdf`` 本身仍是生产代码。
    """
    from pypdf import PdfReader

    monkeypatch.setattr(
        "app.document.pdf_reader._build_converter",
        lambda settings: FakeConverter(build_sample_document()),
    )

    settings = tmp_settings.model_copy(deep=True)
    settings.fonts.cjk_candidates = _available()

    src = tmp_path / "Source"
    (src / "German").mkdir(parents=True)
    (src / "German" / "bericht.pdf").write_bytes(b"%PDF-1.4 stub")
    build_simple_docx(src / "note.docx", ["Eine Notiz."])
    out = tmp_path / "Translated"

    view, _, _ = _run(tmp_path, settings, src, out)

    assert view.completed == 2
    assert view.failed == 0
    pdf_out = out / "German" / "bericht_zh.pdf"
    assert pdf_out.is_file()

    text = "\n".join(p.extract_text() or "" for p in PdfReader(str(pdf_out)).pages)
    assert "[zh]" in text
    assert "Lombardei" in text or "[zh]Lombardei" in text  # 表格也被渲染出来


def test_second_run_skips_everything(tmp_path: Path, workspace: tuple[Path, Path],
                                      tmp_settings: Settings) -> None:
    """跑第二遍时全部跳过 —— 「跳过已有输出」实际达到了增量的效果。"""
    src, out = workspace
    _run(tmp_path, tmp_settings, src, out)

    store = JobStore(tmp_path / "jobs2.db")
    runner = JobRunner(store, FakeProvider(), tmp_settings, autostart=False)
    view = runner.run_job(runner.submit(JobSpec(str(src), str(out))))

    assert view.completed == 0
    assert view.skipped == 4   # 3 个已翻译 + 1 个本就存在
    assert view.failed == 1    # 坏文件仍然失败（它没有产出，不会被跳过）
