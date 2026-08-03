"""Team 成员级 worktree 隔离 (Phase 6) — 可选配置, 默认 shared.

成员 config.yaml 的 ``team.isolation: worktree`` 时, 该成员在 thread 共享
工作区 (git 仓库) 下创建独立 worktree ``.worktrees/team-{pid}-{agent}_{uid}``,
经 sandbox 挂载路径 ``/mnt/user-data/workspace/.worktrees/...`` 访问 —
sandbox 挂载不变, 隔离只发生在宿主机侧的文件落点 (git 分支级别)。

与 SubAgent worktree 的关键差异:
- **不 merge 回主分支** — 成员改动保留, run 结束仅 log 提示 worktree 路径
- 创建失败 (thread 工作区不是 git 仓库等) → 降级 shared, 不阻断 spawn
- 登记到项目级 ``worktrees.json``, 项目删除/团队解散时按清单回收
  (仅处理 ``team-`` 前缀, 防误删 SubAgent/手动创建的 worktree)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.worktree.types import WorktreeContext

logger = logging.getLogger(__name__)

# team worktree 命名前缀 — 回收时按此前缀过滤, 非前缀登记项一律不碰
TEAM_WORKTREE_PREFIX = "team-"
_REGISTRY_FILENAME = "worktrees.json"
_VALID_ISOLATION = {"shared", "worktree"}
_INVALID_NAME_CHARS_RE = re.compile(r"[^a-z0-9_-]+")


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

def resolve_member_isolation(effective_config: Any) -> str:
    """从 EffectiveConfig.raw 读取成员隔离配置, 返回 "shared" | "worktree".

    读取路径: team.isolation (优先) → 顶层 isolation (兼容) → 默认 shared.
    非法值 log warning 并降级 shared — 隔离是可选项, 配置错误不影响协作。
    """
    raw = getattr(effective_config, "raw", None) or {}
    team_cfg = raw.get("team") if isinstance(raw.get("team"), dict) else {}
    value = team_cfg.get("isolation", raw.get("isolation", "shared"))
    value = str(value).strip().lower()
    if value not in _VALID_ISOLATION:
        logger.warning(
            "未知的 isolation 配置 %r (合法值: shared|worktree) — 降级为 shared",
            value,
        )
        return "shared"
    return value


def sanitize_worktree_name(project_id: str, agent_name: str) -> str:
    """生成合法的 worktree 目录名: team-{project_id}-{agent_name}.

    与 GitWorktreeManager._validate_name 的 ``^[a-z0-9_-]{1,64}$`` 约束对齐:
    转小写, 非法字符折叠为 '-', 截断到 64 字符。
    """
    raw = f"{TEAM_WORKTREE_PREFIX}{project_id}-{agent_name}".lower()
    name = _INVALID_NAME_CHARS_RE.sub("-", raw).strip("-")
    return (name or "team-member")[:64]


# ---------------------------------------------------------------------------
# 登记清单 (项目级 worktrees.json)
# ---------------------------------------------------------------------------

def _registry_path(project_id: str, user_id: str) -> Path:
    """登记清单路径: {base}/users/{uid}/projects/{pid}/worktrees.json."""
    from harness.config.paths import get_paths
    return (
        get_paths().base_dir / "users" / user_id
        / "projects" / project_id / _REGISTRY_FILENAME
    )


def _load_registry(project_id: str, user_id: str) -> list[dict[str, Any]]:
    """加载登记清单 — 文件缺失/损坏均容错返回空列表."""
    p = _registry_path(project_id, user_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("worktree 登记清单损坏 (%s): %s — 按空清单处理", p, exc)
        return []
    return data if isinstance(data, list) else []


def _save_registry(project_id: str, user_id: str, entries: list[dict[str, Any]]) -> None:
    p = _registry_path(project_id, user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 创建 (复用 GitWorktreeManager, 与 SubAgent worktree 同一套机制)
# ---------------------------------------------------------------------------

async def create_member_worktree(
    project_id: str,
    user_id: str,
    thread_id: str,
    agent_name: str,
) -> WorktreeContext | None:
    """为 worktree 隔离成员创建 (或复用) 独立 worktree.

    - 同一成员在同一 thread 已登记且目录仍存在 → 复用 (跨 run 保留工作现场)
    - thread 工作区不是 git 仓库 / 创建失败 → 返回 None, 调用方降级 shared
    - 成功后登记到项目级 worktrees.json

    返回 WorktreeContext (virtual_path 为 sandbox 内路径), 失败返回 None。
    """
    from harness.config.paths import get_paths
    from harness.worktree.manager import GitWorktreeManager
    from harness.worktree.types import WorktreeConfig

    name = sanitize_worktree_name(project_id, agent_name)
    workspace = get_paths().sandbox_work_dir(thread_id, user_id=user_id)

    # ── 复用已登记的 worktree (跨 run / respawn 保留成员工作现场) ──
    entries = _load_registry(project_id, user_id)
    for entry in entries:
        if entry.get("member") == agent_name and entry.get("thread_id") == thread_id:
            path = Path(entry.get("path", ""))
            if path.exists():
                logger.info(
                    "复用成员 '%s' 已登记的 worktree: %s", agent_name, path,
                )
                return WorktreeContext(
                    name=name,
                    path=path,
                    branch=entry.get("branch", ""),
                    virtual_path=entry.get("virtual_path", ""),
                )

    # ── 创建新 worktree: auto_init=False — thread 工作区不是 git 仓库时
    # 直接失败降级 shared, 不为可选特性擅自 git init 共享工作区 ──
    workspace.mkdir(parents=True, exist_ok=True)
    mgr = GitWorktreeManager(
        str(workspace),
        WorktreeConfig(enabled=True, auto_init=False, symlink_deps=[]),
    )
    try:
        await mgr.ensure_git_repo()
        ctx = await mgr.create(name)
    except Exception as exc:
        logger.warning(
            "为成员 '%s' 创建 worktree 失败 (%s) — 降级为 shared 共享工作区",
            agent_name, exc,
        )
        return None

    entries.append({
        "member": agent_name,
        "thread_id": thread_id,
        "path": str(ctx.path),
        "virtual_path": ctx.virtual_path,
        "branch": ctx.branch,
        "repo": str(workspace),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    try:
        _save_registry(project_id, user_id, entries)
    except OSError as exc:
        # 登记失败不阻断 — worktree 已可用, 仅回收时无法按清单找到
        logger.warning("worktree 登记失败 (%s): %s", ctx.path, exc)

    logger.info(
        "成员 '%s' worktree 隔离就绪: path=%s branch=%s virtual=%s",
        agent_name, ctx.path, ctx.branch, ctx.virtual_path,
    )
    return ctx


# ---------------------------------------------------------------------------
# 回收 (项目删除/团队解散时按登记清单清理)
# ---------------------------------------------------------------------------

async def cleanup_project_worktrees(project_id: str, user_id: str) -> int:
    """回收项目登记的全部 team worktree. 返回成功回收数.

    安全约束:
    - 仅处理路径名以 ``team-`` 开头的登记项, 其他一律跳过 (防误删)
    - 改动**不保留** (项目已删除), 用 ``git worktree remove --force``
    - 清单缺失 / 单项失败 / repo 已不存在 均容错, 不阻断项目删除流程
    """
    entries = _load_registry(project_id, user_id)
    if not entries:
        return 0

    removed = 0
    for entry in entries:
        path_str = str(entry.get("path", ""))
        if not Path(path_str).name.startswith(TEAM_WORKTREE_PREFIX):
            logger.warning(
                "跳过非 %s 前缀的 worktree 登记项 (防误删): %s",
                TEAM_WORKTREE_PREFIX, path_str,
            )
            continue

        repo = str(entry.get("repo", ""))
        branch = str(entry.get("branch", ""))
        path = Path(path_str)

        if not path.exists():
            # 目录已不存在 (thread 已清理) — 仅尝试 prune git 元数据
            removed += 1
            if repo and Path(repo).exists():
                await _run_git_best_effort("worktree", "prune", cwd=repo)
            continue

        try:
            await _run_git("worktree", "remove", "--force", path_str, cwd=repo)
            removed += 1
            logger.info("回收 team worktree: %s", path_str)
        except RuntimeError as exc:
            logger.warning("git worktree remove 失败 (%s): %s — 保留目录", path_str, exc)
            continue

        # 分支删除为 best-effort (分支可能已不存在)
        if branch and repo:
            await _run_git_best_effort("branch", "-D", branch, cwd=repo)

    # ── 清理登记表本身 ──
    try:
        _registry_path(project_id, user_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("删除 worktree 登记清单失败: %s", exc)

    return removed


# ---------------------------------------------------------------------------
# git 子进程助手 (与 GitWorktreeManager 同款 asyncio subprocess 模式)
# ---------------------------------------------------------------------------

async def _run_git(*args: str, cwd: str) -> None:
    """运行 git 命令, 失败抛 RuntimeError."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"git {' '.join(args)}: {detail}")


async def _run_git_best_effort(*args: str, cwd: str) -> None:
    """运行 git 命令, 失败仅 log (用于 prune / branch -D 等清理性操作)."""
    try:
        await _run_git(*args, cwd=cwd)
    except RuntimeError as exc:
        logger.debug("git 清理操作失败 (忽略): %s", exc)
