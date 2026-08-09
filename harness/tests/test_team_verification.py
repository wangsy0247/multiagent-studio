"""Phase 3 风险分级 + 独立 Verifier 验收测试.

覆盖:
- infer_task_risk 程序推断 (关键词/验收标准/下游依赖/默认 low)
- delegate_to_member / task_create 的 risk 落定 (显式指定锁定 / 推断 / 依赖升级)
- 低危快速通道 (证据直通 / 证据缺失 fail-safe 转 Lead)
- Verifier 检测、验收子任务创建、VERDICT 消化 (PASS/FAIL/解析失败/无 Verifier)
- _is_complete 含验收子任务场景、状态机回归
"""

import asyncio
import json
import types

import pytest

from harness.team.models import (
    TaskResult,
    TaskSpec,
    TeamTask,
    TeamTaskStatus,
    infer_task_risk,
)
from harness.team.task_store import TeamTaskStore
from harness.team.tools import (
    _VALID_TRANSITIONS,
    create_team_tools,
    set_current_agent,
    set_current_agent_instance,
)
from harness.team.orchestrator import TeamOrchestrator, _parse_verdict


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


@pytest.fixture
def tmp_paths(tmp_path):
    """把全局 Paths 单例指到 tmp 目录 (证据校验/消息总线用), 结束后还原."""
    from harness.config import paths as paths_mod

    old = paths_mod._paths
    paths_mod.set_paths(paths_mod.Paths(base_dir=tmp_path))
    yield tmp_path
    paths_mod._paths = old


def _tools(store, role="member", teammates=None):
    tools = create_team_tools(
        task_store=store, role=role,
        teammates=teammates if teammates is not None else {"worker": object()},
    )
    return {t.name: t for t in tools}


def _make_in_progress_task(store, risk=None, description="只读查询任务"):
    """创建并置为 IN_PROGRESS 的任务 (分配给 worker)."""
    async def _mk():
        task = await store.create_task(
            title="测试任务", description=description,
            assigned_agent="worker", risk=risk,
        )
        await store.update_task(task.id, status=TeamTaskStatus.IN_PROGRESS)
        return task
    return _run(_mk())


def _bare_orch(store, bus=None, roster=()):
    """构造只够跑验收流程的裸 orchestrator (绕过 __init__)."""
    orch = TeamOrchestrator.__new__(TeamOrchestrator)
    orch.task_store = store
    orch.message_bus = bus
    orch.teammates = {}
    orch._member_names = list(roster)
    orch._user_id = "default"
    orch._thread_id = "thread1"
    orch._project_id = "test_project"
    orch._event_queue = asyncio.Queue()
    orch._progress_event = asyncio.Event()
    return orch


# ═════════════════════════════════════════════════════════════════
# 1. infer_task_risk 程序推断
# ═════════════════════════════════════════════════════════════════

def test_infer_risk_write_keyword():
    """写操作类关键词 (修改/删除/部署等) → high."""
    t = TeamTask(project_id="p", title="修改配置文件", description="更新 config.yaml 内容")
    assert infer_task_risk(t) == "high"


def test_infer_risk_acceptance_criteria():
    """acceptance_criteria 非空 → high (即使描述是只读类)."""
    spec = TaskSpec(goal="调研方案", acceptance_criteria=["报告覆盖全部要点"])
    t = TeamTask(project_id="p", title="调研资料", spec=spec)
    assert infer_task_risk(t) == "high"


def test_infer_risk_has_downstream():
    """有下游依赖它的任务 → high."""
    t = TeamTask(project_id="p", title="查询数据")
    assert infer_task_risk(t, has_downstream=True) == "high"


def test_infer_risk_readonly_default_low():
    """只读/探索/查询类, 无验收标准 → low."""
    t = TeamTask(project_id="p", title="搜索并总结资料", description="收集竞品信息并汇总")
    assert infer_task_risk(t) == "low"


# ═════════════════════════════════════════════════════════════════
# 2. 创建时 risk 落定 (delegate_to_member / task_create)
# ═════════════════════════════════════════════════════════════════

