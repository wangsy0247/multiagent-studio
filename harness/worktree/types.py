"""Worktree types — data classes for worktree isolation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class WorktreeContext:
    """Context for an active git worktree."""

    name: str              # SubAgent name, e.g. "coder"
    path: Path             # Host path, e.g. .../workspace/.worktrees/coder_a1b2/
    branch: str            # Git branch, e.g. "subagent/coder/a1b2"
    virtual_path: str      # Sandbox path, e.g. "/mnt/user-data/workspace/.worktrees/coder_a1b2/"


@dataclass
class MergeResult:
    """Result of merging a worktree branch back to main."""

    status: Literal["ok", "conflict", "no_changes", "error"]
    files_changed: int = 0
    conflict_files: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class WorktreeConfig:
    """Configuration for the git worktree isolation system."""

    enabled: bool = True
    auto_init: bool = True                   # Auto git init non-git workspace
    symlink_deps: list[str] = field(default_factory=lambda: [".venv", "node_modules"])
    keep_on_conflict: bool = True            # Keep worktree on merge conflict
    cleanup_stale_on_start: bool = True      # Clean stale worktrees at startup
