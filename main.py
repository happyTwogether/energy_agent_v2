"""
FastAPI 应用入口模块。

使用工厂模式创建 FastAPI 实例，集中注册路由、中间件和生命周期事件。
"""

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import AsyncGenerator
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from app.api.routes import router
from app.agent.errors import AgentPublicError
from app.core.config import get_settings
from app.core.logging import get_logger
from app.self_service import warm_business_catalog
from app.services.database import check_session_storage, dispose_self_service_engine, init_db
from app.services.database_readiness import DatabaseReadiness

logger = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期管理。"""
    logger.info("AI Agent 微服务启动中...")
    initialized = False

    async def check_database():
        nonlocal initialized
        if initialized:
            await check_session_storage()
        else:
            await init_db()
            initialized = True

    status = DatabaseReadiness(
        check_database,
        on_ready=warm_business_catalog if get_settings().self_service_enabled else None,
    )
    if app is not None:
        app.state.database_readiness = status
    await status.check()
    recovery_task = asyncio.create_task(status.run(), name="database-readiness")
    logger.info("HTTP 服务启动，数据库就绪=%s；未就绪时自动重试并拒绝业务请求", status.ready)
    try:
        yield
    finally:
        recovery_task.cancel()
        with suppress(asyncio.CancelledError):
            await recovery_task
        await dispose_self_service_engine()
        logger.info("AI Agent 微服务正在关闭...")


def create_app() -> FastAPI:
    """应用工厂函数，创建并配置 FastAPI 实例。"""
    application = FastAPI(
        title="AI Agent Microservice",
        version="1.0.0",
        description="生产级单智能体微服务，基于 AgentScope 2.x 架构",
        lifespan=lifespan,
    )

    settings = get_settings()

    @application.middleware("http")
    async def require_database_readiness(request: Request, call_next):
        status = getattr(request.app.state, "database_readiness", None)
        path = request.url.path.rstrip("/")
        if (
            status is not None and not status.ready and request.method != "OPTIONS"
            and path.startswith("/api/v1/")
            and path not in {"/api/v1/health", "/api/v1/health/live"}
        ):
            return JSONResponse(
                status_code=503, content={"code": status.code, "message": status.message},
                headers={"Retry-After": "5", "Cache-Control": "no-store"},
            )
        return await call_next(request)

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(router, prefix="/api/v1")

    @application.exception_handler(AgentPublicError)
    async def handle_agent_public_error(request: Request, exc: AgentPublicError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"message": str(exc), "code": exc.code})

    # 静态文件目录
    export_dir = os.path.join(os.getcwd(), settings.export_dir)
    os.makedirs(export_dir, exist_ok=True)
    application.mount("/static", StaticFiles(directory="static"), name="static")

    @application.get("/")
    async def serve_chat_html() -> FileResponse:
        return FileResponse(Path(__file__).parent / "chat.html", media_type="text/html")

    @application.get("/downloads/{filename}")
    async def download_excel(filename: str) -> FileResponse:
        """强制下载 Excel 文件（添加 Content-Disposition: attachment 头）。"""
        file_path = os.path.join(export_dir, filename)
        if not os.path.exists(file_path):
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(
            file_path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    return application


app = create_app()


if __name__ == "__main__":
    """本机开发入口：允许直接 python main.py 启动服务。"""
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=["app"],
    )
