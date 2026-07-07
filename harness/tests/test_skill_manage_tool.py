"""Tests for the skill_manage agent tool — create, edit, patch, delete, file management."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from harness.skills.types import SKILL_MD_FILE


# ===================================================================
# Helpers
# ===================================================================


_VALID_SKILL_MD = """---
name: {name}
description: A test skill for {name}.
license: MIT
---
# {name}

This is a test skill.
"""


def _make_storage(tmp_path):
    """Create a real SkillStorage pointed at tmp_path."""
    from harness.skills.storage import SkillStorage

    root = tmp_path / "skills"
    (root / "public").mkdir(parents=True)
    (root / "custom").mkdir(parents=True)
    return SkillStorage(root)


def _create_custom_skill(storage, name, content=None):
    """Helper: create a custom skill via storage."""
    if content is None:
        content = _VALID_SKILL_MD.format(name=name)
    storage.write_custom_skill(name, SKILL_MD_FILE, content)
    # Create the SKILL.md file for parsing
    skill_dir = storage.get_custom_skill_dir(name)
    md_path = skill_dir / SKILL_MD_FILE
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(content)
    return name


async def _call_tool(tool, **kwargs):
    """Call the async LangChain tool and return its string result."""
    return await tool.ainvoke(kwargs)


# ===================================================================
# create action
# ===================================================================


class TestSkillManageCreate:
    @pytest.mark.asyncio
    async def test_create_new_skill(self, tmp_path):
        """Create a new custom skill via the tool."""
        storage = _make_storage(tmp_path)
        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(
            tool,
            action="create",
            name="my-test",
            content=_VALID_SKILL_MD.format(name="my-test"),
        )
        assert "created successfully" in result
        assert storage.custom_skill_exists("my-test")

    @pytest.mark.asyncio
    async def test_create_duplicate_skill_fails(self, tmp_path):
        """Creating a skill that already exists returns an error."""
        storage = _make_storage(tmp_path)
        _create_custom_skill(storage, "my-test")

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(
            tool,
            action="create",
            name="my-test",
            content=_VALID_SKILL_MD.format(name="my-test"),
        )
        assert "already exists" in result

    @pytest.mark.asyncio
    async def test_create_with_name_mismatch_fails(self, tmp_path):
        """Content frontmatter name must match the requested name."""
        storage = _make_storage(tmp_path)

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(
            tool,
            action="create",
            name="wanted-name",
            content=_VALID_SKILL_MD.format(name="different-name"),
        )
        assert "does not match" in result.lower()

    @pytest.mark.asyncio
    async def test_create_with_invalid_frontmatter_fails(self, tmp_path):
        """Content with invalid frontmatter is rejected."""
        storage = _make_storage(tmp_path)

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(
            tool,
            action="create",
            name="bad-skill",
            content="Just some markdown, no frontmatter",
        )
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_create_without_storage_fails(self):
        """Without skill_storage, tool returns error."""
        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=None)
        result = await _call_tool(
            tool,
            action="create",
            name="test",
            content=_VALID_SKILL_MD.format(name="test"),
        )
        assert "not initialised" in result.lower()

    @pytest.mark.asyncio
    async def test_create_adds_history(self, tmp_path):
        """Creating a skill appends a history record."""
        storage = _make_storage(tmp_path)
        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        await _call_tool(
            tool,
            action="create",
            name="my-test",
            content=_VALID_SKILL_MD.format(name="my-test"),
        )
        history = storage.read_history("my-test")
        assert len(history) >= 1
        assert history[0]["action"] == "create"


# ===================================================================
# edit action
# ===================================================================


class TestSkillManageEdit:
    @pytest.mark.asyncio
    async def test_edit_existing_skill(self, tmp_path):
        """Edit an existing custom skill."""
        storage = _make_storage(tmp_path)
        _create_custom_skill(storage, "my-test")

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)

        new_content = """---
name: my-test
description: Updated description.
license: MIT
---
# Updated

