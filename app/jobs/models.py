"""Job 数据模型与状态机（方案 §9 / §19）。

方案 §9 把 ``parsing / translating / rendering`` 列为 **Job** 状态，
但 §19 又要求完成后给出 Completed / Skipped / Failed 三个**文件级**计数，
§26 还要求单文件失败隔离。两者需要两个层级：

- ``jobs.status`` 用 §9 的原词，取值是**当前正在处理文件**所处的阶段
- ``job_files.status`` 记每个文件的最终结果

这是 SQLite 内的一张附加表，**没有引入任何新中间件**，仍符合 §9 与 §23。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    PARSING = "parsing"
    TRANSLATING = "translating"
    RENDERING = "rendering"
    COMPLETED = "completed"
    FAILED = "failed"


class FileStatus(StrEnum):
    QUEUED = "queued"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


# 合法跃迁表。
# ``rendering -> parsing`` 是合法的：一个文件渲染完，轮到下一个文件开始解析。
# ``queued -> completed`` 也合法：目录里所有文件都被跳过时 Job 直接完成。
ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.QUEUED: {JobStatus.PARSING, JobStatus.COMPLETED, JobStatus.FAILED},
    JobStatus.PARSING: {JobStatus.TRANSLATING, JobStatus.COMPLETED, JobStatus.FAILED},
    JobStatus.TRANSLATING: {JobStatus.RENDERING, JobStatus.COMPLETED, JobStatus.FAILED},
    JobStatus.RENDERING: {JobStatus.PARSING, JobStatus.COMPLETED, JobStatus.FAILED},
    JobStatus.COMPLETED: set(),
    JobStatus.FAILED: set(),
}

TERMINAL_STATUSES = {JobStatus.COMPLETED, JobStatus.FAILED}


def now_iso() -> str:
    """ISO-8601 UTC。SQLite 没有原生 datetime 类型，一律存字符串。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class JobSpec:
    """创建 Job 的入参，字段与方案 §18 的请求体一致。"""

    source_dir: str
    output_dir: str
    source_language: str = "auto"
    target_language: str = "zh"


@dataclass
class Job:
    job_id: str
    source_path: str
    output_path: str
    source_language: str
    target_language: str
    status: JobStatus = JobStatus.QUEUED
    progress: int = 0
    total: int = 0
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None


@dataclass
class JobFile:
    job_id: str
    seq: int
    source_path: str
    output_path: str
    status: FileStatus = FileStatus.QUEUED
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    started_at: str | None = None
    completed_at: str | None = None


@dataclass
class JobStatusView:
    """给 API 层用的聚合视图。

    三项计数在这里就算好，API 层只做序列化 —— 业务聚合不该散落到两处。
    """

    job_id: str
    status: JobStatus
    progress: int
    total: int
    completed: int
    skipped: int
    failed: int
    source_path: str
    output_path: str
    source_language: str
    target_language: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    failures: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def summary_line(self) -> str:
        """方案 §19 的完成显示格式。"""
        return f"Completed: {self.completed}  Skipped: {self.skipped}  Failed: {self.failed}"