def test_delegate_explicit_risk_respected_and_locked(store):
    """Lead 显式指定 high → 尊重并锁定; 显式 low 且程序也判 low → 锁定."""
    set_current_agent("__team_lead__")

    async def _test():
        tools = _tools(store, role="lead")
        out = await tools["delegate_to_member"].ainvoke({
            "agent_name": "worker",
            "title": "只读查询但显式高危",
            "description": "查询日志",
            "risk": "high",
        })
        assert "Risk level: high" in out
        await tools["delegate_to_member"].ainvoke({
            "agent_name": "worker",
            "title": "只读查询且显式低危",
            "description": "查询日志",
            "risk": "low",
        })
        tasks = await store.load_tasks()
        by_title = {t.title: t for t in tasks}
        assert by_title["只读查询但显式高危"].risk == "high"
        assert by_title["只读查询但显式高危"].risk_locked is True
        assert by_title["只读查询且显式低危"].risk == "low"
        assert by_title["只读查询且显式低危"].risk_locked is True

    _run(_test())


def test_delegate_low_risk_one_way_upgraded(store):
    """Lead 标 low 但命中写操作信号 → 单向升级 high (E2E 回归: Lead 给写文件任务标 low)."""
    set_current_agent("__team_lead__")

    async def _test():
        tools = _tools(store, role="lead")
        out = await tools["delegate_to_member"].ainvoke({
            "agent_name": "worker",
            "title": "创建 hello.html 问候页面",
            "description": "在工作区创建文件",
            "risk": "low",
        })
        assert "upgraded" in out
        t = (await store.load_tasks())[0]
        assert t.risk == "high"
        assert t.risk_locked is False

    _run(_test())


def test_delegate_member_check_roster_aware(store):
    """懒加载名册: 成员未 spawn 但在名册中 → 不误报"不在团队中" (E2E 回归)."""
    set_current_agent("__team_lead__")

    async def _test():
        lead_stub = type("T", (), {"_role": "lead"})()  # 仅提供过滤所需属性
        tools = create_team_tools(
            task_store=store, role="lead",
            teammates={"__team_lead__": lead_stub},
            member_names=["Frontend-Developer", "AI-Engineer"],
        )
        by_name = {t.name: t for t in tools}
        out = await by_name["delegate_to_member"].ainvoke({
            "agent_name": "Frontend-Developer", "title": "查询任务",
        })
        assert "not in the current team" not in out
        out2 = await by_name["delegate_to_member"].ainvoke({
            "agent_name": "Nobody", "title": "查询任务2",
        })
        assert "not in the current team" in out2
        # list_teammates 应列出 standby 成员
        out3 = await by_name["list_teammates"].ainvoke({})
        assert "standby" in out3 and "Frontend-Developer" in out3

    _run(_test())


def test_validate_evidence_sandbox_absolute_path(tmp_path):
    """/mnt/user-data/ 沙箱绝对路径 → 映射回宿主机 thread 工作区 (E2E 回归)."""
    from harness.team.tools import _validate_evidence
    udata = tmp_path / "user-data"
    (udata / "workspace").mkdir(parents=True)
    (udata / "workspace" / "hello.html").write_text("<html/>")
    roots = [udata / "workspace", tmp_path, udata]
    ok, missing = _validate_evidence(["/mnt/user-data/workspace/hello.html"], roots)
    assert ok and not missing
    ok2, missing2 = _validate_evidence(["/mnt/user-data/workspace/nope.html"], roots)
    assert not ok2 and missing2


def test_delegate_inferred_risk(store):
    """未指定 → 程序推断: 写操作 → high, 只读 → low."""
    set_current_agent("__team_lead__")

    async def _test():
        tools = _tools(store, role="lead")
        await tools["delegate_to_member"].ainvoke({
            "agent_name": "worker", "title": "部署服务到生产环境",
        })
        await tools["delegate_to_member"].ainvoke({
            "agent_name": "worker", "title": "查询日志并汇总",
        })
        tasks = await store.load_tasks()
        by_title = {t.title: t for t in tasks}
        assert by_title["部署服务到生产环境"].risk == "high"
        assert by_title["部署服务到生产环境"].risk_locked is False
        assert by_title["查询日志并汇总"].risk == "low"

    _run(_test())