New content."""
        result = await _call_tool(
            tool, action="edit", name="my-test", content=new_content,
        )
        assert "updated successfully" in result.lower()
        saved = storage.read_custom_skill("my-test")
        assert "New content" in saved

    @pytest.mark.asyncio
    async def test_edit_nonexistent_skill_fails(self, tmp_path):
        """Editing a non-existent skill returns error."""
        storage = _make_storage(tmp_path)

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(
            tool, action="edit", name="no-such", content=_VALID_SKILL_MD.format(name="no-such"),
        )
        assert "not found" in result.lower()


# ===================================================================
# patch action
# ===================================================================


class TestSkillManagePatch:
    @pytest.mark.asyncio
    async def test_patch_append(self, tmp_path):
        """Patch-append adds content to the end of SKILL.md."""
        storage = _make_storage(tmp_path)
        _create_custom_skill(storage, "my-test")

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)

        result = await _call_tool(
            tool,
            action="patch",
            name="my-test",
            content="## New Section\n\nAdded content.",
            patch_operation="append",
        )
        assert "patched successfully" in result.lower()
        saved = storage.read_custom_skill("my-test")
        assert "Added content" in saved

    @pytest.mark.asyncio
    async def test_patch_replace_section(self, tmp_path):
        """Patch-replace_section replaces a markdown section."""
        content = """---
name: my-test
description: A test skill.
---
# Test

## Workflow

Old workflow content here.

## Usage

Use it like this."""
        storage = _make_storage(tmp_path)
        _create_custom_skill(storage, "my-test", content=content)

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)

        result = await _call_tool(
            tool,
            action="patch",
            name="my-test",
            content="New workflow content.",
            patch_operation="replace_section",
            patch_target="Workflow",
        )
        assert "patched successfully" in result.lower()
        saved = storage.read_custom_skill("my-test")
        assert "New workflow content" in saved
        assert "Old workflow content" not in saved

    @pytest.mark.asyncio
    async def test_patch_nonexistent_section(self, tmp_path):
        """Patching a section that doesn't exist returns error."""
        storage = _make_storage(tmp_path)
        _create_custom_skill(storage, "my-test")

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(
            tool,
            action="patch",
            name="my-test",
            content="New stuff",
            patch_operation="replace_section",
            patch_target="NonExistentSection",
        )
        assert "Error" in result


# ===================================================================
# delete action
# ===================================================================


class TestSkillManageDelete:
    @pytest.mark.asyncio
    async def test_delete_existing_skill(self, tmp_path):
        """Delete a custom skill."""
        storage = _make_storage(tmp_path)
        _create_custom_skill(storage, "my-test")

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(tool, action="delete", name="my-test")
        assert "deleted successfully" in result.lower()
        assert not storage.custom_skill_exists("my-test")

    @pytest.mark.asyncio
    async def test_delete_nonexistent_skill_fails(self, tmp_path):
        """Deleting a non-existent skill returns error."""
        storage = _make_storage(tmp_path)

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(tool, action="delete", name="no-such")
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_delete_archives_content_to_history(self, tmp_path):
        """Deleting a skill archives its content to history."""
        storage = _make_storage(tmp_path)
        _create_custom_skill(storage, "my-test")

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        await _call_tool(tool, action="delete", name="my-test")

        history = storage.read_history("my-test")
        delete_entries = [h for h in history if h["action"] == "delete"]
        assert len(delete_entries) >= 1
        assert "archived_content" in delete_entries[0]


# ===================================================================
# write_file / remove_file actions
# ===================================================================


