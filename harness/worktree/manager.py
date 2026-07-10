"""Git worktree manager — create, merge, cleanup isolated worktrees for SubAgents.

Concurrency model:
- create(): concurrent — git worktree add has internal locking
- merge(): serialised — asyncio.Lock per repo (git requires atomic merges)
- cleanup(): called from merge() or on error — holds the same lock
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import uuid
from pathlib import Path

from harness.worktree.types import MergeResult, WorktreeConfig, WorktreeContext

logger = logging.getLogger(__name__)

# SubAgent names are already validated by SubAgentConfig.  This is an
# additional safety net to reject names that contain filesystem-significant
# characters a malicious LLM tool call might inject.
_SAFE_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


class GitWorktreeManager:
    """Manage git worktrees for SubAgent file-system isolation.

    Worktrees are created under ``{workspace}/.worktrees/{name}_{uuid}``
    and automatically added to ``.gitignore`` so they are never tracked.

    Parameters
    ----------
    workspace_path : str
        Full host path to the main workspace directory (must contain or
        become a git repository).
    config : WorktreeConfig
        Feature flags and tuning knobs.
    """

    def __init__(self, workspace_path: str, config: WorktreeConfig | None = None):
        self._workspace = Path(workspace_path).resolve()
        self._config = config or WorktreeConfig()
        self._merge_lock = asyncio.Lock()
        self._worktree_dir = self._workspace / ".worktrees"

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def ensure_git_repo(self) -> None:
        """Make sure *workspace* is a git repository.

        When ``auto_init`` is enabled and the directory is not already a
        repo, initialise one and create an initial empty commit so that
        ``git worktree add`` has a base ref to branch from.
        """
        git_dir = self._workspace / ".git"
        if git_dir.exists():
            return

        if not self._config.auto_init:
            raise RuntimeError(
                f"Workspace {self._workspace} is not a git repository "
                f"and auto_init is disabled"
            )

        logger.info("Initialising git repository in %s", self._workspace)
        self._workspace.mkdir(parents=True, exist_ok=True)
        await self._run_git("init", cwd=self._workspace)

        # Create .gitignore so worktrees are never tracked.
        gitignore = self._workspace / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(".worktrees/\n")

        # Create an initial commit so branches have a common ancestor.
        # Allow-empty is needed when the workspace is empty.
        await self._run_git("add", "-A", cwd=self._workspace)
        try:
            await self._run_git(
                "commit", "-m", "Initial commit (auto)",
                "--allow-empty", cwd=self._workspace,
            )
        except RuntimeError:
            # Already committed or nothing to commit — ok.
            pass

    async def create(self, name: str) -> WorktreeContext:
        """Create an isolated git worktree for a SubAgent.

        Parameters
        ----------
        name : str
            SubAgent name (e.g. "coder").  Must pass ``_validate_name``.

        Returns
        -------
        WorktreeContext
        """
        name = self._validate_name(name)

        # Ensure .worktrees/ exists and is gitignored.
        self._worktree_dir.mkdir(parents=True, exist_ok=True)
        await self._ensure_gitignored(".worktrees/")

        uid = uuid.uuid4().hex[:6]
        safe_name = f"{name}_{uid}"
        worktree_path = self._worktree_dir / safe_name
        branch = f"subagent/{name}/{uid}"

        # If the worktree directory already exists (e.g. leftover from a
        # crash), remove it first so git worktree add can proceed.
        if worktree_path.exists():
            logger.warning("Removing stale worktree directory %s", worktree_path)
            await asyncio.to_thread(shutil.rmtree, str(worktree_path))

        logger.info(
            "Creating worktree for '%s': path=%s branch=%s",
            name, worktree_path, branch,
        )
        await self._run_git(
            "worktree", "add", str(worktree_path),
            "-b", branch,
            cwd=self._workspace,
        )

        # Environment setup (best-effort, errors are non-fatal).
        await self._symlink_deps(worktree_path)
        await self._copy_configs(worktree_path)

        virtual_path = f"/mnt/user-data/workspace/.worktrees/{safe_name}/"

        logger.info(
            "Worktree ready: name=%s path=%s branch=%s virtual=%s",
            name, worktree_path, branch, virtual_path,
        )
        return WorktreeContext(
            name=name,
            path=worktree_path,
            branch=branch,
            virtual_path=virtual_path,
        )

    async def merge(self, ctx: WorktreeContext) -> MergeResult:
        """Merge worktree changes back into the main branch.

        Acquires ``_merge_lock`` so only one merge runs at a time per repo.
        """
        async with self._merge_lock:
            return await self._merge_impl(ctx)

    async def cleanup(self, ctx: WorktreeContext) -> None:
        """Remove the worktree and its branch.

        Acquires ``_merge_lock`` to avoid racing with an in-flight merge.
        """
        async with self._merge_lock:
            await self._cleanup_impl(ctx)

    async def cleanup_stale(self) -> int:
        """Remove stale worktrees left over from previous crashes.

        Returns the number of worktrees removed.
        """
        count = 0

        # 1. git worktree prune — removes git metadata for missing directories
        try:
            await self._run_git("worktree", "prune", cwd=self._workspace)
        except RuntimeError as exc:
            logger.warning("git worktree prune failed: %s", exc)

        # 2. Remove orphan directories under .worktrees/ not tracked by git
        if not self._worktree_dir.exists():
            return count

        try:
            active = set(await self.list_worktrees())
        except RuntimeError:
            active = set()

        for entry in list(self._worktree_dir.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name not in active:
                logger.info("Removing stale worktree directory %s", entry)
                try:
                    await asyncio.to_thread(shutil.rmtree, str(entry))
                    count += 1
                except OSError as exc:
                    logger.warning("Failed to remove %s: %s", entry, exc)

            # 3. Prune stale branches
            try:
                await self._run_git("branch", "-D", f"subagent/{entry.name}", cwd=self._workspace)
            except RuntimeError:
                pass  # branch may not exist

        return count

    async def list_worktrees(self) -> list[str]:
        """List active git worktree directory names.

        Parses ``git worktree list --porcelain`` output.
        """
        output, _ = await self._run_git_capture("worktree", "list", "--porcelain", cwd=self._workspace)
        names: list[str] = []
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("worktree "):
                wt_path = Path(line.split(" ", 1)[1])
                if self._worktree_dir in wt_path.parents or wt_path.parent == self._worktree_dir:
                    names.append(wt_path.name)
        return names

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    async def _get_default_branch(self) -> str:
        """Return the name of the default branch (main or master)."""
        try:
            output, _ = await self._run_git_capture(
                "rev-parse", "--abbrev-ref", "HEAD",
                cwd=self._workspace,
            )
            branch = output.strip()
            if branch and branch != "HEAD":
                return branch
        except RuntimeError:
            pass
        # Fallback: check if main or master exists.
        for candidate in ("main", "master"):
            try:
                await self._run_git(
                    "rev-parse", "--verify", candidate,
                    cwd=self._workspace,
                )
                return candidate
            except RuntimeError:
                continue
        return "main"  # last resort

    @staticmethod
    def _validate_name(name: str) -> str:
        """Reject names that could escape the worktree directory."""
        if not name or not _SAFE_NAME_RE.match(name):
            raise ValueError(
                f"Invalid worktree name '{name}'. "
                f"Must match {_SAFE_NAME_RE.pattern}"
            )
        return name

    async def _ensure_gitignored(self, pattern: str) -> None:
        """Append *pattern* to ``.gitignore`` if not already present."""
        gitignore = self._workspace / ".gitignore"
        try:
            lines = (
                gitignore.read_text().splitlines()
                if gitignore.exists()
                else []
            )
        except OSError:
            lines = []

        if pattern not in lines:
            lines.append(pattern)
            gitignore.write_text("\n".join(lines) + "\n")

    async def _merge_impl(self, ctx: WorktreeContext) -> MergeResult:
        """Merge implementation — caller MUST hold ``_merge_lock``."""
        default_branch = await self._get_default_branch()

        # Stage and commit in the worktree.
        try:
            await self._run_git("add", "-A", cwd=ctx.path)
            await self._run_git(
                "commit", "-m",
                f"subagent({ctx.name}): automated commit",
                "--allow-empty",
                cwd=ctx.path,
            )
        except RuntimeError as exc:
            logger.warning("Commit in worktree failed: %s", exc)

        # Detect if there are changes to merge (commits ahead of default branch).
        try:
            output, _ = await self._run_git_capture(
                "rev-list", "--count", f"{default_branch}..{ctx.branch}",
                cwd=self._workspace,
            )
            ahead = int(output.strip())
        except (ValueError, RuntimeError):
            ahead = 0

        if ahead == 0:
            logger.info(
                "Worktree '%s' has no changes to merge (branch=%s)",
                ctx.name, ctx.branch,
            )
            return MergeResult(status="no_changes", summary="No changes to merge")

        # Switch to default branch and merge.
        try:
            await self._run_git("checkout", default_branch, cwd=self._workspace)
        except RuntimeError:
            # Workspace might have uncommitted changes — try stash.
            try:
                await self._run_git("stash", cwd=self._workspace)
                await self._run_git("checkout", default_branch, cwd=self._workspace)
            except RuntimeError as exc:
                return MergeResult(
                    status="error",
                    summary=f"Cannot checkout {default_branch}: {exc}",
                )

        try:
            await self._run_git(
                "merge", "--no-ff", ctx.branch,
                "-m", f"Merge subagent({ctx.name}) into {default_branch}",
                cwd=self._workspace,
            )
            logger.info(
                "Merge of '%s' OK (branch=%s → %s)",
                ctx.name, ctx.branch, default_branch,
            )
            return MergeResult(
                status="ok",
                files_changed=ahead,
                summary=f"Successfully merged {ctx.branch} into {default_branch}",
            )
        except RuntimeError as exc:
            # Merge conflict — keep worktree for inspection if configured.
            msg = str(exc)
            logger.warning("Merge conflict for '%s': %s", ctx.name, msg)
            conflict_files = await self._get_conflict_files()
            if not self._config.keep_on_conflict:
                await self._run_git("merge", "--abort", cwd=self._workspace)
            return MergeResult(
                status="conflict",
                conflict_files=conflict_files,
                summary=f"Merge conflict: {msg[:200]}",
            )

    async def _cleanup_impl(self, ctx: WorktreeContext) -> None:
        """Cleanup implementation — caller MUST hold ``_merge_lock``."""
        if ctx.path.exists():
            try:
                await self._run_git(
                    "worktree", "remove", "--force", str(ctx.path),
                    cwd=self._workspace,
                )
                logger.info("Removed worktree %s", ctx.path)
            except RuntimeError as exc:
                logger.warning("git worktree remove failed: %s — force-removing", exc)
                await asyncio.to_thread(shutil.rmtree, str(ctx.path))

        try:
            await self._run_git("branch", "-D", ctx.branch, cwd=self._workspace)
        except RuntimeError:
            pass  # branch may already be gone

        # Clean up .worktrees/ parent if it's now empty.
        try:
            if self._worktree_dir.exists():
                remaining = list(self._worktree_dir.iterdir())
                if not remaining:
                    self._worktree_dir.rmdir()
        except OSError:
            pass

    async def _get_conflict_files(self) -> list[str]:
        """Return list of files with merge conflicts."""
        try:
            output, _ = await self._run_git_capture(
                "diff", "--name-only", "--diff-filter=U",
                cwd=self._workspace,
            )
            return [f for f in output.splitlines() if f.strip()]
        except RuntimeError:
            return []

    async def _symlink_deps(self, worktree_path: Path) -> None:
        """Symlink large dependency directories from the main workspace."""
        for dep in self._config.symlink_deps:
            src = self._workspace / dep
            dst = worktree_path / dep
            if src.exists() and not dst.exists():
                try:
                    dst.symlink_to(src, target_is_directory=True)
                    logger.debug("Symlinked %s → %s", dst, src)
                except OSError as exc:
                    logger.debug("Failed to symlink %s: %s", dep, exc)

    async def _copy_configs(self, worktree_path: Path) -> None:
        """Copy configuration files that are gitignored but needed at runtime."""
        for pattern in [".env", ".env.local", "config.local.yaml"]:
            src = self._workspace / pattern
            if src.exists():
                dst = worktree_path / pattern
                if not dst.exists():
                    try:
                        shutil.copy2(str(src), str(dst))
                        logger.debug("Copied %s → %s", src, dst)
                    except OSError as exc:
                        logger.debug("Failed to copy %s: %s", pattern, exc)

    # ------------------------------------------------------------------
    # git helpers
    # ------------------------------------------------------------------

    async def _run_git(self, *args: str, cwd: Path) -> None:
        """Run a git command, raise RuntimeError on failure."""
        await self._run_git_impl(args, cwd, capture=False)

    async def _run_git_capture(self, *args: str, cwd: Path) -> tuple[str, str]:
        """Run a git command, return (stdout, stderr)."""
        return await self._run_git_impl(args, cwd, capture=True)  # type: ignore[return-value]

    @staticmethod
    async def _run_git_impl(
        args: tuple[str, ...], cwd: Path, *, capture: bool
    ) -> tuple[str, str] | None:
        """Execute git and return (stdout, stderr) or None."""
        cmd = ["git", *args]
        logger.debug("git: %s (cwd=%s)", " ".join(cmd), cwd)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )
        stdout, stderr = await proc.communicate()
        stdout_str = stdout.decode("utf-8", errors="replace").strip()
        stderr_str = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            detail = stderr_str or stdout_str or f"exit code {proc.returncode}"
            raise RuntimeError(f"git {' '.join(args)}: {detail}")

        if capture:
            return stdout_str, stderr_str
        return None
