"""Harness API route definitions."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from harness.agents.presets import PRESET_SUBAGENTS
from harness.api.server import HarnessService, get_harness
from harness.models import (
    ClarificationResponse,
    CreateAgentRequest,
    ExecuteRequest,
    SubAgentConfig,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@router.post("/execute")
async def execute(
    request: ExecuteRequest,
    harness: HarnessService = Depends(get_harness),
):
    """Execute an agent task with SSE streaming output."""

    async def event_stream():
        async for event in harness.execute(
            thread_id=request.thread_id,
            user_id=request.user_id,
            message=request.message,
            graph=request.execution_graph,
        ):
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/execute/{thread_id}/respond")
async def respond_clarification(
    thread_id: str,
    request: ClarificationResponse,
    harness: HarnessService = Depends(get_harness),
):
    """Respond to a pending clarification request — streams resumed execution."""

    async def event_stream():
        async for event in harness.respond_to_clarification(
            thread_id=thread_id,
            clarification_id=request.clarification_id,
            answer=request.answer,
        ):
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/stop/{thread_id}")
async def stop_execution(
    thread_id: str,
    harness: HarnessService = Depends(get_harness),
):
    """Cancel a running execution."""
    await harness.stop(thread_id)
    return {"status": "stopped", "thread_id": thread_id}


@router.get("/status/{thread_id}")
async def get_status(
    thread_id: str,
    harness: HarnessService = Depends(get_harness),
):
    """Return the current execution status of a thread."""
    return await harness.get_status(thread_id)


# ---------------------------------------------------------------------------
# Agent management
# ---------------------------------------------------------------------------


@router.post("/agents")
async def create_agent(
    request: CreateAgentRequest,
    harness: HarnessService = Depends(get_harness),
):
    """Create a new SubAgent."""
    agent = await harness.subagent_manager.create(request.config)
    return {"id": request.config.name, "status": "created"}


@router.get("/agents")
async def list_agents(
    harness: HarnessService = Depends(get_harness),
):
    """List all registered SubAgents."""
    return harness.subagent_manager.list()


@router.delete("/agents/{name}")
async def delete_agent(
    name: str,
    harness: HarnessService = Depends(get_harness),
):
    """Delete a SubAgent by name."""
    await harness.subagent_manager.delete(name)
    return {"status": "deleted", "name": name}


@router.get("/agents/presets")
async def get_preset_agents():
    """Return predefined SubAgent templates."""
    return PRESET_SUBAGENTS


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


@router.get("/traces/{thread_id}")
async def get_trace(
    thread_id: str,
    harness: HarnessService = Depends(get_harness),
):
    """Get trace details for a thread."""
    return harness.observability.get_trace(thread_id)


@router.get("/metrics/token-usage")
async def get_token_usage(
    user_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    harness: HarnessService = Depends(get_harness),
):
    """Get token consumption statistics."""
    return harness.observability.get_token_usage(user_id, start_date, end_date)


# ---------------------------------------------------------------------------
# Tool Groups
# ---------------------------------------------------------------------------


@router.get("/tool-groups")
async def get_tool_groups(
    harness: HarnessService = Depends(get_harness),
):
    """Return the available tool groups."""
    return harness.tool_registry.setup_tool_groups()
