"""
FastAPI 应用入口模块。

使用工厂模式创建 FastAPI 实例，集中注册路由、中间件和生命周期事件。
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.database import init_db

logger = get_logger("main")


def _register_tools() -> None:
    """显式注册所有工具模块，确保工具在服务启动时完成注册。"""
    import app.tools.weather_tool  # noqa: F401
    import app.tools.metric_query_tool  # noqa: F401
    import app.tools.report_query_tool  # noqa: F401
    import app.tools.energy_saving_tool  # noqa: F401
    import app.tools.anomaly_query_tool  # noqa: F401
    import app.tools.energy_param_check_tool  # noqa: F401
    import app.tools.batch_energy_tool  # noqa: F401


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期管理。"""
    logger.info("AI Agent 微服务启动中...")
    _register_tools()
    await init_db()
    logger.info("服务已就绪，等待请求")
    yield
    logger.info("AI Agent 微服务正在关闭...")


def create_app() -> FastAPI:
    """应用工厂函数，创建并配置 FastAPI 实例。"""
    application = FastAPI(
        title="AI Agent Microservice",
        version="1.0.0",
        description="生产级单智能体微服务框架，基于 ReAct Loop 模式",
        lifespan=lifespan,
    )

    settings = get_settings()
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(router, prefix="/api/v1")

    # 挂载静态文件目录，提供 Excel 批量报告下载服务
    os.makedirs("static/exports", exist_ok=True)
    application.mount("/static", StaticFiles(directory="static"), name="static")

    @application.get("/")
    async def serve_chat_html() -> FileResponse:
        return FileResponse(Path(__file__).parent / "chat.html", media_type="text/html")

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