def test_dependency_task_upgraded_to_high(store):
    """创建带依赖的任务时, 被依赖的任务自动升级 high (未锁定时)."""
    set_current_agent("__team_lead__")

    async def _test():
        tools = _tools(store, role="lead")
        await tools["task_create"].ainvoke({"title": "收集原始资料"})
        dep = (await store.load_tasks())[0]
        assert dep.risk == "low"

        await tools["delegate_to_member"].ainvoke({
            "agent_name": "worker", "title": "基于资料产出报告",
            "dependencies": [dep.id],
        })
        reloaded = await store.get_task(dep.id)
        assert reloaded.risk == "high"

    _run(_test())


# ═════════════════════════════════════════════════════════════════
# 3. 低危快速通道 (task_update 提交 in_review)
# ═════════════════════════════════════════════════════════════════

def test_low_risk_no_evidence_fast_track(store):
    """低危 + 无证据 → 提交 in_review 直通 COMPLETED."""
    task = _make_in_progress_task(store, risk="low")
    set_current_agent("worker")

    async def _test():
        tools = _tools(store)
        out = await tools["task_update"].ainvoke({
            "task_id": task.id, "status": "in_review",
            "result": json.dumps({"output": "查询结果汇总"}),
        })
        assert "completed directly" in out
        t = await store.get_task(task.id)
        assert t.status == TeamTaskStatus.COMPLETED

    _run(_test())


def test_low_risk_evidence_exists_fast_track(store, tmp_paths):
    """低危 + 证据文件存在 → 直通 COMPLETED."""
    # 在线程共享 workspace 下造一个证据文件
    ws = tmp_paths / "users" / "default" / "threads" / "t1" / "user-data" / "workspace"
    ws.mkdir(parents=True)
    (ws / "report.md").write_text("# 报告", encoding="utf-8")

    task = _make_in_progress_task(store, risk="low")
    set_current_agent("worker")
    # 注入 agent 实例上下文 (证据解析需要 thread_id/user_id)
    set_current_agent_instance(types.SimpleNamespace(_thread_id="t1", _user_id="default"))

    async def _test():
        tools = _tools(store)
        out = await tools["task_update"].ainvoke({
            "task_id": task.id, "status": "in_review",
            "result": json.dumps({"output": "报告已生成", "evidence": ["report.md"]}),
        })
        assert "completed directly" in out
        t = await store.get_task(task.id)
        assert t.status == TeamTaskStatus.COMPLETED

    _run(_test())
    set_current_agent_instance(None)


def test_low_risk_evidence_missing_goes_to_review(store, tmp_paths):
    """低危 + 证据文件不存在 → 保持 IN_REVIEW 转 Lead 审查 (fail-safe)."""
    task = _make_in_progress_task(store, risk="low")
    set_current_agent("worker")
    set_current_agent_instance(types.SimpleNamespace(_thread_id="t1", _user_id="default"))

    async def _test():
        tools = _tools(store)
        out = await tools["task_update"].ainvoke({
            "task_id": task.id, "status": "in_review",
            "result": json.dumps({"output": "报告已生成", "evidence": ["no_such_file.md"]}),
        })
        assert "Evidence validation failed" in out
        t = await store.get_task(task.id)
        assert t.status == TeamTaskStatus.IN_REVIEW

    _run(_test())
    set_current_agent_instance(None)


def test_high_risk_no_fast_track(store):
    """高危任务提交 in_review 不直通 (等 Verifier/Lead 验收)."""
    task = _make_in_progress_task(store, risk="high")
    set_current_agent("worker")

    async def _test():
        tools = _tools(store)
        await tools["task_update"].ainvoke({
            "task_id": task.id, "status": "in_review",
            "result": json.dumps({"output": "完成"}),
        })
        t = await store.get_task(task.id)
        assert t.status == TeamTaskStatus.IN_REVIEW

    _run(_test())


def test_legacy_task_risk_none_inferred_at_submit(store):
    """历史任务 (risk=None) 提交时按描述推断: 只读 → 直通; 写操作 → 留 IN_REVIEW."""
    t_read = _make_in_progress_task(store, risk=None, description="搜索资料并汇总")
    t_write = _make_in_progress_task(store, risk=None, description="修改配置文件")
    set_current_agent("worker")

    async def _test():
        tools = _tools(store)
        await tools["task_update"].ainvoke({
            "task_id": t_read.id, "status": "in_review", "output": "汇总完成",
        })
        await tools["task_update"].ainvoke({
            "task_id": t_write.id, "status": "in_review", "output": "改完了",
        })
        assert (await store.get_task(t_read.id)).status == TeamTaskStatus.COMPLETED
        assert (await store.get_task(t_write.id)).status == TeamTaskStatus.IN_REVIEW

    _run(_test())


