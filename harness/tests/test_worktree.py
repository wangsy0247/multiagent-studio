"""Integration tests for GitWorktreeManager and SubAgent worktree isolation."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness.models import SubAgentConfig
from harness.worktree import GitWorktreeManager, MergeResult, WorktreeConfig


def _dummy_config(**overrides) -> SubAgentConfig:
    defaults = {
        "name": "test_agent",
        "display_name": "Test Agent",
        "description": "Test subagent",
        "system_prompt": "You are a test agent.",
        "isolation": "none",
    }
    defaults.update(overrides)
    return SubAgentConfig(**defaults)


class TestWorktreeConfig:
    def test_defaults(self):
        cfg = WorktreeConfig()
        assert cfg.enabled is True
        assert cfg.auto_init is True
        assert cfg.keep_on_conflict is True
        assert cfg.cleanup_stale_on_start is True

    def test_custom(self):
        cfg = WorktreeConfig(enabled=False, auto_init=False)
        assert cfg.enabled is False
        assert cfg.auto_init is False


class TestGitWorktreeManager:
    """Functional tests for GitWorktreeManager using real git."""

    def test_ensure_git_repo_creates_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.join(tmp, "workspace")
            os.makedirs(ws)
            mgr = GitWorktreeManager(ws, WorktreeConfig(auto_init=True))
            asyncio.run(mgr.ensure_git_repo())
            assert (Path(ws) / ".git").exists()
            assert (Path(ws) / ".gitignore").exists()
            gitignore = (Path(ws) / ".gitignore").read_text()
            assert ".worktrees/" in gitignore

    def test_ensure_git_repo_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.join(tmp, "workspace")
            os.makedirs(ws)
            mgr = GitWorktreeManager(ws, WorktreeConfig(auto_init=True))
            asyncio.run(mgr.ensure_git_repo())
            # Second call should not raise
            asyncio.run(mgr.ensure_git_repo())

    def test_validate_name_rejects_dangerous(self):
        mgr = GitWorktreeManager("/tmp/test")
        for bad in ("", "a" * 65, "../escape", "has space", "with.dot"):
            with pytest.raises(ValueError):
                mgr._validate_name(bad)

    def test_validate_name_accepts_safe(self):
        mgr = GitWorktreeManager("/tmp/test")
        for good in ("coder", "my-agent", "researcher_01", "a-b_c"):
            assert mgr._validate_name(good) == good

    def test_create_worktree_isolated(self):
        """Files written in worktree must NOT appear in main workspace before merge."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.join(tmp, "workspace")
            os.makedirs(ws)

            async def _test():
                mgr = GitWorktreeManager(ws, WorktreeConfig(auto_init=True))
                await mgr.ensure_git_repo()
                ctx = await mgr.create("coder")
                (ctx.path / "main.py").write_text("print('hello')")
                # Isolation check
                assert not (Path(ws) / "main.py").exists()
                # Merge
                result = await mgr.merge(ctx)
                assert result.status == "ok"
                assert (Path(ws) / "main.py").exists()
                assert (Path(ws) / "main.py").read_text() == "print('hello')"
                await mgr.cleanup(ctx)
                assert not ctx.path.exists()

            asyncio.run(_test())

    def test_merge_no_changes(self):
        """Worktree with no modifications returns no_changes status."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.join(tmp, "workspace")
            os.makedirs(ws)

            async def _test():
                mgr = GitWorktreeManager(ws, WorktreeConfig(auto_init=True))
                await mgr.ensure_git_repo()
                ctx = await mgr.create("noop")
                result = await mgr.merge(ctx)
                assert result.status in ("ok", "no_changes")
                await mgr.cleanup(ctx)

            asyncio.run(_test())

    def test_concurrent_worktrees(self):
        """Multiple worktrees can be created, modified, and merged without conflict."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.join(tmp, "workspace")
            os.makedirs(ws)

            async def _test():
                mgr = GitWorktreeManager(ws, WorktreeConfig(auto_init=True))
                await mgr.ensure_git_repo()

                N = 3
                contexts = []
                for i in range(N):
                    ctx = await mgr.create(f"worker_{i}")
                    (ctx.path / f"out_{i}.txt").write_text(f"worker {i} result")
                    contexts.append(ctx)

                for i, ctx in enumerate(contexts):
                    result = await mgr.merge(ctx)
                    assert result.status == "ok", f"worker_{i}: {result.status}"
                    await mgr.cleanup(ctx)
                    assert (Path(ws) / f"out_{i}.txt").exists()

                # Verify all files present
                for i in range(N):
                    assert (Path(ws) / f"out_{i}.txt").read_text() == f"worker {i} result"

            asyncio.run(_test())

    def test_stale_cleanup(self):
        """cleanup_stale removes orphan .worktrees directories."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.join(tmp, "workspace")
            os.makedirs(ws)

            async def _test():
                mgr = GitWorktreeManager(ws, WorktreeConfig(auto_init=True))
                await mgr.ensure_git_repo()

                # Manually create orphan directory
                orphan = Path(ws) / ".worktrees" / "orphan_abc"
                orphan.mkdir(parents=True, exist_ok=True)
                (orphan / "junk.txt").write_text("stale")

                removed = await mgr.cleanup_stale()
                assert removed >= 1
                assert not orphan.exists()

            asyncio.run(_test())

    def test_list_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.join(tmp, "workspace")
            os.makedirs(ws)

            async def _test():
                mgr = GitWorktreeManager(ws, WorktreeConfig(auto_init=True))
                await mgr.ensure_git_repo()
                ctx = await mgr.create("list_test")
                names = await mgr.list_worktrees()
                assert len(names) >= 1
                await mgr.cleanup(ctx)

            asyncio.run(_test())


class TestSubAgentConfigIsolation:
    def test_default_is_none(self):
        cfg = _dummy_config()
        assert cfg.isolation == "none"

    def test_worktree_isolation_field(self):
        cfg = _dummy_config(isolation="worktree")
        assert cfg.isolation == "worktree"

    def test_invalid_isolation_accepted_by_pydantic(self):
        """Pydantic doesn't validate the value — it's just a string flag."""
        cfg = _dummy_config(isolation="custom")
        assert cfg.isolation == "custom"


class TestWorktreeContext:
    def test_context_creation(self):
        from harness.worktree.types import WorktreeContext
        ctx = WorktreeContext(
            name="coder",
            path=Path("/tmp/.worktrees/coder_abc"),
            branch="subagent/coder/abc",
            virtual_path="/mnt/user-data/workspace/.worktrees/coder_abc/",
        )
        assert ctx.name == "coder"
        assert str(ctx.path).endswith("coder_abc")


class TestMergeResult:
    def test_ok_result(self):
        r = MergeResult(status="ok", files_changed=3)
        assert r.status == "ok"
        assert r.files_changed == 3

    def test_conflict_result(self):
        r = MergeResult(status="conflict", conflict_files=["a.py", "b.py"])
        assert r.status == "conflict"
        assert len(r.conflict_files) == 2
