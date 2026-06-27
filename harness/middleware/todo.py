"""TodoMiddleware — track and manage plan-mode TODO lists."""
from __future__ import annotations

import logging
from typing import override

from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState, TodoItem

logger = logging.getLogger(__name__)


class TodoMiddleware(HarnessAgentMiddleware):
    """Detect plan-mode context loss and signal exit when all TODOs are resolved.

    - ``abefore_agent``: if plan_mode is set but the todo list is empty the
      middleware marks *context lost* so the agent can recover.
    - ``aafter_model``: if all TODOs are complete/failed it sets ``plan_mode_exit``.
    """

    name = "todo"

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    @override
    async def abefore_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        if state.get("plan_mode") and not state.get("todos"):
            logger.warning("Plan-mode context lost for thread=%s", state.get("thread_id"))
            return {"context_lost": True}
        return None

    @override
    async def aafter_model(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        todos = state.get("todos", [])
        if not todos:
            return None

        terminal = {"completed", "failed"}

        def _todo_status(t: TodoItem | dict) -> str:
            if isinstance(t, TodoItem):
                return t.status
            return t["status"]

        if all(_todo_status(t) in terminal for t in todos):
            logger.debug("All TODOs resolved — plan_mode_exit set")
            return {"plan_mode_exit": True}

        return None