# ═════════════════════════════════════════════════════════════════
# 4. VERDICT 解析
# ═════════════════════════════════════════════════════════════════

def test_parse_verdict_pass_fail_none():
    v, reason = _parse_verdict("核对完毕。\nVERDICT: PASS — 全部验收标准满足")
    assert v == "pass" and "全部验收标准满足" in reason
    v, reason = _parse_verdict("VERDICT: FAIL 第 2 条验收标准不满足")
    assert v == "fail" and "第 2 条" in reason
    assert _parse_verdict("没有结论行的输出") == (None, "")
    assert _parse_verdict("") == (None, "")


# ═════════════════════════════════════════════════════════════════
# 5. Verifier 检测 (平台内置)
# ═════════════════════════════════════════════════════════════════

def test_get_verifier_builtin(store):
    """Verifier 为平台内置成员 — 不依赖项目 roster, 始终返回内置名称."""
    from harness.team.orchestrator import TEAM_VERIFIER_NAME
    orch = _bare_orch(store, roster=["coder", "writer"])
    assert orch._get_verifier() == TEAM_VERIFIER_NAME


def test_verifier_excluded_from_candidates(store):
    """内置 Verifier 与 Lead 不参与普通派单候选 (只接验收子任务)."""
    from harness.team.orchestrator import TEAM_LEAD_NAME, TEAM_VERIFIER_NAME
    orch = _bare_orch(store, roster=["coder", TEAM_VERIFIER_NAME])
    names = [n for n, _ in orch._candidate_names()]
    assert "coder" in names
    assert TEAM_VERIFIER_NAME not in names
    assert TEAM_LEAD_NAME not in names


# ═════════════════════════════════════════════════════════════════
# 6. 验收子任务创建 + VERDICT 消化
# ═════════════════════════════════════════════════════════════════

def _make_high_risk_in_review(store, executor="worker"):
    """高危任务: worker 已提交 IN_REVIEW."""
    async def _mk():
        task = await store.create_task(
            title="修改核心模块", description="修改核心模块实现",
            assigned_agent=executor, risk="high",
        )
        await store.update_task(
            task.id,
            status=TeamTaskStatus.IN_REVIEW,
            result=TaskResult(output="已实现", evidence=["src/core.py"]),
        )
        return task
    return _run(_mk())


def test_verification_subtask_created_for_verifier(store, tmp_paths):
    """高危 IN_REVIEW → 创建验收子任务, 分配给内置 Verifier 而非执行者."""
    from harness.team.message_bus import TeamMessageBus
    from harness.team.orchestrator import TEAM_VERIFIER_NAME
    bus = TeamMessageBus("test_project", "default", "thread1")
    orch = _bare_orch(store, bus=bus, roster=["worker"])

    async def _ensure_ok():  # 模拟内置 Verifier 拉起成功
        return object()
    orch._ensure_verifier = _ensure_ok

    orig = _make_high_risk_in_review(store)

    async def _test():
        progress = await orch._process_verifications()
        assert progress is True
        tasks = await store.load_tasks()
        vtasks = [t for t in tasks if t.verifies_task_id == orig.id]
        assert len(vtasks) == 1
        v = vtasks[0]
        assert v.assigned_agent == TEAM_VERIFIER_NAME  # 不是执行者
        assert v.status == TeamTaskStatus.PENDING
        assert v.risk == "low"                     # 验收任务本身低危 (防递归)
        assert "VERDICT" in v.description          # 委派模板含结论格式要求
        # 原任务仍在 IN_REVIEW 等验收
        assert (await store.get_task(orig.id)).status == TeamTaskStatus.IN_REVIEW
        # 幂等: 再跑一轮不重复创建
        await orch._process_verifications()
        tasks2 = await store.load_tasks()
        assert len([t for t in tasks2 if t.verifies_task_id == orig.id]) == 1

    _run(_test())