class TestSkillManageFiles:
    @pytest.mark.asyncio
    async def test_write_support_file(self, tmp_path):
        """Write a references file under a custom skill."""
        storage = _make_storage(tmp_path)
        _create_custom_skill(storage, "my-test")

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(
            tool,
            action="write_file",
            name="my-test",
            relative_path="references/guide.md",
            content="# Guide\n\nReference content.",
        )
        assert "written" in result.lower()
        skill_dir = storage.get_custom_skill_dir("my-test")
        written = skill_dir / "references" / "guide.md"
        assert written.exists()
        assert written.read_text() == "# Guide\n\nReference content."

    @pytest.mark.asyncio
    async def test_write_file_path_traversal_blocked(self, tmp_path):
        """Writing outside allowed directories is blocked."""
        storage = _make_storage(tmp_path)
        _create_custom_skill(storage, "my-test")

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(
            tool,
            action="write_file",
            name="my-test",
            relative_path="../../../etc/hosts",
            content="evil",
        )
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_remove_support_file(self, tmp_path):
        """Remove a support file from a custom skill."""
        storage = _make_storage(tmp_path)
        _create_custom_skill(storage, "my-test")
        # First write a file
        storage.write_custom_skill("my-test", "references/temp.md", "temp")

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(
            tool,
            action="remove_file",
            name="my-test",
            relative_path="references/temp.md",
        )
        assert "removed" in result.lower()
        skill_dir = storage.get_custom_skill_dir("my-test")
        assert not (skill_dir / "references" / "temp.md").exists()

    @pytest.mark.asyncio
    async def test_remove_nonexistent_file_fails(self, tmp_path):
        storage = _make_storage(tmp_path)
        _create_custom_skill(storage, "my-test")

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(
            tool,
            action="remove_file",
            name="my-test",
            relative_path="references/does_not_exist.md",
        )
        assert "not found" in result.lower()


# ===================================================================
# Edge cases & error handling
# ===================================================================


class TestSkillManageEdgeCases:
    @pytest.mark.asyncio
    async def test_invalid_skill_name_rejected(self, tmp_path):
        """Names that don't match the pattern are rejected."""
        storage = _make_storage(tmp_path)

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(
            tool,
            action="create",
            name="Invalid Name With Spaces!",
            content=_VALID_SKILL_MD.format(name="invalid-name"),
        )
        assert "Invalid skill name" in result or "Error" in result

    @pytest.mark.asyncio
    async def test_unknown_action(self, tmp_path):
        storage = _make_storage(tmp_path)

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(tool, action="nonexistent_action", name="test")
        assert "Error" in result or "Unknown action" in result

    @pytest.mark.asyncio
    async def test_create_with_empty_content(self, tmp_path):
        storage = _make_storage(tmp_path)

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(tool, action="create", name="test", content="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_edit_with_empty_content(self, tmp_path):
        storage = _make_storage(tmp_path)
        _create_custom_skill(storage, "my-test")

        from harness.tools.skill_manage_tool import create_skill_manage_tool

        tool = create_skill_manage_tool(skill_storage=storage)
        result = await _call_tool(tool, action="edit", name="my-test", content="")
        assert "Error" in result


# ===================================================================
# _replace_markdown_section helper
# ===================================================================


class TestReplaceMarkdownSection:
    def test_replace_existing_section(self):
        from harness.tools.skill_manage_tool import _replace_markdown_section

        doc = """# Title

## Section A

Content A here.

## Section B

Content B here.

## Section C

Content C here."""

        result = _replace_markdown_section(doc, "Section B", "REPLACED")
        assert "REPLACED" in result
        assert "Content B here" not in result
        assert "Content A here" in result  # preserved
        assert "Content C here" in result  # preserved

    def test_replace_last_section(self):
        from harness.tools.skill_manage_tool import _replace_markdown_section

        doc = """# Title

## Workflow

Old workflow.
"""
        result = _replace_markdown_section(doc, "Workflow", "New workflow.")
        assert "New workflow" in result
        assert "Old workflow" not in result

    def test_section_not_found_unchanged(self):
        from harness.tools.skill_manage_tool import _replace_markdown_section

        doc = """# Title

## Real Section

Content.
"""
        result = _replace_markdown_section(doc, "NonExistent", "New")
        assert result == doc

    def test_case_insensitive_match(self):
        from harness.tools.skill_manage_tool import _replace_markdown_section

        doc = """## WorkFlow

Content.
"""
        result = _replace_markdown_section(doc, "Workflow", "New.")
        assert "New" in result
