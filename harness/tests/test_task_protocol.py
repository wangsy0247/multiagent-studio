"""Phase 2 任务协议 JSON 化测试 — TaskSpec/TaskResult/工具降级/双路径消费."""

import asyncio
import json

import pytest

from harness.team.models import TaskResult, TaskSpec, TeamTask, TeamTaskStatus
from harness.team.task_store import TeamTaskStore
from harness.team.tools import (
    _build_spec,
    _parse_task_result,
    create_team_tools,
    set_current_agent,
)
from harness.team.orchestrator import TeamOrchestrator


# ── 辅助: 与 test_task_store.py 相同的自定义目录补丁 ──
def _patch_create_with_dir():
    if hasattr(TeamTaskStore, "_create_with_dir"):
        return

    @classmethod
    def _create_with_dir(cls, base_dir, project_id):
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
    return asyncio.run(async_func)


@pytest.fixture
def store(tmp_path):
    return TeamTaskStore._create_with_dir(tmp_path, "test_project")


def _tools(store, role="member", teammates=None):
    """构建工具集并按名字索引."""
    tools = create_team_tools(
        task_store=store, role=role,
        teammates=teammates if teammates is not None else {"worker": object()},
    )
    return {t.name: t for t in tools}


# ═════════════════════════════════════════════════════════════════
# 1. 旧格式 tasks.json 加载兼容
# ═════════════════════════════════════════════════════════════════

