"""M3 测试：扫描、路径映射、SQLite 仓储、状态机、失败隔离、三项计数。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import ConfigError, IllegalTransition
from app.document.model import FileResult, JobPhase, QAWarning
from app.document.scanner import assert_not_overlapping, map_output, scan
from app.jobs.models import FileStatus, Job, JobFile, JobSpec, JobStatus
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from tests.conftest import FakeProvider

# ── 目录树构造 ────────────────────────────────────────────────────────────


def _build_tree(root: Path) -> None:
    """3 层嵌套 + 混入应被忽略的文件。"""
    (root / "English").mkdir(parents=True)
    (root / "German" / "2024").mkdir(parents=True)
    (root / "Italian").mkdir(parents=True)

    (root / "English" / "manual.docx").write_bytes(b"PK")
    (root / "English" / "guide.pdf").write_bytes(b"%PDF")
    (root / "German" / "2024" / "contract.pdf").write_bytes(b"%PDF")
    (root / "German" / "legacy.doc").write_bytes(b"\xd0\xcf\x11\xe0")
    (root / "Italian" / "invoice.DOCX").write_bytes(b"PK")

    # 应被忽略
    (root / "notes.txt").write_text("x", encoding="utf-8")
    (root / "sheet.xlsx").write_bytes(b"PK")
    (root / "English" / "~$manual.docx").write_bytes(b"lock")
    (root / ".DS_Store").write_bytes(b"junk")
    (root / "Italian" / ".hidden.pdf").write_bytes(b"%PDF")


# ── AC-3.1 扫描 ───────────────────────────────────────────────────────────


def test_scan_collects_only_supported_files(tmp_path: Path) -> None:
    src = tmp_path / "Source"
    _build_tree(src)
    items = scan(src, tmp_path / "Translated")
    names = sorted(i.source.name for i in items)
    assert names == ["contract.pdf", "guide.pdf", "invoice.DOCX", "legacy.doc",
                     "manual.docx"]


def test_scan_ignores_office_lock_and_hidden(tmp_path: Path) -> None:
    src = tmp_path / "Source"
    _build_tree(src)
    names = {i.source.name for i in scan(src, tmp_path / "Translated")}
    assert "~$manual.docx" not in names
    assert ".hidden.pdf" not in names
    assert ".DS_Store" not in names


def test_scan_order_is_reproducible(tmp_path: Path) -> None:
    src = tmp_path / "Source"
    _build_tree(src)
    first = [i.source for i in scan(src, tmp_path / "T")]
    second = [i.source for i in scan(src, tmp_path / "T")]
    assert first == second


def test_scan_can_exclude_legacy_doc(tmp_path: Path) -> None:
    """doc_conversion.enabled 为 false 时扫描阶段直接忽略 .doc。"""
    src = tmp_path / "Source"
    _build_tree(src)
    items = scan(src, tmp_path / "T", include_doc=False)
    assert not any(i.source.suffix.lower() == ".doc" for i in items)


def test_scan_missing_source_dir(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="源目录不存在"):
        scan(tmp_path / "nope", tmp_path / "out")


# ── AC-3.2 / AC-3.3 路径映射 ──────────────────────────────────────────────


def test_relative_structure_preserved(tmp_path: Path) -> None:
    src = tmp_path / "Source"
    _build_tree(src)
    out = tmp_path / "Translated"
    mapping = {i.relative.as_posix(): i.output.relative_to(out).as_posix()
               for i in scan(src, out)}
    assert mapping["English/manual.docx"] == "English/manual_zh.docx"
    assert mapping["German/2024/contract.pdf"] == "German/2024/contract_zh.pdf"


def test_doc_output_becomes_docx(tmp_path: Path) -> None:
    """方案 §8：.doc 经转换后处理，输出统一为 .docx。"""
    out = map_output(
        Path("/s/German/legacy.doc"), Path("/s"), Path("/o")
    )
    assert out == Path("/o/German/legacy_zh.docx")


def test_suffix_case_is_preserved(tmp_path: Path) -> None:
    """后缀判断不分大小写，但输出保留原写法 —— 避免大小写不敏感文件系统上的重名。"""
    assert map_output(Path("/s/a.PDF"), Path("/s"), Path("/o")).name == "a_zh.PDF"
    assert map_output(Path("/s/b.DOCX"), Path("/s"), Path("/o")).name == "b_zh.DOCX"
    assert map_output(Path("/s/c.DOC"), Path("/s"), Path("/o")).name == "c_zh.docx"


# ── AC-3.5 目录重叠 ───────────────────────────────────────────────────────


def test_same_directory_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="同一个目录"):
        assert_not_overlapping(tmp_path, tmp_path)


def test_output_inside_source_rejected(tmp_path: Path) -> None:
    src = tmp_path / "S"
    (src / "out").mkdir(parents=True)
    with pytest.raises(ConfigError) as exc:
        assert_not_overlapping(src, src / "out")
    assert "重复翻译" in str(exc.value)


def test_source_inside_output_rejected(tmp_path: Path) -> None:
    out = tmp_path / "O"
    (out / "src").mkdir(parents=True)
    with pytest.raises(ConfigError, match="之内"):
        assert_not_overlapping(out / "src", out)


def test_sibling_directories_are_fine(tmp_path: Path) -> None:
    (tmp_path / "S").mkdir()
    (tmp_path / "O").mkdir()
    assert_not_overlapping(tmp_path / "S", tmp_path / "O")


# ── AC-3.7 / AC-3.12 仓储与状态机 ─────────────────────────────────────────


def _store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.db")


def _seed(store: JobStore, job_id: str = "j1", n: int = 2) -> Job:
    job = Job(job_id=job_id, source_path="/s", output_path="/o",
              source_language="auto", target_language="zh", total=n)
    files = [
        JobFile(job_id=job_id, seq=i, source_path=f"/s/f{i}.docx",
                output_path=f"/o/f{i}_zh.docx")
        for i in range(n)
    ]
    store.create_job(job, files)
    return job


def test_job_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    job = store.get_job("j1")
    assert job is not None
    assert job.status is JobStatus.QUEUED
    assert job.total == 2
    assert len(store.list_files("j1")) == 2


def test_valid_transitions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    for target in (JobStatus.PARSING, JobStatus.TRANSLATING, JobStatus.RENDERING,
                   JobStatus.PARSING, JobStatus.COMPLETED):
        store.set_status("j1", target)
    assert store.get_job("j1").status is JobStatus.COMPLETED


def test_started_at_set_on_first_parsing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store)
    assert store.get_job("j1").started_at is None
    store.set_status("j1", JobStatus.PARSING)
    assert store.get_job("j1").started_at is not None


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (JobStatus.COMPLETED, JobStatus.PARSING),
        (JobStatus.FAILED, JobStatus.TRANSLATING),
        (JobStatus.QUEUED, JobStatus.RENDERING),
        (JobStatus.PARSING, JobStatus.RENDERING),
    ],
)
def test_illegal_transitions_rejected(tmp_path: Path, frm: JobStatus,
                                       to: JobStatus) -> None:
    store = _store(tmp_path)
    _seed(store)
    path = {
        JobStatus.QUEUED: [],
        JobStatus.PARSING: [JobStatus.PARSING],
        JobStatus.COMPLETED: [JobStatus.PARSING, JobStatus.COMPLETED],
        JobStatus.FAILED: [JobStatus.FAILED],
    }[frm]
    for step in path:
        store.set_status("j1", step)
    with pytest.raises(IllegalTransition):
        store.set_status("j1", to)


def test_database_survives_reopen(tmp_path: Path) -> None:
    """AC-3.12：重启后 Job 历史仍在。"""
    db = tmp_path / "jobs.db"
    store = JobStore(db)
    _seed(store)
    store.set_status("j1", JobStatus.PARSING)
    store.close()

    reopened = JobStore(db)
    job = reopened.get_job("j1")
    assert job is not None and job.status is JobStatus.PARSING
    assert len(reopened.list_files("j1")) == 2


def test_progress_never_exceeds_total(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, n=2)
    for _ in range(5):
        store.bump_progress("j1")
    assert store.get_job("j1").progress == 2


def test_status_view_counts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, n=3)
    store.mark_file("j1", 0, FileStatus.COMPLETED, warnings=["w1"])
    store.mark_file("j1", 1, FileStatus.SKIPPED)
    store.mark_file("j1", 2, FileStatus.FAILED, error="boom")
    view = store.get_status_view("j1")
    assert (view.completed, view.skipped, view.failed) == (1, 1, 1)
    assert view.failures == [("/s/f2.docx", "boom")]
    assert view.warnings == [("/s/f0.docx", "w1")]


def test_status_view_summary_line_matches_spec(tmp_path: Path) -> None:
    """方案 §19 的完成显示。"""
    store = _store(tmp_path)
    _seed(store, n=1)
    store.mark_file("j1", 0, FileStatus.COMPLETED)
    assert store.get_status_view("j1").summary_line() == (
        "Completed: 1  Skipped: 0  Failed: 0"
    )


def test_unknown_job_returns_none(tmp_path: Path) -> None:
    assert _store(tmp_path).get_status_view("nope") is None


# ── Runner ────────────────────────────────────────────────────────────────


def _runner(tmp_path: Path, settings: Settings, translate_fn) -> JobRunner:
    return JobRunner(_store(tmp_path), FakeProvider(), settings,
                     translate_fn=translate_fn, autostart=False)


def _ok_translate(src, dst, **kwargs):
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"translated")
    on_phase = kwargs.get("on_phase")
    if on_phase:
        for p in (JobPhase.PARSING, JobPhase.TRANSLATING, JobPhase.RENDERING):
            on_phase(p)
    return FileResult(source=src, output=dst, status="completed")


def test_runner_processes_all_files(tmp_path: Path, tmp_settings: Settings) -> None:
    src = tmp_path / "Source"
    _build_tree(src)
    out = tmp_path / "Translated"
    runner = _runner(tmp_path, tmp_settings, _ok_translate)

    job_id = runner.submit(JobSpec(str(src), str(out)))
    view = runner.run_job(job_id)

    assert view.status is JobStatus.COMPLETED
    assert view.completed == 5
    assert view.progress == view.total == 5
    assert (out / "English" / "manual_zh.docx").is_file()
    assert (out / "German" / "2024" / "contract_zh.pdf").is_file()
    assert (out / "German" / "legacy_zh.docx").is_file()


# ── AC-3.4 跳过 ───────────────────────────────────────────────────────────


def test_existing_output_is_skipped(tmp_path: Path, tmp_settings: Settings) -> None:
    src = tmp_path / "Source"
    _build_tree(src)
    out = tmp_path / "Translated"
    (out / "English").mkdir(parents=True)
    (out / "English" / "manual_zh.docx").write_bytes(b"already there")

    calls: list[Path] = []

    def counting(s, d, **kw):
        calls.append(s)
        return _ok_translate(s, d, **kw)

    runner = _runner(tmp_path, tmp_settings, counting)
    view = runner.run_job(runner.submit(JobSpec(str(src), str(out))))

    assert view.skipped == 1
    assert view.completed == 4
    assert not any(c.name == "manual.docx" for c in calls), "跳过的文件不该被翻译"
    assert (out / "English" / "manual_zh.docx").read_bytes() == b"already there"


def test_skip_is_rechecked_at_execution_time(tmp_path: Path,
                                              tmp_settings: Settings) -> None:
    """扫描与执行之间落地的输出文件也要被跳过。"""
    src = tmp_path / "Source"
    src.mkdir()
    (src / "a.docx").write_bytes(b"PK")
    out = tmp_path / "Translated"
    runner = _runner(tmp_path, tmp_settings, _ok_translate)
    job_id = runner.submit(JobSpec(str(src), str(out)))

    # 提交之后、执行之前，输出文件出现了
    (out / "a_zh.docx").parent.mkdir(parents=True, exist_ok=True)
    (out / "a_zh.docx").write_bytes(b"appeared")

    view = runner.run_job(job_id)
    assert view.skipped == 1 and view.completed == 0


# ── AC-3.6 / AC-3.9 失败隔离与计数 ────────────────────────────────────────


def test_single_failure_does_not_stop_others(tmp_path: Path,
                                              tmp_settings: Settings) -> None:
    src = tmp_path / "Source"
    src.mkdir()
    for i in range(5):
        (src / f"f{i}.docx").write_bytes(b"PK")
    out = tmp_path / "Translated"

    def flaky(s, d, **kw):
        if s.name == "f2.docx":
            raise RuntimeError("解析炸了")
        return _ok_translate(s, d, **kw)

    runner = _runner(tmp_path, tmp_settings, flaky)
    view = runner.run_job(runner.submit(JobSpec(str(src), str(out))))

    assert view.completed == 4
    assert view.failed == 1
    assert view.progress == view.total == 5
    assert view.status is JobStatus.COMPLETED, "有成功文件时 Job 不应整体失败"
    assert view.failures[0][0].endswith("f2.docx")
    assert "解析炸了" in view.failures[0][1]
    # 后续文件确实跑完了，不是被短路
    assert (out / "f3_zh.docx").is_file()
    assert (out / "f4_zh.docx").is_file()


def test_all_failures_make_job_failed(tmp_path: Path, tmp_settings: Settings) -> None:
    src = tmp_path / "Source"
    src.mkdir()
    (src / "a.docx").write_bytes(b"PK")

    def always_fail(s, d, **kw):
        raise RuntimeError("nope")

    runner = _runner(tmp_path, tmp_settings, always_fail)
    view = runner.run_job(runner.submit(JobSpec(str(src), str(tmp_path / "T"))))
    assert view.status is JobStatus.FAILED
    assert view.failed == 1


def test_keyboard_interrupt_is_not_swallowed(tmp_path: Path,
                                              tmp_settings: Settings) -> None:
    """宽捕获只吞 Exception —— KeyboardInterrupt 必须能穿透。"""
    src = tmp_path / "Source"
    src.mkdir()
    (src / "a.docx").write_bytes(b"PK")

    def interrupt(s, d, **kw):
        raise KeyboardInterrupt

    runner = _runner(tmp_path, tmp_settings, interrupt)
    job_id = runner.submit(JobSpec(str(src), str(tmp_path / "T")))
    with pytest.raises(KeyboardInterrupt):
        runner.run_job(job_id)


def test_warnings_are_recorded(tmp_path: Path, tmp_settings: Settings) -> None:
    src = tmp_path / "Source"
    src.mkdir()
    (src / "a.docx").write_bytes(b"PK")

    def with_warnings(s, d, **kw):
        result = _ok_translate(s, d, **kw)
        result.warnings = [QAWarning("missing_url", "缺 URL", "b1")]
        return result

    runner = _runner(tmp_path, tmp_settings, with_warnings)
    view = runner.run_job(runner.submit(JobSpec(str(src), str(tmp_path / "T"))))
    assert view.completed == 1
    assert "missing_url" in view.warnings[0][1]


# ── AC-3.10 原文件不变 ────────────────────────────────────────────────────


def test_source_files_unchanged_after_job(tmp_path: Path,
                                           tmp_settings: Settings) -> None:
    src = tmp_path / "Source"
    _build_tree(src)
    before = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in src.rglob("*") if p.is_file()
    }
    runner = _runner(tmp_path, tmp_settings, _ok_translate)
    runner.run_job(runner.submit(JobSpec(str(src), str(tmp_path / "T"))))
    after = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in src.rglob("*") if p.is_file()
    }
    assert before == after


# ── 状态流转与取消 ────────────────────────────────────────────────────────


def test_phase_callback_drives_job_status(tmp_path: Path,
                                           tmp_settings: Settings) -> None:
    src = tmp_path / "Source"
    src.mkdir()
    (src / "a.docx").write_bytes(b"PK")
    seen: list[JobStatus] = []
    store = _store(tmp_path)

    def observing(s, d, **kw):
        result = _ok_translate(s, d, **kw)
        seen.append(store.get_job(job_id).status)
        return result

    runner = JobRunner(store, FakeProvider(), tmp_settings, translate_fn=observing,
                       autostart=False)
    job_id = runner.submit(JobSpec(str(src), str(tmp_path / "T")))
    runner.run_job(job_id)
    assert seen == [JobStatus.RENDERING]
    assert store.get_job(job_id).status is JobStatus.COMPLETED


def test_cancel_stops_before_next_file(tmp_path: Path, tmp_settings: Settings) -> None:
    src = tmp_path / "Source"
    src.mkdir()
    for i in range(4):
        (src / f"f{i}.docx").write_bytes(b"PK")

    runner = _runner(tmp_path, tmp_settings, _ok_translate)
    job_id = runner.submit(JobSpec(str(src), str(tmp_path / "T")))
    runner.cancel(job_id)
    view = runner.run_job(job_id)
    assert view.completed == 0
    assert view.status is JobStatus.COMPLETED


def test_cancel_unknown_job_returns_false(tmp_path: Path,
                                           tmp_settings: Settings) -> None:
    runner = _runner(tmp_path, tmp_settings, _ok_translate)
    assert runner.cancel("nope") is False


def test_empty_directory_completes_immediately(tmp_path: Path,
                                                tmp_settings: Settings) -> None:
    src = tmp_path / "Source"
    src.mkdir()
    runner = _runner(tmp_path, tmp_settings, _ok_translate)
    view = runner.run_job(runner.submit(JobSpec(str(src), str(tmp_path / "T"))))
    assert view.status is JobStatus.COMPLETED
    assert (view.completed, view.skipped, view.failed) == (0, 0, 0)


def test_submit_rejects_overlapping_dirs(tmp_path: Path,
                                          tmp_settings: Settings) -> None:
    src = tmp_path / "Source"
    src.mkdir()
    runner = _runner(tmp_path, tmp_settings, _ok_translate)
    with pytest.raises(ConfigError):
        runner.submit(JobSpec(str(src), str(src)))
