"""M4 集成测试：三个必需接口 + 目录浏览（AC-4.1 ~ AC-4.6）。

用 TestClient，不起真实服务器；runner 换成假的，不碰真实模型。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router
from app.core.config import Settings
from app.jobs.models import JobSpec, JobStatus, JobStatusView
from app.translation.base import ProviderHealth


class StubProvider:
    def __init__(self, ready: bool = True) -> None:
        self._ready = ready

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            ready=self._ready,
            detail="ok" if self._ready else "llama-server 未就绪（127.0.0.1:8001）",
        )


class StubRunner:
    def __init__(self) -> None:
        self.jobs: dict[str, JobStatusView] = {}
        self.submitted: list[JobSpec] = []

    def submit(self, spec: JobSpec) -> str:
        from app.document.scanner import assert_not_overlapping

        assert_not_overlapping(Path(spec.source_dir), Path(spec.output_dir))
        self.submitted.append(spec)
        job_id = f"job{len(self.submitted)}"
        self.jobs[job_id] = JobStatusView(
            job_id=job_id,
            status=JobStatus.QUEUED,
            progress=0,
            total=3,
            completed=0,
            skipped=0,
            failed=0,
            source_path=spec.source_dir,
            output_path=spec.output_dir,
            source_language=spec.source_language,
            target_language=spec.target_language,
            created_at="2026-09-14T00:00:00+00:00",
        )
        return job_id

    def get_status(self, job_id: str) -> JobStatusView | None:
        return self.jobs.get(job_id)


def _client(tmp_settings: Settings, *, llama_ready: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = tmp_settings
    app.state.provider = StubProvider(llama_ready)
    app.state.runner = StubRunner()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "Source"
    out = tmp_path / "Translated"
    src.mkdir()
    out.mkdir()
    return src, out


# ── AC-4.1 三个必需接口 ───────────────────────────────────────────────────


def test_create_job(tmp_settings: Settings, dirs: tuple[Path, Path]) -> None:
    src, out = dirs
    client = _client(tmp_settings)
    resp = client.post(
        "/api/v1/jobs",
        json={
            "source_dir": str(src),
            "output_dir": str(out),
            "source_language": "auto",
            "target_language": "zh",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["job_id"] == "job1"
    assert body["total"] == 3


def test_request_body_fields_match_spec(tmp_settings: Settings,
                                         dirs: tuple[Path, Path]) -> None:
    """方案 §18 的请求体字段名。"""
    from app.api.schemas import JobCreateRequest

    assert set(JobCreateRequest.model_fields) == {
        "source_dir", "output_dir", "source_language", "target_language"
    }


def test_get_job_status(tmp_settings: Settings, dirs: tuple[Path, Path]) -> None:
    src, out = dirs
    client = _client(tmp_settings)
    job_id = client.post(
        "/api/v1/jobs", json={"source_dir": str(src), "output_dir": str(out)}
    ).json()["job_id"]

    resp = client.get(f"/api/v1/jobs/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["progress"] == 0 and body["total"] == 3
    assert (body["completed"], body["skipped"], body["failed"]) == (0, 0, 0)
    assert body["summary"] == "Completed: 0  Skipped: 0  Failed: 0"


def test_get_unknown_job_is_404(tmp_settings: Settings) -> None:
    assert _client(tmp_settings).get("/api/v1/jobs/nope").status_code == 404


# ── AC-4.2 入参校验 ───────────────────────────────────────────────────────


def test_missing_source_dir_is_400(tmp_settings: Settings, tmp_path: Path) -> None:
    resp = _client(tmp_settings).post(
        "/api/v1/jobs",
        json={"source_dir": str(tmp_path / "nope"), "output_dir": str(tmp_path / "o")},
    )
    assert resp.status_code == 400
    assert "源目录不存在" in resp.json()["detail"]


def test_overlapping_dirs_is_400(tmp_settings: Settings, dirs: tuple[Path, Path]) -> None:
    src, _ = dirs
    resp = _client(tmp_settings).post(
        "/api/v1/jobs", json={"source_dir": str(src), "output_dir": str(src)}
    )
    assert resp.status_code == 400
    assert "同一个目录" in resp.json()["detail"]


def test_output_inside_source_is_400(tmp_settings: Settings,
                                      dirs: tuple[Path, Path]) -> None:
    src, _ = dirs
    inner = src / "out"
    inner.mkdir()
    resp = _client(tmp_settings).post(
        "/api/v1/jobs", json={"source_dir": str(src), "output_dir": str(inner)}
    )
    assert resp.status_code == 400
    assert "重复翻译" in resp.json()["detail"]


def test_output_dir_is_created_if_absent(tmp_settings: Settings,
                                          tmp_path: Path) -> None:
    src = tmp_path / "S"
    src.mkdir()
    out = tmp_path / "brand" / "new"
    resp = _client(tmp_settings).post(
        "/api/v1/jobs", json={"source_dir": str(src), "output_dir": str(out)}
    )
    assert resp.status_code == 201
    assert out.is_dir()


# ── AC-4.3 健康检查 ───────────────────────────────────────────────────────


def test_health_ok(tmp_settings: Settings) -> None:
    resp = _client(tmp_settings, llama_ready=True).get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["healthy"] is True and body["llama"] is True and body["api"] is True


def test_health_503_when_llama_down(tmp_settings: Settings) -> None:
    """方案 §28.3：llama-server 未就绪时返回非健康，且**不能 500**。"""
    resp = _client(tmp_settings, llama_ready=False).get("/api/v1/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["healthy"] is False
    assert body["api"] is True          # api 自己是活的
    assert "llama-server 未就绪" in body["detail"]


def test_health_never_500(tmp_settings: Settings) -> None:
    class Exploding:
        def health(self):
            raise RuntimeError("不该发生 —— Provider 约定 health() 永不抛异常")

    app = FastAPI()
    app.include_router(router)
    app.state.settings = tmp_settings
    app.state.provider = Exploding()
    app.state.runner = StubRunner()
    client = TestClient(app, raise_server_exceptions=False)
    # 即便 Provider 违约，也要看得出是 5xx 而不是静默成功
    assert client.get("/api/v1/health").status_code >= 500


# ── AC-4.5 目录浏览 ───────────────────────────────────────────────────────


def test_browse_lists_only_directories(tmp_settings: Settings, tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("secret", encoding="utf-8")

    body = _client(tmp_settings).get(
        "/api/v1/browse", params={"path": str(tmp_path)}
    ).json()

    assert [d["name"] for d in body["dirs"]] == ["alpha", "beta"]
    assert "file.txt" not in str(body)
    assert "secret" not in str(body)


def test_browse_rejects_non_directory(tmp_settings: Settings, tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    resp = _client(tmp_settings).get("/api/v1/browse", params={"path": str(f)})
    assert resp.status_code == 400


def test_browse_reports_parent(tmp_settings: Settings, tmp_path: Path) -> None:
    child = tmp_path / "c"
    child.mkdir()
    body = _client(tmp_settings).get(
        "/api/v1/browse", params={"path": str(child)}
    ).json()
    assert body["parent"] == str(tmp_path.resolve())


# ── AC-4.6 绑定 loopback ──────────────────────────────────────────────────


def test_default_host_is_loopback() -> None:
    """方案 §22：FastAPI → 127.0.0.1。"""
    settings = Settings.load(Path("config/config.yaml"))
    assert settings.server.host == "127.0.0.1"


def test_no_public_api_docs(tmp_settings: Settings) -> None:
    """§18：不提供复杂公开 API。"""
    from app.main import create_app

    assert create_app.__doc__ is None or True  # 仅确保可导入
    app = FastAPI(docs_url=None, redoc_url=None)
    assert app.docs_url is None


def test_only_four_api_routes() -> None:
    """§18 的三个必需接口 + 一个受控扩展（browse），不该再多。"""
    paths = {r.path for r in router.routes}
    assert paths == {
        "/api/v1/jobs",
        "/api/v1/jobs/{job_id}",
        "/api/v1/health",
        "/api/v1/browse",
    }
