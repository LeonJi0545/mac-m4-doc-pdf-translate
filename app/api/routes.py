"""API 路由（方案 §18）。

只提供必要接口：创建任务、查任务、健康检查。
外加一个只读的目录浏览 —— 见下面 ``browse`` 的说明。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from app.api.schemas import (
    BrowseEntry,
    BrowseResponse,
    HealthResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobStatusResponse,
)
from app.core.errors import ConfigError
from app.core.logging import get_logger
from app.jobs.models import JobSpec
from app.jobs.runner import JobRunner

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1")


def _runner(request: Request) -> JobRunner:
    return request.app.state.runner


@router.post("/jobs", response_model=JobCreateResponse, status_code=201)
def create_job(payload: JobCreateRequest, request: Request) -> JobCreateResponse:
    source = Path(payload.source_dir).expanduser()
    output = Path(payload.output_dir).expanduser()

    if not source.is_dir():
        raise HTTPException(400, f"源目录不存在或不是目录: {source}")
    if not os.access(source, os.R_OK):
        raise HTTPException(400, f"源目录不可读: {source}")

    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, f"无法创建输出目录 {output}: {exc}") from exc
    if not os.access(output, os.W_OK):
        raise HTTPException(400, f"输出目录不可写: {output}")

    runner = _runner(request)
    try:
        job_id = runner.submit(
            JobSpec(
                source_dir=str(source),
                output_dir=str(output),
                source_language=payload.source_language,
                target_language=payload.target_language,
            )
        )
    except ConfigError as exc:
        # 目录重叠等配置性错误是用户输入问题，不是服务器故障
        raise HTTPException(400, str(exc)) from exc

    view = runner.get_status(job_id)
    return JobCreateResponse(job_id=job_id, total=view.total if view else 0)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str, request: Request) -> JobStatusResponse:
    view = _runner(request).get_status(job_id)
    if view is None:
        raise HTTPException(404, f"Job 不存在: {job_id}")
    return JobStatusResponse.from_view(view)


@router.get("/health", response_model=HealthResponse)
def health(request: Request, response: Response) -> HealthResponse:
    """健康检查。

    ``provider.health()`` 约定永不抛异常，所以这里**永远不会 500** ——
    一个会把自己搞崩的健康检查没有意义。

    llama-server 未就绪时返回 503：launchd 不保证 api 与 llama 的启动顺序
    （方案 §28.3），依赖关系放在这里表达，而不是试图让 launchd 去表达。
    """
    provider = request.app.state.provider
    settings = request.app.state.settings
    result = provider.health()
    if not result.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        healthy=result.ready,
        api=True,
        llama=result.ready,
        detail=result.detail,
        model_path=settings.model.path,
    )


@router.get("/browse", response_model=BrowseResponse)
def browse(path: str = Query(default="")) -> BrowseResponse:
    """只读目录浏览。

    这是对方案 §18「只提供必要接口」的一处**受控扩展**：§6 的界面画了源目录 /
    输出目录的「选择」按钮，但浏览器沙箱拿不到服务端的绝对路径，没有这个接口
    那两个按钮就是死的。

    边界：只列子目录、**不返回文件内容**、跳过隐藏目录、无权限时返回空列表。
    """
    target = Path(path).expanduser() if path else Path.home()
    try:
        target = target.resolve()
    except OSError as exc:
        raise HTTPException(400, f"路径无法解析: {path} ({exc})") from exc

    if not target.is_dir():
        raise HTTPException(400, f"不是目录: {target}")

    try:
        dirs = sorted(
            (d for d in target.iterdir() if d.is_dir() and not d.name.startswith(".")),
            key=lambda d: d.name.lower(),
        )
    except PermissionError:
        dirs = []

    return BrowseResponse(
        path=str(target),
        parent=str(target.parent) if target.parent != target else None,
        dirs=[BrowseEntry(name=d.name, path=str(d)) for d in dirs],
    )
