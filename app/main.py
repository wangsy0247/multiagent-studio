"""
App 服务 — 多Agent协作工作台业务层

职责:
- 用户认证 (注册/登录/JWT)
- 会话管理 (Thread CRUD)
- 文件上传管理
- 配置管理 (模型/工具/MCP)
- 代理 Harness 服务 API (执行/SSE/停止/状态)
- 代理 Langfuse 查询 (Trace/Token)
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

from app.db.engine import init_db
from app.api import auth, threads, execute, files, configs, monitoring, agents, projects, scheduled_tasks, internal, extensions
from app.services.scheduler import get_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# CORS origins — 从环境变量读取，默认仅允许前端开发服务器
_CORS_ORIGINS = [
    o.strip() for o in
    os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info("App 服务启动中...")
    await init_db()
    logger.info("数据库初始化完成")
    scheduler = get_scheduler()
    await scheduler.start()
    yield
    await scheduler.shutdown()
    logger.info("App 服务关闭")


def create_app() -> FastAPI:
    app = FastAPI(
        title="MultiAgent Studio - App Service",
        description="多Agent协作工作台业务服务",
        version="1.0.0",
        lifespan=lifespan,
    )

    # CORS — allow_origins 必须为具体列表，不可与 allow_credentials=True 同时使用 "*"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册路由
    app.include_router(auth.router, prefix="/api/auth", tags=["认证"])
    app.include_router(threads.router, prefix="/api/threads", tags=["会话"])
    app.include_router(execute.router, prefix="/api/execute", tags=["执行"])
    app.include_router(files.router, prefix="/api/files", tags=["文件"])
    app.include_router(configs.router, prefix="/api/configs", tags=["配置"])
    app.include_router(monitoring.router, prefix="/api/monitoring", tags=["监控"])
    app.include_router(agents.router, prefix="/api/v1/agents", tags=["Agent管理"])
    app.include_router(projects.router, prefix="/api/v1/projects", tags=["项目管理"])
    app.include_router(scheduled_tasks.router, prefix="/api/scheduled-tasks", tags=["定时任务"])
    app.include_router(internal.router, prefix="/api/internal", tags=["内部接口"])
    app.include_router(extensions.router, prefix="/api/extensions", tags=["扩展管理"])

    # 健康检查
    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "app"}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
