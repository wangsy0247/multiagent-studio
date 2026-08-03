"""Phase 6: Team 成员级 worktree 隔离测试.

覆盖:
- 配置读取: isolation 默认 shared / 显式 worktree / 非法值降级 shared+warning
- worktree 创建: git 仓库中创建成功且命名正确 / 非 git 目录降级 shared 不阻断
- 回收: 按登记清单 remove / 非 team- 前缀不碰 / 清单缺失容错
- 默认 shared 路径行为不变 (prompt 不注入隔离段)
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path

import pytest

from harness.config.config_models import EffectiveConfig
from harness.team.worktree import (
    TEAM_WORKTREE_PREFIX,
    cleanup_project_worktrees,
    create_member_worktree,
    resolve_member_isolation,
    sanitize_worktree_name,
    _load_registry,
    _registry_path,
)


@pytest.fixture(autouse=True)
def temp_data_root(tmp_path, monkeypatch):
    """隔离数据目录, 避免污染真实用户数据."""
    monkeypatch.setenv("HARNESS_DATA_ROOT", str(tmp_path))
    from harness.config.paths import Paths, set_paths

    set_paths(Paths(base_dir=tmp_path))
    yield tmp_path
    set_paths(Paths())


def _git(*args: str, cwd: Path) -> None:
    """同步执行 git (测试用), 附带局部提交身份."""
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test",
         *args],
        cwd=str(cwd), check=True, capture_output=True,
    )


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=path)
    (path / "README.md").write_text("init")
    _git("add", "-A", cwd=path)
    _git("commit", "-m", "init", cwd=path)


def _workspace(tmp_path: Path, thread_id: str = "t1", user_id: str = "default") -> Path:
    return (
        tmp_path / "users" / user_id / "threads" / thread_id
        / "user-data" / "workspace"
    )


# ===================================================================
# 配置读取
# ===================================================================

class TestResolveMemberIsolation:
    def test_default_shared_when_unset(self):
        eff = EffectiveConfig(raw={})
        assert resolve_member_isolation(eff) == "shared"

    def test_default_shared_when_raw_missing(self):
        eff = EffectiveConfig()
        assert resolve_member_isolation(eff) == "shared"

    def test_explicit_worktree_via_team_section(self):
        eff = EffectiveConfig(raw={"team": {"isolation": "worktree"}})
        assert resolve_member_isolation(eff) == "worktree"

    def test_explicit_worktree_via_top_level(self):
        """顶层 isolation 键 (兼容写法) 同样生效, team.isolation 优先."""
        eff = EffectiveConfig(raw={"isolation": "worktree"})
        assert resolve_member_isolation(eff) == "worktree"
        eff2 = EffectiveConfig(raw={
            "isolation": "worktree", "team": {"isolation": "shared"},
        })
        assert resolve_member_isolation(eff2) == "shared"

    def test_invalid_value_degrades_to_shared_with_warning(self, caplog):
        eff = EffectiveConfig(raw={"team": {"isolation": "docker"}})
        with caplog.at_level(logging.WARNING):
            assert resolve_member_isolation(eff) == "shared"
        assert any("isolation" in r.message for r in caplog.records)

    def test_case_insensitive(self):
        eff = EffectiveConfig(raw={"team": {"isolation": "Worktree"}})
        assert resolve_member_isolation(eff) == "worktree"

    def test_end_to_end_via_config_loader(self, tmp_path):
        """agent config.yaml 的 team.isolation 经 ConfigLoader 合并后生效."""
        from harness.config.config_loader import ConfigLoader

        agent_dir = tmp_path / "users" / "u1" / "agents" / "coder"
        agent_dir.mkdir(parents=True)
        (agent_dir / "config.yaml").write_text(
            "model: gpt-4o\nteam:\n  isolation: worktree\n",
            encoding="utf-8",
        )
        eff = ConfigLoader.load_effective(
            user_id="u1", agent_name="coder", base_dir=tmp_path,
        )
        assert resolve_member_isolation(eff) == "worktree"


class TestSanitizeWorktreeName:
    def test_basic_naming(self):
        name = sanitize_worktree_name("proj1", "coder")
        assert name == f"{TEAM_WORKTREE_PREFIX}proj1-coder"

    def test_invalid_chars_folded(self):
        name = sanitize_worktree_name("Proj A", "代码/助手")
        assert name.startswith(TEAM_WORKTREE_PREFIX)
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in name)

    def test_truncated_to_64(self):
        name = sanitize_worktree_name("p" * 80, "a" * 80)
        assert len(name) <= 64
        assert name.startswith(TEAM_WORKTREE_PREFIX)


# ===================================================================
# worktree 创建
# ===================================================================

class TestCreateMemberWorktree:
    def test_create_in_git_repo(self, tmp_path):
        ws = _workspace(tmp_path)
        _init_git_repo(ws)

        ctx = asyncio.run(create_member_worktree(
            project_id="proj1", user_id="default",
            thread_id="t1", agent_name="coder",
        ))
        assert ctx is not None
        # 命名: team-{pid}-{agent}_{uid}, 位于共享工作区 .worktrees/ 下
        assert ctx.path.parent == ws / ".worktrees"
        assert ctx.path.name.startswith(f"{TEAM_WORKTREE_PREFIX}proj1-coder_")
        assert ctx.path.exists()
        assert ctx.virtual_path.startswith("/mnt/user-data/workspace/.worktrees/")
        # 登记清单已写入
        entries = _load_registry("proj1", "default")
        assert len(entries) == 1
        assert entries[0]["member"] == "coder"
        assert entries[0]["path"] == str(ctx.path)

    def test_reuse_registered_worktree(self, tmp_path):
        """同一成员同一 thread 重复创建 → 复用已登记 worktree (跨 run 保留现场)."""
        ws = _workspace(tmp_path)
        _init_git_repo(ws)

        ctx1 = asyncio.run(create_member_worktree(
            project_id="proj1", user_id="default",
            thread_id="t1", agent_name="coder",
        ))
        ctx2 = asyncio.run(create_member_worktree(
            project_id="proj1", user_id="default",
            thread_id="t1", agent_name="coder",
        ))
        assert ctx1 is not None and ctx2 is not None
        assert ctx1.path == ctx2.path
        assert len(_load_registry("proj1", "default")) == 1

    def test_non_git_workspace_degrades_to_none(self, tmp_path):
        """thread 工作区不是 git 仓库 → 返回 None (调用方降级 shared), 不抛异常."""
        ws = _workspace(tmp_path)
        ws.mkdir(parents=True)

        ctx = asyncio.run(create_member_worktree(
            project_id="proj1", user_id="default",
            thread_id="t1", agent_name="coder",
        ))
        assert ctx is None
        # 降级路径不产生登记
        assert _load_registry("proj1", "default") == []

    def test_missing_workspace_degrades_to_none(self, tmp_path):
        """工作区目录不存在 (非 git) → 同样降级, 不阻断."""
        ctx = asyncio.run(create_member_worktree(
            project_id="proj1", user_id="default",
            thread_id="t1", agent_name="coder",
        ))
        assert ctx is None


# ===================================================================
# 回收
# ===================================================================

class TestCleanupProjectWorktrees:
    def test_cleanup_removes_registered_worktree(self, tmp_path):
        ws = _workspace(tmp_path)
        _init_git_repo(ws)
        ctx = asyncio.run(create_member_worktree(
            project_id="proj1", user_id="default",
            thread_id="t1", agent_name="coder",
        ))
        assert ctx is not None and ctx.path.exists()

        removed = asyncio.run(cleanup_project_worktrees("proj1", "default"))
        assert removed == 1
        assert not ctx.path.exists()
        # 登记清单已清除
        assert not _registry_path("proj1", "default").exists()

    def test_cleanup_skips_non_team_prefix(self, tmp_path):
        """非 team- 前缀的登记项 (SubAgent/手动创建) 一律不碰."""
        ws = _workspace(tmp_path)
        _init_git_repo(ws)
        other = ws / ".worktrees" / "subagent_coder_abc123"
        other.mkdir(parents=True)
        (other / "keep.txt").write_text("do not touch")

        registry = _registry_path("proj1", "default")
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps([{
            "member": "coder", "thread_id": "t1",
            "path": str(other), "branch": "subagent/coder/abc123",
            "repo": str(ws),
        }]))

        removed = asyncio.run(cleanup_project_worktrees("proj1", "default"))
        assert removed == 0
        assert other.exists()  # 未被删除
        assert (other / "keep.txt").exists()

    def test_cleanup_missing_registry_is_noop(self, tmp_path):
        assert asyncio.run(cleanup_project_worktrees("nope", "default")) == 0

    def test_cleanup_tolerates_missing_directory(self, tmp_path):
        """登记项目录已不存在 (thread 已清理) → 容错按已回收处理."""
        ws = _workspace(tmp_path)
        _init_git_repo(ws)
        registry = _registry_path("proj1", "default")
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps([{
            "member": "coder", "thread_id": "t1",
            "path": str(ws / ".worktrees" / "team-proj1-coder_gone99"),
            "branch": "", "repo": str(ws),
        }]))

        removed = asyncio.run(cleanup_project_worktrees("proj1", "default"))
        assert removed == 1

    def test_cleanup_corrupted_registry_is_noop(self, tmp_path):
        registry = _registry_path("proj1", "default")
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text("{not json")
        assert asyncio.run(cleanup_project_worktrees("proj1", "default")) == 0


# ===================================================================
# 默认 shared 回归: prompt 不注入隔离段
# ===================================================================

def _make_member(worktree_virtual_path: str = ""):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from harness.team.context import TeamContext
    from harness.team.message_bus import TeamMessageBus
    from harness.team.models import TeamMemberRuntime, TeammateStatus
    from harness.team.task_store import TeamTaskStore
    from harness.team.teammate_agent import TeammateAgent

    ctx = TeamContext(
        project_id="test_proj",
        project_name="Test Project",
        project_description="",
        user_id="default",
        members=[
            TeamMemberRuntime(agent_name="lead", role="lead", status=TeammateStatus.IDLE),
            TeamMemberRuntime(agent_name="alice", role="member", status=TeammateStatus.IDLE),
        ],
    )
    return TeammateAgent(
        agent_name="alice",
        llm=FakeListChatModel(responses=["ok"]),
        tools=[],
        team_context=ctx,
        message_bus=TeamMessageBus("test_proj", user_id="default", thread_id="t1"),
        task_store=TeamTaskStore("test_proj", user_id="default", thread_id="t1"),
        role="member",
        thread_id="t1",
        worktree_virtual_path=worktree_virtual_path,
    )


class TestMemberPromptIsolation:
    def test_shared_default_has_no_isolation_section(self):
        """默认 shared — prompt 与 Phase 6 之前完全一致 (零行为变化)."""
        agent = _make_member()
        instructions = agent._get_member_instructions()
        assert "<workspace_isolation>" not in instructions

    def test_worktree_member_has_isolation_section(self):
        vpath = "/mnt/user-data/workspace/.worktrees/team-proj1-alice_a1b2c3/"
        agent = _make_member(worktree_virtual_path=vpath)
        instructions = agent._get_member_instructions()
        assert "<workspace_isolation>" in instructions
        assert vpath in instructions
