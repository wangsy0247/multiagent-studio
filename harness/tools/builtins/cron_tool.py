"""Built-in ``cron`` tool — Lead Agent 在对话中创建/管理定时任务.

单一压缩工具（参考 hermes cronjob 设计）: 一个工具承载 create/list/update/
pause/resume/remove/trigger 全部动作，减少 tool schema 的 token 开销。

- 身份（origin）: 通过 InjectedState(user_id) / RunnableConfig(thread_id) 自动捕获，
  任务归属当前会话用户，无需模型传参
- 安全闸门: 无人值守执行（定时任务）中禁止调用，防止递归调度
- 后端: 调用 App 服务的 /api/internal/scheduled-tasks（X-Internal-Token 认证）
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated, Any, Literal

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import InjectedState

logger = logging.getLogger(__name__)

APP_SERVICE_URL = os.getenv("APP_SERVICE_URL", "http://localhost:8000")
_HTTP_TIMEOUT = 15.0

# list 返回时只保留这些字段，避免长 prompt 占用上下文
_LIST_FIELDS = ("id", "name", "cron_expr", "recurring", "timezone", "next_run_at", "enabled", "last_status", "last_error")


def extract_cron_context(
    state: dict | None,
    config: RunnableConfig | None,
) -> dict[str, Any]:
    """从注入的 graph state / RunnableConfig 提取用户身份与执行模式.

    user_id 即 app 侧的 username（harness 文件系统目录统一约定）。
    unattended=True 表示当前是定时任务等无人值守执行。
    """
    user_id = ""
    unattended = False
    if state:
        user_id = state.get("user_id") or ""
        metadata = state.get("metadata") or {}
        unattended = bool(metadata.get("unattended"))
    thread_id = ""
    if config:
        thread_id = (config.get("configurable") or {}).get("thread_id", "") or ""
    return {"user_id": user_id, "thread_id": thread_id, "unattended": unattended}


async def _call_app(method: str, path: str, **kwargs) -> Any:
    """调用 App 内部接口，错误统一转为 RuntimeError 由工具层格式化"""
    token = os.getenv("INTERNAL_API_TOKEN", "")
    if not token:
        raise RuntimeError("Cron feature is not enabled (INTERNAL_API_TOKEN not configured on the server)")
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.request(
            method, f"{APP_SERVICE_URL}{path}",
            headers={"X-Internal-Token": token}, **kwargs,
        )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text[:200])
        except Exception:
            detail = resp.text[:200]
        raise RuntimeError(f"Cron service error ({resp.status_code}): {detail}")
    return resp.json()


def _slim(task: dict) -> dict:
    return {k: task.get(k) for k in _LIST_FIELDS}


def cron_tool() -> BaseTool:
    """Create the ``cron`` tool used by the Lead Agent."""

    @tool
    async def cron(
        action: Literal["create", "list", "update", "pause", "resume", "remove", "trigger"],
        job_id: str | None = None,
        name: str | None = None,
        prompt: str | None = None,
        cron_expr: str | None = None,
        run_at: str | None = None,
        delay: str | None = None,
        timezone: str | None = None,
        config: RunnableConfig = None,  # auto-injected by LangChain at call time
        state: Annotated[dict, InjectedState] = None,  # graph state (user_id, metadata)
    ) -> str:
        """Manage scheduled tasks (cron jobs) — let the Agent run on a schedule without the user being online.

        Use cases: the user asks for "do X every day at 9am", "check Y every 30 minutes", "remind me of Z in 10 minutes".

        Usage discipline (must follow):
        - Tasks run unattended in a brand-new session — the prompt must be fully self-contained; do not reference "the current conversation / what we just said"
        - Before creating, use list to check whether a similar task already exists to avoid duplicates
        - Never create a scheduled task from within a scheduled task (no recursive scheduling)
        - You must list first to obtain job_id before update/pause/resume/remove

        Time expression — provide exactly one (choose by scenario; do not estimate the current time yourself):
        - delay: relative duration, e.g. "10m", "2h", "1d", "1h30m". You MUST use it when the user says
          "remind me in N minutes/hours" — the server converts it based on its own clock; you neither need
          nor can know the current time
        - cron_expr: recurring 5-field cron expression "minute hour day month weekday" (e.g. "0 9 * * *" = every day at 9am)
        - run_at: absolute ISO time for a one-shot task (e.g. "2026-07-20T09:00:00"); use only when the user gives an explicit date/time

        Args:
            action: create | list (all tasks) | update | pause | resume | remove | trigger (run once now)
            job_id: task ID (returned by list; required for update/pause/resume/remove/trigger)
            name: task name (required for create)
            prompt: self-contained instruction sent to the Agent when the task fires (required for create)
            cron_expr: cron expression for recurring tasks (exactly one of cron_expr/run_at/delay)
            run_at: absolute trigger time for one-shot tasks, ISO format (exactly one of cron_expr/run_at/delay)
            delay: relative duration "30s"/"10m"/"2h"/"1d"/"1h30m" (exactly one of cron_expr/run_at/delay; recommended for "in N minutes")
            timezone: timezone for cron (default: Asia/Shanghai)
        """
        ctx = extract_cron_context(state, config)
        if ctx["unattended"]:
            return "Error: this is an unattended scheduled-task execution; creating or modifying scheduled tasks is forbidden (prevents recursive scheduling)."
        if not ctx["user_id"]:
            return "Error: cannot determine the owning user of the task (user_id missing)."

        username = ctx["user_id"]
        try:
            if action == "list":
                tasks = await _call_app(
                    "GET", "/api/internal/scheduled-tasks", params={"username": username}
                )
                return json.dumps([_slim(t) for t in tasks], ensure_ascii=False, default=str)

            if action == "create":
                if not name or not prompt:
                    return "Error: create requires both name and prompt."
                if sum(1 for x in (cron_expr, run_at, delay) if x) != 1:
                    return "Error: exactly one of cron_expr, run_at, or delay must be provided."
                body: dict[str, Any] = {"username": username, "name": name, "prompt": prompt}
                if cron_expr:
                    body["cron_expr"] = cron_expr
                elif run_at:
                    body["run_at"] = run_at
                else:
                    body["delay"] = delay
                if timezone:
                    body["timezone"] = timezone
                task = await _call_app("POST", "/api/internal/scheduled-tasks", json=body)
                return json.dumps({"ok": True, "task": _slim(task)}, ensure_ascii=False, default=str)

            if not job_id:
                return f"Error: {action} requires job_id (use list first to obtain it)."

            params = {"username": username}
            if action == "pause" or action == "resume":
                result = await _call_app(
                    "PATCH", f"/api/internal/scheduled-tasks/{job_id}",
                    params=params, json={"enabled": action == "resume"},
                )
                return json.dumps(_slim(result), ensure_ascii=False, default=str)
            if action == "remove":
                return json.dumps(
                    await _call_app("DELETE", f"/api/internal/scheduled-tasks/{job_id}", params=params),
                    ensure_ascii=False,
                )
            if action == "trigger":
                return json.dumps(
                    await _call_app("POST", f"/api/internal/scheduled-tasks/{job_id}/trigger", params=params),
                    ensure_ascii=False,
                )
            # update
            updates: dict[str, Any] = {}
            for key, val in (("name", name), ("prompt", prompt), ("cron_expr", cron_expr),
                             ("run_at", run_at), ("delay", delay), ("timezone", timezone)):
                if val is not None:
                    updates[key] = val
            if not updates:
                return "Error: update requires at least one field to modify."
            result = await _call_app(
                "PATCH", f"/api/internal/scheduled-tasks/{job_id}", params=params, json=updates
            )
            return json.dumps(_slim(result), ensure_ascii=False, default=str)
        except Exception as e:
            logger.warning("cron tool %s failed: %s", action, e)
            return f"Error: {e}"

    return cron
