"""应用入口。

``launchd`` 的 api 服务跑的就是 ``python -m app.main``（方案 §28.3）。

**import 顺序是有意为之**：离线守卫必须在任何第三方库被 import 之前写入环境变量 ——
huggingface_hub / transformers / docling 这类库在**模块导入期**就会读走它们，
晚一步设置就不生效了（方案 §28.1 第 4 步警告的「静默联网」正是这样发生的）。
这会触发 ruff 的 E402，下面逐行标注了 noqa。
"""

from __future__ import annotations

import os  # noqa: E402 —— 见模块 docstring
from pathlib import Path  # noqa: E402

from app.core.config import DEFAULT_CONFIG_PATH, Settings  # noqa: E402
from app.core.offline import apply_offline_guard  # noqa: E402

_CONFIG_PATH = Path(os.environ.get("OFFLINE_TRANSLATOR_CONFIG", str(DEFAULT_CONFIG_PATH)))
_SETTINGS = Settings.load(_CONFIG_PATH) if _CONFIG_PATH.is_file() else Settings()

# ↓↓↓ 必须在其余 import 之前 ↓↓↓
apply_offline_guard(_SETTINGS.pdf.docling_artifacts_path)

from contextlib import asynccontextmanager  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

from app.api.routes import router  # noqa: E402
from app.core.logging import get_logger, setup_logging  # noqa: E402
from app.jobs.runner import JobRunner  # noqa: E402
from app.jobs.store import JobStore  # noqa: E402
from app.translation.hy_mt import HYMTProvider  # noqa: E402

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or _SETTINGS
    setup_logging(cfg.logging.level, cfg.logging.file)

    provider = HYMTProvider(cfg)
    store = JobStore(cfg.paths.db_path)
    runner = JobRunner(store, provider, cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runner.start()
        log.info("离线文档翻译服务已启动（%s:%d）", cfg.server.host, cfg.server.port)
        yield
        runner.shutdown()
        store.close()

    app = FastAPI(
        title="Offline Document Translator",
        version="1.0.0",
        lifespan=lifespan,
        # 内部自用，不开放公开 API 文档（方案 §18）
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = cfg
    app.state.provider = provider
    app.state.store = store
    app.state.runner = runner
    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/v1/defaults", include_in_schema=False)
    def defaults() -> dict[str, str]:
        """给页面预填用。不算对外 API，故不进 schema。"""
        return {
            "source_dir": cfg.defaults.source_dir,
            "output_dir": cfg.defaults.output_dir,
            "source_language": cfg.defaults.source_language,
            "target_language": cfg.defaults.target_language,
        }

    return app


def main() -> None:
    import uvicorn

    cfg = _SETTINGS
    # 只监听 loopback（方案 §22）。host 取自配置，但配置里也写死了 127.0.0.1。
    uvicorn.run(
        create_app(cfg),
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=cfg.logging.level.lower(),
    )


if __name__ == "__main__":
    main()
