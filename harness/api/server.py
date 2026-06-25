"""FastAPI application server for the Harness service."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)

# Harness 通常是内部服务，CORS origins 从环境变量读取
_HARNESS_CORS_ORIGINS = [
    o.strip() for o in
    os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]

# ---------------------------------------------------------------------------
# HarnessService — base interface (implemented by main.HarnessService)
# ---------------------------------------------------------------------------


class HarnessService:
    """Base class for the Harness service (implemented by main.py)."""

    tool_registry: Any = None
    subagent_manager: Any = None
    observability: Any = None

    async def initialize(self) -> None:
        raise NotImplementedError

    async def shutdown(self) -> None:
        pass

    async def execute(self, thread_id: str, user_id: str, message: str, graph: Any = None):
        raise NotImplementedError
        yield  # type: ignore[unreachable]

    async def respond_to_clarification(
        self, thread_id: str, clarification_id: str, answer: str
    ):
        raise NotImplementedError

    async def stop(self, thread_id: str):
        raise NotImplementedError

    async def get_status(self, thread_id: str):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

_harness_instance: HarnessService | None = None


def set_harness(svc: HarnessService) -> None:
    """Store the Harness service singleton for DI access."""
    global _harness_instance
    _harness_instance = svc


def get_harness() -> HarnessService:
    """FastAPI dependency — return the initialized Harness service."""
    if _harness_instance is None:
        raise RuntimeError("Harness service has not been initialized")
    return _harness_instance


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Harness API server starting up...")
        yield
        logger.info("Harness API server shutting down...")
        svc = _harness_instance
        if svc is not None and hasattr(svc, "shutdown"):
            await svc.shutdown()

    app = FastAPI(
        title="Multi-Agent Workbench Harness",
        description="多 Agent 协作工作台后端 Harness 服务",
        version="2.0.0",
        lifespan=lifespan,
    )

    # CORS — allow_origins 必须为具体列表，不可与 allow_credentials=True 同时使用 "*"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_HARNESS_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from harness.api.routers import router

    app.include_router(router)

    return app


app = create_app()
