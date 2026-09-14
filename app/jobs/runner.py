"""Job 执行器（方案 §19 / §21 / §26）。

并发模型：**单后台工作线程，文件串行处理**。
方案 §21 明确只常驻一个模型、一个 llama-server —— 并发翻译不会更快，
只会争抢同一块统一内存。同一时刻也只跑一个 Job，其余排队。
"""

from __future__ import annotations

import queue
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from app.core.config import Settings
from app.core.logging import get_logger
from app.document.model import JobPhase
from app.document.scanner import assert_not_overlapping, scan
from app.jobs.models import FileStatus, Job, JobFile, JobSpec, JobStatus, JobStatusView
from app.jobs.pipeline import translate_file
from app.jobs.store import JobStore
from app.translation.base import TranslationProvider

log = get_logger(__name__)

TranslateFn = Callable[..., object]


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        provider: TranslationProvider,
        settings: Settings,
        *,
        translate_fn: TranslateFn | None = None,
        autostart: bool = True,
    ) -> None:
        self.store = store
        self.provider = provider
        self.settings = settings
        # 注入点：测试用来打桩，生产走真实流水线
        self._translate = translate_fn or translate_file
        # 关掉后就只能用同步入口 run_job()，测试靠它避免依赖线程调度时序
        self._autostart = autostart

        self._queue: queue.Queue[str] = queue.Queue()
        self._cancelled: set[str] = set()
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._loop, name="job-worker", daemon=True)
        self._worker.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._queue.put("")  # 唤醒阻塞中的 get
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            self._worker = None

    # ── 提交与取消 ────────────────────────────────────────────────────────

    def submit(self, spec: JobSpec) -> str:
        """建 Job 并入队，立即返回 job_id。"""
        source = Path(spec.source_dir)
        output = Path(spec.output_dir)
        assert_not_overlapping(source, output)

        items = scan(
            source, output, include_doc=self.settings.doc_conversion.enabled
        )
        job_id = uuid.uuid4().hex[:16]
        job = Job(
            job_id=job_id,
            source_path=str(source),
            output_path=str(output),
            source_language=spec.source_language,
            target_language=spec.target_language,
            total=len(items),
        )
        files = [
            JobFile(
                job_id=job_id,
                seq=i,
                source_path=str(item.source),
                output_path=str(item.output),
            )
            for i, item in enumerate(items)
        ]
        self.store.create_job(job, files)
        self._queue.put(job_id)
        if self._autostart:
            self.start()
        log.info("已创建 Job %s，共 %d 个文件", job_id, len(items))
        return job_id

    def cancel(self, job_id: str) -> bool:
        """协作式取消：当前文件跑完后停止，不中断正在进行的翻译。"""
        view = self.store.get_status_view(job_id)
        if view is None or view.is_terminal:
            return False
        with self._lock:
            self._cancelled.add(job_id)
        return True

    def get_status(self, job_id: str) -> JobStatusView | None:
        return self.store.get_status_view(job_id)

    # ── 执行 ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._queue.get()
            if not job_id or self._stop.is_set():
                continue
            try:
                self.run_job(job_id)
            except Exception:  # noqa: BLE001 —— worker 线程不能因单个 Job 而死
                log.exception("Job %s 执行异常", job_id)

    def run_job(self, job_id: str) -> JobStatusView | None:
        """跑完一个 Job。同步入口，测试直接调它，避免依赖线程调度时序。

        用原子「认领」保证同一个 Job 不会被并发跑两遍 —— 否则同一个文件会被
        处理两次，计数错乱，输出还可能互相覆盖。
        """
        job = self.store.get_job(job_id)
        if job is None:
            return None

        with self._lock:
            if job_id in self._running:
                log.debug("Job %s 已在执行中，跳过重复调度", job_id)
                return self.store.get_status_view(job_id)
            self._running.add(job_id)

        try:
            return self._run_claimed(job_id, job)
        finally:
            with self._lock:
                self._running.discard(job_id)

    def _run_claimed(self, job_id: str, job: Job) -> JobStatusView | None:
        for jf in self.store.list_files(job_id):
            with self._lock:
                cancelled = job_id in self._cancelled
            if cancelled:
                log.info("Job %s 已取消，停在第 %d 个文件", job_id, jf.seq)
                break

            source = Path(jf.source_path)
            output = Path(jf.output_path)

            # 执行时**重新**判定跳过 —— 扫描与执行之间可能有文件落地
            if output.exists():
                self.store.mark_file(job_id, jf.seq, FileStatus.SKIPPED)
                self.store.bump_progress(job_id)
                continue

            try:
                result = self._translate(
                    source,
                    output,
                    provider=self.provider,
                    settings=self.settings,
                    source_language=job.source_language,
                    target_language=job.target_language,
                    on_phase=lambda phase: self._set_phase(job_id, phase),
                )
                warnings = [str(w) for w in getattr(result, "warnings", [])]
                self.store.mark_file(
                    job_id, jf.seq, FileStatus.COMPLETED, warnings=warnings
                )
            except Exception as exc:  # noqa: BLE001
                # 刻意的宽捕获：方案 §26 要求「失败文件不影响其他文件」。
                # KeyboardInterrupt / SystemExit 继承自 BaseException，不会被吞。
                log.exception("文件处理失败: %s", source)
                self.store.mark_file(
                    job_id,
                    jf.seq,
                    FileStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                )
            finally:
                self.store.bump_progress(job_id)

        self._finalize(job_id)
        return self.store.get_status_view(job_id)

    def _set_phase(self, job_id: str, phase: JobPhase) -> None:
        try:
            self.store.set_status(job_id, JobStatus(phase.value))
        except Exception:  # noqa: BLE001 —— 状态上报失败不该让文件处理失败
            log.debug("状态上报失败: %s -> %s", job_id, phase, exc_info=True)

    def _finalize(self, job_id: str) -> None:
        view = self.store.get_status_view(job_id)
        if view is None or view.is_terminal:
            return
        # 只要有文件成功或被跳过，Job 就算完成；全部失败才算 Job 失败。
        if view.failed and not view.completed and not view.skipped:
            self.store.set_status(
                job_id,
                JobStatus.FAILED,
                error=f"全部 {view.failed} 个文件处理失败",
            )
        else:
            self.store.set_status(job_id, JobStatus.COMPLETED)
        with self._lock:
            self._cancelled.discard(job_id)
