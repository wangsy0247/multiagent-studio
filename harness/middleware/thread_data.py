"""ThreadDataMiddleware — per-thread workspace with virtual-to-physical path mapping."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import override

from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState

logger = logging.getLogger(__name__)

VIRTUAL_PATH_PREFIX = "/mnt/user-data"
THREAD_SUBDIRS = ("workspace", "uploads", "outputs")


def replace_virtual_path(path: str, thread_data: dict[str, str] | None) -> str:
    """Replace ``/mnt/user-data/*`` virtual paths with real host paths."""
    if thread_data is None:
        return path
    mappings: list[tuple[str, str]] = []
    for key, host_key in [
        ("workspace", "workspace_path"),
        ("uploads", "uploads_path"),
        ("outputs", "outputs_path"),
    ]:
        host = thread_data.get(host_key)
        if host:
            mappings.append((f"{VIRTUAL_PATH_PREFIX}/{key}", host))
    actual_dirs = [Path(p) for _, p in mappings]
    if actual_dirs and len(actual_dirs) == 3:
        common_parent = str(Path(actual_dirs[0]).parent)
        if all(str(p.parent) == common_parent for p in actual_dirs):
            mappings.append((VIRTUAL_PATH_PREFIX, common_parent))
    for virtual_base, actual_base in sorted(mappings, key=lambda x: len(x[0]), reverse=True):
        if path == virtual_base:
            return actual_base
        if path.startswith(f"{virtual_base}/"):
            rest = path[len(virtual_base):].lstrip("/")
            return f"{actual_base}/{rest}"
    return path


def mask_host_paths(output: str, thread_data: dict[str, str] | None) -> str:
    """Mask host absolute paths in output back to /mnt/user-data equivalents."""
    if thread_data is None:
        return output
    result = output
    mappings: list[tuple[str, str]] = []
    for key, host_key in [
        ("workspace", "workspace_path"),
        ("uploads", "uploads_path"),
        ("outputs", "outputs_path"),
    ]:
        host = thread_data.get(host_key)
        if host:
            mappings.append((host, f"{VIRTUAL_PATH_PREFIX}/{key}"))
    for actual_base, virtual_base in sorted(mappings, key=lambda x: len(x[0]), reverse=True):
        escaped = re.escape(actual_base).replace(r"\\", r"[/\\\\]")
        pattern = re.compile(escaped + r"(?:[/\\][^\s\"';&|<>()]*)?")
        def _replace(match: re.Match, ab=actual_base, vb=virtual_base) -> str:
            matched = match.group(0)
            if matched == ab:
                return vb
            rel = matched[len(ab):].lstrip("/\\")
            return f"{vb}/{rel}" if rel else vb
        result = pattern.sub(_replace, result)
    return result


class ThreadDataMiddleware(HarnessAgentMiddleware):
    """Initialise per-thread sandbox directories and virtual path state.

    Creates the DeerFlow-compliant layout::

        {data_root}/users/{user_id}/threads/{thread_id}/user-data/
            workspace/    — working directory
            uploads/      — user uploads
            outputs/      — final deliverables
    """

    name = "thread_data"

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    @override
    async def abefore_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        thread_id = state.get("thread_id", "unknown")
        user_id = state.get("user_id", "anonymous")

        from harness.config.paths import get_paths
        paths_obj = get_paths()
        paths_obj.ensure_thread_dirs(thread_id, user_id=user_id)

        user_data_root = paths_obj.sandbox_user_data_dir(thread_id, user_id=user_id)

        dirs: dict[str, str] = {}
        for sub in THREAD_SUBDIRS:
            d = user_data_root / sub
            dirs[f"{sub}_path"] = str(d)

        workspace_path = dirs["workspace_path"]

        logger.debug(
            "ThreadData initialized — thread=%s user=%s root=%s",
            thread_id, user_id, user_data_root,
        )
        return {
            "thread_data": dirs,
            "workspace": workspace_path,
            "thread_start_time": datetime.now(timezone.utc).isoformat(),
        }
