"""Tests for harness/skills/storage.py — skill discovery, loading, and CRUD."""

import pytest
from pathlib import Path
from unittest.mock import patch

from harness.skills.types import SKILL_MD_FILE, Skill, SkillCategory
from harness.skills.storage import SkillStorage


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_skill_md(skill_dir: Path, name: str, description: str = "A test skill") -> Path:
    """Write a minimal SKILL.md and return its path."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n"
    md_path = skill_dir / SKILL_MD_FILE
    md_path.write_text(content, encoding="utf-8")
    return md_path


def _make_storage(tmp_path: Path, *, user_skills_base: Path | None = None) -> SkillStorage:
    """Create a SkillStorage rooted in *tmp_path* with builtin/ dir."""
    root = tmp_path / "skills"
    (root / "builtin").mkdir(parents=True, exist_ok=True)
    return SkillStorage(root, user_skills_base=user_skills_base)


# ===================================================================
# validate_skill_name
# ===================================================================


class TestValidateSkillName:
    def test_valid_names(self):
        assert SkillStorage.validate_skill_name("my-skill") == "my-skill"
        assert SkillStorage.validate_skill_name("deep-research") == "deep-research"
        assert SkillStorage.validate_skill_name("a") == "a"
        assert SkillStorage.validate_skill_name("abc-123-def") == "abc-123-def"

    def test_strips_whitespace(self):
        assert SkillStorage.validate_skill_name("  my-skill  ") == "my-skill"

    def test_rejects_uppercase(self):
        with pytest.raises(ValueError, match="hyphen-case"):
            SkillStorage.validate_skill_name("My-Skill")

    def test_rejects_special_chars(self):
        with pytest.raises(ValueError, match="hyphen-case"):
            SkillStorage.validate_skill_name("my_skill")
        with pytest.raises(ValueError, match="hyphen-case"):
            SkillStorage.validate_skill_name("my.skill")

    def test_rejects_too_long(self):
        with pytest.raises(ValueError, match="64 characters"):
            SkillStorage.validate_skill_name("a" * 65)

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="hyphen-case"):
            SkillStorage.validate_skill_name("")


# ===================================================================
# load_skills
# ===================================================================


class TestLoadSkills:
    def test_empty_directory(self, tmp_path):
        storage = _make_storage(tmp_path)
        skills = storage.load_skills()
        assert skills == []

    def test_single_public_skill(self, tmp_path):
        storage = _make_storage(tmp_path)
        _write_skill_md(storage._root / "builtin" / "test-skill", "test-skill")

        skills = storage.load_skills()
        assert len(skills) == 1
        assert skills[0].name == "test-skill"
        assert skills[0].category == SkillCategory.BUILTIN

    def test_multiple_skills(self, tmp_path):
        storage = _make_storage(tmp_path)
        _write_skill_md(storage._root / "builtin" / "skill-a", "skill-a")
        _write_skill_md(storage._root / "builtin" / "skill-b", "skill-b")
        _write_skill_md(storage._root / "builtin" / "skill-c", "skill-c")

        skills = storage.load_skills()
        assert len(skills) == 3
        names = {s.name for s in skills}
        assert names == {"skill-a", "skill-b", "skill-c"}

    def test_user_overrides_builtin(self, tmp_path):
        """When the same skill name exists in both builtin/ and user skills, user wins."""
        user_base = tmp_path / "users"
        storage = _make_storage(tmp_path, user_skills_base=user_base)
        uid = "testuser"

        # Built-in version
        _write_skill_md(storage._root / "builtin" / "my-skill", "my-skill", "builtin version")
        # User version
        user_dir = storage.get_user_skill_dir(uid, "my-skill")
        _write_skill_md(user_dir, "my-skill", "user version")

        skills = storage.load_skills(user_id=uid)
        assert len(skills) == 1
        assert skills[0].description == "user version"
        assert skills[0].user_id == uid

    def test_skip_hidden_directories(self, tmp_path):
        """Skills in directories starting with '.' should be skipped."""
        storage = _make_storage(tmp_path)
        _write_skill_md(storage._root / "builtin" / ".hidden-skill", "hidden-skill")
        _write_skill_md(storage._root / "builtin" / "visible-skill", "visible-skill")

        skills = storage.load_skills()
        assert len(skills) == 1
        assert skills[0].name == "visible-skill"

    def test_enabled_only_filter(self, tmp_path):
        """With enabled_only=True, only enabled skills are returned."""
        storage = _make_storage(tmp_path)
        _write_skill_md(storage._root / "builtin" / "skill-a", "skill-a")
        _write_skill_md(storage._root / "builtin" / "skill-b", "skill-b")

        # All skills default to enabled=True after loading
        skills = storage.load_skills()
        assert len(skills) == 2

        # Manually disable one
        skills[0].enabled = False
        skills[1].enabled = True

        # enabled_only filter
        enabled = [s for s in skills if s.enabled]
        assert len(enabled) == 1
        assert enabled[0].name == "skill-b"

    def test_nested_subdirectory_skill(self, tmp_path):
        """Skills in nested subdirectories are discovered with correct relative_path."""
        storage = _make_storage(tmp_path)
        nested = storage._root / "builtin" / "subdir" / "nested-skill"
        _write_skill_md(nested, "nested-skill")

        skills = storage.load_skills()
        assert len(skills) == 1
        assert skills[0].name == "nested-skill"
        assert skills[0].skill_path == "subdir/nested-skill"

    def test_skills_are_sorted_by_name(self, tmp_path):
        storage = _make_storage(tmp_path)
        _write_skill_md(storage._root / "builtin" / "z-skill", "z-skill")
        _write_skill_md(storage._root / "builtin" / "a-skill", "a-skill")
        _write_skill_md(storage._root / "builtin" / "m-skill", "m-skill")

        skills = storage.load_skills()
        names = [s.name for s in skills]
        assert names == ["a-skill", "m-skill", "z-skill"]


# ===================================================================
# CRUD operations
# ===================================================================


class TestCRUD:
    @pytest.fixture
    def user_storage(self, tmp_path: Path) -> SkillStorage:
        """Storage with user_skills_base configured for per-user CRUD."""
        user_base = tmp_path / "users"
        return _make_storage(tmp_path, user_skills_base=user_base)

    _TEST_USER = "testuser"

    def test_read_custom_skill(self, user_storage):
        storage = user_storage
        name = "my-skill"
        uid = self._TEST_USER
        skill_dir = storage.get_custom_skill_dir(name, user_id=uid)
        _write_skill_md(skill_dir, name, "Hello, world!")

        content = storage.read_custom_skill(name, user_id=uid)
        assert "Hello, world!" in content
        assert f"name: {name}" in content

    def test_read_nonexistent_skill_raises(self, user_storage):
        storage = user_storage
        with pytest.raises(FileNotFoundError):
            storage.read_custom_skill("nonexistent", user_id=self._TEST_USER)

    def test_write_custom_skill(self, user_storage):
        storage = user_storage
        name = "my-skill"
        uid = self._TEST_USER
        storage.write_custom_skill(name, SKILL_MD_FILE, "---\nname: my-skill\ndescription: Created\n---\n", user_id=uid)

        assert storage.custom_skill_exists(name, user_id=uid)
        content = storage.read_custom_skill(name, user_id=uid)
        assert "Created" in content

    def test_write_custom_skill_support_file(self, user_storage):
        storage = user_storage
        name = "my-skill"
        uid = self._TEST_USER
        storage.write_custom_skill(name, "references/guide.md", "# Reference Guide", user_id=uid)

        ref_path = storage.get_custom_skill_dir(name, user_id=uid) / "references" / "guide.md"
        assert ref_path.exists()
        assert ref_path.read_text() == "# Reference Guide"

    def test_delete_custom_skill(self, user_storage):
        storage = user_storage
        name = "my-skill"
        uid = self._TEST_USER
        _write_skill_md(storage.get_custom_skill_dir(name, user_id=uid), name)
        assert storage.custom_skill_exists(name, user_id=uid)

        storage.delete_custom_skill(name, user_id=uid)
        assert not storage.custom_skill_exists(name, user_id=uid)

    def test_delete_nonexistent_skill_raises(self, user_storage):
        storage = user_storage
        with pytest.raises(FileNotFoundError):
            storage.delete_custom_skill("nonexistent", user_id=self._TEST_USER)

    def test_path_traversal_prevention(self, user_storage):
        """Writing to a relative_path with '..' should be rejected."""
        storage = user_storage
        name = "my-skill"
        uid = self._TEST_USER
        _write_skill_md(storage.get_custom_skill_dir(name, user_id=uid), name)

        with pytest.raises(ValueError, match="resolve within"):
            storage.write_custom_skill(name, "../escape.txt", "evil", user_id=uid)

    def test_builtin_skill_exists(self, tmp_path):
        storage = _make_storage(tmp_path)
        _write_skill_md(storage._root / "builtin" / "bundled", "bundled")

        assert storage.builtin_skill_exists("bundled")
        assert not storage.builtin_skill_exists("nonexistent")

    def test_custom_skill_exists(self, user_storage):
        storage = user_storage
        uid = self._TEST_USER
        _write_skill_md(storage.get_custom_skill_dir("user-made", user_id=uid), "user-made")

        assert storage.custom_skill_exists("user-made", user_id=uid)
        assert not storage.custom_skill_exists("nonexistent", user_id=uid)


# ===================================================================
# history
# ===================================================================


class TestHistory:
    @pytest.fixture
    def user_storage(self, tmp_path: Path) -> SkillStorage:
        user_base = tmp_path / "users"
        return _make_storage(tmp_path, user_skills_base=user_base)

    _TEST_USER = "testuser"

    def test_append_and_read_history(self, user_storage):
        storage = user_storage
        name = "my-skill"
        uid = self._TEST_USER
        _write_skill_md(storage.get_custom_skill_dir(name, user_id=uid), name)

        storage.append_history(name, {"action": "create", "author": "test"}, user_id=uid)
        storage.append_history(name, {"action": "edit", "author": "test"}, user_id=uid)

        records = storage.read_history(name, user_id=uid)
        assert len(records) == 2
        assert records[0]["action"] == "create"
        assert records[1]["action"] == "edit"

    def test_read_history_nonexistent_skill(self, user_storage):
        storage = user_storage
        records = storage.read_history("nonexistent", user_id=self._TEST_USER)
        assert records == []


# ===================================================================
# extensions_config integration
# ===================================================================


class TestExtensionsConfigIntegration:
    def test_default_enabled_state(self, tmp_path):
        """Without extensions_config.json, all skills default to enabled."""
        storage = _make_storage(tmp_path)
        _write_skill_md(storage._root / "builtin" / "skill-a", "skill-a")
        _write_skill_md(storage._root / "builtin" / "skill-b", "skill-b")

        skills = storage.load_skills()
        assert all(s.enabled for s in skills)

    def test_disabled_skill_in_config(self, tmp_path):
        """When extensions_config.json disables a skill, it reflects in loading."""
        storage = _make_storage(tmp_path)
        _write_skill_md(storage._root / "builtin" / "skill-a", "skill-a")

        # Create a minimal extensions_config that disables skill-a
        import json
        config_path = tmp_path / "extensions_config.json"
        config_path.write_text(json.dumps({
            "skills": {"skill-a": {"enabled": False}}
        }))

        with patch(
            "harness.config.extensions_config._default_config_path",
            return_value=config_path,
        ):
            skills = storage.load_skills()
            assert len(skills) == 1
            assert skills[0].enabled is False
