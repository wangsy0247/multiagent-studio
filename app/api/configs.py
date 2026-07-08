"""
配置 API 路由: 用户配置、预设、工具组
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.engine import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.configuration import UserConfig
from app.services.harness_client import get_harness_client, HarnessUnavailableError

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def get_config(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取用户配置"""
    result = await db.execute(
        select(UserConfig).where(UserConfig.user_id == current_user.id)
    )
    config = result.scalar_one_or_none()

    if config is None:
        # 返回默认配置
        config = UserConfig(user_id=current_user.id)
        db.add(config)
        await db.commit()
        await db.refresh(config)

    return {
        "id": str(config.id),
        "default_model": config.default_model,
        "tools_enabled": config.tools_enabled,
        "mcp_config": config.mcp_config,
        "max_concurrent_subagents": config.max_concurrent_subagents,
    }


@router.put("")
async def update_config(
    req: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新用户配置"""
    result = await db.execute(
        select(UserConfig).where(UserConfig.user_id == current_user.id)
    )
    config = result.scalar_one_or_none()

    if config is None:
        config = UserConfig(user_id=current_user.id)
        db.add(config)

    # 更新字段
    if "default_model" in req:
        config.default_model = req["default_model"]
    if "tools_enabled" in req:
        config.tools_enabled = req["tools_enabled"]
    if "mcp_config" in req:
        config.mcp_config = req["mcp_config"]
    if "max_concurrent_subagents" in req:
        config.max_concurrent_subagents = req["max_concurrent_subagents"]

    config.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    await db.refresh(config)

    return {
        "id": str(config.id),
        "default_model": config.default_model,
        "tools_enabled": config.tools_enabled,
        "mcp_config": config.mcp_config,
        "max_concurrent_subagents": config.max_concurrent_subagents,
    }


@router.get("/presets")
async def get_presets(current_user: User = Depends(get_current_user)):
    """获取预设的 SubAgent 类型列表 (代理到 Harness)"""
    harness = get_harness_client()
    try:
        return await harness.get_presets()
    except HarnessUnavailableError:
        # 返回硬编码的预设作为降级
        return [
            {"name": "researcher", "display_name": "信息检索专家", "description": "搜索网络和文献"},
            {"name": "coder", "display_name": "代码执行专家", "description": "执行 Python/Shell 代码"},
            {"name": "analyst", "display_name": "数据分析专家", "description": "数据分析和可视化"},
            {"name": "writer", "display_name": "文档撰写专家", "description": "撰写技术文档"},
            {"name": "reviewer", "display_name": "审查专家", "description": "代码和文档审查"},
        ]


@router.get("/tool-groups")
async def get_tool_groups(current_user: User = Depends(get_current_user)):
    """获取可用工具组 (代理到 Harness)"""
    harness = get_harness_client()
    try:
        return await harness.get_tool_groups()
    except HarnessUnavailableError:
        return []
