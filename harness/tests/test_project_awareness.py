"""单 agent 项目感知 (Phase 4) 测试.

覆盖:
- DynamicContextMiddleware 的 <projects> 索引块注入 (仅单 agent)
- project_info / project_memory_search 只读工具
- 工具的只读硬约束 (调用前后 projects/ 目录无任何变化)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from harness.config.memory_config import MemoryConfig, set_memory_config, get_memory_config
from harness.config.paths import Paths, get_paths, set_paths
from harness.memory.project_index import format_projects_index, list_projects
from harness.memory.task_memory import TaskMemory, TaskMemoryStore
from harness.middleware.dynamic_context import DynamicContextMiddleware
from harness.tools.builtins.project_tools import project_info, project_memory_search

USER = "tester"


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def data_root(tmp_path):
    """把 Paths 单例指向临时目录, 测试后恢复."""
    old = get_paths()
    set_paths(Paths(str(tmp_path)))
    yield tmp_path
    set_paths(old)


def _write_project(
    root: Path, pid: str, *, name: str = "", description: str = "",
    members: list | None = None,
) -> Path:
    """在临时 data_root 下创建新格式项目目录 + project.json."""
    pdir = root / "users" / USER / "projects" / pid
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "project.json").write_text(
        json.dumps({
            "id": pid,
            "name": name or pid,
            "description": description,
            "members": members or [],
            "thread_count": 1,
            "task_count": 2,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return pdir


def _snapshot(root: Path) -> dict[str, tuple[float, int, str]]:
    """快照目录下所有文件 (mtime, size, 内容), 用于只读性断言."""
    snap: dict[str, tuple[float, int, str]] = {}
    base = root / "users"
    if not base.exists():
        return snap
    for p in sorted(base.rglob("*")):
        if p.is_file():
            snap[str(p)] = (p.stat().st_mtime, p.stat().st_size, p.read_text(encoding="utf-8"))
    return snap


def _build_middleware(**kwargs) -> DynamicContextMiddleware:
    return DynamicContextMiddleware(agent_name="lead", **kwargs)


# ── <projects> 索引块注入 ─────────────────────────────────────────────────


class TestProjectsIndexInjection:
    def test_injects_projects_block(self, data_root):
        """单 agent 模式: 有项目时注入 <projects> 块 (含名称/成员)."""
        _write_project(data_root, "p1", name="商城重构", description="电商平台",
                       members=["coder", "reviewer"])
        mw = _build_middleware(inject_projects_index=True)
        reminder, _ = mw._build_full_reminder(user_id=USER)
        assert "<projects>" in reminder
        assert "商城重构" in reminder
        assert "p1" in reminder
        assert "coder, reviewer" in reminder

    def test_no_projects_no_block(self, data_root):
        """无项目时不注入 <projects> 块."""
        mw = _build_middleware(inject_projects_index=True)
        reminder, _ = mw._build_full_reminder(user_id=USER)
        assert "<projects>" not in reminder
        assert "<system-reminder>" in reminder  # reminder 本体仍正常

    def test_team_mode_not_injected(self, data_root):
        """Team 模式 (默认 inject_projects_index=False) 不注入 <projects> 块."""
        _write_project(data_root, "p1", name="商城重构")
        mw = _build_middleware()  # 与 teammate_middleware 一致, 不传 inject_projects_index
        reminder, _ = mw._build_full_reminder(user_id=USER)
        assert "<projects>" not in reminder

    def test_truncation_over_limit(self, data_root):
        """超过 projects_index_max 时截断并提示总数."""
        for i in range(5):
            _write_project(data_root, f"p{i}", name=f"项目{i}")
        old_cfg = get_memory_config()
        try:
            set_memory_config(MemoryConfig(projects_index_max=2))
            mw = _build_middleware(inject_projects_index=True)
            reminder, _ = mw._build_full_reminder(user_id=USER)
        finally:
            set_memory_config(old_cfg)
        assert "5 projects in total" in reminder
        # 只显示前 2 个项目
        assert reminder.count("(id: p") == 2

    def test_read_failure_degrades(self, data_root, monkeypatch):
        """项目索引读取抛异常时静默降级: 不注入块, reminder 不中断."""
        def _boom(user_id):
            raise RuntimeError("disk exploded")

        monkeypatch.setattr(
            "harness.memory.project_index.list_projects", _boom,
        )
        _write_project(data_root, "p1", name="商城重构")
        mw = _build_middleware(inject_projects_index=True)
        reminder, _ = mw._build_full_reminder(user_id=USER)
        assert "<projects>" not in reminder
        assert "<current_date>" in reminder

    def test_invalid_project_json_skipped(self, data_root):
        """单个 project.json 损坏时跳过该项目, 其余正常."""
        _write_project(data_root, "good", name="好项目")
        bad = data_root / "users" / USER / "projects" / "bad"
        bad.mkdir(parents=True, exist_ok=True)
        (bad / "project.json").write_text("{not json", encoding="utf-8")
        projects = list_projects(USER)
        assert [p["id"] for p in projects] == ["good"]


# ── format_projects_index 单元测试 ─────────────────────────────────────────


class TestFormatProjectsIndex:
    def test_empty(self):
        assert format_projects_index([]) == ""

    def test_members_dict_form(self):
        """members 兼容 [{"name": ...}] 形态."""
        block = format_projects_index([
            {"id": "p1", "name": "A", "members": [{"name": "coder"}]},
        ])
        assert "members: coder" in block


# ── project_info 工具 ──────────────────────────────────────────────────────


class TestProjectInfoTool:
    @pytest.mark.asyncio
    async def test_returns_full_info(self, data_root):
        """正常返回: 项目元数据 + AgentCard 摘要 + 团队记忆摘录."""
        pdir = _write_project(data_root, "p1", name="商城重构", description="电商平台",
                              members=["coder"])
        (pdir / "agent_card.json").write_text(json.dumps({
            "project_id": "p1",
            "cards": {
                "coder": {"name": "coder", "display_name": "码农", "role": "member",
                          "description": "写代码的"},
            },
        }, ensure_ascii=False), encoding="utf-8")
        mem_dir = pdir / "memory"
        mem_dir.mkdir()
        (mem_dir / "team_memory.json").write_text(json.dumps({
            "best_practices": [{"practice": "先跑测试再提交"}],
            "known_pitfalls": [{"pitfall": "别动生产库"}],
            "recent_runs": [],
        }, ensure_ascii=False), encoding="utf-8")

        result = await project_info.coroutine(project_id="p1", state={"user_id": USER})
        assert "商城重构" in result
        assert "码农 (coder) — member: 写代码的" in result
        assert "先跑测试再提交" in result
        assert "别动生产库" in result

    @pytest.mark.asyncio
    async def test_project_not_found(self, data_root):
        """项目不存在返回友好错误."""
        result = await project_info.coroutine(project_id="nope", state={"user_id": USER})
        assert "not found" in result
        assert "nope" in result

    @pytest.mark.asyncio
    async def test_no_user_id(self, data_root):
        """无法确定用户时返回错误."""
        result = await project_info.coroutine(project_id="p1", state={})
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_read_only_no_side_effects(self, data_root):
        """只读硬约束: 调用后 projects/ 目录无新增/修改文件."""
        _write_project(data_root, "p1", name="商城重构")
        before = _snapshot(data_root)
        await project_info.coroutine(project_id="p1", state={"user_id": USER})
        # 查询不存在的项目同样不得产生任何写
        await project_info.coroutine(project_id="ghost", state={"user_id": USER})
        after = _snapshot(data_root)
        assert before == after


# ── project_memory_search 工具 ─────────────────────────────────────────────


async def _seed_memory(pid: str, task_id: str, title: str, summary: str,
                       tags: list[str]) -> None:
    store = TaskMemoryStore(pid, USER)
    await store.save(TaskMemory(
        task_id=task_id, task_title=title, status="completed",
        summary=summary, tags=tags,
    ))


class TestProjectMemorySearchTool:
    @pytest.mark.asyncio
    async def test_hit(self, data_root):
        """有记忆命中: 返回相关任务记忆摘要."""
        _write_project(data_root, "p1", name="商城重构")
        await _seed_memory("p1", "t1", "支付网关接入", "接入了 Stripe 支付",
                           ["payment", "stripe"])
        result = await project_memory_search.coroutine(
            project_id="p1", query="payment 接入", state={"user_id": USER},
        )
        assert "支付网关接入" in result
        assert "Stripe" in result

    @pytest.mark.asyncio
    async def test_no_memory_empty_result(self, data_root):
        """无任务记忆时返回空结果提示 (且不创建 tasks 目录)."""
        _write_project(data_root, "p1", name="商城重构")
        result = await project_memory_search.coroutine(
            project_id="p1", query="payment", state={"user_id": USER},
        )
        assert "No task memories" in result
        # 只读守卫: 不得创建 memory/tasks 目录
        assert not (data_root / "users" / USER / "projects" / "p1"
                    / "memory" / "tasks").exists()

    @pytest.mark.asyncio
    async def test_no_match(self, data_root):
        """有记忆但关键词不匹配."""
        _write_project(data_root, "p1", name="商城重构")
        await _seed_memory("p1", "t1", "支付网关接入", "接入了 Stripe 支付",
                           ["payment"])
        result = await project_memory_search.coroutine(
            project_id="p1", query="kubernetes 部署", state={"user_id": USER},
        )
        assert "No task memories" in result or "matched" in result

    @pytest.mark.asyncio
    async def test_cross_project_isolation(self, data_root):
        """跨项目隔离: 查 A 项目不返回 B 项目的记忆."""
        _write_project(data_root, "pa", name="项目A")
        _write_project(data_root, "pb", name="项目B")
        await _seed_memory("pb", "t1", "支付网关接入", "B项目的Stripe方案",
                           ["payment"])
        result = await project_memory_search.coroutine(
            project_id="pa", query="payment", state={"user_id": USER},
        )
        assert "B项目的Stripe方案" not in result
        assert "支付网关接入" not in result

    @pytest.mark.asyncio
    async def test_project_not_found(self, data_root):
        result = await project_memory_search.coroutine(
            project_id="nope", query="x", state={"user_id": USER},
        )
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_read_only_no_side_effects(self, data_root):
        """只读硬约束: 检索后 projects/ 目录无新增/修改文件."""
        _write_project(data_root, "p1", name="商城重构")
        await _seed_memory("p1", "t1", "支付网关接入", "接入了 Stripe 支付",
                           ["payment"])
        before = _snapshot(data_root)
        await project_memory_search.coroutine(
            project_id="p1", query="payment", state={"user_id": USER},
        )
        after = _snapshot(data_root)
        assert before == after
