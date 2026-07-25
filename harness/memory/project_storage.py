"""Project memory storage — progressive-loading markdown files for team mode.

In team mode, each project can have a ``memory/`` directory with markdown files
that describe the project's architecture, conventions, environment, and folder
layout. These are injected into the agent's system prompt so every teammate
shares the same project context.

Loading strategy (progressive):

  L0 — Always loaded:  ``description.md`` (project overview, tech stack,
       architecture, folder descriptions). Injected on first turn.

  L1 — On-demand:      Other ``.md`` files in the memory directory. The agent
       can request them via a future tool (not yet implemented).

Directory layout::

    {project_root}/
    └── memory/
        ├── description.md    ← L0, always loaded
        ├── architecture.md   ← L1, on-demand
        ├── conventions.md    ← L1
        └── ...

*project_root* is resolved as:
  1. ``MemoryConfig.project_memory_root`` if explicitly configured
  2. The project directory derived from ``project_id`` (team mode)
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_ALWAYS_LOAD = "description.md"


# ── ProjectMemoryStorage ─────────────────────────────────────────────────────


class ProjectMemoryStorage:
    """Loads project memory markdown files with progressive loading.

    Only used in team mode. Single-agent mode does not load project memory.
    """

    def __init__(self, project_root: str = ""):
        """Initialise the project memory storage.

        Args:
            project_root: Path to the project directory. The ``memory/``
                subdirectory under this path is where markdown files live.
                If empty, the storage is effectively disabled until
                ``set_project_root()`` is called.
        """
        self._project_root = Path(project_root) if project_root else None
        self._cache: dict[str, str] = {}

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def memory_dir(self) -> Path | None:
        if self._project_root is None:
            return None
        return self._project_root / "memory"

    @property
    def is_available(self) -> bool:
        d = self.memory_dir
        return d is not None and d.is_dir()

    # ── Public API ───────────────────────────────────────────────────────

    def set_project_root(self, project_root: str) -> None:
        """Update the project root path (e.g. after project is loaded)."""
        self._project_root = Path(project_root)
        self._cache.clear()

    def load_description(self) -> str:
        """Load the always-loaded ``description.md`` (L0).

        Returns:
            The markdown content, or an empty string if the file is missing,
            unreadable, or project_memory is not available.
        """
        return self._load_file(_ALWAYS_LOAD)

    def load_by_name(self, filename: str) -> str:
        """Load a named ``.md`` file from the memory directory (L1).

        Only files directly under ``memory/`` can be loaded this way.
        The ``.md`` extension is appended if not already present.

        Args:
            filename: File name relative to the ``memory/`` directory
                (e.g. ``"architecture"`` or ``"architecture.md"``).

        Returns:
            The markdown content, or an empty string if not found.
        """
        if not filename.endswith(".md"):
            filename = f"{filename}.md"
        # Prevent path traversal
        safe_name = Path(filename).name
        return self._load_file(safe_name)

    def list_available(self) -> list[str]:
        """List all ``.md`` files in the memory directory."""
        if not self.is_available:
            return []
        return sorted(
            p.name for p in self.memory_dir.glob("*.md")  # type: ignore[union-attr]
        )

    # ── Internal ─────────────────────────────────────────────────────────

    def _load_file(self, filename: str) -> str:
        """Load a single file, with caching."""
        if not self.is_available:
            return ""

        file_path = self.memory_dir / filename  # type: ignore[operator]
        if not file_path.is_file():
            logger.debug("Project memory file not found: %s", file_path)
            return ""

        # Check cache (keyed by path for cache invalidation on set_project_root)
        cache_key = str(file_path)
        try:
            mtime = file_path.stat().st_mtime
        except OSError:
            return self._cache.pop(cache_key, "")

        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            content = file_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Failed to read project memory file %s: %s", file_path, e)
            return ""

        if not content:
            return ""

        # Cache and return
        self._cache[cache_key] = content
        logger.info(
            "Project memory loaded: %s (%d chars)", filename, len(content),
        )
        return content


# ── Global singleton ─────────────────────────────────────────────────────────

_project_storage: ProjectMemoryStorage | None = None


def get_project_memory_storage() -> ProjectMemoryStorage:
    """Get or create the global ``ProjectMemoryStorage`` singleton."""
    global _project_storage
    if _project_storage is None:
        _project_storage = ProjectMemoryStorage()
    return _project_storage


def reset_project_memory_storage() -> None:
    """Reset the global singleton (useful for tests)."""
    global _project_storage
    _project_storage = None
