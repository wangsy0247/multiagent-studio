"""Skill archive installer — validates and extracts ``.skill`` packages.

A ``.skill`` archive is a ZIP file containing:
    SKILL.md            (required — YAML frontmatter + Markdown body)
    references/         (optional — documentation, checklists, etc.)
    templates/          (optional — report templates, config templates)
    scripts/            (optional — executable helper scripts)
    assets/             (optional — images, fonts, static resources)

Files outside these directories are rejected.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Allowed top-level directories inside a .skill archive.
_ALLOWED_TOP_LEVEL = frozenset({"SKILL.md", "references", "templates", "scripts", "assets"})

# Directories whose contents are treated as executable.
_EXECUTABLE_DIRS = frozenset({"scripts"})


async def install_skill_from_archive(
    archive_path: Path,
    target_root: Path,
    category: str = "custom",
    *,
    force: bool = False,
    model_client: Any | None = None,
) -> str:
    """Install a skill from a ``.skill`` ZIP archive.

    Workflow:
    1. Extract to a staging directory.
    2. Validate SKILL.md exists and has valid frontmatter.
    3. Security-scan SKILL.md content.
    4. Security-scan every file under ``scripts/`` (executable=true).
    5. If *force* is False, check that the skill name doesn't already exist.
    6. Move from staging to ``<target_root>/<category>/<name>/``.
    7. Clean up staging on failure.

    Args:
        archive_path: Path to the ``.skill`` ZIP file.
        target_root: Root directory containing ``public/`` and ``custom/``
            skill category directories.
        category: Target category — ``"custom"`` (default) or ``"public"``.
        force: When ``True``, overwrite an existing skill with the same name.
        model_client: LLM client for security scanning.  When ``None``,
            installation is blocked.

    Returns:
        The skill name that was installed.

    Raises:
        ValueError: Validation or security scan failure.
        FileExistsError: Skill already exists and ``force`` is False.
        OSError: Archive extraction failure.
    """
    if not archive_path.exists():
        raise ValueError(f"Archive not found: {archive_path}")

    staging = Path(tempfile.mkdtemp(prefix="skill-install-"))
    skill_name: str | None = None

    try:
        # ── 1. Extract to staging ──
        _extract_archive(archive_path, staging)

        # ── 2. Locate and parse SKILL.md ──
        skill_md = staging / "SKILL.md"
        if not skill_md.exists():
            raise ValueError(
                "Archive must contain SKILL.md at the root. "
                f"Found: {sorted(p.name for p in staging.iterdir())}"
            )

        content = skill_md.read_text(encoding="utf-8")

        # Validate frontmatter
        from harness.skills.parser import parse_skill_file
        from harness.skills.types import SkillCategory

        parsed = parse_skill_file(
            skill_md,
            category=SkillCategory(category),
            relative_path=Path(staging.name),
        )
        if parsed is None:
            raise ValueError(
                "SKILL.md frontmatter is invalid.  Ensure it has valid YAML "
                "between --- fences with at least 'name' and 'description' fields."
            )

        # Full validation
        from harness.skills.validation import _validate_skill_frontmatter

        is_valid, msg, skill_name = _validate_skill_frontmatter(staging)
        if not is_valid:
            raise ValueError(f"SKILL.md validation failed: {msg}")

        assert skill_name is not None

        # ── 3. Security scan SKILL.md ──
        from harness.skills.security_scanner import scan_skill_content

        scan_result = await scan_skill_content(
            content, executable=False, model_client=model_client,
        )
        if scan_result.is_blocked:
            raise ValueError(
                f"Security scan blocked SKILL.md: {scan_result.reason}"
            )
        if scan_result.decision == "warn":
            logger.warning(
                "Skill '%s' SKILL.md scan returned WARN: %s",
                skill_name,
                scan_result.reason,
            )

        # ── 4. Security scan scripts/ (executable=true) ──
        scripts_dir = staging / "scripts"
        if scripts_dir.exists() and scripts_dir.is_dir():
            for script_file in scripts_dir.rglob("*"):
                if not script_file.is_file():
                    continue
                script_content = script_file.read_text(encoding="utf-8")
                script_scan = await scan_skill_content(
                    script_content,
                    executable=True,
                    model_client=model_client,
                )
                if script_scan.is_blocked:
                    raise ValueError(
                        f"Security scan blocked {script_file.name}: "
                        f"{script_scan.reason}"
                    )

        # ── 5. Check for existing skill ──
        target_dir = target_root / category / skill_name
        if target_dir.exists() and not force:
            raise FileExistsError(
                f"Skill '{skill_name}' already exists. Use force=True to overwrite."
            )

        # ── 6. Move to target ──
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(target_dir))

        logger.info("Installed skill '%s' to %s", skill_name, target_dir)
        return skill_name

    except Exception:
        # ── 7. Clean up staging on failure ──
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _extract_archive(archive_path: Path, staging: Path) -> None:
    """Extract and validate a .skill ZIP archive to the staging directory.

    Raises ValueError for path-traversal attempts or disallowed top-level entries.
    """
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            # Pre-scan: check for path traversal and disallowed entries
            for member in zf.namelist():
                # Normalise path separators
                norm = member.replace("\\", "/")

                # Skip directory entries
                if norm.endswith("/"):
                    continue

                # Resolve to detect traversal
                resolved = (staging / norm).resolve()
                if not str(resolved).startswith(str(staging.resolve())):
                    raise ValueError(
                        f"Path traversal detected in archive: {member}"
                    )

                # Check top-level entry
                top = norm.split("/")[0]
                if top not in _ALLOWED_TOP_LEVEL:
                    raise ValueError(
                        f"Disallowed top-level entry '{top}' in archive. "
                        f"Allowed: {sorted(_ALLOWED_TOP_LEVEL)}"
                    )

            # Safe to extract
            zf.extractall(staging)

    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid or corrupted .skill archive: {exc}")


def ensure_safe_support_path(relative_path: str) -> Path:
    """Validate and normalise a support-file relative path.

    Only allows files under ``references/``, ``templates/``, ``scripts/``,
    or ``assets/`` directories.  Rejects path traversal, absolute paths,
    and disallowed directories.

    Returns a ``Path`` object safe to resolve under a skill directory.

    Raises:
        ValueError: Path is outside allowed directories or contains traversal.
    """
    if not relative_path or not relative_path.strip():
        raise ValueError("relative_path must not be empty")

    # Normalise separators
    norm = relative_path.replace("\\", "/")

    # Reject absolute paths
    if norm.startswith("/"):
        raise ValueError("Absolute paths are not allowed")

    # Reject traversal
    parts = norm.split("/")
    if ".." in parts:
        raise ValueError("Path traversal (..) is not allowed")

    cleaned = Path(norm)

    # Must resolve under one of the allowed subdirectories
    top = parts[0] if parts else ""
    if top not in _ALLOWED_TOP_LEVEL or top == "SKILL.md":
        raise ValueError(
            f"Support files must be under one of: references, templates, "
            f"scripts, assets.  Got: {top}"
        )

    # Double-check: resolve against a fake root to catch any shenanigans
    fake_root = Path("/fake-skill")
    resolved = (fake_root / cleaned).resolve()
    if not str(resolved).startswith("/fake-skill"):
        raise ValueError(f"Path resolves outside skill directory: {relative_path}")

    return cleaned
