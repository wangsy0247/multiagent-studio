"""Skill management REST API — list, get, enable/disable, install, CRUD, history, rollback.

Registered under ``/api/skills`` in the App service (port 8000).

All write operations go through validation + security scanning.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills", tags=["技能管理"])


# ---------------------------------------------------------------------------
# Dependency — get skill storage via the harness service
# ---------------------------------------------------------------------------


def _get_skill_storage() -> Any:
    """Resolve the ``SkillStorage`` instance from the harness singleton."""
    try:
        from harness.api.server import get_harness

        harness = get_harness()
        storage = getattr(harness, "skill_storage", None)
        if storage is None:
            raise HTTPException(
                status_code=503,
                detail="Skill storage is not initialised",
            )
        return storage
    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="Harness service is not available",
        )


def _get_extensions_config_path() -> Path:
    """Resolve the extensions_config.json path."""
    import os

    if env := os.getenv("EXTENSIONS_CONFIG_PATH"):
        return Path(env)
    return (
        Path(os.path.dirname(os.path.abspath(__file__)))
        .parent / "extensions_config.json"
    )


def _read_extensions_config() -> dict[str, Any]:
    """Read the current extensions_config.json as a dict."""
    path = _get_extensions_config_path()
    if not path.exists():
        return {"mcpServers": {}, "skills": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read extensions_config.json: %s", exc)
        return {"mcpServers": {}, "skills": {}}


def _write_extensions_config(config: dict[str, Any]) -> None:
    """Atomically write extensions_config.json."""
    path = _get_extensions_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile

    content = json.dumps(config, indent=2, ensure_ascii=False)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _refresh_cache() -> None:
    """Invalidate the skills prompt cache after mutation."""
    try:
        from harness.skills.cache import refresh_skills_system_prompt_cache

        refresh_skills_system_prompt_cache()
    except Exception:
        logger.warning("Failed to refresh skills cache", exc_info=True)


def _invalidate_graph_cache(user_id: str | None = None) -> None:
    """Invalidate the per-user graph cache so system prompts are rebuilt.

    Must be called after any skill mutation (toggle, create, update, delete,
    install, rollback) because the ``<skill_system>`` block in the system
    prompt is compiled into the graph at build time and cached until shutdown.
    """
    try:
        from harness.api.server import get_harness

        harness = get_harness()
        harness.invalidate_graph_cache(user_id=user_id)
    except Exception:
        logger.warning("Failed to invalidate graph cache", exc_info=True)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class SkillSummary(BaseModel):
    """Lightweight skill info for list endpoints."""

    name: str
    description: str
    category: str  # "builtin" or user-private (has user_id)
    enabled: bool
    allowed_tools: list[str] | None = None
    license: str | None = None


class SkillDetail(BaseModel):
    """Full skill info for detail endpoint."""

    name: str
    description: str
    category: str
    enabled: bool
    allowed_tools: list[str] | None = None
    license: str | None = None
    location: str  # container path to SKILL.md
    has_support_files: bool = False


class SkillToggleRequest(BaseModel):
    """Enable or disable a skill."""

    enabled: bool


class SkillContentRequest(BaseModel):
    """Create or edit a custom skill."""

    content: str = Field(..., description="Full SKILL.md content")


class SkillInstallRequest(BaseModel):
    """Install a skill from a .skill archive path."""

    archive_path: str = Field(..., description="Path to the .skill ZIP file")
    force: bool = False


class SkillRollbackRequest(BaseModel):
    """Rollback to a specific history version."""

    target_index: int = Field(..., ge=0, description="Index in the history array to rollback to")


# ---------------------------------------------------------------------------
# Helper — build skill summary/detail from Skill object
# ---------------------------------------------------------------------------


def _skill_summary(skill: Any) -> SkillSummary:
    return SkillSummary(
        name=skill.name,
        description=skill.description,
        category=str(skill.category),
        enabled=skill.enabled,
        allowed_tools=skill.allowed_tools,
        license=skill.license,
    )


def _skill_detail(skill: Any) -> SkillDetail:
    # Detect support files
    skill_dir = skill.skill_dir
    has_support = False
    for sub in ("references", "templates", "scripts", "assets"):
        sub_path = skill_dir / sub
        if sub_path.exists() and sub_path.is_dir() and any(sub_path.iterdir()):
            has_support = True
            break

    return SkillDetail(
        name=skill.name,
        description=skill.description,
        category=str(skill.category),
        enabled=skill.enabled,
        allowed_tools=skill.allowed_tools,
        license=skill.license,
        location=skill.get_container_file_path(),
        has_support_files=has_support,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════


@router.get("")
async def list_skills(
    enabled_only: bool = Query(False, description="Only return enabled skills"),
    storage: Any = Depends(_get_skill_storage),
) -> dict[str, Any]:
    """List all skills with optional enabled-only filter."""
    try:
        skills = storage.load_skills(enabled_only=enabled_only)
    except Exception as exc:
        logger.exception("Failed to load skills")
        raise HTTPException(status_code=500, detail=f"Failed to load skills: {exc}")

    return {
        "skills": [_skill_summary(s) for s in skills],
        "count": len(skills),
    }


@router.get("/{name}")
async def get_skill(
    name: str,
    storage: Any = Depends(_get_skill_storage),
) -> dict[str, Any]:
    """Get detailed info for a single skill."""
    try:
        storage.validate_skill_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    skills = storage.load_skills()
    for skill in skills:
        if skill.name == name:
            return {"skill": _skill_detail(skill).model_dump()}

    raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")


@router.put("/{name}")
async def toggle_skill(
    name: str,
    body: SkillToggleRequest,
    storage: Any = Depends(_get_skill_storage),
) -> dict[str, Any]:
    """Enable or disable a skill by writing to extensions_config.json."""
    try:
        storage.validate_skill_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Verify the skill exists
    skills = storage.load_skills()
    found = None
    for s in skills:
        if s.name == name:
            found = s
            break
    if found is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    # Update extensions_config.json
    config = _read_extensions_config()
    skills_cfg: dict = config.setdefault("skills", {})
    skills_cfg[name] = {"enabled": body.enabled}
    _write_extensions_config(config)

    _refresh_cache()
    _invalidate_graph_cache()  # global toggle → invalidate all users

    logger.info("Skill '%s' %s via REST API", name, "enabled" if body.enabled else "disabled")
    return {"status": "ok", "name": name, "enabled": body.enabled}


@router.post("/install")
async def install_skill(
    body: SkillInstallRequest,
    storage: Any = Depends(_get_skill_storage),
) -> dict[str, Any]:
    """Install a skill from a .skill ZIP archive."""
    archive_path = Path(body.archive_path)
    if not archive_path.exists():
        raise HTTPException(status_code=400, detail=f"Archive not found: {archive_path}")

    # Only .skill and .zip extensions
    if archive_path.suffix not in (".skill", ".zip"):
        raise HTTPException(
            status_code=400,
            detail="Archive must be a .skill or .zip file",
        )

    try:
        from harness.skills.installer import install_skill_from_archive

        skill_name = await install_skill_from_archive(
            archive_path,
            storage.get_skills_root_path(),
            category="builtin",
            force=body.force,
            model_client=None,  # No model client in REST context — write blocked
        )
        _refresh_cache()
        _invalidate_graph_cache()  # builtin install → invalidate all users
        return {"status": "installed", "name": skill_name}
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Skill installation failed")
        raise HTTPException(status_code=500, detail=f"Installation failed: {exc}")


# ═══════════════════════════════════════════════════════════════════════════
# Custom skill CRUD
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/custom")
async def list_custom_skills(
    enabled_only: bool = Query(False),
    user_id: str = Query("default", description="User ID for private skills"),
    storage: Any = Depends(_get_skill_storage),
) -> dict[str, Any]:
    """List only user-private (user-created) skills."""
    try:
        all_skills = storage.load_skills(enabled_only=enabled_only, user_id=user_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load skills: {exc}")

    custom = [s for s in all_skills if s.user_id is not None]
    return {
        "skills": [_skill_summary(s) for s in custom],
        "count": len(custom),
    }


@router.get("/custom/{name}")
async def read_custom_skill(
    name: str,
    user_id: str = Query("default", description="User ID for private skills"),
    storage: Any = Depends(_get_skill_storage),
) -> dict[str, Any]:
    """Read the full SKILL.md content of a user-private skill."""
    try:
        storage.validate_skill_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        content = storage.read_custom_skill(name, user_id=user_id)
        return {"name": name, "content": content}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Custom skill '{name}' not found")


@router.put("/custom/{name}")
async def write_custom_skill(
    name: str,
    body: SkillContentRequest,
    user_id: str = Query("default", description="User ID for private skills"),
    storage: Any = Depends(_get_skill_storage),
) -> dict[str, Any]:
    """Create or overwrite a user-private skill's SKILL.md.

    Validation + security scan are enforced.
    """
    try:
        storage.validate_skill_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    content = body.content
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content must not be empty")

    # Validate frontmatter
    import tempfile

    from harness.skills.validation import _validate_skill_frontmatter

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        md_path = tmp_path / "SKILL.md"
        md_path.write_text(content, encoding="utf-8")

        is_valid, msg, validated_name = _validate_skill_frontmatter(tmp_path)
        if not is_valid:
            raise HTTPException(status_code=400, detail=f"Validation failed: {msg}")

        if validated_name != name:
            raise HTTPException(
                status_code=400,
                detail=f"SKILL.md name '{validated_name}' does not match URL name '{name}'",
            )

    # Write
    is_new = not storage.custom_skill_exists(name, user_id=user_id)
    try:
        storage.write_custom_skill(name, "SKILL.md", content, user_id=user_id)
        storage.append_history(name, {
            "action": "create" if is_new else "edit",
            "file": "SKILL.md",
        }, user_id=user_id)
    except Exception as exc:
        logger.exception("Failed to write custom skill '%s'", name)
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}")

    _refresh_cache()
    _invalidate_graph_cache(user_id=user_id)  # user-private mutation

    logger.info(
        "Custom skill '%s' %s via REST API",
        name,
        "created" if is_new else "updated",
    )
    return {"status": "created" if is_new else "updated", "name": name}


@router.delete("/custom/{name}")
async def delete_custom_skill(
    name: str,
    user_id: str = Query("default", description="User ID for private skills"),
    storage: Any = Depends(_get_skill_storage),
) -> dict[str, Any]:
    """Delete a user-private skill, archiving content to history first."""
    try:
        storage.validate_skill_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not storage.custom_skill_exists(name, user_id=user_id):
        raise HTTPException(status_code=404, detail=f"Custom skill '{name}' not found")

    # Archive before delete
    try:
        content = storage.read_custom_skill(name, user_id=user_id)
        storage.append_history(name, {
            "action": "delete",
            "archived_content": content,
        }, user_id=user_id)
    except Exception:
        logger.warning("Failed to archive skill '%s' before delete", name)

    try:
        storage.delete_custom_skill(name, user_id=user_id)
    except Exception as exc:
        logger.exception("Failed to delete custom skill '%s'", name)
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}")

    _refresh_cache()
    _invalidate_graph_cache(user_id=user_id)  # user-private mutation

    logger.info("Custom skill '%s' deleted via REST API", name)
    return {"status": "deleted", "name": name}


@router.get("/custom/{name}/history")
async def read_skill_history(
    name: str,
    user_id: str = Query("default", description="User ID for private skills"),
    storage: Any = Depends(_get_skill_storage),
) -> dict[str, Any]:
    """Read the JSONL history for a user-private skill."""
    try:
        storage.validate_skill_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        records = storage.read_history(name, user_id=user_id)
        return {"name": name, "history": records, "count": len(records)}
    except Exception as exc:
        logger.exception("Failed to read history for '%s'", name)
        raise HTTPException(status_code=500, detail=f"Failed to read history: {exc}")


@router.post("/custom/{name}/rollback")
async def rollback_skill(
    name: str,
    body: SkillRollbackRequest,
    user_id: str = Query("default", description="User ID for private skills"),
    storage: Any = Depends(_get_skill_storage),
) -> dict[str, Any]:
    """Rollback a user-private skill to a previous version from history."""
    try:
        storage.validate_skill_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    records = storage.read_history(name, user_id=user_id)
    if not records:
        raise HTTPException(status_code=404, detail=f"No history found for skill '{name}'")

    if body.target_index >= len(records):
        raise HTTPException(
            status_code=400,
            detail=f"Target index {body.target_index} out of range (0-{len(records) - 1})",
        )

    target = records[body.target_index]
    archived = target.get("archived_content")
    if not archived:
        raise HTTPException(
            status_code=400,
            detail=f"History entry {body.target_index} does not contain archived content. "
                   f"Action was '{target.get('action', 'unknown')}'.",
        )

    # Validate + write
    import tempfile

    from harness.skills.validation import _validate_skill_frontmatter

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        md_path = tmp_path / "SKILL.md"
        md_path.write_text(archived, encoding="utf-8")

        is_valid, msg, validated_name = _validate_skill_frontmatter(tmp_path)
        if not is_valid:
            raise HTTPException(
                status_code=500,
                detail=f"Archived content failed validation: {msg}",
            )

    storage.write_custom_skill(name, "SKILL.md", archived, user_id=user_id)
    storage.append_history(name, {
        "action": "rollback",
        "from_index": body.target_index,
        "from_ts": target.get("ts", "unknown"),
    }, user_id=user_id)

    _refresh_cache()
    _invalidate_graph_cache(user_id=user_id)  # user-private mutation

    logger.info("Skill '%s' rolled back to history index %d", name, body.target_index)
    return {"status": "rolled_back", "name": name, "from_index": body.target_index}
