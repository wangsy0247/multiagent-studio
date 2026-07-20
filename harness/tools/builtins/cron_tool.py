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
        raise RuntimeError("定时任务功能未启用（服务未配置 INTERNAL_API_TOKEN）")
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
        raise RuntimeError(f"定时任务服务错误 ({resp.status_code}): {detail}")
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
        """Manage scheduled tasks (定时任务) — 让 Agent 按时间表自动执行，无需用户在线。

        使用场景: 用户要求"每天早上 9 点做 X"、"每 30 分钟检查 Y"、"10 分钟后提醒我 Z"。

        使用纪律（必须遵守）:
        - 任务在无人值守的全新会话中执行 — prompt 必须完全自包含，不要引用"当前对话/刚才说的"
        - 创建前先用 list 检查是否已存在类似任务，避免重复创建
        - 禁止为定时任务再创建定时任务（递归调度）
        - update/pause/resume/remove 前必须先 list 获取 job_id

        时间表达三选一（按场景选择，不要自己估算当前时间）:
        - delay: 相对时长，如 "10m"、"2h"、"1d"、"1h30m"。用户说"N 分钟后/小时后提醒我"时【必须】用它，
          服务器会基于自己的时钟换算，你无需也无法知道当前时间
        - cron_expr: 周期任务 5 字段表达式 "分 时 日 月 星期"（如 "0 9 * * *" = 每天 9 点）
        - run_at: 一次性任务的绝对 ISO 时间（如 "2026-07-20T09:00:00"），仅在用户给出明确日期时间时用

        Args:
            action: create 创建 | list 列出全部 | update 修改 | pause 暂停 | resume 恢复 | remove 删除 | trigger 立即运行一次
            job_id: 任务 ID（list 返回；update/pause/resume/remove/trigger 必填）
            name: 任务名称（create 必填）
            prompt: 到点发给 Agent 的自包含指令（create 必填）
            cron_expr: 周期任务 cron 表达式（与 run_at/delay 三选一）
            run_at: 一次性任务绝对触发时间，ISO 格式（与 cron_expr/delay 三选一）
            delay: 一次性任务相对时长 "30s"/"10m"/"2h"/"1d"/"1h30m"（与 cron_expr/run_at 三选一，推荐用于"N 分钟后"）
            timezone: cron 所用时区（默认 Asia/Shanghai）
        """
        ctx = extract_cron_context(state, config)
        if ctx["unattended"]:
            return "错误: 当前是无人值守的定时任务执行，禁止创建或修改定时任务（防递归调度）。"
        if not ctx["user_id"]:
            return "错误: 无法确定任务归属用户（user_id 缺失）。"

        username = ctx["user_id"]
        try:
            if action == "list":
                tasks = await _call_app(
                    "GET", "/api/internal/scheduled-tasks", params={"username": username}
                )
                return json.dumps([_slim(t) for t in tasks], ensure_ascii=False, default=str)

            if action == "create":
                if not name or not prompt:
                    return "错误: create 需要提供 name 和 prompt。"
                if sum(1 for x in (cron_expr, run_at, delay) if x) != 1:
                    return "错误: cron_expr、run_at、delay 必须且只能提供一个。"
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
                return f"错误: {action} 需要 job_id（请先 list 获取）。"

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
                return "错误: update 需要提供至少一个待修改字段。"
            result = await _call_app(
                "PATCH", f"/api/internal/scheduled-tasks/{job_id}", params=params, json=updates
            )
            return json.dumps(_slim(result), ensure_ascii=False, default=str)
        except Exception as e:
            logger.warning("cron tool %s failed: %s", action, e)
            return f"错误: {e}"

    return cron
