"""Skill 自进化 (Phase 5) 单元测试 — Team 成员私有技能库.

覆盖: 生命周期 (probation → pending → promote/archive) / record_use 计数与
阈值边界 / 连续失败与长期未用归档 / 注入渲染 (probation 标注, archived 不注入) /
候选提取 (启发式 + LLM mock) / 转正审批消息流 (mock message_bus) /
AgentCard 技能收敛 / 文件锁与原子写冒烟 / TaskResult.skill_feedback 兼容性。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from harness.config.paths import Paths, get_paths, set_paths
from harness.skills.evolution.member import (
    STATE_ACTIVE,
    STATE_ARCHIVED,
    STATE_PROBATION,
    MemberSkillEvolutionStore,
    distill_skill_candidate,
    is_procedural_lesson,
    is_risky_skill,
    parse_skill_md,
    render_evolved_skills_section,
    render_promotion_request,
    send_promotion_approval_request,
)
from harness.team.models import TaskResult, TeamMessageType

AGENT = "coder"

_SAMPLE_SKILL_MD = """---
name: api-retry-workflow
description: 调用外部 API 时带超时与指数退避重试的标准流程
---

# API 重试流程

## 适用场景
调用不稳定的第三方 HTTP 接口。

## 步骤
1. 设置超时 5s
2. 失败时指数退避重试, 最多 3 次
3. 记录每次重试的响应码

## 注意事项
不要对 4xx 错误重试。
"""

_RISKY_SKILL_MD = """---
name: cleanup-temp-files
description: 清理临时文件的流程
---

# 清理临时文件

