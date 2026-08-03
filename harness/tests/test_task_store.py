"""TeamTaskStore 单元测试."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from harness.team.models import TeamTask, TeamTaskStatus
from harness.team.task_store import TeamTaskStore


# ── 辅助: 允许 TeamTaskStore 使用自定义目录 ──
def _patch_create_with_dir():
    @classmethod
    def _create_with_dir(cls, base_dir: Path, project_id: str):
        store = cls.__new__(cls)
        store._project_id = project_id
        store._user_id = "default"
        store._tasks_dir = base_dir
        store._tasks_dir.mkdir(parents=True, exist_ok=True)
        store._file = base_dir / f"{project_id}.json"
        store._cache = None
        store._cache_mtime = 0.0
        return store
    TeamTaskStore._create_with_dir = _create_with_dir


_patch_create_with_dir()


def _run(async_func):
    """在同步测试中运行异步函数."""
    return asyncio.run(async_func)


@pytest.fixture
def store(tmp_path):
    """创建使用临时目录的 TeamTaskStore."""
    return TeamTaskStore._create_with_dir(tmp_path, "test_project")


def test_create_task(store):
    """创建任务并验证."""
    async def _test():
        task = await store.create_task(
            title="测试任务",
            description="这是一个测试",
            priority="high",
        )
        assert task.id
        assert task.title == "测试任务"
        assert task.status == TeamTaskStatus.PENDING
        assert task.priority == "high"

        loaded = await store.get_task(task.id)
        assert loaded is not None
        assert loaded.title == "测试任务"

    _run(_test())


def test_update_task(store):
    """更新任务."""
    async def _test():
        task = await store.create_task(title="原始标题")
        updated = await store.update_task(
            task.id, title="新标题", status=TeamTaskStatus.IN_PROGRESS,
        )
        assert updated is not None
        assert updated.title == "新标题"
        assert updated.status == TeamTaskStatus.IN_PROGRESS

    _run(_test())


def test_update_nonexistent(store):
    """更新不存在的任务返回 None."""
    async def _test():
        result = await store.update_task("nonexistent", title="x")
        assert result is None

    _run(_test())


def test_list_tasks(store):
    """列出任务."""
    async def _test():
        await store.create_task(title="Task A")
        await store.create_task(title="Task B")
        tasks = await store.list_tasks()
        assert len(tasks) == 2

        pending = await store.list_tasks(status=TeamTaskStatus.PENDING)
        assert len(pending) == 2
        in_progress = await store.list_tasks(status=TeamTaskStatus.IN_PROGRESS)
        assert len(in_progress) == 0

    _run(_test())


def test_dependency_resolution(store):
    """测试依赖解析."""
    async def _test():
        task_a = await store.create_task(title="Task A")
        task_b = await store.create_task(
            title="Task B", dependencies=[task_a.id],
        )

        # A 未完成时，B 不应就绪
        ready = await store.get_ready_tasks()
        ready_ids = {t.id for t in ready}
        assert task_a.id in ready_ids
        assert task_b.id not in ready_ids

        # 完成 A → B 应就绪
        await store.update_task(task_a.id, status=TeamTaskStatus.COMPLETED)
        ready = await store.get_ready_tasks()
        ready_ids = {t.id for t in ready}
        assert task_b.id in ready_ids

    _run(_test())


def test_circular_dependency_detection(store):
    """检测依赖环."""
    async def _test():
        task_a = await store.create_task(title="Task A")
        task_b = await store.create_task(title="Task B")

        tasks = await store.load_tasks()
        for t in tasks:
            if t.id == task_a.id:
                t.dependencies = [task_b.id]
            elif t.id == task_b.id:
                t.dependencies = [task_a.id]

        with open(store._file, "w") as f:
            json.dump([t.model_dump() for t in tasks], f)
        store._cache = None

        cycles = await store.check_circular_dependency()
        assert len(cycles) > 0

    _run(_test())
