"""API 请求 / 响应模型（方案 §18）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.jobs.models import JobStatusView


class JobCreateRequest(BaseModel):
    """字段与方案 §18 的请求体示例逐字一致。"""

    source_dir: str
    output_dir: str
    source_language: str = "auto"
    target_language: str = "zh"


class JobCreateResponse(BaseModel):
    job_id: str
    total: int


class FailureItem(BaseModel):
    source_path: str
    error: str


class WarningItem(BaseModel):
    source_path: str
    warning: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: int
    total: int
    completed: int
    skipped: int
    failed: int
    summary: str
    source_path: str
    output_path: str
    source_language: str
    target_language: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    failures: list[FailureItem] = Field(default_factory=list)
    warnings: list[WarningItem] = Field(default_factory=list)

    @classmethod
    def from_view(cls, view: JobStatusView) -> JobStatusResponse:
        return cls(
            job_id=view.job_id,
            status=str(view.status),
            progress=view.progress,
            total=view.total,
            completed=view.completed,
            skipped=view.skipped,
            failed=view.failed,
            summary=view.summary_line(),
            source_path=view.source_path,
            output_path=view.output_path,
            source_language=view.source_language,
            target_language=view.target_language,
            created_at=view.created_at,
            started_at=view.started_at,
            completed_at=view.completed_at,
            error=view.error,
            failures=[
                FailureItem(source_path=p, error=e) for p, e in view.failures
            ],
            warnings=[
                WarningItem(source_path=p, warning=w) for p, w in view.warnings
            ],
        )


class HealthResponse(BaseModel):
    """方案 §28.3：llama-server 未就绪时整体返回非健康。"""

    healthy: bool
    api: bool = True
    llama: bool = False
    detail: str = ""
    model_path: str = ""

    # `model_path` 撞上 pydantic v2 的 `model_` 保留前缀
    model_config = {"protected_namespaces": ()}


class BrowseEntry(BaseModel):
    name: str
    path: str


class BrowseResponse(BaseModel):
    path: str
    parent: str | None
    dirs: list[BrowseEntry] = Field(default_factory=list)
