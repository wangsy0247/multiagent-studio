"""TeamTaskStore — 持久化任务板，支持依赖解析和原子更新。

任务板以 JSON 文件存储:
    {data_root}/users/{user_id}/team_tasks/{project_id}.json

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

    def __init__(self, project_id: str, user_id: str = "default") -> None:
        self._project_id = project_id
        self._user_id = user_id
        paths = get_paths()
        self._tasks_dir = paths.base_dir / "users" / user_id / "team_tasks"
        self._tasks_dir.mkdir(parents=True, exist_ok=True)
        self._file = self._tasks_dir / f"{project_id}.json"
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
                data = json.load(f)
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
        if self._file.exists():
            self._cache_mtime = self._file.stat().st_mtime

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def load_tasks(self) -> list[TeamTask]:
        """加载所有任务（带缓存）."""
        if self._file.exists():
            mtime = self._file.stat().st_mtime
            if self._cache is not None and mtime == self._cache_mtime:
                return list(self._cache)
        # 需要重新加载
        with open(self._file, "a+") as f:  # a+ 确保文件存在
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                raw = self._read()
                tasks = [TeamTask(**r) for r in raw]
                self._cache = tasks
                self._cache_mtime = self._file.stat().st_mtime if self._file.exists() else 0.0
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
    ) -> TeamTask:
        """创建新任务."""
        task = TeamTask(
            id=str(uuid.uuid4())[:8],
            project_id=self._project_id,
            title=title,
            description=description,
            status=TeamTaskStatus.PENDING,
            assigned_agent=assigned_agent,
            dependencies=dependencies or [],
            priority=priority,
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
        """更新任务字段."""
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
        """返回所有依赖已满足的待办任务.

        "ready" 条件:
        - status == PENDING
        - 所有 dependencies 中的任务状态均为 COMPLETED
        """
        tasks = await self.load_tasks()
        completed_ids = {t.id for t in tasks if t.status == TeamTaskStatus.COMPLETED}
        ready: list[TeamTask] = []
        for t in tasks:
            if t.status != TeamTaskStatus.PENDING:
                continue
            if all(dep in completed_ids for dep in t.dependencies):
                ready.append(t)
        return ready

    async def get_blocked_tasks(self) -> list[TeamTask]:
        """返回因依赖未满足而阻塞的待办任务."""
        tasks = await self.load_tasks()
        completed_ids = {t.id for t in tasks if t.status == TeamTaskStatus.COMPLETED}
        blocked: list[TeamTask] = []
        for t in tasks:
            if t.status != TeamTaskStatus.PENDING:
                continue
            if t.dependencies and not all(dep in completed_ids for dep in t.dependencies):
                blocked.append(t)
        return blocked

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

    async def assign_task(self, task_id: str, agent_name: str) -> TeamTask | None:
        """将任务分配给指定 member，状态改为 IN_PROGRESS."""
        return await self.update_task(
            task_id,
            assigned_agent=agent_name,
            status=TeamTaskStatus.IN_PROGRESS,
            started_at=_now_iso(),
        )

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
