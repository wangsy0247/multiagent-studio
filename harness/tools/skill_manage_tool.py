"""Agent self-management tool for skills — create, edit, patch, delete.

Allows the Lead Agent (and authorised subagents) to manage custom skills
programmatically.  Built-in ``public/`` skills are read-only — only skills
under ``custom/`` can be mutated.

Every write operation follows the pipeline:
1. Acquire per-skill lock (via SkillStorage's atomic writes)
2. Validate frontmatter
3. Security scan content
4. Atomic write via SkillStorage
5. Append JSONL history
6. Refresh prompt cache
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool

logger = logging.getLogger(__name__)

# Context var set by HarnessService before graph execution.
# When not set (tests, legacy), skill_manage operates on project shared skills.
_skill_user_id: ContextVar[str | None] = ContextVar("skill_user_id", default=None)


def set_skill_user_id(user_id: str | None) -> None:
    """Set the current user for skill management operations."""
    _skill_user_id.set(user_id)

# ---------------------------------------------------------------------------
# Valid action types
# ---------------------------------------------------------------------------

ActionType = Literal[
    "create",       # Create a new custom skill from full SKILL.md content
    "edit",         # Replace the entire SKILL.md of an existing custom skill
    "patch",        # Partial update — append/replace sections of SKILL.md
    "delete",       # Delete a custom skill entirely
    "write_file",   # Write a support file under references/ templates/ scripts/ assets/
    "remove_file",  # Remove a support file
]


def create_skill_manage_tool(
    *,
    skill_storage: Any | None = None,
    model_client: Any | None = None,
) -> BaseTool:
    """Create the ``skill_manage`` tool.

    Args:
        skill_storage: ``SkillStorage`` instance for CRUD operations.
        model_client: LLM client for security scanning.  When ``None``,
            all write operations are blocked (security-first).

    Returns:
        A LangChain ``BaseTool`` that the agent can call.
    """

    async def _do_security_scan(content: str, *, executable: bool = False) -> None:
        """Run security scan and raise ValueError if blocked."""
        if model_client is None:
            raise ValueError(
                "Security scanner unavailable — write operations are blocked. "
                "Configure a model_client to enable skill management."
            )
        from harness.skills.security_scanner import scan_skill_content

        result = await scan_skill_content(
            content, executable=executable, model_client=model_client,
        )
        if result.is_blocked:
            raise ValueError(f"Security scan blocked: {result.reason}")

    async def _validate_and_scan_skill_md(content: str) -> tuple[str, str]:
        """Validate frontmatter + security scan SKILL.md content.

        Returns (skill_name, validated_content).
        """
        import tempfile
        from pathlib import Path

        from harness.skills.validation import _validate_skill_frontmatter

        # Write to a temp dir so _validate_skill_frontmatter can find SKILL.md
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            md_path = tmp_path / "SKILL.md"
            md_path.write_text(content, encoding="utf-8")

            is_valid, msg, skill_name = _validate_skill_frontmatter(tmp_path)
            if not is_valid:
                raise ValueError(f"Frontmatter validation failed: {msg}")

        # Security scan (non-executable mode for SKILL.md)
        await _do_security_scan(content, executable=False)

        assert skill_name is not None
        return skill_name, content

    @tool
    async def skill_manage(
        action: ActionType,
        name: str,
        content: str = "",
        relative_path: str = "",
        patch_operation: Literal["append", "replace_section"] = "append",
        patch_target: str = "",
    ) -> str:
        """Manage custom skills — create, edit, patch, delete, and manage support files.

        Built-in skills under public/ are READ-ONLY.  All write operations
        go through validation + security scanning.

        Args:
            action: The operation to perform.
                - create: Create a new custom skill.  ``content`` = full SKILL.md.
                - edit: Replace an existing custom skill's SKILL.md.  ``content`` = new SKILL.md.
                - patch: Partial update.  ``patch_operation`` controls how.
                - delete: Remove a custom skill entirely.
                - write_file: Write a support file.  ``relative_path`` is relative to the skill dir.
                - remove_file: Remove a support file.  ``relative_path`` is relative to the skill dir.
            name: Skill name (hyphen-case, e.g. "my-workflow").
            content: SKILL.md content (for create/edit) or file content (for write_file).
            relative_path: Support file path relative to the skill directory
                (e.g. "references/my_doc.md" or "scripts/helper.sh").
                Must be under references/, templates/, scripts/, or assets/.
            patch_operation: How to apply the patch — "append" (add to end) or
                "replace_section" (replace lines between markers).
            patch_target: For replace_section — the target section heading (e.g. "## Workflow").
        """
        if skill_storage is None:
            return "Error: Skill storage is not initialised."

        try:
            skill_storage.validate_skill_name(name)
        except ValueError as e:
            return f"Error: Invalid skill name '{name}' — {e}"

        # ── Dispatch by action ──
        try:
            if action == "create":
                return await _handle_create(name, content)
            elif action == "edit":
                return await _handle_edit(name, content)
            elif action == "patch":
                return await _handle_patch(name, content, patch_operation, patch_target)
            elif action == "delete":
                return await _handle_delete(name)
            elif action == "write_file":
                return await _handle_write_file(name, relative_path, content)
            elif action == "remove_file":
                return await _handle_remove_file(name, relative_path)
            else:
                return f"Error: Unknown action '{action}'"
        except ValueError as e:
            return f"Error: {e}"
        except FileNotFoundError as e:
            return f"Error: {e}"
        except Exception:
            logger.exception("Unexpected error in skill_manage action=%s name=%s", action, name)
            return f"Error: Internal error processing '{action}' on '{name}'."

    # ═════════════════════════════════════════════════════════════════════
    # Action handlers
    # ═════════════════════════════════════════════════════════════════════

    def _uid() -> str | None:
        """Return the current user_id for skill operations (from context var)."""
        return _skill_user_id.get()

    async def _handle_create(name: str, content: str) -> str:
        if not content.strip():
            return "Error: 'content' is required for create action."

        uid = _uid()
        # Check not already exists
        if skill_storage.custom_skill_exists(name, user_id=uid):
            return f"Error: Custom skill '{name}' already exists. Use edit to modify it."

        # Validate + scan
        validated_name, _ = await _validate_and_scan_skill_md(content)
        if validated_name != name:
            return (
                f"Error: SKILL.md frontmatter name '{validated_name}' does not "
                f"match requested name '{name}'."
            )

        # Write
        skill_storage.write_custom_skill(name, "SKILL.md", content, user_id=uid)
        skill_storage.append_history(name, {
            "action": "create",
            "file": "SKILL.md",
        }, user_id=uid)

        # Refresh cache
        _refresh_cache()

        logger.info("skill_manage: created custom skill '%s' (user=%s)", name, uid)
        return f"Custom skill '{name}' created successfully."

    async def _handle_edit(name: str, content: str) -> str:
        if not content.strip():
            return "Error: 'content' is required for edit action."

        uid = _uid()
        if not skill_storage.custom_skill_exists(name, user_id=uid):
            return f"Error: Custom skill '{name}' not found. Use create to make a new one."

        # Validate + scan
        validated_name, _ = await _validate_and_scan_skill_md(content)
        if validated_name != name:
            return (
                f"Error: SKILL.md frontmatter name '{validated_name}' does not "
                f"match requested name '{name}'."
            )

        # Save old content for history
        try:
            old_content = skill_storage.read_custom_skill(name, user_id=uid)
        except FileNotFoundError:
            old_content = ""

        # Write
        skill_storage.write_custom_skill(name, "SKILL.md", content, user_id=uid)
        skill_storage.append_history(name, {
            "action": "edit",
            "file": "SKILL.md",
            "old_length": len(old_content),
            "new_length": len(content),
        }, user_id=uid)

        _refresh_cache()

        logger.info("skill_manage: edited custom skill '%s' (user=%s)", name, uid)
        return f"Custom skill '{name}' updated successfully."

    async def _handle_patch(
        name: str,
        content: str,
        operation: str,
        target: str,
    ) -> str:
        if not content.strip():
            return "Error: 'content' is required for patch action."

        uid = _uid()
        if not skill_storage.custom_skill_exists(name, user_id=uid):
            return f"Error: Custom skill '{name}' not found."

        try:
            current = skill_storage.read_custom_skill(name, user_id=uid)
        except FileNotFoundError:
            return f"Error: Cannot read SKILL.md for '{name}'."

        if operation == "append":
            new_content = current.rstrip() + "\n\n" + content
        elif operation == "replace_section":
            if not target:
                return "Error: 'patch_target' is required for replace_section operation."
            new_content = _replace_markdown_section(current, target, content)
            if new_content == current:
                return f"Error: Section '{target}' not found in SKILL.md."
        else:
            return f"Error: Unknown patch_operation '{operation}'."

        # Validate + scan the new content
        validated_name, _ = await _validate_and_scan_skill_md(new_content)
        if validated_name != name:
            return (
                f"Error: Patch would change skill name from '{name}' to "
                f"'{validated_name}' — not allowed."
            )

        skill_storage.write_custom_skill(name, "SKILL.md", new_content, user_id=uid)
        skill_storage.append_history(name, {
            "action": "patch",
            "operation": operation,
            "target": target,
            "patch_length": len(content),
        }, user_id=uid)

        _refresh_cache()

        logger.info("skill_manage: patched custom skill '%s' (op=%s, user=%s)", name, operation, uid)
        return f"Custom skill '{name}' patched successfully ({operation})."

    async def _handle_delete(name: str) -> str:
        uid = _uid()
        if not skill_storage.custom_skill_exists(name, user_id=uid):
            return f"Error: Custom skill '{name}' not found."

        # Archive before delete — save current SKILL.md to history
        try:
            current = skill_storage.read_custom_skill(name, user_id=uid)
            skill_storage.append_history(name, {
                "action": "delete",
                "archived_content": current,
            }, user_id=uid)
        except Exception:
            logger.warning("Failed to archive skill '%s' before delete", name)

        skill_storage.delete_custom_skill(name, user_id=uid)

        _refresh_cache()

        logger.info("skill_manage: deleted custom skill '%s' (user=%s)", name, uid)
        return f"Custom skill '{name}' deleted successfully."

    async def _handle_write_file(name: str, relative_path: str, content: str) -> str:
        if not relative_path.strip():
            return "Error: 'relative_path' is required for write_file action."
        if not content.strip():
            return "Error: 'content' is required for write_file action."

        uid = _uid()
        # Validate support file path
        from harness.skills.installer import ensure_safe_support_path

        try:
            safe_path = ensure_safe_support_path(relative_path)
        except ValueError as e:
            return f"Error: Invalid path — {e}"

        # Security scan — executable=true for scripts/
        is_executable = str(safe_path).startswith("scripts/")
        await _do_security_scan(content, executable=is_executable)

        # Write
        skill_storage.write_custom_skill(name, str(safe_path), content, user_id=uid)
        skill_storage.append_history(name, {
            "action": "write_file",
            "file": str(safe_path),
            "length": len(content),
        }, user_id=uid)

        _refresh_cache()

        logger.info(
            "skill_manage: wrote support file '%s' in skill '%s' (user=%s)",
            safe_path, name, uid,
        )
        return f"Support file '{safe_path}' written in skill '{name}'."

    async def _handle_remove_file(name: str, relative_path: str) -> str:
        if not relative_path.strip():
            return "Error: 'relative_path' is required for remove_file action."

        uid = _uid()
        from harness.skills.installer import ensure_safe_support_path

        try:
            safe_path = ensure_safe_support_path(relative_path)
        except ValueError as e:
            return f"Error: Invalid path — {e}"

        skill_dir = skill_storage.get_custom_skill_dir(name, user_id=uid)
        target = skill_dir / safe_path

        if not target.exists():
            return f"Error: File '{safe_path}' not found in skill '{name}'."

        target.unlink()
        skill_storage.append_history(name, {
            "action": "remove_file",
            "file": str(safe_path),
        }, user_id=uid)

        _refresh_cache()

        logger.info(
            "skill_manage: removed support file '%s' from skill '%s' (user=%s)",
            safe_path, name, uid,
        )
        return f"Support file '{safe_path}' removed from skill '{name}'."

    # ═════════════════════════════════════════════════════════════════════
    # Helpers
    # ═════════════════════════════════════════════════════════════════════

    def _refresh_cache() -> None:
        """Refresh the skills prompt cache after any mutation."""
        try:
            from harness.skills.cache import refresh_skills_system_prompt_cache

            refresh_skills_system_prompt_cache()
        except Exception:
            logger.warning("Failed to refresh skills prompt cache", exc_info=True)

    return skill_manage


def _replace_markdown_section(
    document: str,
    section_heading: str,
    new_content: str,
) -> str:
    """Replace a Markdown section identified by its heading.

    Finds the heading (any level, e.g. ``## Workflow``), then replaces
    everything from that heading to the next heading of the same or higher
    level, or end of document.

    Args:
        document: Full Markdown document.
        section_heading: The heading text to match (without the leading ``#``).
        new_content: Replacement content (inserted after the heading line).

    Returns:
        Modified document, or the original if the section was not found.
    """
    import re

    # Escape for regex
    escaped = re.escape(section_heading.strip())
    # Match the heading line: optional #s, the heading text, optional trailing #s
    pattern = re.compile(
        rf"^(#+\s*{escaped}\s*#*\s*\n)",
        re.MULTILINE | re.IGNORECASE,
    )

    match = pattern.search(document)
    if not match:
        return document

    heading_line = match.group(1)
    content_start = match.end()

    # Determine heading level
    level = len(heading_line.lstrip()) - len(heading_line.lstrip().lstrip("#"))
    heading_prefix = heading_line.lstrip()[:level]

    # Find the next heading of the same or higher level
    next_heading_pattern = re.compile(
        rf"^{heading_prefix}\s+",
        re.MULTILINE,
    )

    remaining = document[content_start:]
    next_match = next_heading_pattern.search(remaining)

    if next_match:
        end = content_start + next_match.start()
    else:
        end = len(document)

    return document[:content_start] + "\n" + new_content.strip() + "\n\n" + document[end:]