def test_no_verifier_falls_back_to_lead(store, tmp_paths):
    """内置 Verifier 拉起失败 → 不创建验收子任务, 原任务留 IN_REVIEW 走 Lead task_review."""
    orch = _bare_orch(store, roster=["worker"])

    async def _ensure_fail():  # 模拟拉起失败 (LLM 不可用等)
        return None
    orch._ensure_verifier = _ensure_fail

    orig = _make_high_risk_in_review(store)

    async def _test():
        await orch._process_verifications()
        tasks = await store.load_tasks()
        assert not [t for t in tasks if t.verifies_task_id == orig.id]
        assert (await store.get_task(orig.id)).status == TeamTaskStatus.IN_REVIEW

    _run(_test())


def test_verifier_must_not_be_executor(store, tmp_paths):
    """Verifier 与执行者是同一人 → 不创建验收子任务 (执行者不得验收自己的产出)."""
    from harness.team.message_bus import TeamMessageBus
    from harness.team.orchestrator import TEAM_VERIFIER_NAME
    bus = TeamMessageBus("test_project", "default", "thread1")
    orch = _bare_orch(store, bus=bus, roster=[])

    async def _ensure_ok():
        return object()
    orch._ensure_verifier = _ensure_ok

    orig = _make_high_risk_in_review(store, executor=TEAM_VERIFIER_NAME)

    async def _test():
        await orch._process_verifications()
        tasks = await store.load_tasks()
        assert not [t for t in tasks if t.verifies_task_id == orig.id]
        assert (await store.get_task(orig.id)).status == TeamTaskStatus.IN_REVIEW

    _run(_test())


def _complete_verification(store, orig_id, output):
    """直接造一个已完成的验收子任务 (模拟 Verifier 提交)."""
    async def _mk():
        v = await store.create_task(
            title=f"验收: 修改核心模块", assigned_agent="verifier",
            verifies_task_id=orig_id, risk="low",
        )
        await store.update_task(
            v.id, status=TeamTaskStatus.COMPLETED,
            result=TaskResult(output=output),
        )
        return v
    return _run(_mk())


def test_verdict_pass_approves(store, tmp_paths):
    """VERDICT PASS → 原任务 APPROVED, 反馈含验收理由."""
    from harness.team.message_bus import TeamMessageBus
    bus = TeamMessageBus("test_project", "default", "thread1")
    orch = _bare_orch(store, bus=bus, roster=["worker", "verifier"])
    orig = _make_high_risk_in_review(store)
    _complete_verification(store, orig.id, "逐条核对完毕。\nVERDICT: PASS — 全部标准满足")

    async def _test():
        progress = await orch._process_verifications()
        assert progress is True
        t = await store.get_task(orig.id)
        assert t.status == TeamTaskStatus.APPROVED
        assert "全部标准满足" in t.review_feedback

    _run(_test())


def test_verdict_fail_revision_needed(store, tmp_paths):
    """VERDICT FAIL → 原任务 REVISION_NEEDED, 附 FAIL 理由, revision_count+1."""
    from harness.team.message_bus import TeamMessageBus
    bus = TeamMessageBus("test_project", "default", "thread1")
    orch = _bare_orch(store, bus=bus, roster=["worker", "verifier"])
    orig = _make_high_risk_in_review(store)
    _complete_verification(store, orig.id, "VERDICT: FAIL 第 2 条标准不满足, 缺少错误处理")

    async def _test():
        progress = await orch._process_verifications()
        assert progress is True
        t = await store.get_task(orig.id)
        assert t.status == TeamTaskStatus.REVISION_NEEDED
        assert t.revision_count == 1
        assert "第 2 条标准不满足" in t.review_feedback
        # 执行者收到打回通知 (含理由)
        inbox = await bus.read_inbox("worker")
        assert any("verification rejected" in m.content for m in inbox)

    _run(_test())


def test_verdict_parse_failure_falls_back(store, tmp_paths):
    """VERDICT 解析失败 → 原任务留 IN_REVIEW 转 Lead 审查 (fail-safe)."""
    from harness.team.message_bus import TeamMessageBus
    bus = TeamMessageBus("test_project", "default", "thread1")
    orch = _bare_orch(store, bus=bus, roster=["worker", "verifier"])
    orig = _make_high_risk_in_review(store)
    _complete_verification(store, orig.id, "我觉得还行, 但是没有按格式输出结论")

    async def _test():
        await orch._process_verifications()
        t = await store.get_task(orig.id)
        assert t.status == TeamTaskStatus.IN_REVIEW

    _run(_test())