def test_legacy_tasks_json_without_spec_result_loads(store):
    """历史 tasks.json (无 spec/result 字段) 必须正常加载, spec/result 为 None."""
    legacy = [
        {
            "id": "legacy01",
            "project_id": "test_project",
            "title": "历史任务",
            "description": "旧格式纯文本描述",
            "status": "completed",
            "assigned_agent": "worker",
            "dependencies": [],
            "priority": "medium",
            "output": "旧产出文本",
            "error": None,
            "retry_count": 0,
            "max_retries": 3,
            "revision_count": 0,
            "review_feedback": "",
            "origin": "team",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    store._file.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    async def _test():
        tasks = await store.load_tasks()
        assert len(tasks) == 1
        t = tasks[0]
        assert t.spec is None
        assert t.result is None
        assert t.effective_output() == "旧产出文本"
        # 回写不丢旧字段, 且新字段以 None 序列化
        await store.update_task(t.id, output="更新产出")
        reloaded = await store.get_task(t.id)
        assert reloaded.effective_output() == "更新产出"

    _run(_test())


def test_task_with_spec_roundtrip(store):
    """含 spec/result 的任务持久化后能完整读回."""
    async def _test():
        spec = TaskSpec(goal="目标", constraints=["约束1"], acceptance_criteria=["标准1"])
        task = await store.create_task(title="结构化任务", spec=spec)
        result = TaskResult(output="成果", evidence=["file.py"], uncertainty="medium")
        await store.update_task(task.id, result=result)

        loaded = await store.get_task(task.id)
        assert loaded.spec is not None
        assert loaded.spec.goal == "目标"
        assert loaded.spec.constraints == ["约束1"]
        assert loaded.result is not None
        assert loaded.result.evidence == ["file.py"]
        assert loaded.result.uncertainty == "medium"
        assert loaded.effective_output() == "成果"

    _run(_test())


# ═════════════════════════════════════════════════════════════════
# 2. delegate_to_member: 结构化 spec / 纯文本降级
# ═════════════════════════════════════════════════════════════════

def test_delegate_with_full_spec_creates_structured_task(store):
    """delegate 传完整 spec 字段 → 任务含 spec, 描述含渲染文本与提交要求."""
    async def _test():
        tools = _tools(store, role="lead")
        out = await tools["delegate_to_member"].ainvoke({
            "agent_name": "worker",
            "title": "实现登录接口",
            "goal": "交付可用的 JWT 登录 API",
            "background": "项目使用 FastAPI",
            "constraints": ["不得引入新依赖", "遵循现有代码风格"],
            "acceptance_criteria": ["pytest 全绿"],
            "priority": "high",
        })
        assert "Created and delegated task" in out

        tasks = await store.load_tasks()
        assert len(tasks) == 1
        t = tasks[0]
        assert t.assigned_agent == "worker"
        assert t.priority == "high"
        assert t.spec is not None
        assert t.spec.goal == "交付可用的 JWT 登录 API"
        assert t.spec.constraints == ["不得引入新依赖", "遵循现有代码风格"]
        assert t.spec.acceptance_criteria == ["pytest 全绿"]
        # 描述渲染自 spec + 提交要求 (引导 result JSON)
        assert "[Goal]" in t.description
        assert "[Submission Requirement]" in t.description
        assert "result" in t.description

    _run(_test())


def test_delegate_plain_text_degrades_gracefully(store):
    """delegate 只传非结构化 description → 降级纯文本, 不报错, 照常创建."""
    async def _test():
        tools = _tools(store, role="lead")
        out = await tools["delegate_to_member"].ainvoke({
            "agent_name": "worker",
            "title": "随便看看",
            "description": "帮忙看一下 README 是否最新",
        })
        assert "Error" not in out
        assert "Created and delegated task" in out

        tasks = await store.load_tasks()
        assert len(tasks) == 1
        t = tasks[0]
        assert t.spec is None  # 纯文本降级路径不生成 spec
        assert "帮忙看一下 README 是否最新" in t.description
        assert "[Submission Requirement]" in t.description

    _run(_test())


def test_delegate_unknown_member_still_creates_with_warning(store):
    """成员不存在 → 警告但任务仍创建."""
    async def _test():
        tools = _tools(store, role="lead", teammates={"other": object()})
        out = await tools["delegate_to_member"].ainvoke({
            "agent_name": "ghost",
            "title": "任务",
            "goal": "目标",
        })
        assert "⚠️" in out
        assert "ghost" in out
        assert len(await store.load_tasks()) == 1

    _run(_test())


# ═════════════════════════════════════════════════════════════════
# 3. task_update: result JSON 存储 / 非法 JSON 降级
# ═════════════════════════════════════════════════════════════════

def _make_in_progress_task(store):
    async def _make():
        task = await store.create_task(title="任务", assigned_agent="worker")
        await store.update_task(task.id, status=TeamTaskStatus.IN_PROGRESS)
        return task
    return _run(_make())


def test_task_update_with_result_json(store):
    """member 提交合法 result JSON → 结构化存储, 旧 output 字段同步."""
    task = _make_in_progress_task(store)
    set_current_agent("worker")

    async def _test():
        tools = _tools(store, role="member")
        out = await tools["task_update"].ainvoke({
            "task_id": task.id,
            "status": "in_review",
            "result": json.dumps({
                "output": "完成了登录接口",
                "evidence": ["app/api/auth.py", "pytest: 12 passed"],
                "uncertainty": "medium",
            }, ensure_ascii=False),
        })
        assert "updated" in out

        t = await store.get_task(task.id)
        assert t.status == TeamTaskStatus.IN_REVIEW
        assert t.result is not None
        assert t.result.output == "完成了登录接口"
        assert t.result.evidence == ["app/api/auth.py", "pytest: 12 passed"]
        assert t.result.uncertainty == "medium"
        assert t.result.status == "in_review"  # 自动回填任务状态
        # 旧字段同步, 未适配 result 的下游仍能读到
        assert t.output == "完成了登录接口"

    _run(_test())


def test_task_update_with_invalid_result_degrades_to_output(store):
    """非法 result JSON → 不报错, 整个字符串降级为纯文本 output."""
    task = _make_in_progress_task(store)
    set_current_agent("worker")

    async def _test():
        tools = _tools(store, role="member")
        out = await tools["task_update"].ainvoke({
            "task_id": task.id,
            "status": "in_review",
            "result": "这不是 JSON, 就是一段成果说明",
        })
        assert "Error" not in out

        t = await store.get_task(task.id)
        assert t.result is None
        assert t.output == "这不是 JSON, 就是一段成果说明"

    _run(_test())


def test_task_update_failed_with_failure_reason(store):
    """failed 时 result.failure_reason 同步到 error 字段."""
    task = _make_in_progress_task(store)
    set_current_agent("worker")

    async def _test():
        tools = _tools(store, role="member")
        await tools["task_update"].ainvoke({
            "task_id": task.id,
            "status": "failed",
            "result": json.dumps({"failure_reason": "依赖服务不可用"}, ensure_ascii=False),
        })
        t = await store.get_task(task.id)
        assert t.status == TeamTaskStatus.FAILED
        assert t.result is not None
        assert t.result.failure_reason == "依赖服务不可用"
        assert t.error == "依赖服务不可用"
        assert t.effective_failure_reason() == "依赖服务不可用"

    _run(_test())


def test_task_update_plain_output_still_works(store):
    """旧用法 (纯 output 文本) 行为不变."""
    task = _make_in_progress_task(store)
    set_current_agent("worker")

    async def _test():
        tools = _tools(store, role="member")
        out = await tools["task_update"].ainvoke({
            "task_id": task.id,
            "status": "completed",
            "output": "旧式纯文本产出",
        })
        assert "updated" in out
        t = await store.get_task(task.id)
        assert t.result is None
        assert t.output == "旧式纯文本产出"
        assert t.effective_output() == "旧式纯文本产出"

    _run(_test())


# ═════════════════════════════════════════════════════════════════
# 4. synthesize 双路径 (有 result / 无 result)
# ═════════════════════════════════════════════════════════════════

def _bare_orchestrator(store):
    """构造只够跑 _synthesize_results 的裸 orchestrator."""
    orch = TeamOrchestrator.__new__(TeamOrchestrator)
    orch.task_store = store
    orch._stale_terminal_ids = set()
    orch._thread_id = "test-thread"
    return orch


async def _collect_synthesis(orch):
    events = []
    async for ev in orch._synthesize_results():
        events.append(ev)
    return events


def test_synthesize_with_structured_result(store):
    """有 result: 汇总取 result.output, 展示 evidence, 失败取 failure_reason."""
    async def _test():
        t1 = await store.create_task(title="成功任务", assigned_agent="worker")
        await store.update_task(
            t1.id,
            status=TeamTaskStatus.COMPLETED,
            result=TaskResult(
                output="结构化成果文本",
                evidence=["src/main.py"],
                uncertainty="low",
            ),
        )
        t2 = await store.create_task(title="失败任务", assigned_agent="worker")
        await store.update_task(
            t2.id,
            status=TeamTaskStatus.FAILED,
            result=TaskResult(failure_reason="结构化失败原因"),
        )

        events = await _collect_synthesis(_bare_orchestrator(store))
        contents = "\n".join(ev["content"] for ev in events)
        assert "结构化成果文本" in contents
        assert "src/main.py" in contents  # evidence 展示
        assert "结构化失败原因" in contents

    _run(_test())


def test_synthesize_without_result_falls_back(store):
    """无 result: 回退旧 output/error 字段, 汇总行为与现状一致."""
    async def _test():
        t1 = await store.create_task(title="旧成功任务", assigned_agent="worker")
        await store.update_task(
            t1.id, status=TeamTaskStatus.COMPLETED, output="旧式产出文本",
        )
        t2 = await store.create_task(title="旧失败任务", assigned_agent="worker")
        await store.update_task(
            t2.id, status=TeamTaskStatus.FAILED, error="旧式错误原因",
        )

        events = await _collect_synthesis(_bare_orchestrator(store))
        contents = "\n".join(ev["content"] for ev in events)
        assert "旧式产出文本" in contents
        assert "旧式错误原因" in contents

    _run(_test())


# ═════════════════════════════════════════════════════════════════
# 5. spec.render() 输出关键字段
# ═════════════════════════════════════════════════════════════════

def test_spec_render_contains_key_fields():
    spec = TaskSpec(
        background="背景信息",
        goal="交付目标",
        description="详细描述",
        constraints=["约束甲", "约束乙"],
        format="Markdown 报告",
        acceptance_criteria=["标准一"],
    )
    text = spec.render()
    for fragment in (
        "[Background]", "背景信息",
        "[Goal]", "交付目标",
        "[Description]", "详细描述",
        "[Constraints]", "约束甲", "约束乙",
        "[Output Format]", "Markdown 报告",
        "[Acceptance Criteria]", "标准一",
    ):
        assert fragment in text


def test_spec_render_light_task_goal_only():
    """轻任务只填 goal 也合法, 渲染只含目标段."""
    spec = TaskSpec(goal="只做一件事")
    text = spec.render()
    assert "[Goal]" in text and "只做一件事" in text
    assert "[Background]" not in text and "[Constraints]" not in text
    assert not spec.is_empty()


def test_build_spec_empty_returns_none():
    assert _build_spec() is None
    assert _build_spec(description="") is None
    assert _build_spec(goal="x") is not None


def test_parse_task_result_edge_cases():
    # ```json 代码块包裹容忍
    r = _parse_task_result('```json\n{"output": "x"}\n```', status="in_review")
    assert r is not None and r.output == "x" and r.status == "in_review"
    # 非法 uncertainty → 整体解析失败 (降级)
    assert _parse_task_result('{"output": "x", "uncertainty": "bogus"}') is None
    # 非 dict JSON → None
    assert _parse_task_result('["a", "b"]') is None
    assert _parse_task_result("") is None


# ── E2E 回归: LLM 将 list 参数双重编码为 JSON 字符串 (曾致 delegate 连续校验失败) ──
def test_normalize_str_list_json_encoded():
    from harness.team.tools import _normalize_str_list
    assert _normalize_str_list('["a", "b"]') == ["a", "b"]
    assert _normalize_str_list('["hello.html 文件存在于工作区", "页面标题为 Hello Team"]') == [
        "hello.html 文件存在于工作区", "页面标题为 Hello Team"]
    # 非 JSON 字符串仍按行分割
    assert _normalize_str_list("a\nb") == ["a", "b"]
    # 非法 JSON 以 [ 开头 → 按行兜底不报错
    assert _normalize_str_list("[not json") == ["[not json"]
    # list 直传不受影响
    assert _normalize_str_list(["x", "y"]) == ["x", "y"]


def test_build_spec_with_json_encoded_lists():
    spec = _build_spec(goal="g", acceptance_criteria='["c1", "c2"]', constraints='["k1"]')
    assert spec is not None
    assert spec.acceptance_criteria == ["c1", "c2"]
    assert spec.constraints == ["k1"]
