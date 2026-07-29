"""TeamTaskStore — 持久化任务板，支持依赖解析和原子更新。

任务板以 JSON 文件存储:
    {data_root}/users/{user_id}/projects/{project_id}/tasks.json

支持:
- CRUD 操作
- 依赖 DAG 解析 (get_ready_tasks)
- 原子更新 (读-改-写 + 文件锁)
- 依赖环检测
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from harness.config.paths import get_paths
from harness.team.models import TeamTask, TeamTaskStatus

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TeamTaskStore:
    """持久化任务板。

    使用文件锁 (fcntl.flock) 保证并发安全。每次写操作都是全量覆盖。
    """

    def __init__(self, project_id: str, user_id: str = "default", thread_id: str = "") -> None:
        self._project_id = project_id
        self._user_id = user_id
        self._thread_id = thread_id
        paths = get_paths()
        self._tasks_dir = (
            paths.base_dir / "users" / user_id / "projects" /
            project_id / "threads" / thread_id
        )
        self._tasks_dir.mkdir(parents=True, exist_ok=True)
        self._file = self._tasks_dir / "tasks.json"
        # 内存缓存（受文件锁保护）
        self._cache: list[TeamTask] | None = None
        self._cache_mtime: float = 0.0

    # ------------------------------------------------------------------
    # 内部: 文件 I/O + 锁
    # ------------------------------------------------------------------

    def _read(self) -> list[dict[str, Any]]:
        """读取原始任务列表（不加锁 — 由调用方负责）."""
        if not self._file.exists():
            return []
        try:
            with open(self._file, encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return []
                data = json.loads(content)
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read tasks file %s: %s", self._file, exc)
        return []

    def _write(self, tasks: list[dict[str, Any]]) -> None:
        """写入原始任务列表（不加锁 — 由调用方负责）."""
        with open(self._file, "w", encoding="utf-8") as f:
            json.dump(tasks, f, indent=2, ensure_ascii=False)

    def _load_locked(self) -> list[TeamTask]:
        """在持有锁的情况下重新加载任务."""
        raw = self._read()
        tasks = [TeamTask(**r) for r in raw]
        self._cache = tasks
        self._cache_mtime = self._file.stat().st_mtime if self._file.exists() else 0.0
        return tasks

    def _save_locked(self, tasks: list[TeamTask]) -> None:
        """在持有锁的情况下保存任务列表."""
        raw = [t.model_dump() for t in tasks]
        self._write(raw)
        # 更新缓存
        self._cache = deepcopy(tasks)
        self._cache_mtime = self._file.stat().st_mtime

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def load_tasks(self) -> list[TeamTask]:
        """加载所有任务（带缓存）."""
        # 文件不存在时直接返回空列表，避免 "a+" 创建空文件
        if not self._file.exists():
            return []
        mtime = self._file.stat().st_mtime
        if self._cache is not None and mtime == self._cache_mtime:
            return list(self._cache)
        # 需要重新加载
        with open(self._file, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                raw = self._read()
                tasks = [TeamTask(**r) for r in raw]
                self._cache = tasks
                self._cache_mtime = mtime
                return list(tasks)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    async def get_task(self, task_id: str) -> TeamTask | None:
        """按 ID 获取单个任务."""
        tasks = await self.load_tasks()
        for t in tasks:
            if t.id == task_id:
                return t
        return None

    async def create_task(
        self,
        title: str,
        description: str = "",
        assigned_agent: str | None = None,
        dependencies: list[str] | None = None,
        priority: str = "medium",
        origin: str = "team",
    ) -> TeamTask:
        """创建新任务. origin: "team"=团队运行产生 | "user"=用户手工创建."""
        task = TeamTask(
            id=str(uuid.uuid4())[:8],
            project_id=self._project_id,
            title=title,
            description=description,
            status=TeamTaskStatus.PENDING,
            assigned_agent=assigned_agent,
            dependencies=dependencies or [],
            priority=priority,
            origin=origin,
        )
        task.created_at = _now_iso()
        task.updated_at = _now_iso()

        with open(self._file, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                tasks = self._load_locked()
                tasks.append(task)
                self._save_locked(tasks)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

        logger.info("Task created: id=%s title=%s", task.id, task.title)
        return task

    async def update_task(self, task_id: str, **fields: Any) -> TeamTask | None:
        """更新任务字段 (自动将 status 字符串转为枚举，消除 Pydantic 序列化警告)."""
        # 防御：字符串 status → TeamTaskStatus 枚举
        if "status" in fields and isinstance(fields["status"], str):
            fields["status"] = TeamTaskStatus(fields["status"])

        result: TeamTask | None = None

        with open(self._file, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                tasks = self._load_locked()
                for t in tasks:
                    if t.id == task_id:
                        for key, value in fields.items():
                            if hasattr(t, key):
                                setattr(t, key, value)
                        t.updated_at = _now_iso()
                        result = t
                        break
                if result is not None:
                    self._save_locked(tasks)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

        if result is not None:
            logger.info("Task updated: id=%s fields=%s", task_id, list(fields.keys()))
        return result

    async def atomic_update(
        self, task_id: str, update_fn: Callable[[TeamTask], TeamTask | None],
    ) -> TeamTask | None:
        """原子更新：读-改-写，返回更新后的任务."""
        result: TeamTask | None = None

        with open(self._file, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                tasks = self._load_locked()
                for i, t in enumerate(tasks):
                    if t.id == task_id:
                        updated = update_fn(t)
                        if updated is not None:
                            updated.updated_at = _now_iso()
                            tasks[i] = updated
                            result = updated
                        break
                if result is not None:
                    self._save_locked(tasks)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

        return result

    async def claim(self, task_id: str, agent_name: str) -> TeamTask | None:
        """原子认领任务 — 消除多成员并发认领/派单的竞态 (CAS).

        认领条件 (全部在 flock 锁内校验, 单一收口兜底所有并发写):
        ├─ PENDING: 未分配或已分配给认领者, 且依赖全部 is_success
        └─ REVISION_NEEDED: 仅原成员可认领 (拿回修改)

        返回认领成功后的任务; 条件不满足返回 None (= 已被他人拿走/不可认领).
        """
        tasks_snapshot = await self.load_tasks()
        success_ids = {t.id for t in tasks_snapshot if t.status.is_success}

        def _do(t: TeamTask) -> TeamTask | None:
            # ── REVISION_NEEDED → IN_PROGRESS (仅原成员) ──
            if t.status == TeamTaskStatus.REVISION_NEEDED:
                if t.assigned_agent != agent_name:
                    return None
                t.status = TeamTaskStatus.IN_PROGRESS
                return t

            # ── PENDING → IN_PROGRESS ──
            if t.status != TeamTaskStatus.PENDING:
                return None
            if t.assigned_agent is not None and t.assigned_agent != agent_name:
                return None
            if not all(dep in success_ids for dep in t.dependencies):
                return None
            t.assigned_agent = agent_name
            t.status = TeamTaskStatus.IN_PROGRESS
            return t

        return await self.atomic_update(task_id, _do)

    async def propagate_failures(self) -> list[TeamTask]:
        """级联取消: 依赖中含 FAILED/CANCELLED(终态)的 PENDING 任务 → CANCELLED.

        在 flock 内迭代至不动点 (取消本身会成为下游的新原因, 沿依赖链传播).
        返回本次被取消的任务列表 (调用方负责发 SSE/通知).

        安全性: 崩溃回收是先把任务回滚为 PENDING 重试、重试耗尽才置 FAILED,
        所以传播只发生在"终局失败"之后, 不会误杀正在重试的任务的下游.
        """
        cancelled: list[TeamTask] = []

        with open(self._file, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                tasks = self._load_locked()
                failed_ids = {
                    t.id for t in tasks
                    if t.status in (TeamTaskStatus.FAILED, TeamTaskStatus.CANCELLED)
                }
                while True:
                    changed = False
                    for t in tasks:
                        if t.status != TeamTaskStatus.PENDING:
                            continue
                        bad_deps = [d for d in t.dependencies if d in failed_ids]
                        if bad_deps:
                            t.status = TeamTaskStatus.CANCELLED
                            t.error = f"依赖任务失败/取消: {', '.join(bad_deps)}"
                            t.updated_at = _now_iso()
                            cancelled.append(t)
                            failed_ids.add(t.id)  # 级联: 取消也是下游的失败原因
                            changed = True
                    if not changed:
                        break
                if cancelled:
                    self._save_locked(tasks)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

        for t in cancelled:
            logger.warning("Task '%s' cancelled (依赖失败传播): %s", t.id, t.error)
        return cancelled

    async def list_tasks(
        self,
        status: TeamTaskStatus | None = None,
        assigned_agent: str | None = None,
    ) -> list[TeamTask]:
        """列出所有任务（可按状态和分配过滤）."""
        tasks = await self.load_tasks()
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        if assigned_agent is not None:
            tasks = [t for t in tasks if t.assigned_agent == assigned_agent]
        return tasks

    # ------------------------------------------------------------------
    # 依赖解析
    # ------------------------------------------------------------------

    async def get_ready_tasks(self) -> list[TeamTask]:
        """返回所有依赖已满足的就绪任务.

        "ready" 条件:
        - PENDING: 所有 dependencies 中的任务状态均为 is_success
        - REVISION_NEEDED: 所有 dependencies 中的任务状态均为 is_success (成员拿回修改)
        - IN_REVIEW 不返回 (等待 Lead 审查)
        """
        tasks = await self.load_tasks()
        success_ids = {t.id for t in tasks if t.status.is_success}
        ready: list[TeamTask] = []
        for t in tasks:
            if t.status == TeamTaskStatus.IN_REVIEW:
                continue
            if t.status not in (TeamTaskStatus.PENDING, TeamTaskStatus.REVISION_NEEDED):
                continue
            if all(dep in success_ids for dep in t.dependencies):
                ready.append(t)
        return ready

    async def get_unclaimed_tasks(self) -> list[TeamTask]:
        """ 返回所有未被认领且依赖已满足的任务.

        认领条件:
        - status == PENDING (不包含 REVISION_NEEDED, 它有 assigned_agent)
        - assigned_agent is None (无 owner)
        - 所有 dependencies 均为 is_success
        """
        tasks = await self.load_tasks()
        success_ids = {t.id for t in tasks if t.status.is_success}
        unclaimed: list[TeamTask] = []
        for t in tasks:
            if t.status != TeamTaskStatus.PENDING:
                continue
            if t.assigned_agent is not None:
                continue
            if not all(dep in success_ids for dep in t.dependencies):
                continue
            unclaimed.append(t)
        return unclaimed

    async def check_circular_dependency(self) -> list[list[str]]:
        """检测依赖图中的环。返回所有检测到的环."""
        tasks = await self.load_tasks()
        task_map: dict[str, list[str]] = {}
        for t in tasks:
            task_map[t.id] = list(t.dependencies)

        # DFS 检测环
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {tid: WHITE for tid in task_map}
        cycles: list[list[str]] = []

        def dfs(node: str, path: list[str]) -> None:
            color[node] = GRAY
            path.append(node)
            for dep in task_map.get(node, []):
                if dep not in color:
                    continue  # 外部引用，跳过
                if color[dep] == GRAY:
                    # 找到环
                    cycle_start = path.index(dep)
                    cycles.append(path[cycle_start:] + [dep])
                elif color[dep] == WHITE:
                    dfs(dep, path)
            path.pop()
            color[node] = BLACK

        for tid in task_map:
            if color[tid] == WHITE:
                dfs(tid, [])

        return cycles

    async def recover_orphaned_tasks(self, active_teammates: set[str]) -> list[TeamTask]:
        """回收无主 IN_PROGRESS 任务 (上一个 team run 崩溃后遗留).

        仅在 initialize() 中调用一次, 不替代 Lead 的决策权。
        只处理 IN_PROGRESS 且 assigned_agent 不在活跃 teammate 列表中的任务:
        - retry_count < max_retries → INTERRUPTED (保留 assigned_agent, 等待原成员恢复)
        - retry_count >= max_retries → CANCELLED (已达重试上限)

        PENDING / IN_REVIEW / REVISION_NEEDED 等 Lead 在 triage 阶段感知并自行决策,
        不做任何自动处理。
        """
        recovered: list[TeamTask] = []

        with open(self._file, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                tasks = self._load_locked()
                changed = False
                for t in tasks:
                    if t.origin != "team" or t.status != TeamTaskStatus.IN_PROGRESS:
                        continue
                    if t.assigned_agent and t.assigned_agent in active_teammates:
                        continue  # 成员还在, 不是孤儿
                    t.retry_count += 1
                    if t.retry_count < t.max_retries:
                        t.status = TeamTaskStatus.INTERRUPTED
                        t.error = "上次团队运行中断, 等待原成员恢复"
                        t.updated_at = _now_iso()
                    else:
                        t.status = TeamTaskStatus.CANCELLED
                        t.error = f"中断恢复失败: 已达最大重试次数 ({t.max_retries})"
                        t.updated_at = _now_iso()
                    recovered.append(t)
                    changed = True
                if changed:
                    self._save_locked(tasks)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

        for t in recovered:
            logger.info("Orphaned task recovered: id=%s status=%s retry=%d/%d",
                        t.id, t.status.value, t.retry_count, t.max_retries)
        return recovered

    async def clear_all(self) -> int:
        """清空所有任务（每次新 Team 运行时调用，避免旧结果混入新对话）。返回清除的任务数。"""
        count = 0
        with open(self._file, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                tasks = self._load_locked()
                count = len(tasks)
                self._save_locked([])
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        if count:
            logger.info("Cleared all %d tasks from %s", count, self._file)
        return count

    async def delete_task(self, task_id: str) -> bool:
        """删除任务."""
        with open(self._file, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                tasks = self._load_locked()
                new_tasks = [t for t in tasks if t.id != task_id]
                if len(new_tasks) == len(tasks):
                    return False
                self._save_locked(new_tasks)
                return True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