## 步骤
1. 找到 /tmp 下过期文件
2. 执行命令 rm -rf 删除它们
"""


def _run(async_func):
    """在同步测试中运行异步函数."""
    return asyncio.run(async_func)


@pytest.fixture
def store(tmp_path):
    """低阈值的隔离 store (base_dir=tmp_path)."""
    return MemberSkillEvolutionStore(
        user_id="u1", base_dir=tmp_path,
        promote_success_uses=3, fail_archive_threshold=2, stale_days=30,
    )


@pytest.fixture
def paths_isolated(tmp_path):
    """隔离 get_paths().base_dir (agent_card 读写用), 结束后还原."""
    old = get_paths()
    set_paths(Paths(str(tmp_path)))
    yield tmp_path
    set_paths(old)


# ──────────────────────────────────────────────────────────────────────────────
# SKILL.md 解析与启发式
# ──────────────────────────────────────────────────────────────────────────────

def test_parse_skill_md_ok():
    parsed = parse_skill_md(_SAMPLE_SKILL_MD)
    assert parsed is not None
    name, description = parsed
    assert name == "api-retry-workflow"
    assert "重试" in description


def test_parse_skill_md_rejects_invalid():
    assert parse_skill_md("") is None
    assert parse_skill_md("没有 frontmatter 的文本") is None
    # name 不合法 (大写/空格) → None
    bad = "---\nname: Bad Name\ndescription: x\n---\n正文"
    assert parse_skill_md(bad) is None
    # 缺 description → None
    missing = "---\nname: good-name\n---\n正文"
    assert parse_skill_md(missing) is None


def test_is_procedural_lesson():
    # 步骤标记命中
    assert is_procedural_lesson("处理流程: 第一步先备份, 第二步再迁移")
    assert is_procedural_lesson("做法:\n1. 先读取配置\n2. 再写文件")
    assert is_procedural_lesson("standard step 1: check input")
    # 工具序列命中
    assert is_procedural_lesson("先用 file_read 读入, 再用 file_write 写出")
    assert is_procedural_lesson("搜索 → 总结 → 输出")
    # 复用 ≥2 次命中 (即使无步骤标记)
    assert is_procedural_lesson("调用外部接口要加超时", reuse_count=2)
    # 普通经验不命中
    assert not is_procedural_lesson("调用外部接口要加超时", reuse_count=1)
    assert not is_procedural_lesson("这个项目用 JWT 做鉴权")


def test_is_risky_skill():
    assert is_risky_skill(_RISKY_SKILL_MD)
    assert not is_risky_skill(_SAMPLE_SKILL_MD)


# ──────────────────────────────────────────────────────────────────────────────
# 生命周期: 候选 → probation → pending → promote/archive
# ──────────────────────────────────────────────────────────────────────────────

def test_add_candidate_probation(store):
    name = store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    assert name == "api-retry-workflow"
    meta = store.get_meta(AGENT, name)
    assert meta is not None
    assert meta["state"] == STATE_PROBATION
    assert meta["source"] == "evolved"
    assert meta["success_uses"] == 0
    # SKILL.md 文件已落盘
    assert "name: api-retry-workflow" in store.read_skill_content(AGENT, name)


def test_add_candidate_dedup(store):
    assert store.add_candidate(AGENT, _SAMPLE_SKILL_MD) is not None
    # 同名 (任意状态) 不重复添加
    assert store.add_candidate(AGENT, _SAMPLE_SKILL_MD) is None
    # 解析失败返回 None
    assert store.add_candidate(AGENT, "垃圾内容") is None


def test_record_use_pending_then_promote(store):
    name = store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    # 阈值边界: 前 2 次成功不触发 pending
    store.record_use(AGENT, name, True)
    store.record_use(AGENT, name, True)
    assert store.get_meta(AGENT, name)["pending_promotion"] is False
    assert store.pending_promotions(AGENT) == []
    # 第 3 次成功 → pending_promotion
    rec = store.record_use(AGENT, name, True)
    assert rec["success_uses"] == 3
    assert rec["pending_promotion"] is True
    pendings = store.pending_promotions(AGENT)
    assert [p["name"] for p in pendings] == [name]
    # promote → active
    assert store.promote(AGENT, name) is True
    meta = store.get_meta(AGENT, name)
    assert meta["state"] == STATE_ACTIVE
    assert meta["promoted_at"]
    assert meta["pending_promotion"] is False
    # 已转正不再出现在 pending 列表
    assert store.pending_promotions(AGENT) == []


def test_promotion_rejected_archives(store):
    name = store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    for _ in range(3):
        store.record_use(AGENT, name, True)
    assert store.archive(AGENT, name) is True
    assert store.get_meta(AGENT, name)["state"] == STATE_ARCHIVED
    # 归档后不再计数
    rec = store.record_use(AGENT, name, True)
    assert rec["success_uses"] == 3
    # 归档不可再 promote
    assert store.promote(AGENT, name) is False


def test_consecutive_failures_archive(store):
    name = store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    # 第 1 次失败不归档 (阈值=2)
    rec = store.record_use(AGENT, name, False)
    assert rec["state"] == STATE_PROBATION
    assert rec["consecutive_fails"] == 1
    # 第 2 次连续失败 → archived
    rec = store.record_use(AGENT, name, False)
    assert rec["state"] == STATE_ARCHIVED
    assert store.pending_promotions(AGENT) == []


def test_success_resets_consecutive_fails(store):
    name = store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    store.record_use(AGENT, name, False)
    rec = store.record_use(AGENT, name, True)
    assert rec["consecutive_fails"] == 0
    # 后续再失败从 1 重新计
    rec = store.record_use(AGENT, name, False)
    assert rec["consecutive_fails"] == 1
    assert rec["state"] == STATE_PROBATION


def test_record_use_unknown_skill(store):
    assert store.record_use(AGENT, "no-such-skill", True) is None


def test_stale_skill_archived_by_sweep(store, tmp_path):
    name = store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    # 把 created_at 改到 31 天前
    meta_path = tmp_path / "users" / "u1" / "agents" / AGENT / "skills" / "skills_meta.json"
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    data[name]["created_at"] = old
    meta_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    now = datetime.now(timezone.utc)
    assert store.sweep_stale(AGENT, now=now) == 1
    assert store.get_meta(AGENT, name)["state"] == STATE_ARCHIVED
    # 再次 sweep 幂等
    assert store.sweep_stale(AGENT, now=now) == 0


def test_recently_used_skill_not_swept(store):
    name = store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    store.record_use(AGENT, name, True)
    assert store.sweep_stale(AGENT) == 0
    assert store.get_meta(AGENT, name)["state"] == STATE_PROBATION


def test_promotion_requested_excluded_from_pending(store):
    name = store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    for _ in range(3):
        store.record_use(AGENT, name, True)
    assert len(store.pending_promotions(AGENT)) == 1
    store.mark_promotion_requested(AGENT, name)
    assert store.pending_promotions(AGENT) == []


# ──────────────────────────────────────────────────────────────────────────────
# 注入渲染: probation 标注试验性, archived 不注入
# ──────────────────────────────────────────────────────────────────────────────

def test_list_skills_excludes_archived(store):
    name = store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    assert len(store.list_skills(AGENT)) == 1
    store.archive(AGENT, name)
    assert store.list_skills(AGENT) == []


def test_list_skills_skips_missing_file(store, tmp_path):
    name = store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    skill_md = (tmp_path / "users" / "u1" / "agents" / AGENT / "skills"
                / name / "SKILL.md")
    skill_md.unlink()
    assert store.list_skills(AGENT) == []


def test_render_probation_annotated(store):
    store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    section = render_evolved_skills_section(store.list_skills(AGENT))
    assert "<member_evolved_skills>" in section
    assert "api-retry-workflow (experimental, use with caution)" in section
    assert "skill_feedback" in section  # probation 存在时提示上报


def test_render_active_no_probation_tag(store):
    name = store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    store.promote(AGENT, name)
    section = render_evolved_skills_section(store.list_skills(AGENT))
    assert "api-retry-workflow" in section
    assert "experimental" not in section
    assert "skill_feedback" not in section


def test_render_empty():
    assert render_evolved_skills_section([]) == ""


# ──────────────────────────────────────────────────────────────────────────────
# 候选提取: LLM 提炼 (mock)
# ──────────────────────────────────────────────────────────────────────────────

class _MockLLM:
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self._text = text
        self._error = error
        self.calls = 0

    async def ainvoke(self, prompt):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return SimpleNamespace(content=self._text)


def test_distill_candidate_success():
    llm = _MockLLM(text=_SAMPLE_SKILL_MD)
    result = _run(distill_skill_candidate("步骤化经验: 第一步超时 第二步重试", llm))
    assert result is not None
    assert parse_skill_md(result) is not None
    assert llm.calls == 1


def test_distill_candidate_strips_code_fence():
    llm = _MockLLM(text=f"```markdown\n{_SAMPLE_SKILL_MD}\n```")
    result = _run(distill_skill_candidate("经验", llm))
    assert result is not None
    assert parse_skill_md(result) is not None


def test_distill_candidate_llm_failure_returns_none():
    llm = _MockLLM(error=RuntimeError("LLM down"))
    assert _run(distill_skill_candidate("经验", llm)) is None
    # LLM 输出不含合法 frontmatter → None
    bad_llm = _MockLLM(text="这不是 SKILL.md")
    assert _run(distill_skill_candidate("经验", bad_llm)) is None


def test_candidate_extraction_once_per_run(store):
    """模拟 _maybe_evolve_skill 的限流逻辑: 每 run 每成员最多 1 个候选."""
    extracted = False
    for _ in range(2):  # 两次任务完成
        if extracted:
            continue
        name = store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
        if name:
            extracted = True
    assert len(store.list_skills(AGENT)) == 1


# ──────────────────────────────────────────────────────────────────────────────
# 转正审批消息流 (mock message_bus)
# ──────────────────────────────────────────────────────────────────────────────

class _MockMessageBus:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, msg):
        self.sent.append(msg)


def test_promotion_request_message(store):
    name = store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    for _ in range(3):
        store.record_use(AGENT, name, True)
    pendings = store.pending_promotions(AGENT)
    assert len(pendings) == 1

    bus = _MockMessageBus()
    req_id = _run(send_promotion_approval_request(
        bus, from_agent=AGENT, lead_name="leader",
        record=pendings[0], skill_content=store.read_skill_content(AGENT, name),
    ))
    assert req_id
    assert len(bus.sent) == 1
    msg = bus.sent[0]
    # 复用 plan_approval 通道, 定向发给 Lead
    assert msg.msg_type == TeamMessageType.PLAN_APPROVAL_REQUEST
    assert msg.from_agent == AGENT
    assert msg.to_agent == "leader"
    assert msg.request_id == req_id
    # 内容含技能全文 + 使用统计
    assert "<skill_promotion>" in msg.content
    assert "api-retry-workflow" in msg.content
    assert "3 succeeded" in msg.content
    assert "Risk flag: low" in msg.content


def test_promotion_request_risky_flag():
    record = {"agent": AGENT, "name": "cleanup-temp-files",
              "success_uses": 3, "fail_uses": 0, "created_at": "2026-07-31"}
    content = render_promotion_request(record, _RISKY_SKILL_MD)
    assert "Risk flag: high" in content
    assert "requires user confirmation" in content


# ──────────────────────────────────────────────────────────────────────────────
# AgentCard 技能收敛 (spawn 自检)
# ──────────────────────────────────────────────────────────────────────────────

def test_sync_agent_card_skills_prunes_unavailable(paths_isolated):
    from harness.team.agent_card import (
        AgentCard, get_card, save_project_cards, sync_agent_card_skills,
    )
    cards = {
        AGENT: AgentCard(name=AGENT, skills=["good-skill", "gone-skill"]),
    }
    save_project_cards("p1", cards, user_id="u1")

    changed = sync_agent_card_skills(
        "p1", AGENT, user_id="u1", available_skills={"good-skill"},
    )
    assert changed is True
    card = get_card("p1", AGENT, user_id="u1")
    assert card is not None
    assert card.skills == ["good-skill"]


def test_sync_agent_card_skills_noop(paths_isolated):
    from harness.team.agent_card import (
        AgentCard, save_project_cards, sync_agent_card_skills,
    )
    # 无卡片 → False
    assert sync_agent_card_skills("p1", "ghost", user_id="u1",
                                  available_skills=set()) is False
    # 全部可用 → 无变化, 不写盘
    save_project_cards("p1", {AGENT: AgentCard(name=AGENT, skills=["a"])},
                       user_id="u1")
    assert sync_agent_card_skills("p1", AGENT, user_id="u1",
                                  available_skills={"a"}) is False


# ──────────────────────────────────────────────────────────────────────────────
# TaskResult.skill_feedback 向后兼容
# ──────────────────────────────────────────────────────────────────────────────

def test_task_result_skill_feedback_optional():
    # 历史 JSON (无 skill_feedback) 正常加载
    old = TaskResult(**{"output": "done", "evidence": ["a.py"]})
    assert old.skill_feedback == []
    # 新协议正常解析
    new = TaskResult(**{
        "output": "done",
        "skill_feedback": [{"name": "api-retry-workflow", "success": True}],
    })
    assert len(new.skill_feedback) == 1
    assert new.skill_feedback[0].name == "api-retry-workflow"
    assert new.skill_feedback[0].success is True


def test_parse_task_result_with_feedback():
    from harness.team.tools import _parse_task_result
    raw = json.dumps({
        "output": "ok",
        "skill_feedback": [{"name": "s1", "success": False}],
    })
    result = _parse_task_result(raw, status="completed")
    assert result is not None
    assert result.skill_feedback[0].success is False


# ──────────────────────────────────────────────────────────────────────────────
# 文件锁 / 原子写冒烟
# ──────────────────────────────────────────────────────────────────────────────

def test_meta_file_lock_and_atomic_write(store, tmp_path):
    name = store.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    # 连续多次 record_use (每次都走 加锁读-改-写 + 原子写)
    for i in range(10):
        store.record_use(AGENT, name, i % 2 == 0)
    meta_path = tmp_path / "users" / "u1" / "agents" / AGENT / "skills" / "skills_meta.json"
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert data[name]["success_uses"] == 5
    assert data[name]["fail_uses"] == 5
    # 无临时文件残留
    assert not list(meta_path.parent.glob("*.tmp"))
    # 损坏的 meta 文件 → 容错返回空, 不抛异常
    meta_path.write_text("{损坏的 json", encoding="utf-8")
    assert store.get_meta(AGENT, name) is None


def test_two_stores_same_path_consistent(tmp_path):
    """两个 store 实例操作同一目录 (模拟 teammate/tools 两处写), 计数不丢."""
    s1 = MemberSkillEvolutionStore(user_id="u1", base_dir=tmp_path)
    s2 = MemberSkillEvolutionStore(user_id="u1", base_dir=tmp_path)
    name = s1.add_candidate(AGENT, _SAMPLE_SKILL_MD)
    s1.record_use(AGENT, name, True)
    s2.record_use(AGENT, name, True)
    assert s1.get_meta(AGENT, name)["success_uses"] == 2