# ═════════════════════════════════════════════════════════════════
# 7. _is_complete 含验收子任务
# ═════════════════════════════════════════════════════════════════

def test_is_complete_with_verification_subtask(store):
    """验收子任务未终态 → 未完成; 全部终态 → 完成."""
    orch = _bare_orch(store, roster=["worker", "verifier"])
    orig = _make_high_risk_in_review(store)

    async def _test():
        v = await store.create_task(
            title="验收: 修改核心模块", assigned_agent="verifier",
            verifies_task_id=orig.id,
        )
        assert await orch._is_complete() is False
        await store.update_task(v.id, status=TeamTaskStatus.COMPLETED)
        # 原任务仍 IN_REVIEW → 未完成
        assert await orch._is_complete() is False
        await store.update_task(orig.id, status=TeamTaskStatus.APPROVED)
        assert await orch._is_complete() is True

    _run(_test())


# ═════════════════════════════════════════════════════════════════
# 8. 状态机回归 (Phase 3 不破坏现有合法流转)
# ═════════════════════════════════════════════════════════════════

def test_valid_transitions_unchanged():
    """现有合法流转不被破坏; INTERRUPTED 仍不在表中 (orchestrator 独占)."""
    assert _VALID_TRANSITIONS[TeamTaskStatus.PENDING] == {TeamTaskStatus.IN_PROGRESS}
    assert _VALID_TRANSITIONS[TeamTaskStatus.IN_PROGRESS] == {
        TeamTaskStatus.IN_REVIEW, TeamTaskStatus.COMPLETED, TeamTaskStatus.FAILED,
    }
    assert _VALID_TRANSITIONS[TeamTaskStatus.IN_REVIEW] == {
        TeamTaskStatus.APPROVED, TeamTaskStatus.REVISION_NEEDED,
    }
    assert _VALID_TRANSITIONS[TeamTaskStatus.REVISION_NEEDED] == {TeamTaskStatus.IN_PROGRESS}
    # 终态不可再变更
    for terminal in (TeamTaskStatus.APPROVED, TeamTaskStatus.COMPLETED,
                     TeamTaskStatus.FAILED, TeamTaskStatus.CANCELLED):
        assert _VALID_TRANSITIONS[terminal] == set()
    # INTERRUPTED 有意不在表中
    assert TeamTaskStatus.INTERRUPTED not in _VALID_TRANSITIONS


def test_infer_risk_create_keywords():
    """E2E 回归: '创建 hello.html' 曾误判 low — 创建/新建/create 属写操作."""
    t = TeamTask(project_id="p", title="创建 hello.html 问候页面",
                 description="在工作区创建一个文件")
    assert infer_task_risk(t) == "high"
    t2 = TeamTask(project_id="p", title="新建配置文件", description="")
    assert infer_task_risk(t2) == "high"


def test_high_risk_cannot_complete_directly(store):
    """E2E 回归: 高危任务禁止 member 直达 COMPLETED (绕过验收的漏洞)."""
    set_current_agent("worker")
    task = _make_in_progress_task(store, risk="high", description="修改核心模块")

    async def _test():
        tools = _tools(store, role="member", teammates={"worker": object()})
        out = await tools["task_update"].ainvoke({
            "task_id": task.id, "status": "completed", "output": "搞定了",
        })
        assert "High-risk tasks cannot be marked completed directly" in out
        assert (await store.get_task(task.id)).status == TeamTaskStatus.IN_PROGRESS
        # 提交 in_review 则放行 (进入验收)
        out2 = await tools["task_update"].ainvoke({
            "task_id": task.id, "status": "in_review", "output": "已实现",
        })
        assert "Error" not in out2
        assert (await store.get_task(task.id)).status == TeamTaskStatus.IN_REVIEW

    _run(_test())


def test_low_risk_can_complete_directly(store):
    """低危任务直达 COMPLETED 不受高危守卫影响."""
    set_current_agent("worker")
    task = _make_in_progress_task(store, risk="low", description="查询日志")

    async def _test():
        tools = _tools(store, role="member", teammates={"worker": object()})
        out = await tools["task_update"].ainvoke({
            "task_id": task.id, "status": "completed", "output": "查完了",
        })
        assert "Error" not in out
        assert (await store.get_task(task.id)).status == TeamTaskStatus.COMPLETED

    _run(_test())
