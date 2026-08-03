"""Member memory (L1/L3) 单元测试 — Phase 4 记忆分层.

覆盖: 语义指纹 / add_lesson 去重与容量淘汰 / L3→L1 晋升 (LLM mock) /
L3 相关性检索 / extract_lessons_from_task 程序式提取 / 文件锁与原子写冒烟。
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from harness.config.memory_config import (
    MemoryConfig,
    get_memory_config,
    set_memory_config,
)
from harness.memory.member_memory import (
    MemberMemoryStore,
    extract_lessons_from_task,
    is_similar_experience,
    jaccard_similarity,
    normalize_keywords,
    text_fingerprint,
)
from harness.team.models import TaskResult, TaskSpec, TeamTask, TeamTaskStatus


def _run(async_func):
    """在同步测试中运行异步函数."""
    return asyncio.run(async_func)


class _MockLLM:
    """晋升泛化改写的 mock LLM (记录调用次数, 可注入失败)."""

    def __init__(self, text: str = "通用经验: 调用外部接口要加超时与重试",
                 error: Exception | None = None) -> None:
        self._text = text
        self._error = error
        self.calls = 0

    async def ainvoke(self, prompt):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return SimpleNamespace(content=self._text)


@pytest.fixture
def mem_config():
    """用小容量/低阈值配置隔离测试, 结束后还原全局单例."""
    original = get_memory_config()
    set_memory_config(MemoryConfig(
        member_memory_l1_max_items=3,
        member_memory_l3_max_items=3,
        member_memory_promote_projects=2,
        member_memory_promote_reuse=3,
        member_memory_l3_top_k=2,
    ))
    try:
        yield
    finally:
        set_memory_config(original)


@pytest.fixture
def store(tmp_path, mem_config):
    return MemberMemoryStore(user_id="u1", base_dir=tmp_path)


def _l3(store, project_id, agent):
    return store._load_file(store._l3_path(project_id, agent))


def _l1(store, agent):
    return store._load_file(store._l1_path(agent))


# ──────────────────────────────────────────────────────────────────────────────
# 语义指纹: 归一化 / Jaccard / 同类判定 (中英案例)
# ──────────────────────────────────────────────────────────────────────────────

def test_normalize_keywords_english():
    kws = normalize_keywords("The API uses JWT, for Auth!")
    assert "jwt" in kws and "api" in kws and "auth" in kws
    # 停用词被去除
    assert "the" not in kws and "for" not in kws


def test_normalize_keywords_chinese_bigram():
    # 中文无空格: 取二字 bigram 作为关键词
    kws = normalize_keywords("数据库连接")
    assert "数据" in kws and "据库" in kws and "库连" in kws and "连接" in kws


def test_fingerprint_stable():
    # 大小写/语序/标点不影响指纹
    assert text_fingerprint("Use JWT for API") == text_fingerprint("API, use jwt!")


def test_jaccard_similarity():
    assert jaccard_similarity(set(), {"a"}) == 0.0
    assert jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard_similarity({"a", "b"}, {"c", "d"}) == 0.0


def test_is_similar_experience_english():
    fp1 = text_fingerprint("Use JWT for API authentication")
    fp2 = text_fingerprint("API authentication should use JWT tokens")
    fp3 = text_fingerprint("Database schema migration failed")
    assert is_similar_experience(fp1, fp2) is True
    assert is_similar_experience(fp1, fp3) is False


def test_is_similar_experience_chinese():
    fp1 = text_fingerprint("数据库连接要加超时")
    fp2 = text_fingerprint("数据库连接需要加超时")
    fp3 = text_fingerprint("前端页面样式错乱")
    assert is_similar_experience(fp1, fp2) is True
    assert is_similar_experience(fp1, fp3) is False


# ──────────────────────────────────────────────────────────────────────────────
# add_lesson: 指纹去重 / source_task_id 幂等 / 容量淘汰
# ──────────────────────────────────────────────────────────────────────────────

def test_add_lesson_dedup_reuse(store):
    added = _run(store.add_lesson(
        "p1", "coder", "practice",
        "Use JWT for API authentication", source_task_id="t1",
    ))
    assert added is True
    # 同类经验 (指纹相似) → 不新增, reuse_count+1
    added = _run(store.add_lesson(
        "p1", "coder", "practice",
        "API authentication should use JWT tokens", source_task_id="t2",
    ))
    assert added is False
    entries = _l3(store, "p1", "coder")["practices"]
    assert len(entries) == 1
    assert entries[0]["reuse_count"] == 1


def test_add_lesson_source_task_idempotent(store):
    _run(store.add_lesson(
        "p1", "coder", "pitfall", "数据库连接要加超时", source_task_id="t1",
    ))
    # 同一任务再次写入 (teammate 完成时 + orchestrator 结算两处路径) → 跳过
    _run(store.add_lesson(
        "p1", "coder", "pitfall", "完全不同毫不相关的文本", source_task_id="t1",
    ))
    assert len(_l3(store, "p1", "coder")["pitfalls"]) == 1


def test_add_lesson_empty_text_skipped(store):
    assert _run(store.add_lesson("p1", "coder", "practice", "  ")) is False
    assert _l3(store, "p1", "coder")["practices"] == []


def test_add_lesson_capacity_eviction(store):
    # 容量 3/类: 全部零复用时淘汰最旧
    for i, text in enumerate([
        "alpha beta gamma", "delta epsilon zeta",
        "eta theta iota", "kappa lambda mu",
    ]):
        _run(store.add_lesson("p1", "coder", "practice", text,
                              source_task_id=f"t{i}"))
    texts = [e["text"] for e in _l3(store, "p1", "coder")["practices"]]
    assert len(texts) == 3
    assert "alpha beta gamma" not in texts  # 最旧被淘汰


def test_add_lesson_capacity_eviction_keeps_reused(store):
    for i, text in enumerate([
        "alpha beta gamma", "delta epsilon zeta", "eta theta iota",
    ]):
        _run(store.add_lesson("p1", "coder", "practice", text,
                              source_task_id=f"t{i}"))
    # 第 1 条获得复用 → 淘汰时优先保留
    _run(store.add_lesson("p1", "coder", "practice", "alpha beta gamma!",
                          source_task_id="t3b"))
    _run(store.add_lesson("p1", "coder", "practice", "kappa lambda mu",
                          source_task_id="t4"))
    texts = [e["text"] for e in _l3(store, "p1", "coder")["practices"]]
    assert len(texts) == 3
    assert "alpha beta gamma" in texts          # 有复用 → 保留
    assert "delta epsilon zeta" not in texts    # 零复用最旧 → 淘汰


# ──────────────────────────────────────────────────────────────────────────────
# L3 → L1 晋升 (程序计数 + LLM 仅泛化改写)
# ──────────────────────────────────────────────────────────────────────────────

def test_promote_cross_project(tmp_path, mem_config):
    llm = _MockLLM()
    store = MemberMemoryStore(user_id="u1", base_dir=tmp_path, llm=llm)
    _run(store.add_lesson("p1", "coder", "practice", "数据库连接要加超时",
                          source_task_id="t1"))
    assert llm.calls == 0  # 单项目未达标
    _run(store.add_lesson("p2", "coder", "practice", "数据库连接需要加超时",
                          source_task_id="t2"))
    # 跨 2 项目达标 → LLM 泛化改写写 L1
    assert llm.calls == 1
    l1 = _l1(store, "coder")["practices"]
    assert len(l1) == 1
    assert l1[0]["text"] == "通用经验: 调用外部接口要加超时与重试"
    # 晋升后 L3 条目标记 promoted, 后续写入不重复晋升
    _run(store.add_lesson("p1", "coder", "practice", "数据库连接必须加超时",
                          source_task_id="t3"))
    assert llm.calls == 1


def test_promote_reuse_threshold(tmp_path, mem_config):
    llm = _MockLLM()
    store = MemberMemoryStore(user_id="u1", base_dir=tmp_path, llm=llm)
    for i, text in enumerate([
        "Use JWT for API authentication",
        "API authentication should use JWT tokens",
        "JWT tokens for API authentication use",
        "Use JWT in API authentication layer",
    ]):
        _run(store.add_lesson("p1", "coder", "practice", text,
                              source_task_id=f"t{i}"))
    # 1 新增 + 3 复用 → reuse_count=3 ≥ 阈值 → 晋升
    assert llm.calls == 1
    assert len(_l1(store, "coder")["practices"]) == 1


def test_no_promote_below_threshold(store):
    # 单项目且 reuse_count=1 → 两条阈值都不达标
    _run(store.add_lesson("p1", "coder", "practice", "数据库连接要加超时",
                          source_task_id="t1"))
    _run(store.add_lesson("p1", "coder", "practice", "数据库连接需要加超时",
                          source_task_id="t2"))
    assert _l1(store, "coder")["practices"] == []


def test_promote_llm_failure_skips(tmp_path, mem_config):
    llm = _MockLLM(error=RuntimeError("boom"))
    store = MemberMemoryStore(user_id="u1", base_dir=tmp_path, llm=llm)
    # LLM 改写失败 → 跳过本次, 不阻断 (不抛异常, 不写 L1)
    _run(store.add_lesson("p1", "coder", "practice", "数据库连接要加超时",
                          source_task_id="t1"))
    _run(store.add_lesson("p2", "coder", "practice", "数据库连接需要加超时",
                          source_task_id="t2"))
    assert llm.calls == 1
    assert _l1(store, "coder")["practices"] == []


def test_promote_llm_unavailable_skips(tmp_path, mem_config, monkeypatch):
    store = MemberMemoryStore(user_id="u1", base_dir=tmp_path)
    # LLM 不可用 (无凭证) → 跳过晋升, 不阻断
    monkeypatch.setattr(store, "_create_llm", lambda: None)
    _run(store.add_lesson("p1", "coder", "practice", "数据库连接要加超时",
                          source_task_id="t1"))
    _run(store.add_lesson("p2", "coder", "practice", "数据库连接需要加超时",
                          source_task_id="t2"))
    assert _l1(store, "coder")["practices"] == []


def test_promote_audit_log(tmp_path, mem_config, caplog):
    llm = _MockLLM()
    store = MemberMemoryStore(user_id="u1", base_dir=tmp_path, llm=llm)
    with caplog.at_level(logging.INFO, logger="harness.memory.member_memory"):
        _run(store.add_lesson("p1", "coder", "practice", "数据库连接要加超时",
                              source_task_id="t1"))
        _run(store.add_lesson("p2", "coder", "practice", "数据库连接需要加超时",
                              source_task_id="t2"))
    record = next(
        (r for r in caplog.records if "promoted L3→L1" in r.getMessage()), None,
    )
    assert record is not None, "晋升事件应写审计日志"
    msg = record.getMessage()
    # 审计内容: 指纹 / 来源项目 / 改写前后文本
    assert "fingerprint=" in msg and "source_projects=" in msg
    assert "数据库连接" in msg and "通用经验" in msg


# ──────────────────────────────────────────────────────────────────────────────
# 检索注入: get_l3_context 相关性排序 top-K / get_l1_context 全量
# ──────────────────────────────────────────────────────────────────────────────

def test_get_l3_context_ranking_topk(store):
    _run(store.add_lesson("p1", "coder", "practice",
                          "Use JWT for API authentication tokens",
                          source_task_id="t1"))
    _run(store.add_lesson("p1", "coder", "practice",
                          "Database schema migration with alembic",
                          source_task_id="t2"))
    _run(store.add_lesson("p1", "coder", "pitfall",
                          "JWT token expiry causes authentication failures",
                          source_task_id="t3"))
    xml = store.get_l3_context("p1", "coder", "fix JWT authentication bug")
    assert xml.startswith("<project_memory>")
    # top_k=2: 两条 JWT 相关入选, database 不相关落选
    assert "Database" not in xml
    # 相关性相同按时间排序: t1 在前
    assert xml.index("Use JWT") < xml.index("expiry")


def test_get_l3_context_no_match(store):
    _run(store.add_lesson("p1", "coder", "practice",
                          "Database schema migration with alembic",
                          source_task_id="t1"))
    assert store.get_l3_context("p1", "coder", "fix JWT authentication bug") == ""
    # 无任务文本 → 空串 (调用方跳过检索注入)
    assert store.get_l3_context("p1", "coder", "") == ""


def test_get_l1_context(store):
    assert store.get_l1_context("coder") == ""
    store._save_file(store._l1_path("coder"), {
        "practices": [{
            "text": "通用经验 X", "source_task_id": "",
            "created_at": "2026-01-01T00:00:00+00:00",
            "reuse_count": 0, "fingerprint": "x",
        }],
        "pitfalls": [],
        "domain_notes": [],
    })
    xml = store.get_l1_context("coder")
    assert xml.startswith("<member_memory>")
    assert "通用经验 X" in xml


# ──────────────────────────────────────────────────────────────────────────────
# extract_lessons_from_task: 程序式提取规则
# ──────────────────────────────────────────────────────────────────────────────

def test_extract_failed_pitfall():
    task = TeamTask(
        id="t1", project_id="p1", title="接入支付接口",
        status=TeamTaskStatus.FAILED, error="API 返回 401",
    )
    lessons = extract_lessons_from_task(task)
    assert len(lessons) == 1
    kind, text = lessons[0]
    assert kind == "pitfall" and "401" in text


def test_extract_failed_via_result():
    task = TeamTask(
        id="t1", project_id="p1", title="同步数据",
        status=TeamTaskStatus.FAILED,
        result=TaskResult(failure_reason="上游接口超时"),
    )
    lessons = extract_lessons_from_task(task)
    assert len(lessons) == 1
    assert lessons[0][0] == "pitfall" and "超时" in lessons[0][1]


def test_extract_completed_practice():
    task = TeamTask(
        id="t1", project_id="p1", title="实现登录",
        status=TeamTaskStatus.COMPLETED,
        spec=TaskSpec(goal="实现 JWT 登录"),
        result=TaskResult(output="使用 RS256 签名完成"),
    )
    lessons = extract_lessons_from_task(task)
    assert len(lessons) == 1
    kind, text = lessons[0]
    assert kind == "practice"
    assert "实现 JWT 登录" in text and "RS256" in text


def test_extract_completed_without_goal_skipped():
    # output 非空但无 spec.goal → 不写
    task = TeamTask(
        id="t1", project_id="p1", title="t",
        status=TeamTaskStatus.COMPLETED, output="done",
    )
    assert extract_lessons_from_task(task) == []


def test_extract_empty_content_skipped():
    # failed 但无 failure_reason → 不写
    task = TeamTask(id="t1", project_id="p1", title="t",
                    status=TeamTaskStatus.FAILED)
    assert extract_lessons_from_task(task) == []
    # 非终态 → 不写
    task2 = TeamTask(id="t2", project_id="p1", title="t",
                     status=TeamTaskStatus.IN_PROGRESS)
    assert extract_lessons_from_task(task2) == []


def test_extract_then_add_idempotent(store):
    # teammate 完成时写一次 + orchestrator 结算再写一次 → source_task_id 去重
    task = TeamTask(
        id="t1", project_id="p1", title="接入支付接口",
        status=TeamTaskStatus.FAILED, error="API 返回 401",
    )
    for _ in range(2):
        for kind, text in extract_lessons_from_task(task):
            _run(store.add_lesson("p1", "coder", kind, text,
                                  source_task_id=task.id))
    assert len(_l3(store, "p1", "coder")["pitfalls"]) == 1


# ──────────────────────────────────────────────────────────────────────────────
# 文件锁 / 原子写冒烟
# ──────────────────────────────────────────────────────────────────────────────

def test_file_lock_atomic_write_smoke(store, tmp_path):
    _run(store.add_lesson("p1", "coder", "practice", "alpha beta gamma",
                          source_task_id="t1"))
    path = store._l3_path("p1", "coder")
    assert path.exists()
    # 原子写不残留 tmp 文件
    assert not path.with_suffix(".json.tmp").exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["practices"]) == 1

    # 并发写入冒烟: 多个 add_lesson 并发不损坏文件 (容量淘汰到 3 条)
    async def _concurrent():
        await asyncio.gather(*[
            store.add_lesson("p1", "coder", "domain_note",
                             f"note unique{i} keyword{i}", source_task_id=f"c{i}")
            for i in range(5)
        ])

    _run(_concurrent())
    data = json.loads(path.read_text(encoding="utf-8"))  # 仍是合法 JSON
    assert len(data["domain_notes"]) == 3
